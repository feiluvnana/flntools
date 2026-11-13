"""The job: one target of a ported source, from its route to its history rows, in one fixed pipeline.

How it works, for one target (plan/phase-6/redesign.md, B3):

    route(site, value) ──► for each wanted category, in declaration order:
        for post in category(site, context):                        ← the source
            set category; read date                                 ← `posts`
            --time filter; newest_first early stop                  ← `posts`
            files = post.files() if lazy, unless its first key is saved
            for num, file: name from the formats; skip if saved or queued   ← `place`
            queue the download ─► net.download                      ← `Downloads._download`
            finish: shrink → modified time → history row
            history row written last

- **Categories.** The route is called once, and only when at least one of its categories is
  wanted; without `--from` every category is. A route without categories yields the target's
  posts itself, which are filed in the target's own folder.
- **Time.** A post dated outside `--time` is skipped before its files are listed; an undated one
  is kept. In a `newest_first` category, 5 posts in a row older than `since` end the category:
  its generator is closed, so its `finally` blocks run and no more pages are asked for. Pinned
  posts come first and may be old: Instagram and TikTok pin up to 3, X up to 1.
- **Saved-run stop** (A9 rule 3). Under `sync`, a `newest_first` category whose last listing
  reached its end (`targets.db`'s `complete`) ends after 5 posts in a row whose files were all
  already saved: sync has caught up with that feed, and its generator is closed. A post with
  no file to save (none, or text only) neither counts nor breaks the run. Without that
  record, or under `download`, the listing goes to the end. A category ended this way isn't
  recorded again; its earlier record stands.
- **Saved.** A file already on disk, or whose history row points at a file, is skipped, and so
  is one already queued. A lazy post (`files` a function) whose first key is saved is skipped
  without calling it, which saves its request; a post with its files listed checks each file.
  A skip is a debug line (`-v`) naming the file that holds it, since one media is often listed
  twice under two names, a feed's dated post and a tab's undated photo; at info, each category
  says once how many files it skipped.
- **Async.** An `async def` route or category (tlg) runs on the source's own loop one step at a
  time, from the job's thread, so the loop is free while the job waits for room in the download
  pool. There is no handover thread.
- **Errors.** A failed file is logged and the target goes on. A route or category that raises
  fails the target: the files queued but not started are dropped, and the running ones finish.
- **Expired links** (redesign B6). A 403 or 410 for a file of a lazy post (`files` a function),
  such as a signed CDN URL that expired during a long rate-limit wait, calls `files` once more,
  takes the file with the same `num`, and downloads it again, resuming the part the first link
  left. Once per file, never in a loop. The call runs on the job's thread, the one that lists,
  since a source's code (a browser page, say) may not be safe to run on two threads at once:
  the download asks for it and waits, and the job's thread answers between posts and while it
  waits for room in the pool or for the downloads to end.
- **Ctrl-C** (redesign B3, D8). The first one sets `net.stopping`: the job stops listing and
  closes the category it was reading, so the source's `finally` blocks run; a download not yet
  started is dropped, the running ones finish, and the target's row says
  `stopping: 2 files finishing (Ctrl-C again to stop now)` meanwhile. The target ends as
  stopped (`STOPPED`), and a category the stop cut short is never recorded as complete. The
  second sets `net.stopping_now`: the downloads stop at their next chunk, keeping their
  resumable parts, and `KeyboardInterrupt` is raised.

    for placed in job.plan(run, Target("loi", "post", "5930")): ...     # a dry run: nothing downloaded or recorded
    done = job.download(run, Target("loi", "post", "5930"), row)
    done.written, done.skipped, done.error, done.complete
"""

from __future__ import annotations

import datetime
import email.utils
import filecmp
import inspect
import os
import re
import string
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import (TYPE_CHECKING, Any, Callable, Collection, Coroutine, Generator, Iterator,
                    Mapping, Sequence)
from urllib.parse import urlsplit

from flnd import settings as saved_settings
from flnd.common import files as disk
from flnd.common import net
from flnd.common.compress import KINDS, ONE_AT_A_TIME
from flnd.site import SourceRun, TargetSite
from flnd.source import (Category, FetchError, File, LoginError, Post, Range, Resume, Route, Site,
                         Source, Target, day, fill, listing)
from shared import atomic, fmt, log

if TYPE_CHECKING:
    from shared import worker

    from flnd.session import Options

__all__ = ["STOPPED", "Done", "Downloads", "Ledger", "Placed", "download", "place", "plan", "posts",
           "wanted"]

# The history fields of a file's post: its id, its day (`YYYY-MM-DD`), and whether that day is
# the nearest dated neighbour's estimate.
ID, DATE, ESTIMATED = "id", "date", "estimated"
# The statuses that refuse the whole site, which stop asking the `date` hook for the run.
BLOCKS = frozenset({401, 403, 429})
# The day a dated file's name starts with, as the formats write it.
LEADING_DAY = re.compile(r"\d{4}-\d\d-\d\d_")
# The hour of the day, UTC, a dated file's modified time is set to.
NOON = 12
# The history field of a post's first file that holds how many files the post has, when more than one.
FILES = "files"
# Posts in a row older than `--time`'s since that end a `newest_first` category (A9 rule 2): more
# than any site pins (Instagram and TikTok up to 3, X 1), so 5 can only mean the feed has passed it.
OLDER_IN_A_ROW = 5
# Already-saved posts in a row that end a `newest_first` category in `sync` (A9 rule 3), when the
# category's last listing reached its end: gallery-dl's `skip: "abort:N"`. Pinned posts come first
# and may be saved long ago, so one can't end it, and 5 is more than any site pins.
SAVED_IN_A_ROW = 5
# What the key format may use that is known only once a post's files are listed; a key that uses
# one can't tell from a lazy post alone whether it is saved.
LISTED_FIELDS = frozenset({"count", "index", "extension", "path"})
# The extension of a video that comes with a separate audio stream (DASH) when its URL says none.
DASH_EXTENSION = ".mp4"
# The statuses a signed CDN URL answers once it has expired (Instagram's and Facebook's `oe=`,
# TikTok's `x-expires`): 403 mostly, 410 on some edges. Only these refresh a lazy post's link.
EXPIRED = frozenset({403, 410})
# A stopped target's error, in its report: the run was stopped by Ctrl-C before it was done.
STOPPED = "stopped by Ctrl-C"
# The text of a stopping target's row while its running downloads finish (D8).
FINISHING = "stopping: {files} finishing (Ctrl-C again to stop now)"
# Seconds the job's thread waits between looks at its downloads, their refresh requests and
# Ctrl-C: short enough that a second Ctrl-C or a refresh is answered at once to a person.
WAKE = 0.1

# Runs a coroutine to its end, as `SourceRun.run_async` does on the source's loop.
RunAsync = Callable[[Coroutine[Any, Any, Any]], Any]


