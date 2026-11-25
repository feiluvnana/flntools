"""`Engine`: every flnd command as a method, run on one loop, with the run's instance of each
plugin. SPEC.md#for-developers explains it.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import functools
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

import flnd.engine.resolve
from flnd.engine import auth, config, crawl, ctx, jobs, plugins, save, store
from flnd.engine.crawl import Options, RunShared, TargetReport
from flnd.engine.ctx import Ctx, PluginRun, shared_browser
from flnd.engine.events import Failed, Planned, sink
from flnd.engine.run import Stop, Tally, stopping
from flnd.errors import (
    Blocked,
    DiskError,
    Error,
    LoginError,
    NetworkError,
    PluginError,
    Stopped,
    UsageError,
)
from flnd.plugin import Browser, Plugin, Range, Target
from flnd.ui.log import ui
from flnkit import clock, net, text

__all__ = ["Chosen", "Engine", "Results", "TargetInfo"]

T = TypeVar("T")

# How many links deep the targets that listings yield are followed (`Engine._found`).
FOLLOW_DEPTH = 2
# Targets a search's sync downloads at most per run, among those it hadn't seen: a search that
# suddenly finds hundreds (a popular tag) is taken a slice per sync, never all at once.
MAX_SYNC_NEW = 25
SEEN_FLUSH = 10


def _plugin_stop(error: Error | None) -> LoginError | Blocked | None:
    """Return `error` if it means this plugin cannot continue (login refused or bot block)."""
    if isinstance(error, LoginError) or (
            isinstance(error, Blocked) and not error.retryable):
        return error
    return None


def _complete_wall(stamp: str, last_sync: str) -> float | None:
    """UTC Unix timestamp when a category completed, from `complete[cat]` and `last_sync`."""
    text_val = last_sync if (last_sync and last_sync.startswith(stamp)) else stamp
    try:
        if "T" in text_val:
            dt = datetime.datetime.fromisoformat(text_val.replace("Z", "+00:00"))
        else:
            dt = datetime.datetime.fromisoformat(f"{text_val}T00:00:00+00:00")
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.timestamp()


async def _child_done(
    run: PluginRun,
    db: store.Store | None,
    child: Target,
    categories: tuple[str, ...],
    rows: Mapping[str, store.TargetRow],
    failures: Mapping[str, tuple[bool, int, int]],
) -> bool:
    """Whether a discovered target is already completed in `db` for `categories`."""
    if db is None or child.plugin != run.source.code:
        return False
    child_key = store.key(child)
    child_row = rows.get(child_key)
    if child_row is None or child_key in failures:
        return False
    kind = child.kind or child_row.kind
    route = next((r for r in run.source.routes if r.kind == kind), None)
    if route is None:
        return False
    open_cats = set(child_row.cursors) | set(child_row.gaps)
    if not route.categories:
        return route.kind in child_row.complete and route.kind not in open_cats
    own = tuple(cat.name for cat in route.categories if cat.own)
    wanted = own if (not categories or "all" in categories) else categories
    if any(cat not in child_row.complete or cat in open_cats for cat in wanted):
        return False
    if route.single and child.plugin == "loi" and "archive" in wanted:
        post_id = child.value.removeprefix(f"{route.kind}/")
        return bool(await run.off(db.post_saved, child_key, post_id, "archive"))
    return True


def _query_sync_state(
    route: Any, row: store.TargetRow | None, stop_after: int, *, again: bool,
) -> dict[str, Any]:
    """Compute smart-sync state for a discovering query route."""
    seen = set() if again else set(row.seen if row is not None else ())
    resuming = not again and row is not None and bool(row.cursors)
    complete = not again and row is not None and route.kind in row.complete
    gaps = [] if (again or row is None) else list(row.gaps.get(route.kind, ()))
    head = None if (again or row is None) else row.heads.get(route.kind)
    streak: int | None = stop_after
    can_stop = route.newest_first and complete and not resuming
    if can_stop and "" in gaps:
        if head is not None:
            streak = None
        else:
            can_stop = False
    return {
        "seen": seen, "resuming": resuming, "complete": complete,
        "head": head, "streak": streak, "can_stop": can_stop,
    }


def _should_stop_query(state: dict[str, Any], met_head: bool, seen_in_a_row: int) -> bool:
    """Whether a discovering query has caught up to earlier runs."""
    if not state["can_stop"]:
        return False
    if met_head and seen_in_a_row >= crawl.HEAD_IN_A_ROW:
        return True
    streak = state["streak"]
    return streak is not None and seen_in_a_row >= streak


async def _save_query_progress(
    run: PluginRun, db: store.Store, target: Target, kind: str,
    state: dict[str, Any], first_child: str | None, *,
    caught_up: bool, cut_short: bool, failed: bool,
) -> None:
    """Persist `heads`, `complete`, `cursors`, and `gaps` after a query discovery run."""
    if not failed and (caught_up or not cut_short):
        heads = ({kind: first_child}
                 if not state["resuming"] and first_child is not None else None)
        await run.off(functools.partial(
            db.progress, target, cursors={kind: None, "max_id": None}, complete=[kind],
            heads=heads, gaps={kind: []},
        ))
    elif state["complete"] and not caught_up and (cut_short or failed):
        await run.off(functools.partial(
            db.progress, target, cursors={}, complete=[], gaps={kind: [""]},
        ))
# Targets active at once in a run (listing, or with more files queued than their places), and
# targets listing at once per plugin: one for a plugin that reads in the browser,
# whose site sees one person, and `HTTP_AT_ONCE` for one that only sends requests.
ACTIVE = jobs.LISTINGS
HTTP_AT_ONCE = 4
_SCHEDULER: jobs.Scheduler | None = None


class Chosen(NamedTuple):
    """A target as a run takes it: resolved, with the categories it runs."""

    target: Target
    # The categories named in its text (`|reels`), else the stored ones; None takes the
    # route's own.
    categories: tuple[str, ...] | None = None
    # The value as typed, with a link's `user:pw@`, which the target leaves out (`Options.link`);
    # "" when they are the same.
    link: str = ""


@dataclass(frozen=True)
class TargetInfo:
    """A known target's state and its saved files, as `Engine.target_ls` reads them."""

    target: Target
    follow: bool
    at: str
    files: int
    size: int
    seen: int
    categories: tuple[str, ...]
    complete: dict[str, str]
    query: bool
    status: str = ""
    failed: int = 0
    pending: tuple[str, ...] = ()


@dataclass(frozen=True)
class Results:
    """Summary of a `download` or `sync` run, one `TargetReport` per target."""

    targets: tuple[TargetReport, ...]

    @property
    def files(self) -> int:
        """Total files written across all targets."""
        return sum(len(target.written) for target in self.targets)

    @property
    def failed(self) -> tuple[Target, ...]:
        """Targets that failed with an error."""
        return tuple(target.target for target in self.targets if target.error is not None)

    @property
    def ok(self) -> bool:
        """Whether every target succeeded without error."""
        return not self.failed


