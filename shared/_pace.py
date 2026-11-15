"""How long `http.Client` waits before each request to a host: as little as the host tolerates.

The client keeps one `Host` per host, or one for all the hosts a labelled `Pace` names. A
request waits for the latest of: the host's rate headers once 5 or fewer requests are left until
their reset; each budget's sliding window; the learned gap (the floor times the learned speed,
plus up to 40% jitter); and a 429's back-off (its `Retry-After`, else 30 s doubling to 15 min).
The learned speed runs from 1x to 8x: each 429 doubles it, and every 50 clean requests bring it
20% back toward 1x. Private: callers use `http`.
"""

from __future__ import annotations

import math
import random
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
    # A name those hosts go by together: they share one learned speed, budget windows and
    # back-off, and messages and `Client.speeds` use the name. Empty keeps each host apart.
    label: str = ""
    _domains: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        normalized = tuple(domain.lower().lstrip(".") for domain in self.domains)
        object.__setattr__(self, "_domains", normalized)

    def paces(self, host: str) -> bool:
        """Whether the floor and budgets apply to `host`."""
        name = host.lower()
        return not self._domains or any(name == domain or name.endswith("." + domain)
                                        for domain in self._domains)


class Host:
    """One host's pace in a run: its learned speed, budget windows, header hold and back-off."""

    def __init__(self, name: str, pace: Pace | None, *, speed: float = 1.0):
        """Start `name`'s pace (a host's, or its hosts' label) under `pace`, or None for none."""
        self.name = name
        self.floor = pace.floor if pace is not None else 0.0
        budgets = pace.budgets.items() if pace is not None else ()
        self._budgets: tuple[tuple[str, int, float, deque[float]], ...] = tuple(
            (key, requests, per, deque[float]()) for key, (requests, per) in budgets
        )
        self.speed = min(max(speed, 1.0), SLOWEST)
        self.strikes = 0
        self._prev_strikes = 0
        self._epoch = 0
        # Whether a thread is drawing this host's countdown, so others waiting with it draw none.
        self.showing = False
        self._last: float | None = None
        # Until when a rate header or a back-off holds every request, on the monotonic clock.
        self._blocked_until = 0.0
        self._backoff_until = 0.0
        self._clean = 0
        # Every thread of a run asks the same host for turns.
        self._lock = threading.Lock()

    def turn(self, url: str) -> tuple[float, int]:
        """Reserve the next request to `url`, returning `(wait_seconds, dispatch_epoch)`."""
        with self._lock:
            if self._clean > 0:
                self._prev_strikes = 0
            now = clock.now()
            ready = self._blocked_until
            if self._last is not None and self.floor:
                gap = self.floor * self.speed * random.uniform(1, 1 + JITTER)
                ready = max(ready, self._last + gap)
            counted = [budget for budget in self._budgets if budget[0] in url]
            for _, requests, per, window in counted:
                if len(window) >= requests:
                    # The oldest of the last `requests` must leave the window, margin and all.
                    ready = max(ready, window[-requests] + per * (1 + MARGIN))
            start = max(now, ready)
            self._last = start
            for _, requests, _, window in counted:
                window.append(start)
                while len(window) > requests:
                    window.popleft()
            return start - now, self._epoch

    def held(self) -> float:
        """The seconds a rate header or a back-off still holds every request; takes no turn."""
        with self._lock:
            return max(0.0, self._blocked_until - clock.now())

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
            if self.strikes == 0 and self._prev_strikes > 0:
                self.strikes = self._prev_strikes
            self.strikes += 1
            self._epoch += 1
            self.speed = min(self.speed * 2, SLOWEST)
            self._clean = 0
            self._prev_strikes = 0
            step = min(FIRST_BACKOFF * 2 ** (self.strikes - 1), MAX_BACKOFF)
            if retry_after is not None and (not math.isfinite(retry_after) or retry_after <= 0):
                retry_after = None
            if retry_after is None:
                seconds = step * random.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)
            else:
                seconds = min(retry_after, MAX_BACKOFF)
            self._backoff_until = now + seconds
            self._blocked_until = max(self._blocked_until, self._backoff_until)
            return seconds

    def clean(self, epoch: int | None = None) -> None:
        """Count a request that went through: a 429 waits 30 s again, and 50 ease the speed."""
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                return
            self._prev_strikes = self.strikes
            self.strikes = 0
            self._clean += 1
            if self._clean >= CLEAN_STEP:
                self._clean = 0
                self.speed = 1 + (self.speed - 1) * EASE