@dataclass(frozen=True)
class Placed:
    """Where one file of a post goes and what it is keyed by, before it is downloaded.

        placed = place(Example(), post)[0]
        placed.path, placed.key       # "posts/2026-01-02_C1abc_0.jpg", "C1abc_0"
    """

    # The post it belongs to, its category set.
    post: Post
    # The file itself.
    file: File
    # Its place in the post, from 0.
    num: int
    # Its path under the target's folder, `/`-separated, each part a safe file name; the content
    # may still change its extension.
    path: str
    # Its history key without the source's code: the `key` format, or `path` when that is empty.
    key: str
    # What `net.download` checks it as: "image", "video" or "audio", `files.SNIFF` for content to
    # decide, or None for any file kept as it is (an archive); None too for a text file.
    kind: str | None
    # The format fields it was named from.
    values: Mapping[str, Any]


@dataclass(frozen=True)
class Done:
    """What one target's job did, for the engine's `TargetReport` and `targets.db`'s `complete` record."""

    # Every file written, where it ended up after shrinking, in the order they finished.
    written: tuple[Path, ...]
    # Files and lazy posts skipped as already saved or queued.
    skipped: int
    # Why the target failed (its route or a category raised), or None.
    error: str | None
    # The categories whose listing ended by itself, not by an early stop, an error or Ctrl-C.
    complete: tuple[str, ...]


def wanted(source: type[Source], route: Route, options: Options) -> tuple[Category, ...]:
    """The categories of `route` that `--from` takes, in declaration order; every one without `--from`.

        wanted(Example, Example.routes[1], Options(categories=("posts",)))     # (Category("posts", …),)

    Args:
        source: the source, whose `categories` `--from` is read against.
        route: one of its routes.
        options: the run's options.
    """
    from flnd.session import selected

    names = selected(options, source)
    return tuple(category for category in route.categories if category.name in names)


def posts(source: Source, site: Site, target: Target, options: Options, *,
          run_async: RunAsync) -> Generator[Post, None, None]:
    """Every post the target's route and wanted categories yield that `--time` keeps, its `category` set.

        for post in posts(source, site, target, options, run_async=run.run_async): ...

    It lists without saved posts or cursors, for `flnd/dry.py`; `download` runs its own loop.
    Closing it closes the category being read, so its `finally` blocks run.
    """
    time = Range(options.since, options.until)
    route = next(route for route in source.routes if route.kind == target.kind)
    categories = wanted(type(source), route, options)
    if route.categories and not categories:
        return
    context = _context(source, route, site, target, run_async)
    if not route.categories:
        yield from _Kept(context, "", newest_first=False, time=time, run_async=run_async)
        return
    for category in categories:
        method = getattr(source, category.name)
        yield from _Kept(method(site, context), category.name, newest_first=category.newest_first,
                         time=time, run_async=run_async)


def _context(source: Source, route: Route, site: Site, target: Target, run_async: RunAsync) -> Any:
    """What calling the target's route gave: its own posts, or its categories' context.

    Called only when a category of it is wanted, so a route whose categories are all left out
    makes no request.
    """
    context = getattr(source, route.kind)(site, target.value)
    if route.categories and inspect.iscoroutine(context):
        context = run_async(context)
    return context


def plan(run: SourceRun, target: Target, *, row: Any = None) -> Generator[Placed, None, None]:
    """What a download of the target would save, file by file, without downloading, writing or recording anything.

        for placed in plan(run, Target("ins", "user", "example_user")):
            print(placed.post.category, placed.post.id, placed.path)

    Args:
        run: the source's resources for the run; its settings' options say `--from` and `--time`.
        target: the target, as `Engine.resolve` gives it.
        row: its progress row for a long pacing wait to caption, or None.

    Yields:
        Each file of each post the route and categories give and `--time` keeps, named by the
        formats, in listing order. Saved files aren't skipped: it shows everything the listing finds.

    Raises:
        Whatever the route, a category or a lazy post's `files` raises, such as `FetchError`.

    The listing is real (requests, pages, login), so a lazy post's `files` is called. The
    categories' `complete` records and the history are neither read nor written, and the site is
    closed however it ends, which also closes the category being read.
    """
    site = TargetSite(run, target)
    categories = {category.name: category for route in run.source.routes for category in route.categories}
    stream = posts(run.source, site, target, run.settings.options, run_async=run.run_async)
    try:
        for post in stream:
            files = list(post.files()) if callable(post.files) else list(post.files)
            yield from place(run.source, post, files, category=categories.get(post.category))
    finally:
        stream.close()
        site.close()


def place(source: Source, post: Post, files: Sequence[File], *, category: Category | None = None) -> list[Placed]:
    """Each file of a post named and keyed by the formats: the category's, else the source's.

        place(Example(), post, post.files)     # [Placed(path="posts/2026-01-02_C1abc_0.jpg", key="C1abc_0", …)]

    Args:
        source: the post's source, whose `folder`, `filename` and `key` formats apply.
        post: the post, its category set.
        files: its files, listed.
        category: the category it came from, whose own formats win; None for a route's own posts.

    The engine's fields (`ENGINE_FIELDS`) are set for each file: `num` from 0, `count`, `index` ""
    for a one-file post and `_<num>` otherwise, `extension` (".jpg": from the file's `name`, else
    its URL when that names a photo, video or audio extension, else "" and the content decides),
    and, for the key only, `path`. A formatted key that comes out empty is the file's `path`.
    A name the site built as the `filename` format would, with or without the day, is built
    again with the post's day now; any other name the site gave is kept.
    """
    folder, filename, key = (_format(source, category, name) for name in ("folder", "filename", "key"))
    date = day(post.date)
    placed = []
    for num, file in enumerate(files):
        extension, kind = _extension(file)
        values = {**post.fields, "id": post.id, "date": date, "category": post.category, "num": num,
                  "count": len(files), "index": "" if len(files) == 1 else f"_{num}", "extension": extension}
        formatted = fill(filename, values) or fmt.filename(post.id)
        if file.name is None:
            name = formatted + extension
        elif file.content is None and PurePosixPath(file.name).stem in _stems(filename, values):
            # The site built the format's own name, perhaps without the day the listing memory
            # found since: built again with it, keeping the site's extension.
            name = formatted + PurePosixPath(file.name).suffix
        else:
            name = fmt.filename(file.name)
        path = "/".join(part for part in (fill(folder, values), name) if part)
        placed.append(Placed(post, file, num, path, fill(key, {**values, "path": path}, safe=False) or path, kind,
                             values))
    return placed



def _stems(filename: str, values: Mapping[str, Any]) -> set[str]:
    """The names the `filename` format gives a file, with its day and without it."""
    return {fill(filename, values), fill(filename, {**values, "date": None})} - {""}

