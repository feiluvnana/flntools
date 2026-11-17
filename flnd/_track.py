"""One file's download lifecycle: the only place its events come from.

The run's display is the board's summary rows (`flnd.job.Board`), so a file tracks its phase
and its events here, and draws nothing of its own. Architecture guide: flnd/docs/architecture.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from flnd._events import Failed, Saved, Skipped

__all__ = ["Phase", "FileTrack", "Tracks", "_Bar"]


class Phase(StrEnum):
    """Where a file stands in the run; only moves forward."""

    QUEUED = "queued"
    HELD = "held"
    ACTIVE = "active"
    MERGING = "merging"
    UNPACKING = "unpacking"
    SAVED = "saved"
    LINKED = "linked"
    SKIPPED = "skipped"
    FAILED = "failed"
    DROPPED = "dropped"


TERMINAL = frozenset({
    Phase.SAVED, Phase.LINKED, Phase.SKIPPED, Phase.FAILED, Phase.DROPPED,
})

_ORDER = {phase: index for index, phase in enumerate(Phase)}


def _forward(old: Phase, new: Phase) -> None:
    """Raise when a file would leave a terminal state or move backward.

    Args:
        old: the current phase.
        new: the phase to move to.

    Raises:
        ValueError: the phase transition is invalid.
    """
    if old == new:
        return
    if _ORDER[new] < _ORDER[old]:
        raise ValueError(f"file in {old!s} can't go back to {new!s}")
    if old in TERMINAL:
        raise ValueError(f"file already ended as {old!s}")


@dataclass
class FileTrack:
    """One file from queue through its final event.

    Args:
        key: a unique identifier for the file in the download run.
        label: the label used for the progress bar.
        target: the target the file belongs to.
        post_id: the post identifier.
        category: the category of the file (e.g., photo, video).
        dest: the destination path for the file.
        emit: a function called to emit events during the lifecycle.
        retried: whether this is a retry of a previous failed attempt.
        phase: the current phase of the file in the download run.
    """

    key: str
    label: str
    target: Any
    post_id: str
    category: str
    dest: Path
    emit: Callable[[Any], None]
    retried: bool = False
    phase: Phase = Phase.QUEUED
    _lock: threading.Lock = threading.Lock()

    def active(self) -> None:
        """Mark the download as started.

        Raises:
            ValueError: the file already ended or can't start.
        """
        with self._lock:
            if self.phase not in (Phase.QUEUED, Phase.HELD, Phase.ACTIVE):
                raise ValueError(f"file in {self.phase!s} can't start")
            _forward(self.phase, Phase.ACTIVE)
            self.phase = Phase.ACTIVE

    def merging(self) -> None:
        """The DASH video and audio streams are being joined."""
        with self._lock:
            if self.phase in (Phase.QUEUED, Phase.ACTIVE):
                _forward(self.phase, Phase.MERGING)
                self.phase = Phase.MERGING

    def unpacking(self) -> None:
        """The post-download steps (unpack, sidecar) are running."""
        with self._lock:
            if self.phase in (Phase.QUEUED, Phase.ACTIVE, Phase.MERGING):
                _forward(self.phase, Phase.UNPACKING)
                self.phase = Phase.UNPACKING

    def skip(self, reason: str | None = None) -> None:
        """End skipped and emit a `Skipped` event.

        Args:
            reason: the reason for skipping.
        """
        with self._lock:
            _forward(self.phase, Phase.SKIPPED)
            self.phase = Phase.SKIPPED
            self.emit(Skipped(self.target, self.category, self.post_id, self.dest,
                              reason=reason))

    def fail(self, error: BaseException) -> None:
        """End failed and emit a `Failed` event.

        Args:
            error: the error that caused the failure.
        """
        with self._lock:
            _forward(self.phase, Phase.FAILED)
            self.phase = Phase.FAILED
            self.emit(Failed(self.target, error, post=self.post_id, path=self.dest))

    def dropped(self) -> None:
        """Drop the file after Ctrl-C without an event."""
        with self._lock:
            if self.phase in TERMINAL:
                return
            self.phase = Phase.DROPPED

    def saved(self, event: Saved) -> None:
        """End saved and emit the final `Saved` event.

        Args:
            event: the file's final `Saved` row for the run.
        """
        with self._lock:
            _forward(self.phase, Phase.SAVED)
            self.phase = Phase.SAVED
            self.emit(event)

    def bar(self) -> _Bar:
        return _Bar(self)


class _Bar(AbstractContextManager["_Bar"]):
    """Progress sink for `flnd._transfer`, backed by the file track.

    The run's display is the board's summary rows, so the bar carries only what a download
    needs to answer for: the stop checks (a second Ctrl-C) and the phase captions. It keeps
    the same surface as `flnd._transfer._Row`, which `_transfer` uses when there is no track.

    Args:
        track: the file whose lifecycle the sink moves.
    """

    def __init__(self, track: FileTrack) -> None:
        self._track = track
        self.label = track.label
        self.stack: Any = None  # kept for the same shape as `_Row`

    def __enter__(self) -> _Bar:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def start(self, total: float | None) -> None:
        self._track.active()

    def update(self, done: float, total: float | None = None) -> None:
        """Set how much of this try is done; nothing draws, so this counts for nothing."""

    def stoppable(self, done: float, total: float | None = None, *,
                  stop: threading.Event | None = None) -> None:
        if stop is not None and stop.is_set():
            raise KeyboardInterrupt

    def follow(self, stop: threading.Event | None = None) -> _TrackStream:
        return _TrackStream(self, stop)

    def grow(self, old: float | None, new: float | None) -> None:
        self.start(new)

    def advance(self, n: float) -> None:
        """Count `n` more bytes; nothing draws, so this counts for nothing."""

    def caption(self, text: str) -> None:
        if "merg" in text.lower():
            self._track.merging()


class _TrackStream:
    """One stream's progress on a file track (same surface as `flnd._transfer._Stream`).

    Args:
        bar: the file's bar.
        stop: set to stop at the next chunk (second Ctrl-C).
    """

    def __init__(self, bar: _Bar, stop: threading.Event | None) -> None:
        self._bar = bar
        self._stop = stop
        self._total: float | None = None

    @property
    def total(self) -> float | None:
        return self._total

    @total.setter
    def total(self, value: float | None) -> None:
        self._bar.grow(self._total, value)
        self._total = value

    def advance(self, n: float) -> None:
        if self._stop is not None and self._stop.is_set():
            raise KeyboardInterrupt
        self._bar.advance(n)


class Tracks:
    """Every file of one target's download run.

    Args:
        target: the target being downloaded.
        emit: where run events go.
    """

    def __init__(self, target: Any, emit: Callable[[Any], None]) -> None:
        self.target = target
        self._emit = emit
        self._tracks: dict[str, FileTrack] = {}
        self._lock = threading.Lock()

    def add(self, key: str, label: str, post_id: str, category: str, dest: Path,
            *, retried: bool = False) -> FileTrack:
        track = FileTrack(key, label, self.target, post_id, category, dest, self._emit,
                          retried=retried)
        with self._lock:
            self._tracks[key] = track
        return track

    def get(self, key: str) -> FileTrack | None:
        with self._lock:
            return self._tracks.get(key)