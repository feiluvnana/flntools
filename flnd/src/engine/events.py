"""What a run reports, and the rows the library reads.

SPEC.md#for-developers explains it."""

from __future__ import annotations

import datetime
import inspect
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

import msgspec

# Both are read at runtime, by `typing.get_type_hints` (the API snapshot, a decoder), so they
# are real imports. `flnd.errors` is stdlib-only; `flnd.plugin` is the light half of flnd.
from flnd.errors import Error
from flnd.plugin import Target

__all__ = [
    "BUSY", "NOTHING_NEW", "RESUMING", "Checked", "Event", "Failed", "Failure",
    "FileState", "HoldState", "Phase", "Planned", "Record", "Report",
    "Saved", "Skipped", "State", "Status", "TargetState", "Tracked", "sink",
]

# `Skipped.reason` for a listing a sync found nothing new in.
NOTHING_NEW = "nothing new"
# `Skipped.reason` for a file or post already saved.
SAVED = "saved"
# `Skipped.reason` for a file another run is saving: its part is locked.
BUSY = "busy"
# What the job sends the run's sink, beside the events, for a listing that goes on from where
# an earlier run was cut short: the run counts it in `Report.resumed`, and never queues it.
RESUMING = "resuming from the last run"


# ---------------------------------------------------------------------------------------------
# Events: what a run yields, in the order it happens
# ---------------------------------------------------------------------------------------------

class Saved(msgspec.Struct, frozen=True, tag=True):
    """A file was downloaded and went through every step after: it is on disk under its final
    name (after `--compress`, the compressed file's), with its row.

    Example:
        >>> import datetime, pathlib
        >>> from flnd.plugin import Target
        >>> event = Saved(Target("ins:example_user", kind="user"), "posts", "9001",
        ...               pathlib.Path("media/ins/example_user/9001.jpg"),
        ...               datetime.date(2026, 1, 2), 2048)
        >>> event.path.name, event.date.year, event.size
        ('9001.jpg', 2026, 2048)
    """

    # The target the file was downloaded for (for a search, the target the search found).
    target: Target
    # The category the post came from; "" for a target without categories (a single post).
    category: str
    # The post's id, as the site gives it.
    post: str
    # Where the file is now, under the home's `media/` folder; for an archive removed once
    # extracted (`--compress`), the folder it was extracted to.
    path: Path
    # The post's day, which is also the file's modified time; None when the site gave none.
    date: datetime.date | None
    # The file's size in bytes, as it is now (after `--compress`, the compressed size).
    size: int
    # Whether a retry brought it back: an earlier run failed on it (SPEC.md#when-it-goes-wrong).
    retried: bool = False


class Skipped(msgspec.Struct, frozen=True, tag=True):
    """Something wasn't downloaded: a file or post already saved, or a listing with nothing new.

    `reason` says which: `"saved"` for a file or a whole post that is already saved, `"busy"`
    for a file another run is saving, `"nothing new"` for a listing a sync found nothing new
    in, which has no post and no path.

    Example:
        >>> from flnd.plugin import Target
        >>> event = Skipped(Target("ins:example_user", kind="user"), "posts", "9001")
        >>> event.path is None, event.reason
        (True, 'saved')
    """

    target: Target
    # The category the post came from; "" for a target without categories.
    category: str
    # The post's id; "" for a listing with nothing new.
    post: str
    # The file that was skipped; None when the whole post was (it was saved before).
    path: Path | None = None
    # Why: "saved" or "nothing new".
    reason: str = SAVED


class Failed(msgspec.Struct, frozen=True, tag=True):
    """A file, or a whole target, failed: the run went on with the rest.

    A failed file has its `post` and `path`. A failed target (a login it needs, a listing the
    site refused) has neither: `error` says why it ended.

    Example:
        >>> from flnd.errors import LoginError
        >>> from flnd.plugin import Target
        >>> event = Failed(Target("ins:example_user", kind="user"),
        ...                LoginError("not logged in", hint="run flnd auth login ins chrome"))
        >>> event.error.hint
        'run flnd auth login ins chrome'
    """

    target: Target
    # Why: a `flnd.Error`, whose `str()` is the message and `hint` what to do.
    error: Error
    # The post's id, for a failed file; "" for a failed target.
    post: str = ""
    # Where the file would have gone, for a failed file; None for a failed target.
    path: Path | None = None


class Planned(msgspec.Struct, frozen=True, tag=True):
    """What a dry run (`dry=True`) would do with one file: nothing is downloaded or written.

    `state` is `"new"` (a run would download it) or `"saved"` (it is already in the home).
    For a search, each target found is planned as one event with no post and no path.

    Example:
        >>> import pathlib
        >>> from flnd.plugin import Target
        >>> Planned(Target("ins:example_user", kind="user"), "9001",
        ...         pathlib.Path("media/ins/example_user/9001.jpg"), "new").state
        'new'
    """

    target: Target
    # The post's id; "" for a target a search found.
    post: str
    # Where the file would go; None for a target a search found.
    path: Path | None
    # "new" or "saved".
    state: str


# Any event a run yields, for a `match` or an annotation.
Event = Saved | Skipped | Failed | Planned