class Ledger:
    """What one target's listing has seen: the files queued, the skips, and the history it reads.

        outcome, todo = ledger.take(post)

    It runs on the listing's thread only, so it takes no lock.
    """

    def __init__(self, run: SourceRun, site: TargetSite, target: Target):
        """Make the ledger of one target, with nothing queued, skipped or remembered yet."""
        self.source, self.settings, self.site = run.source, run.settings, site
        self.folder = disk.target_folder(self.settings.home, target)
        self.skipped = 0
        self._categories = {category.name: category for route in self.source.routes
                            for category in route.categories}
        # Every path and history key queued so far, each to the name it was queued as, so one file
        # is never written twice and a skip can say which name holds it.
        self._queued: dict[Path | str, str] = {}
        # The files skipped in the category being listed, which `sum_up` reports when it ends: one
        # line per category, since a feed and a tab that list the same media can skip hundreds.
        self._category: str | None = None
        self._category_skips = 0
        # The listing memory: each post's first key to the day any copy of it, or an earlier run,
        # gave; and the undated posts waiting for their category to end, by that key.
        self._dates: dict[str, datetime.date] = {}
        self._waiting: dict[str, Post] = {}
        # Each keyed post of the category being listed, in order, with its day if it gave one: the
        # neighbours an undated post's estimated day comes from.
        self._order: list[tuple[str, datetime.date | None]] = []
        # The first keys whose day is a neighbour's estimate, which the history row marks.
        self._estimated: set[str] = set()
        # False once the site's `date` hook was refused (a block): the rest are estimated.
        self._asking = type(self.source).date is not Source.date
        # Files linked into a second category's folder, and whether a link was refused already.
        self.linked = 0
        self._link_warned = False
        # Links to files queued this run, by their key, made once the downloads are done.
        self._later: list[tuple[Path, str]] = []

    def take(self, post: Post, *, hold: bool = True) -> tuple[Outcome, list[_Queued]]:
        """Each file of one post not saved or queued yet; a lazy post's files are listed only if needed.

        Args:
            post: a post the listing kept, its category set.
            hold: whether an undated post whose date isn't known yet waits for `release`.

        Returns:
            What became of the post, and the files to submit, each marked queued already; a post
            held for its date counts neither way (`EMPTY`).

        Raises:
            Whatever a lazy post's `files` raises, such as `FetchError`.

        """
        # Step 1: a lazy post whose first key is saved is skipped before its files are listed, so its request is saved.
        category = self._categories.get(post.category)
        if post.category != self._category:
            self.sum_up()
            self._category = post.category
        first = self._first_key(post, category)
        dated = self._dated(post, category, hold=hold)
        if dated is None:
            return Outcome.EMPTY, []
        post = dated
        skipped = self._saved_first(post, category) if post.lazy else None
        if skipped is not None:
            self._skip()
            log.debug("skip {post}: already saved", post=post.id)
            return skipped, []
        # Step 2: list the files (a lazy post's `files` is called here), then queue each one not saved or queued.
        listed = list(post.files()) if callable(post.files) else list(post.files)
        found = 0
        todo: list[_Queued] = []
        for placed in place(self.source, post, listed, category=category):
            dest = self.folder.joinpath(*placed.path.split("/"))
            if placed.file.content is not None:
                # A text file is the source's own, written each time its post is downloaded, never keyed.
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Through a `.<name>.<random>.part`, which `flnd sync`'s repair removes if a run dies
                # mid-write; written as bytes, so every "\n" stays "\n" on Windows too.
                atomic.write(dest, placed.file.content.encode("utf-8"))
                continue
            key = f"{self.source.code}:{placed.key}"
            held = self._held(dest, key)
            if held is not None:
                self._skip()
                # Only a file on disk or in the history is caught up; one queued earlier in this run
                # may still fail, so it counts neither way toward `sync`'s stop.
                if dest not in self._queued and key not in self._queued:
                    found += 1
                # The same media is often listed twice under two names (a feed's dated post and a
                # tab's undated photo), so the line names the one that holds it.
                if held == placed.path:
                    log.debug("skip {name}: already saved", name=placed.path)
                else:
                    log.debug("skip {name}: already saved as {held}", name=placed.path, held=held)
                    if key in self._queued:
                        # Its holder is still downloading: linked once it is in place.
                        self._later.append((dest, key))
                    else:
                        self._link(dest, self.folder.joinpath(*held.split("/")))
                continue
            self._queued.update(dict.fromkeys((dest, key), placed.path))
            todo.append(_Queued(placed, dest, key, estimated=first in self._estimated))
        if todo:
            return Outcome.QUEUED, todo
        return (Outcome.SAVED if found else Outcome.EMPTY), []

    def link_queued(self, written: Mapping[str, Path]) -> None:
        """Make the links to files queued this run, now at their final paths in `written`."""
        for dest, key in self._later:
            holder = written.get(key)
            if holder is not None:
                # The content may have given the file another extension; the link takes the same.
                self._link(dest.with_suffix(holder.suffix), holder)
        self._later = []

    def _link(self, dest: Path, holder: Path) -> None:
        """Link a file held in another category's folder at the path it would have had in this one.

        A relative symlink, so the target's folder moves as a whole; else a hard link; else
        nothing, warned of once per run. Within one category nothing is linked.
        """
        if not holder.is_relative_to(self.folder):
            return
        here, there = dest.relative_to(self.folder).parts, holder.relative_to(self.folder).parts
        if len(here) < 2 or len(there) < 2 or here[0] == there[0] or not holder.is_file():
            return
        if dest.exists() or dest.is_symlink():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.symlink_to(os.path.relpath(holder, dest.parent))
        # Windows without Developer Mode or admin refuses a symlink; a hard link needs neither.
        except OSError:
            try:
                os.link(holder, dest)
            # FAT, exFAT or another volume: no link of either kind, so the file stays in one folder.
            except OSError as error:
                if not self._link_warned:
                    self._link_warned = True
                    self.site.warn("can't link files found in two folders here: {error}",
                                   error=error)
                return
        self.linked += 1
        log.debug("link {name} → {held}", name=dest.relative_to(self.folder).as_posix(),
                  held=holder.relative_to(self.folder).as_posix())

    def release(self) -> list[Post]:
        """The undated posts held since the last call, each with any day the memory found meanwhile.

        Called when a category ends: a dated copy in a later category is too late for these, but
        the history and every copy listed so far have had their say.
        """
        waiting, self._waiting = self._waiting, {}
        released = []
        for key, post in waiting.items():
            found = self._dates.get(key) or self._ask(post)
            if found is None:
                found = self._neighbour(key)
                if found is not None:
                    self._estimated.add(key)
            released.append(post if found is None else post.replace(date=found))
        self._order = []
        return released

    def redate(self) -> int:
        """Date each saved file of the target with no day or an estimated one, by the site's hook.

        The file is renamed to its dated name, and its row and modified time updated. When the
        dated name is taken by the same bytes, the undated copy is deleted; by other bytes, both
        stay as they are. A file the site can't date is asked again by the next `sync`.

        Returns:
            How many files were dated.
        """
        if not self._asking:
            return 0
        dated = 0
        history = self.settings.history
        undated = [(key, row) for key, row in list(history.items()) if self._undated(row)]
        for key, row in undated:
            found = self._ask(Post(row[ID]))
            if found is None:
                continue
            estimated = bool(row.get(ESTIMATED))
            path = _redated(Path(row["path"]), found, self.source.filename, estimated=estimated)
            if path is None:
                continue
            _set_mtime(path, found)
            fresh = {name: value for name, value in row.items() if name != ESTIMATED}
            self.settings.history[key] = {**fresh, "path": str(path), DATE: found.isoformat()}
            dated += 1
        if dated:
            self.site.info("{files} dated", files=fmt.count(dated, "saved file"))
        return dated

    def _undated(self, row: Any) -> bool:
        """Whether a history row is one of the target's files, saved undated or estimated."""
        if not isinstance(row, dict) or not isinstance(row.get(ID), str):
            return False
        in_folder = Path(row.get("path", "")).is_relative_to(self.folder)
        return in_folder and (DATE not in row or bool(row.get(ESTIMATED)))

    def _ask(self, post: Post) -> datetime.date | None:
        """The day the site's `date` hook gives a post, or None; a block stops asking."""
        if not self._asking:
            return None
        try:
            return day(self.source.date(self.site, post))
        except (FetchError, LoginError) as error:
            # A refusal of the whole site, not of this item: asking on would only be refused again.
            if isinstance(error, LoginError) or error.status in BLOCKS:
                self._asking = False
                self.site.warn("dating stopped: {error}; the rest are estimated", error=error)
            else:
                log.debug("{post}: not dated: {error}", post=post.id, error=error)
            return None

    def _neighbour(self, key: str) -> datetime.date | None:
        """The day of the nearest dated post in listing order, the one before first on a tie."""
        keys = [listed for listed, _ in self._order]
        if key not in keys:
            return None
        at = keys.index(key)
        for distance in range(1, len(self._order)):
            for index in (at - distance, at + distance):
                if 0 <= index < len(self._order) and self._order[index][1] is not None:
                    return self._order[index][1]
        return None

    def _dated(self, post: Post, category: Category | None, *, hold: bool) -> Post | None:
        """`post` with the day the memory knows, the memory told of its own; None while it waits.

        A post whose key needs its listed files, or holds its date, is never held.
        """
        key = self._first_key(post, category)
        if key is None:
            return post
        known = day(post.date)
        if hold:
            self._order.append((key, known))
        if known is not None:
            self._dates[key] = known
            # A dated copy replaces an undated one held meanwhile.
            self._waiting.pop(key, None)
            return post
        found = self._dates.get(key) or _row_day(self.settings.history.get(key))
        if found is not None:
            self._dates[key] = found
            return post.replace(date=found)
        if not hold:
            return post
        if key in self._waiting:
            # A second undated copy of a post already waiting: one of them is enough.
            self._skip()
            log.debug("skip {post}: listed twice", post=post.id)
            return None
        self._waiting[key] = post
        return None

    def _first_key(self, post: Post, category: Category | None) -> str | None:
        """The history key of a post's first file, from its fields alone; None if it needs more."""
        template = _format(self.source, category, "key")
        if not template or _fields(template) & (LISTED_FIELDS | {"date"}):
            return None
        values = {**post.fields, "id": post.id, "category": post.category, "num": 0}
        key = fill(template, values, safe=False)
        return f"{self.source.code}:{key}" if key else None

    def _saved_first(self, post: Post, category: Category | None) -> Outcome | None:
        """Whether every file of a lazy post is saved, by its keys alone; False when the key needs the listed files.

        The first file's history row holds the post's file count (`FILES`), so a post whose later
        file failed is listed again; a row without it (a post of one file) needs only its own key.
        `SAVED` when they are, `EMPTY` when one is only queued earlier in this run (it may still
        fail, so it counts neither way), None when the post must be listed.
        """
        template = _format(self.source, category, "key")
        if not template or _fields(template) & LISTED_FIELDS:
            return None
        values = {**post.fields, "id": post.id, "date": day(post.date), "category": post.category}
        keys = [f"{self.source.code}:{fill(template, {**values, 'num': num}, safe=False)}" for num in range(2)]
        if keys[0] == f"{self.source.code}:" or self._held(None, keys[0]) is None:
            return None
        row = self.settings.history.get(keys[0])
        count = row.get(FILES) if isinstance(row, dict) else None
        checked = [keys[0]]
        # A key without `num` can't tell the files apart; the first one stands for the post, as before.
        if isinstance(count, int) and count > 1 and keys[1] != keys[0]:
            checked += [f"{self.source.code}:{fill(template, {**values, 'num': num}, safe=False)}"
                        for num in range(1, count)]
            if any(self._held(None, key) is None for key in checked[1:]):
                return None
        return Outcome.EMPTY if any(key in self._queued for key in checked) else Outcome.SAVED

    def sum_up(self) -> None:
        """Log how many files the category being listed skipped as already saved, once, if any.

            ledger.sum_up()     # • ins:example_user: posts: 12 files already saved

        `queue` calls it when a post of another category comes, and `download` once the listing ends.
        """
        if self._category_skips:
            files = fmt.count(self._category_skips, "file")
            if self._category:
                self.site.info("{category}: {files} already saved", category=self._category, files=files)
            else:
                self.site.info("{files} already saved", files=files)
        self._category_skips = 0

    def _skip(self) -> None:
        """Count one skipped file, for the target's total and its category's line."""
        self.skipped += 1
        self._category_skips += 1

    def _held(self, dest: Path | None, key: str) -> str | None:
        """The name that already holds a file: queued this run, at `dest` and not empty, or where its history row says; None when it is new."""
        for known in (dest, key):
            if known is not None and known in self._queued:
                return self._queued[known]
        if dest is not None and dest.is_file() and dest.stat().st_size > 0:
            return self._name(dest)
        row = self.settings.history.get(key)
        found = Path(row.get("path", "")) if isinstance(row, dict) and row.get("path") else None
        return self._name(found) if found is not None and found.is_file() else None

    def _name(self, path: Path) -> str:
        """A saved file's name as a log line shows it: from the target's folder, or whole when it is elsewhere."""
        return path.relative_to(self.folder).as_posix() if path.is_relative_to(self.folder) else str(path)


