"""The save: a post's files from their parts to their rows, then unpack, sidecar, `Saved`, exec.

src/docs/architecture explains it.
"""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import inspect
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import msgspec

from flnd.engine import home, jobs
from flnd.engine.events import BUSY, Failed, Saved, Skipped
from flnd.engine.home import FILE_COLUMNS, FileRow, _file_values, now
from flnd.engine.jobs import Job, Kind
from flnd.errors import (
    DiskError,
    Error,
    NetworkError,
    NotFound,
    SiteError,
    Stopped,
    UsageError,
    classify,
)
from flnd.source import Fetch, File, Target
from flnd.ui.log import ui
from flnkit import clock, files, media, net, text

__all__ = ["Item", "Saver", "Store"]

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
# The log is checkpointed into the database after this many committed rows, and at close.
CHECKPOINT_ROWS = 1_000
# A sidecar is its file's name with this after it: `9001.jpg.json`.
SIDECAR = ".json"
# A post's rows: today's columns, and the sidecar written before them.
INSERT = (f"INSERT OR REPLACE INTO files ({', '.join(FILE_COLUMNS)}, sidecar)"
          f" VALUES ({', '.join('?' * (len(FILE_COLUMNS) + 1))})")
# `exec`'s placeholders, filled in one pass, so a value holding `{post}` stays literal.
PLACEHOLDER = re.compile(r"\{(path|target|post)\}")


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
    row: FileRow
    # Hashes worked out for saved files of the same size, stored with the post's rows.
    hashes: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class _Failure:
    """A file that failed, for its `failures` row in its post's commit."""

    item: Item
    error: Error


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
# The store: the save's reads and writes of flnd.db


class Store:
    """flnd.db as the save uses it: on the loop, and never waiting for another process's lock.

    Its one connection has `busy_timeout = 0` and `wal_autocheckpoint = 0`: a lock held
    elsewhere is reported at once (`jobs.Busy`), and the log is checkpointed from a thread
    (`checkpoint`). It writes the `files` and `failures` rows of the home's schema, version 1.

    Args:
        path: the home's `flnd.db`, its tables made already.

    Raises:
        DiskError: the file can't be opened.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self._sql = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            # Setting up waits for a lock like any other connection, once, at the run's start.
            self._sql.execute("PRAGMA busy_timeout = 30000")
            self._sql.execute("PRAGMA journal_mode = WAL")
            self._sql.execute("PRAGMA synchronous = NORMAL")
            if sys.platform == "darwin":
                self._sql.execute("PRAGMA fullfsync = ON")
            self._sql.execute("PRAGMA cache_size = -65536")
            self._sql.execute("CREATE INDEX IF NOT EXISTS files_size ON files (size)"
                              " WHERE state = 'saved'")
            self._sql.execute("PRAGMA wal_autocheckpoint = 0")
            self._sql.execute("PRAGMA busy_timeout = 0")
        except sqlite3.Error as error:
            raise DiskError(f"{path}: cannot open: {error}") from error

    def close(self) -> None:
        """Close the connection."""
        self._sql.close()

    def sized(self, size: int, *, besides: str) -> list[tuple[str, str, bytes | None]]:
        """Saved files of `size` bytes (`files_size`), as (key, path, hash), at most a few."""
        return self._run(lambda: self._sql.execute(
            "SELECT key, path, hash FROM files WHERE size = ? AND state = 'saved' AND key != ?"
            " LIMIT ?", (size, besides, CANDIDATES)).fetchall())

    def commit(self, rows: Sequence[FileRow], failures: Sequence[_Failure],
               hashes: Mapping[str, bytes], run: int | None) -> None:
        """Write a post's rows, its files' failures and the hashes found, in one transaction.

        Raises:
            jobs.Busy: another process holds the write lock; nothing was written.
            DiskError: flnd.db refused the write.
        """
        def write() -> None:
            sql = self._sql
            sql.execute("BEGIN IMMEDIATE")
            try:
                sql.executemany(INSERT, [(*_file_values(row), row.sidecar) for row in rows])
                sql.executemany("UPDATE files SET hash = ? WHERE key = ?",
                                [(digest, key) for key, digest in hashes.items()])
                for failure in failures:
                    _fail(sql, failure, run)
                sql.execute("COMMIT")
            except BaseException:
                if sql.in_transaction:
                    sql.execute("ROLLBACK")
                raise
        self._run(write)

    def update(self, key: str, fields: Mapping[str, Any]) -> None:
        """Set what an unpack did to a saved file (`unpacked`, `folder`), in one statement."""
        sets = ", ".join(f"{name} = ?" for name in fields)
        self._run(lambda: self._sql.execute(f"UPDATE files SET {sets} WHERE key = ?",
                                            (*fields.values(), key)))

    def checkpoint(self) -> None:
        """Copy the log into the database without waiting for anyone, on a connection of its
        own: run it on a thread."""
        try:
            sql = sqlite3.connect(self.path, isolation_level=None)
            try:
                sql.execute("PRAGMA wal_checkpoint(PASSIVE)")
            finally:
                sql.close()
        except sqlite3.Error as error:
            ui.debug("checkpoint skipped: {error}", error=error)

    def _run(self, call: Callable[[], T]) -> T:
        """`call()`, its SQLite errors as `jobs.Busy` (the lock is taken) or `DiskError`."""
        try:
            return call()
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise jobs.Busy(f"{self.path}: locked by another process") from error
            raise DiskError(f"{self.path}: {error}") from error


def _fail(sql: sqlite3.Connection, failure: _Failure, run: int | None) -> None:
    """One file's `failures` row; a file the site no longer has is also recorded gone."""
    item, error = failure.item, failure.error
    at = now()
    url = item.file.url if isinstance(item.file.url, str) else ""
    sql.execute(
        "INSERT INTO failures (run, key, target, post_id, category, file, url, class, message,"
        " hint, retryable, attempts, at, cursor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)"
        " ON CONFLICT (key) DO UPDATE SET run = excluded.run, target = excluded.target,"
        " post_id = excluded.post_id, category = excluded.category, file = excluded.file,"
        " url = excluded.url, class = excluded.class, message = excluded.message,"
        " hint = excluded.hint, retryable = excluded.retryable, at = excluded.at,"
        " cursor = excluded.cursor, attempts = CASE WHEN EXISTS (SELECT 1 FROM files"
        " WHERE files.key = excluded.key AND files.state = 'saved') THEN 1"
        " ELSE failures.attempts + 1 END",
        (run, item.key, _target_key(item.target), item.post, item.category, str(item.dest),
         text.redact(url) if url else "", type(error).__name__, str(error), error.hint or "",
         int(error.retryable), at, item.cursor))
    if isinstance(error, NotFound):
        sql.execute(INSERT, (*_file_values(FileRow(
            item.key, _target_key(item.target), str(item.dest), post_id=item.post,
            category=item.category, state="gone", at=at)), None))


