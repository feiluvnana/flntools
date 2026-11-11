"""Run work in parallel with one call: `map`, `each`, `pool`, or the `task` decorator.

Tasks run in threads by default, which suits I/O. `processes=True` is for CPU-heavy work.

A Python thread cannot be killed. A task past `timeout` is marked failed with
`TimeoutError` and its worker slot is replaced, but the thread keeps running in the
background until the function returns, and that result is discarded. Use the library's
own timeout (httpx, Playwright) for a real limit; `timeout` here is a safety net.

Pools nest: an outer pool of 2 whose tasks each run a pool of 6 runs up to 12 tasks at
once. There is no global thread limit.
"""

from __future__ import annotations

import concurrent.futures
import functools
import os
import pickle
import random
import threading
import time
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Iterable, Iterator, NamedTuple, TypeVar, overload

from shared._log import log

MAX_BACKOFF = 60.0
ERROR_MODES = ("raise", "log")
# Waits wake at least this often, so Ctrl-C is seen on platforms where a blocked lock ignores it.
WAKE_INTERVAL = 0.25
# Rate limits and retry backoff read time through these, so tests can swap in a fake clock.
_clock = time.monotonic
_sleep = time.sleep


R = TypeVar("R")
A1 = TypeVar("A1")
A2 = TypeVar("A2")
A3 = TypeVar("A3")


