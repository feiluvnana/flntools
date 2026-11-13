"""How long `http.Client` waits before each request to a host: as little as the host tolerates.

The client keeps one `Host` per host. A request waits for the latest of: the host's rate headers
once 5 or fewer requests are left until their reset; each budget's sliding window; the learned
gap (the floor times the learned speed, plus up to 40% jitter); and a 429's back-off (its
`Retry-After`, else 30 s doubling to 15 min). The learned speed runs from 1x to 8x: each 429
doubles it, and every 50 clean requests bring it 20% back toward 1x. Private: callers use `http`.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field

from shared import _clock as clock

__all__ = ["Host", "Pace"]

# The learned speed's bounds, how many clean requests ease it, and by how much.
SLOWEST, CLEAN_STEP, EASE = 8.0, 50, 0.8
# Jitter adds up to this share of the gap, never takes any off: a floor is a floor.
JITTER = 0.4
# Rate headers count down to 0; with this many left, wait for the reset (gallery-dl's rule for X).
HEADER_RESERVE = 5
# Budgets keep 5% of their window as a margin, as Instaloader adds seconds to Instagram's.
MARGIN = 0.05
# A reset larger than this is a Unix time (X's), not seconds from now (the IETF draft's).
EPOCH = 1e9
# A 429 without `Retry-After` waits 30 s, then 60 s, 120 s, … up to 15 min, give or take 20%.
FIRST_BACKOFF, MAX_BACKOFF, BACKOFF_JITTER = 30.0, 900.0, 0.2


@dataclass(frozen=True)
class Pace:
    """The pace a client keeps with the hosts it names: a floor between requests, and budgets.

        http.Pace(0.5, {"/graphql": (275, 600)}, domains=("example.com",))
    """

    # The least seconds between two requests to one host at 1x; jitter adds up to 40% on top.
    floor: float = 0.0
    # A URL part -> (requests, per seconds): at most that many requests with it in any window.
    budgets: Mapping[str, tuple[int, float]] = field(default_factory=dict)
    # The hosts the floor and budgets apply to, with their subdomains; empty means every host.
    domains: tuple[str, ...] = ()

    def paces(self, host: str) -> bool:
        """Whether the floor and budgets apply to `host`."""
        domains = [domain.lower().lstrip(".") for domain in self.domains]
        return not domains or any(host == domain or host.endswith("." + domain)
                                  for domain in domains)


class Host:
    """One host's pace in a run: its learned speed, budget windows, header hold and back-off."""

    def __init__(self, name: str, pace: Pace | None, *, speed: float = 1.0):
        self.name = name
        paced = pace is not None and pace.paces(name)
        self.floor = pace.floor if pace is not None and paced else 0.0
        self.budgets = dict(pace.budgets) if pace is not None and paced else {}
        self.speed = min(max(speed, 1.0), SLOWEST)
        self.strikes = 0
        self._last: float | None = None
        # Until when a rate header or a back-off holds every request, on the monotonic clock.
        self._blocked_until = 0.0
        self._backoff_until = 0.0
        self._windows = {key: deque[float]() for key in self.budgets}
        self._clean = 0
        # Every thread of a run asks the same host for turns.
        self._lock = threading.Lock()

    def turn(self, url: str) -> float:
        """Reserve the next request to `url`, and return the seconds to wait before sending it."""
        with self._lock:
            now = clock.now()
            ready = self._blocked_until
            if self._last is not None and self.floor:
                gap = self.floor * self.speed * clock.random().uniform(1, 1 + JITTER)
                ready = max(ready, self._last + gap)
            counted = [key for key in self.budgets if key in url]
            for key in counted:
                requests, per = self.budgets[key]
                window = self._windows[key]
                if len(window) >= requests:
                    # The oldest of the last `requests` must leave the window, margin and all.
                    ready = max(ready, window[-requests] + per * (1 + MARGIN))
            start = max(now, ready)
            self._last = start
            for key in counted:
                window = self._windows[key]
                window.append(start)
                while len(window) > self.budgets[key][0]:
                    window.popleft()
            return start - now

    def record(self, headers: Mapping[str, str]) -> None:
        """Read a response's rate headers: with 5 or fewer left, hold the host till the reset."""
        for prefix in ("x-rate-limit-", "x-ratelimit-"):
            try:
                remaining = int(headers[prefix + "remaining"])
                reset = float(headers[prefix + "reset"])
            except (KeyError, ValueError):  # missing or not a number: this response says nothing
                continue
            if remaining <= HEADER_RESERVE:
                seconds = reset - clock.wall() if reset > EPOCH else reset
                with self._lock:
                    until = clock.now() + max(seconds, 0.0) + 1.0
                    self._blocked_until = max(self._blocked_until, until)
            return

    def throttled(self, retry_after: float | None) -> float | None:
        """Count a 429: double the speed and hold the host; the seconds held, or None if held."""
        with self._lock:
            now = clock.now()
            # Requests running together see the same 429: that is one wait, not one each.
            if now < self._backoff_until:
                return None
            self.strikes += 1
            self.speed = min(self.speed * 2, SLOWEST)
            self._clean = 0
            seconds = retry_after
            if seconds is None:
                step = min(FIRST_BACKOFF * 2 ** (self.strikes - 1), MAX_BACKOFF)
                seconds = step * clock.random().uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)
            self._backoff_until = now + seconds
            self._blocked_until = max(self._blocked_until, self._backoff_until)
            return seconds

    def clean(self) -> None:
        """Count a request that went through: a 429 waits 30 s again, and 50 ease the speed."""
        with self._lock:
            self.strikes = 0
            self._clean += 1
            if self._clean >= CLEAN_STEP:
                self._clean = 0
                self.speed = 1 + (self.speed - 1) * EASE
