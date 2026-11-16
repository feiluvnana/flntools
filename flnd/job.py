"""The job: one target of a source, from its route to its rows in `flnd.db`.

What it holds: `download`, the one entry for a target; `Board`, the progress rows and the source
of `Progress` events; `Ledger`, which plans each post's files (skip, link, hold for a day,
queue); `Downloads`, which runs the queued files on the run's loop and commits each post's
rows; the listing loop (`_read`, `_list_one`, `_sync_one`, `_retry_failed`) with its early
stops; and `plan`, `place`, `posts` and `discover`, the pieces a dry run and `flnd.testing`
reuse.

Who calls it: the engine (`flnd.engine`), once per target, on the target's listing thread.
What it calls: the source's route and categories through a `TargetSite`, the downloader
(`flnd._transfer`) on the run's loop, the step chain (`flnd._steps`) once a file's row is
written, and the home's `flnd.db` (`flnd._home`).

How one target goes (`download`):
1. Sweep stale parts from the target's folder, and read the target's row: which categories
   were listed to their end, the cursors of cut-short listings, each listing's head (the
   newest post an earlier run saw) and its gaps (where syncs cut short stopped).
2. Call the route, then retry the files earlier runs failed on (`_retry_failed`): each failed
   post is listed again, from the cursor of the page it was on, for fresh URLs, and its failed
   files are queued. A file is retried this way on at most `_home.RETRIES` runs; `flnd retry`
   (`Retry.ONLY`) takes every one and lists nothing else. A gone file (`NotFound`) never is.
3. List the route, or each wanted category (`_list_one`), queueing each post's files that
   aren't saved. The engine owns the early stops, never the source (`_Stops`):
   - a sync of a `newest_first` listing stops after `stop_after` saved posts in a row (5, or
     `[sources.<code>] stop_after` in the home's config.toml), and sooner, after the head and
     one more saved post, once it meets the head an earlier run saw. With nothing new, that
     is one page: one request (a `Skipped` event with the reason "nothing new");
   - a sync whose listing has gaps, once it meets saved posts at the top, jumps to each gap's
     cursor and goes on there, so a sync cut short never leaves posts behind (`_sync_one`);
   - a `newest_first` listing stops after 5 posts in a row older than `--when`;
   - `--limit` cuts it.
4. Wait for the downloads, then keep where each listing stopped: its cursor when a full
   download was cut short, its gaps when a sync was, `complete` when it ran out or caught up,
   and its new head when it was read from the top and left no gap (`Home.progress`).

Targets a listing yields (a post linking to another site) are handed back in the report's
`found`, and the engine downloads them after the run's own targets.

Rules a change must keep:
- A source never decides when a listing stops: it yields, and the job closes its generator.
- A cursor is kept only for a listing cut short; a listing that ran out drops it.
- A head is kept only for a listing read from its newest post, with no gap left: a run that
  resumed from a cursor read the middle of the listing, not its top, and a gap lies above the
  old head, which a later sync with no cursor to jump to reads down to.
- A retry never uses a stored URL: `flnd.db` keeps only a redacted one, and a CDN's signed URL
  expires within hours. It lists the post again, through the source, from `(target, post,
  file)` and the cursor of the post's page.
- Two threads touch a target. The listing thread runs the source, the `Ledger` and
  `Downloads.submit`; each file's task runs on the run's loop. They share only what a lock
  guards: the board's counts (`Board._lock`), the downloads' lists of written and landed files
  (`Downloads._writing`) and its queue count (`Downloads._changed`). A source's code runs on
  the listing thread only, so a task that needs a lazy post's files again asks that thread
  (`_Refresh`), which answers between two files.
- A post's rows commit in one transaction, once its last file settled, and only after each of
  its files is in place: a row never names a file that isn't there. `Saved` comes after the
  commit and the chain's steps, so a script that reads the history on `Saved` finds the row.
"""

from __future__ import annotations

import asyncio
import datetime
import filecmp
import functools
import inspect
import os
import re
import string
import threading
import time
from collections import deque
from collections.abc import Callable, Collection, Coroutine, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import (
    TYPE_CHECKING,
    Any,
)
from urllib.parse import urlsplit

from flnd import _events, _files, _home, _loop, _stats, _text, _transfer
from flnd._events import NOTHING_NEW, RESUMING, Failed, Progress, Saved, Skipped
from flnd._steps import Landed, dedupe, holder, landed, sidecars
from flnd._text import fill
from flnd._time import day
from flnd._ui import ui
from flnd.errors import (
    Blocked,
    Error,
    LoginError,
    NetworkError,
    SiteError,
    SourceError,
    Stopped,
    classify,
)
from flnd.options import Options, Retry, stop_after
from flnd.site import Listing, SourceRun, TargetSite
from flnd.source import Category, File, NotFound, Post, Range, Route, Site, Source, Target
from flnd.sources import SOURCES

if TYPE_CHECKING:
    from flnd._run import Stop

__all__ = [
    "STOPPED", "Board", "Counts", "Downloads", "Ledger", "Placed", "TargetReport",
    "causes", "discover", "download", "failure", "logged_out", "place", "plan", "posts", "tell",
    "wanted",
]

# The statuses that end a listing as a login problem when the site's own domain answers them.
LOGIN_WALLS = frozenset({401, 403})
# The statuses that stop asking a site for dates for the rest of the run.
BLOCKS = LOGIN_WALLS | {429}
# The day a `{date}_` filename template puts in front of a name, which `redate` replaces.
LEADING_DAY = re.compile(r"\d{4}-\d\d-\d\d_")
# The hour (UTC) a file's modified time is set to on its post's day: noon reads as the same day
# in every time zone from UTC-11 to UTC+11.
NOON = 12
# Posts in a row older than `--when` that end a `newest_first` listing: more than the posts a
# profile pins above its newest (Instagram pins up to 3).
OLDER_IN_A_ROW = 5
# Saved posts in a row, the head an earlier run saw counted, that end a sync once it met that
# head: the head itself, and the post after it, which shows the head wasn't pinned above new ones.
HEAD_IN_A_ROW = 2
# Template fields known only once a post's files are listed: a key that names one can't be
# built from the post alone, so a lazy post's files must be listed to tell if it is saved.
LISTED_FIELDS = frozenset({"count", "index", "extension", "path"})
# The extension a video joined with its audio track gets, unless both are WebM.
DASH_EXTENSION = ".mp4"
# The statuses a CDN answers for a signed link that expired; a lazy post is asked again once.
EXPIRED = frozenset({403, 410})
STOPPED = "stopped by Ctrl-C"
FINISHING = "stopping: {files} finishing (Ctrl-C again to stop now)"
# Seconds a waiting thread sleeps between checks of the stop state and the refresh queue: short
# enough that Ctrl-C feels at once, long enough to cost nothing.
WAKE = 0.1
# Seconds an async listing may run without awaiting before it is warned about (`_steps`).
HELD = 10.0
# A target's queue holds this many times its `jobs` files, queued or downloading, before its
# listing waits: enough that a burst of small files never starves the transfers.
QUEUE = 4
# Seconds a post the `date` hook had no day for isn't asked again (30 days), and the key of the
# source's saved state that keeps those posts.
ASKED_TTL = 30 * 24 * 3600
ASKED_KEY = "redate.asked"
# The `--only` value that takes every category.
ALL = "all"
# Posts a retry reads past the cursor of a failed post's page before it gives the post up for
# this run: the post was on that page, and a page holds at most a few dozen, so a hundred
# allows for posts the site inserted since.
RETRY_WINDOW = 100
_FORMATTER = string.Formatter()

RunAsync = Callable[[Coroutine[Any, Any, Any]], Any]


@dataclass(frozen=True)
class Placed:
    """Where one file of a post goes and what it is keyed by, before download."""

    post: Post
    file: File
    num: int
    path: str
    key: str
    kind: str | None
    values: Mapping[str, Any]


@dataclass(frozen=True)
class TargetReport:
    """Summary of one command's execution for a single target.

    `error` is what ended the target: a `flnd.Error` (`str(error)` is its message), `Stopped`
    when Ctrl-C ended it, or None when it finished. A file that failed alone doesn't set it.
    """

    target: Target
    written: tuple[Path, ...]
    skipped: int
    error: Error | Stopped | None = None
    # Targets its listings yielded, such as a post's link to another site, for the engine to
    # download after the run's own targets; each as the source wrote it, not yet resolved.
    found: tuple[Target, ...] = ()


def failure(error: BaseException, target: Target, source: Source | None = None) -> Error:
    """What a failure that ended `target` is, as a `flnd.Error` (see `flnd.errors.classify`).

    One more rule than `classify`'s: a 401 or 403 from the source's own domains that a listing
    let escape is a `LoginError` for that source, since that is what a site's login wall
    answers. The same status from another host (a CDN's expired link) stays a `SiteError`.

    Args:
        error: what ended the target; never a `KeyboardInterrupt` (the caller re-raises those).
        target: the target it ended.
        source: the target's source, whose `domains` tell a login wall; None to skip that rule.

    Returns:
        The `Error`, with `target` set.
    """
    made = classify(error, target=str(target))
    assert isinstance(made, Error), made
    # A bug in a plugin is its author's to fix, not flnd's: the hint says whose code it is.
    plugin = SOURCES.plugin(type(source)) if source is not None else None
    if plugin is not None and isinstance(made, SourceError) and made.__cause__ is error:
        made.hint = (f"a bug in the plugin {plugin.code} ({plugin.where}), not in flnd; "
                     "report it to its author")
    if (source is not None and type(made) is SiteError and made.status in LOGIN_WALLS
            and _on_domains(made, source.domains)):
        login = logged_out(target, f"{source.code}: the site refused the login "
                                   f"(HTTP {made.status})")
        login.__cause__ = made
        return login
    return made


def tell(target: Target, error: Error, emit: Callable[[Any], None]) -> None:
    """Report the failure that ended `target`: a `Failed` event, and debug lines for `-v`.

    The event is what the CLI and a script show; the debug lines add the error's causes.

    Args:
        target: the target it ended.
        error: the failure, as `failure` made it.
        emit: the run's event sink (`RunShared.emit`), which gets one `Failed` event.
    """
    emit(Failed(target, error))
    ui.debug("{target}: {error}", target=str(target), error=error)
    ui.debug("{target}: {chain}", target=str(target), chain=causes(error))


def logged_out(target: Target, message: str | None = None) -> LoginError:
    """The `LoginError` of a target whose source needs a login it doesn't have, or lost.

    Args:
        target: the target that can't go on.
        message: what happened; None for "not logged in". The target isn't in it: it is the
            error's `target`, and `tell` puts it in front.

    Returns:
        A `LoginError` whose hint is the command that logs in again.
    """
    return LoginError(message or "not logged in", hint=f"run flnd login {target.source}",
                      target=str(target))


def _on_domains(error: SiteError, domains: Sequence[str]) -> bool:
    """Whether the response `error` carries came from one of `domains`.

    Args:
        error: a refusal, with the response it came with.
        domains: the source's domains; a host matches one, or is under it.

    Returns:
        True when the response's host is on `domains`, and when there is no response to tell by
        (a browser page's status): a source's own pages are what it mostly asks.
    """
    url = getattr(error.response, "url", None)
    if not isinstance(url, str):
        return True
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


@dataclass
class Counts:
    """One category's counts of files, as its board line and its `Progress` events show them.

    A file moves from `queued` to `downloading`, then to `saved`, `skipped` or `failed`.
    `linked` counts files linked from another category's folder, `later` the links held back
    until the file they point to lands, and `bytes` the bytes saved.
    """

    found: int = 0
    queued: int = 0
    downloading: int = 0
    saved: int = 0
    linked: int = 0
    later: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0

    @property
    def pending(self) -> int:
        """Return files and links not yet finished."""
        return self.queued + self.downloading + self.later


@dataclass
class _Line:
    """One category status line tracked by `Board`."""

    target: Target
    label: str
    counts: Counts
    row: Any = None
    listed: bool = False
    ended: bool = False
    note: str = ""
    # For the live stats (`Board.listing`): posts the listing yielded, saved ones in a row,
    # how it stopped early ("caught up", "cut", "stopped", "failed"; "" when it didn't), and
    # the site's `Listing` of the category, which counts the pages `site.pages` read.
    posts: int = 0
    streak: int = 0
    how: str = ""
    listing: Any = None


# The counts whose change is worth a `Progress` event. A file moving from queued to downloading
# isn't: a script sees it when the file is saved, skipped or fails.
PROGRESS_STEPS = frozenset({"found", "saved", "skipped", "failed", "linked"})


def _ignore(event: Any) -> None:
    """The sink of a run nobody listens to: the event is dropped."""