@dataclass
class Result(Generic[R]):
    item: Any
    value: R | None = None
    error: BaseException | None = None
    attempts: int = 0
    seconds: float = 0.0
    _index: int = field(default=0, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.error is None


class TaskError(Exception):
    """Some tasks failed. `failed` and `results` keep every outcome, so finished work is not lost."""

    def __init__(self, failed: list[Result], results: list[Result]):
        self.failed, self.results = failed, results
        first = failed[0]
        super().__init__(f"{len(failed)} of {len(results)} tasks failed; first: {first.item}: {_describe(first.error)}")


@overload
def map(fn: Callable[[A1], R], items: Iterable[A1], /, **options: Any) -> list[R]: ...
@overload
def map(fn: Callable[[A1, A2], R], items: Iterable[A1], second: Iterable[A2], /, **options: Any) -> list[R]: ...
@overload
def map(fn: Callable[[A1, A2, A3], R], items: Iterable[A1], second: Iterable[A2], third: Iterable[A3], /,
        **options: Any) -> list[R]: ...
@overload
def map(fn: Callable[..., Any], items: Iterable, second: Iterable, third: Iterable, fourth: Iterable, /,
        *more: Iterable, **options: Any) -> list[Any]: ...
def map(fn: Callable, *iterables: Iterable, **options: Any) -> list:
    """Like the builtin `map`, but parallel. Results come back in input order.

    Like the builtin, it is typed for one to three iterables; with more, the results are `Any`.
    """
    with Pool(**options, total=_length(iterables)) as running:
        for index, args in enumerate(zip(*iterables)):
            running._submit(fn, args, index)
    return [result.value for result in sorted(running.results, key=lambda result: result._index)]


def each(fn: Callable, *iterables: Iterable, **options: Any) -> Iterator[Result]:
    """Yield a `Result` as each task finishes. Never raises `TaskError`."""
    running = Pool(**options, total=_length(iterables))
    running._raise_failures = False
    running._keep_unseen = True
    with running:
        for index, args in enumerate(zip(*iterables)):
            while not running._submit(fn, args, index, block=False):
                yield from running._take_unseen(wait=True)
            yield from running._take_unseen(wait=False)
        running._close_input()
        while not running._idle():
            yield from running._take_unseen(wait=True)
        yield from running._take_unseen(wait=False)


def pool(**options: Any) -> Pool:
    """A pool to `submit` work to while other work runs. Leaving the block waits for every task."""
    return Pool(**options)


def task(**options: Any) -> Callable[[Callable], Task]:
    """Attach options to a function: `fn(...)` is still a plain call, `fn.map` and `fn.each` run in parallel."""
    _check_options(**options)
    return lambda fn: Task(fn, options)


class Task:
    def __init__(self, fn: Callable, options: dict):
        self.fn, self.options = fn, options
        functools.update_wrapper(self, fn)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.fn(*args, **kwargs)

    def map(self, *iterables: Iterable, **options: Any) -> list:
        return map(self.fn, *iterables, **{**self.options, **options})

    def each(self, *iterables: Iterable, **options: Any) -> Iterator[Result]:
        return each(self.fn, *iterables, **{**self.options, **options})


class Job(NamedTuple):
    fn: Callable
    args: tuple
    item: Any
    position: int


class Slot:
    """One worker thread's state, read and changed under the pool's condition."""

    def __init__(self) -> None:
        self.job: Job | None = None
        self.deadline: float | None = None
        self.abandoned = False


class Pool:
    def __init__(self, *, workers: int | None = None, retries: int = 0, backoff: float = 1.0,
                 retryable: type | tuple = (Exception,), rate: float | None = None, timeout: float | None = None,
                 progress: str | None = None, errors: str = "raise", setup: Callable | None = None,
                 processes: bool = False, total: int | None = None):
        _check_options(workers=workers, retries=retries, rate=rate, timeout=timeout, errors=errors, setup=setup,
                       processes=processes)
        cpus = os.cpu_count() or 1
        self.workers = workers or (cpus if processes else min(32, cpus + 4))
        self.retries, self.backoff, self.retryable = retries, backoff, retryable
        self.timeout, self.progress, self.errors, self.setup = timeout, progress, errors, setup
        self.results: list[Result] = []
        self.submitted = self.running = self.succeeded = self.failed = 0
        self._total, self._bar = total, None
        self._limiter = RateLimiter(rate) if rate else None
        self._executor = concurrent.futures.ProcessPoolExecutor(self.workers) if processes else None
        self._cond = threading.Condition()
        self._queue: deque[Job] = deque()
        self._live: list[Slot] = []
        self._idle_workers = 0
        self._closed = False
        self._setup_failures = 0
        self._setup_error: Exception | None = None
        self._fatal: BaseException | None = None
        self._raise_failures, self._keep_unseen = True, False
        self._unseen: list[Result] = []
        self._exits = ExitStack()

    def submit(self, fn: Callable, *args: Any) -> None:
        """Queue `fn(*args)`. Blocks while the queue is full."""
        self._submit(fn, args, self.submitted)

    def __enter__(self) -> Pool:
        if self.progress is not None:
            self._bar = self._exits.enter_context(log.progress(self.progress, total=self._total))
        if self.timeout is not None and self._executor is None:
            threading.Thread(target=self._watch_timeouts, name="shared-worker-timeouts", daemon=True).start()
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        try:
            if exc is not None:
                self._stop(interrupted=isinstance(exc, KeyboardInterrupt))
                return
            try:
                self._close_input()
                self._join()
            except KeyboardInterrupt as interrupt:  # Ctrl-C while waiting for the last tasks
                exc = interrupt
                self._stop(interrupted=True)
                raise
            if self._fatal is not None:
                raise self._fatal
            if self.failed and self._bar is not None:
                self._bar.fail()
            if self.failed and self.errors == "raise" and self._raise_failures:
                raise TaskError([result for result in self.results if not result.ok], list(self.results))
        finally:
            self._exits.__exit__(*((type(exc), exc, None) if exc else (None, None, None)))

    def _submit(self, fn: Callable, args: tuple, index: int, *, block: bool = True) -> bool:
        if self._executor is not None and self.submitted == 0:
            _check_picklable(fn, args)
        job = Job(fn, args, args[0] if len(args) == 1 else args, index)
        with self._cond:
            while len(self._queue) >= self.workers * 2 and self._fatal is None:
                if not block:
                    return False
                self._cond.wait(WAKE_INTERVAL)
            if self._fatal is not None:
                raise self._fatal
            if self._closed:
                raise RuntimeError("this pool no longer takes tasks")
            self._queue.append(job)
            self.submitted += 1
            if self._bar is not None and self._total is None:
                self._bar.total = self.submitted
            self._spawn_workers()
            self._cond.notify_all()
        return True

    def _take_unseen(self, *, wait: bool) -> list[Result]:
        with self._cond:
            if wait and not self._unseen:
                self._cond.wait(WAKE_INTERVAL)
            unseen, self._unseen = self._unseen, []
        if self._fatal is not None:
            raise self._fatal
        return unseen

    def _idle(self) -> bool:
        with self._cond:
            return not self._queue and self.running == 0

    def _close_input(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _stop(self, *, interrupted: bool) -> None:
        """Drop queued tasks, then wait for running ones. A second Ctrl-C while waiting raises at once."""
        with self._cond:
            self._queue.clear()
            running = self.running
        self._close_input()
        if interrupted and running:
            log.warn("stopping: waiting for {n} running tasks (Ctrl-C again to force)", n=running)
        self._join()

    def _join(self) -> None:
        with self._cond:
            while self._live:
                self._cond.wait(WAKE_INTERVAL)
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)

    def _spawn_workers(self) -> None:
        if self._setup_failures >= self.workers:
            self._fail_queued(self._setup_error)
        while self._idle_workers < len(self._queue) and len(self._live) + self._setup_failures < self.workers:
            slot = Slot()
            self._live.append(slot)
            self._idle_workers += 1
            threading.Thread(target=self._work, args=(slot,), name="shared-worker", daemon=True).start()

    def _work(self, slot: Slot) -> None:
        resources = ExitStack()
        try:
            resource, ready = None, self.setup is None
            while (job := self._next_job(slot)) is not None:
                if not ready and self.setup is not None:
                    try:
                        value = self.setup()
                        is_context = hasattr(value, "__enter__") and hasattr(value, "__exit__")
                        resource = resources.enter_context(value) if is_context else value
                        ready = True
                    except Exception as error:
                        self._setup_failed(slot, job, error)
                        return
                result = self._run(job, resource, slot)
                self._finish(slot, result)
        except BaseException as error:  # KeyboardInterrupt or SystemExit raised by a task
            with self._cond:
                self._fatal = error
                self._queue.clear()
        finally:
            resources.close()
            with self._cond:
                if slot in self._live:
                    self._live.remove(slot)
                    if slot.job is None:
                        self._idle_workers -= 1
                    else:
                        self.running -= 1
                self._cond.notify_all()

    def _next_job(self, slot: Slot) -> Job | None:
        with self._cond:
            while not slot.abandoned:
                if self._queue and self._fatal is None:
                    slot.job = self._queue.popleft()
                    self._idle_workers -= 1
                    self.running += 1
                    self._cond.notify_all()
                    return slot.job
                if self._closed or self._fatal is not None:
                    return None
                self._cond.wait()
            return None

    def _run(self, job: Job, resource: Any, slot: Slot) -> Result:
        started, attempts = time.monotonic(), 0
        args = job.args if self.setup is None else (resource, *job.args)
        while True:
            attempts += 1
            if self._limiter is not None:
                self._limiter.wait()
            try:
                value = self._attempt(job.fn, args, slot)
            except Exception as error:
                # A timed-out attempt may still be running, so it is never retried.
                timed_out = slot.abandoned or (self._executor is not None and isinstance(error, TimeoutError))
                if attempts <= self.retries and isinstance(error, self.retryable) and not timed_out:
                    _sleep(_backoff_wait(self.backoff, attempts))
                    continue
                return Result(job.item, None, error, attempts, time.monotonic() - started, job.position)
            return Result(job.item, value, None, attempts, time.monotonic() - started, job.position)

    def _attempt(self, fn: Callable, args: tuple, slot: Slot) -> Any:
        if self._executor is not None:
            try:
                return self._executor.submit(fn, *args).result(timeout=self.timeout)
            except concurrent.futures.TimeoutError:
                raise TimeoutError(f"task took longer than {self.timeout}s") from None
        with self._cond:
            slot.deadline = None if self.timeout is None else time.monotonic() + self.timeout
            self._cond.notify_all()
        try:
            return fn(*args)
        finally:
            with self._cond:
                slot.deadline = None

    def _finish(self, slot: Slot, result: Result) -> None:
        with self._cond:
            if slot.abandoned:
                return
            slot.job = None
            self.running -= 1
            self._idle_workers += 1
            self._record(result)

    def _record(self, result: Result) -> None:
        """Count a finished task. Called under the condition."""
        self.results.append(result)
        if result.ok:
            self.succeeded += 1
        else:
            self.failed += 1
            if self.errors == "log" and result.error is not self._setup_error:
                log.error("{item}: {error}", item=result.item, error=_describe(result.error))
        if self._keep_unseen:
            self._unseen.append(result)
        if self._bar is not None:
            self._bar.advance()
        self._cond.notify_all()

    def _setup_failed(self, slot: Slot, job: Job, error: Exception) -> None:
        """Retire this worker's slot and give its job back. When every slot has failed, fail the queue."""
        with self._cond:
            if self._setup_error is None:
                self._setup_error = error
                if self.errors == "log":
                    log.error("setup failed: {error}", error=_describe(error))
            self._setup_failures += 1
            self.running -= 1
            slot.job = None
            self._live.remove(slot)
            self._queue.appendleft(job)
            self._spawn_workers()

    def _fail_queued(self, error: Exception | None) -> None:
        while self._queue:
            job = self._queue.popleft()
            self._record(Result(job.item, None, error, 0, 0.0, job.position))

    def _watch_timeouts(self) -> None:
        with self._cond:
            while not (self._closed and not self._live):
                now = time.monotonic()
                for slot in [slot for slot in self._live if slot.deadline is not None and slot.deadline <= now]:
                    # Only a running job under a timeout has a deadline.
                    assert slot.job is not None and self.timeout is not None
                    slot.abandoned = True
                    self._live.remove(slot)
                    self.running -= 1
                    error = TimeoutError(f"task took longer than {self.timeout}s")
                    self._record(Result(slot.job.item, None, error, 1, self.timeout, slot.job.position))
                    self._spawn_workers()
                deadlines = [slot.deadline for slot in self._live if slot.deadline is not None]
                self._cond.wait(min(deadlines) - now if deadlines else WAKE_INTERVAL)


class RateLimiter:
    """At most `rate` starts per second, spaced evenly and shared by every worker."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self.next_start = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = _clock()
            start = max(now, self.next_start)
            self.next_start = start + self.interval
        if start > now:
            _sleep(start - now)


def _check_options(*, workers: int | None = None, retries: int = 0, rate: float | None = None,
                   timeout: float | None = None, errors: str = "raise", setup: Callable | None = None,
                   processes: bool = False, **_: Any) -> None:
    if workers is not None and workers < 1:
        raise ValueError(f"workers must be at least 1, not {workers}")
    if retries < 0:
        raise ValueError(f"retries must be 0 or more, not {retries}")
    if rate is not None and rate <= 0:
        raise ValueError(f"rate must be above 0, not {rate}")
    if timeout is not None and timeout <= 0:
        raise ValueError(f"timeout must be above 0, not {timeout}")
    if errors not in ERROR_MODES:
        raise ValueError(f"errors must be one of {', '.join(ERROR_MODES)}, not {errors!r}")
    if setup is not None and processes:
        raise ValueError("setup works with threads only: a resource cannot cross processes")


def _check_picklable(fn: Callable, args: tuple) -> None:
    try:
        pickle.dumps((fn, args))
    except Exception as error:
        raise TypeError(f"processes=True needs a module-level function and picklable arguments, "
                        f"not {getattr(fn, '__qualname__', fn)!r}: {error}") from None


def _backoff_wait(backoff: float, attempt: int) -> float:
    return min(MAX_BACKOFF, backoff * 2 ** (attempt - 1) * random.uniform(0.75, 1.25))


def _length(iterables: tuple) -> int | None:
    try:
        return min(len(iterable) for iterable in iterables) if iterables else 0
    except TypeError:
        return None


def _describe(error: BaseException | None) -> str:
    return f"{type(error).__name__} {error}".strip()
