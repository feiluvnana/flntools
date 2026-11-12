"""How long a source waits before each request to its own site: as little as the site tolerates.

A source declares a `Pace`: a floor between two requests, and budgets for the kinds of request a
site is known to count. The session keeps one `Pacer` per source and calls `wait_turn(url)` before
each request and `record(response)` after it, and `slow_down()` when the site throttled it. The
next request then waits for the latest of three things:
1. the site's own rate-limit headers, once 5 or fewer requests remain until their reset;
2. each budget's sliding window, once it is full;
3. the learned gap: the floor times the source's learned speed, plus 0 to 40% jitter.

The learned speed is one multiplier per source, from 1x to 8x: each throttle doubles it, and every
50 clean requests or scrolls bring it 20% back toward 1x. The scroll floor (0.3 s) scales with it
too, so there is one number to learn, saved in the source's `state` between runs. Only URLs on
the source's `domains` are paced; CDN downloads go at once (`concurrency` limits them).

    pacer = Pacer(Pace(floor=1.0, budgets={"/graphql": (275, 600)}), ["instagram.com"])
    pacer.load(state)
    pacer.wait_turn("https://www.instagram.com/graphql/query"); pacer.record(response)
    pacer.save(state)
"""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# The scroll floor at 1x: two scrolls never come closer than this (session.md, "Scrolling").
SCROLL_FLOOR = 0.3
# The learned speed's bounds, how many clean requests or scrolls ease it, and by how much.
SLOWEST, CLEAN_STEP, EASE = 8.0, 50, 0.8
# Jitter adds up to this share of the gap, never takes any off: a floor is a floor.
JITTER = 0.4
# A site's rate headers count down to 0; with this many left, wait for the reset (gallery-dl's twitter rule).
HEADER_RESERVE = 5
# Budgets keep 5% of their window as a margin, as Instaloader adds seconds to Instagram's.
MARGIN = 0.05
# A wait this long (seconds) is shown, so a slow run explains itself.
SHOWN_WAIT = 15.0
# A reset larger than this is a Unix time (Twitter's), not seconds from now (the IETF draft's).
EPOCH = 1e9
# The key of the learned speed in a source's `state`.
STATE_KEY = "pace.speed"


@dataclass(frozen=True)
class Pace:
    """A source's pace: the least gap between two requests, and the budgets its site is known to count.

        Pace(floor=1.0, budgets={"/graphql": (275, 600), "/api/v1/": (75, 660)})
    """

    # The least seconds between two requests at 1x; jitter adds 0 to +40% on top.
    floor: float = 0.5
    # A URL part -> (requests, per seconds): at most that many requests whose URL contains it in any window.
    budgets: Mapping[str, tuple[int, float]] = field(default_factory=dict)

    @classmethod
    def of(cls, value: Pace | float) -> Pace:
        """`value` as a `Pace`: a number is shorthand for `Pace(floor=number)`.

            Pace.of(1.5) == Pace(floor=1.5)
        """
        return value if isinstance(value, Pace) else cls(floor=float(value))


