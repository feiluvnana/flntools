"""One clock for every module of `shared`: monotonic time, wall time, sleeps, and jitter."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from random import Random
from typing import TypeVar

import shared

__all__ = ["Clock", "Error", "Fake", "now", "random", "sleep", "use", "wall"]

# 2026-01-01 UTC, so a test's formatted dates are repeatable.
FAKE_WALL = 1767225600.0

C = TypeVar("C", bound="Clock")


class Error(shared.Error):
    """The base of every error `shared.clock` raises."""


class Clock:
    """The real clock: monotonic seconds, wall time, interruptible sleeps, and jitter."""

    def __init__(self) -> None:
        self.random: Random = Random()

    def now(self) -> float:
        """Return monotonic seconds for measuring intervals and deadlines."""
        return time.monotonic()

    def wall(self) -> float:
        """Return Unix seconds for wall-clock timestamps and expiry times."""
        return time.time()

    def sleep(self, seconds: float, *, stop: threading.Event | None = None) -> bool:
        """Wait `seconds`, or until `stop` is set; return True when `stop` ended the wait."""
        if stop is None:
            time.sleep(max(0.0, seconds))
            return False
        return stop.wait(max(0.0, seconds))


class Fake(Clock):
    """A clock whose sleeps pass at once and are recorded in `slept`, with seeded random."""

    def __init__(self, *, start: float = 0.0, seed: int = 0) -> None:
        super().__init__()
        self.random = Random(seed)
        self.slept: list[float] = []
        self._time: float = start
        self._start = start
        self._lock = threading.Lock()

    def now(self) -> float:
        """Return the fake's monotonic seconds, moved only by `sleep` and `advance`."""
        with self._lock:
            return self._time

    def wall(self) -> float:
        """Return the fake's Unix seconds, starting at 2026-01-01 UTC and moving with `now`."""
        with self._lock:
            return FAKE_WALL + self._time - self._start

    def sleep(self, seconds: float, *, stop: threading.Event | None = None) -> bool:
        """Record `seconds` and advance time at once; return True immediately if `stop` is set."""
        if stop is not None and stop.is_set():
            return True
        with self._lock:
            self.slept.append(seconds)
            self._time += max(0.0, seconds)
        return False

    def advance(self, seconds: float) -> None:
        """Move the fake's time forward by `seconds` without recording a sleep."""
        with self._lock:
            self._time += seconds


_DEFAULT = Clock()
_current: ContextVar[Clock] = ContextVar("shared.clock", default=_DEFAULT)


def now() -> float:
    """Return monotonic seconds on the clock in use."""
    return _current.get().now()


def wall() -> float:
    """Return Unix seconds on the clock in use."""
    return _current.get().wall()


def sleep(seconds: float, *, stop: threading.Event | None = None) -> bool:
    """Wait `seconds` on the clock in use, or until `stop` is set."""
    return _current.get().sleep(seconds, stop=stop)


def random() -> Random:
    """Return the random source of the clock in use."""
    return _current.get().random


@contextmanager
def use(replacement: C) -> Iterator[C]:
    """Install `replacement` as the clock in use for the current context."""
    token = _current.set(replacement)
    try:
        yield replacement
    finally:
        _current.reset(token)
