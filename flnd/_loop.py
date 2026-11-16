"""The run's asyncio `Loop`, and the bounded `Pool` the downloads run on.

`Loop` is one asyncio event loop on a thread of its own. A run makes one
(`flnd.site.shared_browser`), and everything async of that run happens on it: the browser
(`flnd._browser`) and an async source's coroutines (Telegram's Telethon). Other threads hand it
work with `Loop.run`, which waits for the answer; that hop costs about 40 µs, small against one
browser round trip (0.4 ms).

    with Loop() as loop:
        title = loop.run(page.evaluate("document.title"))

Rules a change must keep:
- `Loop.run` is never called on the loop's own thread: it would wait for itself forever, so it
  raises instead. Code already on the loop awaits.
- Closing cancels whatever still runs on the loop, then stops its thread.

`Pool` is a thread pool with retries and backpressure, which a target's downloads run on:

    with worker.Pool(jobs=8, retries=2, stop=event, progress=row) as pool:
        for x in items:
            pool.submit(fn, x)        # blocks once jobs*2 tasks are queued or running
        for r in pool.results:        # Result[Any] as each finishes, until every task is done
            r.item, r.value, r.error, r.ok, r.attempts
        pool.pause(); pool.resume()   # no new task starts while paused; running ones finish
        pool.done, pool.total         # counts
        pool.call(fn)                 # run fn on the thread that created the pool, wait for it
        pool.stop()                   # cancel what hasn't started; running tasks see `stopping`
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import os
import random
import threading
from collections import deque
from collections.abc import Awaitable, Iterator
from dataclasses import dataclass
from typing import Any, Callable, Generic, Literal, TypeVar

from flnd import _time

__all__ = ["Loop", "Pool", "Result"]

R = TypeVar("R")

BACKOFF = 0.5
MAX_BACKOFF = 8.0
# A blocked thread wakes this often (seconds): a caller's `stop` event can't notify the pool.
WAKE = 0.1
# How long closing waits for the event loop's cancelled tasks before leaving them.
LOOP_GRACE = 10.0


class Loop:
    """One asyncio event loop on its own thread, started on first use; `run` hands it a coroutine.

        with Loop() as loop:
            loop.run(asyncio.sleep(0.1))

    Any thread may call `run`, except the loop's own (see `run`). `close` (or leaving `with`)
    cancels what still runs and ends the thread; a closed loop refuses more work.
    """

    def __init__(self) -> None:
        # Guards the start and the close, which two listing threads may reach at once.
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def __enter__(self) -> Loop:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @property
    def here(self) -> bool:
        """Whether the calling code runs on this loop's thread, where it must await, not `run`."""
        return self._thread is not None and threading.get_ident() == self._thread.ident

    def run(self, awaitable: Awaitable[R]) -> R:
        """Run `awaitable` on the loop, wait for it, and return its value or raise its error.

        Args:
            awaitable: a coroutine (or any awaitable) made for this loop.

        Returns:
            What the awaitable returned.

        Raises:
            RuntimeError: the loop is closed, or this is the loop's own thread, which would wait
                for itself forever.

        A Ctrl-C while waiting cancels the coroutine on the loop before it goes on up.
        """
        try:
            loop = self._started()
            if self.here:
                raise RuntimeError("Loop.run was called on the loop's own thread; await it instead")
        except RuntimeError:
            # Closed, so nothing will ever await it, and Python would warn of that.
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise
        future = asyncio.run_coroutine_threadsafe(_carried(awaitable), loop)
        try:
            ok, value = future.result()
        except BaseException:
            future.cancel()
            raise
        if not ok:
            raise value
        return value

    def close(self) -> None:
        """Cancel what still runs on the loop, stop it and end its thread; once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        cancelled = asyncio.run_coroutine_threadsafe(_cancel_pending(), loop)
        concurrent.futures.wait([cancelled], timeout=LOOP_GRACE)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(LOOP_GRACE)
        if not thread.is_alive():
            loop.close()

    def _started(self) -> asyncio.AbstractEventLoop:
        """The event loop, with its thread started on the first call.

        Returns:
            The running event loop.

        Raises:
            RuntimeError: the loop is closed.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("the loop is closed")
            if self._loop is None:
                # On Windows the default loop is the proactor, which Playwright's driver needs
                # for its subprocess pipes; elsewhere the selector loop serves both.
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=_serve_loop, args=(self._loop,), name="flnd-loop", daemon=True,
                )
                self._thread.start()
            return self._loop


# Using a `Pool` after it closed or stopped is a bug in the caller, so it raises the builtin
# `RuntimeError("pool is closed")`, never a `flnd.Error`.
@dataclass
class Result(Generic[R]):
    """Outcome record for a single completed task."""

    item: Any
    value: R | None = None
    error: BaseException | None = None
    attempts: int = 0

    @property
    def ok(self) -> bool:
        """Whether the task succeeded without error."""
        return self.error is None


