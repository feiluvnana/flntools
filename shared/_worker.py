"""Run a function on many inputs at once: `map`, `race` and `pool`.

Tasks run in threads by default, which suits work that waits: network, files, browsers.
`processes=True` runs them in processes, for work that computes. `async def` functions run
on an event loop in a background thread, so the calling code stays synchronous.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import os
import pickle
import random
import signal
import threading
import time
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Generic, Iterable, Iterator, NamedTuple, TypedDict, TypeVar, overload

from shared._log import ProgressBar, log

if TYPE_CHECKING:
    from typing_extensions import Unpack

MAX_RETRY_WAIT = 60.0
# Waits wake at least this often, so Ctrl-C is seen on platforms where a blocked lock ignores it.
WAKE_INTERVAL = 0.25

R = TypeVar("R")
A1 = TypeVar("A1")
A2 = TypeVar("A2")
A3 = TypeVar("A3")


@dataclass
class Result(Generic[R]):
    """How one task went: its input, and what it returned or raised.

    ```python
    for result in worker.race(download, urls):
        print(result.item, result.value if result.ok else result.error, result.attempts)
    ```
    """

    item: Any
    value: R | None = None
    error: BaseException | None = None
    attempts: int = 0
    _position: int = field(default=0, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.error is None


class TaskError(Exception):
    """Raised by `map` when some tasks failed, once every other task has finished.

    ```python
    try:
        sizes = worker.map(file_size, urls)
    except worker.TaskError as error:
        failed = [result for result in error.results if not result.ok]
    ```

    `results` holds every `Result` in input order, so the work that succeeded is not lost.
    """

    def __init__(self, results: list[Result[Any]]):
        self.results = results
        failed = [result for result in results if not result.ok]
        first = failed[0]
        super().__init__(f"{len(failed)} of {len(results)} tasks failed; first: {first.item}: {_describe(first.error)}")


class _Options(TypedDict, total=False):
    concurrency: int | None
    retries: int
    timeout: float | None
    progress: str | None
    processes: bool


@overload
def map(fn: Callable[[A1], R], items: Iterable[A1], /, **options: Unpack[_Options]) -> list[R]: ...
@overload
def map(fn: Callable[[A1, A2], R], items: Iterable[A1], second: Iterable[A2], /,
        **options: Unpack[_Options]) -> list[R]: ...
@overload
def map(fn: Callable[[A1, A2, A3], R], items: Iterable[A1], second: Iterable[A2], third: Iterable[A3], /,
        **options: Unpack[_Options]) -> list[R]: ...
@overload
def map(fn: Callable[..., Any], items: Iterable[Any], second: Iterable[Any], third: Iterable[Any],
        fourth: Iterable[Any], /, *more: Iterable[Any], **options: Unpack[_Options]) -> list[Any]: ...
def map(fn: Callable[..., Any], *iterables: Iterable[Any], **options: Any) -> list[Any]:
    """Run `fn` on every item, several at once, and return the results in input order.

    ```python
    sizes = worker.map(file_size, urls, concurrency=8, retries=2, timeout=30, progress="files")
    ```

    With several iterables, `fn` gets one item from each, paired up like the builtin `map`.
    If a task failed, it raises `TaskError` once the others have finished.

    Options, the same for `race` and `pool`:
    - `concurrency`: how many tasks run at once; about 8 to 32 threads, or one process per CPU;
    - `retries`: how many more tries a failed task gets, after waits of 1, 2, 4… s (at most 60 s);
    - `timeout`: seconds before one try fails with `TimeoutError`. A coroutine or a process really
      stops; a thread can't be stopped, so it runs on in the background and its result is thrown away;
    - `progress`: a label for a progress bar of finished tasks;
    - `processes`: run tasks in processes; `fn` must then be defined at the top level of a module.

    Ctrl-C once cancels queued tasks and lets running ones finish; twice stops at once.
    It is typed for one to three iterables; with more, the results are `Any`.
    """
    with _Run(total=_length(iterables), **options) as run:
        for args in zip(*iterables):
            run.submit(fn, args)
    results = sorted(run.finished, key=lambda result: result._position)
    if any(not result.ok for result in results):
        raise TaskError(results)
    return [result.value for result in results]


@overload
def race(fn: Callable[[A1], R], items: Iterable[A1], /, **options: Unpack[_Options]) -> Iterator[Result[R]]: ...
@overload
def race(fn: Callable[[A1, A2], R], items: Iterable[A1], second: Iterable[A2], /,
         **options: Unpack[_Options]) -> Iterator[Result[R]]: ...
@overload
def race(fn: Callable[[A1, A2, A3], R], items: Iterable[A1], second: Iterable[A2], third: Iterable[A3], /,
         **options: Unpack[_Options]) -> Iterator[Result[R]]: ...
@overload
def race(fn: Callable[..., Any], items: Iterable[Any], second: Iterable[Any], third: Iterable[Any],
         fourth: Iterable[Any], /, *more: Iterable[Any], **options: Unpack[_Options]) -> Iterator[Result[Any]]: ...
def race(fn: Callable[..., Any], *iterables: Iterable[Any], **options: Any) -> Iterator[Result[Any]]:
    """Run `fn` on every item, several at once, and yield each `Result` as soon as it is ready.

    ```python
    for result in worker.race(download, urls, concurrency=8):
        if not result.ok:
            log.error("{url}: {error}", url=result.item, error=result.error)
    ```

    Results come in the order they finish. Unlike JavaScript's `Promise.race`, which keeps only
    the first, it yields every result, and it never raises because a task failed.
    It takes the same options as `map`.
    """
    with _Run(total=_length(iterables), **options) as run:
        seen = 0
        for args in zip(*iterables):
            while not run.has_room():
                run.wait(lambda: run.has_room() or len(run.finished) > seen)
                fresh = run.finished[seen:]
                seen += len(fresh)
                yield from fresh
            run.submit(fn, args)
        run.close()
        while seen < run.submitted:
            run.wait(lambda: len(run.finished) > seen)
            fresh = run.finished[seen:]
            seen += len(fresh)
            yield from fresh


def pool(**options: Unpack[_Options]) -> Pool:
    """Start a pool that takes tasks one at a time, while other work is still going on.

    ```python
    with worker.pool(concurrency=6) as pool:
        for post in scroll_through_feed():
            pool.submit(download, post.url)
    print(len(pool.results), "done")
    ```

    Leaving the block calls `close()` then `join()`; if the block raised, queued tasks are
    dropped instead, with a warning line. Without `with`, call `close()` and `join()` yourself.
    It takes the same options as `map`, and never raises because a task failed.
    """
    return Pool(_Run(**options))


class Pool:
    """Tasks submitted one at a time; made by `worker.pool`.

    ```python
    pool = worker.pool(concurrency=6)
    pool.submit(download, url)
    pool.close()
    results = pool.join()
    ```
    """

    def __init__(self, run: _Run):
        self._run = run

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        """Queue `fn(*args)`. Waits while twice `concurrency` tasks are queued; raises `RuntimeError` after `close()`."""
        self._run.submit(fn, args)

    def close(self) -> None:
        """Say that no more tasks are coming. Queued tasks still run."""
        self._run.close()

    def join(self, timeout: float | None = None) -> list[Result[Any]]:
        """Wait for every task submitted so far, or `timeout` seconds, and return `results`."""
        run = self._run
        if run.wait(run.is_idle, timeout) and run.closed:
            run.end(None)
        return self.results

    @property
    def results(self) -> list[Result[Any]]:
        """The finished results, in the order they finished."""
        return list(self._run.finished)

    @property
    def submitted(self) -> int:
        return self._run.submitted

    @property
    def raw(self) -> Any:
        """The `ProcessPoolExecutor` with `processes=True`, the asyncio loop once an `async def`
        function was submitted, else `None`: plain tasks run on worker's own threads."""
        return self._run.executor or self._run.loop

    def __enter__(self) -> Pool:
        return self

    def __exit__(self, error_type: type[BaseException] | None, error: BaseException | None, traceback: Any) -> None:
        self._run.__exit__(error_type, error, traceback)

    def __del__(self) -> None:
        if not self._run.ended:
            unfinished = self._run.submitted - len(self._run.finished)
            log.warn("worker.pool was never joined; {n} tasks may not have finished", n=unfinished)
            self._run.close()