class Board:
    """Status board drawing per-target, per-category progress in a `ui.Group`.

    It is also where `Progress` events come from: each count that a script cares about
    (`PROGRESS_STEPS`), and each listing that ends, sends the category's counts to `emit`. The
    event is made under the board's lock, so its counts agree, and sent after it is released,
    since a sink may wait for the script to take the event.

    Args:
        rows: the `ui.Group` the rows are drawn in.
        subject_width: the width the targets are padded to; 0 to leave them as they are.
        label_width: the width the categories are padded to; 0 to leave them as they are.
        parent: the search whose found targets share this board's rows; None for a plain run.
        emit: the run's event sink; None drops the events.
    """

    def __init__(
        self, rows: Any, *, subject_width: int = 0, label_width: int = 0,
        parent: Target | None = None, emit: Callable[[Any], None] | None = None,
    ) -> None:
        self.rows, self.widths, self.parent = rows, (subject_width, label_width), parent
        self.emit = emit or _ignore
        self._lock = threading.Lock()
        self._lines: dict[tuple[Target, str], _Line] = {}
        self._child_skipped: dict[Target, int] = {}
        # The run's live stats read the board's rows (`listing`, `queued`) until the run ends.
        _stats.track(_stats.BOARD, self)

    def start(self, target: Target, category: str, *, slots: int = 0,
              listing: Any = None) -> None:
        """Open the category's line as its listing starts, unless already open.

        Args:
            target: the target listed.
            category: its category; "" for a target without categories.
            slots: unused; kept for the callers.
            listing: the category's `flnd.site.Listing`, whose pages the live stats show.
        """
        owner = self.parent or target
        with self._lock:
            line = self._lines.get((owner, category))
            if line is None or line.ended:
                line = self._open(owner, category)
            if listing is not None:
                line.listing = listing

    def seen(self, target: Target, category: str, *, streak: int) -> None:
        """Count one post the listing yielded, and the saved posts in a row so far.

        Args:
            target: the target listed.
            category: its category.
            streak: already-saved posts in a row, toward the early stop.
        """
        with self._lock:
            line = self._lines.get((self.parent or target, category))
            if line is not None:
                line.posts += 1
                line.streak = streak

    def count(self, target: Target, category: str, **steps: int) -> None:
        """Add `steps` to the category's counts, refresh its row, and maybe send `Progress`.

        Called from the listing thread (found, skipped) and from download tasks on the loop
        (downloading, saved, failed), so the counts change under the board's lock. A found
        target of a search counts on the search's line, and its skips are also kept per found
        target, for its own report.

        Args:
            target: the target the files belong to.
            category: its category; "" for a target without categories.
            steps: how much each count of `Counts` moves, such as `saved=1, downloading=-1`.
        """
        owner = self.parent or target
        with self._lock:
            line = self._lines.get((owner, category)) or self._open(owner, category)
            for name, step in steps.items():
                setattr(line.counts, name, getattr(line.counts, name) + step)
            if self.parent is not None and target != self.parent and "skipped" in steps:
                self._child_skipped[target] = self._child_skipped.get(target, 0) + steps["skipped"]
            event = self._progress(owner, category, line) if PROGRESS_STEPS & steps.keys() else None
        self._show(line)
        if event is not None:
            self.emit(event)

    def listed(self, target: Target, category: str, *, how: str = "") -> None:
        """Mark the category's listing complete so its row finishes once nothing is pending.

        Args:
            target: the target listed.
            category: its category.
            how: how it stopped early ("caught up", "cut"); "" when it ran out of posts.
        """
        if self.parent is not None and target != self.parent:
            return
        with self._lock:
            line = self._lines.get((target, category)) or self._open(target, category)
            line.listed = True
            line.how = how
            event = self._progress(target, category, line)
        self._show(line)
        self.emit(event)

    def say(self, target: Target, category: str, note: str) -> None:
        """Send a `Progress` event with a note about how a listing goes ("resuming …").

        Args:
            target: the target.
            category: the listing's category; "" for a route's own posts.
            note: what the event's `note` says.
        """
        owner = self.parent or target
        with self._lock:
            line = self._lines.get((owner, category)) or self._open(owner, category)
            c = line.counts
            event = Progress(owner, category, found=c.found, saved=c.saved + c.linked,
                             skipped=c.skipped, failed=c.failed, bytes=c.bytes,
                             listed=line.listed, note=note)
        self.emit(event)

    def note(self, target: Target, note_text: str) -> None:
        """Attach a status note to each open line for `target`."""
        for line in self._of(target):
            if not line.ended:
                line.note = note_text
                self._show(line)

    def end(self, target: Target, *, failed: bool, how: str = "") -> None:
        """Finalize every open line for `target` as ok or failed.

        Args:
            target: the target.
            failed: whether its open lines end failed.
            how: why, for the live stats: "stopped", or "" for "failed" when `failed`.
        """
        if self.parent is not None and target != self.parent:
            return
        for line in self._of(target):
            if line.ended or line.row is None:
                continue
            line.ended = True
            if failed:
                line.how = how or "failed"
            line.row.fail() if failed else line.row.finish()

    def row(self, target: Target, category: str) -> Any:
        """Return the `ui.Row` for `(target, category)`, or None before it opens."""
        owner = self.parent or target
        with self._lock:
            line = self._lines.get((owner, category))
        return None if line is None else line.row

    def totals(self, target: Target) -> Counts:
        """The counts of every category of `target`, summed.

        Args:
            target: the target; for a search's found target, only its skips are kept apart.

        Returns:
            A new `Counts`; changing it changes nothing on the board.
        """
        total = Counts()
        with self._lock:
            if self.parent is not None and target != self.parent:
                total.skipped = self._child_skipped.get(target, 0)
                return total
            for line in self._lines.values():
                if line.target == target:
                    for name in vars(total):
                        setattr(total, name, getattr(total, name) + getattr(line.counts, name))
        return total

    def listing(self) -> list[_events.Listing]:
        """A snapshot of every line, for the live stats: one `Listing` each, in open order.

        Returns:
            The rows, read under the board's lock; any thread may call it.
        """
        with self._lock:
            return [self._listing(target, category, line)
                    for (target, category), line in self._lines.items()]

    def queued(self) -> int:
        """Files found and queued, not started yet, over every line."""
        with self._lock:
            return sum(line.counts.queued for line in self._lines.values())

    @staticmethod
    def _listing(target: Target, category: str, line: _Line) -> _events.Listing:
        """One line's `Listing`; the caller holds the lock.

        Args:
            target: the line's target (a search's, for the targets it found).
            category: the line's category.
            line: the line.

        Returns:
            The row, with its state: how it stopped, else listing, finishing or done.
        """
        c = line.counts
        if line.how in ("stopped", "failed"):
            state = line.how
        elif not line.listed:
            state = "listing"
        elif c.pending:
            state = "finishing"
        else:
            state = line.how or "done"
        pages = line.listing.read if line.listing is not None else 0
        return _events.Listing(target, category, pages=pages, posts=line.posts, found=c.found,
                       saved=c.saved + c.linked, skipped=c.skipped, failed=c.failed,
                       bytes=c.bytes, streak=line.streak, state=state)

    @staticmethod
    def _progress(target: Target, category: str, line: _Line) -> Progress:
        """The `Progress` event of one line's counts; the caller holds the lock."""
        c = line.counts
        return Progress(target, category, found=c.found, saved=c.saved + c.linked,
                        skipped=c.skipped, failed=c.failed, bytes=c.bytes, listed=line.listed)

    def _open(self, target: Target, category: str) -> _Line:
        """Open a new `ui.Row` for `(target, category)` under the lock."""
        line = _Line(target, category or target.kind, Counts())
        line.row = self.rows.add(self._label(line))
        self._lines[(target, category)] = line
        return line

    def _of(self, target: Target) -> list[_Line]:
        """Return lines belonging to `target` in open order."""
        with self._lock:
            return [line for line in self._lines.values() if line.target == target]

    def _show(self, line: _Line) -> None:
        """Redraw a line's row, and finish it once its listing ended and no file is pending.

        The label and the state are read under the lock; the row is drawn outside it, since a
        row may take the UI's own lock.

        Args:
            line: the line to redraw.
        """
        with self._lock:
            label = self._label(line)
            done = line.listed and not line.counts.pending
            completed = (
                line.counts.saved + line.counts.skipped + line.counts.linked + line.counts.failed
            )
            if done:
                line.ended = True
        if line.row is not None:
            line.row.label = label
            line.row.count = completed
            if done:
                if line.counts.failed:
                    line.row.fail()
                else:
                    line.row.finish()

    def _label(self, line: _Line) -> str:
        """One line's label: target, category, then the counts that aren't zero, and its note.

        Args:
            line: the line.

        Returns:
            Such as `ins:example_user posts | 12 saved | 3 skipped | 4.1 MB`.
        """
        c = line.counts
        subj = str(line.target).ljust(self.widths[0]) if self.widths[0] else str(line.target)
        cat = line.label.ljust(self.widths[1]) if self.widths[1] else line.label
        parts = [f"{subj} {cat}", f"{c.saved} saved"]
        if c.linked:
            parts.append(f"{c.linked} linked")
        if c.skipped:
            parts.append(f"{c.skipped} skipped")
        if c.failed:
            parts.append(f"{c.failed} failed")
        if c.bytes:
            parts.append(_text.size(c.bytes))
        if line.note:
            parts.append(line.note)
        return " | ".join(parts)


def wanted(source: type[Source], route: Route, options: Options) -> tuple[Category, ...]:
    """The categories of `route` that `--only` selects, in the order the source declares them.

    Args:
        source: the source's class (unused; kept for the callers).
        route: the target's route.
        options: the target's options; `categories` None or naming `all` takes every one.

    Returns:
        The categories; a name `--only` gives that the route lacks is left out (`validate`
        refused it before the run).
    """
    names = options.categories
    if names is None or ALL in names:
        return route.categories
    selected = set(names)
    return tuple(category for category in route.categories if category.name in selected)


def posts(
    source: Source, site: Site, target: Target, options: Options, *, run_async: RunAsync
) -> Generator[Post, None, None]:
    """Every post of `target`'s route, or of each wanted category, that `--when` keeps.

    It is the plain listing, with none of the job's stops at saved posts: `flnd.testing` and a
    plan use it. The route is called once, and each category gets what it returned.

    Args:
        source: the target's source.
        site: the `Site` the route and categories are given.
        target: the target.
        options: its options: `--when` and `--only`.
        run_async: runs a coroutine on the run's loop, for an async route or category.

    Yields:
        Each post, filed under its category, in the order the source yields them.
    """
    span = Range(options.since, options.until)
    route = next(r for r in source.routes if r.kind == target.kind)
    categories = wanted(type(source), route, options)
    if route.categories and not categories:
        return
    context = _context(source, route, site, target, run_async)
    if not route.categories:
        yield from _Kept(context, "", newest_first=route.newest_first, span=span,
                         run_async=run_async)
        return
    for category in categories:
        method = getattr(source, category.name)
        yield from _Kept(
            method(site, context), category.name, newest_first=category.newest_first,
            span=span, run_async=run_async,
        )


def _context(
    source: Source, route: Route, site: Site, target: Target, run_async: RunAsync
) -> Any:
    """Call `target`'s route method, awaiting it on the run's loop when it is async.

    A route without categories returns its listing, which is left as it is (an async
    generator is iterated by `_steps`); a route with categories returns its context, the value
    every category gets.

    Args:
        source: the target's source.
        route: its route.
        site: the `Site` the route is given.
        target: the target, whose `value` the route reads.
        run_async: runs a coroutine on the run's loop.

    Returns:
        What the route returned, awaited when it is a coroutine.
    """
    context = getattr(source, route.kind)(site, target.value)
    if route.categories and inspect.iscoroutine(context):
        context = run_async(context)
    return context


def plan(
    run: SourceRun, target: Target, options: Options, *, row: Any = None
) -> Generator[Placed, None, None]:
    """Every file a download of `target` would save, placed, with nothing written (`--dry`).

    A lazy post's files are listed, which may cost a request each. `--limit` counts posts.

    Args:
        run: the source's run.
        target: the target.
        options: its options: `--when`, `--only`, `--limit`.
        row: unused; kept for the callers.

    Yields:
        Each file's `Placed`: its path under the target's folder and its history key.
    """
    site = TargetSite(run, target, time=Range(options.since, options.until))
    route = next(r for r in run.source.routes if r.kind == target.kind)
    categories = {c.name: c for c in route.categories}
    stream = posts(run.source, site, target, options, run_async=run.run_async)
    taken = 0
    try:
        for post in stream:
            if options.limit is not None and taken >= options.limit:
                break
            taken += 1
            files = list(post.files()) if callable(post.files) else list(post.files)
            yield from place(
                run.source, post, files, category=categories.get(post.category),
                route=route, target=target, options=options,
            )
    finally:
        stream.close()
        site.close()


def place(
    source: Source, post: Post, files: Sequence[File], *, category: Category | None = None,
    route: Route | None = None, target: Target | None = None, options: Options | None = None,
) -> list[Placed]:
    """Give each file of `post` its path under the target's folder and its history key.

    The path is the folder template (category, else route, else source) filled with the
    file's fields, then its name (`_resolve_name`). Two files of one post with one name get
    the later one's number added, so neither is skipped as already queued. The key template's
    default is the path itself.

    Args:
        source: the post's source, whose templates apply when its category has none.
        post: the post.
        files: its files, in order; a file's number is its index.
        category: the post's category; None for a route's own posts.
        route: the target's route, whose `folder` comes before the source's.
        target: the target, for the `{source}` and `{target}` fields; None for the source's.
        options: the target's options, for the `{query}` field of a search's found target.

    Returns:
        One `Placed` per file, in order.
    """
    folder = _folder_format(source, route, category)
    filename, key = (_format(source, category, name) for name in ("filename", "key"))
    posted = day(post.date)
    placed: list[Placed] = []
    used: set[str] = set()
    for num, file in enumerate(files):
        extension, kind = _extension(file)
        values = _file_values(
            source, post, file, num, len(files), posted, extension, target, options,
        )
        name = _resolve_name(post, file, filename, values, extension)
        where = fill(folder, values)
        path = "/".join(part for part in (where, name) if part)
        # Files of one post may share a name (`cover.jpg`): a later one takes its number, or it
        # would be skipped as already queued.
        if path in used:
            name = f"{PurePosixPath(name).stem}_{num}{PurePosixPath(name).suffix}"
            path = "/".join(part for part in (where, name) if part)
        used.add(path)
        resolved_key = fill(key, {**values, "path": path}, safe=False) or path
        placed.append(Placed(post, file, num, path, resolved_key, kind, values))
    return placed