@dataclass
class _Task:
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    item: Any
    context: contextvars.Context


@dataclass
class _Call:
    fn: Callable[[], Any]
    future: concurrent.futures.Future[Any]


class Pool:
    """Thread pool that also runs coroutine functions on its own event loop thread."""

    def __init__(
        self,
        *,
        jobs: int | Literal["cpu", "io"] = "io",
        retries: int = 0,
        stop: threading.Event | None = None,
        progress: Any = None,
    ) -> None:
        if retries < 0:
            raise ValueError(f"retries must be non-negative, not {retries}")
        self._jobs = _size(jobs)
        self._retries = retries
        self._stop = stop
        self._halt = threading.Event()
        self._progress = progress
        self._owner = threading.get_ident()
        # One lock guards every field below; it is notified on each change of them.
        self._changed = threading.Condition()
        self._queue: deque[_Task] = deque()
        self._finished: deque[Result[Any]] = deque()
        self._calls: deque[_Call] = deque()
        self._threads: list[threading.Thread] = []
        self._running = self._done = self._total = 0
        self._paused = self._closing = self._shut = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    def __enter__(self) -> Pool:
        return self

    def __exit__(self, kind: type[BaseException] | None, *_: Any) -> None:
        if kind is not None:
            self.stop()
        self.close()

    @property
    def done(self) -> int:
        """How many submitted tasks have finished, cancelled ones included."""
        return self._done

    @property
    def total(self) -> int:
        """How many tasks were submitted."""
        return self._total

    @property
    def stopping(self) -> bool:
        """Whether `stop()` was called or the `stop` event is set."""
        return self._halt.is_set() or (self._stop is not None and self._stop.is_set())

    @property
    def results(self) -> Iterator[Result[Any]]:
        """Each task's `Result` as it finishes, until every submitted task is done."""
        while True:
            with self._changed:
                self._block(lambda: bool(self._finished) or self._done == self._total)
                if not self._finished:
                    return
                result = self._finished.popleft()
            yield result

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        """Queue `fn(*args)`, waiting while jobs*2 tasks are queued or running."""
        item = args[0] if len(args) == 1 else (args or fn)
        task = _Task(fn, args, item, contextvars.copy_context())
        with self._changed:
            self._block(lambda: self._closing or len(self._queue) + self._running < 2 * self._jobs)
            if self._closing or self.stopping:
                raise RuntimeError("pool is closed")
            self._queue.append(task)
            self._total += 1
            if self._progress is not None:
                self._progress.total = self._total
            if len(self._threads) < self._jobs:
                self._start_thread()
            self._changed.notify_all()

    def wait(self, fn: Callable[..., Awaitable[R]], *args: Any) -> R:
        """Run the coroutine `fn(*args)` on the pool's event loop and return its value."""
        self._event_loop()  # a closed pool raises before `fn` makes a coroutine nobody awaits
        return self._await(fn(*args))

    def call(self, fn: Callable[[], R]) -> R:
        """Run `fn` on the thread that created the pool, and return its value."""
        if threading.get_ident() == self._owner:
            return fn()
        future: concurrent.futures.Future[R] = concurrent.futures.Future()
        with self._changed:
            if self._shut:
                raise RuntimeError("pool is closed")
            self._calls.append(_Call(fn, future))
            self._changed.notify_all()
        return future.result()

    def pause(self) -> None:
        """Start no new task until `resume`; running tasks finish."""
        with self._changed:
            self._paused = True

    def resume(self) -> None:
        """Start queued tasks again after `pause`."""
        with self._changed:
            self._paused = False
            self._changed.notify_all()

    def stop(self) -> None:
        """Cancel every task that hasn't started; running tasks see `stopping`."""
        self._halt.set()
        with self._changed:
            self._cancel_if_stopping()
            self._changed.notify_all()

    def close(self) -> None:
        """Wait for every queued and running task, then end the threads and the event loop."""
        with self._changed:
            if self._shut:
                return
            # A paused pool would never empty its queue.
            self._closing, self._paused = True, False
            self._changed.notify_all()
            self._block(lambda: not self._queue and not self._running)
            self._shut = True
            calls, self._calls = list(self._calls), deque()
        for call in calls:
            call.future.set_exception(RuntimeError("pool is closed"))
        for thread in self._threads:
            thread.join()
        self._close_loop()

    def _block(self, ready: Callable[[], bool]) -> None:
        """Wait (lock held) until `ready()`, running queued calls if this is the owner thread."""
        owner = threading.get_ident() == self._owner
        while True:
            self._cancel_if_stopping()
            if owner:
                self._run_calls()
            if ready():
                return
            self._changed.wait(WAKE)

    def _cancel_if_stopping(self) -> None:
        if not self.stopping:
            return
        self._halt.set()
        while self._queue:
            self._record(Result(self._queue.popleft().item, error=InterruptedError("stopped")))

    def _run_calls(self) -> None:
        # The lock is released while a call runs: the call may itself wait on a task.
        while self._calls:
            call = self._calls.popleft()
            self._changed.release()
            try:
                _answer(call)
            finally:
                self._changed.acquire()

    def _record(self, result: Result[Any]) -> None:
        self._finished.append(result)
        self._done += 1
        if self._progress is not None:
            self._progress.advance(1)
        self._changed.notify_all()

    def _start_thread(self) -> None:
        thread = threading.Thread(target=self._serve, name="flnd-worker", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _serve(self) -> None:
        while (task := self._next()) is not None:
            result = task.context.run(self._attempt, task)
            with self._changed:
                self._running -= 1
                self._record(result)

    def _next(self) -> _Task | None:
        with self._changed:
            while True:
                self._cancel_if_stopping()
                if self._queue and not self._paused:
                    self._running += 1
                    return self._queue.popleft()
                if not self._queue and (self._closing or self.stopping):
                    return None
                self._changed.wait(WAKE)

    def _attempt(self, task: _Task) -> Result[Any]:
        attempts = 0
        while True:
            attempts += 1
            try:
                value = task.fn(*task.args)
                return Result(task.item, value=self._await_if_needed(value), attempts=attempts)
            except BaseException as error:
                if attempts > self._retries or self.stopping:
                    return Result(task.item, error=error, attempts=attempts)
                if not _time.sleep(_backoff(attempts), stop=self._halt):
                    return Result(task.item, error=error, attempts=attempts)

    def _await_if_needed(self, value: Any) -> Any:
        return self._await(value) if inspect.isawaitable(value) else value

    def _await(self, awaitable: Awaitable[R]) -> R:
        future = asyncio.run_coroutine_threadsafe(_awaited(awaitable), self._event_loop())
        try:
            return future.result()
        except BaseException:
            future.cancel()
            raise

    def _event_loop(self) -> asyncio.AbstractEventLoop:
        with self._changed:
            if self._shut:
                raise RuntimeError("pool is closed")
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._loop_thread = threading.Thread(
                    target=_serve_loop, args=(self._loop,), name="flnd-worker-loop", daemon=True,
                )
                self._loop_thread.start()
            return self._loop

    def _close_loop(self) -> None:
        loop, thread = self._loop, self._loop_thread
        if loop is None or thread is None:
            return
        cancelled = asyncio.run_coroutine_threadsafe(_cancel_pending(), loop)
        concurrent.futures.wait([cancelled], timeout=LOOP_GRACE)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(LOOP_GRACE)
        if not thread.is_alive():
            loop.close()


def _size(jobs: int | Literal["cpu", "io"]) -> int:
    cpu = os.cpu_count() or 1
    if jobs == "cpu":
        return cpu
    if jobs == "io":
        return min(32, cpu + 4)
    if isinstance(jobs, int) and not isinstance(jobs, bool) and jobs >= 1:
        return jobs
    raise ValueError(f"jobs must be a positive int, 'cpu' or 'io', not {jobs!r}")


def _answer(call: _Call) -> None:
    if not call.future.set_running_or_notify_cancel():
        return
    try:
        call.future.set_result(call.fn())
    except BaseException as error:
        call.future.set_exception(error)
        # A Ctrl-C landing in the call is the owner's too.
        if not isinstance(error, Exception):
            raise


def _backoff(attempts: int) -> float:
    return min(MAX_BACKOFF, BACKOFF * 2 ** (attempts - 1)) * random.uniform(0.5, 1.0)


async def _awaited(awaitable: Awaitable[R]) -> R:
    return await awaitable


async def _carried(awaitable: Awaitable[R]) -> tuple[bool, Any]:
    """`(True, value)`, or `(False, error)` for any error but a cancel, for `Loop.run` to raise.

    asyncio lets a `KeyboardInterrupt` (and so `flnd.Stopped`) raised in a task out of the loop,
    which would end the loop's thread and leave its caller waiting forever; carried back as a
    value, it is raised on the caller's thread instead, where it belongs.

    Args:
        awaitable: what `Loop.run` was given.

    Returns:
        Whether it returned, and its value or its error.
    """
    try:
        return True, await awaitable
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        return False, error


def _serve_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


async def _cancel_pending() -> None:
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    loop = asyncio.get_running_loop()
    await loop.shutdown_asyncgens()
    await loop.shutdown_default_executor()