def _target_key(target: Target) -> str:
    return f"{target.source}:{target.value}"


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
        emit: where `Saved`, `Skipped` and `Failed` go.
        run: the run's id in `runs`, for failures; None outside a run.
        dedupe: whether a file whose bytes are saved already becomes a hard link to them.
        sidecars: whether a file with `meta` gets `<name>.json`.
        exec: the program and arguments to run after each saved file, read only from the
            user's own config; None runs nothing.
        stop: the run's second Ctrl-C, which a plugin's `fetch` function stops at when it
            next reports its progress; None never stops it.
        tally: the run's live counts (`flnd.engine.job.Tally`); None counts nothing.
        sample: keeps a scrubbed sample of a `Changed` answer, and points its hint at it.
    """

    def __init__(self, store: Store, scheduler: jobs.Scheduler, emit: Callable[[Any], None], *,
                 run: int | None = None, dedupe: bool = True, sidecars: bool = True,
                 exec: Sequence[str] | None = None, stop: threading.Event | None = None,
                 tally: Any = None, sample: Callable[[Error], None] | None = None) -> None:
        if exec is not None and (not isinstance(exec, (list, tuple)) or not exec
                                 or not all(isinstance(arg, str) for arg in exec)):
            raise UsageError("exec must be a list: the program, then its arguments",
                             hint='exec = ["echo", "{path}"]')
        self.store, self.scheduler, self.emit, self.run = store, scheduler, emit, run
        self.dedupe, self.sidecars, self.exec, self.stop = dedupe, sidecars, exec, stop
        self.tally, self.sample = tally, sample
        self.done = _Done()
        self._rows = 0
        # The pairs of devices (saved file's, new file's) found unable to hard-link.
        self._unlinkable: set[tuple[int, int]] = set()
        self._checkpoints: set[asyncio.Task[Any]] = set()
        # Each target's last file to join the scheduler's queue (`_turn`).
        self._turns: dict[Target, asyncio.Event] = {}

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
        await self._call(self.store.checkpoint)
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
            self.emit(Skipped(item.target, item.category, item.post, item.dest, reason=BUSY))
            ui.info("{name}: being saved by another run", name=item.label or item.dest.name)
            return None
        except Error as error:
            if jobs.stops(error):
                raise
            return self._failed(item, error, flow)
        except Exception as error:
            wrapped = classify(error)
            if jobs.stops(wrapped):
                raise wrapped from error
            return self._failed(item, wrapped, flow)
        if landed is None:
            self._count(item, **({"downloading": -1} if flow.started else {"queued": -1}))
        return landed

    def _failed(self, item: Item, error: Error, flow: _Flow) -> _Failure:
        if self.sample is not None:
            self.sample(error)
        self._count(item, failed=1, **({"downloading": -1} if flow.started else {"queued": -1}))
        self.emit(Failed(item.target, error, post=item.post, path=item.dest))
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
        if file.url is None:
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
            if fresh is None or fresh.url is None or fresh.content is not None:
                raise
            saved = await self._transfer(item, fresh, client, plugin, received, flow)
        dated = item.day or served_day(received.get("last-modified"))
        row = FileRow(
            item.key, _target_key(item.target), str(saved.path), saved.size, saved.mtime,
            post_id=item.post, category=item.category,
            date=item.day.isoformat() if item.day else None, estimated=item.estimated,
            kind=files.kind_of(saved.path.name), at=now(), files=item.count, hash=saved.hash)
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
        mirrors: list[str | Fetch] = (
            [file.url] if isinstance(file.url, str) or callable(file.url) else list(file.url))
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
                    await self._call(_place, held, item.kind)
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
                            answered = await self._mirror(item, mirror, held, client, meter)
                        received.update(answered)
                        if item.kind is not None and number < len(mirrors):
                            await self._call(_place, held, item.kind)
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
        finally:
            mine.set()

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
                      client: net.Client, meter: net.Meter) -> Mapping[str, str]:
        """One mirror into the part: a URL by the client, or a plugin's `fetch` function."""
        if isinstance(mirror, str):
            return await client.fill(mirror, held, headers=item.headers, progress=meter)
        # A fetch that resumes keeps its offsets in `<part>.json`, which goes when it lands.
        held.side = True

        def progress(*_: Any) -> None:
            if self.stop is not None and self.stop.is_set():
                raise KeyboardInterrupt

        if inspect.iscoroutinefunction(mirror):
            await mirror(held.path, progress)
            return {}
        outcome = await self.scheduler.on(Kind.CALL, mirror, held.path, progress)
        if inspect.isawaitable(outcome):
            await outcome
        return {}

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
        found = self.store.sized(row.size, besides=row.key)
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
                self._unlinkable.add(pair)
                ui.warn("{folder} can't hard-link to {other}, so the same file saved twice is"
                        " kept twice", folder=path.parent, other=twin.parent)
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
        await self._write(self.store.commit, rows, failures, hashes, self.run)
        self._rows += len(rows)
        if self._rows >= CHECKPOINT_ROWS:
            self._rows = 0
            task = asyncio.ensure_future(self._call(self.store.checkpoint))
            self._checkpoints.add(task)
            task.add_done_callback(self._checkpoints.discard)

    async def _write(self, call: Callable[..., Any], *args: Any) -> None:
        """`call(*args)` on the store, on the loop, as a `WRITE` job."""
        async def work() -> None:
            call(*args)
        await self.scheduler.run(Job(kind=Kind.WRITE, work=work))

    # -- stages 6 to 9: unpack, `Saved`, exec; the sidecar went before the commit -----------

    async def _finish(self, landed: _Landed, plugin: str) -> None:
        item, path = landed.item, landed.path
        if item.file.password is not None or files.is_archive(path):
            await self._write(self.store.update, item.key, await self._unpack(item, path, plugin))
        self._count(item, downloading=-1, saved=1, bytes=landed.row.size)
        self.emit(Saved(item.target, item.category, item.post, path, landed.day,
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


@contextmanager
def attached(settings: Any) -> Iterator[Saver | None]:
    """Give a run's settings their save for the block (`settings.saver`), then close it.

    The save reads `dedupe` and `sidecars` from the home's `config.toml`, and `exec` from the
    user's (`config.program`, the one file it is read from); it writes through a
    store of its own on the home's `flnd.db`, and runs its jobs on the run's scheduler. It is
    closed on the run's loop before the loop and the database close. A run with no database,
    or a dry one, saves nothing.

    Args:
        settings: the run's `RunShared`, inside `site.shared_browser`.

    Yields:
        The save; None when the run saves nothing.

    Raises:
        UsageError: the home's `config.toml` is broken.
    """
    from flnd.engine import site
    from flnd.engine.config import program, user_file
    from flnd.engine.options import config

    scheduler = site.scheduler(settings)
    if settings.db is None or settings.dry or scheduler is None:
        yield None
        return
    found = config(settings.home)
    argv = program(user_file())
    store = Store(settings.db.ready())
    saver = Saver(store, scheduler, settings.emit, run=settings.run, dedupe=found.dedupe,
                  sidecars=found.sidecars, exec=argv or None, stop=settings.stop.now,
                  tally=settings.tally, sample=lambda error: sampled(settings, error))
    settings.saver = saver
    try:
        yield saver
    finally:
        settings.saver = None
        try:
            settings.browser_pool.loop.run(saver.close())
        finally:
            store.close()


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


def _place(held: files.Part, kind: str) -> None:
    """Point `held` at the name its content calls for; drop it when it is a web page."""
    try:
        head = held.head()
        extension = files.sniff(head)
        if extension in files.PAGES:
            raise SiteError(f"got a web page instead of {files.NOUNS.get(kind, 'a file')}",
                            hint=NOT_MEDIA_HINT)
        if kind == files.SNIFF:
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