class Downloads:
    """One target's downloads: the pool, the encodes, the refresh requests, and the history rows.

        downloads.submit(queued)
        written = downloads.finish()

    `submit` runs on the listing's thread; downloads run on the pool's, and video encodes on a
    pool of their own, one at a time across the run.
    """

    def __init__(self, run: SourceRun, target: Target, row: Any):
        """Make one target's downloads, with no pool started; the pool starts on the first file."""
        self.run, self.target, self.row = run, target, row
        self.source, self.settings = run.source, run.settings
        self._pool: worker.Pool | None = None
        self._encodes: worker.Pool | None = None
        # Each written file by its history key, at its path after shrinking (a compressed copy
        # may have a new name). Download threads and the encode pool's thread both write it, so
        # under the lock. A video counts from its download; another file once it is finished.
        self._written: dict[str, Path] = {}
        self._writing = threading.Lock()
        # Downloads submitted and not ended (`_pending`) and those started (`_running`), and the
        # refreshes download threads ask the listing's thread for: they all change them, under
        # `_changed`, which is notified on every change.
        self._changed = threading.Condition()
        self._pending = 0
        self._running = 0
        # Every download submitted so far, for the target's row, which counts them.
        self._submitted = 0
        self._counts = ""
        self._refreshes: deque[_Refresh] = deque()
        # False once it stops answering refreshes (`finish` after an error); a refresh asked for
        # then gets no fresh link, so no download waits for an answer that won't come.
        self._answering = True
        # The thread that lists, where a lazy post's `files` may run.
        self._lister = threading.current_thread()

    @property
    def written(self) -> dict[str, Path]:
        """Each file written so far by its history key, at its final path."""
        with self._writing:
            return dict(self._written)

    def submit(self, queued: _Queued) -> bool:
        """Start one file's download once the pool has room; False when Ctrl-C stopped the wait."""
        # Waiting for room keeps the pool from growing without bound while the listing runs ahead.
        if not self._room():
            return False
        try:
            self._downloads().submit(self._task, queued)
        except BaseException:
            self._ended()
            raise
        return True

    def wait(self, row: Any = None) -> None:
        """Wait until every download submitted has ended, answering their refresh requests meanwhile.

        Args:
            row: the target's progress row, which says how many files are finishing after the
                first Ctrl-C; None says nothing.

        Raises:
            KeyboardInterrupt: a second Ctrl-C (`net.stopping_now`); the downloads stop too.
        """
        shown = None
        while True:
            self._answer()
            with self._changed:
                if not self._pending:
                    return
                if net.stopping_now.is_set():
                    raise KeyboardInterrupt
                text = FINISHING.format(files=fmt.count(self._running, "file")) if net.stopping.is_set() else None
                if text == shown:
                    self._changed.wait(WAKE)
                    continue
            # The row takes the screen's lock, so it's set outside this one.
            if row is not None and text is not None:
                row.caption = text
            shown = text

    def finish(self, error: BaseException | None = None) -> tuple[Path, ...]:
        """Wait for the downloads and their encodes (or, after `error`, drop the queued ones), and give the files written.

        Args:
            error: what ended the listing, if anything; queued downloads are then dropped, and
                running ones finish.

        Returns:
            Each file written, at its path after shrinking, after logging each failed one.
        """
        if error is None:
            self.wait()
        else:
            # The job's thread is about to wait inside the pool, where it can't answer: a
            # download asking for a fresh link now gets none, and fails as its old link did.
            with self._changed:
                self._answering = False
                waiting, self._refreshes = list(self._refreshes), deque()
            for refresh in waiting:
                refresh.done.set()
        # Downloads queue video encodes, so their pool ends first; the target waits for both.
        for name in ("_pool", "_encodes"):
            pool: worker.Pool | None = getattr(self, name)
            if pool is None:
                continue
            pool.__exit__(type(error) if error else None, error, error.__traceback__ if error else None)
            # The pool keeps failures in its results instead of raising, so each one is named here.
            for result in pool.results:
                if not result.ok:
                    item = result.item[0] if isinstance(result.item, tuple) else result.item
                    log.error("{file}: {kind} {error}", file=item, kind=type(result.error).__name__,
                              error=result.error)
                    if result.error is not None:
                        log.debug("{file}: {chain}", file=item, chain=causes(result.error))
        with self._writing:
            return tuple(self._written.values())

    def _downloads(self) -> worker.Pool:
        """The target's download pool, made on the first file: the source's `concurrency` at once."""
        if self._pool is None:
            from shared import worker

            self._pool = worker.pool(concurrency=self.source.concurrency, label=str(self.target))
        return self._pool

    def _encoder(self) -> worker.Pool:
        """The target's pool for video encodes, which wait for the run's one encode at a time."""
        if self._encodes is None:
            from shared import worker

            self._encodes = worker.pool(concurrency=1)
        return self._encodes

    def _room(self) -> bool:
        """Wait until the pool can take one more download without blocking, and count it; False after Ctrl-C.

        The pool's own `submit` would wait too, but there the job's thread couldn't answer a
        refresh, and every running download might be waiting for one.
        """
        limit = 2 * max(self.source.concurrency, 1)
        while True:
            self._answer()
            with self._changed:
                if net.stopping.is_set():
                    return False
                if self._pending < limit:
                    self._pending += 1
                    self._submitted += 1
                    break
                self._changed.wait(WAKE)
        self._show_counts()
        return True

    def _ended(self) -> None:
        """One submitted download has ended, however."""
        with self._changed:
            self._pending -= 1
            self._changed.notify_all()
        self._show_counts()

    def _show_counts(self) -> None:
        """Caption the target's row with its files, such as `120 files · 4 running · 31 queued`.

        The row, not one row per queued file, says how many wait, so the live area's height stays
        steady however many files a target has. Once Ctrl-C was pressed `wait` captions it instead.
        """
        row = self.row
        if row is None or net.stopping.is_set():
            return
        with self._changed:
            text = f"{fmt.count(self._submitted, 'file')} · {self._running} running · {self._pending - self._running} queued"
        # Compared outside the lock on purpose: two threads racing here only set the same caption twice.
        if text != self._counts:
            self._counts = text
            row.caption = text

    def _task(self, queued: _Queued) -> bool:
        """One queued download on a pool thread, unless Ctrl-C came first: a file not yet started is dropped."""
        try:
            with self._changed:
                if net.stopping.is_set():
                    return False
                self._running += 1
            self._show_counts()
            try:
                # The file's bar is drawn under the target's row, one of at most `concurrency`.
                with log.under(self.row):
                    return self._download(queued)
            finally:
                with self._changed:
                    self._running -= 1
        finally:
            self._ended()

    def _download(self, queued: _Queued) -> bool:
        """Download one file on a pool thread, then finish it; False when it was refused."""
        placed = queued.placed
        received: dict[str, str] = {}
        try:
            saved = self._fetch(queued, placed.file, received)
            if saved is None and queued.refreshed is not None:
                refreshed = self._expired(queued)
                saved = self._fetch(queued, refreshed, received) if refreshed is not None else None
        except FetchError as error:
            # Refused, or its fresh link refused too: the same answer next time, so logged, not retried.
            log.error("{name}: {error}", name=placed.path, error=error)
            log.debug("{name}: {chain}", name=placed.path, chain=causes(error))
            return False
        except KeyboardInterrupt:
            if net.stopping.is_set() and not net.stopping_now.is_set():
                # A rate-limit wait the first Ctrl-C cut short: the file is left for the next run.
                return False
            raise
        if saved is None:
            return False
        # The kind comes from the name the file got, which the content may have changed (`_as_media`).
        kind = disk.kind_of(saved.name)
        row = {"path": str(saved), "size": saved.stat().st_size,
               "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
        posted = day(placed.post.date)
        # The post's id and day: the listing memory gives the day to a later undated copy, and
        # `sync` asks the site's `date` hook by the id for a file saved undated or estimated.
        row[ID] = placed.post.id
        if posted is not None:
            row[DATE] = posted.isoformat()
        if queued.estimated:
            row[ESTIMATED] = True
        count = placed.values.get("count", 1)
        if placed.num == 0 and isinstance(count, int) and count > 1:
            # The first file's row holds the post's file count, so a later run skips the lazy post
            # only when every one of its files is saved, not just this one.
            row[FILES] = count
        shrink = bool(self.settings.options.shrink and kind in KINDS)
        # The post's day dates the file; else the server's Last-Modified does.
        dated = day(placed.post.date) or _served_day(received.get("last-modified"))
        finished = _Finished(row, kind if shrink else None, dated)
        if shrink and kind in ONE_AT_A_TIME:
            # A video waits for the run's one encode at a time on a pool of its own, so downloads go on.
            # It counts as written now, as its download is done; its encode only moves it.
            with self._writing:
                self._written[queued.key] = saved
            self._encoder().submit(self._finish, queued.key, saved, finished)
        else:
            self._finish(queued.key, saved, finished)
        return True

    def _fetch(self, queued: _Queued, file: File, received: dict[str, str]) -> Path | None:
        """`net.download` of one file onto its place; None when its link expired and a lazy post can refresh it.

        Raises:
            FetchError: the file was refused, and refreshing can't help (not a 403 or 410, or a
                post whose files were listed, not a function).
        """
        placed = queued.placed
        headers = placed.post.headers
        source = self._sync_fetch(file.url) if callable(file.url) else file.url
        if source is None:
            # Only a text file has no URL, and it is written by `queue`, never downloaded.
            return None
        try:
            return net.download(self.run.files, source, queued.dest, label=placed.path,
                                headers=headers, size=file.size, kind=placed.kind, audio=file.audio,
                                received=received)
        except FetchError as error:
            if error.status not in EXPIRED or not callable(placed.post.files) or queued.refreshed:
                raise
            log.info("{name}: HTTP {status}, so the link may have expired; asking for a fresh one",
                     name=placed.path, status=error.status)
            queued.refreshed = error
            return None

    def _expired(self, queued: _Queued) -> File | None:
        """The file with the same `num` from a lazy post's `files`, called once more; raises the old refusal without one."""
        refusal = queued.refreshed
        assert refusal is not None
        refresh = _Refresh(queued.placed.post)
        if threading.current_thread() is self._lister:
            refresh.run()
        else:
            with self._changed:
                if self._answering:
                    self._refreshes.append(refresh)
                    self._changed.notify_all()
                else:
                    refresh.done.set()
            while not refresh.done.wait(WAKE):
                if net.stopping_now.is_set():
                    raise KeyboardInterrupt
        if refresh.error is not None:
            log.warn("{name}: no fresh link: {error}", name=queued.placed.path, error=refresh.error)
            raise refusal
        num = queued.placed.num
        fresh = refresh.files[num] if refresh.files is not None and num < len(refresh.files) else None
        if fresh is None or fresh.content is not None:
            # The post no longer has that file (deleted, or out of `--time` now).
            raise refusal
        return fresh

    def _answer(self) -> None:
        """Run the refreshes download threads asked for, on this, the job's thread."""
        while True:
            with self._changed:
                if not self._refreshes:
                    return
                refresh = self._refreshes.popleft()
            refresh.run()

    def _finish(self, key: str, path: Path, finished: _Finished) -> None:
        """Shrink the file if asked, date it, then write its row: last, so a row means a whole file."""
        if finished.shrink is not None:
            # Slow: a photo takes a moment, a video minutes. A file this machine can't compress is
            # left with no `compressed` field, so a later run with ffmpeg compresses it.
            found = self.settings.compressor.compress(path, finished.shrink)
            if found is not None:
                finished.row.update(found)
                path = Path(found.get("path", path))
        if finished.day is not None:
            _set_mtime(path, finished.day)
        with self._writing:
            self._written[key] = path
        self.settings.history[key] = finished.row

    def _sync_fetch(self, fetch: Callable[..., Any]) -> Callable[..., Any]:
        """A `Fetch` function whose coroutine, if it returns one, runs on the source's loop."""
        def call(dest: Path, progress: Callable[..., None]) -> Any:
            outcome = fetch(dest, progress)
            return self.run.run_async(outcome) if inspect.iscoroutine(outcome) else outcome

        return call



def download(run: SourceRun, target: Target, row: Any, *, sync: bool = False) -> Done:
    """Download one target of a ported source, ending its row at ok or error, and say what it did.

        done = download(run, Target("loi", "post", "5930"), row)
        len(done.written)                   # how many files were written

    Args:
        run: the source's resources for the run.
        target: the target, as `Engine.resolve` gives it.
        row: its progress row, which ends at ok, or at error when the route or a category raised.
        sync: stop each `newest_first` category after 5 already-saved posts in a row, when its
            last listing reached its end (A9 rule 3). Without that record, or without `sync`, it
            lists to its end and skips what is saved.

    Returns:
        The files written, how many were skipped, the error that failed the target if one did,
        and the categories that listed to their end (even when a later one failed).

    Raises:
        KeyboardInterrupt: a second Ctrl-C, after the running downloads stopped, keeping their
            resumable parts; other `BaseException`s, after the queued downloads are dropped and
            the running ones finish. An `Exception` is logged and fails the row instead.

    After a first Ctrl-C it returns: the row ends at error with "stopped", and `Done.error` is
    `STOPPED`; see the module for what finishes.
    """
    # One site and one job for the target: the site holds its browser and pacing, the job its downloads.
    site = TargetSite(run, target)
    ledger, downloads = Ledger(run, site, target), Downloads(run, target, row)
    work = _Work(ledger, downloads)
    # A target's row shows its running files, as many as the source downloads at once.
    row.slots = run.source.concurrency
    complete: list[str] = []
    listed = _listed(run.settings.home, target) if sync else {}
    resumes: dict[str, Resume] = {}
    error: BaseException | None = None
    try:
        try:
            if sync:
                ledger.redate()
            # The first Ctrl-C stops listing between posts (D8); `queue` sees it too, between files.
            _read(work, run, site, target, listed=listed, resumes=resumes, complete=complete)
        except KeyboardInterrupt:
            # The first Ctrl-C ends a rate-limit or pacing wait at once, raising this; that is
            # the graceful stop, not a failure. A second one is.
            if not net.stopping.is_set() or net.stopping_now.is_set():
                raise
        finally:
            site.close()
            ledger.sum_up()
        downloads.wait(row)
    except BaseException as caught:  # a route or a category failed, or a second Ctrl-C: handled below
        error = caught
    written = downloads.finish(error)
    ledger.link_queued(downloads.written)
    # A category cut short keeps the cursor of the page it was reading, for the next run to go on
    # from; one that ended by itself has nothing to resume.
    cursors = {name: None if name in complete else resume.cursor
               for name, resume in resumes.items()}
    saved_settings.keep_cursors(run.settings.home, target, cursors)
    if error is not None:
        if not isinstance(error, Exception):
            raise error
        log.error("{target}: {error}", target=str(target), error=error)
        log.debug("{target}: {chain}", target=str(target), chain=causes(error))
        row.fail()
        return Done(written, ledger.skipped, str(error), tuple(complete))
    if net.stopping.is_set():
        row.caption = "stopped"
        row.fail()
        return Done(written, ledger.skipped, STOPPED, tuple(complete))
    row.finish()
    log.debug("{target}: {written} written, {skipped} skipped; complete: {complete}", target=str(target),
              written=len(written), skipped=ledger.skipped, complete=", ".join(complete) or "none")
    return Done(written, ledger.skipped, None, tuple(complete))


def causes(error: BaseException) -> str:
    """An exception and every cause under it, for the run log: `FetchError: HTTP 403 ← HTTPStatusError: …`.

        log.debug("{name}: {chain}", name=path, chain=causes(error))

    Args:
        error: the exception; its `__cause__`, else its `__context__` unless suppressed, is followed
            down, each once.
    """
    links: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        links.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return " ← ".join(links)



@dataclass
class _Finished:
    """What finishing a downloaded file needs: its row so far, its kind to shrink, its day."""

    row: dict[str, Any]
    # Its kind when the run shrinks it (`--shrink`), else None.
    shrink: str | None
    # The day its modified time is set to; None keeps the download time.
    day: datetime.date | None


def _redated(path: Path, found: datetime.date, filename: str, *, estimated: bool) -> Path | None:
    """Rename a saved file to its name with `found` as its day; None if other bytes hold that name.

    A format that starts with the day (`{date}_…`) puts it in front, replacing an estimated one;
    any other name keeps its name, and only its row and modified time change.
    """
    name = path.name
    leading = LEADING_DAY.match(name)
    if estimated and leading:
        name = name[leading.end():]
    if filename.startswith("{date}_") and not LEADING_DAY.match(name):
        name = f"{found.isoformat()}_{name}"
    target = path.with_name(name)
    if target == path or not path.exists():
        return path if path.exists() else None
    if target.exists():
        # The dated copy was saved too, before the day was known: the same bytes keep the dated one.
        if not filecmp.cmp(path, target, shallow=False):
            return None
        path.unlink()
        return target
    path.rename(target)
    return target


def _row_day(row: Any) -> datetime.date | None:
    """The post's day a history row recorded, or None."""
    value = row.get(DATE) if isinstance(row, dict) else None
    try:
        return datetime.date.fromisoformat(value) if isinstance(value, str) else None
    # A row edited by hand, or by another version: no day, rather than a failed run.
    except ValueError:
        return None


def _set_mtime(path: Path, dated: datetime.date) -> None:
    """Set a file's modified time to noon UTC of its day, the same day from UTC-11 to UTC+11."""
    noon = datetime.datetime.combine(dated, datetime.time(NOON), tzinfo=datetime.timezone.utc)
    stamp = noon.timestamp()
    try:
        os.utime(path, (stamp, stamp))
    # The file was just written, so only a disk or permission problem lands here; its date is a
    # convenience, never a reason to fail the download.
    except OSError as error:
        log.debug("{name}: modified time not set: {error}", name=path.name, error=error)


def _served_day(value: str | None) -> datetime.date | None:
    """The UTC day of an HTTP date such as `Last-Modified`; None if missing or unreadable."""
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    # A server's malformed date says nothing about the file; the download day stands.
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).date()

