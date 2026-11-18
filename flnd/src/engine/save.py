"""The save: a post's files from their parts to their rows, then unpack, sidecar, `Saved`, exec.

src/docs/architecture explains it.
"""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar
from urllib.parse import urlsplit

import msgspec

from flnd.engine import home, jobs, store
from flnd.engine.events import BUSY, Failed, Saved, Skipped
from flnd.engine.jobs import Job, Kind
from flnd.engine.run import sink
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
NOT_MEDIA_HINT = "it is usually a login wall: run flnd login <source>, then try again"
# A lazy post's link that answers one of these has expired: its post is asked for a fresh one.
EXPIRED = frozenset({403, 410})
# How many saved files of the same size dedupe hashes at most, to compare with a new one.
CANDIDATES = 8
# A sidecar is its file's name with this after it: `9001.jpg.json`.
SIDECAR = ".json"
# `exec`'s placeholders, filled in one pass, so a value holding `{post}` stays literal.
PLACEHOLDER = re.compile(r"\{(path|target|post)\}")
# The kind of an API 2 file named by a stem (5.3): its extension is what its bytes are, else
# what its `Content-Type` names, else its URL's, else none; the planned name may hold the URL's.
STEM = "stem"


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
        account: the account it is fetched with; None for the plugin's default.
        count: how many files the post has, on its first file's row only; None otherwise.
        estimated: whether `day` was taken from the posts around it.
        meta: its sidecar's fields; None writes no sidecar.
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
    account: str | None = None
    count: int | None = None
    estimated: bool = False
    meta: Mapping[str, Any] | None = None
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
    # Hashes worked out for saved files of the same size, stored with the post's rows.
    hashes: dict[str, bytes] = field(default_factory=dict)


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
    """One file's segments while it transfers: the plan, and a task per segment.

    Args:
        saver: the run's save.
        item: the file.
        urls: its mirrors; the first is the one that answered first.
        held: its part.
        client: the plugin's downloading client.
        job: the first transfer's `FILE` job, whose host and target the segments take.
    """

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
        # Whether a sidecar of the part's segments is on disk.
        self._kept = False

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
        """Wait until the sidecar holds `meta` as it is now. One task writes it, a snapshot at
        a time, taken on the loop: a check that comes while it writes is in its next one."""
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
    """A `ranged` fetch file's segments while it transfers (`Saver._fetched`).

    The file's own `FILE` job takes the segments one at a time (`take`); before each, while
    the host's cap has a free place and nobody waits for it (`Scheduler.room`), one more `FILE`
    job starts and takes segments too. A segment is taken when its transfer starts, so a job
    still waiting for a place holds none, and the file's job waits for no other: the others
    are waited for past it (`finish`). The part's sidecar keeps each segment, written by one
    task after each `net.BUFFER` bytes and as a segment ends, so a later run resumes each.

    Args:
        saver: the run's save.
        item: the file.
        fetch: the plugin's `fetch`.
        held: its part.
        job: the file's own `FILE` job, whose host and target the others take.
        plan: its segments.
    """

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
    linked: int = 0


