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
- **Saved.** A file already on disk, or whose history row points at a file, is skipped, and so
  is one already queued. A lazy post (`files` a function) whose first key is saved is skipped
  without calling it, which saves its request; a post with its files listed checks each file.
- **Async.** An `async def` route or category (tlg) runs on the source's own loop one step at a
  time, from the job's thread, so the loop is free while the job waits for room in the download
  pool. There is no handover thread.
- **Errors.** A failed file is logged and the target goes on. A route or category that raises
  fails the target, after the files already queued finish.

Phase 6, step 8 adds two things here: refreshing an expired link (a 403 or 410 from a signed CDN
URL calls a lazy post's `files` once more and takes the file with the same `num`, in `_download`),
and a graceful first Ctrl-C (stop listing: close every category generator, and let running
downloads finish, in `download` and `Job.finish`).

    saved = job.download(run, Target("loi", "post", "5930"), row)
"""

from __future__ import annotations

import datetime
import inspect
import string
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Generator, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

from flnd import post as postprocess
from flnd.common import files as disk
from flnd.common import net
from flnd.common.compress import KINDS, choice_of
from flnd.site import SourceRun, TargetSite
from flnd.source import Category, File, Post, Range, Route, Site, Source, Target, day, fill
from shared import atomic, fmt, log

if TYPE_CHECKING:
    from shared import worker

    from flnd.session import Options

__all__ = ["Job", "Placed", "download", "place", "posts", "wanted"]

# Posts in a row older than `--time`'s since that end a `newest_first` category (A9 rule 2): more
# than any site pins (Instagram and TikTok up to 3, X 1), so 5 can only mean the feed has passed it.
OLDER_IN_A_ROW = 5
# What the key format may use that is known only once a post's files are listed; a key that uses
# one can't tell from a lazy post alone whether it is saved.
LISTED_FIELDS = frozenset({"count", "index", "extension", "path"})
# The extension of a video that comes with a separate audio stream (DASH) when its URL says none.
DASH_EXTENSION = ".mp4"

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

    Args:
        source: the target's source.
        site: the handle the route and categories act through.
        target: the target, whose `kind` names its route.
        options: the run's options: `--from` and `--time`.
        run_async: runs a coroutine to its end, for an `async def` route or category.

    Raises:
        Whatever the route or a category raises, such as `FetchError` or `LoginError`.

    Closing this iterator closes the category being read, so its `finally` blocks run.
    """
    route = next(route for route in source.routes if route.kind == target.kind)
    time = Range(options.since, options.until)
    function = getattr(source, route.kind)
    if not route.categories:
        yield from _kept(function(site, target.value), "", newest_first=False, time=time, run_async=run_async)
        return
    categories = wanted(type(source), route, options)
    if not categories:
        return
    context = function(site, target.value)
    if inspect.iscoroutine(context):
        context = run_async(context)
    for category in categories:
        yielded = getattr(source, category.attribute)(site, context)
        yield from _kept(yielded, category.name, newest_first=category.newest_first, time=time, run_async=run_async)


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
        self._categories = {category.name: category for route in self.source.routes for category in route.categories}
        self._pool: worker.Pool | None = None
        self._encodes: worker.Pool | None = None
        # Every path and history key queued so far, so one file is never written twice.
        self._queued: set[Path | str] = set()

    def queue(self, post: Post) -> None:
        """Queue every file of one post that isn't saved or queued yet, listing a lazy post's files only if needed.

        Args:
            post: a post `posts` kept, its category set.

        Raises:
            Whatever a lazy post's `files` raises, such as `FetchError`.
        """
        category = self._categories.get(post.category)
        if post.lazy and self._saved_first(post, category):
            self.skipped += 1
            log.info("skip {post}", post=post.id)
            return
        listed = list(post.files()) if callable(post.files) else list(post.files)
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
                log.info("skip {name}", name=placed.path)
                continue
            self._queued.update((dest, key))
            self._downloads().submit(self._download, _Queued(placed, dest, key))

    def finish(self, error: BaseException | None = None) -> int:
        """Wait for the downloads and their encodes (or, after `error`, drop the queued ones), and count the files written.

        Args:
            error: what ended the listing, if anything; queued downloads are then dropped, and
                running ones finish.

        Returns:
            How many files were written, after logging each failed one.
        """
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
        if self._pool is None:
            return 0
        return sum(1 for result in self._pool.results if result.value is True)

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

    def _download(self, queued: _Queued) -> bool:
        """Download one file on a pool thread, then postprocess it and write its history row; False when refused."""
        placed, file = queued.placed, queued.placed.file
        headers = file.headers if file.headers is not None else placed.post.headers
        source = file.url
        if callable(source):
            source = self._sync_fetch(source)
        if source is None:
            return False
        received: dict[str, str] = {}
        # step 8: a 403 or 410 from a signed CDN URL of a lazy post calls its `files` once more here.
        saved = net.download(self.run.http, source, queued.dest, label=placed.path, backoff=self.run.backoff,
                             headers=headers, wait=type(self.source).wait, size=file.size, kind=placed.kind,
                             audio=file.audio, received=received)
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
            self._encoder().submit(self._postprocess, queued.key, saved, fields)
        else:
            self._postprocess(queued.key, saved, fields)
        return True

    def _postprocess(self, key: str, path: Path, fields: postprocess.Fields) -> None:
        """Run every postprocessor in order, then write the file's history row: last, so a row means a finished file."""
        for step in postprocess.ORDER:
            path = step(path, fields)
        self.settings.history[key] = fields.row

    def _sync_fetch(self, fetch: Callable[..., Any]) -> Callable[..., Any]:
        """A `Fetch` function whose coroutine, if it returns one, runs on the source's loop."""
        def call(dest: Path, progress: Callable[..., None]) -> Any:
            outcome = fetch(dest, progress)
            return self.run.run_async(outcome) if inspect.iscoroutine(outcome) else outcome

        return call


def download(run: SourceRun, target: Target, row: Any) -> int:
    """Download one target of a ported source, ending its row at ok or error, and return how many files were written.

        written = download(run, Target("loi", "post", "5930"), row)

    Args:
        run: the source's resources for the run.
        target: the target, as `Engine.resolve` gives it.
        row: its progress row, which ends at ok, or at error when the route or a category raised.

    Raises:
        KeyboardInterrupt and other `BaseException`s, after the queued downloads are dropped and
        the running ones finish. An `Exception` is logged and fails the row instead.
    """
    site = TargetSite(run, target, row=row)
    job = Job(run, site, target)
    listing = posts(run.source, site, target, run.settings.options, run_async=run.run_async)
    try:
        # step 8: a first Ctrl-C closes `listing` here and lets the running downloads finish.
        for post in listing:
            job.queue(post)
    except BaseException as error:
        listing.close()
        site.close()
        written = job.finish(error)
        if not isinstance(error, Exception):
            raise
        log.error("{target}: {error}", target=str(target), error=error)
        row.fail()
        return written
    site.close()
    written = job.finish()
    row.finish()
    return written


@dataclass(frozen=True)
class _Queued:
    """One queued download. It prints as its path, so a failed download names its file."""

    placed: Placed
    dest: Path
    key: str

    def __str__(self) -> str:
        return self.placed.path


def _kept(yielded: Any, category: str, *, newest_first: bool, time: Range, run_async: RunAsync) -> Iterator[Post]:
    """The posts a route or category yielded, its `category` set, that `--time` keeps; see the module.

    Args:
        yielded: what the method returned: a generator, an async generator, a coroutine giving
            an iterable (a route's own posts), or any iterable of posts.
        category: the category's name, "" for a route's own posts.
        newest_first: whether 5 posts in a row older than `time.since` end it.
        time: `--time`.
        run_async: runs one step of an async generator, or a coroutine, on the source's loop.
    """
    if inspect.iscoroutine(yielded):
        yielded = run_async(yielded)
    stream = _steps(yielded, run_async)
    older = 0
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
                continue
            if newest_first and time.since is not None and posted is not None and posted < time.since:
                older += 1
                if older >= OLDER_IN_A_ROW:
                    return
    finally:
        # Closed however this ends (an early stop, an error, the job stopping): its `finally` blocks run.
        stream.close()


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