@dataclass
class _Queued:
    """One queued download. It prints as its path, so a failed download names its file."""

    placed: Placed
    dest: Path
    key: str
    # Whether its post's day is the nearest dated neighbour's, which `sync` asks the site again for.
    estimated: bool = False
    # The refusal of its first link, once its lazy post was asked for a fresh one: once per file.
    refreshed: FetchError | None = None

    def __str__(self) -> str:
        return self.placed.path


@dataclass(eq=False)
class _Refresh:
    """A download's request for a lazy post's files, listed once more on the job's thread."""

    post: Post
    # Set once answered, with `files` or `error`; or with neither, when the job stopped answering.
    done: threading.Event = field(default_factory=threading.Event)
    files: list[File] | None = None
    error: Exception | None = None

    def run(self) -> None:
        """Call the post's `files` and keep what it gives or raises, then say it's done."""
        try:
            assert callable(self.post.files)
            self.files = list(self.post.files())
        except Exception as error:  # the source's own failure, such as a FetchError: the download reports it
            self.error = error
        except KeyboardInterrupt:
            # The first Ctrl-C ends a rate-limit or pacing wait inside `files` at once: that is
            # the graceful stop, so there is no fresh link and the target ends as stopped. A
            # second Ctrl-C stops everything.
            if not net.stopping.is_set() or net.stopping_now.is_set():
                raise
        finally:
            self.done.set()