class Engine:
    """flnd's commands as methods: `run_download`, `run_sync`, `resolve`, and the target reads."""

    def __init__(
        self,
        *,
        home: Path = Path(".flnd"),
        sources: Mapping[str, type[Plugin]] = plugins.PLUGINS,
        emit: Callable[[Any], Any] | None = None,
        stop: Stop | None = None,
    ) -> None:
        """Make the engine of one home.

        Args:
            home: the home folder.
            sources: the sources it knows, by code.
            emit: where its runs send their events (`flnd.engine.run.Run._emit`, whose answer
                is awaited; a plain function is called); None drops them.
            stop: the stop state its runs watch, when the caller owns it (a library run, whose
                `stop()` sets it); None makes one per command, set by Ctrl-C.
        """
        self.home, self.sources = home, sources
        self.emit = sink(emit)
        # Where the snapshots go (`run.state`), at once and never awaited.
        self._shown: Callable[[Any], Any] = emit or (lambda state: None)
        self.stop = stop
        # The `runs` row of the command going on, which its failures name; None between them.
        self.run: int | None = None
        # The live counts of the command going on, which `run.state` is made from.
        self.tally = Tally(self._shown)
        # The scheduler of the command going on; None until it opens one.
        self._scheduler: jobs.Scheduler | None = None
        # The database of the run going on; None between them.
        self._db: store.Store | None = None
        self._manifest: flnd.engine.resolve.Manifest | None = None

    def resolve(self, texts: Sequence[str]) -> list[Target]:
        """Resolve every target's text (`chosen`), leaving out the categories it names.

        Raises:
            UsageError: a text names no known plugin, or no route reads it.
            NetworkError: a short link couldn't be followed.
        """
        return [found.target for found in self.chosen(texts)]

    def chosen(self, targets: Sequence[Target | str | Chosen]) -> list[Chosen]:
        """Each target resolved, with its categories: those its text names (`|reels,posts`,
        `|all`), else the stored ones of a known target, else None for the route's own.

        A `Target` takes its stored categories, and a `Chosen` is taken as it is.

        Raises:
            UsageError: a text names no known plugin, no route reads it, or it names a
                category its route lacks.
            NetworkError: a short link couldn't be followed.
        """
        # Only the rows that keep categories: each imports its plugin, to write its key.
        rows = {item: row for item, row in self._rows().items() if row.categories}
        stored = {str(target): rows[store.key(target)].categories
                  for target in self._recorded(rows)}
        found: list[Chosen] = []
        for given in targets:
            if isinstance(given, Chosen):
                found.append(given)
            elif isinstance(given, Target):
                found.append(Chosen(given, stored.get(str(given))))
            else:
                chosen = self._read(given, stored)
                # The target alone: the text as typed may hold a link's `user:pw@`.
                ui.debug("resolved {target} ({kind}), categories {categories}",
                         target=chosen.target, kind=chosen.target.kind,
                         categories=chosen.categories)
                found.append(chosen)
        return found

    @property
    def manifest(self) -> flnd.engine.resolve.Manifest:
        """What resolving reads of the plugins, each from its class when first asked for."""
        if self._manifest is None:
            self._manifest = flnd.engine.resolve.Manifest(
                self.sources, lambda code: plugins.domains(self.sources, code))
        return self._manifest

    def _read(self, text: str, stored: Mapping[str, Sequence[str]]) -> Chosen:
        """One target's text, resolved (`flnd.engine.resolve`), with its categories."""
        found = flnd.engine.resolve.resolve(text, manifest=self.manifest, known=stored,
                                            follow=_follow)
        kept = found.named or found.key in stored
        return Chosen(Target(found.key, kind=found.kind),
                      found.categories if kept else None, found.link)

    async def run_download(
        self,
        targets: Sequence[Target | str | Chosen],
        *,
        time: Range | None = None,
        limit: int | None = None,
        dry: bool = False,
        sync: bool = False,
        again: bool = False,
        keep: bool = False,
        folder: str | Mapping[str, str] | None = None,
    ) -> Results:
        """Download each target with its saved or given options and return a `Results`.

        Args:
            targets: the targets, as text (`ins:example_user|reels`) or resolved.
            time: `--when`, as a `Range`; None for no limit.
            limit: at most this many posts per target, or targets per search; None for all.
            dry: plan what would be saved (`Planned` events), and write nothing.
            sync: follow the targets, so `sync` takes them later.
            again: ignore saved cursors and the stop at saved posts.
            keep: whether to save each target's categories, for later syncs (`follow`).
            folder: a folder template under `media/<plugin>/`, or a list or mapping of them by
                category (`config.folders`); None for each target's folder. A search needs one.

        Returns:
            One `TargetReport` per target, in order.

        Raises:
            UsageError: a bad target, category, limit or folder, before anything is fetched.
            DiskError: the home can't be written; the run stopped.
            KeyboardInterrupt: Ctrl-C (or the run's stop) ended it.
        """

        if limit is not None and limit < 1:
            raise UsageError("--limit must be at least 1")
        if folder is not None:
            config.check_folder(config.folder_spec(folder), self.sources)
        span = time or Range()
        # On a thread: a short link is followed with a request of its own.
        resolved = await asyncio.to_thread(self.chosen, targets)
        folder = await _off(self.check_folder, folder, resolved, again=again)
        stored = {item: row.folder for item, row in (await _off(self._rows)).items()
                  if row.folder}
        base = Options(
            since=span.since,
            until=span.until,
            limit=limit,
            again=again,
            stop_at_saved=sync and not again,
            folder=folder,
        )
        chosen = [(found.target, replace(
            base, categories=found.categories, link=found.link or None,
            folder=stored.get(store.key(found.target)) if folder is None
            and self._is_search(found.target) else folder))
            for found in resolved]
        with stopping(self.stop) as stop:
            async with self._live():
                if dry:
                    return self._finish_report(await self._dry_run(chosen, span, stop), stop)
                ui.info("Storage: {path}", path=await _off((self.home / store.MEDIA).resolve))
                async with self._recording("get", [t for t, _ in chosen]):
                    async with self._running(stop) as runner:
                        return await self._download(runner, chosen, span=span, sync=sync,
                                                    stop=stop, keep=keep)

    def check_folder(self, folder: str | Mapping[str, str] | None,
                     resolved: Sequence[Chosen], *, again: bool = False) -> str | None:
        """Check a call's folder value against its targets before any request, and return it as
        one string.

        A search with no folder takes the one its row kept; one given a different folder than
        its row kept is refused unless `again`.

        Raises:
            UsageError: the value is wrong (`config.check_folder`), or it moves a followed search.
        """
        spec = None if folder is None else config.folder_spec(folder)
        stored = {item: row.folder for item, row in self._rows().items() if row.folder}
        config.check_folder(spec, self.sources, resolved, stored=stored)
        for found in resolved if spec is not None and not again else ():
            old = stored.get(store.key(found.target))
            if old is not None and old != spec:
                raise UsageError(
                    f"{found.target.plugin}?{found.target.value}: this search is saved under "
                    f"--folder {old!r}, not {spec!r}",
                    hint="add --again to use the new folder; files already saved stay where "
                         "they are")
        return spec

    async def run_sync(self, targets: Sequence[Target | str] = (), *,
                       time: Range | None = None) -> Results:
        """Bring synced targets up to date: fetch new posts and sum up.

        Args:
            targets: only these targets; every synced target when empty.
            time: the days every target takes posts from (`[defaults] when` in config.toml`);
                None for every day.

        Returns:
            One `TargetReport` per target.
        """
        # On a thread: a short link is followed with a request of its own.
        resolved = await asyncio.to_thread(self.chosen, targets)
        span = time or Range()
        chosen = [(target, replace(options, since=span.since, until=span.until))
                  for target, options in await _off(self._plan, resolved)]
        if not chosen:
            ui.info("nothing to sync")
            return Results(())
        with stopping(self.stop) as stop:
            async with self._recording("get", [t for t, _ in chosen]), \
                    self._live(), self._running(stop) as runner:
                results: dict[int, TargetReport] = {}
                await self._sync_normal(runner, chosen, results, stop)
                await self._sync_queries(runner, chosen, results, stop)
                report = Results(tuple(results[i] for i in range(len(chosen))))
            return self._finish_report(report, stop)

    def target_ls(self, *, plugin: str | None = None) -> list[TargetInfo]:
        """Every target `flnd.db` has a row for, with its state and saved files.

        Args:
            plugin: only this plugin's targets; None for all.
        """
        with store.open(self.home) as db:
            rows = db.targets()
            failures = db.target_failures()
            return [self._target_info(target, rows, db, failures=failures)
                    for target in self._recorded(rows)
                    if plugin is None or target.plugin == plugin]

    def target_follow(self, targets: Sequence[Target | str], *, on: bool) -> list[TargetInfo]:
        """Follow the targets, so `get` with none takes them, or stop following them.

        A target is followed even if it was never downloaded, except a single post; stopping
        takes a known target only. Every target is checked before any row changes.

        Raises:
            UsageError: a target is a single post, or (to stop) isn't known.
        """
        resolved = self.resolve(targets)
        rows = self._rows()
        for target in resolved:
            if on and self._is_single(target):
                raise UsageError(f"{target}: a single post is never followed")
            if not on and store.key(target) not in rows:
                raise UsageError(f"{target}: not a known target", hint="flnd get <target> first")
        with store.open(self.home) as db:
            for target in resolved:
                db.follow(target, on)
        return self._infos(resolved)

    def target_retry(self, targets: Sequence[Target | str]) -> list[TargetInfo]:
        """Make the failed files of known targets retryable again, for the next `get`.

        Raises:
            UsageError: a target isn't known.
        """
        resolved = self._known(targets)
        with store.open(self.home) as db:
            for target in resolved:
                db.retry(store.key(target))
        return self._infos(resolved)

    def target_clean(self, targets: Sequence[Target | str], *, media: bool = False,
                     ) -> list[TargetInfo]:
        """Forget known targets: their rows, files rows, failures, cursors, heads and gaps.

        With `media`, their recorded files, sidecars, links and extracted folders go too, and
        the folders left empty. Files under `media/` no row names stay.

        Returns:
            Each target as it stood when it was forgotten.

        Raises:
            UsageError: a target isn't known.
        """
        resolved = self._known(targets)
        found = self._infos(resolved)
        with store.open(self.home) as db:
            for target in resolved:
                rows = db.forget(store.key(target))
                if media:
                    store.erase(self.home, rows)
        return found

    def _known(self, targets: Sequence[Target | str]) -> list[Target]:
        """The targets resolved, each with a row in `flnd.db`."""
        resolved = self.resolve(targets)
        rows = self._rows()
        for target in resolved:
            if store.key(target) not in rows:
                raise UsageError(f"{target}: not a known target", hint="flnd get <target> first")
        return resolved

    def _infos(self, targets: Sequence[Target]) -> list[TargetInfo]:
        """Each target's `TargetInfo`, in order, as its row stands."""
        keys = [store.key(target) for target in targets]
        found = {store.key(info.target): info for info in self.target_ls()}
        return [found[key] for key in keys if key in found]

    async def _prune_fresh(
        self,
        runner: _Runner,
        chosen: Sequence[tuple[Target, Options]],
    ) -> tuple[list[tuple[int, Target, Options]], dict[int, TargetReport]]:
        """Drop targets or categories marked complete within `[defaults] fresh`."""
        fresh_text = runner.settings.config.defaults.fresh
        fresh_s = config.duration_seconds(fresh_text)
        if fresh_s <= 0:
            return [(i, t, o) for i, (t, o) in enumerate(chosen)], {}
        rows = await _off(self._rows)
        db = runner.settings.db
        failures = await _off(db.target_failures) if db is not None else {}
        now = clock.wall()
        active: list[tuple[int, Target, Options]] = []
        skipped: dict[int, TargetReport] = {}
        for i, (target, options) in enumerate(chosen):
            if options.again or options.since is not None or options.until is not None:
                active.append((i, target, options))
                continue
            tkey = store.key(target)
            row = rows.get(tkey)
            route = self._route(target)
            if row is None or route is None or tkey in failures:
                active.append((i, target, options))
                continue
            open_cats = set(row.cursors) | set(row.gaps)
            route_name = getattr(route, "name", None) or route.kind

            def is_fresh(name: str, row: store.TargetRow = row,
                         open_cats: set[str] = open_cats) -> bool:
                stamp = row.complete.get(name)
                if not stamp or name in open_cats:
                    return False
                done_at = _complete_wall(stamp, row.last_sync)
                return done_at is not None and 0 <= now - done_at < fresh_s

            if self._is_query(target) or not route.categories:
                if is_fresh(route_name) and "" not in row.gaps:
                    ui.info("{target}: complete; skipped (within {fresh})",
                            target=target, fresh=fresh_text)
                    skipped[i] = TargetReport(target, (), 0)
                    continue
                active.append((i, target, options))
                continue
            own = tuple(
                cat[0] if isinstance(cat, tuple) else cat.name
                for cat in route.categories
                if (cat[1] if isinstance(cat, tuple) else cat.own)
            )
            wanted = own if (not options.categories or "all" in options.categories) else (
                options.categories)
            remaining: list[str] = []
            for cat in wanted:
                fresh = is_fresh(cat)
                if (fresh and route.single and target.plugin == "loi"
                        and cat == "archive" and db is not None):
                    post_id = target.value.removeprefix(f"{route_name}/")
                    fresh = await _off(db.post_saved, tkey, post_id, "archive")
                if not fresh:
                    remaining.append(cat)
            if not remaining:
                ui.info("{target}: complete; skipped (within {fresh})",
                        target=target, fresh=fresh_text)
                skipped[i] = TargetReport(target, (), 0)
            elif len(remaining) < len(wanted):
                active.append((i, target, replace(options, categories=tuple(remaining))))
            else:
                active.append((i, target, options))
        return active, skipped

    async def _download(
        self,
        runner: _Runner,
        chosen: Sequence[tuple[Target, Options]],
        *,
        span: Range,
        sync: bool,
        stop: Stop,
        keep: bool = False,
    ) -> Results:
        """Download the targets, recording each in flnd.db first."""
        active, skipped = await self._prune_fresh(runner, chosen)
        if not active:
            return self._finish_report(
                Results(tuple(skipped[i] for i in range(len(chosen)))), stop)
        rows = await _off(self._rows)
        kept = []
        for _, target, options in active:
            # A followed target stops at its saved posts, unless told to list them again.
            row = rows.get(store.key(target))
            followed = row is not None and row.follow and not options.again
            kept.append((target, replace(
                options, stop_at_saved=options.stop_at_saved or followed)))
        await _off(self._record, kept, follow=sync, keep=keep)
        if not any(self._is_query(target) for target, _ in kept):
            run_reports = await runner.run(kept)
            by_orig: dict[int, list[TargetReport]] = {
                orig_i: [rep] for (orig_i, _, _), rep in zip(active, run_reports)
            }
            if len(run_reports) > len(active):
                by_orig[active[-1][0]].extend(run_reports[len(active):])
        else:
            normal = [(i, item) for i, item in enumerate(kept) if not self._is_query(item[0])]
            queries = [(i, item) for i, item in enumerate(kept) if self._is_query(item[0])]
            by_slot: dict[int, list[TargetReport]] = {}

            async def run_normal() -> None:
                got = await runner.run([item for _, item in normal])
                for (slot, _), rep in zip(normal, got):
                    by_slot[slot] = [rep]
                if len(got) > len(normal):
                    by_slot[normal[-1][0]].extend(got[len(normal):])

            async def run_query(slot: int, target: Target, options: Options) -> None:
                by_slot[slot] = await self._run_query(runner, target, options, span=span,
                                                      stop=stop)

            tasks = [asyncio.ensure_future(run_query(i, t, o)) for i, (t, o) in queries]
            if normal:
                tasks.append(asyncio.ensure_future(run_normal()))
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            by_orig = {active[slot][0]: by_slot[slot] for slot in range(len(kept))}
        reports: list[TargetReport] = []
        for i in range(len(chosen)):
            if i in skipped:
                reports.append(skipped[i])
            else:
                reports.extend(by_orig[i])
        return self._finish_report(Results(tuple(reports)), stop)

    def _finish_report(self, report: Results, stop: Stop) -> Results:
        """Raise `KeyboardInterrupt` when the run was stopped, else log its totals and return it."""
        if stop.soon.is_set():
            raise KeyboardInterrupt
        _log_totals(report)
        return report

    async def _sync_normal(
        self,
        runner: _Runner,
        chosen: Sequence[tuple[Target, Options]],
        results: dict[int, TargetReport],
        stop: Stop,
    ) -> None:
        """Sync the plain targets of `chosen` (not searches), each stopping at saved posts."""
        indices = [i for i, (target, _) in enumerate(chosen) if not self._is_query(target)]
        if not indices:
            return
        normal = [chosen[i] for i in indices]
        await _off(self._record, normal)
        ui.info("Storage: {path}", path=await _off((self.home / store.MEDIA).resolve))
        reports = await runner.run([(t, replace(opts, stop_at_saved=True)) for t, opts in normal])
        if stop.soon.is_set():
            raise KeyboardInterrupt
        for pos, rep in zip(indices, reports):
            results[pos] = rep

    async def _sync_queries(
        self,
        runner: _Runner,
        chosen: Sequence[tuple[Target, Options]],
        results: dict[int, TargetReport],
        stop: Stop,
    ) -> None:
        """Sync the searches of `chosen`, one after another, after the plain targets."""
        indices = [i for i, (target, _) in enumerate(chosen) if self._is_query(target)]
        if not indices:
            return
        query_chosen = [chosen[i] for i in indices]
        await _off(self._record, query_chosen)
        for pos, (target, options) in zip(indices, query_chosen):
            results[pos] = await self._sync_query(runner, target, options, stop=stop)
            if stop.soon.is_set():
                raise KeyboardInterrupt

    def _target_info(
        self, target: Target, rows: Mapping[str, store.TargetRow], db: store.Store, *,
        failures: Mapping[str, tuple[bool, int, int]] | None = None,
    ) -> TargetInfo:
        """A target's state and its saved files' count and bytes, from `flnd.db` alone."""
        target_key = store.key(target)
        row = rows.get(target_key)
        is_query = self._is_query(target)
        if is_query:
            files, size = 0, 0
        elif row is not None:
            files, size = row.files, row.bytes
        else:
            files, size = db.count(target_key)
        kept = row.categories if row is not None else ()
        categories = () if is_query else kept or tuple(self.sources[target.plugin].categories)
        complete = dict(row.complete) if row is not None else {}
        is_single = self._is_single(target)
        open_cats = set(row.cursors) | set(row.gaps) if row is not None else set()
        pending = (
            tuple(cat for cat in categories if cat not in complete or cat in open_cats)
            if not is_query and not is_single else ()
        )
        failure = (failures if failures is not None else db.target_failures()).get(
            target_key, (False, 0, 0))
        status, failed = _target_status(
            row, target, categories, complete, pending=pending, files=files,
            is_query=is_query, is_single=is_single, failure=failure,
        )
        return TargetInfo(
            target=target,
            follow=row.follow if row is not None else False,
            at=row.last_sync if row is not None else "",
            files=files,
            size=size,
            seen=len(row.seen) if is_query and row is not None else 0,
            categories=categories,
            complete=complete,
            query=is_query,
            status=status,
            failed=failed,
            pending=pending,
        )

    def _route(self, target: Target) -> Any:
        """The route that reads `target` (its manifest entry or its class's), or None."""
        plugin = self.manifest.get(target.plugin)
        if plugin is not None:
            return next((r for r in plugin.routes if r.name == target.kind), None)
        try:
            source = self.sources[target.plugin]
        except PluginError:
            return None
        return next((r for r in source.routes if r.kind == target.kind), None)

    def _is_query(self, target: Target) -> bool:
        """Whether `target`'s route discovers other targets (a search) rather than posts."""
        route = self._route(target)
        return route is not None and route.discovers

    def _is_search(self, target: Target) -> bool:
        """Whether `target` is a query (`<code>?<query>`, or a search link), which keeps a
        folder."""
        try:
            return self._is_query_kind(target.plugin, target.kind)
        except PluginError:
            return False

    def _is_single(self, target: Target) -> bool:
        """Whether `target`'s route reads one post, which is never followed."""
        route = self._route(target)
        return route is not None and route.single

    async def _dry_run(
        self,
        chosen: Sequence[tuple[Target, Options]],
        span: Range,
        stop: Stop,
    ) -> Results:
        """Plan every target without saving anything (`--dry`), one `Planned` event per file."""
        # A dry run reads `flnd.db` to tell "saved" from "new", but never writes it: a home
        # without one stays untouched.
        async with self._running(stop, dry=True) as runner:
            return Results(tuple([await self._dry_one(runner, target, options, span, stop)
                                 for target, options in chosen]))

    async def _dry_one(
        self,
        runner: _Runner,
        target: Target,
        options: Options,
        span: Range,
        stop: Stop,
    ) -> TargetReport:
        """Plan one target of a dry run, on its plugin's instance."""

        entry, failed = await runner.ready(target)
        if entry is None or failed is not None:
            assert failed is not None
            return failed
        try:
            await _log_dry(entry.run, target, options, span, is_query=self._is_query(target))
        except KeyboardInterrupt:
            if not stop.soon.is_set() or stop.now.is_set():
                raise
            return TargetReport(target, (), 0, error=Stopped(crawl.STOPPED))
        except Exception as error:
            failure = crawl.failure(error, target, entry.run.source)
            await crawl.tell(target, failure, self.emit)
            return TargetReport(target, (), 0, error=failure)
        return TargetReport(target, (), 0)

    async def _run_query(
        self,
        runner: _Runner,
        target: Target,
        options: Options,
        *,
        span: Range,
        stop: Stop,
    ) -> list[TargetReport]:
        """Download a search, its row recorded already: list the targets it finds, then
        download each one.

        Its found targets count on the search's row of the run's snapshot (`Tally.adopt`).

        Args:
            runner: the command's run.
            target: the search.
            options: its options; `limit` bounds how many targets it takes.
            span: `--when`, which the search filters what it finds by.
            stop: the run's stop state.

        Returns:
            One report per target found; only the search's own when it needs a login it lacks.
        """
        self.tally.add(target, _planned(options))
        entry, failed = await runner.ready(target)
        if entry is None or failed is not None:
            assert failed is not None
            return [failed]
        reports: list[TargetReport] = []
        await self._discover_and_run(runner, entry.run, target, options, reports, span=span,
                                     stop=stop)
        if reports and (stopped := _plugin_stop(reports[0].error)) is not None:
            entry.refused = stopped
        return reports

    async def _discover_and_run(
        self,
        runner: _Runner,
        run: PluginRun,
        target: Target,
        options: Options,
        reports: list[TargetReport],
        *,
        span: Range,
        stop: Stop,
    ) -> None:
        """List a search's targets and download each new one, short-circuiting when caught up."""

        db = run.settings.db
        route = next(r for r in run.source.routes if r.kind == target.kind)
        row = await run.off(db.target, store.key(target)) if db is not None else None
        known_rows = await _off(self._rows) if db is not None and not options.again else {}
        known_failures = (
            await run.off(db.target_failures) if db is not None and not options.again else {}
        )
        state = _query_sync_state(route, row, run.config.stop_after, again=options.again)
        seen_batch: list[str] = []
        discover_failed = False
        caught_up = False
        cut_short = False
        quota_skipped = False
        first_child: str | None = None
        seen_in_a_row = 0
        met_head = False
        taken = 0
        child_opts = replace(options, limit=None, query=target.value, link=None)
        planned = _planned(child_opts)
        try:
            self.tally.start(target, target.kind)
            listing = crawl.Listing(
                None, label=f"{target}: {route.kind}",
                head=state["head"] if state["can_stop"] else None,
            )
            async with contextlib.aclosing(crawl.discover(
                run, target, limit=None, time_range=span, again=options.again,
                head=listing.head, listing=listing,
            )) as found:
                async for child in found:
                    if stop.soon.is_set():
                        break
                    child_key = store.key(child)
                    if first_child is None and not state["resuming"]:
                        first_child = child_key
                    if not options.again and (
                        child_key in state["seen"]
                        or await _child_done(run, db, child, child_opts.categories,
                                             known_rows, known_failures)
                    ):
                        if child_key not in state["seen"]:
                            state["seen"].add(child_key)
                            seen_batch.append(child_key)
                            if db is not None and len(seen_batch) >= SEEN_FLUSH:
                                await run.off(db.seen, target, seen_batch)
                                seen_batch.clear()
                        seen_in_a_row += 1
                        if state["head"] is not None and child_key == state["head"]:
                            met_head = True
                        if _should_stop_query(state, met_head, seen_in_a_row):
                            caught_up = True
                            listing.caught_up = True
                            break
                        continue
                    if options.limit is not None and taken >= options.limit:
                        cut_short = True
                        break
                    seen_in_a_row = 0
                    self.tally.add(child, planned)
                    left_before = getattr(run.source, "_left", 0)
                    rep = await runner.target(child, child_opts)
                    if getattr(run.source, "_left", 0) > left_before:
                        quota_skipped = True
                    elif rep.error is None:
                        state["seen"].add(child_key)
                        seen_batch.append(child_key)
                        if db is not None and len(seen_batch) >= SEEN_FLUSH:
                            await run.off(db.seen, target, seen_batch)
                            seen_batch.clear()
                    reports.append(rep)
                    taken += 1
                    if options.limit is not None and taken >= options.limit:
                        cut_short = True
                        break
            self.tally.end(target, failed=stop.soon.is_set())
        except KeyboardInterrupt:
            discover_failed = True
            if not stop.soon.is_set() or stop.now.is_set():
                raise
            reports.append(TargetReport(target, (), 0, error=Stopped(crawl.STOPPED)))
        except Exception as error:
            discover_failed = True
            failed = crawl.failure(error, target, run.source)
            await crawl.tell(target, failed, self.emit)
            reports.append(TargetReport(target, (), 0, error=failed))
        finally:
            if discover_failed:
                self.tally.end(target, failed=True)
            if seen_batch and db is not None:
                await run.off(db.seen, target, seen_batch)
            if db is not None:
                await _save_query_progress(
                    run, db, target, route.kind, state, first_child,
                    caught_up=caught_up, cut_short=cut_short or quota_skipped,
                    failed=discover_failed or stop.soon.is_set(),
                )

    async def _sync_query(
        self,
        runner: _Runner,
        target: Target,
        options: Options,
        *,
        stop: Stop,
    ) -> TargetReport:
        """Sync one search: download the targets it finds that it hadn't seen before."""
        rows = await _off(self._rows)
        row = rows.get(store.key(target))
        seen_keys = set() if options.again else set(row.seen if row is not None else ())
        known_keys = set() if options.again else set(rows)
        self.tally.add(target, _planned(options))
        entry, failed = await runner.ready(target)
        if entry is None or failed is not None:
            assert failed is not None
            return failed
        report = await self._sync_query_run(runner, entry.run, target, options, row,
                                            seen_keys, known_keys, stop)
        if (stopped := _plugin_stop(report.error)) is not None:
            entry.refused = stopped
        return report

    async def _sync_query_run(
        self,
        runner: _Runner,
        run: PluginRun,
        target: Target,
        options: Options,
        row: store.TargetRow | None,
        seen_keys: set[str],
        known_keys: set[str],
        stop: Stop,
    ) -> TargetReport:
        """Run a search's sync, and keep the keys it saw, however it ends."""

        written: list[Path] = []
        skipped, child_failed, seen_to_mark, failed = 0, None, [], False
        route = next(r for r in run.source.routes if r.kind == target.kind)
        state = _query_sync_state(route, row, run.config.stop_after, again=options.again)
        state["seen"] = seen_keys
        outcome = {"caught_up": False, "cut_short": False, "first_child": None}
        try:
            skipped, child_failed = await self._sync_query_items(
                runner, run, target, options, state, known_keys, stop, written,
                seen_to_mark, outcome)
        except KeyboardInterrupt:
            if not stop.soon.is_set() or stop.now.is_set():
                raise
            failed = True
            return TargetReport(target, tuple(written), skipped, error=Stopped(crawl.STOPPED))
        except Exception as error:
            failed = True
            ended = crawl.failure(error, target, run.source)
            await crawl.tell(target, ended, self.emit)
            return TargetReport(target, tuple(written), skipped, error=ended)
        finally:
            self.tally.end(target, failed=failed)
            db = run.settings.db
            if seen_to_mark and db is not None:
                await run.off(db.seen, target, seen_to_mark)
            if db is not None:
                await _save_query_progress(
                    run, db, target, route.kind, state, outcome["first_child"],
                    caught_up=outcome["caught_up"], cut_short=outcome["cut_short"],
                    failed=failed or stop.soon.is_set(),
                )
        return TargetReport(target, tuple(written), skipped, error=child_failed)

    async def _sync_query_items(
        self,
        runner: _Runner,
        run: PluginRun,
        target: Target,
        options: Options,
        state: dict[str, Any],
        known_keys: set[str],
        stop: Stop,
        written: list[Path],
        seen_to_mark: list[str],
        outcome: dict[str, Any],
    ) -> tuple[int, Error | Stopped | None]:
        """List a search's targets and download the new ones, at most `MAX_SYNC_NEW` of them."""

        db = run.settings.db
        skipped, new_count, child_failed = 0, 0, None
        seen_in_a_row, met_head = 0, False
        child_opts = replace(options, limit=None, query=target.value, link=None)
        planned = _planned(child_opts)
        span = Range(options.since, options.until)
        self.tally.start(target, target.kind)
        listing = crawl.Listing(
            None, label=f"{target}: {target.kind}",
            head=state["head"] if state["can_stop"] else None,
        )
        async with contextlib.aclosing(crawl.discover(
            run, target, time_range=span, again=options.again,
            head=listing.head, listing=listing,
        )) as found:
            async for child in found:
                if stop.soon.is_set():
                    break
                child_key = store.key(child)
                if outcome["first_child"] is None and not state["resuming"]:
                    outcome["first_child"] = child_key
                already = (not options.again) and (
                    child_key in state["seen"] or child_key in known_keys
                )
                if already:
                    if child_key not in state["seen"]:
                        state["seen"].add(child_key)
                        seen_to_mark.append(child_key)
                        if db is not None and len(seen_to_mark) >= SEEN_FLUSH:
                            await run.off(db.seen, target, seen_to_mark)
                            seen_to_mark.clear()
                    seen_in_a_row += 1
                    if state["head"] is not None and child_key == state["head"]:
                        met_head = True
                    if _should_stop_query(state, met_head, seen_in_a_row):
                        outcome["caught_up"] = True
                        listing.caught_up = True
                        break
                    continue
                if new_count >= MAX_SYNC_NEW:
                    outcome["cut_short"] = True
                    break
                seen_in_a_row = 0
                new_count += 1
                self.tally.add(child, planned)
                left_before = getattr(run.source, "_left", 0)
                rep = await runner.target(child, child_opts)
                written.extend(rep.written)
                skipped += rep.skipped
                if rep.error is None and getattr(run.source, "_left", 0) == left_before:
                    state["seen"].add(child_key)
                    seen_to_mark.append(child_key)
                    if db is not None and len(seen_to_mark) >= SEEN_FLUSH:
                        await run.off(db.seen, target, seen_to_mark)
                        seen_to_mark.clear()
                elif rep.error is not None:
                    child_failed = rep.error
        self.tally.end(target, failed=False)
        return skipped, child_failed

    @contextlib.asynccontextmanager
    async def _live(self) -> AsyncIterator[None]:
        """Count one command afresh, publishing its snapshots while it runs (`run.state`)."""
        self.tally = Tally(self._shown)
        self._scheduler = None
        async with self.tally.publishing():
            yield

    @contextlib.asynccontextmanager
    async def _running(self, stop: Stop, *, dry: bool = False) -> AsyncIterator[_Runner]:
        """A command's run: the home's database, the browser, the save and the plugin instances."""

        # The home's settings are read once, off the loop: its browser, its save and each
        # plugin's run take theirs from them, and a file's warnings are said once.
        found = await _off(config.settings, self.home, plugins=self.sources)
        opened_here = False
        db = self._db
        if db is None:
            db = await _off(store.open, self.home)
            self._db = db
            opened_here = True
        try:
            settings = RunShared(home=self.home, db=db, run=None if dry else self.run,
                                 stop=stop, dry=dry, emit=self.emit, resolve=self._one,
                                 tally=self.tally, config=found)
            settings.claims = crawl.Claims()
            runner = _Runner(self, settings)
            settings.follow = runner.follow
            async with shared_browser(settings), save.attached(settings):
                scheduler = ctx.scheduler(settings)
                assert scheduler is not None
                self._scheduler = scheduler
                global _SCHEDULER
                _SCHEDULER = scheduler
                try:
                    yield runner
                finally:
                    try:
                        await runner.close()
                    finally:
                        _SCHEDULER = None
                        self._scheduler = None
        finally:
            if opened_here:
                self._db = None
                await _off(db.close)

    @contextlib.asynccontextmanager
    async def script(self, code: str) -> AsyncIterator[Ctx]:
        """A plugin's `Ctx` for a script, on the caller's running loop.

        It opens what a run opens (the home's database, the scheduler with its holds and walls,
        the plugin's instance, its login check and its `connect` hook), and on the way out
        writes the cookies and the state back and closes it all, an error included. Nothing
        stops it but its caller: there is no Ctrl-C handler.

        Args:
            code: the plugin's code.

        Raises:
            UsageError: no such plugin, or the home's config.toml has a value it doesn't take.
            LoginError: a login the plugin needs.
        """
        if code not in self.sources:
            raise UsageError(f"unknown plugin {code!r}. Plugins: {', '.join(self.sources)}")
        async with self._running(Stop()) as runner:
            entry = await runner.entry(code)
            if entry.error is not None:
                raise entry.error
            if not entry.logged_in and _required(entry.run.source):
                raise crawl.logged_out(Target(f"{code}:", kind=auth.LOGIN))
            yield _plugin_ctx(entry.run)

    def _recorded(self, rows: Mapping[str, store.TargetRow]) -> list[Target]:
        """The targets of `flnd.db`'s rows, rebuilt from each row's source, kind and value."""
        return [
            Target(f"{row.plugin}{'?' if self._is_query_kind(row.plugin, row.kind) else ':'}"
                   f"{row.value}", kind=row.kind)
            for row in rows.values()
            if row.kind and row.plugin in self.sources
        ]

    def _rows(self) -> dict[str, store.TargetRow]:
        """Every target `flnd.db` knows, by key; none for a home that has no database yet."""
        if self._db is not None:
            return self._db.targets()
        with store.open(self.home) as db:
            return db.targets()

    def _record(self, chosen: Sequence[tuple[Target, Options]], *, follow: bool | None = None,
                keep: bool = False) -> None:
        """Remember the targets of a run in `flnd.db`, in one transaction."""
        if not chosen:
            return
        if self._db is not None:
            with self._db.transaction():
                for target, options in chosen:
                    self._db.record(
                        target, query=options.query,
                        folder=options.folder if self._is_search(target) else None,
                        follow=None if follow is None else follow and not self._is_single(target),
                        categories=(options.categories or ()) if keep else None)
            return
        with store.open(self.home) as db, db.transaction():
            for target, options in chosen:
                db.record(target, query=options.query,
                          folder=options.folder if self._is_search(target) else None,
                          follow=None if follow is None else follow and not self._is_single(target),
                          categories=(options.categories or ()) if keep else None)

    @contextlib.asynccontextmanager
    async def _recording(self, command: str, targets: Sequence[Target]) -> AsyncIterator[None]:
        """A `runs` row for a command that downloads, ended with the files it saved, however
        it ends (a stopped run's row keeps its count too); its reads and writes are off the
        loop.

        Args:
            command: the command's name.
            targets: its targets; the first three are named in the row.
        """
        named = " ".join([command, *map(str, targets[:3])])
        opened_here = False
        if self._db is None:
            self._db = await _off(store.open, self.home)
            opened_here = True
        try:
            self.run = await _off(self._started, named if len(targets) <= 3 else f"{named} …")
            try:
                yield
            finally:
                run, self.run = self.run, None
                # The command's tally: `_live` made it before, or inside, this block.
                await _off(self._ended, run, self.tally.saved)
        finally:
            if opened_here and self._db is not None:
                db, self._db = self._db, None
                await _off(db.close)

    def _started(self, named: str) -> int:
        """Open a command's `runs` row, after sweeping the month-old logs
        (SPEC.md#install-and-first-run)."""
        store.sweep(self.home)
        if self._db is not None:
            return self._db.start(named)
        with store.open(self.home) as db:
            return db.start(named)

    def _ended(self, run: int | None, saved: int) -> None:
        if self._db is not None:
            self._db.end(run, saved=saved)
            return
        with store.open(self.home) as db:
            db.end(run, saved=saved)

    def _is_query_kind(self, code: str, kind: str) -> bool:
        """Whether the route of `kind` in source `code` takes a query (`<code>?<query>`)."""
        route = next((r for r in self.sources[code].routes if r.kind == kind), None)
        return route is not None and route.is_query

    def _plan(self, targets: Sequence[Chosen]) -> list[tuple[Target, Options]]:
        """Which targets a sync takes, each with the options its row keeps."""

        rows = self._rows()
        if not targets:
            followed = {item: row for item, row in rows.items() if row.follow}
            targets = [Chosen(target, rows[store.key(target)].categories or None)
                       for target in self._recorded(followed)]
        planned = []
        for target, categories, link in targets:
            row = rows.get(store.key(target))
            planned.append((target, Options(categories=categories, link=link or None,
                                            query=row.query if row else None,
                                            folder=row.folder if row else None)))
        return planned

    def _one(self, text: str) -> Target:
        """Resolve one target's text on any plugin, following a short link once; the
        categories it names are left out."""
        return self._read(text, {}).target


