"""The steps after a file lands, in their one fixed order,
and the threads that run the slow ones."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flnd import _files, _text
from flnd._ui import ui
from flnd.errors import DiskError, Error

if TYPE_CHECKING:
    from flnd._events import Saved
    from flnd._home import FileRow, Home

__all__ = ["Chain", "Landed", "attached", "dedupe", "holder", "landed", "record", "sidecars"]

# How many files a lane holds waiting; a full lane makes `landed` wait (memory stays flat).
QUEUE = 10_000
# Threads per lane (see stage 1).
LANES = {"archive": 1}
# The `files` columns this module writes.
UNPACKED, FOLDER = "unpacked", "folder"
EXTRACTED, FAILED = "extracted", "failed"
# A sidecar's name is its file's name with this after it: `9001.jpg.json`.
SIDECAR = ".json"
# How many saved files with the same bytes dedupe tries as the one to link to.
HOLDERS = 4


@dataclass(frozen=True)
class Landed:
    """A file saved and recorded, for the steps after its row.

    Args:
        key: its history key (`ins:ABC_0`).
        path: where it is.
        password: an archive's password from its source; None for none.
        compress: whether its target asked for `--compress`.
        hash: the SHA-256 of its bytes, which finds the hard links dedupe made; None for none.
        event: the `Saved` to send once its steps are done, with the path and size they leave;
            None for a file an earlier run saved (`backlog`), which sends nothing.
        meta: what its sidecar holds; None when sidecars are off.
    """

    key: str
    path: Path
    password: str | None = None
    hash: bytes | None = None
    event: Saved | None = None
    meta: Mapping[str, Any] | None = field(default=None, hash=False)


@dataclass
class _Tally:
    """What the chain did this run, for its summary line."""

    unpacked: int = 0
    unpack_failed: int = 0
    linked: int = 0


def record(db: Home | None, key: str, **fields: Any) -> None:
    """Set `fields` on the file's row of `flnd.db`, in one statement (`Home.update`).

    Args:
        db: the run's database; None records nothing.
        key: the file's key; a key with no row is left alone.
        **fields: the columns to set, such as `shrink="compressed"`.
    """
    if db is not None:
        db.update(key, **fields)


class Chain:
    """The steps after a file's row for one run: lanes for unpack and compress, then the
    sidecar and the `Saved` event.

    Args:
        db: the run's database; None records nothing.
        stop: the run's Ctrl-C signal (`soon` and `now` events); None never stops.
        emit: where `Saved` events go (the run's sink); None drops them.
        dedupe: whether a file whose bytes are already saved becomes a hard link to them.
        sidecars: whether each saved file gets `<file>.json` with its post's metadata.
        tracks: the file tracks for the download run; None if not tracked.
    """

    def __init__(self, db: Home | None, stop: Any = None, *,
                 emit: Callable[[Any], None] | None = None, dedupe: bool = True,
                 sidecars: bool = False, tracks: Any = None) -> None:
        self.db, self.stop = db, stop
        self.emit = emit or (lambda event: None)
        self.tracks = tracks
        self.dedupe, self.sidecars = dedupe, sidecars
        self.tally = _Tally()
        self._lanes: dict[str, _Lane] = {}
        self._lock = threading.Lock()
        self._no_links = False

    def landed(self, item: Landed) -> None:
        """Queue a recorded file for the steps it needs (stage 1), or finish it at once.

        Args:
            item: the landed file to process.
        """
        from flnd import _unpack

        if self._stopping():
            self._finish(item, moved=False)
            return
        if item.password is not None or _unpack.is_archive(item.path):
            self._lane("archive").put(item)
            return
        self._finish(item, moved=False)

    def close(self) -> None:
        """Wait for every lane, or finish what hasn't started after Ctrl-C, and log a summary."""
        for lane in self._lanes.values():
            lane.close()
        self._lanes.clear()
        self._summary()

    @property
    def jobs(self) -> dict[str, tuple[int, int]]:
        """A snapshot of each lane started so far: its files running now, and waiting.

        Returns:
            `(running, waiting)` by lane name (`archive`, `photo`, `video`). Any thread may
            read it.
        """
        with self._lock:
            lanes = list(self._lanes.items())
        return {name: (lane.running, lane.waiting) for name, lane in lanes}

    def __enter__(self) -> Chain:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- the lanes --------------------------------------------------------------------------

    def _lane(self, name: str) -> _Lane:
        """The lane `name`, started on first use.

        Args:
            name: a key of `LANES`.

        Returns:
            The lane, with its threads running.
        """
        with self._lock:
            lane = self._lanes.get(name)
            if lane is None:
                lane = self._lanes[name] = _Lane(name, LANES[name], self._run, self._dropped,
                                                 self._stopping)
            return lane

    def _stopping(self) -> bool:
        return self.stop is not None and self.stop.soon.is_set()

    def _run(self, item: Landed) -> None:
        """Run the slow steps for one file, in the chain's order (stage 2), then finish it.

        A failure is the file's: it is logged and recorded, and the lane goes on. A file gone
        since it landed (removed by hand, or moved by a hard link's compress) is passed by.

        Args:
            item: the landed file.
        """
        from flnd import _unpack

        try:
            if not item.path.exists():
                ui.debug("{name}: gone before its steps ran", name=item.path.name)
            elif item.password is not None or _unpack.is_archive(item.path):
                self._unpack(item)
        finally:
            self._finish(item, moved=True)

    def _dropped(self, item: Landed) -> None:
        """A queued file Ctrl-C dropped before its slow steps: it is saved, so it's finished."""
        self._finish(item, moved=False)

    def _unpack(self, item: Landed) -> None:
        """The unpack step: extract beside the archive, then remove it under `--compress`.

        A failure (a wrong password, a damaged or unsafe archive, no 7-Zip) keeps the archive,
        records `unpacked = "failed"` and warns once for the file.

        Args:
            item: the landed archive.
        """
        from flnd import _unpack

        try:
            folder = _unpack.extract(item.path, password=item.password)
        except Error as error:
            self.tally.unpack_failed += 1
            ui.warn("{name}: not extracted: {error}", name=item.path.name, error=error)
            record(self.db, item.key, **{UNPACKED: FAILED})
            return
        self.tally.unpacked += 1
        record(self.db, item.key, **{UNPACKED: EXTRACTED, FOLDER: str(folder)})
        ui.debug("{name}: extracted to {folder}", name=item.path.name, folder=folder.name)

    # -- dedupe -----------------------------------------------------------------------------

    def _dedupe(self, row: FileRow, held: Path, mtime: int) -> None:
        """The dedupe step for one landed file, not recorded yet, whose bytes `held` has.

        The file is replaced, atomically, by a hard link to `held`, and its row takes the
        shared file's modified time. A file system that can't link keeps both copies, with
        one warning, and links nothing more this run.

        Args:
            row: the file's row, about to be written; changed in place.
            held: the saved file with the same bytes (`_holder`).
            mtime: its modified time, in nanoseconds.
        """
        if self._no_links:
            return
        path = Path(row.path)
        try:
            linked = _files.link(held, path)
        except DiskError as error:
            ui.debug("{name}: not linked: {error}", name=path.name, error=error)
            return
        if not linked:
            with self._lock:
                warn, self._no_links = not self._no_links, True
            if warn:
                ui.warn("this folder can't hard-link files, so the same file saved twice is "
                        "kept twice")
            return
        # One file now, with the holder's modified time.
        row.mtime = mtime
        with self._lock:
            self.tally.linked += 1
        ui.debug("{name}: the same as {held}, linked", name=path.name, held=held.name)

    def _holder(self, row: FileRow) -> tuple[Path | None, int | None]:
        """A saved file with the same bytes as `row`'s, on disk at the size its row says.

        The landed file was just renamed from a new part, so it can't already be a link of it.

        Args:
            row: the landed file's row.

        Returns:
            Its path and modified time (ns); `(None, None)` when there is none.
        """
        if self.db is None or row.hash is None:
            return None, None
        # A few candidates: the first on disk at its size is enough.
        for twin in self.db.twins(row.hash, besides=row.key, limit=HOLDERS):
            if twin.state != "saved":
                continue
            path = Path(twin.path)
            if path == Path(row.path) or path.is_symlink():
                continue
            try:
                found = path.stat()
            except OSError:
                continue
            if found.st_size == row.size:
                return path, found.st_mtime_ns
        return None, None

    # -- finishing: the sidecar and `Saved` -------------------------------------------------

    def _finish(self, item: Landed, *, moved: bool) -> None:
        """The steps after the slow ones (stage 4): the sidecar, then the `Saved` event.

        Args:
            item: the file; one without an event (a backlog file) has nothing to finish.
            moved: whether a slow step ran, so the row is read for where the file is now.
        """
        if item.event is None:
            return
        path, size = item.path, item.event.size
        row = self.db.row(item.key) if moved and self.db is not None else None
        if row is not None:
            path, size = Path(row.path), row.size
            # An archive removed once extracted is where it was extracted to.
            if row.folder and row.unpacked == EXTRACTED and not path.exists():
                path = Path(row.folder)
        if item.meta is not None and self.sidecars and path.is_file():
            self._sidecar(item, path)
        from msgspec.structs import replace

        event = replace(item.event, path=path, size=size)
        if self.tracks is not None:
            track = self.tracks.get(item.key)
            if track is not None:
                track.saved(event)
                return
        self.emit(event)

    def _sidecar(self, item: Landed, path: Path) -> None:
        """The sidecar step: the post's metadata as `<file>.json`, beside the file.

        A disk that refuses it fails the sidecar alone: the file stays saved.

        Args:
            item: the file, with its metadata (`meta`).
            path: where the file is now.
        """
        import msgspec

        side = path.with_name(path.name + SIDECAR)
        body = msgspec.json.format(msgspec.json.encode(item.meta, enc_hook=str), indent=2)
        try:
            _files.write(side, body + b"\n")
            record(self.db, item.key, sidecar=str(side))
        except DiskError as error:
            ui.warn("{name}: no sidecar: {error}", name=path.name, error=error)

    # -- savings and the summary ------------------------------------------------------------

    def _summary(self) -> None:
        """Log one line for what the chain did, when it did anything."""
        tally = self.tally
        parts = []
        if tally.unpacked:
            parts.append(f"{_text.count(tally.unpacked, 'archive')} extracted")
        if tally.unpack_failed:
            parts.append(f"{tally.unpack_failed} not extracted")
        if tally.linked:
            parts.append(f"{_text.count(tally.linked, 'file')} already saved elsewhere, linked")
        if parts:
            (ui.warn if tally.unpack_failed else ui.ok)("Media: {line}", line="; ".join(parts))


class _Lane:
    """One bounded queue and its threads, started on the first `put`.

    Args:
        name: the lane's name, for its threads.
        threads: how many threads run its items.
        run: what runs one item; it must not raise for an item's own failure.
        drop: what an item dropped after Ctrl-C is handed to instead.
        stopping: True once queued items should be dropped (Ctrl-C).
    """

    _END = object()

    def __init__(self, name: str, threads: int, run: Any, drop: Any, stopping: Any) -> None:
        self._items: queue.Queue[Any] = queue.Queue(maxsize=QUEUE)
        self._run, self._drop, self._stopping = run, drop, stopping
        # Items a thread is running now, for `Chain.jobs`; changed by the lane's own threads.
        self.running = 0
        self._counting = threading.Lock()
        self._threads = [threading.Thread(target=self._serve, name=f"flnd-{name}-{n}",
                                          daemon=True) for n in range(threads)]
        for thread in self._threads:
            thread.start()

    @property
    def waiting(self) -> int:
        """Items queued and not started yet (an estimate while threads take them)."""
        return self._items.qsize()

    def put(self, item: Landed) -> None:
        """Queue `item`, waiting while the lane is full.

        Args:
            item: the landed file to queue.
        """
        self._items.put(item)

    def close(self) -> None:
        """Let the threads finish what is queued (or drop it after Ctrl-C), then join them."""
        for _ in self._threads:
            self._items.put(self._END)
        for thread in self._threads:
            thread.join()

    def _serve(self) -> None:
        """One lane thread: run items until the end marker, dropping them after Ctrl-C.

        A step's own failure is handled inside `run`; anything else it raises is a bug, logged
        here so one file never ends the lane.
        """
        while True:
            item = self._items.get()
            if item is self._END:
                return
            try:
                if self._stopping():
                    self._drop(item)
                else:
                    with self._counting:
                        self.running += 1
                    try:
                        self._run(item)
                    finally:
                        with self._counting:
                            self.running -= 1
            except KeyboardInterrupt:
                continue
            except Exception as error:  # a bug in a step: the file fails, the lane goes on
                ui.error("{name}: {kind} {error}", name=item.path.name,
                         kind=type(error).__name__, error=error)


# -------------------------------------------------------------------------------------------
# The run's chain


@contextmanager
def attached(settings: Any) -> Iterator[Chain]:
    """Give a run's settings their chain for the block, and close it at the end.

    The chain reads `dedupe` and `sidecars` from the home's `config.toml`. It is closed before
    the block's caller closes the database, which it writes to.

    Args:
        settings: the run's `RunShared`, whose `home`, `db`, `stop` and `emit` the chain uses.

    Yields:
        The chain, also set as `settings.chain`.

    Raises:
        UsageError: the home's `config.toml` is broken.
    """
    from flnd.options import config

    found = config(settings.home)
    chain = Chain(settings.db, settings.stop, emit=settings.emit, dedupe=found.dedupe,
                  sidecars=found.sidecars, tracks=settings.tracks)
    settings.chain = chain
    try:
        yield chain
    finally:
        settings.chain = None
        chain.close()


def landed(settings: Any, item: Landed) -> None:
    """Hand a recorded file to the run's chain; a run without one sends its `Saved` at once.

    Args:
        settings: the run's `RunShared`.
        item: the file to hand over.
    """
    chain = getattr(settings, "chain", None)
    if chain is not None:
        chain.landed(item)
    elif item.event is not None:
        tracks = getattr(settings, "tracks", None)
        if tracks is not None:
            track = tracks.get(item.key)
            if track is not None:
                track.saved(item.event)
                return
        settings.emit(item.event)


def holder(settings: Any, row: FileRow) -> tuple[Path, int] | None:
    """Dedupe's lookup for a landed file: a saved file with the same bytes, if there is one.

    One indexed query and one `stat`, cheap enough for the run's loop; the link itself
    (`dedupe`) is a few hundred microseconds of file-system work, so it goes to a thread.

    Args:
        settings: the run's `RunShared`; a run without a chain, or with dedupe off, finds
            nothing.
        row: the landed file's row, with its hash.

    Returns:
        The saved file's path and modified time (ns); None for none.
    """
    chain = getattr(settings, "chain", None)
    if chain is None or not chain.dedupe or chain._no_links:
        return None
    held, mtime = chain._holder(row)
    return (held, mtime) if held is not None and mtime is not None else None


def dedupe(settings: Any, row: FileRow, found: tuple[Path, int]) -> None:
    """The dedupe step for one landed file, before its row is written: link it to `found`.

    Args:
        settings: the run's `RunShared`, with its chain.
        row: the file's row; its `mtime` changes to the shared file's once linked.
        found: what `holder` found (path and modified time).
    """
    chain = getattr(settings, "chain", None)
    if chain is not None:
        chain._dedupe(row, *found)


def sidecars(settings: Any) -> bool:
    """Whether the run writes sidecars, so the job builds each file's metadata only then.

    Args:
        settings: the run's `RunShared`.

    Returns:
        True when the home's config turned sidecars on, False otherwise.
    """
    chain = getattr(settings, "chain", None)
    return chain is not None and chain.sidecars