class Saver:
    """A run's saves: each post's files through the nine stages, their rows one commit a post.

    Every step is a job of the run's scheduler: a file's transfer a `FILE` job (with its
    mirrors), a DASH join a `MERGE` job, a hash a `HASH` job, an unpack an `UNPACK` job, a
    program an `EXEC` job and a post's commit a `WRITE` job; blocking calls go to the threads
    of their kind (`Scheduler.on`). Used on the run's loop only.

    Args:
        store: the run's flnd.db.
        scheduler: the run's scheduler.
        emit: where `Saved`, `Skipped` and `Failed` go; what it gives is awaited.
        run: the run's id in `runs`, for failures; None outside a run.
        dedupe: whether a file whose bytes are saved already becomes a hard link to them.
        sidecars: whether a file with `meta` gets `<name>.json`.
        exec: the program and arguments to run after each saved file, read only from the
            user's own config; None runs nothing.
        stop: the run's second Ctrl-C, which a plugin's `fetch` function stops at when it
            next reports its progress; None never stops it.
        tally: the run's live counts (`flnd.engine.run.Tally`); None counts nothing.
        sample: keeps a scrubbed sample of a `Changed` answer, and points its hint at it.
    """

    def __init__(self, db: store.Store, scheduler: jobs.Scheduler,
                 emit: Callable[[Any], Awaitable[None]], *,
                 run: int | None = None, dedupe: bool = True, sidecars: bool = True,
                 exec: Sequence[str] | None = None, stop: threading.Event | None = None,
                 tally: Any = None, sample: Callable[[Error], None] | None = None) -> None:
        if exec is not None and (not isinstance(exec, (list, tuple)) or not exec
                                 or not all(isinstance(arg, str) for arg in exec)):
            raise UsageError("exec must be a list: the program, then its arguments",
                             hint='exec = ["echo", "{path}"]')
        self.store, self.scheduler, self.run = db, scheduler, run
        self.emit = sink(emit)
        self.dedupe, self.sidecars, self.exec, self.stop = dedupe, sidecars, exec, stop
        self.tally, self.sample = tally, sample
        self.done = _Done()
        # The pairs of devices (saved file's, new file's) found unable to hard-link.
        self._unlinkable: set[tuple[int, int]] = set()
        self._checkpoints: set[asyncio.Task[Any]] = set()
        # Each target's last file to join the scheduler's queue (`_turn`).
        self._turns: dict[Target, asyncio.Event] = {}
        # Each ranged fetch file's segments, by its first transfer's job (`_ranged`).
        self._fetching: dict[Job[Any], _Fetching] = {}

    async def post(self, items: Sequence[Item], *, client: net.Client,
                   plugin: str) -> dict[str, Path]:
        """Save one post's files at once, commit their rows together, then finish each.

        A file that fails or is busy is reported, and the others go on.

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
        if done.linked:
            parts.append(f"{text.count(done.linked, 'file')} already saved elsewhere, linked")
        if parts:
            (ui.warn if done.unpack_failed else ui.ok)("Media: {line}", line="; ".join(parts))

    # -- stages 1 to 4: the part, the transfer, the content check, dedupe ---------------------

    async def _file(self, item: Item, client: net.Client,
                    plugin: str) -> _Landed | _Failure | None:
        """One file into place and its row made; None when it was dropped, busy or had no
        URL."""
        flow = _Flow()
        try:
            landed = await self._land(item, client, plugin, flow)
        except _Dropped:
            self._count(item, queued=-1)
            return None
        except files.BusyError:
            self._count(item, downloading=-1, skipped=1)
            await self.emit(Skipped(item.target, item.category, item.post, item.dest,
                                    reason=BUSY))
            ui.info("{name}: being saved by another run", name=item.label or item.dest.name)
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
        """A transfer of `item` starts: drop it after the first Ctrl-C, else count it.

        Raises:
            _Dropped: it hadn't started, and is dropped.
        """
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
        dated = item.day or served_day(received.get("last-modified"))
        row = store.FileRow(
            item.key, store.key(item.target), str(saved.path), saved.size, saved.mtime,
            post_id=item.post, category=item.category,
            date=item.day.isoformat() if item.day else None, estimated=item.estimated,
            kind=files.kind_of(saved.path.name), at=store.now(), files=item.count,
            hash=saved.hash)
        landed = _Landed(item, saved.path, dated, row)
        if self.dedupe:
            await self._dedupe(landed, plugin)
        if self.sidecars and item.meta is not None:
            # Written before the post's commit, so its row names it in the same transaction.
            side = saved.path.with_name(saved.path.name + SIDECAR)
            body = msgspec.json.format(msgspec.json.encode(item.meta, enc_hook=str), indent=2)
            try:
                await self._call(files.write, side, body + b"\n")
                row.sidecar = str(side)
            except DiskError as error:
                ui.warn("{name}: no sidecar: {error}", name=saved.path.name, error=error)
        return landed

    async def _transfer(self, item: Item, file: File, client: net.Client, plugin: str,
                        received: dict[str, str], flow: _Flow) -> files.Saved:
        """Fill the file's part from its mirrors in order, check it and land it.

        A DASH pair (`File.audio`) is the first mirror; when it fails, the other mirrors are
        tried as plain files. Everything else opens its part on a thread, then fills it from its
        mirrors as one `FILE` job (`_mirrors`), then is checked and lands on a thread.
        """
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
                if held.mtime is None:
                    served = served_day(received.get("last-modified"))
                    held.mtime = noon(served) if served is not None else None
        finally:
            # A file that never queued (a busy part) lets the next one go on.
            turn[1].set()
        assert held.saved is not None
        return held.saved

    def _turn(self, target: Target) -> tuple[asyncio.Event | None, asyncio.Event]:
        """The file's turn among its target's: the one before it, and its own. A part opens on
        a thread, and threads finish in any order; a file joins the scheduler's queue only after
        the one listed before it did, so the target's places go in the order of the listing."""
        mine = asyncio.Event()
        before = self._turns.get(target)
        self._turns[target] = mine
        return before, mine

    async def _mirrors(self, item: Item, mirrors: Sequence[str | Fetch], held: files.Part,
                       client: net.Client, plugin: str, received: dict[str, str], flow: _Flow,
                       turn: tuple[asyncio.Event | None, asyncio.Event]) -> None:
        """Fill the open part from each mirror in turn, as one `FILE` job, which the scheduler
        tries again as its policy says. Each mirror but the last is checked in the job, so a
        web page there moves on to the next."""
        job = self._file_job(item, plugin, mirrors[0], _nothing)
        sink = _Bytes(job, self.scheduler.slowest)
        # One meter for every try, so a try that resumes counts only what it adds; its writes go
        # to the thread of the device the file is on.
        meter = self._meter(sink, item.dest)

        async def work() -> None:
            self._start(item, flow)
            sink.flight, sink.speed = self._fly(item), jobs.Speed()
            try:
                for number, mirror in enumerate(mirrors, start=1):
                    try:
                        if number == 1 and isinstance(mirror, str):
                            urls = [one for one in mirrors if isinstance(one, str)]
                            answered = await self._segmented(item, urls, held, client, meter,
                                                             job)
                        else:
                            answered = await self._mirror(item, mirror, held, client, meter,
                                                          job)
                        received.update(answered)
                        if item.kind is not None and number < len(mirrors):
                            await self._call(_place, held, item, received)
                        return
                    except Exception as error:
                        if number == len(mirrors) or _final(error):
                            raise
                        ui.warn("mirror {n} failed: {error}; trying the next", n=number,
                                error=error)
                        # The next mirror may serve other bytes: it starts from an empty part.
                        await self._call(held.reset)
            except Exception as error:
                if isinstance(error, Error):
                    raise
                # A plugin's `fetch` function raises what it meets (a dropped connection):
                # the scheduler's policy reads it as flnd's error for it.
                raise classify(error) from error
            finally:
                if sink.flight is not None:
                    sink.flight.land()

        job.work = work
        before, mine = turn
        try:
            if before is not None:
                await before.wait()
            running = self.scheduler.run(job)
            # Set before the job queues, which it does at once, ahead of the next file's.
            mine.set()
            await running
            fetching = self._fetching.get(job)
            if fetching is not None:
                # The other segments' jobs, waited for here, past this file's own job, which
                # holds no place of its host meanwhile.
                await fetching.finish()
        finally:
            mine.set()
            fetching = self._fetching.pop(job, None)
            if fetching is not None:
                await fetching.cancel()

    async def _segmented(self, item: Item, urls: Sequence[str], held: files.Part,
                         client: net.Client, meter: net.Meter,
                         job: Job[Any]) -> Mapping[str, str]:
        """The first URL into the part, split into segments while its host has room (6.2).

        A file of `SPLIT_FILE` bytes or more, from a host that takes ranges, may take more
        places: from `SPLIT_AFTER` s into its transfer, at each piece written (at most each
        `TICK` s) and at once when a segment ends, the largest range left of `SPLIT_RANGE`
        bytes or more is halved while the host's cap has a free place and no other file waits
        for it, up to `SEGMENTS`. The first transfer stops where its range now ends (`until`);
        each new segment is a `FILE` job of its own, written at its offset into the one part.
        Where each stands goes into the part's sidecar after every piece, so a later try or run
        resumes each segment (`_resume`).

        Returns:
            The first answer's headers.
        """
        plan = jobs.Segments.of(held.meta) if held.size and held.meta.get("segments") else None
        if plan is not None:
            try:
                return await self._resume(item, urls, held, client, plan, job)
            except _Mismatch:
                # Another file under the same name now: it comes whole.
                await self._call(held.reset)
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
            answered = await client.fill(urls[0], held, headers=item.headers, progress=meter,
                                         until=split.end)
            await split.finish()
        except BaseException:
            await split.cancel()
            raise
        finally:
            meter.opened = meter.wrote = None
        if split.plan is not None and len(split.plan.parts) > 1:
            held.note(segments=None, size=None, last_modified=None)
        return answered

    async def _resume(self, item: Item, urls: Sequence[str], held: files.Part,
                      client: net.Client, plan: jobs.Segments, job: Job[Any]) -> Mapping[str, str]:
        """Go on with each segment a part's sidecar kept, every one a `FILE` job of its own.

        Raises:
            _Mismatch: the first URL now serves another file (its length or `ETag`).
        """
        split = _Split(self, item, urls, held, client, job)
        split.plan = plan
        try:
            for part in plan.parts:
                if part.left:
                    split.start(part)
            await split.finish()
        except BaseException:
            await split.cancel()
            raise
        held.note(segments=None, size=None, last_modified=None)
        return {"etag": plan.etag, "last-modified": plan.last_modified} if plan.last_modified \
            else {"etag": plan.etag}

    async def _mirror(self, item: Item, mirror: str | Fetch, held: files.Part,
                      client: net.Client, meter: net.Meter, job: Job[Any]) -> Mapping[str, str]:
        """One mirror into the part: a URL by the client, or a plugin's `fetch` function."""
        if isinstance(mirror, str):
            return await client.fill(mirror, held, headers=item.headers, progress=meter)
        await self._fetched(item, mirror, held, meter, job)
        return {}

    async def _fetched(self, item: Item, fetch: Fetch, held: files.Part,
                       meter: net.Meter, job: Job[Any]) -> None:
        """A plugin's `fetch` into the part, as `flnd.plugin.Part`s.

        A file that isn't `ranged`, or whose size is unknown or under `jobs.SPLIT_FILE`, is one
        part, from what the part holds already (a resumed try or run). So is a `ranged` one
        when its host has no free place as it starts. Otherwise it is cut into up to
        `jobs.FETCH_SEGMENTS` even segments (`_Fetching`): this transfer takes them one by one,
        and more `FILE` jobs take the others while the host has room. A running `Part`'s end
        can't move, so a segment is never split once it began.

        Raises:
            NetworkError: the fetch stopped short of the size it said.
        """
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
                            kind=Kind.MERGE, work=join, plugin=plugin, account=item.account,
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
        return Job(kind=Kind.FILE, work=work, plugin=plugin, account=item.account,
                   host=host or plugin, target=str(item.target), size=item.file.size,
                   path=item.dest, priority=0 if item.retried else None)

    def _meter(self, sink: _Bytes, dest: Path, *, at: int = 0) -> net.Meter:
        """A transfer's meter: its writes on the thread of `dest`'s device, each chunk under
        the run's `rate` when it has one; `at` is where a segment starts."""
        meter = net.Meter(sink, write=self._writer(dest), at=at)
        if self.scheduler.rate.rate:
            meter.pace = self.scheduler.rate.take
        return meter

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

        Raises:
            BusyError: another run holds the part: the file's, never the run's stop.
        """
        manager = files.part(dest, resume=True)
        held = await self._enter(manager)
        if held is None:
            raise files.BusyError(f"{dest.name}: being saved by another run")
        try:
            yield held
        except BaseException as error:
            # The part keeps its bytes for the next run, and raises `error` again.
            await self._call(manager.__exit__, type(error), error, error.__traceback__)
            raise
        await self._call(manager.__exit__, None, None, None)

    async def _enter(self, manager: Any) -> files.Part | None:
        """Open a part on a thread; None when another run holds it. A cancel waits for the
        open, then leaves what it opened, so no lock outlives the save."""
        opened: list[files.Part] = []

        def enter() -> files.Part | None:
            try:
                held = manager.__enter__()
            except files.BusyError:
                return None
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
            return await self._call(enter)
        except asyncio.CancelledError as error:
            if opened:
                await self._call(manager.__exit__, type(error), error, None)
            raise

    async def _dedupe(self, landed: _Landed, plugin: str) -> None:
        """Link the file to a saved one with the same bytes, among those of its size."""
        row = landed.row
        # One indexed read on the loop, on the store's one connection, which no thread shares.
        found = [(twin.key, twin.path, twin.hash) for twin in
                 self.store.twins(besides=row.key, size=row.size, limit=CANDIDATES)]
        if not found:
            return
        path = Path(row.path)
        unhashed = [(key, Path(where)) for key, where, digest in found if digest is None]
        mine = [] if row.hash is not None else [self._hash(path, row.size, plugin)]
        digests = _raised(await _all(*mine, *(self._hash(where, row.size, plugin)
                                              for _, where in unhashed)))
        if mine:
            row.hash = digests.pop(0)
        for (key, _), digest in zip(unhashed, digests, strict=True):
            if digest is not None:
                landed.hashes[key] = digest
        hashes = {key: digest for key, _, digest in found if digest is not None}
        hashes.update(landed.hashes)
        twins = [Path(where) for key, where, _ in found
                 if row.hash is not None and hashes.get(key) == row.hash]
        for twin in twins:
            if twin == path:
                continue
            mtime, pair = await self._call(_link, twin, path, row.size,
                                           frozenset(self._unlinkable))
            if mtime == _NO_LINKS:
                # Two targets' files may meet the same pair at once: one warning for it.
                if pair not in self._unlinkable:
                    self._unlinkable.add(pair)
                    ui.warn("{folder} can't hard-link to {other}, so the same file saved twice"
                            " is kept twice", folder=path.parent, other=twin.parent)
                return
            if mtime is not None:
                # One file now, with the saved one's modified time.
                row.mtime = mtime
                self.done.linked += 1
                return

    async def _hash(self, path: Path, size: int, plugin: str) -> bytes | None:
        """The SHA-256 of a file on a hasher thread; None when it isn't on disk at `size`."""
        async def work() -> bytes | None:
            return await self.scheduler.on(Kind.HASH, _digest, path, size)
        return await self.scheduler.run(Job(kind=Kind.HASH, work=work, plugin=plugin, size=size))

    # -- stage 5: the commit ------------------------------------------------------------------

    async def _commit(self, landed: Sequence[_Landed], failures: Sequence[_Failure]) -> None:
        """A post's rows in one transaction on the loop, as a `WRITE` job: a lock held
        elsewhere puts it back in the scheduler's queue (`jobs.Busy`), so the loop never
        stalls."""
        rows = [one.row for one in landed]
        hashes = {key: digest for one in landed for key, digest in one.hashes.items()}
        failed = [_failed_row(one) for one in failures]
        await self._write(self.store.commit, rows, hashes=hashes, failures=failed, run=self.run)
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

    # -- stages 6 to 9: unpack, `Saved`, exec; the sidecar went before the commit -----------

    async def _finish(self, landed: _Landed, plugin: str) -> None:
        item, path = landed.item, landed.path
        if item.file.password is not None or files.is_archive(path):
            await self._write(self.store.update, item.key,
                              **await self._unpack(item, path, plugin))
        self._count(item, downloading=-1, saved=1, bytes=landed.row.size)
        await self.emit(Saved(item.target, item.category, item.post, path, landed.day,
                              landed.row.size, retried=item.retried))
        if self.exec:
            argv = self.exec

            async def work() -> None:
                await self.scheduler.on(Kind.EXEC, _exec, argv, path, item.target, item.post)
            await self.scheduler.run(Job(kind=Kind.EXEC, work=work, plugin=plugin,
                                         target=str(item.target)))

    async def _unpack(self, item: Item, path: Path, plugin: str) -> dict[str, Any]:
        """Extract an archive beside itself on the unpack lane; a failure keeps it, and warns."""
        async def work() -> Path:
            return await self.scheduler.on(Kind.UNPACK, _extract, path, item.file.password)
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
        return {"unpacked": "extracted", "folder": str(folder)}

    async def _call(self, call: Callable[..., T], *args: Any, **options: Any) -> T:
        """`call(*args)` on the scheduler's `CALL` threads, waited for to its end."""
        if options:
            return await self.scheduler.on(Kind.CALL, lambda: call(*args, **options))
        return await self.scheduler.on(Kind.CALL, call, *args)