def _file_values(
    source: Source, post: Post, file: File, num: int, count: int, posted: datetime.date | None,
    extension: str, target: Target | None, options: Options | None,
) -> dict[str, Any]:
    """The fields a folder, filename or key template of one file may name.

    The post's own `fields` come first, so flnd's names (`post`, `date`, `num`…) win.

    Args:
        source: the post's source; its code is `{source}` when no target is given.
        post: the post.
        file: the file (unused; kept for the callers).
        num: the file's number in the post, from 0.
        count: how many files the post has; `{index}` is "" for a post of one file.
        posted: the post's day; None when the site gave none.
        extension: the file's extension, with its dot; "" before it is known.
        target: the target, for `{source}` and `{target}`; None for "".
        options: the target's options, for `{query}`; None for "".

    Returns:
        The fields, by name.
    """
    code = target.source if target is not None else source.code
    target_val = target.value if target is not None else ""
    query_val = (options.query or "") if options is not None else ""
    return {
        **post.fields, "post": post.id, "date": posted, "category": post.category,
        "num": num, "count": count, "index": "" if count == 1 else f"_{num}",
        "extension": extension, "source": code, "target": target_val, "query": query_val,
    }


def _resolve_name(
    post: Post, file: File, filename: str, values: Mapping[str, Any], extension: str
) -> str:
    """The file's name: from the filename template, or the name the source gave it.

    A file with no name of its own is named by the template, plus its extension. A file the
    source named after the template (with or without the date) keeps the template's form and
    its own suffix. Any other name the source gave is kept, made safe for every file system.

    Args:
        post: the post; its id names the file when the template comes out empty.
        file: the file.
        filename: the filename template.
        values: the file's fields (`_file_values`).
        extension: the file's extension, with its dot.

    Returns:
        The file's name, with no folder.
    """
    formatted = fill(filename, values) or _text.filename(post.id)
    if file.name is None:
        return formatted + extension
    if file.content is None and PurePosixPath(file.name).stem in _stems(filename, values):
        return formatted + PurePosixPath(file.name).suffix
    return _text.filename(file.name)


def _stems(filename: str, values: Mapping[str, Any]) -> set[str]:
    """Return filename stems with and without the post's date."""
    return {fill(filename, values), fill(filename, {**values, "date": None})} - {""}


class Ledger:
    """One target's plan of what to download: which files are new, skipped, linked or held.

    The listing thread hands it each post (`take`); it places the post's files, skips those
    the history or the disk already has, links a file saved in another category's folder, and
    returns the rest for `Downloads`. It also dates posts the site gave no day: a post is held
    until its category ends, then given the day its key's row has, the source's `date` hook's
    answer, or its nearest dated neighbour's in listing order (marked estimated).

    Only the listing thread calls it, so its own state needs no lock.

    Args:
        run: the source's run: its source and settings (the home, the database, the sink).
        site: the target's site, for warnings and the source's saved state.
        target: the target.
        board: the progress rows its counts go to.
        options: the target's options, for `--again` and the templates' fields; None for none.
    """

    def __init__(
        self, run: SourceRun, site: TargetSite, target: Target, board: Board, *,
        options: Options | None = None,
    ) -> None:
        """Initialize a target ledger with its stores, dates, and category state."""
        self.source, self.settings, self.site = run.source, run.settings, site
        self.target, self.board, self._options = target, board, options
        self.folder = _home.target_folder(self.settings.home, target)
        # The home's database; None for a plan with nothing to read (`flnd.dry`).
        self._db = self.settings.db
        # Told to ask again for everything: files recorded gone are asked for once more.
        self._again = options is not None and options.again
        self._route = next((r for r in self.source.routes if r.kind == target.kind), None)
        self._categories = {c.name: c for r in self.source.routes for c in r.categories}
        self._queued: dict[Path | str, str] = {}
        self._dates: dict[str, datetime.date] = {}
        self._waiting: dict[str, Post] = {}
        self._order: list[tuple[str, datetime.date | None]] = []
        self._estimated: set[str] = set()
        self._asking = type(self.source).date is not Source.date
        self._link_warned = False
        self._later: list[tuple[Path, str, str]] = []
        self._asked: dict[str, float] = self._load_asked()
        # Whether the files queued now are a retry's (`_retry_failed`), which their `Saved`
        # events say.
        self.retrying = False

    def take(self, post: Post, *, hold: bool = True) -> tuple[Outcome, list[_Queued]]:
        """Plan one post: the files to download, after skipping what is saved.

        1. Date it (`_dated`): an undated post may be held until its category ends.
        2. A lazy post (files listed only on demand) is checked by its keys first, so a saved
           one costs no request for its files.
        3. Place its files and queue those not saved (`_queue_post_files`).

        Args:
            post: the post, as the listing yielded it.
            hold: whether an undated post may be held for a day; False when it is released.

        Returns:
            What became of the post, and its files to download (empty unless `QUEUED`).
        """
        category = self._categories.get(post.category)
        first = self._first_key(post, category)
        dated = self._dated(post, category, hold=hold)
        if dated is None:
            return Outcome.EMPTY, []
        post = dated
        skipped = self._saved_first(post, category) if post.lazy else None
        if skipped is None and post.lazy and self._unkeyed(category) and self._db is not None \
                and not self._again \
                and self._db.post_saved(_home.key(self.target), post.id, post.category):
            # Its keys are its files' paths, known only once listed (LOIBUS's archives): the
            # post's own rows say it was saved, so its files function, which may cost the
            # account a download from its quota, isn't called again.
            skipped = Outcome.SAVED
        if skipped is not None:
            self._skip(post.category, post.id)
            ui.debug("{post}: already saved", post=post.id)
            return skipped, []
        listed = list(post.files()) if callable(post.files) else list(post.files)
        return self._queue_post_files(post, listed, category, first)

    def _queue_post_files(
        self, post: Post, listed: Sequence[File], category: Category | None, first: str | None
    ) -> tuple[Outcome, list[_Queued]]:
        """Place each file of `post` and queue those not held, writing inline text files now.

        A file with `content` (a caption, a text post) is written at once, never queued. A
        held file is skipped, or linked from another category's folder. Videos go to the end
        of the post's queue, so its small files land first.

        Args:
            post: the post, dated.
            listed: its files.
            category: its category; None for a route's own posts.
            first: its first file's key, for whether its day is estimated; None for none.

        Returns:
            `QUEUED` with the files to download; else `SAVED` when every file was held (and
            not by this run's queue), or `EMPTY`.
        """
        found = 0
        todo: list[_Queued] = []
        for placed in place(
            self.source, post, listed, category=category, route=self._route,
            target=self.target, options=self._options,
        ):
            dest = self.folder.joinpath(*placed.path.split("/"))
            if placed.file.content is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _files.write(dest, placed.file.content)
                continue
            item_key = f"{self.source.code}:{placed.key}"
            held = self._held(dest, item_key)
            if held is not None:
                if dest not in self._queued and item_key not in self._queued:
                    found += 1
                self._skip_held(post, placed.path, dest, item_key, held)
                continue
            self.board.count(self.target, post.category, found=1)
            self._queued.update(dict.fromkeys((dest, item_key), placed.path))
            # The cursor of the page the post is on, which a failure keeps for its retry.
            listing = self.site.listing
            todo.append(_Queued(placed, dest, item_key, estimated=first in self._estimated,
                                cursor=listing.cursor if listing is not None else None,
                                retried=self.retrying))
        if todo:
            todo.sort(key=lambda queued: queued.placed.kind != _files.VIDEO)
            return Outcome.QUEUED, todo
        return (Outcome.SAVED if found else Outcome.EMPTY), []

    def _skip_held(self, post: Post, name: str, dest: Path, item_key: str, held: str) -> None:
        """Count a skipped file and link it when held in another category's folder.

        Args:
            post: the post the file belongs to.
            name: the file's path under the target's folder.
            dest: where the file would go.
            item_key: its history key.
            held: the name of the file that holds the same content.
        """
        category = post.category
        self._skip(category, post.id, dest)
        if held == name:
            ui.debug("skip {name}", name=name)
            return
        ui.debug("skip {name} -> {held}", name=name, held=held)
        if item_key in self._queued:
            self._later.append((dest, item_key, category))
            self.board.count(self.target, category, later=1)
        else:
            candidate = self.folder.joinpath(*held.split("/"))
            self._link(dest, candidate if candidate.is_file() else Path(held), category, item_key)

    def link_queued(self, written: Mapping[str, Path]) -> None:
        """Make the links held back because their file was still queued in this run.

        Called once the downloads finished. The file's row gives its final path, since the
        compress step may have changed its suffix; a file that never landed gets no link.

        Args:
            written: each landed file's path, by history key.
        """
        for dest, item_key, category in self._later:
            holder = written.get(item_key)
            # The compress step may have moved the file to a new suffix; its row says where.
            found = self._db.held(item_key) if holder is not None and self._db else None
            if found is not None and found[0]:
                holder = Path(found[0])
            if holder is not None:
                self._link(dest.with_suffix(holder.suffix), holder, category, item_key)
            self.board.count(self.target, category, later=-1)
        self._later = []

    def _link(self, dest: Path, holder: Path, category: str, item_key: str) -> None:
        """Link `holder` at `dest`, in another category's folder, and record the link.

        The link's row (`Home.link`) carries the holder's hash, so a later compress of the
        holder moves the link with it, and `check` knows the file.

        Args:
            dest: where the file would go in this category's folder.
            holder: the file saved in another category's folder.
            category: this category.
            item_key: the file's key, whose row is the holder's.
        """
        if not holder.is_file() or not _distinct_folders(self.folder, dest, holder):
            return
        if dest.exists() or dest.is_symlink():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not _try_link(dest, holder):
            if not self._link_warned:
                self._link_warned = True
                self.site.warn("can't link files found in two folders here")
            return
        if self._db is not None:
            self._db.link(item_key, dest, category)
        self.board.count(self.target, category, linked=1)
        ui.debug("link {name} -> {held}", name=self._name(dest), held=self._name(holder))

    def release(self) -> list[Post]:
        """Hand back the posts held for a day, as their category ends, each dated if it can be.

        A post's day comes from the listing (its key seen dated later), the source's `date`
        hook, or else its nearest dated neighbour in listing order, marked estimated so a later
        run may date it for real (`redate`).

        Returns:
            The held posts, in the order they were held; one with no day at all stays undated.
        """
        waiting, self._waiting = self._waiting, {}
        nearest = self._nearest_dates() if waiting else {}
        released: list[Post] = []
        for item_key, post in waiting.items():
            found = self._dates.get(item_key) or self._ask(post)
            if found is None and item_key in nearest:
                found = nearest[item_key]
                self._estimated.add(item_key)
            released.append(post if found is None else post.replace(date=found))
        self._order.clear()
        return released

    def redate(self) -> int:
        """Date the target's saved files that have no day, or an estimated one (a sync's start).

        Only for a source with a `date` hook. Each such row's post is asked once; the answer
        renames the file when its filename template starts with `{date}_`, sets its modified
        time, and updates its row. A post the hook had no day for isn't asked again for 30
        days (`ASKED_TTL`).

        Returns:
            How many files were dated.
        """
        if not self._asking:
            return 0
        if self._db is None:
            return 0
        dated = 0
        # A partial index holds only undated rows, so a target with none costs one lookup.
        for row in self._db.undated(_home.key(self.target)):
            if row.post_id in self._asked:
                continue
            found = self._ask(Post(row.post_id))
            if found is None:
                continue
            cat = self._categories.get(row.category)
            filename_tpl = _format(self.source, cat, "filename")
            path = _redated(Path(row.path), found, filename_tpl, estimated=row.estimated)
            if path is None:
                continue
            _set_mtime(path, found)
            self._db.redate(row.key, path, found)
            dated += 1
        self._save_asked()
        if dated:
            self.site.info("{files} dated", files=_text.count(dated, "saved file"))
        return dated

    def _load_asked(self) -> dict[str, float]:
        """The posts the `date` hook had no day for, asked in the last 30 days.

        Returns:
            Each post id with the time it was asked, from the source's saved state; entries
            older than `ASKED_TTL` are dropped.
        """
        now = time.time()
        raw = self.site.state.get(ASKED_KEY)
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): float(v) for k, v in raw.items()
            if isinstance(v, (int, float)) and now - v < ASKED_TTL
        }

    def _save_asked(self) -> None:
        """Persist the 30-day `asked` cache to `site.state` when non-empty."""
        if self._asked:
            self.site.state[ASKED_KEY] = self._asked

    def _ask(self, post: Post) -> datetime.date | None:
        """Ask the source's `date` hook for `post`'s day; a miss isn't asked again for 30 days.

        A refused login, a block, a 401, 403 or 429 stops asking for the rest of the run, with
        one warning; any other site or network error leaves this post undated.

        Args:
            post: the post.

        Returns:
            Its day; None when the hook has none, failed, or asking has stopped.
        """
        if not self._asking:
            return None
        try:
            found = day(self.source.date(self.site, post))
        except (SiteError, NetworkError, LoginError) as error:
            blocked = isinstance(error, (LoginError, Blocked))
            if blocked or getattr(error, "status", None) in BLOCKS:
                self._asking = False
                self.site.warn("dating stopped: {error}; the rest are estimated", error=error)
            else:
                ui.debug("{post}: not dated: {error}", post=post.id, error=error)
            return None
        if found is None:
            self._asked[post.id] = time.time()
        return found

    def _nearest_dates(self) -> dict[str, datetime.date]:
        """For each held undated post, the day of its nearest dated post in listing order.

        Two passes over the order: the first notes each undated post's nearest dated one
        before it, the second the nearest after it; the closer wins, the earlier on a tie.

        Returns:
            The day, by the undated post's key; a post with no dated neighbour is left out.
        """
        n = len(self._order)
        left: list[tuple[int, datetime.date] | None] = [None] * n
        last: tuple[int, datetime.date] | None = None
        for i, (_, known) in enumerate(self._order):
            if known is not None:
                last = (i, known)
            else:
                left[i] = last
        nearest: dict[str, datetime.date] = {}
        nxt: tuple[int, datetime.date] | None = None
        for i in range(n - 1, -1, -1):
            item_key, known = self._order[i]
            if known is not None:
                nxt = (i, known)
                continue
            prev = left[i]
            if prev is not None and (nxt is None or i - prev[0] <= nxt[0] - i):
                nearest.setdefault(item_key, prev[1])
            elif nxt is not None:
                nearest.setdefault(item_key, nxt[1])
        return nearest

    def _dated(self, post: Post, category: Category | None, *, hold: bool) -> Post | None:
        """Give `post` a day if one is known, or hold it until its category ends.

        A post whose first key can't be built from its fields alone (`_first_key`) can't be
        matched later, so it goes on as it is. A day comes from the post, an earlier post of
        the same key in this run, or the key's row in `flnd.db`.

        Args:
            post: the post.
            category: its category, whose key template applies.
            hold: whether it may be held; False when it is released, so it goes on undated.

        Returns:
            The post, dated when a day was found; None when it is held (or a repeat of a held
            post, which is skipped).
        """
        item_key = self._first_key(post, category)
        if item_key is None:
            return post
        known = day(post.date)
        if hold:
            self._order.append((item_key, known))
        if known is not None:
            self._dates[item_key] = known
            self._waiting.pop(item_key, None)
            return post
        found = self._dates.get(item_key)
        if found is None and self._db is not None:
            found = self._db.day(item_key)
        if found is not None:
            self._dates[item_key] = found
            return post.replace(date=found)
        if not hold:
            return post
        if item_key in self._waiting:
            self._skip(post.category, post.id)
            ui.debug("{post}: listed twice", post=post.id)
            return None
        self._waiting[item_key] = post
        return None

    def _first_key(self, post: Post, category: Category | None) -> str | None:
        """The history key of `post`'s first file, from the post's fields alone.

        Args:
            post: the post.
            category: its category, whose key template applies.

        Returns:
            `<code>:<key>`; None when the key template needs what only listing the files gives
            (`LISTED_FIELDS`) or the day, or fills to nothing.
        """
        template = _format(self.source, category, "key")
        if not template or _fields(template) & (LISTED_FIELDS | {"date"}):
            return None
        values = {**post.fields, "post": post.id, "category": post.category, "num": 0}
        built = fill(template, values, safe=False)
        return f"{self.source.code}:{built}" if built else None

    def _unkeyed(self, category: Category | None) -> bool:
        """Whether a file's key needs its listed path: no key from the post's fields alone."""
        template = _format(self.source, category, "key")
        return not template or bool(_fields(template) & LISTED_FIELDS)

    def _saved_first(self, post: Post, category: Category | None) -> Outcome | None:
        """Whether a lazy post's files are all saved, judged by their keys, before listing them.

        The first file's row holds the post's count of files; each later key is checked too.

        Args:
            post: the lazy post.
            category: its category, whose key template applies.

        Returns:
            `SAVED` when every file is held; `EMPTY` when one is queued in this run already;
            None when it can't be told without listing the files.
        """
        template = _format(self.source, category, "key")
        if not template or _fields(template) & LISTED_FIELDS:
            return None
        values = {**post.fields, "post": post.id, "date": day(post.date),
                  "category": post.category}
        keys = [
            f"{self.source.code}:{fill(template, {**values, 'num': num}, safe=False)}"
            for num in range(2)
        ]
        if keys[0] == f"{self.source.code}:" or self._held(None, keys[0]) is None:
            return None
        count = self._db.files_of_post(keys[0]) if self._db is not None else None
        checked = [keys[0]]
        if isinstance(count, int) and count > 1 and keys[1] != keys[0]:
            checked += [
                f"{self.source.code}:{fill(template, {**values, 'num': num}, safe=False)}"
                for num in range(1, count)
            ]
            if any(self._held(None, k) is None for k in checked[1:]):
                return None
        return Outcome.EMPTY if any(k in self._queued for k in checked) else Outcome.SAVED

    def _skip(self, category: str, post: str, path: Path | None = None) -> None:
        """Count one file (or post) found and skipped, and send its `Skipped` event.

        Args:
            category: the category whose status line counts it.
            post: the post's id.
            path: the file skipped; None when the whole post was.
        """
        self.board.count(self.target, category, found=1, skipped=1)
        self.settings.emit(Skipped(self.target, category, post, path))

    def _held(self, dest: Path | None, item_key: str) -> str | None:
        """The name of what holds a file already: queued, on disk, saved, or gone.

        A file the site answered 404 or 410 for is recorded gone, and held by its planned name,
        so it isn't asked for again unless the run was told to (`again`). An archive removed
        once extracted is held by the folder it was extracted to. The disk is checked before
        the database, since a file there with bytes is saved whatever the rows say.

        Args:
            dest: where the file would go; None to check by key only.
            item_key: its history key.

        Returns:
            The holder's name (relative to the target's folder when inside it); None when the
            file is new.
        """
        for known in (dest, item_key):
            if known is not None and known in self._queued:
                return self._queued[known]
        if dest is not None and dest.is_file() and dest.stat().st_size > 0:
            return self._name(dest)
        found = self._db.held(item_key) if self._db is not None else None
        if found is None:
            return None
        path, state, folder = found
        if state == _home.GONE:
            return None if self._again else self._name(Path(path))
        if Path(path).is_file():
            return self._name(Path(path))
        return self._name(Path(folder)) if folder and Path(folder).is_dir() else None

    def _name(self, path: Path) -> str:
        """A path as the debug lines and links name it.

        Args:
            path: a path.

        Returns:
            It relative to the target's folder, with `/`, when inside it; else in full.
        """
        return (
            path.relative_to(self.folder).as_posix()
            if path.is_relative_to(self.folder)
            else str(path)
        )