# ---------------------------------------------------------------------------------------------
# The snapshot: what the screen and a script read while a run goes (`run.state`)
# ---------------------------------------------------------------------------------------------

class Phase(StrEnum):
    """Where a target stands in a run."""

    WAITING = "waiting"
    LISTING = "listing"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"


class TargetState(msgspec.Struct, frozen=True):
    """One target of a run, as `State.targets` shows it.

    Example:
        >>> row = TargetState("ins:example_user", ("posts",), Phase.LISTING, found=3, saved=1)
        >>> row.phase, row.found - row.saved
        (<Phase.LISTING: 'listing'>, 2)
    """

    # The target, as written (`ins:example_user`).
    text: str
    # Its categories: the ones listed so far, else the ones it will take; empty when unknown.
    categories: tuple[str, ...]
    phase: Phase
    # Files found so far, then how each ended.
    found: int = 0
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    # Bytes saved.
    bytes: int = 0
    # Bytes a second over the last 3 s.
    speed: float = 0.0


class FileState(msgspec.Struct, frozen=True):
    """One file in flight, as `State.files` shows it.

    Example:
        >>> row = FileState("ins:example_user", "posts/9001.mp4", 512, 2048)
        >>> row.done / row.total
        0.25
    """

    # The target, as written.
    target: str
    # Where it goes, under the target's folder.
    path: str
    # Bytes received, and its size; `total` is None when the size isn't known.
    done: int
    total: int | None
    # Bytes a second over the last 3 s.
    speed: float = 0.0


class HoldState(msgspec.Struct, frozen=True):
    """A wait the run is in: a site asked it to come back later, or a source waits.

    Example:
        >>> hold = HoldState("twt", "pages", 42.0, "429 from api.example.com", 3)
        >>> hold.until, hold.count
        (42.0, 3)
    """

    # The source's code.
    plugin: str
    # What waits: "pages" for the listing.
    bucket: str
    # When the wait ends, in seconds since the run started (`State.elapsed`'s clock).
    until: float
    # Why, in the site's or the source's words.
    reason: str
    # The round of waits in a row this one is, from 1.
    count: int = 1


class State(msgspec.Struct, frozen=True):
    """What a run is doing now: `run.state`, readable from any thread.

    The run swaps in a new one 30 times a second while something changed, so a reader keeps the one
    it holds as long as it likes; `version` says whether it is newer than the last one read.

    Example:
        >>> state = State()
        >>> state.version, state.targets, state.saved
        (0, (), 0)
    """

    # One more at each new snapshot; 0 before the run starts.
    version: int = 0
    # Seconds since the run started.
    elapsed: float = 0.0
    # Every target of the run, in the order they were given.
    targets: tuple[TargetState, ...] = ()
    # The files in flight, oldest first, at most 6; `more` counts the rest.
    files: tuple[FileState, ...] = ()
    more: int = 0
    # The waits going on.
    holds: tuple[HoldState, ...] = ()
    # What is happening, in one line: the newest hold if one is going on, else the newest
    # listing step (`"ins:example_user · posts, page 4"`).
    now: str = ""
    # The whole run's counts, bytes saved, and bytes a second over the last 3 s.
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    speed: float = 0.0

# ---------------------------------------------------------------------------------------------
# The report and the reads
# ---------------------------------------------------------------------------------------------

class Report(msgspec.Struct, frozen=True):
    """What a run did, counted from its events: `run.report` so far, `run.wait()` at the end.

    A failed file is a `Failed` event and a line of `failed`, never an exception, so a script
    that wants to be strict checks `report.failed` (or `report.ok`).

    Example:
        >>> report = Report(saved=2, bytes=4096)
        >>> report.ok, report.saved, report.failed
        (True, 2, ())
    """

    # Files downloaded and placed.
    saved: int = 0
    # Files (or whole posts) already saved, so not downloaded again.
    skipped: int = 0
    # The bytes of the saved files, as they are on disk (after `--compress`, compressed).
    bytes: int = 0
    # Files a dry run planned.
    planned: int = 0
    # Each failed file and failed target, in the order they failed.
    failed: tuple[Failed, ...] = ()
    # Whether `run.stop()` ended the run before it was done.
    stopped: bool = False
    # Files a retry brought back: earlier runs failed on them (they count in `saved` too).
    retried: int = 0
    # Listings that went on from where an earlier run was cut short.
    resumed: int = 0
    # Listings a sync found nothing new in.
    unchanged: int = 0

    @property
    def ok(self) -> bool:
        """Whether nothing failed."""
        return not self.failed


class Record(msgspec.Struct, frozen=True):
    """A saved file's row: where it is, how big, and which post it came from.

    Example:
        >>> import pathlib
        >>> record = Record("ins:9001_0", pathlib.Path("media/ins/example_user/9001.jpg"), 2048,
        ...                 post="9001", category="posts")
        >>> record.kind
        'image'
    """

    # The row's key: the source's code, then the file's key (`ins:9001_0`).
    key: str
    # Where the file is.
    path: Path
    # Its size in bytes when it was saved.
    size: int
    # The post's id.
    post: str = ""
    # The category the post came from; "" for a target without categories.
    category: str = ""
    # The post's day; None when the site gave none.
    date: datetime.date | None = None
    # When the file was saved, as an ISO 8601 time in UTC; "" when unknown.
    at: str = ""

    @property
    def kind(self) -> str | None:
        """What the file is by its name: "image", "video" or "audio"; None for anything else."""
        # Imported here: the table of extensions lives with the other path rules, and reading
        # a `Record` shouldn't import them until a script asks.
        from flnkit.files import kind_of

        return kind_of(self.path.name)


