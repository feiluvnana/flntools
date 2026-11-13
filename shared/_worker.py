"""Run a function on many inputs at once: `as_ordered`, `as_completed` and `pool`.

How it works: each entry point starts a run, a bounded queue of tasks served by runner threads
that `worker` starts as tasks arrive, up to `concurrency`, and that exit when the queue is empty.
A runner tries a task, waits 1, 2, 4… s (with jitter, at most 60 s) between `retries`, and gives
up on one try after `timeout` seconds. Inputs are read only as fast as runners take them, so an
endless generator never fills memory. Tasks run in threads by default, which suits work that
waits: network, files, browsers. `processes=True` hands each try to a stdlib
`concurrent.futures.ProcessPoolExecutor`, for work that computes. An `async def` function runs on
an `asyncio` event loop in a background thread, so the calling code stays synchronous. Every
task's outcome is a `Result`: `as_ordered` returns the values in input order and raises `TaskError`
if any failed, `as_completed` yields each `Result` as it finishes, and `pool` takes tasks one
at a time, for work that is found while other work runs. `label=` draws a progress row with
`shared.log`; `progress=` reports to a callback instead.

    from shared import worker

    sizes = worker.as_ordered(file_size, urls, concurrency=8, retries=2, label="files")
    for result in worker.as_completed(download, urls, timeout=30):
        print(result.item, result.value if result.ok else result.error)
    with worker.pool(concurrency=6) as pool:
        pool.submit(download, url)

It wraps the stdlib's `concurrent.futures` (for processes), `asyncio` (for coroutines) and
`threading` (for the runners and timeouts). `Pool.raw` is the `ProcessPoolExecutor` or the event
loop, once one is started; there is no other `.raw`.

Threads: `as_ordered`, `as_completed` and `pool` may be called from several threads at once, each
call with a run of its own. A `Pool` may be fed from several threads: `submit`, `close`, `join` and
its properties take the run's lock. `fn` itself runs on worker's threads, several at once, so
whatever it shares needs its own lock; so does `progress`, which runner threads call one at a time.
Ctrl-C once cancels queued tasks and lets running ones finish; twice stops at once.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import os
import pickle
import signal
import threading
import time
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Generic, Iterable, Iterator, NamedTuple, TypedDict, TypeVar, overload

import shared
from shared import _clock as clock
from shared._log import Progress, log

if TYPE_CHECKING:
    from typing_extensions import Unpack

__all__ = ["Error", "Pool", "Result", "StalledError", "TaskError", "as_completed", "as_ordered",
           "pool"]

# The longest wait between two tries, which caps the exponential backoff at one minute.
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

        for result in worker.as_completed(download, urls):
            print(result.item, result.value if result.ok else result.error, result.attempts)

    A `Result` is made by a runner thread and never changed after it is handed out, so any
    thread may read it.
    """

    # The task's input: the item, or a tuple of one item from each iterable when there are several.
    item: Any
    # What `fn` returned; None when it failed.
    value: R | None = None
    # What the last try raised; None when it succeeded.
    error: BaseException | None = None
    # How many tries it took, 1 when the first one did.
    attempts: int = 0
    # Where the item was in the input, which `as_ordered` sorts by.
    _position: int = field(default=0, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        """Whether the task succeeded: `error` is None."""
        return self.error is None


class Error(shared.Error):
    """The base of every error `shared.worker` raises."""


class StalledError(Error, TimeoutError):
    """One try of a task ran past the pool's `timeout`."""


class TaskError(Error):
    """Raised by `as_ordered` when some tasks failed, once every other task has finished.

        try:
            sizes = worker.as_ordered(file_size, urls)
        except worker.TaskError as error:
            failed = [result for result in error.results if not result.ok]

    `results` holds every `Result` in input order, so the work that succeeded is not lost.
    """

    def __init__(self, results: list[Result[Any]]):
        self.results = results
        failed = [result for result in results if not result.ok]
        first = failed[0]
        super().__init__(f"{len(failed)} of {len(results)} tasks failed; first: {first.item}: {_describe(first.error)}")


class _Options(TypedDict, total=False):
    """The options every entry point takes; see `as_ordered`'s docstring."""

    concurrency: int | None
    retries: int
    timeout: float | None
    label: str | None
    progress: Callable[[int, int], Any] | None
    processes: bool


@overload
def as_ordered(fn: Callable[[A1], R], items: Iterable[A1], /, **options: Unpack[_Options]) -> list[R]: ...
@overload
def as_ordered(fn: Callable[[A1, A2], R], items: Iterable[A1], second: Iterable[A2], /,
               **options: Unpack[_Options]) -> list[R]: ...
@overload
def as_ordered(fn: Callable[[A1, A2, A3], R], items: Iterable[A1], second: Iterable[A2], third: Iterable[A3], /,
               **options: Unpack[_Options]) -> list[R]: ...
@overload
def as_ordered(fn: Callable[..., Any], items: Iterable[Any], second: Iterable[Any], third: Iterable[Any],
               fourth: Iterable[Any], /, *more: Iterable[Any], **options: Unpack[_Options]) -> list[Any]: ...
def as_ordered(fn: Callable[..., Any], *iterables: Iterable[Any], **options: Any) -> list[Any]:
    """Run `fn` on every item, several at once, and return every result in input order, as a list.

        sizes = worker.as_ordered(file_size, urls, concurrency=8, retries=2, timeout=30, label="files")

    Args:
        fn: the task, a function or an `async def` function; with `processes=True`, one defined
            at the top level of a module, so it can be sent to another process.
        *iterables: the inputs. With several, `fn` gets one item from each, paired up like the
            builtin `map`, and the shortest one ends the run.
        **options: the keyword options below, each optional.
            concurrency: how many tasks run at once. None: `min(32, CPUs + 4)` threads, or one process
                per CPU with `processes=True`.
            retries: how many more tries a failed task gets, after waits of 1, 2, 4… s (at most 60 s).
            timeout: seconds before one try fails with `StalledError`; None waits forever. A
                coroutine or a process really stops; a thread can't be stopped, so it runs on in
                the background and its result is thrown away.
            label: the text of a progress row of finished tasks, drawn with `shared.log`; None draws none.
            progress: a callback `progress(done, total)`, called after each task finishes with how many
                have and how many there are: the input's length, or the number read so far when the
                input has no length. None calls nothing.
            processes: run tasks in processes, for work that computes, instead of threads.

    Returns:
        What `fn` returned for each item, in the input's order.

    Raises:
        TaskError: some task failed after its retries, raised once the others have finished.
        ValueError: `concurrency` is below 1, `retries` below 0 or `timeout` not above 0.
        TypeError: `progress` isn't callable, or, with `processes=True`, `fn` can't be sent to
            another process.

    The same options go to `as_completed` and `pool`. `progress` is called from worker's runner
    threads, one call at a time, before `as_ordered` returns; if it raises, the run stops and the
    error is raised here. It is typed for one to three iterables; with more, the results are `Any`.
    It may be called from several threads at once.
    """
    with _Run(total=_length(iterables), **options) as run:
        for args in zip(*iterables):
            run.submit(fn, args)
    results = sorted(run.finished, key=lambda result: result._position)
    if any(not result.ok for result in results):
        raise TaskError(results)
    return [result.value for result in results]


@overload
def as_completed(fn: Callable[[A1], R], items: Iterable[A1], /,
                 **options: Unpack[_Options]) -> Iterator[Result[R]]: ...
@overload
def as_completed(fn: Callable[[A1, A2], R], items: Iterable[A1], second: Iterable[A2], /,
                 **options: Unpack[_Options]) -> Iterator[Result[R]]: ...
@overload
def as_completed(fn: Callable[[A1, A2, A3], R], items: Iterable[A1], second: Iterable[A2], third: Iterable[A3], /,
                 **options: Unpack[_Options]) -> Iterator[Result[R]]: ...
@overload
def as_completed(fn: Callable[..., Any], items: Iterable[Any], second: Iterable[Any], third: Iterable[Any],
                 fourth: Iterable[Any], /, *more: Iterable[Any],
                 **options: Unpack[_Options]) -> Iterator[Result[Any]]: ...
def as_completed(fn: Callable[..., Any], *iterables: Iterable[Any], **options: Any) -> Iterator[Result[Any]]:
    """Run `fn` on every item, several at once, and yield each `Result` as soon as it is ready.

        for result in worker.as_completed(download, urls, concurrency=8):
            if not result.ok:
                log.error("{url}: {error}", url=result.item, error=result.error)

    Args:
        fn: the task, as for `as_ordered`.
        *iterables: the inputs, as for `as_ordered`.
        **options: `concurrency`, `retries`, `timeout`, `label`, `progress` and `processes`,
            as for `as_ordered`.

    Returns:
        An iterator of every task's `Result`, in the order they finish, as the stdlib's
        `concurrent.futures.as_completed` gives futures. Stopping early (a `break`) cancels the
        queued tasks and waits for the running ones.

    Raises:
        ValueError: a bad option, as for `as_ordered`.
        TypeError: with `processes=True`, `fn` can't be sent to another process.

    It never raises because a task failed: each failure is a `Result` whose `ok` is False. The
    iterator belongs to the thread that reads it; several threads may each run their own.
    """
    with _Run(total=_length(iterables), **options) as run:
        # `seen` counts the results already yielded, so each result is yielded exactly once.
        seen = 0
        # Step 1: submit inputs, yielding finished results whenever the queue has no room.
        for args in zip(*iterables):
            while not run.has_room():
                run.wait(lambda: run.has_room() or len(run.finished) > seen)
                fresh = run.finished[seen:]
                seen += len(fresh)
                yield from fresh
            run.submit(fn, args)
        # Step 2: no more inputs are coming, so drain the rest in completion order.
        run.close()
        while seen < run.submitted:
            run.wait(lambda: len(run.finished) > seen)
            fresh = run.finished[seen:]
            seen += len(fresh)
            yield from fresh


def pool(**options: Unpack[_Options]) -> Pool:
    """Start a pool that takes tasks one at a time, while other work is still going on.

        with worker.pool(concurrency=6, label="downloads") as pool:
            for post in scroll_through_feed():
                pool.submit(download, post.url)
        print(len(pool.results), "done")

    Args:
        **options: `concurrency`, `retries`, `timeout`, `label`, `progress` and `processes`, as
            for `as_ordered`. The total a progress row or `progress` sees is the number submitted
            so far, since a pool can't know how many will come.

    Returns:
        A `Pool`. Leaving its `with` block calls `close()` then `join()`; if the block raised,
        queued tasks are dropped instead, with a warning line. Without `with`, call `close()` and
        `join()` yourself.

    Raises:
        ValueError: a bad option, as for `as_ordered`.

    It never raises because a task failed. It may be called from several threads at once.
    """
    return Pool(_Run(**options))


class Pool:
    """Tasks submitted one at a time; made by `worker.pool`.

        pool = worker.pool(concurrency=6)
        pool.submit(download, url)
        pool.close()
        results = pool.join()

    Several threads may submit to one `Pool`: every member takes the run's lock.
    """

    def __init__(self, run: _Run):
        self._run = run

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        """Queue `fn(*args)`, waiting while twice `concurrency` tasks are already queued.

            pool.submit(download, url, dest)

        Args:
            fn: the task, as for `as_ordered`.
            *args: its arguments. The `Result`'s `item` is `args[0]` when there is one, else `args`.

        Raises:
            RuntimeError: the pool was closed.
            TypeError: with `processes=True`, `fn` can't be sent to another process.
        """
        self._run.submit(fn, args)

    def close(self) -> None:
        """Say that no more tasks are coming. Queued tasks still run."""
        self._run.close()

    def join(self, timeout: float | None = None) -> list[Result[Any]]:
        """Wait for every task submitted so far, or `timeout` seconds, and return `results`.

            results = pool.join(timeout=60)

        Args:
            timeout: the most seconds to wait; None waits until every task is done.

        Returns:
            The finished results, as `results` gives them; fewer than `submitted` after a timeout.
            Once the pool is closed and every task is done, the processes, the event loop and the
            progress row are ended too.
        """
        run = self._run
        if run.wait(run.is_idle, timeout) and run.closed:
            run.end(None)
        return self.results

    @property
    def results(self) -> list[Result[Any]]:
        """The finished results, in the order they finished, as a new list."""
        with self._run.changed:
            return list(self._run.finished)

    @property
    def submitted(self) -> int:
        """How many tasks were submitted, finished or not."""
        return self._run.submitted

    @property
    def raw(self) -> Any:
        """The `ProcessPoolExecutor` with `processes=True`, or the asyncio loop once an `async def`
        function was submitted; else None, since plain tasks run on worker's own threads."""
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
    """One queued task: the function, the arguments to call it with, and where it sits in the input."""

    # The task to call; a coroutine function runs on the event loop.
    fn: Callable[..., Any]
    # The positional arguments for one call of `fn`.
    args: tuple[Any, ...]
    # The Result's `item`: the single argument, or the tuple of arguments when there are several.
    item: Any
    # The task's index in submission order, which `as_ordered` sorts its results by.
    position: int


class _Run:
    """The engine under `as_ordered`, `as_completed` and `pool`: a bounded queue, runner threads, retries and stopping.

    Every count below is read and changed under `changed`, which is notified on every change: the
    caller's thread submits and waits, and every runner thread takes jobs and adds results.
    """

    def __init__(self, *, concurrency: int | None = None, retries: int = 0, timeout: float | None = None,
                 label: str | None = None, progress: Callable[[int, int], Any] | None = None,
                 processes: bool = False, total: int | None = None):
        """Check the options and set up an empty run; no thread or process starts until the first submit."""
        # Step 1: check the options before anything is started, so a bad one raises with nothing to clean up.
        if concurrency is not None and concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, not {concurrency}")
        if retries < 0:
            raise ValueError(f"retries must be 0 or more, not {retries}")
        if timeout is not None and timeout <= 0:
            raise ValueError(f"timeout must be above 0, not {timeout}")
        if progress is not None and not callable(progress):
            # Before Phase 6 `progress=` took a row's text; say where that went rather than fail later.
            raise TypeError(f"progress must be a progress(done, total) callback, not {progress!r}; "
                            "a progress row's text goes in label=")
        # Step 2: the default concurrency. Threads use the stdlib ThreadPoolExecutor's own default rule;
        # processes take one per CPU.
        cpus = os.cpu_count() or 1
        self.concurrency = concurrency or (cpus if processes else min(32, cpus + 4))
        self.retries, self.timeout, self.processes = retries, timeout, processes
        # Step 3: state shared by the caller's thread and the runners. Every field below is read and
        # changed only under `changed`, the one lock the run has.
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
        # Step 4: what is started lazily, and what must be shut down on the way out.
        self.executor: concurrent.futures.ProcessPoolExecutor | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.exits = ExitStack()
        self.bar: Progress | None = None
        if label is not None:
            self.bar = self.exits.enter_context(log.progress(label, total=total))
        self.total = total
        self.progress = progress
        # Runner threads report one at a time, so `progress` never runs twice at once and its
        # `done` never goes down; `reported` is the last `done` it was given.
        self.reporting = threading.Lock()
        self.reported = 0

    def __enter__(self) -> _Run:
        return self

    def __exit__(self, error_type: type[BaseException] | None, error: BaseException | None, traceback: Any) -> None:
        """Finish the run: stop it on an error, or close and wait for every task on a normal exit."""
        # Step 1: an error in the block stops the run at once; queued tasks are dropped.
        if error is not None:
            self.stop(error)
        else:
            # Step 2: a normal exit waits for every queued task. A Ctrl-C during that wait stops the run.
            try:
                self.close()
                self.wait(self.is_idle)
            except BaseException as late:
                self.stop(late)
                self.end(late)
                raise
        # A consumer that stops reading `as_completed` early ends it on purpose, not by failing.
        self.end(None if isinstance(error, GeneratorExit) else error)

    def has_room(self) -> bool:
        """Whether another task may be queued: fewer than twice `concurrency` are waiting."""
        # Twice the concurrency is enough queued work to keep every runner busy, without reading far ahead.
        return len(self.queued) < 2 * self.concurrency

    def is_idle(self) -> bool:
        """Whether nothing is queued and no runner holds a task."""
        return not self.queued and self.busy == 0

    def submit(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
        """Queue one task, waiting first while the queue is full, and start a runner when one is missing.

        Raises:
            RuntimeError: the run was closed before this submit.
            TypeError: with `processes`, `fn` can't be sent to another process.
        """
        # With processes, the picklability check runs first, in the caller's thread, so the error reaches the caller.
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
            if self.bar is not None and self.total is None:
                self.bar.total = self.submitted
            # Runners are added as tasks arrive, up to `concurrency`, and each exits when the queue is empty.
            if self.runners < self.concurrency:
                self.runners += 1
                threading.Thread(target=self._serve, name="shared-worker", daemon=True).start()
            self.changed.notify_all()

    def close(self) -> None:
        """Say that no more tasks will be submitted; queued tasks still run."""
        with self.changed:
            self.closed = True

    def wait(self, ready: Callable[[], bool], timeout: float | None = None) -> bool:
        """Wait until `ready()` holds, or `timeout` seconds pass. Ctrl-C here stops the run."""
        # Real time, not `clock`: the deadline bounds a real condition wait.
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
        """Start the process pool, or the event loop for a coroutine, the first time it is needed."""
        # Processes are one pool per run. A coroutine gets its own loop on a thread, so the caller stays synchronous.
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
                self._interrupt(interrupt)
            with self.changed:
                if result is not None and not self.ended:
                    self.finished.append(result)
                    if self.bar is not None:
                        self.bar.advance()
                self.changed.notify_all()
            # Reported while this task still counts as busy, so the run can't end before its last report.
            if result is not None and self.progress is not None:
                try:
                    self._report(self.progress)
                except BaseException as error:  # the caller's bug: it stops the run and reaches the caller
                    self._interrupt(error)
            with self.changed:
                self.busy -= 1
                self.changed.notify_all()

    def _interrupt(self, error: BaseException) -> None:
        """Stop the run from a runner thread; the caller's next wait raises `error`."""
        with self.changed:
            self.task_interrupt = error
            self.stop_requested.set()
            self.changed.notify_all()

    def _report(self, progress: Callable[[int, int], Any]) -> None:
        """Call `progress(done, total)` with the current count, unless another runner already did."""
        with self.reporting:
            with self.changed:
                done = len(self.finished)
                total = self.submitted if self.total is None else self.total
            if done > self.reported:
                self.reported = done
                progress(done, total)

    def _take(self) -> _Job | None:
        """Take the next queued task for a runner, marking it busy, or None when the runner should exit."""
        with self.changed:
            # A stop empties the queue for good, so a runner that finds a stop requested just exits.
            if self.queued and not self.stop_requested.is_set():
                self.busy += 1
                self.changed.notify_all()
                return self.queued.popleft()
            self.runners -= 1
            return None

    def _run_task(self, job: _Job) -> Result[Any]:
        """Try one task until it succeeds, or until its retries run out or a stop cancels the next retry.

        A task's own Exception becomes the Result's error; a BaseException such as Ctrl-C is not caught here.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return Result(job.item, self._counted_attempt(job), None, attempt, job.position)
            except Exception as error:
                # After Ctrl-C a pending retry is cancelled too: waiting a minute to retry is not "finishing".
                stopped = attempt <= self.retries and clock.sleep(_retry_wait(attempt),
                                                                   stop=self.stop_requested)
                if attempt > self.retries or stopped:
                    return Result(job.item, None, error, attempt, job.position)

    def _counted_attempt(self, job: _Job) -> Any:
        """One try, counted in `running` for as long as it lasts, so a stop can wait for it."""
        with self.changed:
            self.running += 1
        try:
            return self._attempt(job)
        finally:
            with self.changed:
                self.running -= 1
                self.changed.notify_all()

    def _attempt(self, job: _Job) -> Any:
        """One try of one task. Past `timeout` it raises `StalledError`."""
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
            raise StalledError(f"took longer than {self.timeout}s") from None


def _check_picklable(fn: Callable[..., Any]) -> None:
    """Raise TypeError with a hint when fn can't be pickled, so a process run fails before it starts."""
    # pickle is used as the test: a function that pickles is one a child process can import and run.
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
    """Run an event loop on the thread it is given to, until `end` stops it, then cancel and close it."""
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
        raise StalledError(f"took longer than {timeout}s")

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
    """The seconds to wait before the next try: 1, 2, 4… with jitter, capped at MAX_RETRY_WAIT."""
    # Jitter keeps tasks that failed together from all retrying at the same moment.
    return min(MAX_RETRY_WAIT, 2 ** (attempt - 1) * clock.random().uniform(0.75, 1.25))


def _stop_processes(executor: concurrent.futures.ProcessPoolExecutor, force: bool) -> None:
    """Shut the process pool down: wait for running tasks, or with `force` terminate the processes at once."""
    if force:
        # ProcessPoolExecutor has no public way to stop running tasks before Python 3.14, so the
        # private `_processes` map is the only handle on the child processes.
        for process in list((executor._processes or {}).values()):
            process.terminate()
    executor.shutdown(wait=not force, cancel_futures=True)


def _logging_failure(step: Callable[..., Any], *args: Any) -> None:
    """Call one shutdown step, and log a warning instead of raising when it fails."""
    # Shutdown carries on past a failure, so one broken process or loop can't make it hang.
    try:
        step(*args)
    # Exception, not BaseException: a Ctrl-C during shutdown is still left to stop the run.
    except Exception as error:
        log.warn("worker couldn't shut down cleanly: {error}", error=_describe(error))


def _length(iterables: tuple[Iterable[Any], ...]) -> int | None:
    """The shortest input's length, which is the run's total, or None when an input has no length."""
    # A generator has no len(), so the TypeError means the total is unknown and the progress row counts up instead.
    try:
        return min(len(iterable) for iterable in iterables) if iterables else 0  # pyright: ignore[reportArgumentType]
    except TypeError:
        return None


def _describe(error: BaseException | None) -> str:
    return f"{type(error).__name__} {error}".strip()
