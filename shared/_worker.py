"""Bounded thread pool with retries, timeouts, owner-thread dispatch, and asyncio `Loop`.

    with worker.Pool(concurrency=6) as pool:
        pool.submit(download, url)
        for result in pool.as_completed():
            ...
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import os
import random
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Generic, Iterator, NamedTuple, TypeVar

import shared
from shared import _clock as clock
from shared._ui import ui

__all__ = [
    "ClosedError",
    "Error",
    "Loop",
    "Pool",
    "Result",
    "StalledError",
]

# The longest wait between two tries, which caps the exponential backoff at one minute.
MAX_RETRY_WAIT = 60.0
# Waits wake at least this often, so Ctrl-C is seen on platforms where a blocked lock ignores it.
WAKE_INTERVAL = 0.25

R = TypeVar("R")


@dataclass
class Result(Generic[R]):
    """Outcome record for a single completed task."""

    item: Any
    value: R | None = None
    error: BaseException | None = None
    attempts: int = 0
    _position: int = field(default=0, init=False, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        """Whether the task succeeded without error."""
        return self.error is None


class Error(shared.Error):
    """Base exception for all `worker` errors."""


class StalledError(Error, TimeoutError):
    """Raised when a task exceeds its per-attempt `timeout`."""


class ClosedError(Error, RuntimeError):
    """Raised when submitting to or running on a closed `Pool` or `Loop`."""


class Pool:
    """Reusable bounded thread pool with retries, timeouts, owner-thread dispatch, and stop."""

    def __init__(
        self,
        *,
        concurrency: int | None = None,
        retries: int = 0,
        timeout: float | None = None,
        progress: Callable[[int, int], Any] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self._run = _Run(
            concurrency=concurrency,
            retries=retries,
            timeout=timeout,
            progress=progress,
            stop=stop,
        )

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        """Enqueue `fn(*args)` for execution in the pool."""
        self._run.submit(fn, args)

    def as_completed(self) -> Iterator[Result[Any]]:
        """Yield `Result` objects as submitted tasks finish."""
        run = self._run
        seen = 0
        while True:
            run.wait(
                run.done_cond,
                lambda: len(run.finished) > seen or run.is_idle() or run.ended,
            )
            with run.lock:
                fresh = run.finished[seen:]
                done = run.is_idle() or run.ended
            if fresh:
                seen += len(fresh)
                yield from fresh
            elif done:
                return

    def join(self, timeout: float | None = None) -> list[Result[Any]]:
        """Wait for all submitted tasks to finish and return their `Result` list."""
        run = self._run
        if run.wait(run.done_cond, run.is_idle, timeout) and run.closed:
            run.end(None)
        with run.lock:
            return list(run.finished)

    def run_on_owner(self, fn: Callable[[], R]) -> R:
        """Execute `fn()` on the thread that owns the pool."""
        return self._run.run_on_owner(fn)

    def stop(self, error: BaseException | None = None) -> None:
        """Request immediate shutdown and cancel queued tasks."""
        self._run.stop(error)
        self._run.end(error)

    def close(self) -> None:
        """Shut down the pool and release worker threads."""
        self._run.close()

    @property
    def raw(self) -> concurrent.futures.Executor:
        """Underlying `concurrent.futures.Executor`."""
        return self._run.ensure_executor()

    def __enter__(self) -> Pool:
        return self

    def __exit__(
        self, error_type: type[BaseException] | None, error: BaseException | None, traceback: Any
    ) -> None:
        self._run.__exit__(error_type, error, traceback)

    def __del__(self) -> None:
        run = getattr(self, "_run", None)
        if run is not None and not sys.is_finalizing() and not run.ended:
            unfinished = run.submitted - len(run.finished)
            if unfinished > 0:
                ui.warn(f"worker.Pool was never joined; {unfinished} tasks may not have finished")
            run.close()


class Loop:
    """Dedicated background `asyncio` event-loop runner for sync callers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def raw(self) -> asyncio.AbstractEventLoop | None:
        """Underlying `asyncio.AbstractEventLoop`, or `None` before first use / after close."""
        return self._loop

    def run(self, coro: Coroutine[Any, Any, R], *, timeout: float | None = None) -> R:
        """Run `coro` to completion on the background loop and return its result."""
        loop = self._ensure_loop(coro)
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            _wait_future(future, deadline=deadline, timeout=timeout)
        except KeyboardInterrupt:
            future.cancel()
            raise
        try:
            return future.result()
        except (concurrent.futures.CancelledError, asyncio.CancelledError):
            raise Error("task was cancelled") from None

    def close(self) -> None:
        """Stop the background event loop and join its thread."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
            self._loop = None
            self._thread = None
        if loop is not None:
            _logging_failure(loop.call_soon_threadsafe, loop.stop)
        if thread is not None:
            thread.join()

    def _ensure_loop(self, coro: Coroutine[Any, Any, Any]) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed:
                coro.close()
                raise ClosedError("loop is closed")
            if self._loop is None:
                self._loop, self._thread = _start_loop_thread()
            return self._loop

    def __enter__(self) -> Loop:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __del__(self) -> None:
        if not sys.is_finalizing() and not self._closed:
            self.close()


class _Job(NamedTuple):
    """One queued task: the function, its arguments, and its position in the input."""

    fn: Callable[..., Any]
    args: tuple[Any, ...]
    item: Any
    position: int


class _OwnerCall:
    """One function marshaled from a worker thread to the pool's owner thread."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class _Run:
    """Internal queue, runner threads, retries, timeouts, and stop coordination."""

    def __init__(
        self,
        *,
        concurrency: int | None = None,
        retries: int = 0,
        timeout: float | None = None,
        progress: Callable[[int, int], Any] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        if concurrency is not None and concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, not {concurrency}")
        if retries < 0:
            raise ValueError(f"retries must be 0 or more, not {retries}")
        if timeout is not None and timeout <= 0:
            raise ValueError(f"timeout must be above 0, not {timeout}")
        if progress is not None and not callable(progress):
            raise TypeError(f"progress must be a progress(done, total) callback, not {progress!r}")
        cpus = os.cpu_count() or 1
        self.concurrency = concurrency or min(32, cpus + 4)
        self.retries, self.timeout = retries, timeout
        self.lock = threading.Lock()
        self.room_cond = threading.Condition(self.lock)
        self.work_cond = threading.Condition(self.lock)
        self.done_cond = threading.Condition(self.lock)
        self.queued: deque[_Job] = deque()
        self.finished: list[Result[Any]] = []
        self.submitted = self.busy = self.running = self.runners = 0
        self.closed = self.ended = False
        self.stop_requested = stop if stop is not None else threading.Event()
        self.task_interrupt: BaseException | None = None
        self._owner_id = threading.get_ident()
        self._owner_accepting = True
        self._owner_calls: deque[_OwnerCall] = deque()
        self._worker_ids: set[int] = set()
        self._runner_threads: list[threading.Thread] = []
        self._idle_workers: deque[tuple[threading.Event, list[Any]]] = deque()
        self.executor: concurrent.futures.ThreadPoolExecutor | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self.progress = progress
        self.reporting = threading.Lock()
        self.reported = 0

    def __enter__(self) -> _Run:
        return self

    def __exit__(
        self, error_type: type[BaseException] | None, error: BaseException | None, traceback: Any
    ) -> None:
        if error is not None:
            self.stop(error)
        else:
            try:
                self.close()
                self.wait(self.done_cond, self.is_idle)
            except BaseException as late:
                self.stop(late)
                self.end(late)
                raise
        self.end(None if isinstance(error, GeneratorExit) else error)

    def ensure_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """Return the underlying `ThreadPoolExecutor`, creating it on first use."""
        with self.lock:
            if self.executor is None:
                self.executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self.concurrency, thread_name_prefix="shared-worker"
                )
            return self.executor

    def has_room(self) -> bool:
        """Whether another task may be queued: fewer than twice `concurrency` are waiting."""
        return len(self.queued) < 2 * self.concurrency

    def is_idle(self) -> bool:
        """Whether nothing is queued and no runner holds a task."""
        return not self.queued and self.busy == 0

    def submit(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
        """Queue one task, waiting while the queue is full unless called from a worker."""
        with self.lock:
            if self.closed:
                raise ClosedError("this pool was closed: no more tasks can be submitted")
            is_worker = threading.get_ident() in self._worker_ids
        if not is_worker:
            self.wait(self.room_cond, self.has_room)
        with self.lock:
            if self.closed:
                raise ClosedError("this pool was closed: no more tasks can be submitted")
            self._start_backend(fn)
            self.queued.append(_Job(fn, args, args[0] if len(args) == 1 else args, self.submitted))
            self.submitted += 1
            if self.runners < self.concurrency:
                self.runners += 1
                ctx = contextvars.copy_context()
                thread = threading.Thread(
                    target=ctx.run, args=(self._serve,), name="shared-worker", daemon=True
                )
                self._runner_threads.append(thread)
                thread.start()
            self.work_cond.notify()

    def run_on_owner(self, fn: Callable[[], R]) -> R:
        """Run `fn()` on the owner thread and return its result, or run directly if on owner."""
        if threading.get_ident() == self._owner_id:
            return fn()
        with self.lock:
            if not self._owner_accepting or self.ended:
                raise ClosedError("pool is closed or stopping")
            call = _OwnerCall(fn)
            self._owner_calls.append(call)
            self.room_cond.notify_all()
            self.done_cond.notify_all()
        call.done.wait()
        if call.error is not None:
            raise call.error
        return call.result

    def close(self) -> None:
        """Say that no more tasks will be submitted; queued tasks still run."""
        with self.lock:
            self.closed = True
            self.work_cond.notify_all()

    def wait(
        self, cond: threading.Condition, ready: Callable[[], bool], timeout: float | None = None
    ) -> bool:
        """Wait on `cond` until `ready()` holds or `timeout` expires, servicing owner calls."""
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            with self.lock:
                while True:
                    self._drain_owner_calls()
                    if self.task_interrupt is not None:
                        interrupt, self.task_interrupt = self.task_interrupt, None
                        raise interrupt
                    if ready():
                        return True
                    left = (
                        WAKE_INTERVAL
                        if deadline is None
                        else min(WAKE_INTERVAL, deadline - time.monotonic())
                    )
                    if left <= 0:
                        return False
                    cond.wait(left)
        except BaseException as error:
            self.stop(error)
            self.end(error)
            raise

    def stop(self, error: BaseException | None = None) -> None:
        """Cancel queued tasks and pending retries, then wait for running tries to finish."""
        with self.lock:
            dropped = len(self.queued)
            self.queued.clear()
            self.closed = True
            self.stop_requested.set()
            running = self.running
            self._reject_owner_calls()
            self.work_cond.notify_all()
            self.room_cond.notify_all()
        self._log_stop(error, running, dropped)
        try:
            with self.lock:
                while self.busy:
                    self.done_cond.wait(WAKE_INTERVAL)
        except KeyboardInterrupt as second:
            self.end(second, force=True)
            raise

    def end(self, error: BaseException | None, *, force: bool = False) -> None:
        """Shut down executor, event loop, and worker threads. Idempotent."""
        with self.lock:
            if self.ended:
                return
            self.ended = True
            self.closed = True
            self._reject_owner_calls()
            idle = list(self._idle_workers)
            self._idle_workers.clear()
            runners = list(self._runner_threads) if not force else []
            self.work_cond.notify_all()
            self.done_cond.notify_all()
        for wake, box in idle:
            box[0] = None
            wake.set()
        if self.executor is not None:
            _logging_failure(self.executor.shutdown, wait=not force, cancel_futures=True)
        if self.loop is not None:
            _logging_failure(self.loop.call_soon_threadsafe, self.loop.stop)
            if self._loop_thread is not None and not force:
                self._loop_thread.join()
        for thread in runners:
            thread.join()

    def _drain_owner_calls(self) -> None:
        if threading.get_ident() != self._owner_id:
            return
        while self._owner_calls:
            call = self._owner_calls.popleft()
            self.lock.release()
            try:
                call.result = call.fn()
            except BaseException as err:
                call.error = err
                if isinstance(err, (KeyboardInterrupt, SystemExit)):
                    raise
            finally:
                call.done.set()
                self.lock.acquire()

    def _reject_owner_calls(self) -> None:
        self._owner_accepting = False
        while self._owner_calls:
            call = self._owner_calls.popleft()
            call.error = ClosedError("pool is stopping")
            call.done.set()

    def _log_stop(self, error: BaseException | None, running: int, dropped: int) -> None:
        if isinstance(error, KeyboardInterrupt):
            if running:
                ui.warn(
                    f"stopping: waiting for {running} running tasks (Ctrl-C again to force)"
                )
        elif error is not None and dropped and not isinstance(error, GeneratorExit):
            ui.warn(f"dropped {dropped} queued tasks after {type(error).__name__}")

    def _start_backend(self, fn: Callable[..., Any]) -> None:
        if self.loop is None and inspect.iscoroutinefunction(fn):
            self.loop, self._loop_thread = _start_loop_thread()
            if self._loop_thread.ident is not None:
                self._worker_ids.add(self._loop_thread.ident)

    def _serve(self) -> None:
        ident = threading.get_ident()
        with self.lock:
            self._worker_ids.add(ident)
        try:
            while (job := self._take()) is not None:
                self._finish_job(job)
        finally:
            with self.lock:
                self._worker_ids.discard(ident)

    def _finish_job(self, job: _Job) -> None:
        result: Result[Any] | None = None
        try:
            result = self._run_task(job)
        except BaseException as interrupt:
            self._interrupt(interrupt)
        with self.lock:
            if result is not None and not self.ended:
                self.finished.append(result)
            self.done_cond.notify_all()
        if result is not None and self.progress is not None:
            try:
                self._report(self.progress)
            except BaseException as error:
                self._interrupt(error)
        with self.lock:
            self.busy -= 1
            self.done_cond.notify_all()

    def _interrupt(self, error: BaseException) -> None:
        with self.lock:
            self.task_interrupt = error
            self.stop_requested.set()
            self.work_cond.notify_all()
            self.room_cond.notify_all()
            self.done_cond.notify_all()

    def _report(self, progress: Callable[[int, int], Any]) -> None:
        with self.reporting:
            with self.lock:
                done = len(self.finished)
                total = self.submitted
            if done > self.reported:
                self.reported = done
                progress(done, total)

    def _take(self) -> _Job | None:
        with self.lock:
            while not self.queued and not self.closed and not self.stop_requested.is_set():
                self.work_cond.wait()
            if self.queued and not self.stop_requested.is_set():
                job = self.queued.popleft()
                self.busy += 1
                if self.has_room():
                    self.room_cond.notify_all()
                return job
            self.runners -= 1
            self.done_cond.notify_all()
            return None

    def _run_task(self, job: _Job) -> Result[Any]:
        attempt = 0
        while True:
            attempt += 1
            try:
                value = self._counted_attempt(job)
                return _make_result(job.item, value, None, attempt, job.position)
            except Exception as error:
                stopped = attempt <= self.retries and clock.sleep(
                    _retry_wait(attempt), stop=self.stop_requested
                )
                if attempt > self.retries or stopped:
                    return _make_result(job.item, None, error, attempt, job.position)

    def _counted_attempt(self, job: _Job) -> Any:
        with self.lock:
            self.running += 1
        try:
            return self._attempt(job)
        finally:
            with self.lock:
                self.running -= 1
                self.done_cond.notify_all()

    def _attempt(self, job: _Job) -> Any:
        future: concurrent.futures.Future[Any]
        if self.loop is not None and inspect.iscoroutinefunction(job.fn):
            future = asyncio.run_coroutine_threadsafe(job.fn(*job.args), self.loop)
        elif self.timeout is None:
            return job.fn(*job.args)
        else:
            future = self._submit_timed(job.fn, job.args)
        if self.timeout is None:
            return future.result()
        if not future.done():
            concurrent.futures.wait((future,), timeout=self.timeout)
        if not future.done():
            future.cancel()
            raise StalledError(f"took longer than {self.timeout}s")
        return future.result()

    def _submit_timed(
        self, fn: Callable[..., Any], args: tuple[Any, ...]
    ) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        with self.lock:
            if self._idle_workers:
                wake, box = self._idle_workers.popleft()
            else:
                wake = threading.Event()
                box: list[Any] = [None]
                threading.Thread(
                    target=self._try_worker, args=(wake, box), name="shared-worker-try", daemon=True
                ).start()
        box[0] = (future, fn, args)
        wake.set()
        return future

    def _try_worker(self, wake: threading.Event, box: list[Any]) -> None:
        ident = threading.get_ident()
        with self.lock:
            self._worker_ids.add(ident)
        try:
            while True:
                wake.wait()
                wake.clear()
                task = box[0]
                if task is None:
                    return
                box[0] = None
                future, fn, args = task
                try:
                    future.set_result(fn(*args))
                except BaseException as error:
                    future.set_exception(error)
                with self.lock:
                    if self.ended:
                        return
                    self._idle_workers.append((wake, box))
        finally:
            with self.lock:
                self._worker_ids.discard(ident)


def _make_result(
    item: Any, value: Any, error: BaseException | None, attempts: int, position: int
) -> Result[Any]:
    result = Result(item, value, error, attempts)
    result._position = position
    return result


def _wait_future(
    future: concurrent.futures.Future[Any],
    *,
    deadline: float | None,
    timeout: float | None,
) -> None:
    while not future.done():
        left = (
            WAKE_INTERVAL
            if deadline is None
            else min(WAKE_INTERVAL, deadline - time.monotonic())
        )
        if left <= 0:
            future.cancel()
            raise StalledError(f"took longer than {timeout}s")
        concurrent.futures.wait((future,), timeout=left)


def _start_loop_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Create and run an asyncio event loop inside a new background thread."""
    ready = threading.Event()
    box: list[asyncio.AbstractEventLoop] = []

    def body() -> None:
        # Creating the loop on the background thread avoids binding signal.set_wakeup_fd to _csock.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box.append(loop)
        ready.set()
        try:
            loop.run_forever()
            leftover = asyncio.all_tasks(loop)
            for task in leftover:
                task.cancel()
            if leftover:
                loop.run_until_complete(asyncio.gather(*leftover, return_exceptions=True))
        finally:
            loop.close()

    thread = threading.Thread(target=body, name="shared-worker-loop", daemon=True)
    thread.start()
    ready.wait()
    return box[0], thread


def _retry_wait(attempt: int) -> float:
    """The seconds to wait before the next try: 1, 2, 4… with jitter, capped at MAX_RETRY_WAIT."""
    return min(MAX_RETRY_WAIT, 2 ** (attempt - 1) * random.uniform(0.75, 1.25))


def _logging_failure(step: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Call one shutdown step, and warn instead of raising when it fails."""
    try:
        step(*args, **kwargs)
    except Exception as error:
        ui.warn(f"worker couldn't shut down cleanly: {_describe(error)}")


def _describe(error: BaseException | None) -> str:
    return f"{type(error).__name__} {error}".strip()
