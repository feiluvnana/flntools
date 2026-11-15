"""Bounded thread pool with retries/progress and a background asyncio `Loop`."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import os
import threading
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

import shared
from shared import _clock as clock

__all__ = ["ClosedError", "Error", "Loop", "Pool", "Result", "StalledError", "map"]

R = TypeVar("R")


class Error(shared.Error):
    """The base of every error `shared.worker` raises."""


class ClosedError(Error, RuntimeError):
    """Raised when submitting to a closed `Pool` or `Loop`."""


class StalledError(Error, TimeoutError):
    """Raised when `Pool.join` or `Loop.run` exceeds its timeout."""


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


class Pool:
    """Thread pool supporting `submit`, `as_completed`, `join`, retries, and `stop`."""

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
            raise ValueError(f"retries must be non-negative, not {retries}")
        workers = concurrency if concurrency is not None else min(32, (os.cpu_count() or 1) + 4)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        self._retries = retries
        self._timeout = timeout
        self._progress = progress
        self._stop = stop or threading.Event()
        self._futures: list[tuple[int, Any, concurrent.futures.Future[Result[Any]]]] = []
        self._closed = False
        self._as_completed_used = False
        self._error: BaseException | None = None
        self._done = 0
        self._lock = threading.Lock()

    def __enter__(self) -> Pool:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def raw(self) -> concurrent.futures.Executor:
        """Underlying `ThreadPoolExecutor`."""
        return self._executor

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        """Schedule `fn(*args)` on the pool."""
        if self._closed or self._stop.is_set():
            raise ClosedError("pool is closed")
        item = args[0] if len(args) == 1 else (args if args else fn)
        idx = len(self._futures)
        ctx = contextvars.copy_context()
        fut = self._executor.submit(ctx.run, self._run_task, fn, args, item)
        self._futures.append((idx, item, fut))

    def _run_task(self, fn: Callable[..., Any], args: tuple[Any, ...], item: Any) -> Result[Any]:
        attempts = 0
        while True:
            if self._stop.is_set():
                return Result(
                    item=item, error=self._error or InterruptedError("stopped"), attempts=attempts,
                )
            attempts += 1
            try:
                value = fn(*args)
                return Result(item=item, value=value, attempts=attempts)
            except BaseException as error:
                if attempts > self._retries or self._stop.is_set():
                    return Result(item=item, error=error, attempts=attempts)
                clock.sleep(min(0.5 * (2 ** (attempts - 1)), 8.0), stop=self._stop)

    def as_completed(self) -> Iterator[Result[Any]]:
        """Yield `Result` objects as tasks finish; later calls yield nothing."""
        if self._as_completed_used:
            return
        self._as_completed_used = True
        futs = [f for _, _, f in self._futures]
        total = len(futs)
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=self._timeout):
                res = fut.result()
                with self._lock:
                    self._done += 1
                    if self._progress is not None:
                        self._progress(self._done, total)
                yield res
        except concurrent.futures.TimeoutError as error:
            self.stop(error)
            raise StalledError("worker pool timed out") from error

    def join(self, timeout: float | None = None) -> list[Result[Any]]:
        """Wait for all submitted tasks and return their `Result` list in submission order."""
        futs = [f for _, _, f in self._futures]
        try:
            done, not_done = concurrent.futures.wait(
                futs, timeout=timeout if timeout is not None else self._timeout
            )
            if not_done:
                raise StalledError("worker pool timed out")
            results = [self._result(item, fut) for _, item, fut in self._futures]
            if self._progress is not None and futs:
                self._progress(len(results), len(results))
            return results
        except StalledError:
            self.stop()
            raise

    @staticmethod
    def _result(item: Any, fut: concurrent.futures.Future[Result[Any]]) -> Result[Any]:
        try:
            return fut.result()
        except concurrent.futures.CancelledError:
            return Result(item=item, error=InterruptedError("stopped"))

    def run_on_owner(self, fn: Callable[[], R]) -> R:
        """Execute `fn()` directly (kept for compatibility)."""
        return fn()

    def stop(self, error: BaseException | None = None) -> None:
        """Signal the pool to stop accepting new work and cancel pending futures."""
        self._error = error
        self._stop.set()
        for _, _, fut in self._futures:
            fut.cancel()

    def close(self) -> None:
        """Shut down the underlying executor."""
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=True, cancel_futures=self._stop.is_set())


def map(
    fn: Callable[..., R],
    items: Any,
    *,
    concurrency: int | None = None,
    retries: int = 0,
    timeout: float | None = None,
    progress: Callable[[int, int], Any] | None = None,
    stop: threading.Event | None = None,
) -> list[Result[R]]:
    """Call `fn(item)` for each item on a pool and return the results in order."""
    item_list = list(items)
    with Pool(
        concurrency=concurrency, retries=retries, timeout=timeout,
        progress=progress, stop=stop,
    ) as pool:
        for item in item_list:
            pool.submit(fn, item)
        return pool.join()


class Loop:
    """Background thread running an `asyncio` event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name="shared-worker-loop", daemon=True)
        self._thread.start()

    def __enter__(self) -> Loop:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def raw(self) -> asyncio.AbstractEventLoop | None:
        """Underlying `asyncio` event loop, or `None` once closed."""
        return self._loop

    def _serve(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, R], *, timeout: float | None = None) -> R:
        """Submit `coro` to the background loop and wait for its result."""
        if self._loop is None:
            coro.close()
            raise ClosedError("loop is closed")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError as error:
            fut.cancel()
            raise StalledError("asyncio loop call timed out") from error

    def close(self) -> None:
        """Stop the event loop and join its background thread."""
        if self._loop is not None:
            loop = self._loop
            self._loop = None
            loop.call_soon_threadsafe(loop.stop)
            self._thread.join(timeout=5.0)
            loop.close()
