"""The job: one target of a ported source, from its route to its history rows, in one fixed pipeline.

How it works, for one target (plan/phase-6/redesign.md, B3):

    route(site, value) ──► for each wanted category, in declaration order:
        for post in category(site, context):                        ← the source
            set category; read date                                 ← `posts`
            --time filter; newest_first early stop                  ← `posts`
            files = post.files() if lazy, unless its first key is saved
            for num, file: name from the formats; skip if saved or queued   ← `place`
            queue the download ─► net.download                      ← `Job._download`
            postprocess: compress → (classify) → mtime              ← flnd/post.py
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
import inspect
import string
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Collection, Coroutine, Generator, Mapping, Sequence
from urllib.parse import urlsplit

from flnd import post as postprocess
from flnd import settings as saved_settings
from flnd.common import files as disk
from flnd.common import net
from flnd.common.compress import KINDS, choice_of
from flnd.site import SourceRun, TargetSite
from flnd.source import Category, FetchError, File, Post, Range, Route, Site, Source, Target, day, fill
from shared import atomic, fmt, log

if TYPE_CHECKING:
    from shared import worker

    from flnd.session import Options

__all__ = ["STOPPED", "Done", "Job", "Placed", "download", "place", "plan", "posts", "wanted"]

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

    # Every file written, where it ended up after its postprocessors, in the order they finished.
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


def posts(source: Source, site: Site, target: Target, options: Options, *, run_async: RunAsync,
          ended: Callable[[str], None] | None = None, saved: Callable[[], bool | None] | None = None,
          listed: Collection[str] = ()) -> Generator[Post, None, None]:
    """Every post the target's route and wanted categories yield that `--time` keeps, its `category` set.

        for post in posts(source, site, target, options, run_async=run.run_async): ...

    Args:
        source: the target's source.
        site: the handle the route and categories act through.
        target: the target, whose `kind` names its route.
        options: the run's options: `--from` and `--time`.
        run_async: runs a coroutine to its end, for an `async def` route or category.
        ended: called with a category's name when its generator ends by itself, having yielded
            every post; not after a `newest_first` early stop, an error, or this iterator closing.
        saved: says, once the caller has handled the post last yielded, whether all of its files
            were already saved; None for a run that doesn't stop at saved posts (`download`).
        listed: the categories whose last listing reached its end (`targets.db`'s `complete`
            record). With `saved`, a `newest_first` one of these ends after 5 saved posts in a row.

    Raises:
        Whatever the route or a category raises, such as `FetchError` or `LoginError`.

    Closing this iterator closes the category being read, so its `finally` blocks run.
    """
    route = next(route for route in source.routes if route.kind == target.kind)
    time = Range(options.since, options.until)
    function = getattr(source, route.kind)
    if not route.categories:
        yield from _kept(function(site, target.value), "", newest_first=False, time=time, run_async=run_async,
                         saved=None)
        return
    categories = wanted(type(source), route, options)
    if not categories:
        return
    context = function(site, target.value)
    if inspect.iscoroutine(context):
        context = run_async(context)
    for category in categories:
        yielded = getattr(source, category.attribute)(site, context)
        stops = saved if category.newest_first and category.name in listed else None
        finished = yield from _kept(yielded, category.name, newest_first=category.newest_first, time=time,
                                    run_async=run_async, saved=stops)
        if finished and ended is not None:
            ended(category.name)


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
    site = TargetSite(run, target, row=row)
    categories = {category.name: category for route in run.source.routes for category in route.categories}
    listing = posts(run.source, site, target, run.settings.options, run_async=run.run_async)
    try:
        for post in listing:
            files = list(post.files()) if callable(post.files) else list(post.files)
            yield from place(run.source, post, files, category=categories.get(post.category))
    finally:
        listing.close()
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
    """
    folder, filename, key = (_format(source, category, name) for name in ("folder", "filename", "key"))
    date = day(post.date)
    placed = []
    for num, file in enumerate(files):
        extension, kind = _extension(file)
        values = {**post.fields, "id": post.id, "date": date, "category": post.category, "num": num,
                  "count": len(files), "index": "" if len(files) == 1 else f"_{num}", "extension": extension}
        if file.name is not None:
            name = fmt.filename(file.name)
        else:
            name = (fill(filename, values) or fmt.filename(post.id)) + extension
        path = "/".join(part for part in (fill(folder, values), name) if part)
        placed.append(Placed(post, file, num, path, fill(key, {**values, "path": path}, safe=False) or path, kind,
                             values))
    return placed