def _distinct_folders(root: Path, dest: Path, holder: Path) -> bool:
    """Whether two files sit in different category folders of one target.

    Only then is one linked from the other: two names in one folder are the same file twice.

    Args:
        root: the target's folder.
        dest: where the link would go.
        holder: the file saved already.

    Returns:
        True when both are under `root`, at least one folder deep, in different first folders.
    """
    if not (dest.is_relative_to(root) and holder.is_relative_to(root)):
        return False
    here = dest.relative_to(root).parts
    there = holder.relative_to(root).parts
    return len(here) >= 2 and len(there) >= 2 and here[0] != there[0]


def _try_link(dest: Path, holder: Path) -> bool:
    """Link `dest` to `holder`: a relative symlink, else a hard link where symlinks fail.

    Windows refuses a symlink without developer mode, and some file systems (FAT, some network
    shares) refuse both.

    Args:
        dest: the link's path; its folder exists.
        holder: the file it points to.

    Returns:
        Whether a link was made.
    """
    try:
        dest.symlink_to(os.path.relpath(holder, dest.parent))
        return True
    except OSError:
        try:
            os.link(holder, dest)
            return True
        except OSError:
            return False


class Downloads:
    """One target's downloads: a coroutine per file on the run's loop, fed by the listing.

    1. The listing thread `submit`s each file. The queue holds `QUEUE` times the source's
       `jobs` files (queued or downloading); past that, `submit` waits, so a listing never runs
       far ahead of its downloads and memory stays flat.
    2. Each file becomes a task on the run's loop (`flnd._transfer.save`), which waits for one
       of the target's `jobs` places; the source's per-host caps (`flnd.site`), shared by
       every target, bound the transfers to each host on top of that.
    3. A file that lands is recorded in history on the loop, then handed to the run's step
       chain (`flnd._steps`), which unpacks archives and, under `--compress`, compresses media
       on its own threads.
    4. An expired link of a lazy post is asked for again on the listing thread, which answers
       between two files (`_answer`), since a source's listing runs only there.

    Args:
        run: the source's run: its settings, clients and loop.
        target: the target the files belong to.
        board: the status lines counts go to.
        compress: whether the target's files go through the compress step.
    """

    def __init__(
        self, run: SourceRun, target: Target, board: Board, *, compress: bool = False,
    ) -> None:
        self.run, self.target, self.board = run, target, board
        # Whether this target's files go through the compress step once recorded.
        self.compress = compress
        self.source, self.settings = run.source, run.settings
        # Set once the listing failed, so a file that hasn't started ends at once.
        self._halted = False
        # The target's `jobs` places, made on the loop on first use.
        self._slots: asyncio.Semaphore | None = None
        self._written: dict[str, Path] = {}
        # Each post's files submitted and not settled yet, and those that landed meanwhile: a
        # post's rows commit together when its last file settles (`_settle`).
        self._open: dict[str, int] = {}
        self._landed: dict[str, list[_Landed]] = {}
        self._writing = threading.Lock()
        self._changed = threading.Condition()
        self._pending = 0
        self._running = 0
        self._refreshes: deque[_Refresh] = deque()
        self._answering = True

    @property
    def written(self) -> dict[str, Path]:
        """Return each file written so far by its history key."""
        with self._writing:
            return dict(self._written)

    def submit(self, queued: _Queued) -> bool:
        """Start one file's download on the loop once the queue has room.

        Called on the listing thread; it waits while the queue is full, answering the
        refreshes download tasks ask for meanwhile. The post's count of open files goes up
        first, so its rows commit only once its last file settles.

        Args:
            queued: the file, placed, with its destination and key.

        Returns:
            True once it is queued; False when the run was told to stop, and nothing was.
        """
        if not self._room():
            return False
        self._count(queued, queued=1)
        with self._writing:
            post = queued.placed.post.id
            self._open[post] = self._open.get(post, 0) + 1
        try:
            # The task never raises (see `_task`), so its future isn't kept.
            self.run.loop.submit(self._task(queued))
        except BaseException:
            self._count(queued, queued=-1)
            self._settle(queued)
            self._ended()
            raise
        return True

    def wait(self) -> None:
        """Wait on the listing thread until every submitted download ends, answering refreshes.

        After the first Ctrl-C, the target's rows say how many files are finishing.

        Raises:
            KeyboardInterrupt: the second Ctrl-C (`stop.now`); the running files stop through
                the client's own stop.
        """
        shown: str | None = None
        while True:
            self._answer()
            with self._changed:
                if self.settings.stop.now.is_set():
                    raise KeyboardInterrupt
                if not self._pending:
                    return
                note_text = (
                    FINISHING.format(files=_text.count(self._running, "file"))
                    if self.settings.stop.soon.is_set()
                    else None
                )
                if note_text == shown:
                    self._changed.wait(WAKE)
                    continue
            if note_text is not None:
                self.board.note(self.target, note_text)
            shown = note_text

    def finish(self, error: BaseException | None = None) -> tuple[Path, ...]:
        """Wait for the downloads (or, after `error`, cancel them) and return what landed.

        Args:
            error: what ended the listing; the files still queued are dropped, and the running
                ones finish.

        Returns:
            The paths written, in the order they landed.
        """
        if error is None:
            self.wait()
        else:
            self._cancel_refreshes()
            # The files still queued are dropped; the running ones finish (a second Ctrl-C
            # stops them through the client's stop), so what they fetched isn't lost.
            self._halted = True
            with self._changed:
                while self._pending:
                    self._changed.wait(WAKE)
        # Tasks a stop cancelled never settled: the files of their posts that did land are in
        # place, so their rows are written now.
        with self._writing:
            left, self._landed, self._open = list(self._landed.values()), {}, {}
        for items in left:
            self._commit(items)
        with self._writing:
            return tuple(self._written.values())

    def _cancel_refreshes(self) -> None:
        """Stop answering refresh requests so waiting downloads fail fast."""
        with self._changed:
            self._answering = False
            waiting, self._refreshes = list(self._refreshes), deque()
        for refresh in waiting:
            refresh.done.set()

    def _room(self) -> bool:
        """Wait for room in the target's queue (`QUEUE` times `jobs`), answering refreshes.

        A place taken here is given back by `_ended` when the file's task ends.

        Returns:
            True with a place taken; False once the run was told to stop.
        """
        limit = QUEUE * max(self.run.jobs, 1)
        while True:
            self._answer()
            with self._changed:
                if self.settings.stop.soon.is_set():
                    return False
                if self._pending < limit:
                    self._pending += 1
                    return True
                self._changed.wait(WAKE)

    def _ended(self) -> None:
        """Mark one submitted download finished."""
        with self._changed:
            self._pending -= 1
            self._changed.notify_all()

    def _count(self, file: _Queued, /, **steps: int) -> None:
        """Update counts on the category's status line."""
        self.board.count(self.target, file.placed.post.category, **steps)

    async def _task(self, queued: _Queued) -> bool:
        """One file's task on the loop: its download, counted, and never an exception out.

        A `KeyboardInterrupt` (the second Ctrl-C, `Stopped`) escaping a task would end the
        loop's thread, so whatever the download raises is logged here, and the listing thread
        sees the stop through the run's events. However it ends, the file settles (its post's
        rows may commit) and its queue place is given back.

        Args:
            queued: the file.

        Returns:
            Whether the file landed.
        """
        started = False
        try:
            if self._slots is None:
                self._slots = asyncio.Semaphore(max(self.run.jobs, 1))
            # A file waits here, queued, for one of the target's `jobs` places; the first
            # Ctrl-C drops the ones still waiting and lets the running ones finish.
            async with self._slots:
                with self._changed:
                    started = not (self._halted or self.settings.stop.soon.is_set())
                    self._running += started
                if not started:
                    return False
                self._count(queued, queued=-1, downloading=1)
                try:
                    return await self._download(queued)
                finally:
                    with self._changed:
                        self._running -= 1
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            ui.error("{file}: {kind} {error}", file=queued.placed.path,
                     kind=type(error).__name__, error=error)
            ui.debug("{file}: {chain}", file=queued.placed.path, chain=causes(error))
            return False
        finally:
            if not started:
                self._count(queued, queued=-1)
            try:
                self._settle(queued)
            finally:
                self._ended()

    def _settle(self, queued: _Queued) -> None:
        """Count one submitted file of a post as done; the post's last one commits its rows.

        Runs on the loop, as the file's task ends. The count and the landed list change under
        `_writing`; the commit runs outside it, since `flnd.db` has its own lock.

        Args:
            queued: the file whose task ended, saved, failed or never started.
        """
        post = queued.placed.post.id
        with self._writing:
            left = self._open.get(post, 1) - 1
            if left > 0:
                self._open[post] = left
                return
            self._open.pop(post, None)
            items = self._landed.pop(post, [])
        self._commit(items)

    def _commit(self, items: list[_Landed]) -> None:
        """Write one post's landed files in one transaction, then count and report each.

        Each file is already in place, so a row is never written before its file. The `Saved`
        events follow the commit and the chain's steps, so a script that reads `fl.files()` on
        one finds its row, at the file's final path.

        Args:
            items: the post's files in place; empty does nothing.

        Raises:
            DiskError: the rows can't be written; the files count as failed.
        """
        if not items:
            return
        rows = [item.row for item in items]
        try:
            if self.settings.db is not None:
                self.settings.db.save(rows)
        except BaseException:
            with self._writing:
                for item in items:
                    self._written.pop(item.queued.key, None)
            for item in items:
                self._count(item.queued, downloading=-1, failed=1)
            raise
        metadata = sidecars(self.settings)
        for item in items:
            self._count(item.queued, downloading=-1, saved=1, bytes=item.row.size)
            placed = item.queued.placed
            event = Saved(self.target, placed.post.category, placed.post.id, item.path,
                          item.day, item.row.size, retried=item.queued.retried)
            # The steps after the row: unpack and compress on the chain's own threads, then
            # the sidecar, then `Saved` with the file's final path (`flnd._steps`).
            landed(self.settings, Landed(
                item.queued.key, item.path, password=placed.file.password,
                compress=self.compress, hash=item.row.hash, event=event,
                meta=_metadata(self.source, self.target, placed) if metadata else None))

    async def _download(self, queued: _Queued) -> bool:
        """Download one file into place, on the loop, and hold its row for its post's commit.

        1. Fetch it (`_fetch`); a lazy post's expired link is asked for again once.
        2. A file another run is saving is skipped; a site or network failure fails this file
           alone, recorded in `flnd.db` for a later retry.
        3. Dedupe: a file whose bytes are saved already becomes a hard link to them.
        4. Hold its row until its post's last file settles (`_downloaded`).

        Args:
            queued: the file.

        Returns:
            Whether it landed; False for a skip, a failure, or a stop after the first Ctrl-C.

        Raises:
            KeyboardInterrupt: the second Ctrl-C.
            DiskError: the file can't be written; it is recorded failed first.
        """
        placed = queued.placed
        received: dict[str, str] = {}
        try:
            saved = await self._fetch(queued, placed.file, received)
            if saved is None and queued.refreshed is not None:
                refreshed = await self._expired(queued)
                saved = (await self._fetch(queued, refreshed, received)
                         if refreshed is not None else None)
        # Another run holds this file's part: it saves the file and writes its row, so this run
        # skips it (docs/storage, "Sharing a home").
        except _files.BusyError:
            self._count(queued, downloading=-1, skipped=1)
            self.settings.emit(Skipped(self.target, placed.post.category, placed.post.id,
                                       queued.dest))
            ui.info("{name}: being saved by another run", name=placed.path)
            return False
        # A file the site or the network failed fails alone; the run goes on (docs/errors).
        except (SiteError, NetworkError) as error:
            self._record_failed(queued, error)
            self._count(queued, downloading=-1, failed=1)
            self.settings.emit(Failed(self.target, error, post=placed.post.id, path=queued.dest))
            ui.debug("{name}: {error}", name=placed.path, error=error)
            ui.debug("{name}: {chain}", name=placed.path, chain=causes(error))
            return False
        except (KeyboardInterrupt, asyncio.CancelledError):
            self._count(queued, downloading=-1)
            if self.settings.stop.soon.is_set() and not self.settings.stop.now.is_set():
                return False
            raise
        except BaseException as error:
            self._record_failed(queued, error)
            self._count(queued, downloading=-1, failed=1)
            raise
        if saved is None:
            self._count(queued, downloading=-1)
            return False
        row = self._row(queued, saved)
        # The dedupe step: a file whose bytes are already saved becomes a hard link to them.
        # The lookup is one indexed query; the link is file-system work, kept off the loop.
        found = holder(self.settings, row)
        if found is not None:
            await asyncio.to_thread(dedupe, self.settings, row, found)
        self._downloaded(queued, saved, received, row)
        return True

    def _downloaded(
        self, queued: _Queued, saved: _files.Saved, received: Mapping[str, str],
        row: _home.FileRow,
    ) -> None:
        """Hold a landed file's row until its post's last file settles, which commits it.

        It stays "downloading" on the board until then, which is one board update less.

        Args:
            queued: the file's queue entry.
            saved: where it landed, with its size and modified time from the landing.
            received: the response's headers; `Last-Modified` dates a post the site gave
                no day.
            row: its row, after dedupe.
        """
        post = queued.placed.post
        dated = day(post.date) or _transfer.served_day(received.get("last-modified"))
        with self._writing:
            self._written[queued.key] = saved.path
            self._landed.setdefault(post.id, []).append(_Landed(queued, saved.path, dated, row))

    def _row(self, queued: _Queued, saved: _files.Saved) -> _home.FileRow:
        """The `files` row of a landed file.

        Args:
            queued: the file's queue entry.
            saved: where it landed, with its size and modified time.

        Returns:
            The row; only a post's first file of several holds the post's count.
        """
        placed = queued.placed
        posted = day(placed.post.date)
        count = placed.values.get("count", 1)
        return _home.FileRow(
            queued.key, _home.key(self.target), str(saved.path), saved.size, saved.mtime,
            post_id=placed.post.id, category=placed.post.category,
            date=posted.isoformat() if posted is not None else None,
            estimated=queued.estimated, kind=_files.kind_of(saved.path.name), at=_home.now(),
            files=count if placed.num == 0 and isinstance(count, int) and count > 1 else None,
            hash=saved.hash,
        )

    async def _fetch(
        self, queued: _Queued, file: File, received: dict[str, str]
    ) -> _files.Saved | None:
        """Save `file` into its part and onto its name (`flnd._transfer.save`).

        A file the source fetches itself (a callable `url`) takes a place under the source's
        own gate, since the client has no host to cap it by. A 403 or 410 for a lazy post's
        file, not refreshed yet, is an expired link: it is kept on `queued.refreshed`, and the
        caller asks for a fresh one.

        Args:
            queued: the file's queue entry.
            file: the file to fetch: the listed one, or its refreshed copy.
            received: filled with the response's headers.

        Returns:
            Where it landed; None when it has no URL or its link expired.

        Raises:
            SiteError: the site refused it (an expired link a second time included).
            NetworkError: the transfer failed after its retries.
        """
        placed = queued.placed
        if file.url is None:
            return None
        saving = _transfer.save(
            self.run.files, file, queued.dest, label=placed.path,
            headers=placed.post.headers, kind=placed.kind, received=received,
            day=day(placed.post.date),
        )
        try:
            if callable(file.url):
                # A file the source fetches itself has no host for the client to cap, so it
                # takes a place under its source's first domain.
                async with self.run.gate():
                    return await saving
            return await saving
        except SiteError as error:
            if error.status not in EXPIRED or not callable(placed.post.files) or queued.refreshed:
                raise
            ui.info("{name}: HTTP {status}, asking for a fresh link",
                    name=placed.path, status=error.status)
            queued.refreshed = error
            return None

    async def _expired(self, queued: _Queued) -> File | None:
        """Ask the listing thread for a lazy post's files again, for a fresh copy of one link.

        A source's listing runs only on its thread, so the task queues a `_Refresh` there and
        waits for it (`_dispatch_refresh`).

        Args:
            queued: the file whose link expired; `refreshed` holds the refusal.

        Returns:
            The fresh file, at the same number in the post.

        Raises:
            SiteError: the refusal again, when no fresh link came (the listing failed, or the
                post has fewer files now, or the file became inline content).
        """
        refusal = queued.refreshed
        assert refusal is not None
        refresh = _Refresh(queued.placed.post, self.settings.stop)
        await self._dispatch_refresh(refresh)
        if refresh.error is not None:
            ui.warn("{name}: no fresh link: {error}", name=queued.placed.path, error=refresh.error)
            raise refusal
        num = queued.placed.num
        fresh = (
            refresh.files[num]
            if refresh.files is not None and num < len(refresh.files)
            else None
        )
        if fresh is None or fresh.content is not None:
            raise refusal
        return fresh

    async def _dispatch_refresh(self, refresh: _Refresh) -> None:
        """Queue a refresh onto the listing thread and wait, on the loop, for it to be done.

        Once the listing ended (`_cancel_refreshes`), the refresh is done at once, with no
        files, so the task fails fast instead of waiting for an answer that won't come.

        Args:
            refresh: the request; `done` is set once it is answered.

        Raises:
            KeyboardInterrupt: the second Ctrl-C came while waiting.
        """
        with self._changed:
            if self._answering:
                self._refreshes.append(refresh)
                self._changed.notify_all()
            else:
                refresh.done.set()
        # A threading event can't wake a coroutine, so it is polled.
        while not refresh.done.is_set():
            if self.settings.stop.now.is_set():
                raise KeyboardInterrupt
            await asyncio.sleep(WAKE)

    def _answer(self) -> None:
        """Process pending link refreshes on the lister thread."""
        while True:
            with self._changed:
                if not self._refreshes:
                    return
                refresh = self._refreshes.popleft()
            refresh.run()

    def _record_failed(self, queued: _Queued, error: BaseException) -> None:
        """Write a file's failure to `flnd.db`; a 404 or 410 records the file as gone.

        Args:
            queued: the file's queue entry.
            error: what failed it.
        """
        db = self.settings.db
        if db is None:
            return
        sampled(self.settings, error)
        placed = queued.placed
        url = placed.file.url if isinstance(placed.file.url, str) else ""
        db.fail(run=self.settings.run, item=queued.key, target=_home.key(self.target),
                error=error, post=placed.post.id, category=placed.post.category,
                file=str(queued.dest), url=url, cursor=queued.cursor)