class Tracked(msgspec.Struct, frozen=True):
    """A target the home knows: gotten once, or followed, with what is on disk for it.

    Example:
        >>> from flnd.plugin import Target
        >>> row = Tracked(Target("ins:example_user", kind="user"), follow=True, files=3)
        >>> row.follow, row.files
        (True, 3)
    """

    target: Target
    # Whether `get` with no target takes it.
    follow: bool = False
    # When it was last run, as an ISO 8601 time; "" when never.
    at: str = ""
    # How many files its folder holds, and their bytes.
    files: int = 0
    size: int = 0
    # For a search: how many targets it found so far.
    seen: int = 0
    # The categories it takes: the ones kept with it, else the plugin's own.
    categories: tuple[str, ...] = ()
    # Each category listed to its end, with the day it was.
    complete: dict[str, str] = {}
    # Whether it is a search (`<plugin>?<words>`) rather than one account or post.
    query: bool = False
    # Download status: "synced", "partial", "failed", "retry", or "new" ("" when unset).
    status: str = ""
    # Pending failed files (not NotFound and not saved since).
    failed: int = 0
    # Categories not yet completed (missing from complete, or with open cursors or gaps).
    pending: tuple[str, ...] = ()


class Failure(msgspec.Struct, frozen=True):
    """A file or target that failed and isn't saved since, as `fl.status` lists it.

    It is read from `flnd.db`, which keeps one row per failed file: the run it last failed in,
    and how many runs failed it.

    Example:
        >>> import pathlib
        >>> from flnd.errors import NetworkError
        >>> failure = Failure("ins:example_user", NetworkError("connection reset"),
        ...                   post="9001", path=pathlib.Path("media/ins/example_user/9001.jpg"))
        >>> type(failure.error).__name__, failure.attempts
        ('NetworkError', 1)
    """

    # The target, as written (`ins:example_user`).
    target: str
    # Why: the `flnd.Error` it failed with, made again from its class, message and hint.
    error: Error
    # The post's id, for a failed file; "" for a failed target.
    post: str = ""
    # Where the file would have gone, for a failed file; None for a failed target.
    path: Path | None = None
    # The runs that tried it and failed. A file is retried by itself on at most 3 runs; past
    # them, `flnd target list`'s r still tries it.
    attempts: int = 1
    # The file's URL, redacted: no query and no user info; "" for none.
    url: str = ""
    # When it last failed, as an ISO 8601 time in UTC.
    at: str = ""


class Status(msgspec.Struct, frozen=True):
    """The last run that downloaded, and the failures it left: `fl.status`, `flnd status`.

    Example:
        >>> status = Status("sync", "2026-11-16T10:00:00+00:00", saved=12)
        >>> status.saved, status.failures
        (12, ())
    """

    # What ran: the command and its first targets (`download ins:example_user`).
    command: str
    # When it started and ended, as ISO 8601 times in UTC; `ended` is "" while it runs, or when
    # it never ended (a crash).
    started: str
    ended: str = ""
    # Files it saved.
    saved: int = 0
    # The failures it left that still stand: a file saved since is left out.
    failures: tuple[Failure, ...] = ()


class Checked(msgspec.Struct, frozen=True):
    """What `fl.check()` found comparing the history with the disk, and whether it fixed it.

    A missing file's and a wrong-sized file's `Record` is its row, as the history holds it.

    Example:
        >>> import pathlib
        >>> found = Checked(files=3, unknown=(pathlib.Path("media/ins/example_user/x.jpg"),))
        >>> found.ok, len(found.unknown)
        (False, 1)
    """

    # How many rows were checked: saved files and their links.
    files: int = 0
    # Files the history has and the disk doesn't.
    missing: tuple[Record, ...] = ()
    # Files under `media/` the history doesn't have.
    unknown: tuple[Path, ...] = ()
    # Files whose size on disk isn't the history's.
    resized: tuple[Record, ...] = ()
    # Whether `fix=True` made the history match the disk.
    fixed: bool = False

    @property
    def ok(self) -> bool:
        """Whether history and disk agree, now: nothing was found, or all of it was fixed."""
        return self.fixed or not (self.missing or self.unknown or self.resized)


# ---------------------------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------------------------

def sink(emit: Callable[[Any], Any] | None) -> Callable[[Any], Awaitable[None]]:
    """`emit` as a coroutine function the loop awaits: `Run._emit` as it is, a plain function
    (a test's `list.append`) called at once, and None dropping every event."""

    async def put(event: Any) -> None:
        if emit is not None:
            done = emit(event)
            if inspect.isawaitable(done):
                await done

    return put
