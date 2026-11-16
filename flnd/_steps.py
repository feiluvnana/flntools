"""The steps after a file lands, in their one fixed order, and the threads that run the slow ones.

Every saved file goes through the same chain (the architecture guide, docs/media):

    download (hashing) → dedupe → unpack → compress → date → place → record → sidecar → Saved

Each step is on or off by its flag or setting; the order never changes, so no combination can,
say, compress a file before dedupe has linked it. `--exec` is the CLI's: it runs a command for
each `Saved` event it reads, so it always comes after the whole chain. Where each step runs:
- download, date, place and record run on the download's own thread, in `flnd.job`: the part
  is hashed as it is written (`flnd._files`), renamed into place and dated, and its row written
  to `flnd.db` with the post's other files;
- dedupe runs there too, as each file lands and before its row is written: the lookup on the
  loop (`holder`), the link on a worker thread (`dedupe`). A file whose bytes a saved file
  already has becomes a hard link to it (`[defaults] dedupe = "link"`, the default);
- unpack and compress are slow (seconds for a video, minutes for a big archive), so the job
  hands the recorded file to this module's `Chain`, which runs them on lanes of their own
  threads, and they re-run place and record for the file they change. A slow encode never
  holds up a download;
- the sidecar (`[defaults] sidecars = true`) and the `Saved` event come last, wherever the file
  ended: on the lane that ran its slow steps, or at once on the download's thread for a file
  that needs none (`_finish`). So `Saved.path` is always the file's final name.

How a landed file goes through the chain:
1. `landed` puts it on a lane: an archive on the `archive` lane; a photo on the `photo` lane
   (2 threads: libavif already spreads one encode over every core); a video or song on the
   `video` lane (1 thread: ffmpeg uses every core). A file with no slow step is finished at
   once. A lane's queue is bounded, but at `QUEUE` items, far more than downloads produce
   while one encode runs.
2. A lane thread runs `_run`: unpack an archive (`flnd._unpack.extract`), or compress a media
   file (`flnd._shrink.shrink`) when its target asked for `--compress`. Compress keeps dedupe's
   links: the result is linked at every path that shared the original's bytes (`_relink`), and
   every row moves in one transaction.
3. Each result is written to the file's row of `flnd.db` by `record` (`Home.update`), or by
   `Home.change` for a file and its links.
4. `_finish` writes the sidecar and sends `Saved`, with the path and size the row now holds.
5. `close` waits for the lanes, or, after Ctrl-C, finishes what hasn't started without its slow
   steps and lets running steps end (a second Ctrl-C kills a running ffmpeg). It logs one
   summary line.

`backlog` feeds the same lanes from `flnd.db`: `flnd sync --compress` uses it to compress files
saved before, skipping each file already done (one query, no `stat` per done file). Those files
were saved by an earlier run, so they send no `Saved`.

Rules a change must keep:
- every write to `flnd.db` goes through `record` or `Home.change`, a file before its row;
- a step's failure is the file's, never the run's: it is logged and recorded as `failed`, and
  the file is still finished (its `Saved` is sent);
- every file handed to `landed` is finished exactly once, whatever happens to its slow steps;
- `Chain` is closed before the database it writes to (`attached` does both in order);
- `flnd._unpack`, `flnd._shrink` and what they import (zipfile, tarfile, Pillow) are imported
  inside the functions that use them, so `flnd --help` never loads them.
"""

from __future__ import annotations

import contextlib
import os
import queue
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flnd import _files, _text
from flnd.errors import DiskError, Error
from flnd._ui import ui

if TYPE_CHECKING:
    from flnd._events import Saved
    from flnd._home import FileRow, Home

__all__ = ["Chain", "Landed", "attached", "dedupe", "holder", "landed", "record", "sidecars"]

