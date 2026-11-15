"""The job: one target of a source, from its route to its history rows."""

from __future__ import annotations

import datetime
import email.utils
import filecmp
import inspect
import os
import re
import string
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import (
    TYPE_CHECKING, Any, Callable, Collection, Coroutine, Generator, Iterator, Mapping, Sequence,
)
from urllib.parse import urlsplit

import msgspec

from flnd import targets
from flnd.common import net, paths
from flnd.common.compress import KINDS, ONE_AT_A_TIME
from flnd.options import Options
from flnd.site import SourceRun, TargetSite
from flnd.source import (
    Category, FetchError, File, LoginError, Post, Range, Resume, Route, Site, Source, Target,
    day, fill, listing,
)
from shared import fs, text, ui, worker

if TYPE_CHECKING:
    from shared import cli
    from shared._ui import Group, Row

__all__ = [
    "STOPPED", "Board", "Counts", "Downloads", "Ledger", "Placed", "TargetReport",
    "causes", "discover", "download", "place", "plan", "posts", "wanted",
]

ID, DATE, ESTIMATED, CATEGORY = "id", "date", "estimated", "category"
BLOCKS = frozenset({401, 403, 429})
LEADING_DAY = re.compile(r"\d{4}-\d\d-\d\d_")
NOON = 12
FILES = "files"
OLDER_IN_A_ROW = 5
SAVED_IN_A_ROW = 5
LISTED_FIELDS = frozenset({"count", "index", "extension", "path"})
DASH_EXTENSION = ".mp4"
EXPIRED = frozenset({403, 410})
STOPPED = "stopped by Ctrl-C"
FINISHING = "stopping: {files} finishing (Ctrl-C again to stop now)"
WAKE = 0.1
LOOKUP_TTL = 24 * 3600
LOOKUP_KEY = "query.lookups"
ASKED_TTL = 30 * 24 * 3600
ASKED_KEY = "redate.asked"
ALL = "all"
_FORMATTER = string.Formatter()

RunAsync = Callable[[Coroutine[Any, Any, Any]], Any]


class _HistoryRow(msgspec.Struct, omit_defaults=True):
    """Serialized metadata for one downloaded file in `history.db`."""

    path: str
    size: int
    at: str
    id: str
    category: str = ""
    date: str | None = None
    estimated: bool = False
    files: int | None = None
    mtime: int | None = None


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
    """Summary of one command's execution for a single target."""

    target: Target
    written: tuple[Path, ...]
    skipped: int
    repaired: int = 0
    missing: int = 0
    settings: str = ""
    error: str | None = None


@dataclass
class Counts:
    """Counters for one category's files and saved bytes."""

    found: int = 0
    queued: int = 0
    downloading: int = 0
    shrinking: int = 0
    saved: int = 0
    linked: int = 0
    later: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0

    @property
    def pending(self) -> int:
        """Return files and links not yet finished."""
        return self.queued + self.downloading + self.shrinking + self.later


@dataclass
class _Line:
    """One category status line tracked by `Board`."""

    target: Target
    label: str
    counts: Counts
    row: Row | None = None
    listed: bool = False
    ended: bool = False
    note: str = ""


class Board:
    """Status board drawing per-target, per-category progress in a `ui.Group`."""

    def __init__(
        self, rows: Group, *, subject_width: int = 0, label_width: int = 0,
        parent: Target | None = None,
    ) -> None:
        self.rows, self.widths, self.parent = rows, (subject_width, label_width), parent
        self._lock = threading.Lock()
        self._lines: dict[tuple[Target, str], _Line] = {}
        self._child_skipped: dict[Target, int] = {}

    def start(self, target: Target, category: str, *, slots: int = 0) -> None:
        """Open the category's line as its listing starts, unless already open."""
        owner = self.parent or target
        with self._lock:
            line = self._lines.get((owner, category))
            if line is None or line.ended:
                self._open(owner, category)

    def count(self, target: Target, category: str, **steps: int) -> None:
        """Add `steps` to the category's counts and refresh its row."""
        owner = self.parent or target
        with self._lock:
            line = self._lines.get((owner, category)) or self._open(owner, category)
            for name, step in steps.items():
                setattr(line.counts, name, getattr(line.counts, name) + step)
            if self.parent is not None and target != self.parent and "skipped" in steps:
                self._child_skipped[target] = self._child_skipped.get(target, 0) + steps["skipped"]
        self._show(line)

    def listed(self, target: Target, category: str) -> None:
        """Mark the category's listing complete so its row finishes once nothing is pending."""
        if self.parent is not None and target != self.parent:
            return
        with self._lock:
            line = self._lines.get((target, category)) or self._open(target, category)
            line.listed = True
        self._show(line)

    def note(self, target: Target, note_text: str) -> None:
        """Attach a status note to each open line for `target`."""
        for line in self._of(target):
            if not line.ended:
                line.note = note_text
                self._show(line)

    def end(self, target: Target, *, failed: bool) -> None:
        """Finalize every open line for `target` as ok or failed."""
        if self.parent is not None and target != self.parent:
            return
        for line in self._of(target):
            if line.ended or line.row is None:
                continue
            line.ended = True
            line.row.fail() if failed else line.row.finish()

    def row(self, target: Target, category: str) -> Row | None:
        """Return the `ui.Row` for `(target, category)`, or None before it opens."""
        owner = self.parent or target
        with self._lock:
            line = self._lines.get((owner, category))
        return None if line is None else line.row

    def totals(self, target: Target) -> Counts:
        """Return summed counts across every category for `target`."""
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
        """Update the row label/count and finish it when listed and idle."""
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
                line.row.finish()

    def _label(self, line: _Line) -> str:
        """Format one status line's compact summary label."""
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
            parts.append(text.fmt.size(c.bytes))
        if line.note:
            parts.append(line.note)
        return " | ".join(parts)