@dataclass(eq=False)
class _Entry:
    """One plugin's instance in a run: its source run, its login, and its `connect` hook."""

    run: PluginRun
    stack: contextlib.AsyncExitStack
    # Whether its login is kept (a source without a login always is).
    logged_in: bool = True
    # What broke it as it started (its `connect`, its login check); its targets fail with it.
    error: BaseException | None = None
    # A login or protecting block the site raised: later targets fail with it.
    refused: LoginError | Blocked | None = None
    # Set once its login is checked and its `connect` entered: what its other targets wait for.
    started: asyncio.Event = field(default_factory=asyncio.Event)


class _Runner:
    """A command's run: what its targets share, one instance per plugin, and the targets."""

    def __init__(self, engine: Engine, settings: RunShared) -> None:
        self.engine, self.settings = engine, settings
        # Each plugin's instance.
        self._entries: dict[str, _Entry] = {}
        self._active = asyncio.Semaphore(ACTIVE)
        # Each plugin's listings at once.
        self._listing: dict[str, asyncio.Semaphore] = {}
        # The targets the run has started, each run once however often it is named; set
        # once each ends, with its report.
        self._once: dict[Target, asyncio.Event] = {}
        self._reports: dict[Target, TargetReport] = {}
        # Targets a listing of another plugin discovered, handed to the run (`follow`).
        self._handed: list[asyncio.Future[tuple[bool, Any]]] = []
        # Each target's turn at its first step, in the order they started (`crawl.Turn`).
        self._turns: list[asyncio.Event] = []

    async def entry(self, code: str) -> _Entry:
        """The run's instance of the plugin `code`, started on first use.

        Raises:
            UsageError: the home's config.toml has a value the plugin doesn't take.
            LoginError: the login file can't be read.
        """

        entry = self._entries.get(code)
        if entry is not None:
            await entry.started.wait()
            if entry.run is None:
                # Its plugin's run didn't open: each of its targets fails with that.
                assert entry.error is not None
                raise entry.error
            return entry
        source = self.engine.sources[code]()
        stack = contextlib.AsyncExitStack()
        # Kept before the first wait, so a target that comes meanwhile waits for this one.
        entry = self._entries[code] = _Entry(None, stack)  # type: ignore[arg-type]
        try:
            run = entry.run = await stack.enter_async_context(
                ctx.opened(self.settings, source))
        except BaseException as error:
            entry.error = error
            entry.started.set()
            raise
        try:
            # The plugin's state, read once, off the loop, and written back as the run ends.
            await run.off(run.load_state)
            stack.push_async_callback(run.off, run.save_state)
            entry.logged_in = await _logged_in(run)
            if not entry.logged_in and _required(source):
                return entry
            if not entry.logged_in:
                ui.warn("{code}: not logged in, using public access; run flnd auth login {code}",
                        code=code)
            stack.push_async_callback(run.off, auth.write_back, code, run.session.jar)
            await stack.enter_async_context(_connected(run, _plugin_ctx(run)))
            # One cache of the posts the `date` hook had no day for, shared by its targets.
            await run.off(crawl.asked, run)
        except Exception as error:
            entry.error = error
            ui.debug("{source}: {error}", source=code, error=error)
        finally:
            entry.started.set()
        return entry

    async def ready(self, target: Target) -> tuple[_Entry | None, TargetReport | None]:
        """The instance `target` runs on, or the failed report of a target that can't run.

        A target fails here, with a `Failed` event, when its plugin didn't start, needs a
        login it lacks, or had its login refused by an earlier target of the run.
        """

        settings, tally = self.settings, self.settings.tally
        try:
            entry = await self.entry(target.plugin)
        except Exception as error:
            failed = crawl.failure(error, target)
            tally.end(target, failed=True)
            await crawl.tell(target, failed, settings.emit)
            return None, TargetReport(target, (), 0, error=failed)
        failed = None
        if entry.error is not None:
            failed = crawl.failure(entry.error, target, entry.run.source)
        elif not entry.logged_in and _required(entry.run.source):
            failed = crawl.logged_out(target)
        elif entry.refused is not None:
            failed = entry.refused
        if failed is None:
            return entry, None
        if failed is entry.refused:
            return entry, await self._refused(target, failed)
        tally.end(target, failed=True)
        await crawl.tell(target, failed, settings.emit)
        return entry, TargetReport(target, (), 0, error=failed)

    async def _refused(self, target: Target, login: LoginError | Blocked) -> TargetReport:
        """A target whose plugin's login an earlier target had refused: it fails, unasked."""

        self.settings.tally.end(target, failed=True)
        await self.settings.emit(Failed(target, login))
        return TargetReport(target, (), 0, error=login)

    async def follow(self, target: Target, options: Options, *, inside: str) -> None:
        """Run a target a listing of the plugin `inside` discovered.

        One of the same plugin runs inside that listing, which holds its places. One of
        another plugin is handed to the run as a target of its own, which the listing doesn't
        wait for: waiting for that plugin's listing place while holding its own could wait
        forever, for a listing of that plugin waiting the other way.
        """
        if inside == target.plugin:
            await self.target(target, options, inside=inside)
            return
        self.settings.tally.add(target, _planned(options))
        # The target's coroutine is made once its task starts: one cancelled before (a full
        # disk) leaves no coroutine never awaited.
        self._handed.append(asyncio.ensure_future(_carried_call(self.target, target, options)))

    async def target(self, target: Target, options: Options, *,
                     inside: str | None = None,
                     turn: crawl.Turn | None = None) -> TargetReport:
        """Run one target on its plugin's instance, a discovered or linked one included; a
        target named twice in the run runs once, and the second gets the first's report.

        It waits for a place among its plugin's listings, then among the run's active
        targets (`_held`). A target a listing of its own plugin discovered runs inside that
        listing (`inside`), and takes no place of its own (`follow`).

        Args:
            target: the target, resolved.
            options: its options.
            inside: the plugin of the listing it runs inside; None for a target of its own.
            turn: its turn at its route's call (`crawl.Turn`); None takes the next.

        Returns:
            Its report.

        Raises:
            DiskError: its files can't be written; the whole run stops.
        """

        began = turn or self._turn()
        started = self._once.get(target)
        if started is not None:
            began.release()
            await started.wait()
            return self._reports.get(target) or TargetReport(target, (), 0)
        self._once[target] = asyncio.Event()
        try:
            report = self._reports[target] = await self._run(target, options, inside, began)
        except BaseException as error:
            # What stops the run (a full disk) keeps the later targets' routes from running.
            if not jobs.stops(error):
                began.release()
            raise
        finally:
            self._once[target].set()
        began.release()
        return report

    def _turn(self) -> crawl.Turn:
        """The next target's turn at its first step, after every turn given out so far."""

        turn = crawl.Turn(self._turns)
        self._turns.append(turn.passed)
        return turn

    async def _run(self, target: Target, options: Options, inside: str | None,
                   began: crawl.Turn) -> TargetReport:

        entry, failed = await self.ready(target)
        if entry is None or failed is not None:
            assert failed is not None
            return failed
        refused = None
        if inside == target.plugin:
            report = await crawl.download(entry.run, target, self.settings.tally, options,
                                          turn=began)
        else:
            async with self._held(entry, began) as listed:
                # Refused while this target waited for its place: it fails, unasked, once
                # it has given its places back.
                refused = entry.refused
                if refused is None:
                    report = await crawl.download(entry.run, target, self.settings.tally,
                                                  options, listed=listed, turn=began)
        if refused is not None:
            return await self._refused(target, refused)
        if (stopped := _plugin_stop(report.error)) is not None:
            entry.refused = stopped
        # A full or refused disk stops the whole run: every later file would fail too.
        elif isinstance(report.error, DiskError):
            raise report.error
        return report

    @contextlib.asynccontextmanager
    async def _held(self, entry: _Entry,
                    turn: crawl.Turn) -> AsyncIterator[Callable[[Any], Awaitable[None]]]:
        """A target's places: one of its plugin's listings, then one of the run's active ones;
        a target that waits for one lets the next targets have their turns.

        Yields:
            What the crawl awaits once the target's listings ended: it gives the listing's
            place back at once, and the active one when the target has no more files queued
            than its places, so the next target lists while these download.
        """
        source = entry.run.source
        listing = self._listing.get(source.code)
        if listing is None:
            browser = type(source).browser is not Browser.NONE
            listing = self._listing[source.code] = asyncio.Semaphore(
                1 if browser else HTTP_AT_ONCE)
        held = {"listing": False, "active": False}
        if listing.locked():
            turn.release()
        await listing.acquire()
        held["listing"] = True
        try:
            if self._active.locked():
                turn.release()
            await self._active.acquire()
            held["active"] = True

            async def listed(downloads: Any) -> None:
                if held["listing"]:
                    held["listing"] = False
                    listing.release()
                await downloads.below(entry.run.jobs)
                if held["active"]:
                    held["active"] = False
                    self._active.release()

            yield listed
        finally:
            if held["active"]:
                self._active.release()
            if held["listing"]:
                listing.release()

    async def run(self, chosen: Sequence[tuple[Target, Options]], *,
                  depth: int = 0) -> list[TargetReport]:
        """Run the targets at once, as their places allow; then the targets their listings
        found (`found`).

        They start in order: each once the one before it has called its route, so what stops
        the run at a target's first step (a full disk) stops it before the next starts.

        Args:
            chosen: the targets, each with its options.
            depth: how many links deep these targets are; 0 for the run's own.

        Returns:
            One report per target of `chosen`, in order, then one per target found.

        Raises:
            DiskError: a target's files can't be written; the whole run stops.
            KeyboardInterrupt: the second Ctrl-C.
        """
        # A target named twice runs once, with one report.
        once: dict[Target, tuple[Target, Options]] = {}
        for target, options in chosen:
            once.setdefault(target, (target, options))
        chosen = list(once.values())
        for target, options in chosen:
            self.settings.tally.add(target, _planned(options))
        tasks: list[asyncio.Future[tuple[bool, Any]]] = []
        try:
            tasks = [asyncio.ensure_future(_carried(self.target(target, options,
                                                                turn=self._turn())))
                     for target, options in chosen]
            pending = list(tasks)
            while pending:
                for ended in asyncio.as_completed(pending):
                    ok, value = await ended
                    if not ok:
                        # A full disk or a second Ctrl-C stops the run: the targets still
                        # listing or waiting for a place end at once.
                        raise value
                # The targets the listings handed over meanwhile run too, as the run's own.
                pending, self._handed = self._handed, []
                tasks.extend(pending)
        except BaseException:
            # The targets handed over by a listing end with the rest.
            tasks.extend(self._handed)
            self._handed = []
            raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        reports: list[TargetReport] = []
        for task in tasks:
            report = task.result()[1]
            # A handed target that was one of the run's own already has its report.
            if all(report.target != kept.target for kept in reports):
                reports.append(report)
        return reports + await self.found(chosen, reports, depth=depth)

    async def found(self, chosen: Sequence[tuple[Target, Options]],
                    reports: Sequence[TargetReport], *, depth: int) -> list[TargetReport]:
        """Download the targets the run's listings yielded (`TargetReport.found`), once each.

        A post that links to another site hands the link on this way. Each found target is
        resolved by its own source and downloaded with every category, after the run's own
        targets; what those find in turn is followed up to `FOLLOW_DEPTH` links deep, so two
        posts linking to each other can't loop.

        Args:
            chosen: the targets that ran, with their options.
            reports: their reports, in order.
            depth: how many links deep these reports are.

        Returns:
            The found targets' reports, in the order they were found.
        """

        if depth >= FOLLOW_DEPTH or self.settings.stop.soon.is_set():
            return []
        ran = {target for target, _ in chosen}
        found: dict[Target, Options] = {}
        for (target, options), report in zip(chosen, reports):
            for item in report.found:
                try:
                    child = item if item.kind else await asyncio.to_thread(self.engine._one,
                                                                           str(item))
                except Error as error:
                    ui.warn("{target}: can't follow {link}: {error}", target=str(target),
                            link=str(item), error=error)
                    continue
                if child in ran or child in found:
                    continue
                found[child] = Options(since=options.since, until=options.until,
                                       folder=options.folder)
        if not found:
            return []
        ui.info("{targets} linked from the listings", targets=text.count(len(found), "target"))
        await _off(self.engine._record, list(found.items()), follow=False)
        return await self.run(list(found.items()), depth=depth + 1)

    async def close(self) -> None:
        """End the targets handed to the run that no `run` waited for (a search's found
        targets', say), then leave each plugin's `connect` hook, write back its cookies and
        close its run.

        A hook whose exit fails is reported, as a `Failed` event for the plugin:
        the files are saved, but the run isn't all right.
        """

        scheduler = ctx.scheduler(self.settings)
        stopped = scheduler is not None and scheduler.stopped is not None
        while self._handed:
            handed, self._handed = self._handed, []
            if stopped or self.settings.stop.now.is_set():
                # What stopped the run ends them, as it ends the run's own targets.
                for task in handed:
                    task.cancel()
            await asyncio.gather(*handed, return_exceptions=True)
        entries, self._entries = list(self._entries.values()), {}
        for entry in entries:
            try:
                await entry.stack.aclose()
            except Exception as error:
                plugin = Target(f"{entry.run.source.code}:", kind=auth.LOGIN)
                await crawl.tell(plugin, crawl.failure(error, plugin, entry.run.source),
                                 self.settings.emit)


