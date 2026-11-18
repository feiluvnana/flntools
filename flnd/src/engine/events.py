"""What a run reports, and the rows the library reads: events, `Report`, `Login`,
`Record`, `Tracked`, `Checked`, `Status`, `Failure` and `Installed`.

src/docs/library explains it.

Every type here is a frozen msgspec struct, so it is cheap to make, safe to share between the
run's threads, and encodes to JSON (`encode`) for a script or a cron job. The module imports
msgspec, the errors and `flnd.source` (for `Target`), never the engine, so `flnd.Saved` is
cheap to reach.

Who makes them:
1. the job and the engine make `Saved`, `Skipped`, `Failed`, `Progress` and `Planned` as a run
   goes, and hand them to the run's sink (`flnd.engine.run.Run._emit`);
2. `Run` counts them into a `Report`, and yields them to whoever iterates the run;
3. `Flnd` makes `Login`, `Record`, `Tracked`, `Status` and `Failure` from the home's
   `flnd.db`, for its reads, `Checked` from `fl.check()`, and `Installed` from the sources it
   knows (`fl.sources`).

The one rule a change must keep: an event holds data, never a live object (a site, a file
handle, a lock), because a script may keep it after the run ends, and `encode` must turn it
into JSON. An error is the one exception: `Failed.error` is the `flnd.Error` itself, so a
script can read its `hint`; `encode` writes its class, message and hint.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import msgspec

# Both are read at runtime, by `typing.get_type_hints` (the API snapshot, a decoder), so they
# are real imports. `flnd.errors` is stdlib-only; `flnd.source` is the light half of flnd.
from flnd.errors import Error
from flnd.source import Target

__all__ = [
    "NOTHING_NEW", "RESUMING", "Checked", "Event", "Failed", "Failure", "Installed", "Login",
    "Planned", "Progress", "Record", "Report", "Saved", "Skipped", "Status", "Tracked", "encode",
]

# `Skipped.reason` for a listing a sync found nothing new in.
NOTHING_NEW = "nothing new"
# `Skipped.reason` for a file or post already saved, or being saved by another run.
SAVED = "saved"
# `Progress.note` for a listing that goes on from where an earlier run was cut short.
RESUMING = "resuming from the last run"


# ---------------------------------------------------------------------------------------------
# Events: what a run yields, in the order it happens
# ---------------------------------------------------------------------------------------------

class Saved(msgspec.Struct, frozen=True, tag=True):
    """A file was downloaded and went through every step after: it is on disk under its final
    name (after `--compress`, the compressed file's), with its row.

    Example:
        >>> import datetime, pathlib
        >>> from flnd.source import Target
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
    # Whether a retry brought it back: an earlier run failed on it (src/docs/errors).
    retried: bool = False


class Skipped(msgspec.Struct, frozen=True, tag=True):
    """Something wasn't downloaded: a file or post already saved, or a listing with nothing new.

    `reason` says which: `"saved"` for a file or a whole post that is already saved (or that
    another run is saving), `"nothing new"` for a listing a sync found nothing new in, which has
    no post and no path.

    Example:
        >>> from flnd.source import Target
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
        >>> import flnd
        >>> from flnd.source import Target
        >>> event = Failed(Target("ins:example_user", kind="user"),
        ...                flnd.LoginError("not logged in", hint="run flnd login ins chrome"))
        >>> event.error.hint
        'run flnd login ins chrome'
    """

    target: Target
    # Why: a `flnd.Error`, whose `str()` is the message and `hint` what to do.
    error: Error
    # The post's id, for a failed file; "" for a failed target.
    post: str = ""
    # Where the file would have gone, for a failed file; None for a failed target.
    path: Path | None = None


class Progress(msgspec.Struct, frozen=True, tag=True):
    """The counts of one category of one target so far, sent as they change.

    Example:
        >>> from flnd.source import Target
        >>> step = Progress(Target("ins:example_user", kind="user"), "posts", found=3, saved=1)
        >>> step.found - step.saved - step.skipped - step.failed
        2
    """

    target: Target
    # The category being listed; "" for a target without categories.
    category: str
    # Files found so far: queued, saved, skipped or failed.
    found: int = 0
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    # Bytes saved so far.
    bytes: int = 0
    # Whether the listing is done: what is still found is only finishing downloads.
    listed: bool = False
    # How the listing goes, when it is worth saying: "resuming from the last run" when it goes
    # on where an earlier run was cut short; "" otherwise.
    note: str = ""


class Planned(msgspec.Struct, frozen=True, tag=True):
    """What a dry run (`dry=True`) would do with one file: nothing is downloaded or written.

    `state` is `"new"` (a run would download it) or `"saved"` (it is already in the home).
    For a search, each target found is planned as one event with no post and no path.

    Example:
        >>> import pathlib
        >>> from flnd.source import Target
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
Event = Saved | Skipped | Failed | Progress | Planned

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


class Login(msgspec.Struct, frozen=True):
    """One source's login, as `fl.logins` lists it: whether it is there, and for how long.

    Example:
        >>> login = Login("ins", "Instagram", "logged in", origin="Chrome")
        >>> login.source, login.state
        ('ins', 'logged in')
    """

    # The source's code.
    source: str
    # The site's name, as people know it.
    name: str
    # "logged in", "api key only" or "not logged in".
    state: str
    # Where the login came from: a browser's name, a cookie file's name, or the source's own
    # way ("qr", "phone"); "" when there is none.
    origin: str = ""
    # Each cookie that expires soon, with the seconds it has left (0 or less: expired).
    expiring: tuple[tuple[str, float], ...] = ()


class Installed(msgspec.Struct, frozen=True):
    """One source flnd knows, as `fl.sources` lists it: where it came from, and whether it works.

    A plugin that failed to load or was refused is listed too, with `disabled` saying why, so
    `flnd sources` can show it; its other fields are what its name told (its code, its origin).

    Example:
        >>> row = Installed("exm", "Example", "file", where="/plugins/exm.py", api=1)
        >>> row.origin, row.disabled
        ('file', '')
    """

    # The source's code.
    code: str
    # The site's name, as people know it; the code for a plugin that didn't load.
    name: str
    # "built-in", "package" (an installed package's entry point), "file" (the plugin folder)
    # or "script" (`fl.add`).
    origin: str
    # The module, entry point (`flnd_example:Example`) or file it comes from.
    where: str = ""
    # The package's version (flnd's own for a built-in), or a file plugin's `__version__`;
    # "" when there is none.
    version: str = ""
    # The source API it was written for; 0 for one that didn't load.
    api: int = 0
    # Its login: "logged in", "not logged in" or "needs no login"; "" for one that didn't load.
    login: str = ""
    # Its domains, the target forms it reads, its categories and its ways to log in.
    domains: tuple[str, ...] = ()
    forms: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    logins: tuple[str, ...] = ()
    # Why it is disabled, with the file and line of the failure; "" when it works.
    disabled: str = ""


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
    """A target the home knows: downloaded once, or followed, with what is on disk for it.

    Example:
        >>> from flnd.source import Target
        >>> row = Tracked(Target("ins:example_user", kind="user"), follow=True, files=3)
        >>> row.follow, row.files
        (True, 3)
    """

    target: Target
    # Whether `sync` takes it: True once followed, False after `unfollow`.
    follow: bool = False
    # When it was last downloaded or synced, as an ISO 8601 time; "" when never.
    at: str = ""
    # How many files its folder holds, and their bytes.
    files: int = 0
    size: int = 0
    # For a search: how many targets it found so far.
    seen: int = 0
    # Each category's state: "complete", "interrupted" or "incomplete".
    categories: dict[str, str] = {}
    # Whether it is a search (`<source>?<words>`) rather than one account or post.
    query: bool = False


class Failure(msgspec.Struct, frozen=True):
    """A file or target that failed and isn't saved since, as `fl.status` lists it.

    It is read from `flnd.db`, which keeps one row per failed file: the run it last failed in,
    and how many runs failed it.

    Example:
        >>> import flnd, pathlib
        >>> failure = Failure("ins:example_user", flnd.NetworkError("connection reset"),
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
    # them, `flnd retry` still tries it.
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

def encode(value: Any) -> bytes:
    """One event, report or row as JSON: a path as text, an error as its class, message, hint.

    Args:
        value: anything from this module, or a list of them.

    Returns:
        The JSON, as UTF-8 bytes. An event carries its class's name in `"type"`.

    Example:
        >>> from flnd.source import Target
        >>> encode(Skipped(Target("ins:example_user", kind="user"), "posts", "9001"))[:16]
        b'{"type":"Skipped'
    """
    return msgspec.json.encode(value, enc_hook=_plain)


def _plain(value: Any) -> Any:
    """What `encode` writes for a value msgspec can't: a path, an error.

    Args:
        value: the value msgspec met.

    Returns:
        Text for a path; `{"class", "message", "hint"}` for an exception.

    Raises:
        NotImplementedError: anything else, which msgspec reports as unsupported.
    """
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, BaseException):
        return {"class": type(value).__name__, "message": str(value),
                "hint": getattr(value, "hint", "")}
    raise NotImplementedError(f"can't encode {type(value).__name__}")
