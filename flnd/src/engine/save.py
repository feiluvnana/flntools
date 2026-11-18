"""The save: a post's files from their parts to their rows, then unpack, sidecar, `Saved`, exec.

src/docs/architecture explains it.
"""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import enum
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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

import msgspec

from flnd.engine.events import Failed, Saved, Skipped
from flnd.engine.home import FILE_COLUMNS, FileRow, _file_values, now
from flnd.errors import DiskError, Error, NotFound, SiteError, UsageError, classify
from flnd.source import Fetch, File, Target
from flnd.ui.log import ui
from flnkit import clock, files, media, text

__all__ = ["Item", "Job", "Kind", "Saver", "Scheduler", "Store"]

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
# A commit that finds the write lock taken goes again after this many seconds, for up to
# `BUSY_FOR`: a run never waits on the loop, and a lock held this long is a stuck process.
BUSY_PAUSE, BUSY_FOR = 0.02, 30.0
# The log is checkpointed into the database after this many committed rows, and at close.
CHECKPOINT_ROWS = 1_000
# A sidecar is its file's name with this after it: `9001.jpg.json`.
SIDECAR = ".json"
# Priorities: a retried failure first, then files, then the steps of a saved file.
RETRIED, FILE_PRIORITY, STEP = 0, 2, 3
# A post's rows: today's columns, and the sidecar written before them.
INSERT = (f"INSERT OR REPLACE INTO files ({', '.join(FILE_COLUMNS)}, sidecar)"
          f" VALUES ({', '.join('?' * (len(FILE_COLUMNS) + 1))})")
# `exec`'s placeholders, filled in one pass, so a value holding `{post}` stays literal.
PLACEHOLDER = re.compile(r"\{(path|target|post)\}")


# ---------------------------------------------------------------------------------------------
# What the scheduler runs


class Kind(enum.StrEnum):
    """What a job waits for: the scheduler's pools, by name."""

    PAGE = "page"
    FILE = "file"
    TAB = "tab"
    CALL = "call"
    HASH = "hash"
    UNPACK = "unpack"
    MERGE = "merge"
    EXEC = "exec"
    WRITE = "write"


@dataclass(kw_only=True)
class Job:
    """One unit of work for the scheduler: what it waits for, and the coroutine it runs.

    A blocking step (a file-system call, a hash, an unpack, exec) is a coroutine that runs it
    on a thread, so the scheduler only decides when it starts and how many run at once.
    """

    kind: Kind
    work: Callable[[], Awaitable[Any]]
    plugin: str = ""
    account: str | None = None
    bucket: str = ""
    host: str = ""
    target: str | None = None
    priority: int = STEP
    size: int | None = None


class Scheduler(Protocol):
    """Runs a job once its pools have room, under the run's one retry policy."""

    async def run(self, job: Job) -> Any:
        """Wait for the job's pools, run its work (trying again as the policy says), and return
        what it returned."""
        ...


class Fills(Protocol):
    """What streams a URL into a part: a plugin's `flnkit.net.Client`."""

    async def fill(self, url: str, held: files.Part, *,
                   headers: Mapping[str, str] | None = None) -> Mapping[str, str]:
        """Stream `url` into `held`, going on from the bytes it holds; returns the headers."""
        ...


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


# ---------------------------------------------------------------------------------------------
# The store: the save's reads and writes of flnd.db


class _Busy(Exception):
    """Another process holds flnd.db's write lock (`SQLITE_BUSY`)."""