async def _off(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """`fn(*args, **kwargs)` on a thread: the engine's own blocking step (a read or write of
    `flnd.db`, a stat), so the loop never waits for it. It runs on the scheduler's CALL pool
    when one is active, else on asyncio's default executor."""
    scheduler = _SCHEDULER
    if scheduler is not None:
        if kwargs:
            return await scheduler.on(jobs.Kind.CALL, functools.partial(fn, *args, **kwargs))
        return await scheduler.on(jobs.Kind.CALL, fn, *args)
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _carried(awaitable: Awaitable[T]) -> tuple[bool, Any]:
    """`(True, value)`, or `(False, error)`, so a `KeyboardInterrupt` never leaves the loop."""
    try:
        return True, await awaitable
    except BaseException as error:
        return False, error


async def _carried_call(call: Callable[..., Awaitable[T]], *args: Any) -> tuple[bool, Any]:
    """`_carried(call(*args))`, the coroutine made only as this one runs."""
    return await _carried(call(*args))


def _target_status(
    row: store.TargetRow | None,
    target: Target,
    categories: tuple[str, ...],
    complete: Mapping[str, str],
    *,
    pending: tuple[str, ...],
    files: int,
    is_query: bool,
    is_single: bool,
    failure: tuple[bool, int, int],
) -> tuple[str, int]:
    """Summary status and pending failed-file count for a tracked target."""
    whole, failed, exhausted = failure
    if whole:
        return "failed", failed
    if exhausted > 0:
        return "retry", failed
    if failed > 0:
        return "failed", failed
    at = row.last_sync if row is not None else ""
    has_open = row is not None and (bool(row.cursors) or bool(row.gaps))
    started = bool(complete or has_open or files or (row is not None and (row.heads or row.seen)))
    if not started and (not at or not is_single):
        return "new", 0
    if has_open or pending:
        return "partial", 0
    if is_query:
        return ("synced" if target.kind in complete else "partial"), 0
    return "synced", 0


def _planned(options: Options) -> tuple[str, ...]:
    """The categories a target's row shows before its listings start: those it names."""

    names = options.categories or ()
    return () if crawl.ALL in names else tuple(names)


def _plugin_ctx(run: PluginRun) -> Ctx:
    """The `Ctx` of a plugin, not of a target: what `connect` and `check` see."""

    return Ctx(run, Target(f"{run.source.code}:", kind=auth.LOGIN))


async def _logged_in(run: PluginRun) -> bool:
    """Whether the plugin's login is kept; a plugin without a login always is."""
    login = run.source.login
    if login is None:
        return True
    return bool(await login.check(_plugin_ctx(run)))


def _required(source: Plugin) -> bool:
    """Whether the source lists nothing without a login."""
    return source.login is not None and source.login.required


async def _log_dry(
    run: PluginRun, target: Target, options: Options, span: Range, *, is_query: bool,
) -> None:
    """List what a download of `target` would save: a log line and a `Planned` event each."""

    emit = run.settings.emit
    if is_query:
        async with contextlib.aclosing(crawl.discover(run, target, limit=options.limit,
                                                      time_range=span)) as found:
            async for item in found:
                ui.info("{target}", target=str(item))
                await emit(Planned(item, "", None, "new"))
        return
    folder = crawl.target_root(run.settings.home, target, options, run.source)
    db = run.settings.db
    shown: set[str] = set()
    async with contextlib.aclosing(crawl.plan(run, target, options)) as planned:
        async for placed in planned:
            ui.info("{target}: {path}", target=str(target), path=placed.path)
            dest = crawl.checked_dest(folder, placed.path, check=options.folder is not None)
            if placed.post.category not in shown:
                shown.add(placed.post.category)
                ui.info("{target}: {category} goes to {folder}", target=str(target),
                        category=placed.post.category or "(posts)",
                        folder=dest.parent.relative_to(run.settings.home).as_posix())
            saved = await run.off(_planned_saved, db, f"{run.source.code}:{placed.key}", dest)
            await emit(Planned(target, placed.post.id, dest, "saved" if saved else "new"))

def _planned_saved(db: store.Store | None, key: str, dest: Path) -> bool:
    """Whether a planned file is saved: its row or its file is there (off the loop)."""
    return (db is not None and db.known(key) is not None) or dest.is_file()



def _log_totals(report: Results) -> None:
    """Log a command's totals and its exit status at -v, for a log read after the run."""
    ui.debug(
        "totals: {files} written, {skipped} skipped, {failed} of {targets} failed; exit {status}",
        files=report.files,
        skipped=sum(target.skipped for target in report.targets),
        failed=len(report.failed),
        targets=len(report.targets),
        status=0 if report.ok else 1,
    )


def _follow(url: str) -> str:
    """Where a short link leads, from one HEAD request (`net.follow`)."""
    try:
        return net.follow(url)
    except NetworkError as error:
        raise NetworkError(f"can't follow {text.redact(url)}: {error}",
                           retryable=error.retryable) from None


@contextlib.asynccontextmanager
async def _connected(run: PluginRun, plugin: Ctx) -> AsyncIterator[None]:
    """Run the plugin's `connect` hook, an async context manager, around a block, on the run's
    loop. An error in the block is handed to its exit, which may swallow it, as `async with`
    would; an error of the exit itself goes on up, for the run to report."""
    async with run.source.connect(plugin):
        yield
