"""What a run reports, and the rows the library reads: events, `Stats`, `Report`, `Login`,
`Record`, `Tracked`, `Checked`, `Status`, `Failure` and `Installed`.

Every type here is a frozen msgspec struct, so it is cheap to make, safe to share between the
run's threads, and encodes to JSON (`encode`) for a script or a cron job. The module imports
msgspec, the errors and `flnd.source` (for `Target`), never the engine, so `flnd.Saved` is
cheap to reach.

Who makes them:
1. the job and the engine make `Saved`, `Skipped`, `Failed`, `Progress` and `Planned` as a run
   goes, and hand them to the run's sink (`flnd._run.Run._emit`);
2. `Run` counts them into a `Report`, and yields them to whoever iterates the run; between
   two, it yields a `Stats` its meter sampled (`flnd._stats`), made of `Host`, `Transfer`,
   `Transfers`, `Listing`, `Pause` and `System` rows, which never pass through the event queue;
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
    # Whether a retry brought it back: an earlier run failed on it (flnddocs/errors).
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


# ---------------------------------------------------------------------------------------------
# Live stats: one snapshot a second while a run is read (`flnd._stats` makes them)
# ---------------------------------------------------------------------------------------------

class Host(msgspec.Struct, frozen=True):
    """One download host's load now: its adaptive cap, and its transfers in flight and waiting.

    Example:
        >>> host = Host("cdn.example.com", cap=8, busy=8, waiting=3, most=32)
        >>> host.busy == host.cap
        True
    """

    # The host's name, lower case.
    host: str
    # The transfers at once it may have now; it adapts between 1 and `most`.
    cap: int = 0
    # The transfers in flight to it.
    busy: int = 0
    # The transfers waiting for one of its places.
    waiting: int = 0
    # The most its cap may grow to.
    most: int = 0


class Transfer(msgspec.Struct, frozen=True):
    """One file downloading now: how far it is, from which host, and how fast.

    Example:
        >>> Transfer("example_user/9001.mp4", "cdn.example.com", done=512, total=2048).total
        2048
    """

    # The file's path under its target's folder.
    name: str
    # The host it comes from; "" for a file the source fetches itself.
    host: str = ""
    # Bytes received so far, counting what an earlier run left in its part.
    done: int = 0
    # The file's size; None while the server hasn't said.
    total: int | None = None
    # Bytes a second over the last sample.
    speed: float = 0.0
    # Whether it went on from a part an earlier run left.
    resumed: bool = False


class Transfers(msgspec.Struct, frozen=True):
    """The run's downloads now: files in flight and queued, and each host's load.

    Example:
        >>> Transfers(running=2, queued=5).queued
        5
    """

    # Files downloading now.
    running: int = 0
    # Files found and queued, not started yet.
    queued: int = 0
    # Files in flight that went on from a part an earlier run left.
    resuming: int = 0
    # Each host's load, the busiest first.
    hosts: tuple[Host, ...] = ()
    # Each file in flight, the oldest first.
    files: tuple[Transfer, ...] = ()


class Listing(msgspec.Struct, frozen=True):
    """One category of one target as it is listed: how far, and whether it stopped early.

    `state` is one of "listing", "finishing" (listed, downloads still going), "done", or how
    the listing stopped early: "caught up" (5 saved posts in a row), "cut" (`limit` reached),
    "stopped" (Ctrl-C or `run.stop()`) or "failed".

    Example:
        >>> from flnd.source import Target
        >>> row = Listing(Target("ins:example_user", kind="user"), "posts", posts=40, found=52)
        >>> row.state, row.found
        ('listing', 52)
    """

    target: Target
    # The category; "" for a target without categories.
    category: str = ""
    # The pages its paged listing (`site.pages`) has read; 0 for a listing that isn't paged.
    pages: int = 0
    # Posts the listing has yielded so far.
    posts: int = 0
    # Files found, saved, skipped and failed so far, as `Progress` counts them.
    found: int = 0
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    # Bytes saved so far.
    bytes: int = 0
    # Already-saved posts in a row: at 5, a sync stops early ("caught up").
    streak: int = 0
    state: str = "listing"


class Pause(msgspec.Struct, frozen=True):
    """A source waiting before its next request to a host, and why.

    `reason` is "Retry-After" (the site said when to come back), "back-off" (a refusal halved
    the pace, or a failure is being tried again), "pace" (the source's human pace between
    requests), or the source's own words for a wait it asked for.

    Example:
        >>> Pause("ins", "www.example.com", 180.0, "Retry-After").seconds
        180.0
    """

    # The source's code.
    source: str
    # The host it waits for; for a wait the source asked for itself, the target.
    host: str
    # Seconds until the next request may go.
    seconds: float
    reason: str


class System(msgspec.Struct, frozen=True):
    """The machine's side of a run: the browser, flnd's RAM and CPU, the media jobs, the disk.

    Example:
        >>> System(tabs=2, cap=6, ram=150_000_000).tabs
        2
    """

    # Browser tabs open now, and the most one target may hold.
    tabs: int = 0
    cap: int = 0
    # The resident memory of the browser's process tree (Playwright's driver and Chromium), in
    # bytes; 0 when no browser runs.
    browser: int = 0
    # flnd's own resident memory, in bytes.
    ram: int = 0
    # flnd's CPU over the last sample, in percent of one core (above 100 on several cores).
    cpu: float = 0.0
    # ffmpeg processes running (joining DASH tracks, compressing video).
    ffmpeg: int = 0
    # Photos compressing in flnd's own process (Pillow).
    pillow: int = 0
    # Archives being extracted.
    unpack: int = 0
    # Files waiting for a compress or unpack step.
    steps: int = 0
    # Free bytes on the volume of the home folder.
    disk: int = 0


class Stats(msgspec.Struct, frozen=True, tag=True):
    """A snapshot of a run, once a second while it is read: totals, downloads, listings, waits,
    and the machine.

    A run yields one about every second while someone iterates it, and `run.stats` holds the
    latest (None before the first). `run.wait()` with nobody iterating samples nothing. It is
    the same snapshot the full-screen view draws, so a script sees what the screen shows.

    Example:
        >>> stats = Stats(elapsed=12.0, saved=30, bytes=90_000_000, speed=7_500_000.0)
        >>> stats.saved, stats.eta, stats.system.tabs
        (30, None, 0)
    """

    # Seconds since the run started.
    elapsed: float = 0.0
    # The run's files so far, as `run.report` counts them.
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    # Bytes a second received over the last sample, every transfer together.
    speed: float = 0.0
    # Seconds left, once every listing is done and files are still to come; None before.
    eta: float | None = None
    # "" while it runs; "soon" after a first Ctrl-C, `q` or `run.stop()` (listing stopped,
    # files in flight finishing); "now" after a second Ctrl-C.
    stopping: str = ""
    # Whether `run.suspend()` holds it: no new request or file starts until `run.resume()`.
    suspended: bool = False
    transfers: Transfers = Transfers()
    # One row per target and category, in the order they began.
    listing: tuple[Listing, ...] = ()
    # Each source waiting before its next request, the longest wait first.
    pauses: tuple[Pause, ...] = ()
    system: System = System()


# Any event a run yields, for a `match` or an annotation.
Event = Saved | Skipped | Failed | Progress | Planned | Stats


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
        from flnd._files import kind_of

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
