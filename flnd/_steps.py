"""The steps after a file lands, in their one fixed order, and the threads that run the slow ones.

Every saved file goes through the same chain (the architecture guide, docs/media):

    download → dedupe → unpack → compress → date → place → record → sidecar → exec → Saved

Each step is on or off by its flag or setting; the order never changes, so no combination can,
say, compress a file before dedupe has linked it. Where each step runs today:
- download, date, place and record run on the download's own thread, in `flnd.job`: the part
  is renamed into place, dated, and its history row written;
- unpack and compress are slow (seconds for a video, minutes for a big archive), so the job
  hands the recorded file to this module's `Chain`, which runs them on lanes of their own
  threads, and they re-run place and record for the file they change. A slow encode never
  holds up a download;
- dedupe, sidecar and exec arrive with their own commit; their places in `_run` are marked.

How a landed file goes through the chain:
1. `landed` puts it on a lane: an archive on the `archive` lane; a photo on the `photo` lane
   (2 threads: libavif already spreads one encode over every core); a video or song on the
   `video` lane (1 thread: ffmpeg uses every core). A lane's queue is bounded, but at
   `QUEUE` items, far more than downloads produce while one encode runs.
2. A lane thread runs `_run`: unpack an archive (`flnd._unpack.extract`), or compress a media
   file (`flnd._shrink.shrink`) when its target asked for `--compress`.
3. Each result is written to the file's history row by `record`, the one function that knows
   where the row lives.
4. `close` waits for the lanes, or, after Ctrl-C, drops what hasn't started and lets running
   steps end (a second Ctrl-C kills a running ffmpeg). It logs one summary line.

`backlog` feeds the same lanes from history: `flnd sync --compress` uses it to compress files
saved before, skipping each file already done (one read of history, no `stat` per done file).

Rules a change must keep:
- every history write goes through `record`, so the store can move without touching the steps;
- a step's failure is the file's, never the run's: it is logged and recorded as `failed`;
- `Chain` is closed before the history it writes to (`attached` does both in order);
- `flnd._unpack`, `flnd._shrink` and what they import (zipfile, tarfile, Pillow) are imported
  inside the functions that use them, so `flnd --help` never loads them.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterable, Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flnd import _text
from flnd.errors import DiskError, Error
from flnd._ui import ui

__all__ = ["Chain", "Landed", "attached", "landed", "record"]

# How many files a lane holds waiting; a full lane makes `landed` wait (memory stays flat).
QUEUE = 10_000
# Threads per lane (see stage 1).
LANES = {"archive": 1, "photo": 2, "video": 1}
# The history fields this module writes.
SHRINK, TOOLS, BEFORE, UNPACKED, FOLDER = "shrink", "tools", "before", "unpacked", "folder"
EXTRACTED, FAILED = "extracted", "failed"


@dataclass(frozen=True)
class Landed:
    """A file saved and recorded, for the slow steps.

    Args:
        key: its history key (`ins:ABC_0`).
        path: where it is.
        password: an archive's password from its source; None for none.
        compress: whether its target asked for `--compress`.
        video: "av1" or "hevc", the codec its videos go to.
    """

    key: str
    path: Path
    password: str | None = None
    compress: bool = False
    video: str = "av1"


@dataclass
class _Tally:
    """What the chain did this run, for its summary line."""

    compressed: int = 0
    kept: int = 0
    failed: int = 0
    saved: int = 0
    unpacked: int = 0
    unpack_failed: int = 0


def record(history: MutableMapping[str, Any], key: str, **fields: Any) -> None:
    """Merge `fields` into the history row at `key`, in one write.

    This is the only place the steps write history, so moving history to another store (one
    `flnd.db` with a column per field) changes this function and nothing else.

    Args:
        history: the run's history table.
        key: the file's history key; a key with no row is left alone.
        **fields: the fields to set, such as `shrink="compressed"`.
    """
    row = history.get(key)
    if isinstance(row, dict):
        history[key] = {**row, **fields}


class Chain:
    """The lanes that run unpack and compress for one run, writing to its history.

    Args:
        history: the run's history table.
        stop: the run's Ctrl-C signal (`soon` and `now` events); None never stops.
    """

    def __init__(self, history: MutableMapping[str, Any], stop: Any = None) -> None:
        from flnd import _shrink

        self.history, self.stop = history, stop
        self.savings = _shrink.Savings()
        self.tally = _Tally()
        self._lanes: dict[str, _Lane] = {}
        self._lock = threading.Lock()
        self._seeded = False

    def landed(self, item: Landed) -> None:
        """Queue a recorded file for the steps it needs (stage 1); others pass straight by.

        Args:
            item: the file.
        """
        from flnd import _shrink, _unpack

        if self._stopping():
            return
        if item.password is not None or _unpack.is_archive(item.path):
            self._lane("archive").put(item)
            return
        of = _shrink.kind(item.path)
        if not item.compress or of is None:
            return
        self._lane("photo" if of in _shrink.PHOTOS else "video").put(item)

    def backlog(
        self, rows: Iterable[tuple[str, Any]], folders: Iterable[Path], *, video: str = "av1",
    ) -> int:
        """Queue saved files under `folders` that still need unpacking or compressing.

        A file is done when it was compressed, or kept or failed with today's tools. Its kind
        comes from its suffix, so a done file costs no `stat`.

        Args:
            rows: history's (key, row) pairs, read once.
            folders: the target folders whose files to take.
            video: "av1" or "hevc".

        Returns:
            How many files were queued.
        """
        from flnd import _shrink, _unpack

        prefixes = tuple(f"{folder}/" for folder in folders)
        queued = 0
        for key, row in rows:
            path = row.get("path") if isinstance(row, dict) else None
            if not isinstance(path, str) or not path.startswith(prefixes):
                continue
            self._seed_one(row)
            item = Landed(key, Path(path), compress=True, video=video)
            if _unpack.is_archive(item.path):
                if UNPACKED not in row:
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
        """Wait for every lane, or drop what hasn't started after Ctrl-C, and log a summary."""
        for lane in self._lanes.values():
            lane.close()
        self._lanes.clear()
        self._summary()

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
                lane = self._lanes[name] = _Lane(name, LANES[name], self._run, self._stopping)
            return lane

    def _stopping(self) -> bool:
        return self.stop is not None and self.stop.soon.is_set()

    def _run(self, item: Landed) -> None:
        """Run the slow steps for one file, in the chain's order (stage 2).

        A failure is the file's: it is logged and recorded, and the lane goes on. A file gone
        since it landed (removed by hand, or by an earlier step) is passed by.

        Args:
            item: the landed file.
        """
        from flnd import _unpack

        if not item.path.exists():
            ui.debug("{name}: gone before its steps ran", name=item.path.name)
            return
        if item.password is not None or _unpack.is_archive(item.path):
            self._unpack(item)
            return
        self._compress(item)
        # sidecar and exec run here, after compress, once they exist.

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
            record(self.history, item.key, **{UNPACKED: FAILED})
            return
        self.tally.unpacked += 1
        record(self.history, item.key, **{UNPACKED: EXTRACTED, FOLDER: str(folder)})
        ui.debug("{name}: extracted to {folder}", name=item.path.name, folder=folder.name)
        # The archive goes only after the extract was verified and recorded.
        if item.compress:
            item.path.unlink(missing_ok=True)

    def _compress(self, item: Landed) -> None:
        """The compress step, for a file whose kind is still worth it.

        The history row moves with the file inside `shrink`'s `record` call, before the
        original is removed. A full disk fails this file only; the next run retries it.

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

        def moved(outcome: _shrink.Outcome) -> None:
            fields: dict[str, Any] = {SHRINK: outcome.state.value, TOOLS: outcome.tools}
            if outcome.state is _shrink.State.COMPRESSED:
                found = outcome.path.stat()
                fields.update(path=str(outcome.path), size=found.st_size,
                              mtime=found.st_mtime_ns, **{BEFORE: outcome.before})
            record(self.history, item.key, **fields)

        kill = self.stop.now if self.stop is not None else None
        try:
            outcome = _shrink.shrink(item.path, record=moved, video=item.video, stop=kill)
        except DiskError as error:
            self.tally.failed += 1
            ui.error("{name}: {error}", name=item.path.name, error=error)
            return
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
            ui.warn("{name}: not compressed: {note}", name=item.path.name, note=outcome.note)
        else:
            ui.debug("{name}: {state} ({saving:.0%})", name=outcome.path.name,
                     state=outcome.state.value, saving=outcome.saving)

    # -- savings and the summary ------------------------------------------------------------

    def _seed(self) -> None:
        """Fill `savings` from history's earlier results, once, before the first compress."""
        with self._lock:
            if self._seeded:
                return
            self._seeded = True
        for _, row in self.history.items():
            self._seed_one(row)

    def _seed_one(self, row: Any) -> None:
        """Count one history row's earlier compress result in `savings`.

        Args:
            row: a history row; one without a compress result counts for nothing.
        """
        from flnd import _shrink

        if not isinstance(row, dict) or row.get(SHRINK) not in ("compressed", "kept"):
            return
        of = _shrink.kind(Path(str(row.get("path", ""))))
        before, size = row.get(BEFORE), row.get("size")
        if of is None:
            return
        if row[SHRINK] == "kept" or not isinstance(before, int) or not isinstance(size, int):
            self.savings.add(of, 0.0)
        elif before > 0:
            self.savings.add(of, 1 - size / before)

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
        if parts:
            (ui.warn if tally.failed or tally.unpack_failed else ui.ok)(
                "Media: {line}", line="; ".join(parts))


