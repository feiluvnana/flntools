"""One clock for every module of `shared`: the time now, sleeps, and the random source for jitter.

Every wait, timestamp and jitter in `shared` goes through `now`, `wall`, `sleep` and `random`, so
a test swaps the whole clock once with `use` and runs minutes of back-off in no time.

    with clock.use(clock.Fake()) as fake:
        http.fetch("https://example.com/items")     # a 503's retry waits pass at once
        fake.slept                                  # [1.0]
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from random import Random
from typing import TypeVar

import shared

__all__ = ["Clock", "Error", "Fake", "now", "random", "sleep", "use", "wall"]

# Where a `Fake` starts its wall clock: 2026-01-01 UTC, a fixed day so a test's dates repeat.
FAKE_WALL = 1767225600.0

C = TypeVar("C", bound="Clock")


class Error(shared.Error):
    """The base of every error `shared.clock` raises."""


class Clock:
    """The real clock: monotonic seconds, the wall clock, sleeps that a stop event can end, and jitter."""

    def __init__(self) -> None:
        self.random: Random = Random()

    def now(self) -> float:
        """Monotonic seconds, for gaps and deadlines."""
        return time.monotonic()

    def wall(self) -> float:
        """Unix seconds, for a server's dates and resets and for stored expiry times."""
        return time.time()

    def sleep(self, seconds: float, *, stop: threading.Event | None = None) -> bool:
        """Wait `seconds`, or until `stop` is set; True when `stop` ended the wait.

            stopped = clock.sleep(30, stop=ctrl_c)
        """
        if stop is None:
            time.sleep(max(0.0, seconds))
            return False
        return stop.wait(max(0.0, seconds))


class Fake(Clock):
    """A clock whose sleeps pass at once and are kept in `slept`, with a seeded random source.

        fake = clock.Fake(seed=7)
        fake.advance(30)        # now() is 30 s later
    """

    def __init__(self, *, start: float = 0.0, seed: int = 0) -> None:
        super().__init__()
        self.random = Random(seed)
        self.time: float = start
        self.slept: list[float] = []
        self._start = start
        # Sleeps may come from several threads (a pool's retries); each one advances the time once.
        self._lock = threading.Lock()

    def now(self) -> float:
        """The fake's seconds, moved only by `sleep` and `advance`."""
        return self.time

    def wall(self) -> float:
        """2026-01-01 UTC at the start, moving with `now`."""
        return FAKE_WALL + self.time - self._start

    def sleep(self, seconds: float, *, stop: threading.Event | None = None) -> bool:
        """Record `seconds` and move the time on at once; True, without waiting, when `stop` is set.

            fake.sleep(2.0); fake.slept     # [2.0]
        """
        if stop is not None and stop.is_set():
            return True
        with self._lock:
            self.slept.append(seconds)
            self.time += max(0.0, seconds)
        return False

    def advance(self, seconds: float) -> None:
        """Move the time on by `seconds` without recording a sleep."""
        with self._lock:
            self.time += seconds


_current = Clock()


def now() -> float:
    """Monotonic seconds on the clock in use."""
    return _current.now()


def wall() -> float:
    """Unix seconds on the clock in use."""
    return _current.wall()


def sleep(seconds: float, *, stop: threading.Event | None = None) -> bool:
    """Wait `seconds` on the clock in use, or until `stop` is set; True when `stop` ended it.

        clock.sleep(1.5)
    """
    return _current.sleep(seconds, stop=stop)


def random() -> Random:
    """The random source of the clock in use, for jitter a test wants to repeat."""
    return _current.random


@contextmanager
def use(replacement: C) -> Iterator[C]:
    """Make `replacement` the clock of every module until the block ends.

        with clock.use(clock.Fake()) as fake:
            ...
    """
    global _current
    previous: Clock = _current
    _current = replacement
    try:
        yield replacement
    finally:
        _current = previous