def sampled(settings: Any, error: BaseException) -> None:
    """Keep a `Changed` error's answer as a scrubbed sample, and point its hint at it.

    Called before the failure is written and its `Failed` event sent, so both name the sample.

    Args:
        settings: the run's `RunShared`: its home and run id.
        error: what failed; anything but a `Changed` with a response is left as it is.
    """
    path = _home.sample(settings.home, settings.run, error)
    if path is not None and isinstance(error, Error):
        saved = f"the answer is saved, scrubbed, in {path}"
        error.hint = f"{error.hint}; {saved}" if error.hint else saved


def download(run: SourceRun, target: Target, board: Board, options: Options) -> TargetReport:
    """Download one target of a source, counting progress on `board` (see the module's steps).

    Called by the engine on the listing thread, inside the source's run. A failure of the
    listing fails the target alone: it is in the report, never raised.

    Args:
        run: the source's run: its session, clients, loop and settings.
        target: the target, resolved.
        board: the progress rows its counts go to.
        options: its options: `--when`, `--only`, `--limit`, `--again`, sync and retry.

    Returns:
        Its report: the files written, the skips, the targets its listings yielded, and the
        error that ended it (`Stopped` when the run was stopped before it began).
    """
    if run.settings.stop.soon.is_set():
        return TargetReport(target, (), 0, error=Stopped(STOPPED))
    # No lock on the target: each file's part locks itself, so two runs may share a target and
    # the sweep passes over the parts another run holds.
    _files.sweep(_home.target_folder(run.settings.home, target))
    return _run_download(run, target, board, options)


def _run_download(
    run: SourceRun, target: Target, board: Board, options: Options
) -> TargetReport:
    """Run one target from its listing to its rows: the steps of the module docstring.

    1. Make its ledger and downloads, and read its row (what earlier runs left).
    2. List it and wait for its downloads (`_execute_listing`), then make the held links.
    3. Keep its listings' cursors, heads, gaps and completeness (`Home.progress`); a found
       target of a search keeps nothing, since the search's row keeps what it found.
    4. A target that ran to its end clears its whole-target failure and recounts its files.
    5. Report it, and end its rows (`_ended`).

    Args:
        run: the source's run.
        target: the target.
        board: the progress rows.
        options: its options.

    Returns:
        Its report.
    """
    started = time.monotonic()
    site = TargetSite(run, target, time=Range(options.since, options.until))
    ledger = Ledger(run, site, target, board, options=options)
    downloads = Downloads(run, target, board, compress=options.compress)
    work = _Work(ledger, downloads)
    db = run.settings.db
    row = db.target(target) if db is not None else None
    progress = _Progress(row, again=options.again)
    error = _execute_listing(work, run, site, target, options, progress)
    written = downloads.finish(error)
    ledger.link_queued(downloads.written)
    # A target a search found has no row of its own: the search's row keeps what it found.
    if db is not None and not options.query:
        db.progress(target, cursors=progress.cursors(), complete=progress.complete,
                    heads=progress.heads, gaps=progress.gaps)
    if db is not None and error is None and not run.settings.stop.soon.is_set():
        # A failure of the whole target is over once it ran to its end.
        db.clear(_home.key(target))
        # Counting reads every row of the target (35 ms for 100,000), so a run that saved
        # nothing keeps the count it has, unless it has none yet.
        if written or row is None or not row.files:
            db.tally(target)
    report = TargetReport(target, written, board.totals(target).skipped,
                          found=tuple(dict.fromkeys(work.linked)))
    return _ended(
        run, board, report, error, complete=", ".join(progress.complete) or "none",
        started=started,
    )


