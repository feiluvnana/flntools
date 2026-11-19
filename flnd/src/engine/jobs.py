"""The scheduler: jobs, the pools they wait for, and the one retry policy.

src/docs/architecture explains it.
"""

from __future__ import annotations

import asyncio
import contextvars
import heapq
import itertools
import os
import random
import shutil
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from flnd import errors
from flnd.ui.log import ui
from flnkit import clock, files, text

__all__ = ["Bucket", "Budget", "Busy", "Cap", "Hold", "Job", "Kind", "Limits", "Pool",
           "Rate", "Scheduler", "Segment", "Segments", "Speed"]

T = TypeVar("T")

# ---------------------------------------------------------------------------------------------
# The numbers of the pools
# ---------------------------------------------------------------------------------------------

# Seconds between two page requests of one bucket, when neither the config nor the class says.
PACE = 0.5
# Tabs open at once in a run, and scrolling tabs at once per account.
TABS = 6
SCROLLING = 2
# A warm tab opens only while this many tabs of the budget are free, so it never takes the
# last one an active target would wait for.
WARM_FREE = 2
# The threads of the plugins' blocking steps (`ctx.call`): one for each of the run's active
# targets (`flnd.engine.core.ACTIVE`), whatever the number of cores, beside the `CALL` threads.
LISTINGS = 16
# Places per target at least, and FILE jobs a target may queue per place.
PLACES = 32
QUEUED = 4
# Free space kept on a device beyond the files in flight, and how long a reading of it is kept.
SPARE = 64 << 20
FREE_FOR = 1.0

# ---------------------------------------------------------------------------------------------
# The numbers of the retry policy
# ---------------------------------------------------------------------------------------------

# Waits before the 4 tries after a network failure or a 5xx, each times a random 0.5 to 1.5.
NETWORK_WAITS = (1.0, 2.0, 4.0, 8.0)
JITTER = (0.5, 1.5)
# A 429's wait when the site named none, the longest one waited out, and how many 429s in a
# row on one bucket make a block round.
REFUSED_WAIT = 30.0
MAX_WAIT = 3600.0
REFUSALS = 3
# The hold of each block round; one more round fails the job. Rounds are forgotten after RESET
# seconds with no block.
ROUND_WAITS = (30.0, 120.0, 480.0)
RESET = 600.0
# A wall (a login page where a page was asked for) lifts after these waits, since a throttled
# session is walled for a while; one more landing is a login error.
WALL_WAITS = (30.0, 120.0)
# A write that finds flnd.db locked by another process goes back in the queue after BUSY_PAUSE
# seconds, for up to BUSY_FOR: the loop never waits, and a lock held that long is stuck.
BUSY_PAUSE, BUSY_FOR = 0.02, 30.0
# Statuses that say a host is overloaded, so its cap halves (with a reset and a timeout).
OVERLOADED = (429, 503)

# ---------------------------------------------------------------------------------------------
# The numbers of the AIMD cap and the pace (today's `flnkit.net`, the same values)
# ---------------------------------------------------------------------------------------------

# After a 429, a bucket's rate stays halved for this many clean requests.
CLEAN = 20
# One token at most: a pace is a floor between two requests, never a burst after a lull.
BURST = 1.0
# Where a host's cap starts, and the most it grows to without a limit of its own.
CAP = 8
MOST = 32
# A cap measures its host's throughput, and may grow by one, every STEP clean transfers.
STEP = 10
# A step must raise the throughput by this share of what one more place adds, or it is undone.
RISE = 0.5
# After a step that was undone, clean transfers before the cap probes again.
HOLD = 100
# A session's handles leave room for this many requests to the pages, and for SPREAD hosts at
# their most at once (`Limits.handles`).
PAGES = 4
SPREAD = 8
# How often, in seconds, a job held by a suspended run looks again: a person's key, waited out
# in real time even under a test's fake clock.
SUSPENDED_POLL = 0.1

# ---------------------------------------------------------------------------------------------
# The numbers of segments, `slowest` and `rate`
# ---------------------------------------------------------------------------------------------

# The smallest file that may be split, the smallest range left that is split in half, and the
# most segments of one file, and of a file a plugin's `fetch` serves by ranges.
SPLIT_FILE = 16 << 20
SPLIT_RANGE = 8 << 20
SEGMENTS = 8
FETCH_SEGMENTS = 4
# A transfer may first split this long after it started, then every TICK seconds.
SPLIT_AFTER = 2.0
TICK = 1.0
# The window over which `slowest` averages a transfer, in seconds.
SLOW_WINDOW = 30.0



class Kind(Enum):
    """What a job is, which says the pools it waits for."""

    PAGE = "page"
    FILE = "file"
    TAB = "tab"
    CALL = "call"
    HASH = "hash"
    UNPACK = "unpack"
    MERGE = "merge"
    EXEC = "exec"
    WRITE = "write"


# Kinds with threads of their own, which their work reaches through `Scheduler.on`.
THREADED = frozenset({Kind.CALL, Kind.HASH, Kind.UNPACK, Kind.EXEC, Kind.WRITE})
# The kinds an account's hold (a wall, a block of the account) keeps waiting; a bucket's hold
# keeps only that bucket's pages waiting.
ACCOUNT_HELD = frozenset({Kind.PAGE, Kind.TAB, Kind.CALL})
# Lower runs first: 0 is a retried failure.
PRIORITY = {Kind.PAGE: 1, Kind.TAB: 1, Kind.FILE: 2, Kind.CALL: 2, Kind.WRITE: 2,
            Kind.HASH: 3, Kind.UNPACK: 3, Kind.MERGE: 3, Kind.EXEC: 3}