class Job:
    """One target's downloads: the pool, the encodes, what is queued, and the history rows.

        job = Job(run, site, target)
        job.queue(post)                 # every file of one post, as `download` lists them
        written = job.finish()

    Args:
        run: the source's resources for the run.
        site: the target's `TargetSite`.
        target: the target.

    `queue` runs on the job's thread; downloads run on the pool's, and video encodes on a pool of
    their own, one at a time across the run.
    """

    def __init__(self, run: SourceRun, site: TargetSite, target: Target):
        self.run, self.site, self.target = run, site, target
        self.source = run.source
        self.settings = run.settings
        self.folder = disk.target_folder(self.settings.home, target)
        self.skipped = 0
        # Whether the post `queue` last took had every file already saved (none queued), which
        # `sync`'s saved-run stop counts; None for a post with no file to save (none, or text only),
        # which neither counts nor breaks the run.
        self.caught_up: bool | None = False
        self._categories = {category.name: category for route in self.source.routes for category in route.categories}
        self._pool: worker.Pool | None = None
        self._encodes: worker.Pool | None = None
        # Every path and history key queued so far, so one file is never written twice.
        self._queued: set[Path | str] = set()
        # Each written file by its history key, at its path after postprocessing (a compressed copy
        # may have a new name). Download threads and the encode pool's thread both write it, so
        # under the lock. A video counts from its download, as it always did; another file once
        # its postprocessing is done.
        self._written: dict[str, Path] = {}
        self._writing = threading.Lock()
        # Downloads submitted and not ended (`_pending`) and those started (`_running`), and the
        # refreshes download threads ask the job's thread for: download threads and the job's
        # thread all change them, under `_changed`, which is notified on every change.
        self._changed = threading.Condition()
        self._pending = 0
        self._running = 0
        self._refreshes: deque[_Refresh] = deque()
        # False once the job stops answering refreshes (`finish` after an error); a refresh
        # asked for then gets no fresh link, so no download waits for an answer that won't come.
        self._answering = True
        # The thread that lists, where a lazy post's `files` may run.
        self._lister = threading.current_thread()

    def queue(self, post: Post) -> bool:
        """Queue every file of one post that isn't saved or queued yet, listing a lazy post's files only if needed.

        Args:
            post: a post `posts` kept, its category set.

        Returns:
            False when Ctrl-C stopped it before every file was queued, so the listing stops too.

        Raises:
            Whatever a lazy post's `files` raises, such as `FetchError`.

        It waits while the pool holds twice its `concurrency` in downloads, answering the
        downloads' refresh requests meanwhile.
        """
        category = self._categories.get(post.category)
        self.caught_up = False
        if post.lazy and self._saved_first(post, category):
            self.skipped += 1
            self.caught_up = True
            log.info("skip {post}", post=post.id)
            return True
        listed = list(post.files()) if callable(post.files) else list(post.files)
        found = fresh = 0
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
            if dest in self._queued or key in self._queued or self._saved(dest, key):
                self.skipped += 1
                found += 1
                log.info("skip {name}", name=placed.path)
                continue
            if not self._room():
                return False
            fresh += 1
            self._queued.update((dest, key))
            try:
                self._downloads().submit(self._task, _Queued(placed, dest, key))
            except BaseException:
                self._ended()
                raise
        self.caught_up = (not fresh) if found else (False if fresh else None)
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
            Each file written, at its path after postprocessing, after logging each failed one.
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
        with self._writing:
            return tuple(self._written.values())

    def _saved_first(self, post: Post, category: Category | None) -> bool:
        """Whether a lazy post's first file is saved, by its key alone; False when the key needs the listed files."""
        template = _format(self.source, category, "key")
        if not template or _fields(template) & LISTED_FIELDS:
            return False
        values = {**post.fields, "id": post.id, "date": day(post.date), "category": post.category, "num": 0}
        key = fill(template, values, safe=False)
        return bool(key) and self._saved(None, f"{self.source.code}:{key}")

    def _saved(self, dest: Path | None, key: str) -> bool:
        """Whether a file is saved: at `dest` and not empty, or where its history row says."""
        if dest is not None and dest.is_file() and dest.stat().st_size > 0:
            return True
        row = self.settings.history.get(key)
        found = Path(row.get("path", "")) if isinstance(row, dict) and row.get("path") else None
        return found is not None and found.is_file()

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
                    return True
                self._changed.wait(WAKE)

    def _ended(self) -> None:
        """One submitted download has ended, however."""
        with self._changed:
            self._pending -= 1
            self._changed.notify_all()

    def _task(self, queued: _Queued) -> bool:
        """One queued download on a pool thread, unless Ctrl-C came first: a file not yet started is dropped."""
        try:
            with self._changed:
                if net.stopping.is_set():
                    return False
                self._running += 1
            try:
                return self._download(queued)
            finally:
                with self._changed:
                    self._running -= 1
        finally:
            self._ended()

    def _download(self, queued: _Queued) -> bool:
        """Download one file on a pool thread, then postprocess it and write its history row; False when refused."""
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
            return False
        except KeyboardInterrupt:
            if net.stopping.is_set() and not net.stopping_now.is_set():
                # A rate-limit wait the first Ctrl-C cut short: the file is left for the next run.
                return False
            raise
        if saved is None:
            return False
        kind = disk.kind_of(saved.name)
        row = {"path": str(saved), "size": saved.stat().st_size,
               "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
        fields = postprocess.Fields(day=day(placed.post.date), kind=kind, choice=choice_of(self.settings.options, kind),
                                    compressor=self.settings.compressor, last_modified=received.get("last-modified"),
                                    row=row, values=placed.values)
        if fields.choice is not None and kind is not None and KINDS[kind].one_at_a_time:
            # A video waits for the run's one encode at a time on a pool of its own, so downloads go on.
            # It counts as written now, as its download is done; its encode only moves it.
            with self._writing:
                self._written[queued.key] = saved
            self._encoder().submit(self._postprocess, queued.key, saved, fields)
        else:
            self._postprocess(queued.key, saved, fields)
        return True

    def _fetch(self, queued: _Queued, file: File, received: dict[str, str]) -> Path | None:
        """`net.download` of one file onto its place; None when its link expired and a lazy post can refresh it.

        Raises:
            FetchError: the file was refused, and refreshing can't help (not a 403 or 410, or a
                post whose files were listed, not a function).
        """
        placed = queued.placed
        headers = file.headers if file.headers is not None else placed.post.headers
        source = self._sync_fetch(file.url) if callable(file.url) else file.url
        if source is None:
            # Only a text file has no URL, and it is written by `queue`, never downloaded.
            return None
        try:
            return net.download(self.run.http, source, queued.dest, label=placed.path, backoff=self.run.backoff,
                                headers=headers, wait=type(self.source).wait, size=file.size, kind=placed.kind,
                                audio=file.audio, received=received)
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

    def _postprocess(self, key: str, path: Path, fields: postprocess.Fields) -> None:
        """Run every postprocessor in order, then write the file's history row: last, so a row means a finished file."""
        for step in postprocess.ORDER:
            path = step(path, fields)
        with self._writing:
            self._written[key] = path
        self.settings.history[key] = fields.row

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
    site = TargetSite(run, target, row=row)
    job = Job(run, site, target)
    complete: list[str] = []
    listed = _listed(run.settings.home, target) if sync else {}
    listing = posts(run.source, site, target, run.settings.options, run_async=run.run_async, ended=complete.append,
                    saved=lambda: job.caught_up, listed=listed)
    error: BaseException | None = None
    try:
        try:
            for post in listing:
                # The first Ctrl-C stops listing here (D8); `queue` sees it too, between files.
                if net.stopping.is_set() or not job.queue(post):
                    break
        except KeyboardInterrupt:
            # The first Ctrl-C ends a rate-limit or pacing wait at once, raising this; that is
            # the graceful stop, not a failure. A second one is.
            if not net.stopping.is_set() or net.stopping_now.is_set():
                raise
        finally:
            # Closes the category being read, so its `finally` blocks run, and it isn't complete.
            listing.close()
            site.close()
        job.wait(row)
    except BaseException as caught:  # a route or a category failed, or a second Ctrl-C: handled below
        error = caught
    written = job.finish(error)
    if error is not None:
        if not isinstance(error, Exception):
            raise error
        log.error("{target}: {error}", target=str(target), error=error)
        row.fail()
        return Done(written, job.skipped, str(error), tuple(complete))
    if net.stopping.is_set():
        row.caption = "stopped"
        row.fail()
        return Done(written, job.skipped, STOPPED, tuple(complete))
    row.finish()
    return Done(written, job.skipped, None, tuple(complete))


@dataclass
class _Queued:
    """One queued download. It prints as its path, so a failed download names its file."""

    placed: Placed
    dest: Path
    key: str
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


def _kept(yielded: Any, category: str, *, newest_first: bool, time: Range, run_async: RunAsync,
          saved: Callable[[], bool | None] | None) -> Generator[Post, None, bool]:
    """The posts a route or category yielded, its `category` set, that `--time` keeps; see the module.

    Its return value, read with `yield from`, is True when the method's posts ran out by
    themselves, and False after a `newest_first` early stop.

    Args:
        yielded: what the method returned: a generator, an async generator, a coroutine giving
            an iterable (a route's own posts), or any iterable of posts.
        category: the category's name, "" for a route's own posts.
        newest_first: whether 5 posts in a row older than `time.since` end it.
        time: `--time`.
        run_async: runs one step of an async generator, or a coroutine, on the source's loop.
        saved: asked after each post the caller has handled whether it was all saved already; 5 in
            a row end the method (`sync`'s saved-run stop). It answers None for a post with no file
            to save, which neither counts nor breaks the run. None never stops at saved posts.
    """
    if inspect.iscoroutine(yielded):
        yielded = run_async(yielded)
    stream = _steps(yielded, run_async)
    older = already = 0
    try:
        for post in stream:
            if not isinstance(post, Post):
                raise TypeError(f"{category or 'a route'} yielded {type(post).__name__}, not a Post")
            if category:
                post = post.replace()
                object.__setattr__(post, "category", category)
            posted = day(post.date)
            if posted in time:
                older = 0
                yield post
                if saved is not None:
                    caught_up = saved()
                    if caught_up is not None:
                        already = already + 1 if caught_up else 0
                    if already >= SAVED_IN_A_ROW:
                        log.info("{category}: {count} saved posts in a row, caught up", category=category,
                                 count=already)
                        return False
                continue
            if newest_first and time.since is not None and posted is not None and posted < time.since:
                older += 1
                if older >= OLDER_IN_A_ROW:
                    return False
        return True
    finally:
        # Closed however this ends (an early stop, an error, the job stopping): its `finally` blocks run.
        stream.close()


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
    own = getattr(category, name) if category is not None else None
    return own if own is not None else getattr(source, name)


# Reads a format's fields for `_fields`.
_FORMATTER = string.Formatter()