class _Progress:
    """Where a target's listings stood after earlier runs, and where this run leaves them.

    Args:
        row: the target's row in `flnd.db`; None for a target the home doesn't know yet.
        again: list every post again: the earlier runs' cursors, heads and completeness are
            ignored (they are still updated by this run).
    """

    def __init__(self, row: _home.TargetRow | None, *, again: bool) -> None:
        earlier = row if row is not None and not again else None
        # Each listing an earlier run read to its end, with the day it did.
        self.listed: Mapping[str, str] = earlier.complete if earlier is not None else {}
        # The cursor each cut-short listing was reading.
        self.started: Mapping[str, str] = earlier.cursors if earlier is not None else {}
        # The newest post each listing had when an earlier run read it from the top.
        self.seen_heads: Mapping[str, str] = earlier.heads if earlier is not None else {}
        # Where earlier syncs of each listing were cut short, newest first.
        self._gaps: Mapping[str, list[str]] = earlier.gaps if earlier is not None else {}
        # This run's listings, by name ("" for a route's own), the ones it read to their end,
        # their heads, and the gaps the syncs leave.
        self.listings: dict[str, Listing] = {}
        self.complete: list[str] = []
        self.heads: dict[str, str] = {}
        self.gaps: dict[str, list[str]] = {}

    def gaps_of(self, name: str) -> list[str]:
        """A listing's gaps from earlier syncs, newest first.

        A cursor kept for a listing already read to its end is a gap too: a sync cut short,
        from a flnd that kept it as the listing's cursor.

        Args:
            name: the listing's name; "" for a route's own posts.

        Returns:
            The gaps' cursors; "" for a gap with no cursor.
        """
        found = list(self._gaps.get(name, ()))
        old = self.started.get(name)
        if old and name in self.listed and old not in found:
            found.append(old)
        return found

    def cursors(self) -> dict[str, str | None]:
        """Each listing's cursor to keep: the page it was reading, or None when it ran out.

        A sync keeps gaps instead (`gaps`), so its listing's cursor is dropped.

        Returns:
            Each listing's cursor by name, as `Home.progress` takes them.
        """
        return {
            name: None if name in self.complete or name in self.gaps else listing.cursor
            for name, listing in self.listings.items()
        }


def _execute_listing(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    progress: _Progress,
) -> BaseException | None:
    """List the target, then wait for its downloads; any error that ended it is returned.

    Args:
        work: the target's ledger and downloads, which queue each post's files.
        run: the source's resources for the run.
        site: the target's site; its `listing` is set while a listing runs.
        target: the target.
        options: the target's options: `--when`, `--only`, `--limit`, `--again`.
        progress: where the earlier runs left the target's listings, and this run's.

    Returns:
        What ended the target (a `KeyboardInterrupt` past the first Ctrl-C included); None
        when it finished.
    """
    try:
        try:
            if options.stop_at_saved:
                work.ledger.redate()
            _read(work, run, site, target, options, progress)
        except KeyboardInterrupt:
            if not run.settings.stop.soon.is_set() or run.settings.stop.now.is_set():
                raise
        finally:
            site.close()
        work.downloads.wait()
        return None
    except BaseException as caught:
        return caught


def _ended(
    run: SourceRun, board: Board, report: TargetReport, error: BaseException | None, *,
    complete: str, started: float,
) -> TargetReport:
    """End a target's rows and report it: failed, stopped, or done.

    A failure is classified (`failure`), recorded in `flnd.db` as a failure of the whole
    target (with a scrubbed sample of a `Changed` answer), and sent as `Failed`.

    Args:
        run: the source's run.
        board: the progress rows.
        report: the target's report so far.
        error: what ended the listing; None when it finished.
        complete: the listings it read to their end, for the debug line.
        started: when it started, from `time.monotonic`.

    Returns:
        The report, with its `error` set when it failed or was stopped.

    Raises:
        KeyboardInterrupt: the second Ctrl-C (any `error` that isn't an `Exception`).
    """
    target = report.target
    if error is not None:
        if not isinstance(error, Exception):
            raise error
        failed = failure(error, target, run.source)
        if run.settings.db is not None:
            sampled(run.settings, failed)
            run.settings.db.fail(run=run.settings.run, item=_home.key(target),
                                 target=_home.key(target), error=failed)
        tell(target, failed, run.settings.emit)
        board.end(target, failed=True)
        return replace(report, error=failed)
    if run.settings.stop.soon.is_set():
        ui.debug("{target}: stopped", target=str(target))
        board.end(target, failed=True, how="stopped")
        return replace(report, error=Stopped(STOPPED))
    board.end(target, failed=False)
    _summary(board.totals(target), target, time.monotonic() - started)
    ui.debug(
        "{target}: {written} written, {skipped} skipped; complete: {complete}",
        target=str(report.target), written=len(report.written),
        skipped=report.skipped, complete=complete,
    )
    return report


def _summary(totals: Counts, target: Target, seconds: float) -> None:
    """A debug line for a finished target: files saved, size, skips, failures and time.

    The CLI prints its own line from the target's `Progress` events; this one adds the time.

    Args:
        totals: the target's counts.
        target: the target.
        seconds: how long it took.
    """
    parts = [_text.count(totals.saved, "file") + " saved"] if totals.saved else []
    if totals.bytes:
        parts.append(_text.size(totals.bytes))
    if totals.linked:
        parts.append(f"{totals.linked} linked")
    if totals.skipped:
        parts.append(f"{totals.skipped} skipped")
    if totals.failed:
        parts.append(f"{totals.failed} failed (retried on the next runs)")
    body = ", ".join(parts) or "nothing new"
    line = f"{body} in {_text.duration(seconds)}"
    ui.debug("{target}: {line}", target=str(target), line=line)


def causes(error: BaseException) -> str:
    """An error and its causes on one line, for the debug log: `A: … <- B: …`.

    It follows `__cause__`, else `__context__` unless suppressed, and stops at a loop.

    Args:
        error: the error.

    Returns:
        Each error's type and message, the outermost first.
    """
    links: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        links.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return " <- ".join(links)


def _redated(
    path: Path, found: datetime.date, filename: str, *, estimated: bool
) -> Path | None:
    """Rename a saved file for its new day, when its filename template starts with the day.

    An estimated day at the front of its name is dropped first. When the new name is taken
    by the same bytes, the old file goes; by other bytes, nothing changes.

    Args:
        path: the saved file.
        found: its post's day.
        filename: the filename template.
        estimated: whether its name carries an estimated day.

    Returns:
        Where the file is now; None when it is gone, or the new name holds another file.
    """
    name = path.name
    leading = LEADING_DAY.match(name)
    if estimated and leading:
        name = name[leading.end():]
    if filename.startswith("{date}_") and not LEADING_DAY.match(name):
        name = f"{found.isoformat()}_{name}"
    dest = path.with_name(name)
    if dest == path or not path.exists():
        return path if path.exists() else None
    if dest.exists():
        if not filecmp.cmp(path, dest, shallow=False):
            return None
        path.unlink()
        return dest
    path.rename(dest)
    return dest


def _metadata(source: Source, target: Target, placed: Placed) -> dict[str, Any]:
    """What a saved file's sidecar holds: its post's caption, author, tags, day and link.

    `caption`, `author` and `tags` are read from the post's fields under the names sources
    give them; `fields` keeps every field as the source gave it. A file's URL is kept without
    its query, which may carry a signature.

    Args:
        source: the file's source.
        target: the target it was saved for.
        placed: the file, with its post.

    Returns:
        The sidecar's fields, for JSON.
    """
    post = placed.post
    fields = dict(post.fields)

    def first(*names: str) -> Any:
        return next((fields[name] for name in names if fields.get(name)), None)

    posted = day(post.date)
    url = placed.file.url if isinstance(placed.file.url, str) else None
    return {
        "source": source.code, "target": str(target), "post": post.id,
        "category": post.category, "date": posted.isoformat() if posted else None,
        "url": first("url", "link", "permalink"),
        "file": _text.redact(url) if url else None,
        "caption": first("caption", "text", "description", "title"),
        "author": first("author", "user", "username", "owner", "channel"),
        "tags": first("tags", "hashtags"),
        "fields": fields,
    }


def _set_mtime(path: Path, dated: datetime.date) -> None:
    """Set a file's modified time to noon UTC of its post's day.

    Noon, so the day reads the same in every time zone from UTC-11 to UTC+11. A failure (a
    read-only file system) is a debug line: the file is saved, only its time is off.

    Args:
        path: the file.
        dated: its post's day.
    """
    noon = datetime.datetime.combine(dated, datetime.time(NOON), tzinfo=datetime.UTC)
    stamp = noon.timestamp()
    try:
        os.utime(path, (stamp, stamp))
    except OSError as error:
        ui.debug("{name}: modified time not set: {error}", name=path.name, error=error)


@dataclass
class _Landed:
    """A downloaded file in place, waiting for its post's other files before its row commits."""

    queued: _Queued
    path: Path
    # The day its modified time was set to; None when neither the post nor the server had one.
    day: datetime.date | None
    row: _home.FileRow


@dataclass
class _Queued:
    """One file handed from the listing thread to `Downloads`.

    `refreshed` is set on the loop by the file's own task, once, when its link expired; no
    other thread reads it.
    """

    placed: Placed
    dest: Path
    key: str
    estimated: bool = False
    refreshed: SiteError | None = None
    # The cursor of the page its post was listed on; None for the first page.
    cursor: str | None = None
    # Whether a retry queued it (`_retry_failed`).
    retried: bool = False

    def __str__(self) -> str:
        return self.placed.path


@dataclass(eq=False)
class _Refresh:
    """A download's request for a lazy post's files on the lister thread."""

    post: Post
    stop: Stop
    done: threading.Event = field(default_factory=threading.Event)
    files: list[File] | None = None
    error: Exception | None = None

    def run(self) -> None:
        """Invoke `post.files()` and signal completion."""
        try:
            assert callable(self.post.files)
            self.files = list(self.post.files())
        except Exception as error:
            self.error = error
        except KeyboardInterrupt:
            if not self.stop.soon.is_set() or self.stop.now.is_set():
                raise
        finally:
            self.done.set()


class _Kept:
    """The posts a route or category yields, filtered by `--when`.

    It is where the engine reads a listing as it goes: it stops a `newest_first` listing after
    `OLDER_IN_A_ROW` posts older than `--when`, notes the newest post (the listing's head), and
    keeps the `Target`s a listing yields (`linked`) for the engine, rather than as posts.

    Args:
        yielded: the generator (sync or async), or a coroutine giving one.
        category: the category's name; "" for a route's own posts.
        newest_first: whether the listing comes newest first.
        span: `--when`.
        run_async: runs a coroutine on the run's loop.
    """

    def __init__(
        self, yielded: Any, category: str, *, newest_first: bool, span: Range, run_async: RunAsync
    ) -> None:
        if inspect.iscoroutine(yielded):
            yielded = run_async(yielded)
        self.category, self.newest_first, self.time = category, newest_first, span
        self.ran_out = False
        # The targets the listing yielded, in order, each as the source wrote it.
        self.linked: list[Target] = []
        # The newest post listed, by its day (the first one, while none has a day): the head a
        # later sync stops at.
        self.newest: str | None = None
        self._newest_day: datetime.date | None = None
        # Posts it queued files of, counted by `_queue_listed`: none means nothing new.
        self.queued = 0
        self._steps = _steps(yielded, run_async)

    def __iter__(self) -> Iterator[Post]:
        """Yield kept posts, stopping early after 5 older posts on `newest_first` feeds."""
        older = 0
        # A post a source yields in parts (TikTok yields each photo of a photo post as a post of
        # the same id) is one post to the streaks: only its first part counts.
        last: str | None = None
        try:
            for post in self._steps:
                if isinstance(post, Target):
                    self.linked.append(post)
                    continue
                if not isinstance(post, Post):
                    raise TypeError(
                        f"{self.category or 'a route'} yielded {type(post).__name__}, "
                        "not a Post or a Target"
                    )
                if self.category:
                    post = post.filed(self.category)
                posted = day(post.date)
                self._note(post.id, posted)
                if posted in self.time:
                    older, last = 0, post.id
                    yield post
                    continue
                since = self.time.since
                if self.newest_first and since and posted and posted < since:
                    older += post.id != last
                    last = post.id
                    if older >= OLDER_IN_A_ROW:
                        return
            self.ran_out = True
        finally:
            self.close()

    def close(self) -> None:
        """Close the underlying generator."""
        self._steps.close()

    def _note(self, post: str, posted: datetime.date | None) -> None:
        """Keep `post` as the newest so far when it is the first, or newer than the newest."""
        if self.newest is None or (
                posted is not None and (self._newest_day is None or posted > self._newest_day)):
            self.newest, self._newest_day = post, posted


class Outcome(Enum):
    """Outcome of queuing one post into `Ledger`."""

    QUEUED = "queued"
    SAVED = "saved"
    EMPTY = "empty"
    STOPPED = "stopped"


class Ended(Enum):
    """How one category listing ended."""

    RAN_OUT = "ran out"
    CAUGHT_UP = "caught up"
    CUT = "cut"
    STOPPED = "stopped"


@dataclass(frozen=True)
class _Work:
    """A target's `Ledger` and `Downloads`, for queueing kept posts, and the targets it found."""

    ledger: Ledger
    downloads: Downloads
    # The targets its listings yielded (`_Kept.linked`), in order.
    linked: list[Target] = field(default_factory=list)

    def queue(self, post: Post, *, hold: bool = True) -> Outcome:
        """Plan one post (`Ledger.take`), then submit each of its new files for download.

        Args:
            post: the post.
            hold: whether an undated post may be held for a day.

        Returns:
            What became of the post; `STOPPED` when the run stopped while its files queued.
        """
        outcome, todo = self.ledger.take(post, hold=hold)
        for queued in todo:
            if not self.downloads.submit(queued):
                return Outcome.STOPPED
        return outcome


def discover(
    run: SourceRun, target: Target, *, limit: int | None = None,
    time_range: Range = Range(), seen: Collection[str] = (),
) -> Generator[Target, None, None]:
    """The targets a search (or any route that yields targets) finds, each once, resolved.

    Called by the engine for a search, and for its dry run. It stops at the first target in
    `seen` (a sync's earlier finds, newest first), at `limit`, or at Ctrl-C.

    Args:
        run: the source's run.
        target: the search.
        limit: at most this many targets; None for all.
        time_range: `--when`, which the route's `Site` filters by.
        seen: keys of targets found before; the listing stops at the first.

    Yields:
        Each new target, resolved by the source that owns it.
    """
    route = next(r for r in run.source.routes if r.kind == target.kind)
    site = TargetSite(run, target, time=time_range)
    seen_keys = set(seen)
    yielded: set[Target] = set()
    method = getattr(run.source, route.kind)
    try:
        if _at_limit(run, yielded, limit):
            return
        yield from _yield_arg_targets(run, method, site, target.value, seen_keys, yielded, limit)
    finally:
        site.close()


