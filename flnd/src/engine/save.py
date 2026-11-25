"""The save: a post's files from their parts to their rows, then unpack, `Saved`.

SPEC.md#for-developers explains it.
"""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import json
import mimetypes
import re
import shutil
import sys
import threading
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar
from urllib.parse import urlsplit

from flnd.engine import config, jobs, store
from flnd.engine.events import Failed, Saved, sink
from flnd.engine.jobs import Job, Kind
from flnd.errors import (
    DiskError,
    Error,
    NetworkError,
    SiteError,
    Stopped,
    UsageError,
    classify,
)
from flnd.plugin import Fetch, File, Part, Target
from flnd.ui.log import ui
from flnkit import clock, files, media, net, text

__all__ = ["STEM", "Item", "Saver", "stem_extension", "typed"]

T = TypeVar("T")

# A saved file's modified time is noon UTC of its post's day.
NOON = 12
# Extensions that name one format, so a `.jpeg` holding a JPEG keeps its name.
SAME_EXTENSIONS = {".jpeg": ".jpg", ".tiff": ".tif", ".m4v": ".mp4"}
NOT_MEDIA_HINT = "it is usually a login wall: run flnd auth login {code}, then try again"
# A lazy post's link that answers one of these has expired: its post is asked for a fresh one.
EXPIRED = frozenset({403, 410})
# The kind of an API 2 file named by a stem (5.3): its extension is what its bytes are, else
# what its `Content-Type` names, else its URL's, else none; the planned name may hold the URL's.
STEM = "stem"
# A leading `YYYY-MM-DD_` prefix put in front of a name by `{date}_` filename templates.
LEADING_DAY = re.compile(r"\d{4}-\d\d-\d\d_")


@dataclass(frozen=True)
class Item:
    """One file of a post, placed by the crawl: what to fetch, where, and its row's fields.

    Args:
        key: its row's key, `<code>:<key>`.
        target: the target it is saved for.
        post: its post's id.
        category: the category the post was listed in; "" for a route's own posts.
        file: what to fetch: a URL, mirrors, a `fetch` function, or a video with its audio.
        dest: where it lands; the extension may change to what its content calls for.
        kind: what it must be (`files.IMAGE`, `files.SNIFF`, …); None checks nothing.
        day: its post's day, the file's modified time at noon UTC; None uses `Last-Modified`.
        headers: extra request headers for its transfer.
        count: how many files the post has, on its first file's row only; None otherwise.
        estimated: whether `day` was taken from the posts around it.
        dated_name: whether the name uses `{date}_` and may take `Last-Modified`.
        cursor: the page its post was listed on, kept with a failure for its retry.
        retried: whether an earlier run failed on it.
        refresh: asks the post for its files again when the link expired; None for none.
        label: its path under its target, as the run's screen shows it.
        dropped: whether a file whose transfer hasn't started is dropped (the first Ctrl-C);
            None never drops it.
        domain: the plugin's first domain, the host a file its `fetch` function writes counts
            under, so the plugin's `limits` for it apply; "" uses the plugin's code.
    """

    key: str
    target: Target
    post: str
    category: str
    file: File
    dest: Path
    kind: str | None = None
    day: datetime.date | None = None
    headers: Mapping[str, str] | None = None
    count: int | None = None
    estimated: bool = False
    dated_name: bool = False
    cursor: str | None = None
    retried: bool = False
    refresh: Callable[[], Awaitable[File | None]] | None = None
    label: str = ""
    dropped: Callable[[], bool] | None = None
    domain: str = ""


@dataclass
class _Landed:
    """A file in place, waiting for its post's commit."""

    item: Item
    path: Path
    day: datetime.date | None
    row: store.FileRow


@dataclass(frozen=True)
class _Failure:
    """A file that failed, for its `failures` row in its post's commit."""

    item: Item
    error: Error


def _failed_row(failure: _Failure) -> dict[str, Any]:
    """A failed file's `store.Store.fail` arguments, for its post's commit."""
    item = failure.item
    url = item.file.url if isinstance(item.file.url, str) else ""
    return {"item": item.key, "target": store.key(item.target), "error": failure.error,
            "post": item.post, "category": item.category, "file": str(item.dest), "url": url,
            "cursor": item.cursor}


class _Mismatch(Exception):

    """A segment's answer isn't of the file the first URL served: another length or `ETag`."""