class Pacer:
    """One source's pace during a run: who waits how long, and the speed it learned.

        pacer = Pacer(1.0, ["tiktok.com"], on_wait=lambda seconds: show(f"pacing, {seconds:.0f} s"))

    Args:
        pace: the source's `Pace`, or a number for its floor.
        domains: the source's own hosts; a URL on one of them or a subdomain is paced.
        on_wait: called with the seconds about to be waited, for a wait over 15 s. None shows nothing.
        clock: monotonic seconds; `wall`: Unix seconds, for headers that give a Unix reset time.
        sleep: waits the given seconds. All three are the real ones unless a test fakes them.

    Not thread-safe: a source makes its requests one at a time.
    """

    def __init__(self, pace: Pace | float, domains: Sequence[str], *, on_wait: Callable[[float], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] = time.sleep):
        self.pace = Pace.of(pace)
        self.domains = tuple(domain.lower().lstrip(".") for domain in domains)
        self.speed = 1.0
        self._on_wait, self._clock, self._wall, self._sleep = on_wait, clock, wall, sleep
        self._random = random.Random()
        self._last: float | None = None                     # when the last paced request went
        self._blocked_until = 0.0                           # a header's reset, on the monotonic clock
        self._windows = {key: deque[float]() for key in self.pace.budgets}
        self._clean = 0                                     # requests and scrolls since the last throttle

    @property
    def request_floor(self) -> float:
        """The least seconds between two requests now: the floor times the learned speed."""
        return self.pace.floor * self.speed

    @property
    def scroll_floor(self) -> float:
        """The least seconds between two scrolls now, for `Page.scroll(pause=)`: 0.3 s times the learned speed."""
        return SCROLL_FLOOR * self.speed

    def wait_turn(self, url: str) -> float:
        """Wait until a request to `url` may go, count it, and return the seconds waited.

            pacer.wait_turn("https://api.x.com/graphql/…/UserMedia")

        A URL off the source's `domains` returns 0 at once and isn't counted.
        """
        if not self.paces(url):
            return 0.0
        now = self._clock()
        ready = self._blocked_until
        if self._last is not None:
            ready = max(ready, self._last + self.request_floor * self._random.uniform(1, 1 + JITTER))
        for key, (requests, per) in self.pace.budgets.items():
            window = self._windows[key]
            if key in url and len(window) >= requests:
                # The oldest of the last `requests` must leave the window, with the margin, first.
                ready = max(ready, window[-requests] + per * (1 + MARGIN))
        wait = max(0.0, ready - now)
        if wait > SHOWN_WAIT and self._on_wait is not None:
            self._on_wait(wait)
        if wait:
            self._sleep(wait)
        self._last = now + wait
        for key, (requests, per) in self.pace.budgets.items():
            if key in url:
                window = self._windows[key]
                window.append(self._last)
                while len(window) > requests:
                    window.popleft()
        self.clean()
        return wait

    def record(self, response: Any) -> None:
        """Read a response's rate-limit headers: at 5 or fewer left, the next request waits for the reset plus 1 s.

            pacer.record(httpx_response)

        `response` is anything with a `headers` mapping (httpx's, Playwright's). It reads
        `x-rate-limit-remaining` / `-reset` (Twitter's) and `x-ratelimit-remaining` / `-reset`
        (the other spelling); a reset is a Unix time or seconds from now. Other responses add nothing.
        """
        headers = {str(name).lower(): value for name, value in dict(response.headers).items()}
        for prefix in ("x-rate-limit-", "x-ratelimit-"):
            try:
                remaining = int(headers[prefix + "remaining"])
                reset = float(headers[prefix + "reset"])
            # Missing, or a value that isn't a number: this response says nothing about the limit.
            except (KeyError, ValueError):
                continue
            if remaining <= HEADER_RESERVE:
                seconds = reset - self._wall() if reset > EPOCH else reset
                self._blocked_until = max(self._blocked_until, self._clock() + max(seconds, 0.0) + 1.0)
            return

    def slow_down(self) -> None:
        """Count a throttle (a 429, a captcha, a block page): the learned speed doubles, up to 8x."""
        self.speed = min(self.speed * 2, SLOWEST)
        self._clean = 0

    def clean(self) -> None:
        """Count one request or scroll without a throttle; every 50 bring the speed 20% back toward 1x."""
        self._clean += 1
        if self._clean >= CLEAN_STEP:
            self._clean = 0
            self.speed = 1 + (self.speed - 1) * EASE

    def paces(self, url: str) -> bool:
        """Whether `url` is on the source's own domains, so it waits its turn."""
        host = (urlsplit(url).hostname or "").lower()
        return any(host == domain or host.endswith("." + domain) for domain in self.domains)

    def load(self, state: Mapping[str, Any]) -> None:
        """Start from the speed an earlier run saved in the source's `state`, or 1x."""
        saved = state.get(STATE_KEY)
        if isinstance(saved, (int, float)):
            self.speed = min(max(float(saved), 1.0), SLOWEST)

    def save(self, state: MutableMapping[str, Any]) -> None:
        """Keep the learned speed in the source's `state`, so the next run starts where this one ended."""
        state[STATE_KEY] = self.speed