def _yield_arg_targets(
    run: SourceRun, method: Any, site: TargetSite, arg: Any, seen_keys: set[str],
    yielded: set[Target], limit: int | None,
) -> Generator[Target, None, None]:
    """Run a route that yields targets, resolving and deduplicating what it yields.

    A text the run can't resolve is warned about and passed over (`resolved`).

    Args:
        run: the source's run.
        method: the route's method.
        site: the search's `Site`.
        arg: the value the route is called with.
        seen_keys: keys that end the listing.
        yielded: the targets yielded so far; each new one is added.
        limit: at most this many targets in `yielded`; None for all.

    Yields:
        Each new target.
    """
    steps = _steps(method(site, arg), run.run_async)
    try:
        for item in steps:
            if run.settings.stop.soon.is_set():
                return
            child = resolved(run, item)
            if child is None:
                continue
            if _home.key(child) in seen_keys:
                break
            if child in yielded:
                continue
            yielded.add(child)
            yield child
            if _at_limit(run, yielded, limit):
                return
    finally:
        steps.close()


def _at_limit(run: SourceRun, yielded: set[Target], limit: int | None) -> bool:
    """Return whether discovery should stop due to Ctrl-C or `limit`."""
    return run.settings.stop.soon.is_set() or (limit is not None and len(yielded) >= limit)


@dataclass(frozen=True)
class _Stops:
    """When a sync of a listing has caught up: the engine's early stop at saved posts.

    Args:
        sync: whether the listing stops at saved posts at all: any run (but `--again`) of a
            `newest_first` listing an earlier run read to its end, not resumed from a cursor.
        streak: saved posts in a row that end it (`stop_after`); None for no such stop, only
            the head's: a sync with a gap it has no cursor for reads down to the head.
        head: the newest post an earlier run saw in this listing; None when none did.
    """

    sync: bool = False
    streak: int | None = 5
    head: str | None = None


def _queue_all(
    work: _Work, kept: _Kept, *, stops: _Stops = _Stops(), limit: int | None = None
) -> tuple[Ended, int]:
    """Queue kept posts, then release held undated posts.

    Args:
        work: the target's ledger and downloads.
        kept: the listing, filtered by `--when`.
        stops: when a sync has caught up.
        limit: posts the listing may still take; None for no limit.

    Returns:
        How the listing ended, and how many posts it took.
    """
    ended, taken = _queue_listed(work, kept, stops=stops, limit=limit)
    if ended is Ended.STOPPED:
        return ended, taken
    for post in work.ledger.release():
        if limit is not None and taken >= limit:
            return Ended.CUT, taken
        outcome = work.queue(post, hold=False)
        if outcome is Outcome.STOPPED:
            return Ended.STOPPED, taken
        if outcome is not Outcome.EMPTY:
            taken += 1
    return ended, taken


def _queue_listed(
    work: _Work, kept: _Kept, *, stops: _Stops, limit: int | None = None
) -> tuple[Ended, int]:
    """Queue each post in `kept` until stopped, `limit`, or a sync caught up (`_Stops`).

    A sync has caught up after `stops.streak` saved posts in a row, or, once it met the head an
    earlier run saw, after `HEAD_IN_A_ROW`: the head and the post after it. Requiring the post
    after it keeps a head the site later pinned above new posts from ending the sync too soon.

    Args:
        work: the target's ledger and downloads.
        kept: the listing, filtered by `--when`.
        stops: when a sync has caught up.
        limit: posts the listing may still take; None for no limit.

    Returns:
        How the listing ended, and how many posts it took (queued or saved).
    """
    saved_in_a_row = 0
    taken = 0
    met_head = False
    # Consecutive parts of one post (the same id, as `_Kept` says) count once, for `--limit` too.
    last: str | None = None
    stop = work.downloads.settings.stop.soon
    try:
        for post in kept:
            part = post.id == last
            if limit is not None and taken >= limit and not part:
                return Ended.CUT, taken
            outcome = Outcome.STOPPED if stop.is_set() else work.queue(post)
            if outcome is Outcome.STOPPED:
                return Ended.STOPPED, taken
            if outcome is not Outcome.EMPTY:
                last = post.id
                if part:
                    # A part saved like the rest leaves the streak as it was; a new part breaks it.
                    if outcome is Outcome.QUEUED:
                        kept.queued, saved_in_a_row = kept.queued + 1, 0
                    continue
                taken += 1
                kept.queued += outcome is Outcome.QUEUED
                saved_in_a_row = saved_in_a_row + 1 if outcome is Outcome.SAVED else 0
            work.downloads.board.seen(work.downloads.target, kept.category,
                                      streak=saved_in_a_row)
            met_head = met_head or (stops.head is not None and post.id == stops.head)
            # `--limit` is checked as the next post comes, not here: the next may be another part
            # of this one. That reads one more item of the listing, never one more post.
            if not stops.sync:
                continue
            if met_head and saved_in_a_row >= HEAD_IN_A_ROW:
                # The caller says "nothing new", or jumps into the listing's gaps.
                return Ended.CAUGHT_UP, taken
            if stops.streak is not None and saved_in_a_row >= stops.streak:
                ui.info("{category}: {count} saved posts in a row, caught up",
                        category=kept.category or "posts", count=saved_in_a_row)
                return Ended.CAUGHT_UP, taken
    finally:
        kept.close()
    return (Ended.RAN_OUT if kept.ran_out else Ended.CUT), taken


def _read(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    progress: _Progress,
) -> None:
    """List the target's route, or its wanted categories, into `work`.

    A discovering route downloads each target it yields; a route without categories is one
    listing (named ""); otherwise the route is called once, and each wanted category listed
    with what it returned, sharing `--limit`. The files earlier runs failed on go first
    (`_retry_failed`), once the route has run, since a category needs what it returned.

    Args:
        work: the target's ledger and downloads, which queue each post's files.
        run: the source's resources for the run.
        site: the target's site; its `listing` is set while a listing runs.
        target: the target.
        options: the target's options: `--when`, `--only`, `--limit`, `--again`, and what to do
            with earlier failures (`retry`).
        progress: where the earlier runs left the target's listings, and this run's.
    """
    route = next(r for r in run.source.routes if r.kind == target.kind)
    if route.discovers:
        _run_discovered(run, work.ledger.board, options,
                        _steps(_context(run.source, route, site, target, run.run_async),
                               run.run_async))
        return
    listings: list[_Listed]
    if not route.categories:
        listings = [_Listed("", route.newest_first,
                            lambda: getattr(run.source, route.kind)(site, target.value))]
    else:
        categories = wanted(type(run.source), route, options)
        if not categories:
            return
        context = _context(run.source, route, site, target, run.run_async)
        listings = [_Listed(category.name, category.newest_first,
                            functools.partial(getattr(run.source, category.name), site, context))
                    for category in categories]
    _retry_failed(work, run, site, target, options, listings)
    if options.retry is Retry.ONLY:
        return
    remaining = options.limit
    for listed in listings:
        if remaining is not None and remaining <= 0:
            return
        ended, taken = _list_one(
            work, run, site, target, options, progress, name=listed.name,
            newest_first=listed.newest_first, items=listed.items, remaining=remaining,
        )
        if remaining is not None:
            remaining -= taken
        if ended is Ended.STOPPED:
            return


@dataclass(frozen=True)
class _Listed:
    """One listing of a target: a category, or a route's own posts."""

    # The category's name; "" for a route's own posts.
    name: str
    newest_first: bool
    # Calls the category (or route) method, returning what it yields.
    items: Callable[[], Any]


def _list_one(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    progress: _Progress, *, name: str, newest_first: bool, items: Callable[[], Any],
    remaining: int | None,
) -> tuple[Ended, int]:
    """List one category (or a route's own posts) into `work`, and note where it ended.

    How it goes:
    1. A `newest_first` listing an earlier run read to its end is synced (`_sync_one`): from
       the newest, stopping at saved posts, then into its gaps.
    2. Any other `newest_first` listing an earlier run cut short resumes from its saved
       cursor: the site's `Listing` hands it to the first `pages` call. Any other listing reads
       to its end, `--limit` or `--when`.
    3. A listing that ran out is complete; one read from the top keeps its head.
    4. A resumed listing that ran out lists again from the newest, down to the posts it saved
       before, since new posts may have come while it was interrupted.

    Args:
        work: the target's ledger and downloads, which queue each post's files.
        run: the source's resources for the run.
        site: the target's site; its `listing` is set while a listing runs.
        target: the target.
        options: the target's options: `--when`, `--only`, `--limit`, `--again`.
        progress: where the earlier runs left the target's listings, and this run's.
        name: the category's name; "" for a route's own posts.
        newest_first: whether the listing comes newest first.
        items: calls the category (or route) method, returning what it yields.
        remaining: posts the target may still take under `--limit`; None for no limit.

    Returns:
        How the listing ended, and how many posts it took.
    """
    ui.debug("{target}: list {name}", target=str(target), name=name or target.kind)
    stops = _Stops(
        sync=newest_first and name in progress.listed,
        streak=stop_after(run.settings.home, run.source.code),
        head=progress.seen_heads.get(name),
    )
    if stops.sync:
        ended, taken = _sync_one(work, run, site, target, options, progress, name=name,
                                 items=items, stops=stops, remaining=remaining)
        if ended is not Ended.STOPPED:
            work.ledger.board.listed(target, name, how=_early(ended))
        return ended, taken
    start = progress.started.get(name) if newest_first else None
    listing = progress.listings[name] = Listing(start, label=f"{target}: {name or target.kind}")
    # The board's line shows the pages this listing reads, in the live stats.
    work.ledger.board.start(target, name, slots=run.jobs, listing=listing)
    if start:
        ui.info("{target}: {name}: going on where an interrupted run stopped",
                target=str(target), name=name or target.kind)
        work.ledger.board.say(target, name, RESUMING)
    ended, taken, kept = _listed(work, run, site, name, newest_first, items, listing, stops,
                                 options, remaining)
    if ended in (Ended.RAN_OUT, Ended.CAUGHT_UP):
        progress.complete.append(name)
        if not start and kept.newest is not None:
            progress.heads[name] = kept.newest
    if ended is Ended.RAN_OUT and start and listing.taken and not listing.refused:
        # The run that saved the cursor was cut short, so posts newer than those it saw may
        # have come since: list from the newest down to the saved posts.
        rest = None if remaining is None else remaining - taken
        if rest is None or rest > 0:
            ui.debug("{target}: list {name} from the newest", target=str(target),
                     name=name or target.kind)
            ended, extra, newest = _listed(work, run, site, name, True, items, Listing(),
                                           replace(stops, sync=True), options, rest)
            taken += extra
            if ended in (Ended.RAN_OUT, Ended.CAUGHT_UP) and newest.newest is not None:
                progress.heads[name] = newest.newest
    if ended is not Ended.STOPPED:
        work.ledger.board.listed(target, name, how=_early(ended))
    return ended, taken


def _early(ended: Ended) -> str:
    """How a listing stopped early, as the live stats show it; "" for one that ran out."""
    return "" if ended is Ended.RAN_OUT else ended.value


def _sync_one(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    progress: _Progress, *, name: str, items: Callable[[], Any], stops: _Stops,
    remaining: int | None,
) -> tuple[Ended, int]:
    """Sync a `newest_first` listing an earlier run read to its end: the newest, then its gaps.

    A gap is where an earlier sync was cut short (Ctrl-C, an error, `--limit`) before it met
    the posts saved before it: the posts between are unsaved, and a sync that stopped at the
    saved posts on top would never reach them. Every gap lies above the listing's head, since a
    head is kept only by a run that leaves no gap.

    How it goes:
    1. List from the newest, stopping at saved posts (`_Stops`). A gap with no cursor (a feed
       that isn't paged by cursor, or one cut on its first page) turns the stop at 5 saved posts
       off, so the listing reads down to the head, past every gap; with no head, to its end.
    2. Once it met saved posts on top, jump to each gap's cursor, newest first, and list from
       there until it meets saved posts again ("resuming from the last run").
    3. Keep what is left: a listing cut short at the top leaves a new gap above the old ones,
       and one cut inside a gap leaves that gap from where it was cut.
    4. A listing that met saved posts with nothing queued and no gap had nothing new: one page,
       on a site that pages, so one request. It says so with a `Skipped` event.

    Args:
        work: the target's ledger and downloads.
        run: the source's resources for the run.
        site: the target's site.
        target: the target.
        options: the target's options.
        progress: where the earlier runs left the target's listings; this run's gaps, heads and
            completeness are written to it.
        name: the category's name; "" for a route's own posts.
        items: calls the category (or route) method, returning what it yields.
        stops: the sync's stops, with the head an earlier run saw.
        remaining: posts the target may still take under `--limit`; None for no limit.

    Returns:
        How the listing ended, and how many posts it took.
    """
    gaps = progress.gaps_of(name)
    label = f"{target}: {name or target.kind}"
    cursorless = "" in gaps
    if cursorless:
        stops = replace(stops, streak=None) if stops.head is not None else replace(stops,
                                                                                    sync=False)
    listing = progress.listings[name] = Listing(None, label=label)
    # The board's line shows the pages this listing reads, in the live stats.
    work.ledger.board.start(target, name, slots=run.jobs, listing=listing)
    # 1. The newest posts. A listing that raises (the site failed it) is cut short like any:
    # where it was is kept as a gap before the error goes on to fail the target.
    try:
        ended, taken, kept = _listed(work, run, site, name, True, items, listing, stops,
                                     options, remaining)
    except BaseException:
        progress.gaps[name] = list(dict.fromkeys([listing.cursor or "", *gaps]))
        raise
    queued = kept.queued
    left: list[str]
    if ended is Ended.RAN_OUT or (ended is Ended.CAUGHT_UP and cursorless):
        left = []
    elif ended is Ended.CAUGHT_UP:
        # 2. Into each gap, newest first.
        left = list(gaps)
        while left:
            rest = None if remaining is None else remaining - taken
            if rest is not None and rest <= 0:
                ended = Ended.CUT
                break
            ui.info("{target}: resuming from the last run", target=label)
            work.ledger.board.say(target, name, RESUMING)
            jump = progress.listings[name] = Listing(left[0], label=label)
            work.ledger.board.start(target, name, slots=run.jobs, listing=jump)
            try:
                ended, extra, filled = _listed(work, run, site, name, True, items, jump, stops,
                                               options, rest)
            except BaseException:
                progress.gaps[name] = [jump.cursor or left[0], *left[1:]]
                raise
            taken, queued = taken + extra, queued + filled.queued
            if jump.refused:
                # The site no longer pages from the gap's cursor, and listed from the newest
                # instead: the next sync reads down to the head, past the gap.
                left = list(dict.fromkeys(["", *left[1:]]))
                break
            if ended is Ended.RAN_OUT:
                # The listing's end is below every gap.
                left = []
            elif ended is Ended.CAUGHT_UP:
                left = left[1:]
            else:
                left = [jump.cursor or left[0], *left[1:]]
                break
    else:
        # 3. Cut short at the top: a new gap above the old ones.
        left = list(dict.fromkeys([listing.cursor or "", *gaps]))
    progress.gaps[name] = left
    if ended in (Ended.RAN_OUT, Ended.CAUGHT_UP):
        progress.complete.append(name)
        if not left and kept.newest is not None:
            progress.heads[name] = kept.newest
    if ended in (Ended.RAN_OUT, Ended.CAUGHT_UP) and not queued and not gaps:
        # 4. Nothing new.
        ui.info("{category}: nothing new", category=name or "posts")
        run.settings.emit(Skipped(target, name, "", None, reason=NOTHING_NEW))
    return ended, taken