class _Job(NamedTuple):
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    item: Any
    position: int


class _Run:
    """The engine under `map`, `race` and `pool`: a bounded queue, runner threads, retries and stopping.

    Every count below is read and changed under `changed`, which is notified on every change.
    """

    def __init__(self, *, concurrency: int | None = None, retries: int = 0, timeout: float | None = None,
                 progress: str | None = None, processes: bool = False, total: int | None = None):
        if concurrency is not None and concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, not {concurrency}")
        if retries < 0:
            raise ValueError(f"retries must be 0 or more, not {retries}")
        if timeout is not None and timeout <= 0:
            raise ValueError(f"timeout must be above 0, not {timeout}")
        cpus = os.cpu_count() or 1
        self.concurrency = concurrency or (cpus if processes else min(32, cpus + 4))
        self.retries, self.timeout, self.processes = retries, timeout, processes
        self.changed = threading.Condition()
        self.queued: deque[_Job] = deque()
        self.finished: list[Result[Any]] = []
        self.submitted = 0
        self.busy = 0  # tasks a runner took: trying, or waiting to try again
        self.running = 0  # tries in progress
        self.runners = 0
        self.closed = self.ended = False
        self.stop_requested = threading.Event()
        self.task_interrupt: BaseException | None = None
        self.executor: concurrent.futures.ProcessPoolExecutor | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.exits = ExitStack()
        self.bar: ProgressBar | None = None
        if progress is not None:
            self.bar = self.exits.enter_context(log.progress(progress, total=total))
        self.growing_total = total is None

    def __enter__(self) -> _Run:
        return self

    def __exit__(self, error_type: type[BaseException] | None, error: BaseException | None, traceback: Any) -> None:
        if error is not None:
            self.stop(error)
        else:
            try:
                self.close()
                self.wait(self.is_idle)
            except BaseException as late:
                self.stop(late)
                self.end(late)
                raise
        # A consumer that stops reading `race` early ends it on purpose, not by failing.
        self.end(None if isinstance(error, GeneratorExit) else error)

    def has_room(self) -> bool:
        return len(self.queued) < 2 * self.concurrency

    def is_idle(self) -> bool:
        return not self.queued and self.busy == 0

    def submit(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
        if self.processes:
            _check_picklable(fn)
        if self.closed:
            raise RuntimeError("this pool was closed: no more tasks can be submitted")
        # Reading inputs only as fast as runners take them keeps a huge or endless input out of memory.
        self.wait(self.has_room)
        with self.changed:
            self._start_backend(fn)
            self.queued.append(_Job(fn, args, args[0] if len(args) == 1 else args, self.submitted))
            self.submitted += 1
            if self.bar is not None and self.growing_total:
                self.bar.total = self.submitted
            if self.runners < self.concurrency:
                self.runners += 1
                threading.Thread(target=self._serve, name="shared-worker", daemon=True).start()
            self.changed.notify_all()

    def close(self) -> None:
        with self.changed:
            self.closed = True

    def wait(self, ready: Callable[[], bool], timeout: float | None = None) -> bool:
        """Wait until `ready()` holds, or `timeout` seconds pass. Ctrl-C here stops the run."""
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            with self.changed:
                while True:
                    if self.task_interrupt is not None:
                        interrupt, self.task_interrupt = self.task_interrupt, None
                        raise interrupt
                    if ready():
                        return True
                    left = WAKE_INTERVAL if deadline is None else min(WAKE_INTERVAL, deadline - time.monotonic())
                    if left <= 0:
                        return False
                    self.changed.wait(left)
        except BaseException as error:
            # A pool used without `with` has no __exit__ to stop it, so stopping happens here.
            self.stop(error)
            self.end(error)
            raise

    def stop(self, error: BaseException) -> None:
        """Cancel queued tasks and pending retries, then wait for the running tries to finish."""
        with self.changed:
            dropped = len(self.queued)
            self.queued.clear()
            self.closed = True
            self.stop_requested.set()
            running = self.running
        try:
            if isinstance(error, KeyboardInterrupt):
                if running:
                    log.warn("stopping: waiting for {n} running tasks (Ctrl-C again to force)", n=running)
            elif dropped and not isinstance(error, GeneratorExit):
                log.warn("dropped {n} queued tasks after {error}", n=dropped, error=type(error).__name__)
            with self.changed:
                while self.busy:
                    self.changed.wait(WAKE_INTERVAL)
        except KeyboardInterrupt as second:
            self.end(second, force=True)
            raise

    def end(self, error: BaseException | None, *, force: bool = False) -> None:
        """Shut down processes and the event loop, and end the progress bar. Later calls do nothing."""
        with self.changed:
            if self.ended:
                return
            self.ended = True
            failed = any(not result.ok for result in self.finished)
        if self.executor is not None:
            _logging_failure(_stop_processes, self.executor, force)
        if self.loop is not None:
            _logging_failure(self.loop.call_soon_threadsafe, self.loop.stop)
        if self.bar is not None and failed and error is None:
            self.bar.fail()
        if error is None:
            self.exits.close()
        else:
            self.exits.__exit__(type(error), error, error.__traceback__)

    def _start_backend(self, fn: Callable[..., Any]) -> None:
        if self.processes and self.executor is None:
            self.executor = concurrent.futures.ProcessPoolExecutor(self.concurrency, initializer=_ignore_ctrl_c)
        elif not self.processes and self.loop is None and inspect.iscoroutinefunction(fn):
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=_run_loop, args=(self.loop,), name="shared-worker-loop", daemon=True).start()

    def _serve(self) -> None:
        """One runner thread: run queued tasks until none are left, then exit."""
        while (job := self._take()) is not None:
            result: Result[Any] | None = None
            try:
                result = self._run_task(job)
            except BaseException as interrupt:  # KeyboardInterrupt or SystemExit, e.g. Ctrl-C at a question
                with self.changed:
                    self.task_interrupt = interrupt
                    self.stop_requested.set()
            with self.changed:
                self.busy -= 1
                if result is not None and not self.ended:
                    self.finished.append(result)
                    if self.bar is not None:
                        self.bar.advance()
                self.changed.notify_all()

    def _take(self) -> _Job | None:
        with self.changed:
            if self.queued and not self.stop_requested.is_set():
                self.busy += 1
                self.changed.notify_all()
                return self.queued.popleft()
            self.runners -= 1
            return None

    def _run_task(self, job: _Job) -> Result[Any]:
        attempt = 0
        while True:
            attempt += 1
            try:
                return Result(job.item, self._counted_attempt(job), None, attempt, job.position)
            except Exception as error:
                # After Ctrl-C a pending retry is cancelled too: waiting a minute to retry is not "finishing".
                if attempt > self.retries or _sleep(_retry_wait(attempt), self.stop_requested):
                    return Result(job.item, None, error, attempt, job.position)

    def _counted_attempt(self, job: _Job) -> Any:
        with self.changed:
            self.running += 1
        try:
            return self._attempt(job)
        finally:
            with self.changed:
                self.running -= 1
                self.changed.notify_all()

    def _attempt(self, job: _Job) -> Any:
        """One try of one task. Past `timeout` it raises `TimeoutError`."""
        future: concurrent.futures.Future[Any]
        if self.executor is not None:
            future = self.executor.submit(_call_in_child, job.fn, job.args, self.timeout)
        elif self.loop is not None and inspect.iscoroutinefunction(job.fn):
            future = asyncio.run_coroutine_threadsafe(job.fn(*job.args), self.loop)
        elif self.timeout is None:
            return job.fn(*job.args)
        else:
            future = concurrent.futures.Future()
            future.set_running_or_notify_cancel()
            threading.Thread(target=_call_into, args=(future, job.fn, job.args), daemon=True).start()
        try:
            return future.result(self.timeout)
        except concurrent.futures.TimeoutError:
            # Cancelling stops a coroutine. Python can't stop a thread, so a timed-out thread runs on
            # in the background and its result is thrown away. A process stops itself: see _call_in_child.
            future.cancel()
            raise TimeoutError(f"took longer than {self.timeout}s") from None