@asynccontextmanager
async def attached(settings: Any) -> AsyncIterator[Saver | None]:
    """Give a run's settings their save for the block (`settings.saver`), then close it.

    The save reads `dedupe` and `sidecars` from the home's `config.toml`, and `exec` from the
    user's (`config.program`, the one file it is read from); it writes through a
    store of its own on the home's `flnd.db`, and runs its jobs on the run's scheduler. It is
    closed on the run's loop before the loop and the database close. A run with no database,
    or a dry one, saves nothing.

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
    found, argv, db = await asyncio.to_thread(_opened, settings)
    saver = Saver(db, scheduler, settings.emit, run=settings.run, dedupe=found.dedupe,
                  sidecars=found.sidecars, exec=argv or None, stop=settings.stop.now,
                  tally=settings.tally, sample=lambda error: sampled(settings, error))
    settings.saver = saver
    try:
        yield saver
    finally:
        settings.saver = None
        try:
            await saver.close()
        finally:
            await asyncio.to_thread(db.close)


def _opened(settings: Any) -> tuple[Any, Any, store.Store]:
    """`attached`'s reads, off the loop: the home's config, the user's `exec`, and the run's
    commit connection (`loop=True`: it waits for no lock), made now; the config modules are
    imported here too, since an import reads files."""
    from flnd.engine.config import program, user_file
    from flnd.engine.options import config

    db = store.open(settings.home, loop=True)
    try:
        db.ready()
    except BaseException:
        db.close()
        raise
    return config(settings.home), program(user_file()), db


def sampled(settings: Any, error: BaseException) -> None:
    """Keep a `Changed` error's answer as a scrubbed sample, and point its hint at it.

    Called before the failure is written and its `Failed` event sent, so both name the sample.

    Args:
        settings: the run's `RunShared`: its home and run id.
        error: what failed; anything but a `Changed` with a response is left as it is.
    """
    path = home.sample(settings.home, settings.run, error)
    if path is not None and isinstance(error, Error):
        saved = f"the answer is saved, scrubbed, in {path}"
        error.hint = f"{error.hint}; {saved}" if error.hint else saved


# ---------------------------------------------------------------------------------------------
# The blocking steps, each run on a thread


# `_link`'s answer when the file system can't hard-link at all.
_NO_LINKS = -1


async def _nothing() -> None:
    """A job's work until its own is set."""