class _Split:
    """One file's segments while it transfers: the plan, and a task per segment."""

    def __init__(self, saver: Saver, item: Item, urls: Sequence[str], held: files.Part,
                 client: net.Client, job: Job[Any]) -> None:
        self.saver, self.item, self.urls, self.held, self.client = saver, item, urls, held, client
        self.job = job
        self.plan: jobs.Segments | None = None
        self.began = clock.now()
        self._tasks: list[asyncio.Task[Any]] = []
        # The part's one sidecar writer, while it runs, and whether `meta` changed since it
        # took its last snapshot.
        self._keeper: asyncio.Task[None] | None = None
        self._dirty = False
        # Whether a sidecar of the part's segments is on disk, and whether this resumed one.
        self._kept = False
        self.resumed = False

    def end(self) -> int | None:
        """Where the first transfer stops: its segment's end; None while it is one transfer."""
        return self.plan.parts[0].end if self.plan is not None else None

    async def check(self, *, ended: bool = False) -> None:
        """Split when the rule says so, and keep where each segment is in the sidecar.

        The first segment starts only once a sidecar says the part is in segments: a crash
        before it would leave a part a plain fill appends to past the new segment's bytes.
        Later ones start at once, since the sidecar before them already covers their range.
        """
        plan = self.plan
        if plan is None:
            return
        made = plan.split(elapsed=clock.now() - self.began, room=self.saver.scheduler.room(
            self.job.host), ended=ended)
        if made is not None and self._kept:
            self.start(made)
            made = None
        if len(plan.parts) > 1:
            self.held.note(**plan.meta)
            await self._keep()
        if made is not None:
            self.start(made)

    async def _keep(self) -> None:
        """Wait until the sidecar holds `meta` as it is now."""
        self._dirty = True
        if self._keeper is None or self._keeper.done():
            self._keeper = asyncio.ensure_future(self._keeping())
        await asyncio.shield(self._keeper)

    async def _keeping(self) -> None:
        while self._dirty:
            self._dirty = False
            await self.saver._call(self.held.keep, dict(self.held.meta))
            self._kept = True

    def start(self, part: jobs.Segment) -> None:
        """A segment's transfer, as a task."""
        number = len(self._tasks) + 1
        self._tasks.append(asyncio.ensure_future(_kept(self._segment(part, number))))

    async def finish(self) -> None:
        """Wait for every segment, those that start meanwhile too; the first failure is raised.

        Raises:
            Error: a segment failed after its job's tries.
        """
        done = 0
        while done < len(self._tasks):
            result = await self._tasks[done]
            done += 1
            if isinstance(result, BaseException):
                raise result

    async def cancel(self) -> None:
        """Stop the segments still going; their bytes stay, as the sidecar says."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._keeper is not None:
            # The part's own write of its sidecar, as it closes, comes after this one.
            await asyncio.gather(self._keeper, return_exceptions=True)
        if self.plan is not None and len(self.plan.parts) > 1:
            self.held.note(**self.plan.meta)

    async def _segment(self, part: jobs.Segment, number: int) -> None:
        """One segment: from a mirror that serves the same file (by its length and `ETag`), or
        from the first URL, which a mismatch falls back to."""
        urls = self.urls
        url = urls[number % len(urls)] if len(urls) > 1 else urls[0]
        try:
            await self._fetch(part, url)
        except _Mismatch:
            if url == urls[0]:
                raise
            ui.debug("{name}: a mirror serves another file; its segment comes from the first",
                     name=self.item.dest.name)
            await self._fetch(part, urls[0])
        await self.check(ended=True)

    async def _fetch(self, part: jobs.Segment, url: str) -> None:
        saver, plan = self.saver, self.plan
        assert plan is not None
        job = saver._file_job(self.item, self.job.plugin, url, _nothing)
        # The first transfer counts the whole file against its disk's free space.
        job.host, job.bucket, job.size = self.job.host, self.job.host, None

        def opened(status: int, headers: Mapping[str, str], start: int,
                   total: int | None) -> None:
            etag = headers.get("etag", "")
            if total != plan.size or (plan.etag and etag and etag != plan.etag):
                raise _Mismatch(f"{url}: {total} bytes, ETag {etag}")

        async def wrote(size: int) -> None:
            part.written += size
            await self.check()

        async def work() -> None:
            sink = _Bytes(job, saver.scheduler.slowest)
            sink.flight = saver._fly(self.item)
            meter = saver._meter(sink, self.item.dest, at=part.at)
            meter.opened, meter.wrote = opened, wrote
            await self.client.span(url, self.held, part.at, headers=self.item.headers,
                                   validator=plan.etag or None, progress=meter,
                                   until=lambda: part.end)

        job.work = work
        await saver.scheduler.run(job)


class _Fetching:
    """A `ranged` fetch file's segments while it transfers (`Saver._fetched`)."""

    def __init__(self, saver: Saver, item: Item, fetch: Fetch, held: files.Part,
                 job: Job[Any], plan: jobs.Segments) -> None:
        self.saver, self.item, self.fetch, self.held, self.job = saver, item, fetch, held, job
        self.plan = plan
        self.todo: deque[jobs.Segment] = deque(part for part in plan.parts if part.left)
        self._helpers: list[asyncio.Task[Any]] = []
        self._keeper: asyncio.Task[None] | None = None
        self._dirty = False

    async def take(self) -> None:
        """Take segments in this transfer until none is left; one more job starts before each
        while the host has room."""
        moved = _Bytes(self.job, self.saver.scheduler.slowest)
        moved.flight = self.saver._fly(self.item)
        while self.todo:
            if (len(self.todo) > 1 and len(self._helpers) + 1 < len(self.plan.parts)
                    and self.saver.scheduler.room(self.job.host)):
                self._helpers.append(asyncio.ensure_future(_kept(self._helper())))
            await self._next(moved)

    async def _helper(self) -> None:
        """One more `FILE` job of the file's host, taking segments while any is left."""
        saver = self.saver

        async def work() -> None:
            moved = _Bytes(own, saver.scheduler.slowest)
            moved.flight = saver._fly(self.item)
            while self.todo:
                await self._next(moved)

        own = saver._file_job(self.item, self.job.plugin, self.fetch, work)
        own.host, own.bucket, own.size = self.job.host, self.job.host, None
        await saver.scheduler.run(own)

    async def _next(self, moved: _Bytes) -> None:
        """Fetch the next segment left; one that fails goes back for another try."""
        part = self.todo.popleft()
        try:
            await self._segment(part, moved)
        except BaseException:
            self.todo.appendleft(part)
            raise

    async def _segment(self, part: jobs.Segment, moved: _Bytes) -> None:
        stop, write = self.saver.stop, self.saver._writer(self.item.dest)
        unkept = 0

        async def sink(data: bytes) -> None:
            nonlocal unkept
            if stop is not None and stop.is_set():
                raise KeyboardInterrupt
            if not data:
                return
            await write(self.held.write_at, part.at, data)
            part.written += len(data)
            moved.advance(len(data))
            unkept += len(data)
            if unkept >= net.BUFFER:
                unkept = 0
                await self.keep()

        await self.fetch(Part(sink, offset=part.at, end=part.end))
        await self.keep()
        if part.left:
            raise NetworkError(f"{self.item.dest.name}: a part stopped {part.left} bytes short",
                               retryable=True)

    async def keep(self) -> None:
        """Wait until the sidecar holds the segments as they are now. One task writes it, a
        snapshot at a time, as `_Split._keep` does."""
        self.held.note(**self.plan.meta)
        self._dirty = True
        if self._keeper is None or self._keeper.done():
            self._keeper = asyncio.ensure_future(self._keeping())
        await asyncio.shield(self._keeper)

    async def _keeping(self) -> None:
        while self._dirty:
            self._dirty = False
            await self.saver._call(self.held.keep, dict(self.held.meta))

    async def finish(self) -> None:
        """Wait for the other jobs; the first failure is raised. Once every segment is in, the
        sidecar is dropped as the part lands.

        Raises:
            Error: a segment failed after its job's tries.
            NetworkError: a segment was left, its job given up.
        """
        for task in self._helpers:
            result = await task
            if isinstance(result, BaseException):
                raise result
        if self.todo:
            raise NetworkError(f"{self.item.dest.name}: {len(self.todo)} parts left",
                               retryable=True)
        self.held.note(segments=None, size=None, last_modified=None, etag=None)

    async def cancel(self) -> None:
        """Stop the other jobs still going; their bytes stay, as the sidecar says."""
        for task in self._helpers:
            task.cancel()
        await asyncio.gather(*self._helpers, return_exceptions=True)
        if self._keeper is not None:
            await asyncio.gather(self._keeper, return_exceptions=True)