def wanted(source: type[Source], route: Route, options: Options) -> tuple[Category, ...]:
    """Return the categories of `route` selected by `--from` in declaration order."""
    names = options.categories
    if names is None or ALL in names:
        return route.categories
    selected = set(names)
    return tuple(category for category in route.categories if category.name in selected)


def posts(
    source: Source, site: Site, target: Target, options: Options, *, run_async: RunAsync
) -> Generator[Post, None, None]:
    """Yield every post from `target`'s route and wanted categories kept by `--time`."""
    span = Range(options.since, options.until)
    route = next(r for r in source.routes if r.kind == target.kind)
    categories = wanted(type(source), route, options)
    if route.categories and not categories:
        return
    context = _context(source, route, site, target, run_async)
    if not route.categories:
        yield from _Kept(context, "", newest_first=False, span=span, run_async=run_async)
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
    """Call `target`'s route method and await it when async."""
    context = getattr(source, route.kind)(site, target.value)
    if route.categories and inspect.iscoroutine(context):
        context = run_async(context)
    return context


def plan(
    run: SourceRun, target: Target, options: Options, *, row: Any = None
) -> Generator[Placed, None, None]:
    """Yield every `Placed` file a download of `target` would save without writing."""
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
    """Resolve destination path and history key for each file of `post`."""
    folder = _folder_format(source, route, category)
    filename, key = (_format(source, category, name) for name in ("filename", "key"))
    posted = day(post.date)
    placed: list[Placed] = []
    for num, file in enumerate(files):
        extension, kind = _extension(file)
        values = _file_values(
            source, post, file, num, len(files), posted, extension, target, options,
        )
        name = _resolve_name(post, file, filename, values, extension)
        path = "/".join(part for part in (fill(folder, values), name) if part)
        resolved_key = fill(key, {**values, "path": path}, safe=False) or path
        placed.append(Placed(post, file, num, path, resolved_key, kind, values))
    return placed


def _file_values(
    source: Source, post: Post, file: File, num: int, count: int, posted: datetime.date | None,
    extension: str, target: Target | None, options: Options | None,
) -> dict[str, Any]:
    """Build template formatting fields for one file of a post."""
    code = target.source if target is not None else source.code
    target_val = target.value if target is not None else ""
    query_val = (options.query or "") if options is not None else ""
    return {
        **post.fields, "id": post.id, "date": posted, "category": post.category,
        "num": num, "count": count, "index": "" if count == 1 else f"_{num}",
        "extension": extension, "source": code, "site": code,
        "target": target_val, "user": target_val, "query": query_val,
    }


def _resolve_name(
    post: Post, file: File, filename: str, values: Mapping[str, Any], extension: str
) -> str:
    """Compute a safe filename for one file of `post`."""
    formatted = fill(filename, values) or text.clean.filename(post.id)
    if file.name is None:
        return formatted + extension
    if file.content is None and PurePosixPath(file.name).stem in _stems(filename, values):
        return formatted + PurePosixPath(file.name).suffix
    return text.clean.filename(file.name)


def _stems(filename: str, values: Mapping[str, Any]) -> set[str]:
    """Return filename stems with and without the post's date."""
    return {fill(filename, values), fill(filename, {**values, "date": None})} - {""}