def _final(error: BaseException) -> bool:
    """Whether a mirror's failure ends the file at once: it stops the run, or it is a 429,
    which the scheduler waits out on the same mirror."""
    return jobs.stops(error) or getattr(error, "status", None) == 429


def _link(twin: Path, path: Path, size: int,
          unlinkable: frozenset[tuple[int, int]]) -> tuple[int | None, tuple[int, int]]:
    """Make `path` a hard link to `twin` when `twin` is still that size.

    Returns:
        `twin`'s mtime (ns) once linked, `_NO_LINKS` when the two devices can't link, or None
        when it isn't there, the pair is known not to link, or the link was refused; and the
        pair of devices.
    """
    try:
        found = twin.lstat()
        pair = (found.st_dev, path.stat().st_dev)
    except OSError:
        return None, (0, 0)
    if found.st_size != size or not stat.S_ISREG(found.st_mode):
        return None, pair
    if pair in unlinkable:
        return None, pair
    try:
        linked = files.link(twin, path)
    except DiskError as error:
        ui.debug("{name}: not linked: {error}", name=path.name, error=error)
        return None, pair
    return (found.st_mtime_ns if linked else _NO_LINKS), pair


def _digest(path: Path, size: int) -> bytes | None:
    try:
        if path.stat().st_size != size:
            return None
        return files.digest(path)
    except (OSError, DiskError):
        return None