@dataclass(eq=False, kw_only=True)
class Job(Generic[T]):
    """One piece of work and what it needs: `Scheduler.run` waits, runs it, and retries it.

    Attributes:
        kind: which pools it waits for.
        work: the work, a coroutine function for every kind. Work that blocks moves itself
            to its kind's threads with `Scheduler.on`.
        plugin: the plugin's code.
        account: the account it runs as; None for none.
        bucket: the plugin's bucket for a page; "" is the default bucket, the host.
        host: the host it talks to.
        target: the target it is for, which owns a `FILE` job's place.
        priority: lower runs first; None takes its kind's (`PRIORITY`).
        size: the bytes, when known.
        path: the file it writes: its folder's device picks the writer and the free space.
        scroll: a `TAB` job that scrolls a feed, under its account's scroll gate.
        first: a listing job for its target's first page.
        done: the bytes a `FILE` job has moved, which its work keeps up to date.
    """

    kind: Kind
    work: Callable[[], Awaitable[T]]
    plugin: str = ""
    account: str | None = None
    bucket: str = ""
    host: str = ""
    target: str | None = None
    priority: int | None = None
    size: int | None = None
    path: Path | None = None
    scroll: bool = False
    first: bool = False
    done: int = 0

    def __post_init__(self) -> None:
        if self.priority is None:
            self.priority = PRIORITY[self.kind]
        if not self.bucket:
            self.bucket = self.host


# ---------------------------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------------------------