class Ledger:
    """Tracks queued files, skips, links, and undated posts for one target."""

    def __init__(
        self, run: SourceRun, site: TargetSite, target: Target, board: Board, *,
        options: Options | None = None,
    ) -> None:
        self.source, self.settings, self.site = run.source, run.settings, site
        self.target, self.board, self._options = target, board, options
        self.folder = paths.target_folder(self.settings.home, target)
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

    def take(self, post: Post, *, hold: bool = True) -> tuple[Outcome, list[_Queued]]:
        """Queue unsaved files of `post`, skipping already-saved or held posts."""
        category = self._categories.get(post.category)
        first = self._first_key(post, category)
        dated = self._dated(post, category, hold=hold)
        if dated is None:
            return Outcome.EMPTY, []
        post = dated
        skipped = self._saved_first(post, category) if post.lazy else None
        if skipped is not None:
            self._skip(post.category)
            ui.debug("{post}: already saved", post=post.id)
            return skipped, []
        listed = list(post.files()) if callable(post.files) else list(post.files)
        return self._queue_post_files(post, listed, category, first)

    def _queue_post_files(
        self, post: Post, listed: Sequence[File], category: Category | None, first: str | None
    ) -> tuple[Outcome, list[_Queued]]:
        """Place and queue each file of `post`, writing inline text files immediately."""
        found = 0
        todo: list[_Queued] = []
        for placed in place(
            self.source, post, listed, category=category, route=self._route,
            target=self.target, options=self._options,
        ):
            dest = self.folder.joinpath(*placed.path.split("/"))
            if placed.file.content is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                fs.write(dest, placed.file.content.encode("utf-8"))
                continue
            item_key = f"{self.source.code}:{placed.key}"
            held = self._held(dest, item_key)
            if held is not None:
                if dest not in self._queued and item_key not in self._queued:
                    found += 1
                self._skip_held(post.category, placed.path, dest, item_key, held)
                continue
            self.board.count(self.target, post.category, found=1)
            self._queued.update(dict.fromkeys((dest, item_key), placed.path))
            todo.append(_Queued(placed, dest, item_key, estimated=first in self._estimated))
        if todo:
            todo.sort(key=lambda queued: queued.placed.kind != paths.VIDEO)
            return Outcome.QUEUED, todo
        return (Outcome.SAVED if found else Outcome.EMPTY), []

    def _skip_held(self, category: str, name: str, dest: Path, item_key: str, held: str) -> None:
        """Count a skipped file and link it when held in another category's folder."""
        self._skip(category)
        if held == name:
            ui.debug("skip {name}", name=name)
            return
        ui.debug("skip {name} -> {held}", name=name, held=held)
        if item_key in self._queued:
            self._later.append((dest, item_key, category))
            self.board.count(self.target, category, later=1)
        else:
            candidate = self.folder.joinpath(*held.split("/"))
            self._link(dest, candidate if candidate.is_file() else Path(held), category)

    def link_queued(self, written: Mapping[str, Path]) -> None:
        """Create deferred links for files queued earlier in this run."""
        for dest, item_key, category in self._later:
            holder = written.get(item_key)
            if holder is not None:
                self._link(dest.with_suffix(holder.suffix), holder, category)
            self.board.count(self.target, category, later=-1)
        self._later = []

    def _link(self, dest: Path, holder: Path, category: str) -> None:
        """Link `holder` at `dest` via relative symlink or hard link across folders."""
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
        self.board.count(self.target, category, linked=1)
        ui.debug("link {name} -> {held}", name=self._name(dest), held=self._name(holder))

    def release(self) -> list[Post]:
        """Release held undated posts with any discovered or nearest-neighbor date."""
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
        """Date undated or estimated saved files of this target using the site's `date` hook."""
        if not self._asking:
            return 0
        dated = 0
        for item_key, row in self._target_undated_rows():
            post_id = row[ID]
            if post_id in self._asked:
                continue
            found = self._ask(Post(post_id))
            if found is None:
                continue
            cat = self._categories.get(str(row.get(CATEGORY, "")))
            filename_tpl = _format(self.source, cat, "filename")
            path = _redated(
                Path(row["path"]), found, filename_tpl, estimated=bool(row.get(ESTIMATED)),
            )
            if path is None:
                continue
            _set_mtime(path, found)
            fresh = {k: v for k, v in row.items() if k != ESTIMATED}
            self.settings.history[item_key] = {
                **fresh, "path": str(path), DATE: found.isoformat(), **paths.stamp(path),
            }
            dated += 1
        self._save_asked()
        if dated:
            self.site.info("{files} dated", files=text.fmt.count(dated, "saved file"))
        return dated

    def _target_undated_rows(self) -> list[tuple[str, dict[str, Any]]]:
        """Return undated or estimated history rows belonging to this target's folder."""
        history = self.settings.history
        prefix = f"{self.target.source}:"
        folder_str = str(self.folder)
        raw = getattr(history, "raw", None)
        if raw is not None:
            sql = "SELECT key, value FROM kv WHERE key LIKE ? AND value LIKE ?"
            cursor = raw.execute(sql, (f"{prefix}%", f"%{folder_str}%"))
            rows = [(k, msgspec.json.decode(v)) for k, v in cursor.fetchall()]
            return [(k, r) for k, r in rows if self._undated(r)]
        return [(k, r) for k, r in list(history.items())
                if k.startswith(prefix) and self._undated(r)]

    def _undated(self, row: Any) -> bool:
        """Return whether `row` is an undated or estimated file in this target's folder."""
        if not isinstance(row, dict) or not isinstance(row.get(ID), str):
            return False
        in_folder = Path(row.get("path", "")).is_relative_to(self.folder)
        return in_folder and (DATE not in row or bool(row.get(ESTIMATED)))

    def _load_asked(self) -> dict[str, float]:
        """Load non-expired 30-day `asked` cache entries from `site.state`."""
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
        """Query the site's `date` hook for `post`, caching misses for 30 days."""
        if not self._asking:
            return None
        try:
            found = day(self.source.date(self.site, post))
        except (FetchError, LoginError) as error:
            if isinstance(error, LoginError) or error.status in BLOCKS:
                self._asking = False
                self.site.warn("dating stopped: {error}; the rest are estimated", error=error)
            else:
                ui.debug("{post}: not dated: {error}", post=post.id, error=error)
            return None
        if found is None:
            self._asked[post.id] = time.time()
        return found

    def _nearest_dates(self) -> dict[str, datetime.date]:
        """Compute nearest dated neighbor in listing order for each undated key."""
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
        """Resolve `post`'s date from listing memory or hold it until category end."""
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
        found = self._dates.get(item_key) or _row_day(self.settings.history.get(item_key))
        if found is not None:
            self._dates[item_key] = found
            return post.replace(date=found)
        if not hold:
            return post
        if item_key in self._waiting:
            self._skip(post.category)
            ui.debug("{post}: listed twice", post=post.id)
            return None
        self._waiting[item_key] = post
        return None

    def _first_key(self, post: Post, category: Category | None) -> str | None:
        """Return the history key of `post`'s first file from its fields, or None."""
        template = _format(self.source, category, "key")
        if not template or _fields(template) & (LISTED_FIELDS | {"date"}):
            return None
        values = {**post.fields, "id": post.id, "category": post.category, "num": 0}
        built = fill(template, values, safe=False)
        return f"{self.source.code}:{built}" if built else None

    def is_saved(
        self, post_id: str, category: Category | None, posted: datetime.date | None = None
    ) -> bool:
        """Return whether `post_id` is already saved in history or on disk."""
        name = category.name if category is not None else ""
        if self._saved_first(Post(post_id, date=posted, category=name), category) is Outcome.SAVED:
            return True
        template = _format(self.source, category, "key")
        if not template or _fields(template) & LISTED_FIELDS:
            return False
        if not (_fields(template) - {"id", "date", "category", "num"}):
            return False
        values = {"id": post_id, "date": day(posted), "category": name}
        for index in (0, 1):
            filled = fill(template, {**values, "num": index, "n": index, "idx": index}, safe=False)
            for candidate in dict.fromkeys((filled, f"{post_id}_{index}")):
                item_key = f"{self.source.code}:{candidate}"
                if item_key not in self._queued and self._held(None, item_key) is not None:
                    return True
        return False

    def _saved_first(self, post: Post, category: Category | None) -> Outcome | None:
        """Check whether all files of a lazy post are already saved by their keys."""
        template = _format(self.source, category, "key")
        if not template or _fields(template) & LISTED_FIELDS:
            return None
        values = {**post.fields, "id": post.id, "date": day(post.date), "category": post.category}
        keys = [
            f"{self.source.code}:{fill(template, {**values, 'num': num}, safe=False)}"
            for num in range(2)
        ]
        if keys[0] == f"{self.source.code}:" or self._held(None, keys[0]) is None:
            return None
        row = self.settings.history.get(keys[0])
        count = row.get(FILES) if isinstance(row, dict) else None
        checked = [keys[0]]
        if isinstance(count, int) and count > 1 and keys[1] != keys[0]:
            checked += [
                f"{self.source.code}:{fill(template, {**values, 'num': num}, safe=False)}"
                for num in range(1, count)
            ]
            if any(self._held(None, k) is None for k in checked[1:]):
                return None
        return Outcome.EMPTY if any(k in self._queued for k in checked) else Outcome.SAVED

    def _skip(self, category: str) -> None:
        """Count one file found and skipped on `category`'s status line."""
        self.board.count(self.target, category, found=1, skipped=1)

    def _held(self, dest: Path | None, item_key: str) -> str | None:
        """Return the name holding a file if queued, on disk, or in history, else None."""
        for known in (dest, item_key):
            if known is not None and known in self._queued:
                return self._queued[known]
        if dest is not None and dest.is_file() and dest.stat().st_size > 0:
            return self._name(dest)
        row = self.settings.history.get(item_key)
        found = Path(row.get("path", "")) if isinstance(row, dict) and row.get("path") else None
        return self._name(found) if found is not None and found.is_file() else None

    def _name(self, path: Path) -> str:
        """Return `path` relative to `self.folder` when inside it, else as a full string."""
        return (
            path.relative_to(self.folder).as_posix()
            if path.is_relative_to(self.folder)
            else str(path)
        )