# How many files a lane holds waiting; a full lane makes `landed` wait (memory stays flat).
QUEUE = 10_000
# Threads per lane (see stage 1).
LANES = {"archive": 1, "photo": 2, "video": 1}
# The `files` columns this module writes.
SHRINK, TOOLS, BEFORE, UNPACKED, FOLDER = "shrink", "tools", "before", "unpacked", "folder"
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
        video: "av1" or "hevc", the codec its videos go to.
        hash: the SHA-256 of its bytes, which finds the hard links dedupe made; None for none.
        event: the `Saved` to send once its steps are done, with the path and size they leave;
            None for a file an earlier run saved (`backlog`), which sends nothing.
        meta: what its sidecar holds; None when sidecars are off.
    """

    key: str
    path: Path
    password: str | None = None
    compress: bool = False
    video: str = "av1"
    hash: bytes | None = None
    event: Saved | None = None
    meta: Mapping[str, Any] | None = field(default=None, hash=False)


@dataclass
class _Tally:
    """What the chain did this run, for its summary line."""

    compressed: int = 0
    kept: int = 0
    failed: int = 0
    saved: int = 0
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
    """

    def __init__(self, db: Home | None, stop: Any = None, *,
                 emit: Callable[[Any], None] | None = None, dedupe: bool = True,
                 sidecars: bool = False) -> None:
        from flnd import _shrink

        self.db, self.stop = db, stop
        self.emit = emit or (lambda event: None)
        self.dedupe, self.sidecars = dedupe, sidecars
        self.savings = _shrink.Savings()
        self.tally = _Tally()
        self._lanes: dict[str, _Lane] = {}
        self._lock = threading.Lock()
        self._seeded = False
        # One lock per hash being compressed, so two hard links of one file are never
        # compressed at once: the second waits, then finds the first moved it.
        self._twins: dict[bytes, threading.Lock] = {}
        self._no_links = False

    def landed(self, item: Landed) -> None:
        """Queue a recorded file for the steps it needs (stage 1), or finish it at once.

        Args:
            item: the file.
        """
        from flnd import _shrink, _unpack

        if self._stopping():
            self._finish(item, moved=False)
            return
        if item.password is not None or _unpack.is_archive(item.path):
            self._lane("archive").put(item)
            return
        of = _shrink.kind(item.path)
        if not item.compress or of is None:
            self._finish(item, moved=False)
            return
        self._lane("photo" if of in _shrink.PHOTOS else "video").put(item)

    def backlog(self, rows: Iterable[FileRow], *, video: str = "av1") -> int:
        """Queue saved files that still need unpacking or compressing.

        A file is done when it was compressed, or kept or failed with today's tools. Its kind
        comes from its suffix, so a done file costs no `stat`.

        Args:
            rows: the targets' file rows (`Home.unfinished`), read once.
            video: "av1" or "hevc".

        Returns:
            How many files were queued.
        """
        from flnd import _shrink, _unpack

        queued = 0
        for row in rows:
            self._seed_one(row)
            item = Landed(row.key, Path(row.path), compress=True, video=video, hash=row.hash)
            if _unpack.is_archive(item.path):
                if not row.unpacked:
                    self.landed(item)
                    queued += 1
                continue
            of = _shrink.kind(item.path)
            if of is None or _done(row, _shrink.tools(of, video=video)):
                continue
            self.landed(item)
            queued += 1
        self._seeded = True
        return queued

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
            else:
                self._compress(item)
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
        # The archive goes only after the extract was verified and recorded.
        if item.compress:
            item.path.unlink(missing_ok=True)

    def _compress(self, item: Landed) -> None:
        """The compress step, for a file whose kind is still worth it.

        How it keeps dedupe's hard links (docs/storage, "Dedupe"):
        1. It holds the lock of the file's hash, so its links are never compressed at once.
        2. A file whose row is already compressed was moved by a link's compress: done.
        3. The rows with the same hash whose file is this very file (same inode) are its links.
        4. `shrink` encodes once. Its `record` call (`moved`) links the result at each link's
           path, then moves every row in one transaction, then removes the links' old names;
           `shrink` removes the file's own old name after.
        A full disk fails this file only; the next run retries it.

        Args:
            item: the landed photo, video or song.
        """
        from flnd import _shrink

        of = _shrink.kind(item.path)
        if of is None:
            return
        self._seed()
        if not self.savings.worth(of):
            ui.debug("{name}: left alone: {kind} files don't shrink", name=item.path.name,
                     kind=of.value)
            return
        with self._twin_lock(item.hash):
            row = self.db.row(item.key) if self.db is not None else None
            if row is not None and (row.shrink == "compressed" or row.path != str(item.path)):
                ui.debug("{name}: compressed with a link of it", name=item.path.name)
                return
            try:
                original = item.path.stat()
            except OSError:
                return
            twins = self._links(item, original)

            def moved(outcome: _shrink.Outcome) -> None:
                self._moved(item, row, twins, outcome)

            kill = self.stop.now if self.stop is not None else None
            try:
                outcome = _shrink.shrink(item.path, record=moved, video=item.video, stop=kill)
            except DiskError as error:
                self.tally.failed += 1
                ui.error("{name}: {error}", name=item.path.name, error=error)
                return
        self._count(of, outcome)

    def _count(self, of: Any, outcome: Any) -> None:
        """Count one compress result into the savings and the tally, and log it.

        Args:
            of: the file's `_shrink.Kind`.
            outcome: what `shrink` did.
        """
        from flnd import _shrink

        self.savings.add(of, outcome.saving)
        with self._lock:
            if outcome.state is _shrink.State.COMPRESSED:
                self.tally.compressed += 1
                self.tally.saved += outcome.before - outcome.after
            elif outcome.state is _shrink.State.KEPT:
                self.tally.kept += 1
            else:
                self.tally.failed += 1
        if outcome.state is _shrink.State.FAILED:
            ui.warn("{name}: not compressed: {note}", name=outcome.path.name, note=outcome.note)
        else:
            ui.debug("{name}: {state} ({saving:.0%})", name=outcome.path.name,
                     state=outcome.state.value, saving=outcome.saving)

    def _twin_lock(self, digest: bytes | None) -> Any:
        """The lock of one hash, made on first use; a file with no hash has nothing to share."""
        if digest is None:
            return contextlib.nullcontext()
        with self._lock:
            return self._twins.setdefault(digest, threading.Lock())

    def _links(self, item: Landed, original: os.stat_result) -> list[FileRow]:
        """The rows of the other names of this very file: dedupe's and the ledger's links.

        Args:
            item: the file about to be compressed.
            original: its `stat`, before anything changed it.

        Returns:
            The rows with its hash whose path is the same inode; a copy with the same bytes
            that isn't linked (dedupe off, or a file system without links) is left alone.
        """
        if self.db is None or item.hash is None:
            return []
        found = []
        for twin in self.db.twins(item.hash, besides=item.key):
            try:
                if os.path.samestat(os.stat(twin.path), original):
                    found.append(twin)
            except OSError:
                continue
        return found

    def _moved(self, item: Landed, row: FileRow | None, twins: Sequence[FileRow],
               outcome: Any) -> None:
        """`shrink`'s record call: move the file's row, and its links' files and rows with it.

        Runs after the result is in place and before the original's old name is removed.

        Args:
            item: the file compressed.
            row: its row before the compress; None in a run without a database.
            twins: its links' rows (`_links`).
            outcome: what `shrink` did.

        Raises:
            DiskError: the rows can't be written; the links made for them are removed again.
        """
        from flnd import _shrink

        fields: dict[str, Any] = {SHRINK: outcome.state.value, TOOLS: outcome.tools}
        if outcome.state is not _shrink.State.COMPRESSED:
            record(self.db, item.key, **fields)
            return
        found = outcome.path.stat()
        fields.update(path=str(outcome.path), size=found.st_size, mtime=found.st_mtime_ns,
                      hash=_files.digest(outcome.path), **{BEFORE: outcome.before})
        changes: list[tuple[str, dict[str, Any]]] = [(item.key, fields)]
        undo: list[Callable[[], Any]] = []
        old: list[Path] = []
        side = _move_sidecar(row, outcome.path, undo)
        if side is not None:
            fields["sidecar"] = side
        for twin in twins:
            moved = self._relink(twin, outcome.path, undo)
            if moved is None:
                continue
            twin_fields = {**fields, "path": str(moved)}
            twin_fields.pop("sidecar", None)
            twin_side = _move_sidecar(twin, moved, undo)
            if twin_side is not None:
                twin_fields["sidecar"] = twin_side
            changes.append((twin.key, twin_fields))
            if moved != Path(twin.path):
                old.append(Path(twin.path))
        try:
            if self.db is not None:
                self.db.change(changes)
        except BaseException:
            for step in reversed(undo):
                with contextlib.suppress(OSError):
                    step()
            raise
        for path in old:
            path.unlink(missing_ok=True)
        if len(changes) > 1:
            ui.debug("{name}: {links} kept", name=outcome.path.name,
                     links=_text.count(len(changes) - 1, "link"))

    def _relink(self, twin: FileRow, result: Path, undo: list[Callable[[], Any]]) -> Path | None:
        """Link a compress result at one link's path: its own name, with the result's suffix.

        A hard link stays a hard link. A symlink (the ledger's link across category folders)
        is made again, pointing at the result, so it never dangles once the original's old
        name is removed.

        Args:
            twin: the link's row.
            result: the compressed file.
            undo: where to add what removes the new link again, should the rows fail.

        Returns:
            The link's new path; None when it can't be linked (a name taken by another file, a
            file system without links), and it then keeps the original bytes as its own.
        """
        before = Path(twin.path)
        after = before.with_suffix(result.suffix)
        if after != before and (after.exists() or after.is_symlink()):
            return None
        try:
            if before.is_symlink():
                _symlink(result, after)
            elif not _files.link(result, after):
                return None
        except (OSError, DiskError) as error:
            ui.debug("{name}: not linked: {error}", name=after.name, error=error)
            return None
        if after != before:
            undo.append(lambda: after.unlink(missing_ok=True))
        return after

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
            path = Path(twin.path)
            if path == Path(row.path):
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

        self.emit(replace(item.event, path=path, size=size))

    def _sidecar(self, item: Landed, path: Path) -> None:
        """The sidecar step: the post's metadata as `<file>.json`, beside the file.

        A disk that refuses it fails the sidecar alone: the file stays saved.

        Args:
            item: the file, with its `meta`.
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

    def _seed(self) -> None:
        """Fill `savings` from earlier results in `flnd.db`, once, before the first compress."""
        with self._lock:
            if self._seeded:
                return
            self._seeded = True
        for row in self.db.shrunk() if self.db is not None else ():
            self._seed_one(row)

    def _seed_one(self, row: FileRow) -> None:
        """Count one file's earlier compress result in `savings`.

        Args:
            row: the file's row; one without a compress result counts for nothing.
        """
        from flnd import _shrink

        if row.shrink not in ("compressed", "kept"):
            return
        of = _shrink.kind(Path(row.path))
        if of is None:
            return
        if row.shrink == "kept" or row.before is None:
            self.savings.add(of, 0.0)
        elif row.before > 0:
            self.savings.add(of, 1 - row.size / row.before)

    def _summary(self) -> None:
        """Log one line for what the chain did, when it did anything."""
        tally = self.tally
        parts = []
        if tally.compressed:
            parts.append(f"{_text.count(tally.compressed, 'file')} compressed, "
                         f"{_text.size(tally.saved)} saved")
        if tally.kept:
            parts.append(f"{tally.kept} kept as they were")
        if tally.failed:
            parts.append(f"{tally.failed} failed")
        if tally.unpacked:
            parts.append(f"{_text.count(tally.unpacked, 'archive')} extracted")
        if tally.unpack_failed:
            parts.append(f"{tally.unpack_failed} not extracted")
        if tally.linked:
            parts.append(f"{_text.count(tally.linked, 'file')} already saved elsewhere, linked")
        if parts:
            (ui.warn if tally.failed or tally.unpack_failed else ui.ok)(
                "Media: {line}", line="; ".join(parts))


def _done(row: FileRow, tools: str) -> bool:
    """Whether a file needs no compress: compressed, or kept or failed by `tools`."""
    if row.shrink == "compressed":
        return True
    return row.shrink in ("kept", "failed") and row.tools == tools


def _symlink(target: Path, link: Path) -> None:
    """Make `link` a relative symlink to `target`, replacing whatever `link` was, atomically.

    Args:
        target: the file to point at.
        link: the symlink's path.

    Raises:
        OSError: the file system refused.
    """
    made = link.with_name(link.name + ".part.link")
    made.unlink(missing_ok=True)
    made.symlink_to(os.path.relpath(target, link.parent))
    os.replace(made, link)


def _move_sidecar(row: FileRow | None, path: Path,
                  undo: list[Callable[[], Any]]) -> str | None:
    """Move a file's sidecar to follow the file's new name, when it has one and the name changed.

    Args:
        row: the file's row before the move; None or one without a sidecar moves nothing.
        path: the file's new path.
        undo: where to add what moves it back, should the rows fail.

    Returns:
        The sidecar's new path, for its row; None when nothing moved.
    """
    if row is None or not row.sidecar:
        return None
    before = Path(row.sidecar)
    after = path.with_name(path.name + SIDECAR)
    if after == before:
        return None
    try:
        os.replace(before, after)
    except OSError:
        # The sidecar keeps its old name; it still says what it said.
        return None
    undo.append(lambda: os.replace(after, before))
    return str(after)


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
        """Queue `item`, waiting while the lane is full."""
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

    from flnd import _stats

    found = config(settings.home)
    chain = Chain(settings.db, settings.stop, emit=settings.emit, dedupe=found.dedupe,
                  sidecars=found.sidecars)
    settings.chain = chain
    try:
        # The run's live stats read the lanes' jobs (`Chain.jobs`) while the chain is open.
        with _stats.tracked(_stats.CHAIN, chain):
            yield chain
    finally:
        settings.chain = None
        chain.close()


def landed(settings: Any, item: Landed) -> None:
    """Hand a recorded file to the run's chain; a run without one sends its `Saved` at once.

    Args:
        settings: the run's `RunShared`.
        item: the file.
    """
    chain = getattr(settings, "chain", None)
    if chain is not None:
        chain.landed(item)
    elif item.event is not None:
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
        found: what `holder` found.
    """
    chain = getattr(settings, "chain", None)
    if chain is not None:
        chain._dedupe(row, *found)


def sidecars(settings: Any) -> bool:
    """Whether the run writes sidecars, so the job builds each file's metadata only then.

    Args:
        settings: the run's `RunShared`.

    Returns:
        True when the home's config turned sidecars on.
    """
    chain = getattr(settings, "chain", None)
    return chain is not None and chain.sidecars
