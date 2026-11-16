"""Live stats: a run's counters, sampled into a `flnd.Stats` once a second while it is read.

Who calls it:
- `flnd._run.Run` makes one `Meter` per run, puts it in the run's context (`METER`), and asks
  it for a sample (`Meter.poll`) between two events while someone iterates the run;
- the parts of a run *track* what they own, for as long as they own it (`tracked`, `track`):
  the run's loop and browser (`flnd.site.shared_browser`), each source's limits
  (`flnd.site.source_run`), a wait a source asked for (`TargetSite.pause`), each progress
  board (`flnd.job.Board`), the step chain (`flnd._steps.attached`) and each file in flight
  (`flnd._transfer`). Outside a run there is no meter, and tracking does nothing.

What it reads: only small read-only snapshots the parts offer (`Limits.load` and
`Limits.pauses`, `RunBrowser.tabs`, `Board.listing`, `Chain.jobs`, a transfer's bar), never
their internals, so the UI and a script see the same numbers. The machine's side (flnd's RAM
and CPU, the browser's and ffmpeg's processes) comes from psutil, imported on the first
sample, so a run nobody reads never loads it; free disk comes from `shutil.disk_usage`.

How a sample goes (`Meter.poll`):
1. At most once a second (`EVERY`, on `flnd._time`'s clock, so a test's fake clock decides),
   the reader asks the run's loop for the counters it owns (`_counters`): the limits, the
   tabs, the transfers. Those are changed on the loop alone, so they are read there too, and
   never half-changed. The reader doesn't block on it for long (`HOP`): a loop busy handing an
   event to this very reader would wait for it, so a sample not ready yet is picked up at the
   next poll instead.
2. On the reader's thread, it adds what the loop doesn't own: the boards (under their lock),
   the chain's lanes, and the process numbers from psutil. Those are system calls, kept off
   the loop so no transfer waits on them.
3. It works out the speeds from the bytes moved since the last sample, and an ETA once every
   listing is done, and returns the `Stats`.

Rules a change must keep:
- Nothing here runs unless the run is iterated: `wait()` never polls, so it samples nothing.
- Tracking is cheap (a list append under a lock) and never raises into the part that tracks.
- The loop is never blocked by a sample: only `_counters` runs there, and it only copies.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import os
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from flnd import _time
from flnd._events import Host, Listing, Pause, Stats, System, Transfer, Transfers

__all__ = [
    "BOARD", "BROWSER", "CHAIN", "HOME", "LOOP", "METER", "Meter", "SOURCE", "TRANSFER",
    "WAIT", "track", "tracked",
]

# Seconds between two samples.
EVERY = 1.0
# How long a poll waits for the loop's half of a sample before leaving it for the next poll,
# and how long (on the run's clock) before giving up on it: a loop that closed before running it.
HOP = 0.05
STALE = 5.0
# Seconds between two listings of flnd's child processes (the browser's tree, ffmpeg).
CHILDREN = 5.0
# The most tabs one target holds (`flnd._browser.MAX_TABS`), the system panel's cap. Copied
# rather than imported, so a run that never opens a browser never imports Playwright's module.
TABS = 6
# Process names (lower case) that are ffmpeg's; any other child of flnd is the browser's tree
# (Playwright's driver and Chromium under it).
FFMPEG = ("ffmpeg", "ffprobe")

# What can be tracked. Each kind is read by the snapshot it offers:
LOOP = "loop"            # flnd._loop.Loop: where `_counters` runs
BROWSER = "browser"      # flnd._browser.RunBrowser: `.tabs`
HOME = "home"            # a Path: free disk on its volume
SOURCE = "source"        # flnd.site.SourceRun: `.source.code`, `.files.limits`
WAIT = "wait"            # a tuple (source, target, until, reason): a wait a source asked for
BOARD = "board"          # flnd.job.Board: `.listing()`, `.queued()`
CHAIN = "chain"          # flnd._steps.Chain: `.jobs`
TRANSFER = "transfer"    # flnd._transfer._Row: `.label`, `.host`, `.resumed`, `.bar`

# The run's meter, set in the run's context by `flnd._run.Run`; None outside a run.
METER: contextvars.ContextVar[Meter | None] = contextvars.ContextVar("flnd_meter", default=None)


# ---------------------------------------------------------------------------------------------
# Tracking: how a part of the run shows itself to the meter
# ---------------------------------------------------------------------------------------------

def track(kind: str, item: Any) -> None:
    """Show `item` to the run's meter for the rest of the run; nothing outside a run.

    Args:
        kind: what it is: one of the module's kinds (`BOARD`, ...).
        item: the object, read through the snapshot its kind offers.
    """
    meter = METER.get()
    if meter is not None:
        meter.add(kind, item)


@contextmanager
def tracked(kind: str, item: Any) -> Iterator[None]:
    """Show `item` to the run's meter for the block; nothing outside a run.

    Args:
        kind: what it is: one of the module's kinds.
        item: the object.

    Yields:
        Nothing; the item is tracked until the block ends.
    """
    meter = METER.get()
    if meter is None:
        yield
        return
    meter.add(kind, item)
    try:
        yield
    finally:
        meter.remove(kind, item)


# ---------------------------------------------------------------------------------------------
# The meter
# ---------------------------------------------------------------------------------------------

class Meter:
    """What one run tracks, and its samples: `poll` gives a `Stats` once a second.

    Made by `flnd._run.Run`, which calls `start` when the run starts and `poll` while it is
    read. Parts of the run `add` and `remove` themselves from any thread.

    Example:
        >>> meter = Meter()
        >>> meter.start()
        >>> meter.poll(saved=0, skipped=0, failed=0, bytes=0) is None   # not a second yet
        True
    """

    def __init__(self) -> None:
        # Guards `_items` and `_ended`: parts add and remove themselves from many threads.
        self._lock = threading.Lock()
        self._items: dict[str, list[Any]] = {}
        # Transfers that ended since the last sample, with their last count and what their part
        # held when they began, so their bytes still count toward the speed: a small file often
        # starts and ends between two samples.
        self._ended: list[tuple[int, float, int]] = []
        # Each transfer's count at the last sample, by id, for its speed.
        self._last: dict[int, float] = {}
        self._began = 0.0
        self._at = 0.0
        # The loop's half of a sample that wasn't ready at the last poll.
        self._pending: concurrent.futures.Future[Any] | None = None
        self._asked = 0.0
        # psutil's view of this process, made on the first sample; and its CPU seconds then.
        self._process: Any = None
        self._cpu: tuple[float, float] | None = None
        # flnd's child processes as last listed, and when (`time.perf_counter`).
        self._children: list[Any] | None = None
        self._listed = 0.0

    # -- tracking ----------------------------------------------------------------------------

    def add(self, kind: str, item: Any) -> None:
        """Track `item` as `kind`."""
        with self._lock:
            self._items.setdefault(kind, []).append(item)

    def remove(self, kind: str, item: Any) -> None:
        """Stop tracking `item`; a transfer's last count is kept for the next sample's speed.

        Args:
            kind: what it was tracked as.
            item: the object; one not tracked is ignored.
        """
        with self._lock:
            items = self._items.get(kind, [])
            for index, held in enumerate(items):
                if held is item:
                    del items[index]
                    break
            if kind == TRANSFER:
                bar = getattr(item, "bar", None)
                self._ended.append((id(item), bar.count if bar is not None else 0.0,
                                    getattr(item, "resumed", 0)))

    def _all(self, kind: str) -> list[Any]:
        """A copy of what is tracked as `kind`, each item once, in the order it came."""
        with self._lock:
            items = list(self._items.get(kind, ()))
        seen: set[int] = set()
        return [item for item in items if id(item) not in seen and not seen.add(id(item))]

    # -- sampling ----------------------------------------------------------------------------

    def start(self) -> None:
        """Mark the run's start: elapsed counts from here, and the first sample is due in
        `EVERY` seconds."""
        self._began = self._at = _time.now()

    def poll(self, *, saved: int, skipped: int, failed: int, bytes: int,
             stopping: str = "", suspended: bool = False) -> Stats | None:
        """A new sample when one is due and ready; None otherwise. Never blocks for long.

        Args:
            saved: the run's saved files so far, from its report.
            skipped: its skipped files.
            failed: its failed files and targets.
            bytes: the bytes of its saved files.
            stopping: "", "soon" or "now": the run's stop stage.
            suspended: whether the run is suspended.

        Returns:
            The `Stats`, or None when it isn't time yet, or the loop's half isn't ready.
        """
        now = _time.now()
        if self._pending is None:
            if now - self._at < EVERY:
                return None
            self._pending, self._asked = self._ask_loop(), now
        try:
            counted = self._pending.result(timeout=HOP)
        except concurrent.futures.TimeoutError:
            if now - self._asked < STALE:
                return None
            self._pending.cancel()
            counted = _Counted()
        except Exception:  # noqa: BLE001 - a loop that closed meanwhile: sample without it
            counted = _Counted()
        self._pending = None
        return self._build(counted, saved=saved, skipped=skipped, failed=failed, bytes=bytes,
                           stopping=stopping, suspended=suspended)

    def _ask_loop(self) -> concurrent.futures.Future[Any]:
        """Hand `_counters` to the run's loop; read them here when there is no loop running.

        Returns:
            A future of the copies: the loop's, or one already done.
        """
        loops = self._all(LOOP)
        loop = loops[-1] if loops else None
        if loop is not None and loop.running:
            try:
                return loop.submit(self._on_loop())
            except RuntimeError:
                pass
        done: concurrent.futures.Future[Any] = concurrent.futures.Future()
        try:
            done.set_result(self._counters())
        except Exception as error:  # noqa: BLE001 - one broken snapshot never ends a run
            done.set_exception(error)
        return done

    async def _on_loop(self) -> _Counted:
        """`_counters`, as a coroutine for the loop."""
        return self._counters()

    def _counters(self) -> _Counted:
        """Copy what the loop owns: each source's hosts and waits, the tabs, the transfers.

        Runs on the loop (or, with no loop, on the reader's thread), so nothing it reads is
        changing while it reads.

        Returns:
            The copies.
        """
        counted = _Counted()
        now = _time.now()
        for run in self._all(SOURCE):
            limits = run.files.limits
            code = run.source.code
            counted.hosts += [Host(host, load.cap, load.busy, load.waiting, load.most)
                              for host, load in limits.load.items()]
            counted.pauses += [Pause(code, pause.host, pause.seconds, pause.reason)
                               for pause in limits.pauses]
        for code, target, until, reason in self._all(WAIT):
            if until > now:
                counted.pauses.append(Pause(code, target, until - now, reason))
        counted.tabs = sum(browser.tabs for browser in self._all(BROWSER))
        for row in self._all(TRANSFER):
            bar = row.bar
            done = bar.count if bar is not None else 0.0
            total = bar.total if bar is not None else None
            counted.transfers.append((id(row), row.label, row.host, done, total, row.resumed))
        with self._lock:
            counted.ended, self._ended = self._ended, []
        return counted

    def _build(self, counted: _Counted, *, saved: int, skipped: int, failed: int, bytes: int,
               stopping: str, suspended: bool) -> Stats:
        """Make the `Stats` from the loop's copies and what this thread reads.

        Args:
            counted: what `_counters` copied.
            saved: as `poll` takes them, down to `suspended`.
            skipped: see `poll`.
            failed: see `poll`.
            bytes: see `poll`.
            stopping: see `poll`.
            suspended: see `poll`.

        Returns:
            The sample.
        """
        now = _time.now()
        seconds = max(now - self._at, 1e-9)
        self._at = now
        # 1. Speeds: what each transfer moved since the last sample. One seen for the first
        #    time starts from what its part held (`base`), so a resume isn't counted as speed.
        files: list[Transfer] = []
        moved = 0.0
        last: dict[int, float] = {}
        for key, name, host, done, total, base in counted.transfers:
            previous = self._last.get(key, base)
            step = max(0.0, done - previous)
            moved += step
            # Never below what was counted: a bar not opened yet reads 0, and the resumed bytes
            # it then jumps to aren't speed.
            last[key] = max(done, previous)
            files.append(Transfer(name, host, int(done), None if total is None else int(total),
                                  step / seconds, base > 0))
        for key, done, base in counted.ended:
            moved += max(0.0, done - self._last.get(key, base))
        self._last = last
        # 2. Listings and queues, from the boards; lanes from the chain.
        listing: list[Listing] = []
        queued = 0
        for board in self._all(BOARD):
            listing += board.listing()
            queued += board.queued()
        lanes: dict[str, tuple[int, int]] = {}
        for chain in self._all(CHAIN):
            for name, (running, waiting) in chain.jobs.items():
                was = lanes.get(name, (0, 0))
                lanes[name] = (was[0] + running, was[1] + waiting)
        # 3. The ETA: known once every listing is done and files are still to come, at the
        #    pace files have ended so far.
        elapsed = now - self._began
        eta = _eta(listing, saved + failed, elapsed)
        hosts = tuple(sorted(counted.hosts, key=lambda host: (-host.busy, host.host)))
        return Stats(
            elapsed=elapsed, saved=saved, skipped=skipped, failed=failed, bytes=bytes,
            speed=moved / seconds, eta=eta, stopping=stopping, suspended=suspended,
            transfers=Transfers(running=len(files), queued=queued,
                                resuming=sum(file.resumed for file in files), hosts=hosts,
                                files=tuple(files)),
            listing=tuple(listing),
            pauses=tuple(sorted(counted.pauses, key=lambda pause: -pause.seconds)),
            system=self._system(counted.tabs, lanes),
        )

    def _system(self, tabs: int, lanes: dict[str, tuple[int, int]]) -> System:
        """The machine's side: psutil for the processes, `disk_usage` for the home's volume.

        Args:
            tabs: the browser's tabs open, from the loop.
            lanes: each step lane's `(running, waiting)`.

        Returns:
            The `System` part of the sample; a number psutil can't read stays 0.
        """
        ram, cpu, browser, ffmpeg = self._processes()
        homes = self._all(HOME)
        return System(
            tabs=tabs, cap=TABS, browser=browser, ram=ram, cpu=cpu, ffmpeg=ffmpeg,
            pillow=lanes.get("photo", (0, 0))[0], unpack=lanes.get("archive", (0, 0))[0],
            steps=sum(waiting for _, waiting in lanes.values()),
            disk=_free(homes[-1]) if homes else 0,
        )

    def _processes(self) -> tuple[int, float, int, int]:
        """flnd's RSS and CPU, the browser tree's RSS, and the ffmpeg processes, from psutil.

        CPU is the process's user and system seconds since the last sample over the wall
        seconds between (real time, not the run's clock): 100 is one core kept busy.

        Returns:
            `(ram, cpu, browser, ffmpeg)`; zeros where psutil can't read.
        """
        import psutil

        try:
            if self._process is None:
                self._process = psutil.Process()
            process = self._process
            with process.oneshot():
                ram = process.memory_info().rss
                times = process.cpu_times()
            used = times.user + times.system
            wall = time.perf_counter()
            last, self._cpu = self._cpu, (used, wall)
            cpu = 0.0 if last is None else 100.0 * (used - last[0]) / max(wall - last[1], 1e-9)
            # Listing the children walks every process of the machine (7 ms on macOS, the most
            # of a sample), so it is done every `CHILDREN` seconds; in between, the processes
            # found last time are read again. A new ffmpeg shows within that time.
            if self._children is None or wall - self._listed >= CHILDREN:
                self._children, self._listed = process.children(recursive=True), wall
            children = self._children
        except psutil.Error:
            return 0, 0.0, 0, 0
        browser = ffmpeg = 0
        gone = []
        for child in children:
            try:
                with child.oneshot():
                    name = child.name().lower()
                    rss = child.memory_info().rss
            # A process that ended since the listing, or one we may not read.
            except psutil.Error:
                gone.append(child)
                continue
            if name.startswith(FFMPEG):
                ffmpeg += 1
            else:
                browser += rss
        for child in gone:
            children.remove(child)
        return ram, cpu, browser, ffmpeg


class _Counted:
    """What `_counters` copies on the loop, for `_build` on the reader's thread."""

    def __init__(self) -> None:
        self.hosts: list[Host] = []
        self.pauses: list[Pause] = []
        self.tabs = 0
        # (id, label, host, done, total, base) per transfer in flight.
        self.transfers: list[tuple[int, str, str, float, float | None, int]] = []
        # (id, last count, resumed bytes) per transfer that ended since the last sample.
        self.ended: list[tuple[int, float, int]] = []


def _eta(listing: list[Listing], ended: int, elapsed: float) -> float | None:
    """Seconds left, from the files still to come and the pace files have ended at.

    Args:
        listing: every listing row.
        ended: files saved or failed so far (a skipped file costs no time).
        elapsed: the run's seconds so far.

    Returns:
        The seconds; None while a listing still runs (the total isn't known), when nothing is
        left, or before any file ended.
    """
    if not listing or any(row.state == "listing" for row in listing):
        return None
    left = sum(max(0, row.found - row.saved - row.skipped - row.failed) for row in listing)
    if not left or not ended or elapsed <= 0:
        return None
    return left * elapsed / ended


def _free(home: Path) -> int:
    """Free bytes on the volume of `home`, or of its nearest folder that exists yet.

    Args:
        home: the home folder, which a run that saved nothing yet may not have made.

    Returns:
        The free bytes; 0 when the volume can't be read.
    """
    path = Path(os.path.abspath(home))
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0