def _distinct_folders(root: Path, dest: Path, holder: Path) -> bool:
    """Return whether `dest` and `holder` live in different top-level category folders."""
    if not (dest.is_relative_to(root) and holder.is_relative_to(root)):
        return False
    here = dest.relative_to(root).parts
    there = holder.relative_to(root).parts
    return len(here) >= 2 and len(there) >= 2 and here[0] != there[0]


def _try_link(dest: Path, holder: Path) -> bool:
    """Create a relative symlink or hard link from `dest` to `holder`, returning success."""
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
    """Concurrent download and video-encode runner for one target."""

    def __init__(self, run: SourceRun, target: Target, board: Board, *, shrink: bool) -> None:
        self.run, self.target, self.board, self.shrink = run, target, board, shrink
        self.source, self.settings = run.source, run.settings
        self._pool: worker.Pool | None = None
        self._encodes: worker.Pool | None = None
        self._written: dict[str, Path] = {}
        self._writing = threading.Lock()
        self._changed = threading.Condition()
        self._pending = 0
        self._running = 0
        self._refreshes: deque[_Refresh] = deque()
        self._answering = True
        self._lister = threading.current_thread()

    @property
    def written(self) -> dict[str, Path]:
        """Return each file written so far by its history key."""
        with self._writing:
            return dict(self._written)

    def submit(self, queued: _Queued) -> bool:
        """Submit one file for download once pool capacity allows; False on stop."""
        if not self._room():
            return False
        self._count(queued, queued=1)
        try:
            self._downloads().submit(self._task, queued)
        except BaseException:
            self._count(queued, queued=-1)
            self._ended()
            raise
        return True

    def wait(self) -> None:
        """Wait until all submitted downloads finish while answering link refreshes."""
        shown: str | None = None
        while True:
            self._answer()
            with self._changed:
                if not self._pending:
                    return
                if self.settings.stop.now.is_set():
                    raise KeyboardInterrupt
                note_text = (
                    FINISHING.format(files=text.fmt.count(self._running, "file"))
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
        """Wait for downloads and encodes (or cancel refreshes on error) and return paths."""
        if error is None:
            self.wait()
        else:
            self._cancel_refreshes()
        for name in ("_pool", "_encodes"):
            pool: worker.Pool | None = getattr(self, name)
            if pool is not None:
                self._drain_pool(pool, error)
        with self._writing:
            return tuple(self._written.values())

    def _cancel_refreshes(self) -> None:
        """Stop answering refresh requests so waiting downloads fail fast."""
        with self._changed:
            self._answering = False
            waiting, self._refreshes = list(self._refreshes), deque()
        for refresh in waiting:
            refresh.done.set()

    def _drain_pool(self, pool: worker.Pool, error: BaseException | None) -> None:
        """Close `pool` and log any task failures."""
        if error is not None:
            pool.stop(error)
        results = pool.join()
        pool.close()
        for result in results:
            if not result.ok:
                item = result.item[0] if isinstance(result.item, tuple) else result.item
                ui.error("{file}: {kind} {error}", file=item,
                         kind=type(result.error).__name__, error=result.error)
                if result.error is not None:
                    ui.debug("{file}: {chain}", file=item, chain=causes(result.error))

    def _downloads(self) -> worker.Pool:
        """Lazily create the target's download `worker.Pool`."""
        if self._pool is None:
            self._pool = worker.Pool(
                concurrency=self.source.concurrency, stop=self.settings.stop.soon,
            )
        return self._pool

    def _encoder(self) -> worker.Pool:
        """Lazily create the single-thread video encode `worker.Pool`."""
        if self._encodes is None:
            self._encodes = worker.Pool(concurrency=1)
        return self._encodes

    def _room(self) -> bool:
        """Wait for pool capacity while answering refreshes; False on stop."""
        limit = 2 * max(self.source.concurrency, 1)
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

    def _task(self, queued: _Queued) -> bool:
        """Execute one queued download on a worker thread."""
        try:
            with self._changed:
                started = not self.settings.stop.soon.is_set()
                self._running += started
            if not started:
                self._count(queued, queued=-1)
                return False
            self._count(queued, queued=-1, downloading=1)
            try:
                return self._download(queued)
            finally:
                with self._changed:
                    self._running -= 1
        finally:
            self._ended()

    def _download(self, queued: _Queued) -> bool:
        """Download one file, refreshing an expired lazy link once if needed."""
        placed = queued.placed
        received: dict[str, str] = {}
        try:
            saved = self._fetch(queued, placed.file, received)
            if saved is None and queued.refreshed is not None:
                refreshed = self._expired(queued)
                saved = self._fetch(queued, refreshed, received) if refreshed is not None else None
        except FetchError as error:
            self._count(queued, downloading=-1, failed=1)
            ui.error("{name}: {error}", name=placed.path, error=error)
            ui.debug("{name}: {chain}", name=placed.path, chain=causes(error))
            return False
        except KeyboardInterrupt:
            self._count(queued, downloading=-1)
            if self.settings.stop.soon.is_set() and not self.settings.stop.now.is_set():
                return False
            raise
        except BaseException:
            self._count(queued, downloading=-1, failed=1)
            raise
        if saved is None:
            self._count(queued, downloading=-1)
            return False
        self._downloaded(queued, saved, received)
        return True

    def _downloaded(self, queued: _Queued, saved: Path, received: Mapping[str, str]) -> None:
        """Record a downloaded file's history row and shrink it when requested."""
        placed = queued.placed
        kind = paths.kind_of(saved.name)
        row = self._build_row(queued, saved)
        shrink = bool(self.shrink and kind in KINDS)
        dated = day(placed.post.date) or _served_day(received.get("last-modified"))
        finished = _Finished(row, kind if shrink else None, dated)
        if not shrink:
            self._finish(queued, saved, finished)
            return
        self._count(queued, downloading=-1, shrinking=1)
        if kind not in ONE_AT_A_TIME:
            self._finish(queued, saved, finished)
            return
        with self._writing:
            self._written[queued.key] = saved
        self._encoder().submit(self._finish, queued, saved, finished)

    def _build_row(self, queued: _Queued, saved: Path) -> dict[str, Any]:
        """Build the initial history dict for one downloaded file via `msgspec.Struct`."""
        placed = queued.placed
        posted = day(placed.post.date)
        count = placed.values.get("count", 1)
        record = _HistoryRow(
            path=str(saved),
            size=saved.stat().st_size,
            at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            id=placed.post.id,
            category=placed.post.category,
            date=posted.isoformat() if posted is not None else None,
            estimated=queued.estimated,
            files=count if placed.num == 0 and isinstance(count, int) and count > 1 else None,
        )
        return msgspec.structs.asdict(record)

    def _fetch(self, queued: _Queued, file: File, received: dict[str, str]) -> Path | None:
        """Download `file` or signal link expiry for a lazy post."""
        placed = queued.placed
        if file.url is None:
            return None
        if callable(file.url):
            source = self._sync_fetch(file.url)
            if source is None:
                return None
            file = File(source, audio=file.audio, name=file.name, size=file.size)
        try:
            return net.download(
                self.run.files, file, queued.dest, label=placed.path,
                headers=placed.post.headers, kind=placed.kind, received=received,
            )
        except FetchError as error:
            if error.status not in EXPIRED or not callable(placed.post.files) or queued.refreshed:
                raise
            ui.info("{name}: HTTP {status}, asking for a fresh link",
                    name=placed.path, status=error.status)
            queued.refreshed = error
            return None

    def _expired(self, queued: _Queued) -> File | None:
        """Re-fetch a lazy post's files on the lister thread for an expired link."""
        refusal = queued.refreshed
        assert refusal is not None
        refresh = _Refresh(queued.placed.post, self.settings.stop)
        if threading.current_thread() is self._lister:
            refresh.run()
        else:
            self._dispatch_refresh(refresh)
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

    def _dispatch_refresh(self, refresh: _Refresh) -> None:
        """Queue a refresh onto the lister thread and wait for completion."""
        with self._changed:
            if self._answering:
                self._refreshes.append(refresh)
                self._changed.notify_all()
            else:
                refresh.done.set()
        while not refresh.done.wait(WAKE):
            if self.settings.stop.now.is_set():
                raise KeyboardInterrupt

    def _answer(self) -> None:
        """Process pending link refreshes on the lister thread."""
        while True:
            with self._changed:
                if not self._refreshes:
                    return
                refresh = self._refreshes.popleft()
            refresh.run()

    def _finish(self, queued: _Queued, path: Path, finished: _Finished) -> None:
        """Shrink if needed, set modified time, stamp, and write history row."""
        state = "downloading" if finished.shrink is None else "shrinking"
        try:
            if finished.shrink is not None:
                path = self._shrunk(queued, path, finished)
            if finished.day is not None:
                _set_mtime(path, finished.day)
            finished.row.update(paths.stamp(path))
            with self._writing:
                self._written[queued.key] = path
            self.settings.history[queued.key] = finished.row
        except BaseException:
            with self._writing:
                self._written.pop(queued.key, None)
            self._count(queued, **{state: -1}, failed=1)
            raise
        self._count(queued, **{state: -1}, saved=1, bytes=finished.row.get("size", 0))

    def _shrunk(self, queued: _Queued, path: Path, finished: _Finished) -> Path:
        """Compress `path` and update `finished.row` when smaller."""
        assert finished.shrink is not None
        found = self.settings.compressor.compress(path, finished.shrink)
        if found is None:
            return path
        before = finished.row.get("size", 0)
        finished.row.update(found)
        if found.get("compressed") not in (None, "none"):
            ui.debug(
                "shrink {name} {before} -> {after}",
                name=queued.placed.path,
                before=text.fmt.size(found.get("original_size", before)),
                after=text.fmt.size(found.get("size", before)),
            )
        return Path(found.get("path", path))

    def _sync_fetch(self, fetch: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a `Fetch` callable so coroutines run on the source's event loop."""
        def call(dest: Path, progress: Callable[..., None]) -> Any:
            outcome = fetch(dest, progress)
            return self.run.run_async(outcome) if inspect.iscoroutine(outcome) else outcome

        return call


def download(run: SourceRun, target: Target, board: Board, options: Options) -> TargetReport:
    """Download one target of a source, counting progress on `board`."""
    if run.settings.stop.soon.is_set():
        return TargetReport(target, (), 0, error=STOPPED)
    folder = paths.target_folder(run.settings.home, target)
    with fs.lock(folder / ".flnd.lock"):
        fs.sweep(folder)
        return _run_download(run, target, board, options)


def _run_download(
    run: SourceRun, target: Target, board: Board, options: Options
) -> TargetReport:
    """Execute the locked download lifecycle for `target`."""
    site = TargetSite(run, target, time=Range(options.since, options.until))
    ledger = Ledger(run, site, target, board, options=options)
    downloads = Downloads(run, target, board, shrink=options.shrink)
    work = _Work(ledger, downloads)
    complete: list[str] = []
    saved = run.settings.targets
    listed = {} if options.again else targets.listed(saved, target)
    resumes: dict[str, Resume] = {}
    error = _execute_listing(work, run, site, target, options, listed, resumes, complete)
    written = downloads.finish(error)
    ledger.link_queued(downloads.written)
    cursors = {
        name: None if name in complete else resume.cursor
        for name, resume in resumes.items()
    }
    if not options.query:
        targets.keep_cursors(saved, target, cursors)
        targets.complete(saved, target, complete)
    report = TargetReport(target, written, board.totals(target).skipped)
    return _ended(run, board, report, error, complete=", ".join(complete) or "none")


def _execute_listing(
    work: _Work, run: SourceRun, site: TargetSite, target: Target, options: Options,
    listed: Mapping[str, str], resumes: dict[str, Resume], complete: list[str],
) -> BaseException | None:
    """Run listing and wait for downloads, returning any fatal error."""
    try:
        try:
            if options.stop_at_saved:
                work.ledger.redate()
            _read(
                work, run, site, target, options,
                listed=listed, resumes=resumes, complete=complete,
            )
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
    complete: str,
) -> TargetReport:
    """Finalize `board` lines and return `TargetReport` with any error."""
    target = report.target
    if error is not None:
        if not isinstance(error, Exception):
            raise error
        ui.error("{target}: {error}", target=str(target), error=error)
        ui.debug("{target}: {chain}", target=str(target), chain=causes(error))
        board.end(target, failed=True)
        return replace(report, error=str(error))
    if run.settings.stop.soon.is_set():
        ui.error("{target}: stopped", target=str(target))
        board.end(target, failed=True)
        return replace(report, error=STOPPED)
    board.end(target, failed=False)
    ui.debug(
        "{target}: {written} written, {skipped} skipped; complete: {complete}",
        target=str(report.target), written=len(report.written),
        skipped=report.skipped, complete=complete,
    )
    return report


def causes(error: BaseException) -> str:
    """Format an exception and its causal chain for debug logging."""
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


@dataclass
class _Finished:
    """Metadata needed to finalize one downloaded file."""

    row: dict[str, Any]
    shrink: str | None
    day: datetime.date | None


def _redated(
    path: Path, found: datetime.date, filename: str, *, estimated: bool
) -> Path | None:
    """Rename `path` to include `found`, or return None if another file conflicts."""
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


def _row_day(row: Any) -> datetime.date | None:
    """Return the ISO date stored in a history row, or None."""
    value = row.get(DATE) if isinstance(row, dict) else None
    try:
        return datetime.date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _set_mtime(path: Path, dated: datetime.date) -> None:
    """Set `path`'s modified time to noon UTC of `dated`."""
    noon = datetime.datetime.combine(dated, datetime.time(NOON), tzinfo=datetime.timezone.utc)
    stamp = noon.timestamp()
    try:
        os.utime(path, (stamp, stamp))
    except OSError as error:
        ui.debug("{name}: modified time not set: {error}", name=path.name, error=error)


def _served_day(value: str | None) -> datetime.date | None:
    """Parse an HTTP `Last-Modified` date header into a UTC date, or None."""
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).date()


