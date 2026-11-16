"""A swappable clock: monotonic time, wall time, interruptible sleeps, and site dates.

A library module. It imports only the stdlib, so any program can use it; inside flnd every
module that waits or reads the time goes through it (the HTTP client's backoff, the browser's
scrolls, the progress bars, the run's timestamps, the sources' pacing), and the tests swap in
`Fake` so no test ever really sleeps.

What it offers:
- `now()` and `wall()`: monotonic seconds (for measuring) and Unix seconds (for dates);
- `sleep(seconds, stop=…)`: blocks a thread, and wakes early when the `threading.Event` is set;
- `pause(seconds, stop=…)`: the same for a coroutine on an asyncio loop, without blocking it;
- `use(clock)`: makes another clock the active one for the current context, for example `Fake`;
- `day(value)`: turns any date a site gives (a date, a datetime, Unix seconds or milliseconds,
  an ISO 8601 string) into one UTC day, or None when it can't be a real post date.

How a call finds its clock:
1. `use` sets a `ContextVar`. A thread or task started inside the `with` inherits it, since
   asyncio copies the context into each task (and `asyncio.to_thread` into its thread); a plain
   `threading.Thread` does not, so a test
   that starts threads installs the fake before them.
2. `now`, `wall`, `sleep` and `pause` read the variable on every call, so swapping the clock
   needs no change at the call sites.
3. Outside any `use`, the variable holds the one real clock, `_RealClock`.

Rules a change must keep:
- `Fake` is shared by threads (a download pool sleeps on it), so every read and write of its
  time holds its lock.
- `pause` must never block the loop: on the real clock it only awaits `asyncio.sleep`.
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
    """A clock for tests: every sleep passes at once, and is recorded.

    Time moves only when code sleeps on it or a test calls `advance`, so a test that checks a
    backoff or a timeout runs instantly and gives the same result every time. Its wall time starts
    at 2026-01-01 UTC, so formatted dates are repeatable. It is thread-safe.

    Args:
        start: the monotonic seconds it starts at; its wall time still starts at 2026-01-01.

    Example:
        >>> clock = Fake()
        >>> with use(clock):
        ...     sleep(2.5)
        True
        >>> clock.slept, clock.now
        ([2.5], 2.5)
    """

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
        """Record a sleep and move time forward by it, at once.

        Args:
            seconds: how long the caller asked to sleep; a negative value moves time by 0, but
                is still recorded as asked.
            stop: when it is already set, nothing is recorded and the sleep ends at once.

        Returns:
            False when `stop` was set, so the caller sees an interrupted sleep; True otherwise.
        """
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
    """Make `replacement` the active clock inside the `with`, then restore the one before.

    Uses nest: the innermost wins, and each exit restores what was active when it entered.

    Args:
        replacement: the clock every `now`, `wall`, `sleep` and `pause` uses meanwhile, usually
            a `Fake`.

    Yields:
        `replacement` itself, so `with use(Fake()) as clock:` keeps a handle on it.
    """
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
