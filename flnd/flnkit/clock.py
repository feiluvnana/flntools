"""A swappable clock: monotonic time, wall time, interruptible sleeps, and site dates.

src/docs/architecture explains it.
"""

from __future__ import annotations

import asyncio
import datetime
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol, TypeVar

__all__ = ["Clock", "Fake", "current", "day", "now", "pause", "sleep", "use", "wall"]

FAKE_WALL = 1767225600.0
POLL = 0.1
C = TypeVar("C", bound="Clock")
FIRST_YEAR = 2004
MILLISECONDS = 1e11


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
    """A clock for tests: every sleep passes at once, and is recorded.

    Args:
        start: the monotonic time to start at, in seconds.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.slept: list[float] = []
        self._time: float = start
        self._start = start
        self._lock = threading.Lock()

    @property
    def now(self) -> float:
        with self._lock:
            return self._time

    @property
    def wall(self) -> float:
        with self._lock:
            return FAKE_WALL + self._time - self._start

    def sleep(self, seconds: float, *, stop: threading.Event | None = None) -> bool:
        """Advance the fake time by `seconds` at once, and record the sleep.

        Args:
            seconds: how long to sleep; a negative value counts as zero.
            stop: when already set, nothing is recorded and this returns False.

        Returns:
            True when the sleep passed, False when `stop` was already set.
        """
        if stop is not None and stop.is_set():
            return False
        with self._lock:
            self.slept.append(seconds)
            self._time += max(0.0, seconds)
        return True

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._time += seconds


_DEFAULT: Clock = _RealClock()
_current: ContextVar[Clock] = ContextVar("flnkit.clock", default=_DEFAULT)


def current() -> Clock:
    """The active clock: the one `use` set in this context, else the real one."""
    return _current.get()


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
    """Wait `seconds` on the active clock without blocking the loop.

    Args:
        seconds: how long to wait; a negative value counts as zero.
        stop: an event that ends the wait early when it is set.

    Returns:
        True when the full time passed, False when `stop` was set first.
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
    """Make `replacement` the active clock inside the `with`, then restore the one before.

    Args:
        replacement: the clock to use inside the block.

    Yields:
        `replacement`, so the block can read or change it.
    """
    token = _current.set(replacement)
    try:
        yield replacement
    finally:
        _current.reset(token)


def day(value: object) -> datetime.date | None:
    """The UTC day of any date a site gives, or None when it isn't one or can't be right.

    "Right" means from 2004 to next year, on the active clock's wall time.

    Args:
        value: a `date` or `datetime`, a Unix time in seconds or milliseconds, an ISO 8601 text,
            or a text of digits for a Unix time. A bool is never a date.

    Returns:
        The UTC day, or None when `value` is not a date or is not right.
    """
    if isinstance(value, bool) or value is None:
        return None
    found: datetime.date | None = None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            value = int(text)
        else:
            try:
                value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if isinstance(value, datetime.datetime):
        found = (value.astimezone(datetime.UTC) if value.tzinfo else value).date()
    elif isinstance(value, datetime.date):
        found = value
    elif isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) >= MILLISECONDS else value
        try:
            found = datetime.datetime.fromtimestamp(seconds, datetime.UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
    today = datetime.datetime.fromtimestamp(wall(), datetime.UTC).date()
    if found is None or not FIRST_YEAR <= found.year <= today.year + 1:
        return None
    return found