@dataclass
class _Queued:
    """One queued download item."""

    placed: Placed
    dest: Path
    key: str
    estimated: bool = False
    refreshed: FetchError | None = None

    def __str__(self) -> str:
        return self.placed.path


@dataclass(eq=False)
class _Refresh:
    """A download's request for a lazy post's files on the lister thread."""

    post: Post
    stop: cli.Stopping
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
    """Posts from a route or category filtered by `--time`."""

    def __init__(
        self, yielded: Any, category: str, *, newest_first: bool, span: Range, run_async: RunAsync
    ) -> None:
        if inspect.iscoroutine(yielded):
            yielded = run_async(yielded)
        self.category, self.newest_first, self.time = category, newest_first, span
        self.ran_out = False
        self._steps = _steps(yielded, run_async)

    def __iter__(self) -> Iterator[Post]:
        """Yield kept posts, stopping early after 5 older posts on `newest_first` feeds."""
        older = 0
        try:
            for post in self._steps:
                if not isinstance(post, Post):
                    raise TypeError(
                        f"{self.category or 'a route'} yielded {type(post).__name__}, not a Post"
                    )
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
        """Close the underlying generator."""
        self._steps.close()


class Outcome(Enum):
    """Outcome of queuing one post into `Ledger`."""

    QUEUED = "queued"
    SAVED = "saved"
    EMPTY = "empty"
    STOPPED = "stopped"


