"""One clock for every module of `shared`: monotonic time, wall time, and interruptible sleeps."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol, TypeVar

import shared

__all__ = ["Clock", "Error", "Fake", "now", "sleep", "use", "wall"]

# 2026-01-01 UTC, so a test's formatted dates are repeatable.
FAKE_WALL = 1767225600.0

C = TypeVar("C", bound="Clock")


class Error(shared.Error):
    """The base of every error `shared.clock` raises."""


class Clock(Protocol):
    """Protocol defining monotonic time, wall time, and interruptible sleep."""

    def now(self) -> float:
        """Return monotonic clock seconds."""
        ...

    def wall(self) -> float:
        """Return wall-clock Unix timestamp seconds."""
        ...

    def sleep(self, seconds: float, *, stop: threading.Event | None = None) -> bool:
        """Sleep for `seconds` or until `stop` is set; return True if completed."""
        ...


class _RealClock:
    """The real system clock."""

    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def sleep(self, seconds: float, *, stop: threading.Event | None = None) -> bool:
        if stop is None:
            time.sleep(max(0.0, seconds))
            return True
        return not stop.wait(max(0.0, seconds))


class Fake(Clock):
    """Deterministic in-memory `Clock` implementation for tests."""

    def __init__(self, start: float = 0.0) -> None:
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
        """Record `seconds` and advance time at once; return False immediately if `stop` is set."""
        if stop is not None and stop.is_set():
            return False
        with self._lock:
            self.slept.append(seconds)
            self._time += max(0.0, seconds)
        return True

    def advance(self, seconds: float) -> None:
        """Move the fake's time forward by `seconds` without recording a sleep."""
        with self._lock:
            self._time += seconds


_DEFAULT: Clock = _RealClock()
_current: ContextVar[Clock] = ContextVar("shared.clock", default=_DEFAULT)


def now() -> float:
    """Return monotonic seconds on the active clock."""
    return _current.get().now()


def wall() -> float:
    """Return wall-clock Unix seconds on the active clock."""
    return _current.get().wall()


def sleep(seconds: float, *, stop: threading.Event | None = None) -> bool:
    """Sleep on the active clock, waking early if `stop` is set."""
    return _current.get().sleep(seconds, stop=stop)


@contextmanager
def use(replacement: C) -> Iterator[C]:
    """Install `replacement` as the active clock for the current context."""
    token = _current.set(replacement)
    try:
        yield replacement
    finally:
        _current.reset(token)
