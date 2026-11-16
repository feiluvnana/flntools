"""One clock for every private module of flnd: monotonic time, wall time, interruptible sleeps.

`sleep` blocks a thread; `pause` is its twin for a coroutine on an asyncio loop, which waits
without blocking the loop. Both follow the active clock (`use`), so a test's `Fake` makes every
wait pass at once and records it in `slept`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol, TypeVar


__all__ = ["Clock", "Fake", "now", "pause", "sleep", "use", "wall"]

# 2026-01-01 UTC, so a test's formatted dates are repeatable.
FAKE_WALL = 1767225600.0
# How often a `pause` on the real clock looks at its `stop` event, in seconds: an event can't wake
# a coroutine, so it is polled.
POLL = 0.1

C = TypeVar("C", bound="Clock")


class Clock(Protocol):
    """Protocol defining monotonic time, wall time, and interruptible sleep."""

    @property
    def now(self) -> float:
        """Return monotonic clock seconds."""
        ...

    @property
    def wall(self) -> float:
        """Return wall-clock Unix timestamp seconds."""
        ...

    def sleep(self, seconds: float, *, stop: threading.Event | None = None) -> bool:
        """Sleep for `seconds` or until `stop` is set; return True if completed."""
        ...


class _RealClock:
    """The real system clock."""

    @property
    def now(self) -> float:
        return time.monotonic()

    @property
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

    @property
    def now(self) -> float:
        """Return the fake's monotonic seconds, moved only by `sleep` and `advance`."""
        with self._lock:
            return self._time

    @property
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
_current: ContextVar[Clock] = ContextVar("flnd._time", default=_DEFAULT)


def now() -> float:
    """Return monotonic seconds on the active clock."""
    return _current.get().now


def wall() -> float:
    """Return wall-clock Unix seconds on the active clock."""
    return _current.get().wall


def sleep(seconds: float, *, stop: threading.Event | None = None) -> bool:
    """Sleep on the active clock, waking early if `stop` is set."""
    return _current.get().sleep(seconds, stop=stop)


async def pause(seconds: float, *, stop: threading.Event | None = None) -> bool:
    """Wait `seconds` on the active clock without blocking the loop; False if `stop` was set.

    On the real clock the wait is `asyncio.sleep`, in steps of `POLL` while a `stop` is given;
    on a fake clock it is the fake's `sleep`, which passes at once.

    Args:
        seconds: how long; 0 or less returns at once.
        stop: an event that ends the wait early.

    Returns:
        True when the wait ran its full time.
    """
    clock = _current.get()
    if not isinstance(clock, _RealClock):
        return clock.sleep(seconds, stop=stop)
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if stop is not None and stop.is_set():
            return False
        left = end - time.monotonic()
        if left <= 0:
            return True
        await asyncio.sleep(left if stop is None else min(left, POLL))


@contextmanager
def use(replacement: C) -> Iterator[C]:
    """Install `replacement` as the active clock for the current context."""
    token = _current.set(replacement)
    try:
        yield replacement
    finally:
        _current.reset(token)