class Ended(Enum):
    """How one category listing ended."""

    RAN_OUT = "ran out"
    CUT = "cut"
    STOPPED = "stopped"


@dataclass(frozen=True)
class _Work:
    """Pair of `Ledger` and `Downloads` for queuing kept posts."""

    ledger: Ledger
    downloads: Downloads

    def queue(self, post: Post, *, hold: bool = True) -> Outcome:
        """Record `post` in `ledger` and submit its unsaved files."""
        outcome, todo = self.ledger.take(post, hold=hold)
        for queued in todo:
            if not self.downloads.submit(queued):
                return Outcome.STOPPED
        return outcome


def discover(
    run: SourceRun, target: Target, *, limit: int | None = None,
    time_range: Range = Range(), seen: Collection[str] = (),
) -> Generator[Target, None, None]:
    """Yield deduplicated child targets from a query or target-yielding route."""
    route = next(r for r in run.source.routes if r.kind == target.kind)
    site = TargetSite(run, target, time=time_range)
    seen_keys = set(seen)
    yielded: set[Target] = set()
    method = getattr(run.source, route.kind)
    try:
        args = _plan_queries(route, site, target) if route.is_query else [target.value]
        for arg in args:
            if _at_limit(run, yielded, limit):
                return
            yield from _yield_arg_targets(run, method, site, arg, seen_keys, yielded, limit)
    finally:
        site.close()