class _Kept:
    """The posts a route or category yielded that `--time` keeps, each with its `category` set.

    `ran_out` is True once the method's posts ran out by themselves, and stays False after a
    `newest_first` early stop (5 posts in a row older than `--time`'s since) or a close.
    """

    def __init__(self, yielded: Any, category: str, *, newest_first: bool, time: Range,
                 run_async: RunAsync):
        if inspect.iscoroutine(yielded):
            yielded = run_async(yielded)
        self.category, self.newest_first, self.time = category, newest_first, time
        self.ran_out = False
        self._steps = _steps(yielded, run_async)

    def __iter__(self) -> Iterator[Post]:
        """Each kept post, its category set; ends after 5 posts in a row older than `--time`."""
        older = 0
        try:
            for post in self._steps:
                if not isinstance(post, Post):
                    name = type(post).__name__
                    raise TypeError(f"{self.category or 'a route'} yielded {name}, not a Post")
                if self.category:
                    post = post.filed(self.category)
                posted = day(post.date)
                if posted in self.time:
                    older = 0
                    yield post
                    continue
                since = self.time.since
                if self.newest_first and since and posted and posted < since:
                    older += 1
                    if older >= OLDER_IN_A_ROW:
                        return
            self.ran_out = True
        finally:
            self.close()

    def close(self) -> None:
        """Close the method's generator, so its `finally` blocks run; a second call does nothing."""
        self._steps.close()