class Store:
    """flnd.db as the save uses it: on the loop, and never waiting for another process's lock.

    Its one connection has `busy_timeout = 0` and `wal_autocheckpoint = 0`: a lock held
    elsewhere is reported at once, and the log is checkpointed from a thread (`checkpoint`).
    It writes the `files` and `failures` rows of the home's schema, version 1.

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
        """Write a post's rows, its files' failures and the hashes found, in one transaction."""
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
        """`call()`, its SQLite errors as `_Busy` (the lock is taken) or `DiskError`."""
        try:
            return call()
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise _Busy(str(error)) from error
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


class Saver:
    """A run's saves: each post's files through the nine stages, their rows one commit a post.

    Args:
        store: the run's flnd.db.
        jobs: the run's scheduler.
        emit: where `Saved`, `Skipped` and `Failed` go.
        run: the run's id in `runs`, for failures; None outside a run.
        dedupe: whether a file whose bytes are saved already becomes a hard link to them.
        sidecars: whether a file with `meta` gets `<name>.json`.
        exec: the program and arguments to run after each saved file, read only from the
            user's own config; None runs nothing.
        stop: the run's second Ctrl-C, which a plugin's `fetch` function stops at when it
            next reports its progress; None never stops it.
    """

    def __init__(self, store: Store, jobs: Scheduler, emit: Callable[[Any], None], *,
                 run: int | None = None, dedupe: bool = True, sidecars: bool = True,
                 exec: Sequence[str] | None = None,
                 stop: threading.Event | None = None) -> None:
        if exec is not None and (not isinstance(exec, (list, tuple)) or not exec
                                 or not all(isinstance(arg, str) for arg in exec)):
            raise UsageError("exec must be a list: the program, then its arguments",
                             hint='exec = ["echo", "{path}"]')
        self.store, self.jobs, self.emit, self.run = store, jobs, emit, run
        self.dedupe, self.sidecars, self.exec, self.stop = dedupe, sidecars, exec, stop
        self._rows = 0
        # The pairs of devices (saved file's, new file's) found unable to hard-link.
        self._unlinkable: set[tuple[int, int]] = set()
        self._checkpoints: set[asyncio.Task[Any]] = set()

    async def post(self, items: Sequence[Item], *, client: Fills, plugin: str) -> None:
        """Save one post's files at once, commit their rows together, then finish each.

        A file that fails or is busy is reported, and the others go on.

        Args:
            items: the post's files.
            client: the plugin's downloading client.
            plugin: the plugin's code, for its jobs.

        Raises:
            DiskError: the disk or flnd.db refused; what landed is committed first.
            MissingError: ffmpeg isn't installed.
        """
        if not mimetypes.inited:
            # `files.kind_of` reads the system's types once; that read stays off the loop.
            await self._call(mimetypes.init)
        found = await asyncio.gather(*(self._file(item, client, plugin) for item in items),
                                     return_exceptions=True)
        landed = [one for one in found if isinstance(one, _Landed)]
        failures = [one for one in found if isinstance(one, _Failure)]
        fatal = next((one for one in found if isinstance(one, BaseException)), None)
        if landed or failures:
            await self._commit(landed, failures)
        if fatal is not None:
            raise fatal
        await asyncio.gather(*(self._finish(one, plugin) for one in landed))

    async def close(self) -> None:
        """Wait for the checkpoints running, then checkpoint once more."""
        await asyncio.gather(*self._checkpoints)
        await self._call(self.store.checkpoint)

    # -- stages 1 to 4: the part, the transfer, the content check, dedupe ---------------------

    async def _file(self, item: Item, client: Fills, plugin: str) -> _Landed | _Failure | None:
        """One file into place and its row made; None when it was busy or had no URL."""
        try:
            return await self._land(item, client, plugin)
        except files.BusyError:
            self.emit(Skipped(item.target, item.category, item.post, item.dest, reason="busy"))
            return None
        except Error as error:
            if isinstance(error, (DiskError, files.MissingError)):
                raise
            return self._failed(item, error)
        except Exception as error:
            wrapped = classify(error)
            if isinstance(wrapped, DiskError):
                raise wrapped from error
            assert isinstance(wrapped, Error)
            return self._failed(item, wrapped)

    def _failed(self, item: Item, error: Error) -> _Failure:
        self.emit(Failed(item.target, error, post=item.post, path=item.dest))
        return _Failure(item, error)

    async def _land(self, item: Item, client: Fills, plugin: str) -> _Landed | None:
        file = item.file
        if file.url is None:
            return None
        received: dict[str, str] = {}
        try:
            saved = await self._transfer(item, file, client, plugin, received)
        except SiteError as error:
            if error.status not in EXPIRED or item.refresh is None:
                raise
            fresh = await item.refresh()
            if fresh is None or fresh.url is None:
                raise
            saved = await self._transfer(item, fresh, client, plugin, received)
        dated = item.day or served_day(received.get("last-modified"))
        row = FileRow(
            item.key, _target_key(item.target), str(saved.path), saved.size, saved.mtime,
            post_id=item.post, category=item.category,
            date=item.day.isoformat() if item.day else None, estimated=item.estimated,
            kind=files.kind_of(saved.path.name), at=now(), files=item.count)
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

    async def _transfer(self, item: Item, file: File, client: Fills, plugin: str,
                        received: dict[str, str]) -> files.Saved:
        """Open the part, fill it from each mirror in turn, check it and land it."""
        mirrors: list[str | Fetch] = (
            [file.url] if isinstance(file.url, str) or callable(file.url) else list(file.url))
        async with self._part(item.dest) as held:
            if item.day is not None:
                held.mtime = noon(item.day)
            for number, mirror in enumerate(mirrors, start=1):
                try:
                    if number == 1 and file.audio is not None and isinstance(mirror, str):
                        await self._dash(item, mirror, file.audio, held, client, plugin)
                        break
                    received.update(await self._mirror(item, mirror, held, client, plugin))
                    if item.kind is not None:
                        await self._call(_place, held, item.kind)
                    break
                except (DiskError, files.MissingError):
                    raise
                except Exception as error:
                    if number == len(mirrors):
                        raise
                    ui.warn("mirror {n} failed: {error}; trying the next", n=number, error=error)
                    await self._call(held.reset)
            if held.mtime is None:
                served = served_day(received.get("last-modified"))
                held.mtime = noon(served) if served is not None else None
        assert held.saved is not None
        return held.saved

    async def _mirror(self, item: Item, mirror: str | Fetch, held: files.Part, client: Fills,
                      plugin: str) -> Mapping[str, str]:
        """One mirror into the part: a URL by the client, or a plugin's `fetch` function."""
        if isinstance(mirror, str):
            return await self.jobs.run(self._file_job(
                item, plugin, mirror, lambda: client.fill(mirror, held, headers=item.headers)))
        # A fetch that resumes keeps its offsets in `<part>.json`, which goes when it lands.
        held.side = True

        def progress(*_: Any) -> None:
            if self.stop is not None and self.stop.is_set():
                raise KeyboardInterrupt

        async def fetch() -> None:
            if inspect.iscoroutinefunction(mirror):
                await mirror(held.path, progress)
                return
            outcome = await asyncio.to_thread(mirror, held.path, progress)
            if inspect.isawaitable(outcome):
                await outcome

        await self.jobs.run(self._file_job(item, plugin, "", fetch))
        return {}

    async def _dash(self, item: Item, video: str, audio: str, held: files.Part, client: Fills,
                    plugin: str) -> None:
        """A video and its audio as two transfers at once, then one ffmpeg join into `held`.

        Each stream has a part of its own (`<name>.video.part`), kept for the next try while
        the join fails; once joined, or given up on, both go. Empty audio saves the video alone.
        """
        ffmpeg = files.ffmpeg()
        async with AsyncExitStack() as stack:
            streams = [await stack.enter_async_context(
                self._part(held.dest.with_name(f"{held.dest.name}.{name}")))
                for name in ("video", "audio")]
            await _together(*(
                self.jobs.run(self._file_job(
                    item, plugin, url,
                    lambda url=url, part=part: client.fill(url, part, headers=item.headers)))
                for url, part in zip((video, audio), streams, strict=True)))
            for stream in streams:
                await self._call(stream.flush)
            if self.stop is not None and self.stop.is_set():
                raise KeyboardInterrupt

            async def join() -> None:
                try:
                    await media.join(streams[0], streams[1], held, ffmpeg)
                except files.RunError as error:
                    # A join that failed may have read a stream cut short: worth another try.
                    error.retryable = True
                    raise

            try:
                if streams[1].size == 0 and streams[0].size > 0:
                    await self._call(shutil.copyfile, streams[0].path, held.path)
                else:
                    await self.jobs.run(Job(kind=Kind.MERGE, work=join, plugin=plugin,
                                            account=item.account, target=str(item.target)))
            finally:
                for stream in streams:
                    stream.drop()

    def _file_job(self, item: Item, plugin: str, url: str,
                  work: Callable[[], Awaitable[Any]]) -> Job:
        host = (urlsplit(url).hostname or "") if url else ""
        return Job(kind=Kind.FILE, work=work, plugin=plugin, account=item.account, bucket=host,
                   host=host, target=str(item.target), size=item.file.size,
                   priority=RETRIED if item.retried else FILE_PRIORITY)

    @asynccontextmanager
    async def _part(self, dest: Path) -> AsyncIterator[files.Part]:
        """`files.part(dest, resume=True)`, opened and landed on threads, never on the loop."""
        manager = files.part(dest, resume=True)
        held = await self.jobs.run(Job(kind=Kind.CALL, work=lambda: _enter(manager)))
        if held is None:
            # Raised here, not in the job: a busy part is the file's, never the run's stop.
            raise files.BusyError(f"{dest.name}: being saved by another run")
        try:
            yield held
        except BaseException as error:
            # The part keeps its bytes for the next run, and raises `error` again.
            await _through(manager.__exit__, type(error), error, error.__traceback__)
            raise
        await _through(manager.__exit__, None, None, None)

    async def _dedupe(self, landed: _Landed, plugin: str) -> None:
        """Link the file to a saved one with the same bytes; hash only when sizes match."""
        row = landed.row
        found = self.store.sized(row.size, besides=row.key)
        if not found:
            return
        path = Path(row.path)
        unhashed = [(key, Path(where)) for key, where, digest in found if digest is None]
        digests = await asyncio.gather(
            self._hash(path, row.size, plugin),
            *(self._hash(where, row.size, plugin) for _, where in unhashed))
        row.hash = digests[0]
        for (key, _), digest in zip(unhashed, digests[1:]):
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
                return

    async def _hash(self, path: Path, size: int, plugin: str) -> bytes | None:
        """The SHA-256 of a file on a hashing thread; None when it isn't on disk at `size`."""
        async def work() -> bytes | None:
            return await asyncio.to_thread(_digest, path, size)
        return await self.jobs.run(Job(kind=Kind.HASH, work=work, plugin=plugin, size=size))

    # -- stage 5: the commit ------------------------------------------------------------------

    async def _commit(self, landed: Sequence[_Landed], failures: Sequence[_Failure]) -> None:
        """A post's rows in one transaction on the loop; a lock held elsewhere is waited out
        `BUSY_PAUSE` at a time, so the loop never stalls."""
        rows = [one.row for one in landed]
        hashes = {key: digest for one in landed for key, digest in one.hashes.items()}
        await self._write(self.store.commit, rows, failures, hashes, self.run)
        self._rows += len(rows)
        if self._rows >= CHECKPOINT_ROWS:
            self._rows = 0
            task = asyncio.ensure_future(self._call(self.store.checkpoint))
            self._checkpoints.add(task)
            task.add_done_callback(self._checkpoints.discard)

    async def _write(self, call: Callable[..., T], *args: Any) -> T:
        """`call(*args)` on the store, again every `BUSY_PAUSE` while another process writes."""
        until = clock.now() + BUSY_FOR
        while True:
            try:
                return call(*args)
            except _Busy as error:
                if clock.now() >= until:
                    raise DiskError(f"{self.store.path}: locked by another process for"
                                    f" {BUSY_FOR:.0f}s", hint="let the other run finish") from error
                await clock.pause(BUSY_PAUSE)

    # -- stages 6 to 9: unpack, `Saved`, exec; the sidecar went before the commit -----------

    async def _finish(self, landed: _Landed, plugin: str) -> None:
        item, path = landed.item, landed.path
        if item.file.password is not None or files.is_archive(path):
            await self._write(self.store.update, item.key, await self._unpack(item, path, plugin))
        self.emit(Saved(item.target, item.category, item.post, path, landed.day,
                        landed.row.size, retried=item.retried))
        if self.exec:
            argv = self.exec

            async def work() -> None:
                await asyncio.to_thread(_exec, argv, path, item.target, item.post)
            await self.jobs.run(Job(kind=Kind.EXEC, work=work, plugin=plugin,
                                    target=str(item.target)))

    async def _unpack(self, item: Item, path: Path, plugin: str) -> dict[str, Any]:
        """Extract an archive beside itself on the unpack lane; a failure keeps it, and warns."""
        async def work() -> Path:
            return await asyncio.to_thread(files.extract, path, password=item.file.password)
        try:
            folder = await self.jobs.run(Job(kind=Kind.UNPACK, work=work, plugin=plugin,
                                             target=str(item.target)))
        except Error as error:
            ui.warn("{name}: not extracted: {error}", name=path.name, error=error)
            return {"unpacked": "failed"}
        return {"unpacked": "extracted", "folder": str(folder)}

    async def _call(self, call: Callable[..., T], *args: Any) -> T:
        """`call(*args)` as a `CALL` job: on the scheduler's threads, waited for to its end."""
        return await self.jobs.run(Job(kind=Kind.CALL, work=lambda: _through(call, *args)))


# ---------------------------------------------------------------------------------------------
# The blocking steps, each run on a thread


# `_link`'s answer when the file system can't hard-link at all.
_NO_LINKS = -1


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


async def _enter(manager: Any) -> files.Part | None:
    """Open a part on a thread; None when another run holds it. A cancel waits for the open,
    then leaves what it opened, so no lock outlives the save."""
    def enter() -> files.Part | None:
        try:
            held = manager.__enter__()
        except files.BusyError:
            return None
        # An empty write makes the part's buffer here, so a write from the loop opens nothing.
        held.write(b"")
        return held

    work = asyncio.ensure_future(asyncio.to_thread(enter))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError as error:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not work.cancelled() and work.exception() is None and work.result() is not None:
            await _through(manager.__exit__, type(error), error, None)
        raise


async def _together(*works: Awaitable[Any]) -> None:
    """Await `works` at once; the first failure cancels the others, then is raised."""
    tasks = [asyncio.ensure_future(work) for work in works]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _through(call: Callable[..., T], *args: Any) -> T:
    """`call(*args)` on a thread, waited for to its end even through a cancel: a part's file
    calls must finish before anything else touches its descriptor."""
    work = asyncio.ensure_future(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        raise


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