def _yield_arg_targets(
    run: SourceRun, method: Any, site: TargetSite, arg: Any, seen_keys: set[str],
    yielded: set[Target], limit: int | None,
) -> Generator[Target, None, None]:
    """Yield targets for one query argument until stopped or at `limit`."""
    steps = _steps(method(site, arg), run.run_async)
    try:
        for item in steps:
            if run.settings.stop.soon.is_set():
                return
            child = item if isinstance(item, Target) else run.source.resolve(str(item))
            if targets.key(child) in seen_keys:
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


def _plan_queries(route: Route, site: Site, target: Target) -> list[Any]:
    """Plan `target` into `Query` searches, caching lookup ids for 24 hours."""
    from flnd import query

    now = time.time()
    raw = site.state.get(LOOKUP_KEY)
    raw_dict = raw if isinstance(raw, dict) else {}
    entries = {
        k: v for k, v in raw_dict.items()
        if isinstance(v, dict) and isinstance(v.get("id"), str)
        and isinstance(v.get("at"), (int, float)) and now - v["at"] < LOOKUP_TTL
    }
    cache = {k: v["id"] for k, v in entries.items()}
    before = dict(cache)
    try:
        return query.plan(target.value, route.params, site=site, cache=cache, time=site.time)
    finally:
        if cache != before or len(entries) != len(raw_dict):
            for k, v in cache.items():
                if before.get(k) != v:
                    entries[k] = {"id": v, "at": now}
            site.state[LOOKUP_KEY] = entries


def _queue_all(
    work: _Work, kept: _Kept, *, stop_at_saved: bool, limit: int | None = None
) -> tuple[Ended, int]:
    """Queue kept posts, then release held undated posts."""
    ended, taken = _queue_listed(work, kept, stop_at_saved=stop_at_saved, limit=limit)
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
    work: _Work, kept: _Kept, *, stop_at_saved: bool, limit: int | None = None
) -> tuple[Ended, int]:
    """Queue each post in `kept` until stopped, `limit`, or 5 saved posts in a row."""
    saved_in_a_row = 0
    taken = 0
    stop = work.downloads.settings.stop.soon
    try:
        for post in kept:
            if limit is not None and taken >= limit:
                return Ended.CUT, taken
            outcome = Outcome.STOPPED if stop.is_set() else work.queue(post)
            if outcome is Outcome.STOPPED:
                return Ended.STOPPED, taken
            if outcome is not Outcome.EMPTY:
                taken += 1
                saved_in_a_row = saved_in_a_row + 1 if outcome is Outcome.SAVED else 0
            if limit is not None and taken >= limit:
                return Ended.CUT, taken
            if stop_at_saved and saved_in_a_row >= SAVED_IN_A_ROW:
                ui.info("{category}: {count} saved posts in a row, caught up",
                        category=kept.category, count=saved_in_a_row)
                return Ended.CUT, taken
    finally:
        kept.close()
    return (Ended.RAN_OUT if kept.ran_out else Ended.CUT), taken


def _chained(first: Any, steps: Generator[Any, None, None]) -> Generator[Any, None, None]:
    """Yield `first` followed by `steps`, closing `steps` on exit."""
    try:
        if first is not None:
            yield first
        yield from steps
    finally:
        steps.close()


def _read(
    work: _Work, run: SourceRun, site: Site, target: Target, options: Options, *,
    listed: Collection[str], resumes: dict[str, Resume], complete: list[str],
) -> None:
    """List the target's route and wanted categories into `work`."""
    route = next(r for r in run.source.routes if r.kind == target.kind)
    categories = wanted(type(run.source), route, options)
    if route.categories and not categories:
        return
    if not route.categories:
        _read_routeless(work, run, route, site, target, options)
        return
    _read_categories(work, run, route, categories, site, target, options, listed, resumes, complete)