class _Dropped(Stopped):
    """A file whose transfer never started: the run, or its target, stopped first."""


@dataclass
class _Flow:
    """One file's place in the run's counts: whether its transfer started."""

    started: bool = False


class _Bytes:
    """A transfer's progress sink (`net.Meter`'s): the file's `Flight` on the run's screen,
    and its job's `done`, the bytes the host's cap measures."""

    def __init__(self, job: Job[Any], slowest: float = 0.0) -> None:
        self._job = job
        self._slowest = slowest
        # The try's `Flight`, set as each try starts; None without a tally.
        self.flight: Any = None
        # The try's speed, for `slowest`.
        self.speed = jobs.Speed()

    @property
    def total(self) -> int | None:
        return self.flight.total if self.flight is not None else None

    @total.setter
    def total(self, value: float | None) -> None:
        if self.flight is not None:
            self.flight.size(value)

    def advance(self, n: float) -> None:
        """Count `n` more bytes.

        Raises:
            NetworkError: the try averaged under `slowest` over its last 30 s: it is cut, and
                the scheduler tries it again from where it was, as one of its network tries.
        """
        self._job.done += int(n)
        if self.flight is not None:
            self.flight.advance(n)
        self.speed.add(int(n))
        if self.speed.below(self._slowest):
            raise NetworkError(f"slower than {text.size(self._slowest)}/s for "
                               f"{jobs.SLOW_WINDOW:.0f} s", retryable=True)


# ---------------------------------------------------------------------------------------------
# The save


@dataclass
class _Done:
    """What the run's saves did, for the summary line at its end."""

    unpacked: int = 0
    unpack_failed: int = 0