class Outcome(Enum):
    """What became of one post the listing kept."""

    # At least one file was queued.
    QUEUED = "queued"
    # Every file was saved already: toward `sync`'s saved-run stop.
    SAVED = "saved"
    # No file to save, or only files queued earlier in this run (they may fail): counts neither way.
    EMPTY = "empty"
    # Ctrl-C stopped it before every file was queued.
    STOPPED = "stopped"


class Ended(Enum):
    """How one listing ended."""

    # Its posts ran out by themselves: the category is complete.
    RAN_OUT = "ran out"
    # An early stop: 5 posts older than `--time`, or 5 saved posts in a row.
    CUT = "cut"
    # Ctrl-C: nothing more is listed.
    STOPPED = "stopped"



@dataclass(frozen=True)
class _Work:
    """One target's ledger and downloads, which each post the listing keeps goes through."""

    ledger: Ledger
    downloads: Downloads

    def queue(self, post: Post, *, hold: bool = True) -> Outcome:
        """Take one post into the ledger and submit each file it gives; `STOPPED` on Ctrl-C."""
        outcome, todo = self.ledger.take(post, hold=hold)
        for queued in todo:
            if not self.downloads.submit(queued):
                return Outcome.STOPPED
        return outcome

def _queue_all(work: _Work, kept: _Kept, *, stop_at_saved: bool) -> Ended:
    """Queue each post a listing keeps, then the undated ones it held, until Ctrl-C or a stop."""
    ended = _queue_listed(work, kept, stop_at_saved=stop_at_saved)
    if ended is Ended.STOPPED:
        return ended
    # The category is over: an undated post goes now, with the day the memory found, if any.
    for post in work.ledger.release():
        if work.queue(post, hold=False) is Outcome.STOPPED:
            return Ended.STOPPED
    return ended