def _read_routeless(
    work: _Work, run: SourceRun, route: Route, site: Site, target: Target, options: Options
) -> None:
    """List a route without categories (either posts or discovered targets)."""
    ui.debug("{target}: list {route}", target=str(target), route=route.kind)
    context = _context(run.source, route, site, target, run.run_async)
    steps = _steps(context, run.run_async)
    try:
        first = next(steps, None)
    except BaseException:
        steps.close()
        raise
    if isinstance(first, (Target, str)):
        _run_discovered(run, work.ledger.board, options, _chained(first, steps))
        return
    work.ledger.board.start(target, "", slots=run.source.concurrency)
    span = Range(options.since, options.until)
    own = _Kept(_chained(first, steps), "", newest_first=False, span=span, run_async=run.run_async)
    ended, _ = _queue_all(work, own, stop_at_saved=False, limit=options.limit)
    if ended is not Ended.STOPPED:
        work.ledger.board.listed(target, "")


def _read_categories(
    work: _Work, run: SourceRun, route: Route, categories: tuple[Category, ...],
    site: Site, target: Target, options: Options, listed: Collection[str],
    resumes: dict[str, Resume], complete: list[str],
) -> None:
    """List each wanted category of `route`, resuming from saved cursors when present."""
    context = _context(run.source, route, site, target, run.run_async)
    cursors = {} if options.again else targets.cursors(run.settings.targets, target)
    remaining = options.limit
    for category in categories:
        if remaining is not None and remaining <= 0:
            return
        ended, taken = _read_one_category(
            work, run, site, target, context, category, options,
            cursors.get(category.name) if category.newest_first else None,
            listed, resumes, complete, remaining,
        )
        if remaining is not None:
            remaining -= taken
        if ended is Ended.STOPPED:
            return
        work.ledger.board.listed(target, category.name)


def _read_one_category(
    work: _Work, run: SourceRun, site: Site, target: Target, context: Any,
    category: Category, options: Options, start: str | None, listed: Collection[str],
    resumes: dict[str, Resume], complete: list[str], remaining: int | None,
) -> tuple[Ended, int]:
    """List one category from `start` cursor, catching up from newest if resumed."""
    ui.debug("{target}: list {category}", target=str(target), category=category.name)
    work.ledger.board.start(target, category.name, slots=run.source.concurrency)
    stop_at_saved = category.newest_first and category.name in listed and not start
    saved = (
        (lambda pid, posted=None, cat=category: work.ledger.is_saved(pid, cat, posted))
        if stop_at_saved else None
    )
    resume = resumes[category.name] = Resume(start, label=f"{target}: {category.name}", saved=saved)
    if start:
        ui.info("{target}: {category}: going on where an interrupted run stopped",
                target=str(target), category=category.name)
    method = getattr(run.source, category.name)
    span = Range(options.since, options.until)
    kept = _Kept(method(site, context), category.name, newest_first=category.newest_first,
                 span=span, run_async=run.run_async)
    with listing(resume):
        ended, taken = _queue_all(work, kept, stop_at_saved=stop_at_saved, limit=remaining)
    if ended is Ended.RAN_OUT:
        complete.append(category.name)
    if ended is Ended.RAN_OUT and start and resume.taken and not resume.refused:
        rem = None if remaining is None else remaining - taken
        if rem is None or rem > 0:
            ended, extra = _catch_up_newest(work, run, site, target, context, category, span, rem)
            taken += extra
    return ended, taken


def _catch_up_newest(
    work: _Work, run: SourceRun, site: Site, target: Target, context: Any,
    category: Category, span: Range, limit: int | None,
) -> tuple[Ended, int]:
    """List from the newest down to where an interrupted run started."""
    ui.debug("{target}: list {category} from the newest",
             target=str(target), category=category.name)
    method = getattr(run.source, category.name)
    newest = _Kept(method(site, context), category.name, newest_first=True,
                   span=span, run_async=run.run_async)
    with listing(Resume()):
        return _queue_all(work, newest, stop_at_saved=True, limit=limit)


def _run_discovered(
    run: SourceRun, board: Board, options: Options, items: Generator[Any, None, None]
) -> None:
    """Download targets yielded by a non-query target-yielding route."""
    seen: set[Target] = set()
    child_options = replace(options, limit=None)
    try:
        for item in items:
            if run.settings.stop.soon.is_set():
                return
            child = item if isinstance(item, Target) else run.source.resolve(str(item))
            if child in seen:
                continue
            seen.add(child)
            download(run, child, board, child_options)
            if options.limit is not None and len(seen) >= options.limit:
                return
    finally:
        items.close()


def _steps(yielded: Any, run_async: RunAsync) -> Generator[Any, None, None]:
    """Iterate a sync or async iterable, closing it on exit."""
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
    """Infer file extension and media kind before downloading."""
    if file.content is not None:
        return PurePosixPath(file.name or "").suffix, None
    if file.name is not None:
        return PurePosixPath(file.name).suffix, paths.kind_of(file.name)
    url = (
        file.url if isinstance(file.url, str)
        else file.url[0] if isinstance(file.url, tuple)
        else ""
    )
    extension = PurePosixPath(urlsplit(url).path).suffix.lower() if url else ""
    kind = paths.kind_of(f"file{extension}") if extension else None
    if file.audio is not None and kind != paths.VIDEO:
        return DASH_EXTENSION, paths.VIDEO
    if kind is None:
        return "", paths.SNIFF
    return extension, kind


def _fields(template: str) -> set[str]:
    """Return top-level field names referenced in `template`."""
    return {
        name.split(".")[0].split("[")[0] for _, name, _, _ in _FORMATTER.parse(template) if name
    }


def _folder_format(source: Source, route: Route | None, category: Category | None) -> str:
    """Return the folder template for a file: category > route > source."""
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