def _done(row: dict[str, Any], tools: str) -> bool:
    """Whether a history row's file needs no compress: compressed, or kept/failed by `tools`."""
    state = row.get(SHRINK)
    if state == "compressed":
        return True
    return state in ("kept", "failed") and row.get(TOOLS) == tools


class _Lane:
    """One bounded queue and its threads, started on the first `put`.

    Args:
        name: the lane's name, for its threads.
        threads: how many threads run its items.
        run: what runs one item; it must not raise for an item's own failure.
        stopping: True once queued items should be dropped (Ctrl-C).
    """

    _END = object()

    def __init__(self, name: str, threads: int, run: Any, stopping: Any) -> None:
        self._items: queue.Queue[Any] = queue.Queue(maxsize=QUEUE)
        self._run, self._stopping = run, stopping
        self._threads = [threading.Thread(target=self._serve, name=f"flnd-{name}-{n}",
                                          daemon=True) for n in range(threads)]
        for thread in self._threads:
            thread.start()

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
            if self._stopping():
                continue
            try:
                self._run(item)
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

    The chain is closed before the block's caller closes history, which it writes to.

    Args:
        settings: the run's `RunShared`, whose `history` and `stop` the chain uses.

    Yields:
        The chain, also set as `settings.chain`.
    """
    chain = Chain(settings.history, settings.stop)
    settings.chain = chain
    try:
        yield chain
    finally:
        settings.chain = None
        chain.close()


def landed(settings: Any, item: Landed) -> None:
    """Hand a recorded file to the run's chain; nothing happens in a run without one.

    Args:
        settings: the run's `RunShared`.
        item: the file.
    """
    chain = getattr(settings, "chain", None)
    if chain is not None:
        chain.landed(item)