class Pool:
    """Places to run at once, handed out by priority, then first come, first served.

    Used from the loop's thread only.

    Args:
        size: places at once; at least 1.
    """

    def __init__(self, size: int) -> None:
        self.size = max(1, size)
        self.busy = 0
        self._queue: list[tuple[int, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    @property
    def free(self) -> int:
        """Places nobody holds."""
        return max(0, self.size - self.busy)

    @property
    def waiting(self) -> int:
        """Jobs waiting for a place."""
        return len(self._queue)

    async def take(self, priority: int) -> None:
        """Wait for a place, and take it."""
        if self.busy < self.size and not self._queue:
            self.busy += 1
            return
        place: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (priority, next(self._order), place)
        heapq.heappush(self._queue, entry)
        try:
            await place
        except asyncio.CancelledError:
            if place.done() and not place.cancelled():
                # Handed over just as the waiter was cancelled: pass the place on.
                self.give()
            else:
                self._queue.remove(entry)
                heapq.heapify(self._queue)
            raise

    def give(self) -> None:
        """Give a place back, to the first job waiting."""
        self.busy -= 1
        while self._queue and self.busy < self.size:
            place = heapq.heappop(self._queue)[2]
            if not place.done():
                self.busy += 1
                place.set_result(None)

    @asynccontextmanager
    async def place(self, priority: int) -> AsyncIterator[None]:
        """Hold a place for the block."""
        await self.take(priority)
        try:
            yield
        finally:
            self.give()


class Bucket:
    """A token bucket: `rate` requests a second, one token deep, held by a site's wait.

    It refills by the time elapsed, so a slow caller never waits again. A `hold` gives no
    token before its end, and a `slow` halves the rate for `CLEAN` clean requests.

    Args:
        rate: requests a second.
    """

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, not {rate}")
        self.rate = rate
        self._tokens = BURST
        self._at: float | None = None
        self._until = 0.0
        self._slow = 0

    async def take(self, stop: threading.Event | None = None) -> None:
        """Wait for a token, and take it; callers waiting together queue one interval apart.

        Raises:
            Stopped: `stop` was set while waiting.
        """
        rate = self.rate / 2 if self._slow else self.rate
        now = clock.now()
        at = now if self._at is None else self._at
        self._tokens = min(BURST, self._tokens + (now - at) * rate) - 1.0
        self._at = now
        wait = max(-self._tokens / rate, self._until - now)
        if wait > 0 and not await clock.pause(wait, stop=stop):
            raise errors.Stopped("stopped while pacing")

    def hold(self, seconds: float) -> None:
        """Give no token for `seconds` from now: the site said when to come back."""
        self._until = max(self._until, clock.now() + seconds)

    def slow(self) -> None:
        """Halve the rate until `CLEAN` clean requests have passed: the site refused one."""
        self._slow = CLEAN

    def clean(self) -> None:
        """Count one request that went through."""
        if self._slow:
            self._slow -= 1


class Cap:
    """Transfers at once to one host, adapted to what the host can take (AIMD).

    src/docs/performance explains its steps. `take` returns the round a transfer started in,
    and only a failure of the current round halves the cap.

    Args:
        start: transfers at once to begin with; more than `most` starts at `most`.
        most: the most transfers at once it grows to.
    """

    def __init__(self, start: int, *, most: int) -> None:
        if start < 1 or most < 1:
            raise ValueError(f"a cap needs a start and a most of 1 or more, not {start}, {most}")
        self.most = most
        self.value = min(start, most)
        self.busy = 0
        self._waiting: deque[asyncio.Future[None]] = deque()
        self._round = 0
        # The open window: its clean transfers, their bytes and seconds, and whether a place
        # stood free in it.
        self._clean = 0
        self._bytes = 0
        self._seconds = 0.0
        self._slack = False
        # The throughput at `value` from the last window that counted, in bytes a second.
        self._rate: float | None = None
        # Whether the last window grew the cap, so the next one judges that step.
        self._grown = False
        # Clean transfers left before a cap whose step was undone probes again.
        self._held = 0

    @property
    def waiting(self) -> int:
        """Transfers waiting for a place."""
        return len(self._waiting)

    async def take(self) -> int:
        """Wait for a place, and take it; returns the round, for `failed`."""
        if self.busy < self.value and not self._waiting:
            self.busy += 1
            return self._round
        place: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiting.append(place)
        try:
            await place
        except asyncio.CancelledError:
            if place.done() and not place.cancelled():
                self.give()
            else:
                self._waiting.remove(place)
            raise
        return self._round

    def give(self) -> None:
        """Give a place back, to the first transfer waiting when the cap allows."""
        self.busy -= 1
        if not self._waiting:
            self._slack = True
        self._wake()

    def done(self, size: int, seconds: float) -> None:
        """Count a clean transfer of `size` bytes that held its place `seconds`."""
        self._clean += 1
        self._bytes += size
        self._seconds += seconds
        if self._held:
            self._held -= 1
        if self._clean >= STEP:
            self._judge()

    def failed(self, round: int) -> None:
        """A transfer of `round` met an overloaded host: halve, once a round."""
        if round != self._round:
            return
        self._round += 1
        self.value = max(1, self.value // 2)
        self._rate, self._grown, self._held = None, False, 0
        self._open()

    def _judge(self) -> None:
        """Close the window: drop it, undo its step, or grow."""
        rate = self.value * self._bytes / self._seconds if self._seconds > 0 else None
        grown, self._grown = self._grown, False
        slack = self._slack
        self._open()
        if slack or rate is None:
            return
        if grown and self._rate is not None and \
                rate <= self._rate * (1 + RISE / (self.value - 1)):
            self.value -= 1
            self._held = HOLD
            return
        self._rate = rate
        if self._held or self.value >= self.most:
            return
        self.value += 1
        self._grown = True
        self._wake()

    def _open(self) -> None:
        self._clean = self._bytes = 0
        self._seconds = 0.0
        self._slack = False

    def _wake(self) -> None:
        """Hand free places to the transfers waiting, first come first served."""
        while self._waiting and self.busy < self.value:
            place = self._waiting.popleft()
            if not place.done():
                self.busy += 1
                place.set_result(None)


class Limits:
    """Each host's `Cap`, made on first use: from `CAP`, up to `MOST` or the host's own limit.

    A page's pace is the scheduler's `Bucket` per (plugin, account, bucket) instead.

    Args:
        limits: the most transfers at once to a host and its subdomains, by host name.
    """

    def __init__(self, limits: Mapping[str, int] | None = None) -> None:
        self.limits = dict(limits or {})
        self._caps: dict[str, Cap] = {}

    @property
    def places(self) -> int:
        """`FILE` places per target: `PLACES`, or the largest limit when that is more."""
        return max(PLACES, max(self.limits.values(), default=0))

    @property
    def handles(self) -> int:
        """Transfers at once in all, for a session's handles: `SPREAD` hosts at `MOST`, every
        host with a limit at its own, and room for the pages."""
        return PAGES + SPREAD * MOST + sum(self.limits.values())

    @property
    def load(self) -> Mapping[str, tuple[int, int, int]]:
        """Each host's cap, transfers in flight and transfers waiting, as a snapshot."""
        return MappingProxyType({host: (cap.value, cap.busy, cap.waiting)
                                 for host, cap in self._caps.items()})

    def cap(self, host: str) -> Cap:
        """The cap of `host`, lower case."""
        cap = self._caps.get(host)
        if cap is None:
            cap = self._caps[host] = Cap(CAP, most=self._most(host))
        return cap

    def _most(self, host: str) -> int:
        """The most transfers at once to `host`: its own limit, a parent domain's, or `MOST`."""
        for name, value in self.limits.items():
            if host == name or host.endswith(f".{name}"):
                return value
        return MOST


class Rate:
    """One byte bucket for every `FILE` job of a run: `rate` bytes a second, 1 s deep.

    Args:
        rate: bytes a second; 0 limits nothing.
    """

    def __init__(self, rate: float) -> None:
        self.rate = max(0.0, rate)
        self._bytes = self.rate
        self._at: float | None = None

    async def take(self, size: int) -> None:
        """Wait until `size` more bytes may move; 0 waits only for the bytes owed."""
        if not self.rate:
            return
        now = clock.now()
        at = now if self._at is None else self._at
        self._bytes = min(self.rate, self._bytes + (now - at) * self.rate) - size
        self._at = now
        if self._bytes < 0:
            await clock.pause(-self._bytes / self.rate)


class Speed:
    """A transfer's bytes over time, for `slowest`: the average over the last `SLOW_WINDOW` s."""

    def __init__(self) -> None:
        self.began = clock.now()
        self._total = 0
        self._marks: deque[tuple[float, int]] = deque([(self.began, 0)])

    def add(self, size: int) -> None:
        """`size` more bytes arrived."""
        self._total += size
        now = clock.now()
        self._marks.append((now, self._total))
        while len(self._marks) > 2 and self._marks[1][0] <= now - SLOW_WINDOW:
            self._marks.popleft()

    def below(self, slowest: float) -> bool:
        """Whether a transfer at least `SLOW_WINDOW` s old averaged under `slowest` bytes a
        second over its last `SLOW_WINDOW` s; 0 is never below."""
        now = clock.now()
        if not slowest or now - self.began < SLOW_WINDOW:
            return False
        start = now - SLOW_WINDOW
        moved = self._total - next((total for at, total in reversed(self._marks)
                                    if at <= start), 0)
        return moved / SLOW_WINDOW < slowest


class Busy(Exception):
    """Another process holds flnd.db's write lock (`SQLITE_BUSY`): the write goes back in the
    queue (`BUSY_PAUSE`)."""


class Budget:
    """One target's share of the run's tabs, as `flnkit.browser.Tabs` takes it.

    Each tab the target holds takes a place of the run's `TABS`; a warm tab opens only while
    `WARM_FREE` of them are free; and a `human` scroll step waits for its account's scroll
    gate (`SCROLLING` at once). A tab waiting for the gate holds no other place, and the gate's
    holders wait for nothing, so the two can't deadlock.

    Args:
        scheduler: the run's.
        plugin: the target's plugin.
        account: the account it lists as.
    """

    def __init__(self, scheduler: Scheduler, plugin: str, account: str | None) -> None:
        self._scheduler = scheduler
        self._gate = scheduler._scrolls.setdefault((plugin, account), Pool(SCROLLING))

    @property
    def stopped(self) -> BaseException | None:
        """What stopped the run (a full disk, a missing tool); None while nothing did."""
        return self._stop

    @property
    def warm(self) -> bool:
        """Whether a warm tab may open now."""
        return self._scheduler.warm

    async def take(self) -> None:
        """Wait for a place for one more tab, and take it."""
        await self._scheduler.tabs.take(PRIORITY[Kind.TAB])

    def give(self) -> None:
        """Give back a tab's place."""
        self._scheduler.tabs.give()

    async def turn(self, step: Callable[[], Awaitable[T]]) -> T:
        """Run one scroll step once the account's scroll gate lets it."""
        async with self._gate.place(PRIORITY[Kind.TAB]):
            return await step()


@dataclass
class Hold:
    """A hold of (plugin, account, bucket), or of the whole account when `bucket` is None.

    Attributes:
        until: when it ends, on the run's clock.
        reason: why, for the screen.
        rounds: block rounds since the last `RESET` s without one.
        last: when the last block round began.
        refused: 429s in a row on the bucket.
    """

    plugin: str
    account: str | None
    bucket: str | None
    until: float = 0.0
    reason: str = ""
    rounds: int = 0
    last: float = float("-inf")
    refused: int = 0


@dataclass
class _Tries:
    """One job's tries so far, across its retries."""

    network: int = 0
    walls: int = 0
    # When the first try met another process's lock on flnd.db; None while none did.
    busy: float | None = None


# ---------------------------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------------------------


async def _nothing() -> None:
    """The work of a job made only to wait for what it would wait for."""


def _no_switch(job: Job[Any], error: BaseException) -> Job[Any] | None:
    """No other account: a run's scheduler takes the auth manager's (`auth.Switcher`)."""
    return None


class Scheduler:
    """Every job of a run: its pools in order, its work under the retry policy, then release.

    A context manager: its threads end with the block. Used from the run's loop only; its
    waits sleep on the run's clock (`flnkit.clock`).

    Args:
        pace: seconds between two page requests of one bucket, by plugin; `PACE` otherwise.
        limits: the most transfers at once to a host, by host name.
        rate: bytes a second for every `FILE` job together; 0 is no limit.
        slowest: bytes a second a transfer may average over 30 s; 0 is off.
        switch: given a job and what it raised, the same job moved to another free account,
            or None when there is none (the default).
        stop: the run's stop (`flnd.engine.run.Stop`): its first Ctrl-C ends the waits of a
            listing's jobs (`PAGE`, `TAB`, `CALL`), its second every other's, and while it is
            `suspended` no job starts; None never stops.
    """

    def __init__(self, *, pace: Mapping[str, float] | None = None,
                 limits: Mapping[str, int] | None = None, rate: float = 0.0,
                 slowest: float = 0.0,
                 switch: Callable[[Job[Any], BaseException], Job[Any] | None] = _no_switch,
                 stop: Any = None) -> None:
        cpus = os.cpu_count() or 1
        self.stop = stop
        self.pace = dict(pace or {})
        self.limits = Limits(limits)
        self.rate = Rate(rate)
        self.slowest = slowest
        self.switch = switch
        # Accounts a login error ended, for the auth manager to mark expired.
        self.expired: set[tuple[str, str | None]] = set()
        self.tabs = Pool(TABS)
        sizes = {Kind.CALL: min(32, cpus + 4), Kind.HASH: cpus, Kind.UNPACK: 1,
                 Kind.MERGE: max(1, cpus // 2), Kind.EXEC: 1}
        self.pools = {kind: Pool(size) for kind, size in sizes.items()}
        self._threads: dict[Kind, ThreadPoolExecutor] = {}
        self._listings: ThreadPoolExecutor | None = None
        self._writers: dict[int, ThreadPoolExecutor] = {}
        self._scrolls: dict[tuple[str, str | None], Pool] = {}
        self._targets: dict[str | None, tuple[Pool, Pool]] = {}
        self._buckets: dict[tuple[str, str | None, str], Bucket] = {}
        self._holds: dict[tuple[str, str | None, str | None], Hold] = {}
        self._devices: dict[Path, int] = {}
        self._free: dict[int, tuple[float, int]] = {}
        self._flight: dict[int, set[Job[Any]]] = {}
        # What stopped the run (a full disk, a missing tool, a broken plugin): every job raises it.
        self._stop: BaseException | None = None

    def __enter__(self) -> Scheduler:
        return self

    def __exit__(self, *exc: object) -> None:
        # The block may end on the loop, which must not wait for a thread; `close` does.
        for pool in self._pools():
            pool.shutdown(wait=False)

    async def close(self) -> None:
        """End the threads, once the work on them is done, without blocking the loop."""
        loop = asyncio.get_running_loop()
        for pool in self._pools():
            await loop.run_in_executor(None, pool.shutdown)

    def _pools(self) -> list[ThreadPoolExecutor]:
        return [*self._threads.values(), *self._writers.values(),
                *([self._listings] if self._listings is not None else [])]

    async def step(self, plugin: str, account: str | None, work: Callable[..., T],
                   *args: Any) -> T:
        """Run one step of a plugin's plain listing, `work(*args)`, on a listing thread.

        It waits as a `CALL` job would, for its account's holds and a suspension, and raises
        what stopped the run, but takes no `CALL` place: the `LISTINGS` threads are its own,
        so a lazy post's files or the engine's own steps never queue behind listings. It is
        never tried again.
        """
        job: Job[None] = Job(kind=Kind.CALL, work=_nothing, plugin=plugin, account=account)
        await self._held(job, bucket=False)
        await self._work(job)
        if self._listings is None:
            self._listings = ThreadPoolExecutor(LISTINGS, thread_name_prefix="flnd-listing")
        try:
            return await self._thread(self._listings, work, *args)
        except Exception as error:
            # A full disk met at a listing's step stops the run, as it would in a job.
            if stops(error):
                self._stop = self._stop or error
            raise

    @property
    def holds(self) -> tuple[Hold, ...]:
        """The holds in force now, oldest first."""
        now = clock.now()
        return tuple(hold for hold in self._holds.values() if hold.until > now)

    @property
    def stopped(self) -> BaseException | None:
        """What stopped the run (a full disk, a missing tool); None while nothing did."""
        return self._stop

    @property
    def warm(self) -> bool:
        """Whether a warm tab may open now: `WARM_FREE` tabs of the budget are free."""
        return self.tabs.free >= WARM_FREE and not self.tabs.waiting

    def hold(self, plugin: str, account: str | None, *, seconds: float, reason: str,
             bucket: str | None = None) -> float:
        """Hold an account, or one of its buckets, as a block round: the plugin's `ctx.hold`.

        The hold lasts the larger of `seconds` and the round's wait (30, 120, 480 s), and
        rounds are forgotten after `RESET` s without one, as for a `Blocked`.

        Args:
            seconds: what the site asked for; 0 when it said nothing.
            reason: why, for the screen.
            bucket: the bucket held; None holds the whole account.

        Returns:
            The seconds the hold lasts from now.

        Raises:
            Blocked: a fourth round; it fails its job without another try.
        """
        hold = self._counted((plugin, account, bucket))
        if not self._rounded(hold, seconds, reason):
            raise errors.Blocked(f"{reason}: still blocked after {len(ROUND_WAITS)} holds",
                                 wait=seconds or None, retryable=False)
        return hold.until - clock.now()

    async def wait(self, plugin: str, account: str | None, *,
                   bucket: str | None = None) -> None:
        """Wait out the hold of an account, or of one of its buckets as well, as its `PAGE`
        jobs do.

        Raises:
            Stopped: the run's first Ctrl-C came meanwhile.
        """
        await self._held(Job(kind=Kind.PAGE, work=_nothing, plugin=plugin, account=account,
                             bucket=bucket or ""), bucket=bucket is not None)

    def room(self, host: str) -> bool:
        """Whether `host`'s cap has a free place and no `FILE` job waits for it."""
        cap = self.limits.cap(host)
        return cap.busy < cap.value and not cap.waiting

    async def run(self, job: Job[T]) -> T:
        """Wait for the job's pools in order, run its work under the retry policy, release.

        Raises:
            Error: the policy gave up on it; the one that stopped the run, for every job after.
        """
        if job.kind is Kind.EXEC:
            # A retry stays in the lane, so the programs still run in the order of the saves.
            async with self.pools[Kind.EXEC].place(_priority(job)):
                return await self._tries(job)
        return await self._tries(job)

    async def _tries(self, job: Job[T]) -> T:
        tries = _Tries()
        while True:
            if self._stop is not None:
                raise self._stop
            account = job.account
            try:
                result = await self._once(job)
            except Exception as error:
                again = await self._again(job, error, tries)
                if again is not job or again.account != account:
                    # Moved to another account, which has its own walls and tries.
                    tries = _Tries()
                job = again
                continue
            hold = self._holds.get(self._key(job))
            if hold is not None:
                hold.refused = 0
            return result

    async def _work(self, job: Job[T]) -> T:
        """Run the work, once every pool is held, unless the run stopped while it waited.

        Raises:
            Stopped: the run's stop came while the run was suspended.
        """
        suspended = getattr(self.stop, "suspended", None)
        while suspended is not None and suspended.is_set():
            halt = self._halt(job)
            if halt is not None and halt.is_set():
                raise errors.Stopped("stopped while suspended")
            await asyncio.sleep(SUSPENDED_POLL)
        if self._stop is not None:
            raise self._stop
        return await job.work()

    def _halt(self, job: Job[Any]) -> threading.Event | None:
        """What ends `job`'s waits: the first Ctrl-C a listing's, the second any other's."""
        if self.stop is None:
            return None
        return self.stop.soon if job.kind in ACCOUNT_HELD else self.stop.now

    async def _pause(self, job: Job[Any], seconds: float) -> None:
        """Wait `seconds` on the run's clock for `job`.

        Raises:
            Stopped: the run was stopped meanwhile.
        """
        if not await clock.pause(seconds, stop=self._halt(job)):
            raise errors.Stopped("stopped while waiting")

    # The pools of each kind, in order -------------------------------------------------------

    async def _once(self, job: Job[T]) -> T:
        priority = _priority(job)
        if job.kind is Kind.PAGE:
            return await self._page(job)
        if job.kind is Kind.FILE:
            return await self._file(job, priority)
        if job.kind in ACCOUNT_HELD:
            await self._held(job, bucket=False)
        if job.kind is Kind.TAB:
            if not job.scroll:
                async with self.tabs.place(priority):
                    return await self._work(job)
            # The scroll gate first, so a tab waiting for its turn to scroll holds no tab.
            gate = self._scrolls.setdefault((job.plugin, job.account), Pool(SCROLLING))
            async with gate.place(priority), self.tabs.place(priority):
                return await self._work(job)
        if job.kind in (Kind.WRITE, Kind.EXEC):
            # A write's device thread orders the writes; an exec job holds its lane in `run`.
            return await self._work(job)
        async with self.pools[job.kind].place(priority):
            return await self._work(job)

    async def on(self, kind: Kind, work: Callable[..., T], *args: Any,
                 path: Path | None = None) -> T:
        """Run blocking `work(*args)` on a thread of `kind`, from inside a job of that kind.

        The kind's pool has already let the job start, so a `HASH` job gets one of the
        hasher's `os.cpu_count()` threads at once. A `WRITE` goes to the one thread of the
        device `path` is on.

        Raises:
            ValueError: `kind` has no threads.
        """
        if kind not in THREADED:
            raise ValueError(f"{kind.value} jobs have no threads")
        if kind is Kind.WRITE:
            pool = self._writer(path)
        else:
            pool = self._threads.get(kind)
            if pool is None:
                pool = self._threads[kind] = ThreadPoolExecutor(
                    self.pools[kind].size, thread_name_prefix=f"flnd-{kind.value}")
        return await self._thread(pool, work, *args)

    async def _page(self, job: Job[T]) -> T:
        await self._held(job)
        key = (job.plugin, job.account, job.bucket)
        bucket = self._buckets.get(key)
        pace = self.pace.get(job.plugin, PACE)
        if bucket is None and pace > 0:
            bucket = self._buckets[key] = Bucket(1 / pace)
        if bucket is None:
            # A pace of 0 paces nothing.
            return await self._work(job)
        await bucket.take(self._halt(job))
        result = await self._work(job)
        bucket.clean()
        return result

    async def _file(self, job: Job[T], priority: int) -> T:
        queue, places = self._target(job.target)
        async with queue.place(priority), places.place(priority):
            cap = self.limits.cap(job.host)
            round = await cap.take()
            try:
                await self.rate.take(0)
                began = clock.now()
                with self._flying(job):
                    try:
                        result = await self._work(job)
                    except Exception as error:
                        if _overloaded(error):
                            cap.failed(round)
                        raise
                cap.done(job.done or job.size or 0, clock.now() - began)
                return result
            finally:
                cap.give()

    def _target(self, target: str | None) -> tuple[Pool, Pool]:
        """A target's queue (`QUEUED` jobs a place, running or waiting) and its places."""
        pools = self._targets.get(target)
        if pools is None:
            places = self.limits.places
            pools = self._targets[target] = (Pool(places * QUEUED), Pool(places))
        return pools

    async def _thread(self, pool: ThreadPoolExecutor, work: Callable[..., T], *args: Any) -> T:
        """Run `work` on `pool` with the run's clock; a cancel still waits for it to end, since
        a write still in flight would land in whatever file reused its descriptor."""
        done = asyncio.get_running_loop().run_in_executor(
            pool, contextvars.copy_context().run, work, *args)
        try:
            return await asyncio.shield(done)
        except asyncio.CancelledError:
            while not done.done():
                try:
                    await asyncio.shield(done)
                except asyncio.CancelledError:
                    continue
            raise

    def _writer(self, path: Path | None) -> ThreadPoolExecutor:
        """The one writer thread of the device `path` is on."""
        device = self._device(path)
        writer = self._writers.get(device)
        if writer is None:
            writer = self._writers[device] = ThreadPoolExecutor(
                1, thread_name_prefix=f"flnd-writer-{device}")
        return writer

    def _device(self, path: Path | None) -> int:
        """The device of `path`'s folder, from one `stat` per folder; a folder not made yet is
        on its nearest parent's device."""
        folder = Path(path).parent if path is not None else Path(".")
        device = self._devices.get(folder)
        if device is None:
            device = self._devices[folder] = _made(folder)[1].st_dev
        return device

    # The free-space check --------------------------------------------------------------------

    @contextmanager
    def _flying(self, job: Job[Any]) -> Iterator[None]:
        """Count a `FILE` job of known size in flight on its device, once its free space holds.

        Raises:
            DiskError: the device lacks the file's size, the bytes still due from the files in
                flight on it, and `SPARE`.
        """
        if job.size is None or job.path is None:
            yield
            return
        device = self._device(job.path)
        flight = self._flight.setdefault(device, set())
        due = sum(max(0, (other.size or 0) - other.done) for other in flight)
        needed = job.size + due + SPARE
        free = self._free_on(device, job.path)
        if free < needed:
            raise errors.DiskError(f"not enough free space for {job.path}: "
                                   f"{text.size(needed)} needed, {text.size(free)} free",
                                   path=job.path)
        flight.add(job)
        try:
            yield
        finally:
            flight.discard(job)

    def _free_on(self, device: int, path: Path) -> int:
        """Free bytes on `device`, read from `path`'s folder at most once each `FREE_FOR` s."""
        at, free = self._free.get(device, (float("-inf"), 0))
        if clock.now() - at >= FREE_FOR:
            folder = _made(Path(path).parent)[0]
            try:
                free = shutil.disk_usage(folder).free
            except OSError as error:
                raise errors.disk(error, folder) from error
            self._free[device] = (clock.now(), free)
        return free

    # The retry policy ------------------------------------------------------------------------

    async def _again(self, job: Job[T], error: Exception, tries: _Tries) -> Job[T]:
        """The job to run next after `error`, once its wait is over; raises when there is none."""
        if isinstance(error, Busy):
            return await self._busy(job, error, tries)
        if stops(error):
            self._stop = error
            raise error
        if isinstance(error, errors.LoginError):
            return await self._login(job, error, tries)
        if isinstance(error, errors.Blocked) and not error.retryable:
            raise error
        if isinstance(error, errors.Blocked):
            if error.status == 429:
                return await self._refused(job, error)
            return await self._round(job, error, error.wait or 0.0)
        if isinstance(error, errors.Hidden):
            return self._move(job, error)
        if isinstance(error, errors.NotFound) and job.first and job.kind in (Kind.PAGE, Kind.TAB):
            return self._move(job, error)
        if isinstance(error, errors.Changed):
            raise error
        if _again_later(error) and tries.network < len(NETWORK_WAITS):
            wait = NETWORK_WAITS[tries.network] * random.uniform(*JITTER)
            tries.network += 1
            await self._pause(job, wait)
            return _retried(job)
        raise error

    async def _busy(self, job: Job[T], error: Busy, tries: _Tries) -> Job[T]:
        """Another process holds flnd.db's write lock: queue the job again, at its priority,
        after `BUSY_PAUSE` s, until `BUSY_FOR` s have gone by; then the run stops.

        The other process lets go in real time, so these waits are real ones even under a
        test's fake clock, as a suspension's are.

        Raises:
            DiskError: the lock was held `BUSY_FOR` s.
            Stopped: the second Ctrl-C came meanwhile.
        """
        if tries.busy is None:
            tries.busy = time.monotonic()
        if time.monotonic() - tries.busy >= BUSY_FOR:
            stop = errors.DiskError(f"{error}, for {BUSY_FOR:.0f}s",
                                    hint="let the other run finish")
            self._stop = stop
            raise stop from error
        halt = self._halt(job)
        if halt is not None and halt.is_set():
            raise errors.Stopped("stopped while flnd.db was locked") from error
        await asyncio.sleep(BUSY_PAUSE)
        return job

    async def _login(self, job: Job[T], error: errors.LoginError, tries: _Tries) -> Job[T]:
        """A wall (a retryable `LoginError`) holds the account 30 s, then 120 s; after that, or
        on a 401 or 403, the job moves to another account, or fails and the account expires."""
        if error.retryable and tries.walls < len(WALL_WAITS):
            hold = self._hold((job.plugin, job.account, None))
            self._hold_for(hold, WALL_WAITS[tries.walls], str(error))
            tries.walls += 1
            await self._held(job)
            return _retried(job)
        moved = self.switch(job, error)
        if moved is not None:
            return _retried(moved)
        self.expired.add((job.plugin, job.account))
        raise error

    async def _refused(self, job: Job[T], error: errors.Blocked) -> Job[T]:
        """A 429: hold the bucket for the site's wait and halve its pace; the third in a row
        is a block round."""
        key = self._key(job)
        hold = self._hold(key)
        hold.refused += 1
        bucket = self._buckets.get(key)
        if bucket is not None:
            bucket.slow()
        wait = REFUSED_WAIT if error.wait is None else min(error.wait, MAX_WAIT)
        if hold.refused >= REFUSALS:
            hold.refused = 0
            return await self._round(job, error, wait)
        ui.warn("{host}: rate limited; waiting {time}", host=job.host or job.plugin,
                time=text.duration(round(wait)))
        self._hold_for(hold, wait, str(error))
        await self._held(job)
        return _retried(job)

    async def _round(self, job: Job[T], error: errors.Blocked, seconds: float) -> Job[T]:
        """A block round: move the job, or hold its bucket for the round's wait; the fourth
        round fails it."""
        hold = self._counted(self._key(job))
        moved = self.switch(job, error)
        if moved is not None:
            return _retried(moved)
        if not self._rounded(hold, seconds, str(error)):
            raise error
        await self._held(job)
        return _retried(job)

    def _counted(self, key: tuple[str, str | None, str | None]) -> Hold:
        """The hold of `key`, with one more block round counted."""
        hold = self._hold(key)
        now = clock.now()
        if now - hold.last >= RESET:
            hold.rounds = 0
        hold.rounds += 1
        hold.last = now
        return hold

    def _rounded(self, hold: Hold, seconds: float, reason: str) -> bool:
        """Hold for the round's wait, or the site's when larger; False past the last round."""
        if hold.rounds > len(ROUND_WAITS):
            return False
        self._hold_for(hold, max(seconds, ROUND_WAITS[hold.rounds - 1]), reason)
        return True

    def _move(self, job: Job[T], error: Exception) -> Job[T]:
        moved = self.switch(job, error)
        if moved is None:
            raise error
        return _retried(moved)

    # Holds -----------------------------------------------------------------------------------

    @staticmethod
    def _key(job: Job[Any]) -> tuple[str, str | None, str]:
        return (job.plugin, job.account, job.bucket)

    def _hold(self, key: tuple[str, str | None, str | None]) -> Hold:
        hold = self._holds.get(key)
        if hold is None:
            hold = self._holds[key] = Hold(*key)
        return hold

    @staticmethod
    def _hold_for(hold: Hold, seconds: float, reason: str) -> None:
        hold.until = max(hold.until, clock.now() + seconds)
        hold.reason = reason

    async def _held(self, job: Job[Any], *, bucket: bool = True) -> None:
        """Wait out the hold of the job's whole account, and of its bucket unless told not to."""
        keys = [(job.plugin, job.account, None)]
        if bucket:
            keys.append(self._key(job))
        while True:
            until = max((self._holds[key].until for key in keys if key in self._holds),
                        default=0.0)
            left = until - clock.now()
            if left <= 0:
                return
            await self._pause(job, left)


def _retried(job: Job[T]) -> Job[T]:
    """A job tried again goes first; an exec job keeps its place in the order of the saves."""
    if job.kind is not Kind.EXEC:
        job.priority = 0
    return job


def _priority(job: Job[Any]) -> int:
    return job.priority if job.priority is not None else PRIORITY[job.kind]


def _made(folder: Path) -> tuple[Path, os.stat_result]:
    """`folder`, or its nearest parent that exists (a file's folder is made with its part),
    with its `stat`.

    Raises:
        DiskError: none of them can be read.
    """
    for one in (folder, *folder.parents):
        try:
            return one, os.stat(one)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise errors.disk(error, one) from error
    raise errors.DiskError(f"{folder}: no folder of it exists", path=folder)


def stops(error: BaseException) -> bool:
    """Whether `error` stops the run, for every job after it: a full or refused disk (but not
    a part another run holds, which skips only its file), a missing tool (even when its error
    says another try may work), or a broken plugin class."""
    disk = isinstance(error, errors.DiskError) and not isinstance(error, files.BusyError)
    return disk or _broken(error) or _missing(error)


def _again_later(error: Exception) -> bool:
    """Whether another try may mend a failure: any retryable error of any kind (a reset, a
    5xx, a tool that failed once), or a 5xx."""
    if isinstance(error, errors.Error) and error.retryable:
        return True
    return isinstance(error, errors.SiteError) and (error.status or 0) >= 500


def _broken(error: Exception) -> bool:
    """A plugin's class that is broken (the loader sets `broken` on the error), which stops the
    run; anything a plugin raises while it runs fails only its own job."""
    return isinstance(error, errors.PluginError) and getattr(error, "broken", False)


def _overloaded(error: Exception) -> bool:
    """A 429, a 503, a reset, a timeout or a short body: the host's cap halves."""
    return getattr(error, "status", None) in OVERLOADED or (
        isinstance(error, errors.NetworkError) and error.retryable)


def _missing(error: Exception) -> bool:
    """A tool that isn't there (a run of it that failed is `RunError`), which stops the run."""
    return isinstance(error, errors.ToolError) and not isinstance(error, files.RunError)


# ---------------------------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------------------------


@dataclass
class Segment:
    """A range of a file, `start` to `end` (not included), with `written` bytes on disk."""

    start: int
    end: int
    written: int = 0

    @property
    def at(self) -> int:
        """Where it goes on from."""
        return self.start + self.written

    @property
    def left(self) -> int:
        """Bytes still to fetch."""
        return self.end - self.at


@dataclass
class Segments:
    """How one file is split: its segments, and what its first URL said of it.

    A pure planner: the save asks it when to split, fetches each segment, and keeps `meta` in
    the part's sidecar (`<name>.part.json`, `flnkit.files.Part`'s) so a later run resumes each
    segment.
    """

    size: int
    etag: str = ""
    last_modified: str = ""
    parts: list[Segment] = field(default_factory=list)
    # A plugin's `fetch` serves it by ranges (`File(ranged=True)`): `FETCH_SEGMENTS` at most.
    fetch: bool = False
    # The `elapsed` of the last check, so checks come at most every `TICK` s.
    checked: float = field(default=float("-inf"), repr=False)

    def __post_init__(self) -> None:
        if not self.parts:
            self.parts = [Segment(0, self.size)]

    @staticmethod
    def splittable(size: int | None, headers: Mapping[str, str], status: int) -> bool:
        """Whether a file may take more places: `SPLIT_FILE` bytes or more, from a host that
        answered `Accept-Ranges: bytes` or a 206."""
        ranges = _lower(headers).get("accept-ranges", "").strip().lower()
        return size is not None and size >= SPLIT_FILE and (ranges == "bytes" or status == 206)

    @staticmethod
    def same(first: Mapping[str, str], other: Mapping[str, str]) -> bool:
        """Whether a mirror serves the first URL's file: the same `Content-Length` and `ETag`."""
        one, two = _lower(first), _lower(other)
        return all(one.get(name) and one.get(name) == two.get(name)
                   for name in ("content-length", "etag"))

    @property
    def most(self) -> int:
        """The most segments it may have."""
        return FETCH_SEGMENTS if self.fetch else SEGMENTS

    @property
    def done(self) -> bool:
        """Whether every segment is on disk."""
        return all(not part.left for part in self.parts)

    def split(self, *, elapsed: float, room: bool, ended: bool = False) -> Segment | None:
        """The new segment, when the rule says to split now; else None.

        The rule, checked every `TICK` s from `SPLIT_AFTER` s into the transfer, and at once
        when a segment ends: with `room` and fewer than `most` segments, the largest range
        left of `SPLIT_RANGE` bytes or more is cut in half; the second half is the new one.

        Args:
            elapsed: seconds since the transfer started; asking more often than `TICK` is free.
            room: `Scheduler.room` of the file's host: a free place, and nobody waiting.
            ended: a segment just ended, so the rule runs now.
        """
        if elapsed < SPLIT_AFTER or (not ended and elapsed - self.checked < TICK):
            return None
        self.checked = elapsed
        if not room or len(self.parts) >= self.most:
            return None
        largest = max(self.parts, key=lambda part: part.left)
        if largest.left < SPLIT_RANGE:
            return None
        middle = largest.at + largest.left // 2
        made = Segment(middle, largest.end)
        largest.end = middle
        self.parts.append(made)
        return made

    @property
    def meta(self) -> dict[str, Any]:
        """What the part's sidecar keeps of it (`flnkit.files.Part.note`), so a later try or run
        resumes each segment: `{etag, last_modified, size, segments: [[start, end, written]]}`."""
        return {"etag": self.etag, "last_modified": self.last_modified, "size": self.size,
                "segments": [[part.start, part.end, part.written] for part in self.parts]}

    @classmethod
    def of(cls, meta: Mapping[str, Any], *, fetch: bool = False) -> Segments | None:
        """The segments a part's sidecar kept (`meta`), or None to start over.

        A sidecar's segments must cover the file from 0 to its `size`, each starting where the
        one before ends; one with a gap or an overlap is refused. Whether it is still the same
        file is the first answer's to say (its length and `ETag`).

        Args:
            meta: the part's sidecar (`Part.meta`).
            fetch: the file comes from a plugin's `fetch`, as `Segments.fetch`.
        """
        try:
            size = int(meta["size"])
            etag, last_modified = str(meta.get("etag") or ""), str(meta.get("last_modified") or "")
            parts = [Segment(int(start), int(end), int(written))
                     for start, end, written in meta["segments"]]
        except (ValueError, TypeError, KeyError):
            return None
        parts.sort(key=lambda part: part.start)
        ends = [0, *(part.end for part in parts)]
        if not parts or ends[-1] != size or not all(
                part.start == end and part.start <= part.at <= part.end
                for part, end in zip(parts, ends, strict=False)):
            return None
        return cls(size, etag, last_modified, parts, fetch)


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}