def _check_picklable(fn: Callable[..., Any]) -> None:
    try:
        pickle.dumps(fn)
    except Exception:
        name = getattr(fn, "__name__", repr(fn))
        raise TypeError(f"worker can't send {name} to another process: "
                        "use a function defined at the top level of a module") from None


def _ignore_ctrl_c() -> None:
    # Ctrl-C reaches every process in the terminal's group. Only the parent decides what stops,
    # so a first Ctrl-C lets the running tasks finish and a second terminates the processes.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()
    # A timed-out coroutine was only asked to stop; it stops on the loop's next turn, so give it one.
    leftover = asyncio.all_tasks(loop)
    for task in leftover:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*leftover, return_exceptions=True))
    loop.close()


def _call_in_child(fn: Callable[..., Any], args: tuple[Any, ...], timeout: float | None) -> Any:
    """Run one try in a worker process, which stops it itself after `timeout` seconds."""
    # The parent can't stop one task in a shared ProcessPoolExecutor, so the child sets an alarm.
    # Windows has no alarm: there a try past `timeout` runs on, as a thread's does.
    if timeout is None or not hasattr(signal, "setitimer"):
        return fn(*args)

    def give_up(signal_number: int, frame: Any) -> None:
        raise TimeoutError(f"took longer than {timeout}s")

    signal.signal(signal.SIGALRM, give_up)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return fn(*args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def _call_into(future: concurrent.futures.Future[Any], fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
    try:
        future.set_result(fn(*args))
    except BaseException as error:
        future.set_exception(error)


def _retry_wait(attempt: int) -> float:
    # Jitter keeps tasks that failed together from all retrying at the same moment.
    return min(MAX_RETRY_WAIT, 2 ** (attempt - 1) * random.uniform(0.75, 1.25))


def _sleep(seconds: float, stop_requested: threading.Event) -> bool:
    """Wait between tries. True when a stop was requested first. Tests replace it."""
    return stop_requested.wait(seconds)


def _stop_processes(executor: concurrent.futures.ProcessPoolExecutor, force: bool) -> None:
    if force:
        # ProcessPoolExecutor has no public way to stop running tasks before Python 3.14.
        for process in list((executor._processes or {}).values()):
            process.terminate()
    executor.shutdown(wait=not force, cancel_futures=True)


def _logging_failure(step: Callable[..., Any], *args: Any) -> None:
    # Shutdown carries on past a failure, so one broken process or loop can't make it hang.
    try:
        step(*args)
    except Exception as error:
        log.warn("worker couldn't shut down cleanly: {error}", error=_describe(error))


def _length(iterables: tuple[Iterable[Any], ...]) -> int | None:
    try:
        return min(len(iterable) for iterable in iterables) if iterables else 0  # pyright: ignore[reportArgumentType]
    except TypeError:
        return None


def _describe(error: BaseException | None) -> str:
    return f"{type(error).__name__} {error}".strip()