def _queue_listed(work: _Work, kept: _Kept, *, stop_at_saved: bool) -> Ended:
    """Queue each post a listing keeps, until Ctrl-C or, if `stop_at_saved`, 5 saved in a row."""
    saved_in_a_row = 0
    try:
        for post in kept:
            outcome = Outcome.STOPPED if net.stopping.is_set() else work.queue(post)
            if outcome is Outcome.STOPPED:
                return Ended.STOPPED
            if outcome is not Outcome.EMPTY:
                saved_in_a_row = saved_in_a_row + 1 if outcome is Outcome.SAVED else 0
            if stop_at_saved and saved_in_a_row >= SAVED_IN_A_ROW:
                log.info("{category}: {count} saved posts in a row, caught up",
                         category=kept.category, count=saved_in_a_row)
                return Ended.CUT
    finally:
        kept.close()
    return Ended.RAN_OUT if kept.ran_out else Ended.CUT


def _read(work: _Work, run: SourceRun, site: Site, target: Target, *, listed: Collection[str],
          resumes: dict[str, Resume], complete: list[str]) -> None:
    """List the target's route and wanted categories into the job, each from its saved cursor.

    A `newest_first` category in `listed` stops after 5 saved posts in a row. One with a cursor an
    interrupted run saved lists from it to the end, then from the newest down to 5 saved posts in
    a row. Each category that ran out goes into `complete`; each category's `Resume` into `resumes`.
    """
    options, run_async = run.settings.options, run.run_async
    time = Range(options.since, options.until)
    route = next(route for route in run.source.routes if route.kind == target.kind)
    categories = wanted(type(run.source), route, options)
    if route.categories and not categories:
        return
    context = _context(run.source, route, site, target, run_async)
    if not route.categories:
        log.debug("{target}: list {route}", target=str(target), route=route.kind)
        own = _Kept(context, "", newest_first=False, time=time, run_async=run_async)
        _queue_all(work, own, stop_at_saved=False)
        return
    cursors = saved_settings.cursors(run.settings.home, target)
    for category in categories:
        # A phase mark for the run log, so a reader finds which listing a line belongs to.
        log.debug("{target}: list {category}", target=str(target), category=category.name)
        start = cursors.get(category.name) if category.newest_first else None
        resume = resumes[category.name] = Resume(start, label=f"{target}: {category.name}")
        if start:
            log.info("{target}: {category}: going on where an interrupted run stopped",
                     target=str(target), category=category.name)
        method = getattr(run.source, category.name)
        kept = _Kept(method(site, context), category.name, newest_first=category.newest_first,
                     time=time, run_async=run_async)
        stop_at_saved = category.newest_first and category.name in listed and not start
        with listing(resume):
            ended = _queue_all(work, kept, stop_at_saved=stop_at_saved)
        if ended is Ended.STOPPED:
            return
        if ended is Ended.RAN_OUT:
            complete.append(category.name)
        if ended is Ended.RAN_OUT and start and not resume.refused:
            # The newest posts came after the interrupted run began: read down to where it started,
            # which 5 saved posts in a row mark.
            log.debug("{target}: list {category} from the newest", target=str(target),
                      category=category.name)
            kept = _Kept(method(site, context), category.name, newest_first=True, time=time,
                         run_async=run_async)
            with listing(Resume()):
                if _queue_all(work, kept, stop_at_saved=True) is Ended.STOPPED:
                    return


def _listed(home: Path, target: Target) -> dict[str, str]:
    """The categories of a target whose last listing reached its end, each with the day: `targets.db`'s `complete` record."""
    row = saved_settings.rows(home).get(saved_settings.key(target), {})
    found = row.get(saved_settings.COMPLETE) if isinstance(row, dict) else None
    return dict(found) if isinstance(found, dict) else {}


def _steps(yielded: Any, run_async: RunAsync) -> Generator[Any, None, None]:
    """Iterate a sync or async iterable from the job's thread; closing it closes the source's generator."""
    if not hasattr(yielded, "__anext__"):
        iterator = iter(yielded if yielded is not None else ())
        try:
            yield from iterator
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        return
    try:
        while True:
            try:
                yield run_async(yielded.__anext__())
            except StopAsyncIteration:
                return
    finally:
        run_async(yielded.aclose())


def _extension(file: File) -> tuple[str, str | None]:
    """A file's extension before its content is checked, and the kind `net.download` checks it as."""
    if file.content is not None:
        return PurePosixPath(file.name or "").suffix, None
    if file.name is not None:
        extension = PurePosixPath(file.name).suffix
        return extension, disk.kind_of(file.name)
    url = file.url if isinstance(file.url, str) else file.url[0] if isinstance(file.url, tuple) else ""
    extension = PurePosixPath(urlsplit(url).path).suffix.lower() if url else ""
    # `kind_of` reads a name's extension, and ".jpg" alone is a name without one.
    kind = disk.kind_of(f"file{extension}") if extension else None
    if file.audio is not None and kind != disk.VIDEO:
        # A DASH video whose URL names no video extension: merged, it is an MP4.
        return DASH_EXTENSION, disk.VIDEO
    if kind is None:
        # Neither its name nor its URL says what it is: its content decides, extension and all.
        return "", disk.SNIFF
    return extension, kind


def _fields(template: str) -> set[str]:
    """The names of the fields a format uses, `{a.b}` and `{a[0]}` counted as `a`."""
    return {name.split(".")[0].split("[")[0] for _, name, _, _ in _FORMATTER.parse(template) if name}


def _format(source: Source, category: Category | None, name: str) -> str:
    """The `folder`, `filename` or `key` format for a category's files: its own when it gives one, else the source's."""
    own = getattr(category, name, None)
    return own if own is not None else getattr(source, name)


# Reads a format's fields for `_fields`.
_FORMATTER = string.Formatter()