def _extract(path: Path, password: str | None) -> Path:
    return files.extract(path, password=password)


def _exec(argv: Sequence[str], path: Path, target: Target, post: str) -> None:
    """Run the user's program for a saved file, with no shell; a failure is a warning."""
    values = {"path": str(path), "target": str(target), "post": post}
    filled = [PLACEHOLDER.sub(lambda found: values[found[1]], arg) for arg in argv]
    env = {**os.environ, "FLND_PATH": values["path"], "FLND_TARGET": values["target"],
           "FLND_POST": post}
    try:
        done = subprocess.run(filled, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              check=False)
    except OSError as error:
        ui.warn("exec {program}: {error}", program=argv[0], error=error)
        return
    if done.returncode:
        said = done.stderr.decode("utf-8", "replace").strip().splitlines()
        ui.warn("exec {program}: exit {code} for {name}{said}", program=argv[0],
                code=done.returncode, name=path.name, said=f": {said[-1]}" if said else "")


def _place(held: files.Part, item: Item, received: Mapping[str, str]) -> None:
    """Point `held` at the name its content calls for; drop it when it is a web page.

    An API 2 stem (`STEM`) takes what its bytes are, else what its `Content-Type` names, else
    its URL's extension, else none (5.3).
    """
    kind = item.kind
    assert kind is not None
    try:
        head = held.head()
        extension = files.sniff(head)
        if extension in files.PAGES:
            raise SiteError(f"got a web page instead of {files.NOUNS.get(kind, 'a file')}",
                            hint=NOT_MEDIA_HINT)
        if kind == STEM:
            url = _url(item.file)
            planned = stem_extension("", url)
            base = held.dest.name
            if planned and base.lower().endswith(planned):
                base = base[:-len(planned)]
            # The rule (`stem_extension`), then the bytes when they are another known kind.
            found = extension if extension and files.kind_of(f"file{extension}") else None
            held.dest = held.dest.with_name(
                base + (found or stem_extension(received.get("content-type", ""), url)))
        elif kind == files.SNIFF:
            found = files.kind_of(f"file{extension}") if extension else None
            if found is not None:
                held.dest = held.dest.with_name(held.dest.name + extension)
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
    """Await `works` at once, each as a task; what one raised is its result (`_kept`). One work
    alone is awaited here, with no task."""
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
    """A file's mirrors in order: its URL, its mirrors, or its fetch function; empty for
    none."""
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
