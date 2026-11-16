"""One clock for every private module of flnd: monotonic time, wall time, interruptible sleeps.

`sleep` blocks a thread; `pause` is its twin for a coroutine on an asyncio loop, which waits
without blocking the loop. Both follow the active clock (`use`), so a test's `Fake` makes every
wait pass at once and records it in `slept`.
A library module: it imports nothing of flnd. Besides the clock (`now`, `wall`, `sleep`, and
`use` to swap in a `Fake` for tests), it reads the dates sites give: `day` turns a date, a
datetime, Unix seconds or milliseconds, or an ISO 8601 string into one UTC day.
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


__all__ = ["Clock", "Fake", "day", "now", "pause", "sleep", "use", "wall"]

# 2026-01-01 UTC, so a test's formatted dates are repeatable.
FAKE_WALL = 1767225600.0
# How often a `pause` on the real clock looks at its `stop` event, in seconds: an event can't wake
# a coroutine, so it is polled.
POLL = 0.1

C = TypeVar("C", bound="Clock")
# The oldest year a post can be from: the sites flnd reads all started later. A timestamp before
# it is a mistake for another number, such as an id read as seconds.
FIRST_YEAR = 2004
# Seconds since 1970 pass this in 5138; a larger number is milliseconds (13 digits today, 10 for
# seconds).
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


def day(value: object) -> datetime.date | None:
    """The UTC day of any date a site gives, or None when it isn't one or can't be right.

    Args:
        value: a `date`, a `datetime` (naive is read as UTC), Unix seconds or milliseconds as an
            int, float or digit string, an ISO 8601 string, or None.

    Returns:
        The day in UTC; None for None, text that is none of those, a number too large for a date,
        and any day before 2004 or after next year, which are mistakes for another number.

    Example:
        >>> day(1767312000), day("1767312000000"), day("2026-01-02T01:58:16Z")
        (datetime.date(2026, 1, 2), datetime.date(2026, 1, 2), datetime.date(2026, 1, 2))
        >>> day("soon") is None, day(12) is None
        (True, True)
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
        found = (value.astimezone(datetime.timezone.utc) if value.tzinfo else value).date()
    elif isinstance(value, datetime.date):
        found = value
    elif isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) >= MILLISECONDS else value
        try:
            found = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).date()
        # Each platform refuses a huge timestamp its own way: OverflowError, OSError or ValueError.
        except (OverflowError, OSError, ValueError):
            return None
    if found is None or not FIRST_YEAR <= found.year <= datetime.date.today().year + 1:
        return None
    return found