class Saver:
    """A run's saves: each post's files through the eight stages, their rows one commit a post.

    Every step is a job of the run's scheduler: a file's transfer a `FILE` job (with its
    mirrors), a DASH join a `MERGE` job, an unpack an `UNPACK` job and a post's commit a
    `WRITE` job; blocking calls go to the threads of their kind (`Scheduler.on`). Used on the
    run's loop only.

    Args:
        store: the run's flnd.db.
        scheduler: the run's scheduler.
        emit: where `Saved`, `Skipped` and `Failed` go; what it gives is awaited.
        run: the run's id in `runs`, for failures; None outside a run.
        stop: the run's second Ctrl-C, which a plugin's `fetch` function stops at when it
            next reports its progress; None never stops it.
        tally: the run's live counts (`flnd.engine.run.Tally`); None counts nothing.
        sample: keeps a scrubbed sample of a `Changed` answer, and points its hint at it.
    """

    def __init__(self, db: store.Store, scheduler: jobs.Scheduler,
                 emit: Callable[[Any], Awaitable[None]], *,
                 run: int | None = None,
                 timeout: float = 600, stop: threading.Event | None = None, tally: Any = None,
                 sample: Callable[[Error], None] | None = None,
                 claims: Any = None) -> None:
        self.store, self.scheduler, self.run = db, scheduler, run
        self.emit = sink(emit)
        self.stop = stop
        self.timeout = timeout
        self.tally, self.sample = tally, sample
        self.claims = claims
        self.done = _Done()
        self._checkpoints: set[asyncio.Task[Any]] = set()
        # Each target's last file to join the scheduler's queue (`_turn`).
        self._turns: dict[Target, asyncio.Event] = {}
        # Concurrency slot semaphore per target, limiting open parts to scheduler places.
        self._slots: dict[Target, asyncio.Semaphore] = {}
        # Each ranged fetch or segmented file's segments, by its first transfer's job.
        self._fetching: dict[Job[Any], _Fetching] = {}
        self._splitting: dict[Job[Any], _Split] = {}

    async def post(self, items: Sequence[Item], *, client: net.Client,
                   plugin: str) -> dict[str, Path]:
        """Save one post's files at once, commit their rows together, then finish each.

        A file that fails is reported, and the others go on.

        Args:
            items: the post's files.
            client: the plugin's downloading client.
            plugin: the plugin's code, for its jobs.

        Returns:
            Where each file that landed is, by its key.

        Raises:
            Error: what stops the run (`jobs.stops`); what landed is committed first.
            Stopped: the second Ctrl-C; what landed is committed first.
        """
        if not mimetypes.inited:
            # `files.kind_of` reads the system's types once; that read stays off the loop.
            await self._call(mimetypes.init)
        found = await _all(*(self._file(item, client, plugin) for item in items))
        landed = [one for one in found if isinstance(one, _Landed)]
        failures = [one for one in found if isinstance(one, _Failure)]
        fatal = next((one for one in found if isinstance(one, BaseException)), None)
        if landed or failures:
            await self._commit(landed, failures)
        if fatal is not None:
            raise fatal
        _raised(await _all(*(self._finish(one, plugin) for one in landed)))
        return {one.item.key: one.path for one in landed}

    async def close(self) -> None:
        """Wait for the checkpoints running, checkpoint once more, and say what was done."""
        await asyncio.gather(*self._checkpoints)
        await self._call(self.store.checkpoint, force=True)
        done, parts = self.done, []
        if done.unpacked:
            parts.append(f"{text.count(done.unpacked, 'archive')} extracted")
        if done.unpack_failed:
            parts.append(f"{done.unpack_failed} not extracted")
        if parts:
            (ui.warn if done.unpack_failed else ui.ok)("Media: {line}", line="; ".join(parts))

    # -- stages 1 to 3: the part, the transfer, the content check -------------------------

    async def _file(self, item: Item, client: net.Client,
                    plugin: str) -> _Landed | _Failure | None:
        """One file into place and its row made; None when it was dropped or had no URL."""
        flow = _Flow()
        try:
            coro = self._land(item, client, plugin, flow)
            landed = await (asyncio.wait_for(coro, timeout=self.timeout)
                            if self.timeout > 0 else coro)
        except TimeoutError:
            error = NetworkError(f"{item.dest.name}: timed out after {int(self.timeout)}s")
            return await self._failed(item, error, flow)
        except _Dropped:
            self._count(item, queued=-1)
            if self.claims is not None:
                self.claims.release(item.dest, item.key)
            return None
        except Error as error:
            if jobs.stops(error):
                raise
            return await self._failed(item, error, flow)
        except Exception as error:
            wrapped = classify(error)
            if jobs.stops(wrapped):
                raise wrapped from error
            return await self._failed(item, wrapped, flow)
        if landed is None:
            self._count(item, **({"downloading": -1} if flow.started else {"queued": -1}))
        return landed

    async def _failed(self, item: Item, error: Error, flow: _Flow) -> _Failure:
        if self.sample is not None:
            self.sample(error)
        self._count(item, failed=1, **({"downloading": -1} if flow.started else {"queued": -1}))
        await self.emit(Failed(item.target, error, post=item.post, path=item.dest))
        ui.debug("{name}: {error}", name=item.label or item.dest.name, error=error)
        return _Failure(item, error)

    def _count(self, item: Item, **steps: int) -> None:
        if self.tally is not None:
            self.tally.count(item.target, item.category, **steps)

    def _start(self, item: Item, flow: _Flow) -> None:
        """A transfer of `item` starts: drop it after the first Ctrl-C, else count it."""
        if flow.started:
            return
        if item.dropped is not None and item.dropped():
            raise _Dropped(f"{item.dest.name}: dropped")
        flow.started = True
        self._count(item, queued=-1, downloading=1)

    async def _land(self, item: Item, client: net.Client, plugin: str,
                    flow: _Flow) -> _Landed | None:
        file = item.file
        if not _sources(file):
            if getattr(file, "playlist", None) is not None:
                raise UsageError(f"{item.dest.name}: a segmented stream (an HLS or DASH "
                                 "playlist) isn't saved yet")
            return None
        received: dict[str, str] = {}
        try:
            saved = await self._transfer(item, file, client, plugin, received, flow)
        except SiteError as error:
            if error.status not in EXPIRED or item.refresh is None:
                raise
            ui.info("{name}: HTTP {status}, asking for a fresh link",
                    name=item.label or item.dest.name, status=error.status)
            fresh = await item.refresh()
            if fresh is None or not _sources(fresh) or fresh.content is not None:
                raise
            saved = await self._transfer(item, fresh, client, plugin, received, flow)
        served = served_day(received.get("last-modified"))
        dated = served if (item.day is None or item.estimated) and served is not None else item.day
        still_estimated = bool(item.estimated and served is None)
        row = store.FileRow(
            item.key, store.key(item.target), str(saved.path), saved.size, saved.mtime,
            post_id=item.post, category=item.category,
            date=dated.isoformat() if dated else None, estimated=still_estimated,
            kind=files.kind_of(saved.path.name), at=store.now(), files=item.count)
        return _Landed(item, saved.path, dated, row)

    async def _transfer(self, item: Item, file: File, client: net.Client, plugin: str,
                        received: dict[str, str], flow: _Flow) -> files.Saved:
        """Fill the file's part from its mirrors in order, check it and land it."""
        mirrors = _sources(file)
        if file.audio is not None and isinstance(mirrors[0], str):
            try:
                return await self._dash(item, mirrors[0], file.audio, client, plugin, flow)
            except Exception as error:
                if len(mirrors) == 1 or _final(error):
                    raise
                ui.warn("mirror 1 failed: {error}; trying the next", error=error)
                mirrors = mirrors[1:]
        turn = self._turn(item.target)
        try:
            slot = self._slot(item.target)
            await slot.acquire()
            try:
                async with self._part(item.dest) as held:
                    if item.day is not None:
                        held.mtime = noon(item.day)
                    try:
                        await self._mirrors(item, mirrors, held, client, plugin, received, flow,
                                            turn)
                    except _Dropped:
                        if not held.size:
                            # Never started: no empty part is left behind.
                            await self._call(held.drop)
                        raise
                    # The last mirror's content check, and the landing, come after the job gave
                    # back its places: a small file holds its host's cap for its transfer alone.
                    if item.kind is not None:
                        await self._call(_place, held, item, received)
                    _apply_served_day(held, item, received)
            finally:
                slot.release()
        finally:
            # A file that never queued lets the next one go on.
            turn[1].set()
        assert held.saved is not None
        return held.saved

    def _slot(self, target: Target) -> asyncio.Semaphore:
        """A target's places slot semaphore: limits concurrent downloads holding parts."""
        slot = self._slots.get(target)
        if slot is None:
            places = getattr(getattr(self.scheduler, "limits", None), "places", 32)
            slot = self._slots[target] = asyncio.Semaphore(max(1, places))
        return slot

    def _turn(self, target: Target) -> tuple[asyncio.Event | None, asyncio.Event]:
        """The file's turn among its target's: the one before it, and its own."""
        mine = asyncio.Event()
        before = self._turns.get(target)
        self._turns[target] = mine
        return before, mine

    async def _mirrors(self, item: Item, mirrors: Sequence[str | Fetch], held: files.Part,
                       client: net.Client, plugin: str, received: dict[str, str], flow: _Flow,
                       turn: tuple[asyncio.Event | None, asyncio.Event]) -> None:
        """Fill the open part from each mirror in turn, each as a `FILE` job whose segments
        finish outside the job so a host cap or target pool can never deadlock on them."""
        before, mine = turn
        try:
            if before is not None:
                await before.wait()
            for number, mirror in enumerate(mirrors, start=1):
                try:
                    await self._one_mirror(item, mirror, number, mirrors, held, client, plugin,
                                           received, flow, mine)
                    if item.kind is not None and number < len(mirrors):
                        await self._call(_place, held, item, received)
                    return
                except Exception as error:
                    if number == len(mirrors) or _final(error):
                        raise
                    ui.warn("mirror {n} failed: {error}; trying the next", n=number, error=error)
                    # The next mirror may serve other bytes: it starts from an empty part.
                    await self._call(held.reset)
        finally:
            mine.set()

    async def _one_mirror(self, item: Item, mirror: str | Fetch, number: int,
                          mirrors: Sequence[str | Fetch], held: files.Part, client: net.Client,
                          plugin: str, received: dict[str, str], flow: _Flow,
                          mine: asyncio.Event) -> None:
        """Run one mirror's `FILE` job, then finish any segments after its places are released."""
        while True:
            job = self._file_job(item, plugin, mirror, _nothing)
            sink = _Bytes(job, self.scheduler.slowest)
            meter = self._meter(sink, item.dest)

            async def work(job: Job[Any] = job, sink: _Bytes = sink,
                           meter: net.Meter = meter) -> None:
                self._start(item, flow)
                sink.flight, sink.speed = self._fly(item), jobs.Speed()
                try:
                    if number == 1 and isinstance(mirror, str):
                        urls = [one for one in mirrors if isinstance(one, str)]
                        answered = await self._segmented(item, urls, held, client, meter, job)
                    else:
                        answered = await self._mirror(item, mirror, held, client, meter, job)
                    received.update(answered)
                except Exception as error:
                    if isinstance(error, Error):
                        raise
                    raise classify(error) from error
                finally:
                    if (sink.flight is not None and job not in self._splitting
                            and job not in self._fetching):
                        sink.flight.land()

            job.work = work
            try:
                running = self.scheduler.run(job)
                # Set before the job queues, which it does at once, ahead of the next file's.
                mine.set()
                await running
                split = self._splitting.get(job)
                if split is not None:
                    try:
                        # The other segments' jobs, waited for here, past this file's own job,
                        # which holds no place of its host or target meanwhile.
                        await split.finish()
                    except _Mismatch:
                        self._splitting.pop(job, None)
                        await split.cancel()
                        if not split.resumed:
                            raise
                        # Another file under the same name now: it comes whole.
                        await self._call(held.reset)
                        continue
                    self._splitting.pop(job, None)
                    if split.plan is not None and len(split.plan.parts) > 1:
                        held.note(segments=None, size=None, last_modified=None)
                fetching = self._fetching.get(job)
                if fetching is not None:
                    await fetching.finish()
                    self._fetching.pop(job, None)
                return
            finally:
                split = self._splitting.pop(job, None)
                if split is not None:
                    await split.cancel()
                fetching = self._fetching.pop(job, None)
                if fetching is not None:
                    await fetching.cancel()
                if sink.flight is not None:
                    sink.flight.land()

    async def _segmented(self, item: Item, urls: Sequence[str], held: files.Part,
                         client: net.Client, meter: net.Meter,
                         job: Job[Any]) -> Mapping[str, str]:
        """The first URL into the part, split into segments while its host has room (6.2)."""
        plan = jobs.Segments.of(held.meta) if held.size and held.meta.get("segments") else None
        if plan is not None:
            return self._resume(item, urls, held, client, plan, job)
        split = _Split(self, item, urls, held, client, job)

        def opened(status: int, headers: Mapping[str, str], start: int,
                   total: int | None) -> None:
            if start == 0 and jobs.Segments.splittable(total, headers, status):
                assert total is not None
                split.plan = jobs.Segments(total, headers.get("etag", ""),
                                           headers.get("last-modified", ""))

        async def wrote(size: int) -> None:
            if split.plan is not None:
                split.plan.parts[0].written += size
                await split.check()

        meter.opened, meter.wrote = opened, wrote
        try:
            ui.request(client, urls[0], str(item.dest), headers=item.headers, offset=held.size,
                       etag=held.meta.get("etag") if held.size else None)
            answered = await client.fill(urls[0], held, headers=item.headers, progress=meter,
                                         until=split.end)
        except BaseException:
            await split.cancel()
            raise
        finally:
            meter.opened = meter.wrote = None
        if split._tasks:
            self._splitting[job] = split
        return answered

    def _resume(self, item: Item, urls: Sequence[str], held: files.Part,
                client: net.Client, plan: jobs.Segments, job: Job[Any]) -> Mapping[str, str]:
        """Start each unfinished segment a part's sidecar kept; `_one_mirror` waits outside."""
        split = _Split(self, item, urls, held, client, job)
        split.plan = plan
        split._kept = True
        split.resumed = True
        for part in plan.parts:
            if part.left:
                split.start(part)
        self._splitting[job] = split
        return {"etag": plan.etag, "last-modified": plan.last_modified} if plan.last_modified \
            else {"etag": plan.etag}

    async def _mirror(self, item: Item, mirror: str | Fetch, held: files.Part,
                      client: net.Client, meter: net.Meter, job: Job[Any]) -> Mapping[str, str]:
        """One mirror into the part: a URL by the client, or a plugin's `fetch` function."""
        if isinstance(mirror, str):
            ui.request(client, mirror, str(item.dest), headers=item.headers, offset=held.size,
                       etag=held.meta.get("etag") if held.size else None)
            return await client.fill(mirror, held, headers=item.headers, progress=meter)
        await self._fetched(item, mirror, held, meter, job)
        return {}

    async def _fetched(self, item: Item, fetch: Fetch, held: files.Part,
                       meter: net.Meter, job: Job[Any]) -> None:
        """A plugin's `fetch` into the part, as `flnd.plugin.Part`s."""
        size = item.file.size
        if item.file.ranged and size is not None and size >= jobs.SPLIT_FILE:
            fetching = self._fetching.get(job) or await self._ranged(item, fetch, held, job, size)
            if fetching is not None:
                await fetching.take()
                return
        write, stop = self._writer(item.dest), self.stop
        state = {"at": held.size, "total": size}
        meter.start(size, held.size)

        async def sink(data: bytes) -> None:
            if stop is not None and stop.is_set():
                raise KeyboardInterrupt
            if data:
                await write(_append, held, data)
                state["at"] += len(data)
                meter.add(len(data))

        def sized(total: int) -> None:
            state["total"] = total
            meter.start(total, state["at"])

        await fetch(Part(sink, offset=held.size, sized=sized))
        total = state["total"]
        if total is not None and state["at"] < total:
            raise NetworkError(f"{item.dest.name}: {state['at']} of {total} bytes",
                               retryable=True)

    async def _ranged(self, item: Item, fetch: Fetch, held: files.Part, job: Job[Any],
                      size: int) -> _Fetching | None:
        """A `ranged` fetch file's segments: those its sidecar kept, else even ones when the
        host has a free place; None to fetch it as one part (a part begun whole, no room)."""
        plan = jobs.Segments.of(held.meta, fetch=True) if held.size else None
        if plan is not None and plan.size != size:
            await self._call(held.reset)
            plan = None
        if plan is None:
            if held.size or not self.scheduler.room(job.host):
                return None
            count = max(1, min(jobs.FETCH_SEGMENTS, size // jobs.SPLIT_RANGE))
            step = -(-size // count)
            plan = jobs.Segments(size, fetch=True, parts=[
                jobs.Segment(start, min(start + step, size))
                for start in range(0, size, step)])
        fetching = self._fetching[job] = _Fetching(self, item, fetch, held, job, plan)
        await fetching.keep()
        return fetching

    async def _dash(self, item: Item, video: str, audio: str, client: net.Client, plugin: str,
                    flow: _Flow) -> files.Saved:
        """A video and its audio as two `FILE` jobs at once, then one `MERGE` job into the
        file's part.

        Each stream has a part of its own (`<name>.video.part`), kept for the next try while
        the join fails; once joined, or given up on, both go. Empty audio saves the video alone.
        """
        ffmpeg = files.ffmpeg()
        self._start(item, flow)
        async with self._part(item.dest) as held:
            if item.day is not None:
                held.mtime = noon(item.day)
            names = (f"{held.dest.name}.video", f"{held.dest.name}.audio")
            async with self._part(held.dest.with_name(names[0])) as one, \
                    self._part(held.dest.with_name(names[1])) as two:
                streams = (one, two)
                await _together(*(self._stream(item, plugin, url, part, client)
                                  for url, part in zip((video, audio), streams, strict=True)))
                for stream in streams:
                    await self._call(stream.flush)
                if self.stop is not None and self.stop.is_set():
                    raise KeyboardInterrupt

                async def join() -> None:
                    try:
                        await media.join(one, two, held, ffmpeg)
                    except files.RunError as error:
                        # A join that failed may have read a stream cut short: worth another
                        # try, from the streams kept.
                        error.retryable = True
                        raise

                try:
                    if two.size == 0 and one.size > 0:
                        await self._call(shutil.copyfile, one.path, held.path)
                    else:
                        await self.scheduler.run(Job(
                            kind=Kind.MERGE, work=join, plugin=plugin,
                            target=str(item.target)))
                finally:
                    for stream in streams:
                        stream.drop()
        assert held.saved is not None
        return held.saved

    async def _stream(self, item: Item, plugin: str, url: str, part: files.Part,
                      client: net.Client) -> None:
        """One stream of a DASH pair into its part, as a `FILE` job of its host."""
        job = self._file_job(item, plugin, url, _nothing)
        sink = _Bytes(job, self.scheduler.slowest)
        meter = self._meter(sink, item.dest)
        ui.request(client, url, str(part.dest), headers=item.headers, offset=part.size,
                   etag=part.meta.get("etag") if part.size else None)

        async def work() -> None:
            sink.speed = jobs.Speed()
            await client.fill(url, part, headers=item.headers, progress=meter)

        job.work = work
        await self.scheduler.run(job)

    def _file_job(self, item: Item, plugin: str, first: str | Fetch,
                  work: Callable[[], Awaitable[Any]]) -> Job[Any]:
        """A `FILE` job for `item`: of its first mirror's host, or of the plugin's first domain
        for a file its `fetch` function writes."""
        host = (urlsplit(first).hostname or "") if isinstance(first, str) else item.domain
        return Job(kind=Kind.FILE, work=work, plugin=plugin,
                   host=host or plugin, target=str(item.target), size=item.file.size,
                   path=item.dest, priority=0 if item.retried else None)

    def _meter(self, sink: _Bytes, dest: Path, *, at: int = 0) -> net.Meter:
        """A transfer's meter: its writes on the thread of `dest`'s device; `at` is where a
        segment starts."""
        return net.Meter(sink, write=self._writer(dest), at=at)

    def _writer(self, dest: Path) -> net.Write:
        """What writes a transfer's pieces: the `WRITE` thread of `dest`'s device."""
        async def write(call: Callable[..., Any], *args: Any) -> Any:
            return await self.scheduler.on(Kind.WRITE, call, *args, path=dest)
        return write

    def _fly(self, item: Item) -> Any:
        """The file's `Flight` on the run's screen; None without a tally."""
        if self.tally is None:
            return None
        return self.tally.fly(item.key, item.target, item.label or item.dest.name)

    @asynccontextmanager
    async def _part(self, dest: Path) -> AsyncIterator[files.Part]:
        """`files.part(dest, resume=True)`, opened and landed on the scheduler's
        threads, never on the loop.
        """
        manager = files.part(dest, resume=True)
        held = await self._enter(manager, dest)
        try:
            yield held
        except BaseException as error:
            # The part keeps its bytes for the next run, and raises `error` again.
            await self.scheduler.on(Kind.WRITE, manager.__exit__, type(error), error,
                                    error.__traceback__, path=dest)
            raise
        await self.scheduler.on(Kind.WRITE, manager.__exit__, None, None, None, path=dest)

    async def _enter(self, manager: Any, dest: Path) -> files.Part:
        """Open a part on a thread."""
        opened: list[files.Part] = []

        def enter() -> files.Part:
            held = manager.__enter__()
            # An empty write makes the part's buffer here, so a write from the loop opens
            # nothing; one the disk refuses leaves the part at once.
            try:
                held.write(b"")
            except BaseException:
                manager.__exit__(*sys.exc_info())
                raise
            opened.append(held)
            return held

        try:
            return await self.scheduler.on(Kind.WRITE, enter, path=dest)
        except asyncio.CancelledError as error:
            if opened:
                await self.scheduler.on(Kind.WRITE, manager.__exit__, type(error), error, None,
                                        path=dest)
            raise

    # -- stage 4: the commit ------------------------------------------------------------------

    async def _commit(self, landed: Sequence[_Landed], failures: Sequence[_Failure]) -> None:
        """A post's rows in one transaction on the loop, as a `WRITE` job: a lock held
        elsewhere puts it back in the scheduler's queue (`jobs.Busy`), so the loop never
        stalls."""
        rows = [one.row for one in landed]
        failed = [_failed_row(one) for one in failures]
        await self._write(self.store.commit, rows, failures=failed, run=self.run)
        if self.claims is not None:
            for one in landed:
                self.claims.release(one.item.dest, one.item.key)
            for failure in failures:
                self.claims.release(failure.item.dest, failure.item.key)
        if self.store.due:
            task = asyncio.ensure_future(self._call(self.store.checkpoint))
            self._checkpoints.add(task)
            task.add_done_callback(self._checkpoints.discard)

    async def _write(self, call: Callable[..., Any], *args: Any, **options: Any) -> None:
        """`call(*args, **options)` on the store, on the loop, as a `WRITE` job; a lock
        another connection holds puts it back in the queue (`jobs.Busy`)."""
        async def work() -> None:
            try:
                call(*args, **options)
            except DiskError as error:
                if store.busy(error):
                    raise jobs.Busy(f"{self.store.path}: locked by another process") from error
                raise
        await self.scheduler.run(Job(kind=Kind.WRITE, work=work))

    # -- stages 5 to 7: unpack, `Saved` -----------

    async def _finish(self, landed: _Landed, plugin: str) -> None:
        item, path = landed.item, landed.path
        step: dict[str, Any] = {}
        if item.file.password is not None or files.is_archive(path):
            step = await self._unpack(item, path, plugin)
            await self._write(self.store.update, item.key, **step)
        self._count(item, downloading=-1, saved=1, bytes=landed.row.size)
        await self.emit(Saved(item.target, item.category, item.post, path, landed.day,
                              landed.row.size, retried=item.retried))
        if "extracted" in step:
            # Deleted once the row holds what it made.
            with suppress(OSError):
                path.unlink()

    async def _unpack(self, item: Item, path: Path, plugin: str) -> dict[str, Any]:
        """Extract an archive beside itself on the unpack lane; a failure keeps it, and warns."""
        here = item.file.unpack == "here"

        async def work() -> Path | list[Path]:
            return await self.scheduler.on(Kind.UNPACK, _extract, path, item.file.password, here)
        try:
            folder = await self.scheduler.run(Job(kind=Kind.UNPACK, work=work, plugin=plugin,
                                                  target=str(item.target)))
        except Error as error:
            if jobs.stops(error):
                raise
            self.done.unpack_failed += 1
            ui.warn("{name}: not extracted: {error}", name=path.name, error=error)
            return {"unpacked": "failed"}
        self.done.unpacked += 1
        if not here:
            return {"unpacked": "extracted", "folder": str(folder)}
        return {"unpacked": "extracted", "extracted": json.dumps([str(one) for one in folder])}

    async def _call(self, call: Callable[..., T], *args: Any, **options: Any) -> T:
        """`call(*args)` on the scheduler's `CALL` threads, waited for to its end."""
        if options:
            return await self.scheduler.on(Kind.CALL, lambda: call(*args, **options))
        return await self.scheduler.on(Kind.CALL, call, *args)


@asynccontextmanager
async def attached(settings: Any) -> AsyncIterator[Saver | None]:
    """Give a run's settings their save for the block (`settings.saver`), then close it.

    The save commits through a connection of its own to the home's `flnd.db`, and runs its
    jobs on the run's scheduler. It is closed on the run's loop before the loop and the
    database close. A run with no database, or a dry one, saves nothing.

    Args:
        settings: the run's `RunShared`, inside `ctx.shared_browser`.

    Yields:
        The save; None when the run saves nothing.

    Raises:
        UsageError: the home's `config.toml` is broken.
    """
    from flnd.engine import ctx

    scheduler = ctx.scheduler(settings)
    if settings.db is None or settings.dry or scheduler is None:
        yield None
        return
    # The configs and the store are read and opened off the loop.
    found, db = await asyncio.to_thread(_opened, settings)
    defaults = found.defaults
    claims = getattr(settings, "claims", None)
    saver = Saver(db, scheduler, settings.emit, run=settings.run,
                  timeout=config.duration_seconds(defaults.timeout), stop=settings.stop.now,
                  tally=settings.tally, sample=lambda error: sampled(settings, error),
                  claims=claims)
    settings.saver = saver
    try:
        yield saver
    finally:
        settings.saver = None
        try:
            await saver.close()
        finally:
            await asyncio.to_thread(db.close)


def _opened(settings: Any) -> tuple[Any, store.Store]:
    """`attached`'s reads, off the loop: the home's settings (the run's, else read here), and
    the run's commit connection (`loop=True`: it waits for no lock), made now; the config module
    is imported here too, since an import reads files."""
    from flnd.engine import config

    found = getattr(settings, "config", None) or config.settings(settings.home)
    db = store.open(settings.home, loop=True)
    try:
        db.ready()
    except BaseException:
        db.close()
        raise
    return found, db


def sampled(settings: Any, error: BaseException) -> None:
    """Keep a `Changed` error's answer as a scrubbed sample, and point its hint at it.

    Called before the failure is written and its `Failed` event sent, so both name the sample.

    Args:
        settings: the run's `RunShared`: its home and run id.
        error: what failed; anything but a `Changed` with a response is left as it is.
    """
    path = store.sample(settings.home, settings.run, error)
    if path is not None and isinstance(error, Error):
        saved = f"the answer is saved, scrubbed, in {path}"
        error.hint = f"{error.hint}; {saved}" if error.hint else saved


# ---------------------------------------------------------------------------------------------
# The blocking steps, each run on a thread


async def _nothing() -> None:
    """A job's work until its own is set."""


def _final(error: BaseException) -> bool:
    """Whether a mirror's failure ends the file at once: it stops the run, or it is a 429,
    which the scheduler waits out on the same mirror."""
    return jobs.stops(error) or getattr(error, "status", None) == 429


def _extract(path: Path, password: str | None, here: bool) -> Path | list[Path]:
    if here:
        return files.extract_into(path, path.parent, password=password)
    return files.extract(path, password=password)


def _apply_served_day(held: files.Part, item: Item, received: Mapping[str, str]) -> None:
    """Date an undated or estimated file from HTTP `Last-Modified`, updating its `{date}_` name."""
    if item.day is not None and not item.estimated:
        return
    served = served_day(received.get("last-modified"))
    if served is None:
        return
    held.mtime = noon(served)
    if not item.dated_name:
        return
    name = held.dest.name
    leading = LEADING_DAY.match(name)
    if item.estimated and leading:
        name = name[leading.end():]
    if not LEADING_DAY.match(name):
        held.dest = held.dest.with_name(f"{served.isoformat()}_{name}")


def _place(held: files.Part, item: Item, received: Mapping[str, str]) -> None:
    """Point `held` at the name its content calls for; drop it when it is a web page."""
    kind = item.kind
    assert kind is not None
    try:
        head = held.head()
        extension = files.sniff(head)
        if extension in files.PAGES:
            code = item.key.partition(":")[0]
            raise SiteError(f"got a web page instead of {files.NOUNS.get(kind, 'a file')}",
                            hint=NOT_MEDIA_HINT.format(code=code))
        if kind == STEM:
            url = _url(item.file)
            planned = stem_extension("", url)
            # From the planned name, not `held.dest`: a file with mirrors is placed once per
            # mirror, and a name already placed would take its extension twice.
            base = item.dest.name
            if planned and base.lower().endswith(planned):
                base = base[:-len(planned)]
            # The rule (`stem_extension`), then the bytes when they are another known kind.
            found = extension if extension and files.kind_of(f"file{extension}") else None
            held.dest = held.dest.with_name(
                base + (found or stem_extension(received.get("content-type", ""), url)))
        elif kind == files.SNIFF:
            found = files.kind_of(f"file{extension}") if extension else None
            if found is not None:
                held.dest = held.dest.with_name(item.dest.name + extension)
        elif extension is not None and not same_extension(extension, held.dest.suffix):
            held.dest = held.dest.with_suffix(extension)
    except BaseException:
        held.drop()
        raise


async def _together(*works: Awaitable[Any]) -> None:
    """Await `works` at once; the first failure cancels the others, then is raised."""
    tasks = [asyncio.ensure_future(_kept(work)) for work in works]
    try:
        for done in asyncio.as_completed(tasks):
            result = await done
            if isinstance(result, BaseException):
                raise result
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _kept(work: Awaitable[T]) -> T | BaseException:
    """What `work` returned, or what it raised: a task must never raise a Ctrl-C
    (`KeyboardInterrupt`), which would end the loop's thread rather than the task."""
    try:
        return await work
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        return error


async def _all(*works: Awaitable[T]) -> list[T | BaseException]:
    """Await `works` at once, each as a task; what one raised is its result (`_kept`)."""
    if len(works) == 1:
        return [await _kept(works[0])]
    return await asyncio.gather(*(_kept(work) for work in works))


def _raised(results: list[T | BaseException]) -> list[T]:
    """`results`, once none of them is an error; else the first error is raised."""
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results  # type: ignore[return-value]


def _append(held: files.Part, data: bytes) -> None:
    held.write(data)
    held.flush()


def _sources(file: Any) -> list[Any]:
    """A file's mirrors in order: its URL, its mirrors, or its fetch function; empty for none."""
    if file.url:
        return [file.url]
    return list(file.mirrors) if file.mirrors else [file.fetch] if file.fetch else []


def _url(file: Any) -> str:
    """An API 2 file's first URL; "" for a file with no URL."""
    return file.url or (file.mirrors[0] if file.mirrors else "")


def stem_extension(content_type: str, url: str) -> str:
    """The extension an API 2 stem takes (5.3), the engine's and the kit's one rule: what its
    `Content-Type` names when `files.kind_of` knows it, else its URL's when that is a known
    media kind, else none. The bytes then rename a file of another known format as it lands.

    Args:
        content_type: the answer's `Content-Type`; "" when it isn't known yet (a plan).
        url: the file's URL; "" for none.
    """
    found = typed(content_type)
    if found:
        return found
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower() if url else ""
    return suffix if suffix and files.kind_of(f"file{suffix}") else ""


def typed(content_type: str) -> str:
    """The extension of an image, a video or an audio a `Content-Type` names; "" for none."""
    kind = content_type.split(";", 1)[0].strip().lower()
    found = mimetypes.guess_extension(kind) if kind else None
    return found if found and files.kind_of(f"file{found}") else ""


def noon(day: datetime.date) -> float:
    """Noon UTC of `day`, in epoch seconds: the modified time of a file posted that day."""
    moment = datetime.datetime.combine(day, datetime.time(NOON), tzinfo=datetime.UTC)
    return moment.timestamp()


def served_day(value: str | None) -> datetime.date | None:
    """The UTC day of an HTTP `Last-Modified` date; None when missing or malformed."""
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    return when.astimezone(datetime.UTC).date()


def same_extension(one: str, other: str) -> bool:
    """Whether two extensions name one format: `.jpg` and `.jpeg` do."""
    return (SAME_EXTENSIONS.get(one.lower(), one.lower())
            == SAME_EXTENSIONS.get(other.lower(), other.lower()))