def _retry_failed(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    listings: Sequence[_Listed],
) -> None:
    """List again each post earlier runs failed on, and queue its failed files, before new posts.

    A retry works from `(target, post, file)` and never from a stored URL: `flnd.db` keeps only
    a redacted one, and a CDN's signed URL expires within hours. So each failed post is listed
    again through its source, for fresh URLs.

    How it goes:
    1. Read the target's failures (`Home.pending`): those failed on at most `_home.RETRIES`
       runs, or every one for `flnd retry`. A gone file (`NotFound`) is never among them.
    2. Group them by listing, and by the cursor of the page their post was on.
    3. For each group, list from that cursor (`_relist`): each failed post met is queued like
       any post, which downloads only its files that aren't saved: the ones that failed.
    4. A post the listing no longer has is a failure of its own: gone when the listing ran out
       without it, else one more run tried, so the post is given up after its last.

    Args:
        work: the target's ledger and downloads.
        run: the source's resources for the run.
        site: the target's site; its `listing` is set while a retry lists.
        target: the target.
        options: the target's options: `retry` says which failures; `--only` which listings.
        listings: the listings this run reads; a failure of another one waits.
    """
    db = run.settings.db
    if run.settings.dry or options.again or db is None:
        return
    failed = db.pending(_home.key(target), every=options.retry is Retry.ONLY)
    by_name = {listed.name: listed for listed in listings}
    groups: dict[tuple[str, str | None], dict[str, list[_home.FailureRow]]] = {}
    for row in failed:
        if row.category in by_name:
            groups.setdefault((row.category, row.cursor), {}).setdefault(row.post, []).append(row)
    if not groups:
        return
    files = sum(len(rows) for posts in groups.values() for rows in posts.values())
    ui.info("{target}: retrying {files} earlier runs failed on", target=str(target),
            files=_text.count(files, "file"))
    work.ledger.retrying = True
    try:
        for (name, cursor), posts in groups.items():
            if run.settings.stop.soon.is_set():
                return
            listed = by_name[name]
            missing, ran_out = _relist(work, run, site, listed, cursor, set(posts))
            for post in missing:
                _lost(work, run, target, posts[post], ran_out=ran_out)
    finally:
        work.ledger.retrying = False


def _relist(
    work: _Work, run: SourceRun, site: TargetSite, listed: _Listed, cursor: str | None,
    posts: set[str],
) -> tuple[set[str], bool]:
    """List one listing from `cursor` until it has met every post of `posts`, queueing each.

    From a cursor, it gives up `RETRY_WINDOW` posts past the last one it met: the posts were on
    that cursor's page. From the newest (no cursor), it reads until it met them all or the
    listing ran out. `--when` doesn't apply: the posts were within it when they failed.

    Args:
        work: the target's ledger and downloads.
        run: the source's resources for the run.
        site: the target's site, whose `listing` is set for the listing, then cleared.
        listed: the listing.
        cursor: where to start; None for the newest.
        posts: the ids of the posts to meet.

    Returns:
        The posts it didn't meet, and whether the listing ran out (rather than being given
        up or stopped). A run told to stop returns none missing.
    """
    left, past, last = set(posts), 0, None
    site.listing = Listing(cursor, label=f"{site.target}: {listed.name or site.target.kind}")
    kept = _Kept(listed.items(), listed.name, newest_first=listed.newest_first, span=Range(),
                 run_async=run.run_async)
    try:
        for post in kept:
            if run.settings.stop.soon.is_set():
                return set(), False
            # A post the source yields in parts (one id) is queued part by part.
            if post.id in left or post.id == last:
                # Not held for a day from the posts around it: a retry reads too few of them.
                if work.queue(post, hold=False) is Outcome.STOPPED:
                    return set(), False
                left.discard(post.id)
                last, past = post.id, 0
                continue
            if not left:
                return left, False
            last, past = None, past + 1
            if cursor is not None and past > RETRY_WINDOW:
                return left, False
    finally:
        kept.close()
        site.listing = None
    return left, kept.ran_out


def _lost(
    work: _Work, run: SourceRun, target: Target, rows: Sequence[_home.FailureRow], *,
    ran_out: bool,
) -> None:
    """Record that a retry didn't meet a failed post: a failure of each of its files.

    Args:
        work: the target's ledger and downloads, whose board counts each file failed.
        run: the source's resources for the run, with its database and event sink.
        target: the target.
        rows: the post's failures, one per file.
        ran_out: whether its listing ran out without it: it is gone (`NotFound`), never asked
            for again unless told to. Otherwise this run counts as one more that tried it.
    """
    db = run.settings.db
    for row in rows:
        error: SiteError
        if ran_out:
            error = NotFound(f"post {row.post} is no longer in its listing")
        else:
            error = SiteError(f"post {row.post} wasn't found near where it was listed",
                              hint="flnd retry tries again", retryable=True)
        if db is not None:
            db.fail(run=run.settings.run, item=row.key, target=row.target, error=error,
                    post=row.post, category=row.category, file=row.file, cursor=row.cursor)
        work.ledger.board.count(target, row.category, found=1, failed=1)
        run.settings.emit(Failed(target, error, post=row.post, path=Path(row.file)))


def _listed(
    work: _Work, run: SourceRun, site: TargetSite, name: str, newest_first: bool,
    items: Callable[[], Any], listing: Listing, stops: _Stops, options: Options,
    remaining: int | None,
) -> tuple[Ended, int, _Kept]:
    """Run one listing with `listing` set on the site, queueing what it yields into `work`.

    Args:
        work: the target's ledger and downloads; the targets the listing yields join its
            `linked`.
        run: the source's resources for the run.
        site: the target's site, whose `listing` is set for the listing, then cleared.
        name: the category's name; "" for a route's own posts.
        newest_first: whether the listing comes newest first.
        items: calls the category (or route) method, returning what it yields.
        listing: the `Listing` the first `pages` call resumes and reports to.
        stops: when a sync has caught up.
        options: the target's options, for `--when`.
        remaining: posts it may take under `--limit`; None for no limit.

    Returns:
        How it ended, how many posts it took, and the `_Kept` that read it (its head, its
        linked targets).
    """
    site.listing = listing
    try:
        kept = _Kept(items(), name, newest_first=newest_first,
                     span=Range(options.since, options.until), run_async=run.run_async)
        ended, taken = _queue_all(work, kept, stops=stops, limit=remaining)
    finally:
        site.listing = None
    work.linked.extend(kept.linked)
    return ended, taken, kept


def _run_discovered(
    run: SourceRun, board: Board, options: Options, items: Generator[Any, None, None]
) -> None:
    """Download each target a discovering route yields, in turn, within its parent's run.

    Such a route (an author's page listing their works) has no posts of its own; each target
    it yields is downloaded at once with the parent's options, sharing its board. `--limit`
    counts targets. The engine runs most of these as searches (`Engine._run_query`); this is
    the path for one reached by `download` directly, as a found target can be.

    Args:
        run: the source's run.
        board: the parent's progress rows.
        options: the parent's options.
        items: what the route yields; closed when done.
    """
    seen: set[Target] = set()
    child_options = replace(options, limit=None)
    try:
        for item in items:
            if run.settings.stop.soon.is_set():
                return
            child = resolved(run, item)
            if child is None or child in seen:
                continue
            seen.add(child)
            download(run, child, board, child_options)
            if options.limit is not None and len(seen) >= options.limit:
                return
    finally:
        items.close()


def _steps(yielded: Any, run_async: RunAsync) -> Generator[Any, None, None]:
    """Iterate what a route or category yields, plain or async, closing it however it ends.

    An async generator is stepped on the run's loop, one `__anext__` at a time, from the
    listing thread; each step is timed, and one that holds the loop past `HELD` seconds
    without awaiting is warned about, since it stalls every download. None yields nothing.

    Args:
        yielded: a generator, an iterable, an async generator, or None.
        run_async: runs a coroutine on the run's loop.

    Yields:
        Each item, as the source yielded it.
    """
    if not hasattr(yielded, "__anext__"):
        iterator = iter(yielded if yielded is not None else ())
        try:
            yield from iterator
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        return
    # Async code runs on the run's loop, so a blocking call in it stalls every download: each
    # stretch it runs without awaiting is timed, and one past `HELD` seconds warns.
    name = getattr(yielded, "__qualname__", "an async listing")

    def slow(seconds: float) -> None:
        ui.warn("{name} held the loop {seconds:.1f} s without awaiting; await, or use a plain "
                "def, which runs on its own thread", name=name, seconds=seconds)
    try:
        while True:
            try:
                yield run_async(_loop.held(yielded.__anext__(), limit=HELD, slow=slow))
            except StopAsyncIteration:
                return
    finally:
        run_async(yielded.aclose())


def _extension(file: File) -> tuple[str, str | None]:
    """A file's extension and kind (`_files.VIDEO`, …), guessed before it is downloaded.

    From its name, else its URL's path. A video with a separate audio track (DASH) is joined
    into `.webm` when both are WebM, else `.mp4`. A file whose kind can't be told yet is
    sniffed from its first bytes when it lands (`_files.SNIFF`).

    Args:
        file: the file.

    Returns:
        The extension with its dot ("" when unknown), and the kind (None for inline content).
    """
    if file.content is not None:
        return PurePosixPath(file.name or "").suffix, None
    if file.name is not None:
        return PurePosixPath(file.name).suffix, _files.kind_of(file.name)
    url = (
        file.url if isinstance(file.url, str)
        else file.url[0] if isinstance(file.url, tuple)
        else ""
    )
    extension = PurePosixPath(urlsplit(url).path).suffix.lower() if url else ""
    kind = _files.kind_of(f"file{extension}") if extension else None
    if file.audio is not None:
        # A video and its audio track are joined without re-encoding: a WebM pair into WebM,
        # anything else into MP4, which takes every codec the sites send.
        audio = PurePosixPath(urlsplit(file.audio).path).suffix.lower()
        both = extension == ".webm" and audio == ".webm"
        return (".webm" if both else DASH_EXTENSION), _files.VIDEO
    if kind is None:
        return "", _files.SNIFF
    return extension, kind


def _fields(template: str) -> set[str]:
    """Return top-level field names referenced in `template`."""
    return {
        name.split(".")[0].split("[")[0] for _, name, _, _ in _FORMATTER.parse(template) if name
    }


def _folder_format(source: Source, route: Route | None, category: Category | None) -> str:
    """The folder template a file is placed by: its category's, else its route's, else its
    source's.

    Args:
        source: the file's source.
        route: the target's route; None to skip it.
        category: the post's category; None to skip it.

    Returns:
        The template, such as `{category}`.
    """
    if category is not None and category.folder is not None:
        return category.folder
    if route is not None:
        route_folder = getattr(route, "folder", None)
        if route_folder is not None:
            return route_folder
    return source.folder


def _format(source: Source, category: Category | None, name: str) -> str:
    """Return format string `name` from `category` if set, else from `source`."""
    own = getattr(category, name, None)
    return own if own is not None else getattr(source, name)


def resolved(run: SourceRun, item: Target | str) -> Target | None:
    """A target a source yielded, resolved: by the run's resolver, which knows every source.

    A target already resolved (its `kind` set) is taken as it is. One the run can't read is a
    warning, and None, so one bad link never ends the listing that yielded it.

    Args:
        run: the source's run; `settings.resolve` resolves any source's target text, and,
            outside an engine's run, the source's own `resolve` reads its own.
        item: a `Target` or its text.

    Returns:
        The resolved target, or None.
    """
    if isinstance(item, Target) and item.kind:
        return item
    text = str(item)
    try:
        if run.settings.resolve is not None:
            return run.settings.resolve(text)
        own = Target(text)
        if own.source == run.source.code:
            text = f"?{own.value}" if own.query else own.value
        return run.source.resolve(text)
    except Error as error:
        ui.warn("{source}: can't follow {text}: {error}", source=run.source.code, text=text,
                error=error)
        return None

