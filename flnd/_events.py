"""What a run reports, and the rows the library reads: events, `Report`, `Login`, `Record`.

Every type here is a frozen msgspec struct, so it is cheap to make, safe to share between the
run's threads, and encodes to JSON (`encode`) for a script or a cron job. The module imports
msgspec, the errors and `flnd.source` (for `Target`), never the engine, so `flnd.Saved` is
cheap to reach.

Who makes them:
1. the job and the engine make `Saved`, `Skipped`, `Failed`, `Progress` and `Planned` as a run
   goes, and hand them to the run's sink (`flnd._run.Run._emit`);
2. `Run` counts them into a `Report`, and yields them to whoever iterates the run;
3. `Flnd` makes `Login`, `Record` and `Tracked` from today's stores, for its reads.

The one rule a change must keep: an event holds data, never a live object (a site, a file
handle, a lock), because a script may keep it after the run ends, and `encode` must turn it
into JSON. An error is the one exception: `Failed.error` is the `flnd.Error` itself, so a
script can read its `hint`; `encode` writes its class, message and hint.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Union

import msgspec

# Both are read at runtime, by `typing.get_type_hints` (the API snapshot, a decoder), so they
# are real imports. `flnd.errors` is stdlib-only; `flnd.source` is the light half of flnd.
from flnd.errors import Error
from flnd.source import Target

__all__ = [
    "Event", "Failed", "Login", "Planned", "Progress", "Record", "Report", "Saved", "Skipped",
    "Tracked", "encode",
]


# ---------------------------------------------------------------------------------------------
# Events: what a run yields, in the order it happens
# ---------------------------------------------------------------------------------------------

class Saved(msgspec.Struct, frozen=True, tag=True):
    """A file was downloaded and placed: it is on disk under its final name, with its row.

    Example:
        >>> import datetime, pathlib
        >>> from flnd.source import Target
        >>> event = Saved(Target("ins", "user", "example_user"), "posts", "9001",
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
    # Where the file is now, under the home's `media/` folder.
    path: Path
    # The post's day, which is also the file's modified time; None when the site gave none.
    date: datetime.date | None
    # The file's size in bytes.
    size: int


class Skipped(msgspec.Struct, frozen=True, tag=True):
    """A file or a whole post wasn't downloaded: it is already saved, or another run is saving it.

    Example:
        >>> from flnd.source import Target
        >>> Skipped(Target("ins", "user", "example_user"), "posts", "9001").path is None
        True
    """

    target: Target
    # The category the post came from; "" for a target without categories.
    category: str
    # The post's id; "" when the listing gave the same post twice.
    post: str
    # The file that was skipped; None when the whole post was (it was saved before).
    path: Path | None = None


class Failed(msgspec.Struct, frozen=True, tag=True):
    """A file, or a whole target, failed: the run went on with the rest.

    A failed file has its `post` and `path`. A failed target (a login it needs, a listing the
    site refused) has neither: `error` says why it ended.

    Example:
        >>> import flnd
        >>> from flnd.source import Target
        >>> event = Failed(Target("ins", "user", "example_user"),
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
        >>> step = Progress(Target("ins", "user", "example_user"), "posts", found=3, saved=1)
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


class Planned(msgspec.Struct, frozen=True, tag=True):
    """What a dry run (`dry=True`) would do with one file: nothing is downloaded or written.

    `state` is `"new"` (a run would download it) or `"saved"` (it is already in the home).
    For a search, each target found is planned as one event with no post and no path.

    Example:
        >>> import pathlib
        >>> from flnd.source import Target
        >>> Planned(Target("ins", "user", "example_user"), "9001",
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
Event = Union[Saved, Skipped, Failed, Progress, Planned]


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
    # Bytes downloaded.
    bytes: int = 0
    # Files a dry run planned.
    planned: int = 0
    # Each failed file and failed target, in the order they failed.
    failed: tuple[Failed, ...] = ()
    # Whether `run.stop()` ended the run before it was done.
    stopped: bool = False

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
        from flnd.common.paths import kind_of

        return kind_of(self.path.name)


class Tracked(msgspec.Struct, frozen=True):
    """A target the home knows: downloaded once, or followed, with what is on disk for it.

    Example:
        >>> from flnd.source import Target
        >>> row = Tracked(Target("ins", "user", "example_user"), follow=True, files=3)
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
        >>> encode(Skipped(Target("ins", "user", "example_user"), "posts", "9001"))[:16]
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
