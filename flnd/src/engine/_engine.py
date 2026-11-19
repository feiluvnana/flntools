"""`Engine`: every flnd command as a method, run on one loop, with the run's instance of each
plugin and account. src/docs/architecture explains it.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import ChainMap
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
from flnd.errors import DiskError, Error, LoginError, NetworkError, PluginError, Stopped, UsageError
from flnd.plugin import Account, Browser, Plugin, Range, Target
from flnd.ui.log import ui
from flnkit import net, text

__all__ = ["Chosen", "Engine", "Results", "TargetInfo"]

T = TypeVar("T")

# How many links deep the targets that listings yield are followed (`Engine._found`).
FOLLOW_DEPTH = 2
# Targets a search's sync downloads at most per run, among those it hadn't seen: a search that
# suddenly finds hundreds (a popular tag) is taken a slice per sync, never all at once.
MAX_SYNC_NEW = 25
# Targets active at once in a run (listing, or with more files queued than their places), and
# targets listing at once per plugin and account: one for a plugin that reads in the browser,
# whose site sees one person, and `HTTP_AT_ONCE` for one that only sends requests.
ACTIVE = jobs.LISTINGS
HTTP_AT_ONCE = 4


class Chosen(NamedTuple):
    """A target as a run takes it: resolved, with the categories it runs."""

    target: Target
    # The categories named in its text (`/reels`), else the stored ones; None takes the
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
    account: str | None


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
        # What resolving reads of each plugin known so far, by code (`manifest`).
        self._entries: dict[str, Mapping[str, Any]] | None = None
        self._installed: dict[str, Mapping[str, Any]] = {}
        self._manifest: flnd.engine.resolve.Manifest | None = None

    def resolve(self, texts: Sequence[str]) -> list[Target]:
        """Resolve every target's text (`chosen`), leaving out the categories it names.

        Raises:
            UsageError: a text names no known plugin, or no route reads it.
            NetworkError: a short link couldn't be followed.
        """
        return [found.target for found in self.chosen(texts)]

    def chosen(self, targets: Sequence[Target | str | Chosen]) -> list[Chosen]:
        """Each target resolved, with its categories: those its text names (`/reels,posts`,
        `/all`), else the stored ones of a known target, else None for the route's own.

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
        """What resolving reads of the plugins known so far: each built-in's manifest entry,
        which imports none of them, then each other plugin's, from `plugins.json` or its class."""
        if self._entries is None:
            self._entries = {}
            maps = self.sources.maps if isinstance(self.sources, ChainMap) else [self.sources]
            for found in reversed(maps):
                if isinstance(found, plugins.Registry):
                    self._entries |= found.manifest()
                else:
                    self._entries |= {code: flnd.engine.resolve.entry(cls)
                                      for code, cls in found.items()}
        if self._manifest is None:
            installed = {"plugins": self._installed} if self._installed else None
            self._manifest = flnd.engine.resolve.Manifest(self._entries, installed)
        return self._manifest

    def _widen(self, code: str | None = None) -> bool:
        """Add one installed plugin, or every installed plugin, to `manifest`, without importing it.

        Args:
            code: the plugin's code; None for every installed plugin from `plugins.json`.
        """
        known = self.manifest.plugins
        if code is not None and code in known:
            return False
        maps = self.sources.maps if isinstance(self.sources, ChainMap) else [self.sources]
        added_entries: dict[str, Mapping[str, Any]] = {}
        added_installed: dict[str, Mapping[str, Any]] = {}
        for found in reversed(maps):
            if isinstance(found, plugins.Registry):
                for name, entry in found.installed(code).items():
                    if name not in known and name not in self._installed:
                        added_installed[name] = entry
            else:
                codes = [code] if code is not None else list(found)
                for name in codes:
                    if name not in known and name in found:
                        added_entries[name] = flnd.engine.resolve.entry(found[name])
        if added_entries or added_installed:
            self._entries = {**(self._entries or {}), **added_entries}
            self._installed.update(added_installed)
            self._manifest = None
        return bool(added_entries or added_installed)

    def _read(self, text: str, stored: Mapping[str, Sequence[str]]) -> Chosen:
        """One target's text, resolved (`flnd.engine.resolve`), with its categories."""
        code = re.match(r"\s*([A-Za-z][A-Za-z0-9]*)\s*[:?]", text)
        linked = text.strip().lower().startswith(("http://", "https://"))
        if code is not None and not linked:
            self._widen(code[1].lower())
        try:
            found = flnd.engine.resolve.resolve(text, manifest=self.manifest, known=stored,
                                                follow=_follow)
        except UsageError:
            if (code is not None and not linked) or not self._widen():
                raise
            found = flnd.engine.resolve.resolve(text, manifest=self.manifest, known=stored,
                                                follow=_follow)
        if found.code == flnd.engine.resolve.WEB and not text.strip().lower().startswith("web:"):
            if self._widen():
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
    ) -> Results:
        """Download each target with its saved or given options and return a `Results`.

        Args:
            targets: the targets, as text (`ins:example_user/reels`) or resolved.
            time: `--when`, as a `Range`; None for no limit.
            limit: at most this many posts per target, or targets per search; None for all.
            dry: plan what would be saved (`Planned` events), and write nothing.
            sync: follow the targets, so `sync` takes them later.
            again: ignore saved cursors and the stop at saved posts.
            keep: whether to save each target's categories, for later syncs (`follow`).

        Returns:
            One `TargetReport` per target, in order.

        Raises:
            UsageError: a bad target, category or limit, before anything is fetched.
            DiskError: the home can't be written; the run stopped.
            KeyboardInterrupt: Ctrl-C (or the run's stop) ended it.
        """

        if limit is not None and limit < 1:
            raise UsageError("--limit must be at least 1")
        span = time or Range()
        # On a thread: a short link is followed with a request of its own.
        resolved = await asyncio.to_thread(self.chosen, targets)
        base = Options(
            since=span.since,
            until=span.until,
            limit=limit,
            again=again,
            stop_at_saved=sync and not again,
        )
        chosen = [(found.target, replace(base, categories=found.categories,
                                         link=found.link or None))
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
            return [self._target_info(target, rows, db) for target in self._recorded(rows)
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
        rows = await _off(self._rows)
        kept = []
        for target, options in chosen:
            # A followed target stops at its saved posts, unless told to list them again.
            row = rows.get(store.key(target))
            followed = row is not None and row.follow and not options.again
            kept.append((target, replace(
                options, stop_at_saved=options.stop_at_saved or followed)))
        await _off(self._record, kept, follow=sync, keep=keep)
        if not any(self._is_query(target) for target, _ in kept):
            reports = await runner.run(kept)
        else:
            reports = []
            for target, options in kept:
                if self._is_query(target):
                    reports.extend(await self._run_query(runner, target, options, span=span,
                                                         stop=stop))
                else:
                    reports.extend(await runner.run([(target, options)]))
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
        self, target: Target, rows: Mapping[str, store.TargetRow], db: store.Store,
    ) -> TargetInfo:
        """A target's state and its saved files' count and bytes, from `flnd.db` alone."""
        row = rows.get(store.key(target))
        is_query = self._is_query(target)
        if is_query:
            files, size = 0, 0
        elif row is not None:
            files, size = row.files, row.bytes
        else:
            files, size = db.count(store.key(target))
        kept = row.categories if row is not None else ()
        categories = () if is_query else kept or tuple(self.sources[target.plugin].categories)
        return TargetInfo(
            target=target,
            follow=row.follow if row is not None else False,
            at=row.last_sync if row is not None else "",
            files=files,
            size=size,
            seen=len(row.seen) if is_query and row is not None else 0,
            categories=categories,
            complete=dict(row.complete) if row is not None else {},
            query=is_query,
            account=row.account if row is not None else None,
        )

    def _route(self, target: Target) -> Any:
        """The route that reads `target` (its manifest entry or its class's), or None."""
        plugin = self.manifest.plugins.get(target.plugin)
        if plugin is None and self._widen(target.plugin):
            plugin = self.manifest.plugins.get(target.plugin)
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
        """List a search's targets and download each as it comes, then keep what it saw."""

        seen_keys: list[str] = []
        try:
            async with contextlib.aclosing(crawl.discover(run, target, limit=options.limit,
                                                          time_range=span)) as found:
                async for child in found:
                    if stop.soon.is_set():
                        break
                    child_opts = replace(options, limit=None, query=target.value, link=None)
                    rep = await runner.target(child, child_opts, parent=target)
                    if rep.error is None:
                        seen_keys.append(store.key(child))
                    reports.append(rep)
        except KeyboardInterrupt:
            if not stop.soon.is_set() or stop.now.is_set():
                raise
            reports.append(TargetReport(target, (), 0, error=Stopped(crawl.STOPPED)))
        except Exception as error:
            failed = crawl.failure(error, target, run.source)
            await crawl.tell(target, failed, self.emit)
            reports.append(TargetReport(target, (), 0, error=failed))
        finally:
            self.tally.end(target, failed=any(r.error is not None for r in reports))
            if seen_keys and run.settings.db is not None:
                await run.off(run.settings.db.seen, target, seen_keys)

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
        seen_keys = set(row.seen if row is not None else ())
        known_keys = set(rows)
        self.tally.add(target, _planned(options))
        entry, failed = await runner.ready(target)
        if entry is None or failed is not None:
            assert failed is not None
            return failed
        return await self._sync_query_run(runner, entry.run, target, options, seen_keys,
                                          known_keys, stop)

    async def _sync_query_run(
        self,
        runner: _Runner,
        run: PluginRun,
        target: Target,
        options: Options,
        seen_keys: set[str],
        known_keys: set[str],
        stop: Stop,
    ) -> TargetReport:
        """Run a search's sync, and keep the keys it saw, however it ends."""

        written: list[Path] = []
        skipped, child_failed, seen_to_mark, failed = 0, None, [], False
        try:
            skipped, child_failed = await self._sync_query_items(
                runner, run, target, options, seen_keys, known_keys, stop, written,
                seen_to_mark)
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
            self.tally.end(target, failed=failed or child_failed is not None)
            if seen_to_mark and run.settings.db is not None:
                await run.off(run.settings.db.seen, target, seen_to_mark)
        return TargetReport(target, tuple(written), skipped, error=child_failed)

    async def _sync_query_items(
        self,
        runner: _Runner,
        run: PluginRun,
        target: Target,
        options: Options,
        seen_keys: set[str],
        known_keys: set[str],
        stop: Stop,
        written: list[Path],
        seen_to_mark: list[str],
    ) -> tuple[int, Error | Stopped | None]:
        """List a search's targets and download the new ones, at most `MAX_SYNC_NEW` of them."""

        skipped, new_count, child_failed = 0, 0, None
        async with contextlib.aclosing(crawl.discover(run, target, time_range=Range())) as found:
            async for child in found:
                if stop.soon.is_set():
                    break
                child_key = store.key(child)
                if child_key in seen_keys or child_key in known_keys:
                    # A target seen before is passed over: `skipped` counts files alone.
                    seen_to_mark.append(child_key)
                    continue
                if new_count >= MAX_SYNC_NEW:
                    break
                new_count += 1
                child_opts = replace(options, limit=None, query=target.value, link=None)
                rep = await runner.target(child, child_opts, parent=target)
                written.extend(rep.written)
                skipped += rep.skipped
                if rep.error is None:
                    seen_to_mark.append(child_key)
                else:
                    child_failed = rep.error
        return skipped, child_failed

    @contextlib.asynccontextmanager
    async def _live(self) -> AsyncIterator[None]:
        """Count one command afresh, publishing its snapshots while it runs (`run.state`)."""
        self.tally = Tally(self._shown)
        async with self.tally.publishing():
            yield

    @contextlib.asynccontextmanager
    async def _running(self, stop: Stop, *, dry: bool = False) -> AsyncIterator[_Runner]:
        """A command's run: the home's database, the browser, the save and the plugin instances."""

        # The home's settings are read once, off the loop: its browser, its save and each
        # plugin's run take theirs from them, and a file's warnings are said once.
        found = await _off(config.settings, self.home, plugins=self.sources)
        db = await _off(store.open, self.home)
        try:
            settings = RunShared(home=self.home, db=db, run=None if dry else self.run,
                                 stop=stop, dry=dry, emit=self.emit, resolve=self._one,
                                 tally=self.tally, config=found)
            settings.claims = crawl.Claims()
            runner = _Runner(self, settings)
            runner.worked = await _off(_worked, db)
            settings.follow, settings.peer = runner.follow, runner.peer
            async with shared_browser(settings), save.attached(settings):
                scheduler = ctx.scheduler(settings)
                assert scheduler is not None
                runner.switcher = scheduler.switch = auth.Switcher(
                    scheduler, lambda code: found.plugin(code).switch, runner.listed,
                    shown=self.tally.switched)
                settings.switch = scheduler.switch
                try:
                    yield runner
                finally:
                    await runner.close()
        finally:
            await _off(db.close)

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
        with store.open(self.home) as db:
            return db.targets()

    def _record(self, chosen: Sequence[tuple[Target, Options]], *, follow: bool | None = None,
                keep: bool = False) -> None:
        """Remember the targets of a run in `flnd.db`, in one transaction."""
        if not chosen:
            return
        with store.open(self.home) as db, db.transaction():
            for target, options in chosen:
                db.record(target, query=options.query,
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
        self.run = await _off(self._started, named if len(targets) <= 3 else f"{named} …")
        try:
            yield
        finally:
            run, self.run = self.run, None
            # The command's tally: `_live` made it before, or inside, this block.
            await _off(self._ended, run, self.tally.saved)

    def _started(self, named: str) -> int:
        """Open a command's `runs` row, after sweeping the month-old logs (src/docs/storage)."""
        store.sweep(self.home)
        with store.open(self.home) as db:
            return db.start(named)

    def _ended(self, run: int | None, saved: int) -> None:
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
                                            query=row.query if row else None)))
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
    # A login the site refused: its later targets would hit the same wall, so they fail with it.
    refused: LoginError | None = None
    # Set once its login is checked and its `connect` entered: what its other targets wait for.
    started: asyncio.Event = field(default_factory=asyncio.Event)


class _Runner:
    """A command's run: what its targets share, one instance per plugin, and the targets."""

    def __init__(self, engine: Engine, settings: RunShared) -> None:
        self.engine, self.settings = engine, settings
        # Each (plugin, account)'s instance.
        self._entries: dict[tuple[str, str | None], _Entry] = {}
        # Each plugin's accounts, read once; and the account that worked for each target, by
        # key, read as the run starts (`Engine._running`).
        self._accounts: dict[str, asyncio.Future[list[Account]]] = {}
        self.worked: dict[str, str] = {}
        # The run's switcher, whose holds and expiries `close` writes; None until it runs.
        self.switcher: auth.Switcher | None = None
        self._active = asyncio.Semaphore(ACTIVE)
        # Each plugin's and account's listings at once.
        self._listing: dict[tuple[str, str | None], asyncio.Semaphore] = {}
        # The targets the run has started, each run once however often it is named; set
        # once each ends, with its report.
        self._once: dict[Target, asyncio.Event] = {}
        self._reports: dict[Target, TargetReport] = {}
        # Targets a listing of another plugin discovered, handed to the run (`follow`).
        self._handed: list[asyncio.Future[tuple[bool, Any]]] = []
        # Each target's turn at its first step, in the order they started (`crawl.Turn`).
        self._turns: list[asyncio.Event] = []

    async def entry(self, code: str, account: Account | None) -> _Entry:
        """The run's instance of the plugin `code` for `account`, started on first use.

        Raises:
            UsageError: the home's config.toml has a value the plugin doesn't take.
            LoginError: the vault's key is gone.
        """

        key = (code, account.name if account is not None else None)
        entry = self._entries.get(key)
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
        entry = self._entries[key] = _Entry(None, stack)  # type: ignore[arg-type]
        try:
            run = entry.run = await stack.enter_async_context(
                ctx.opened(self.settings, source, account))
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
            if account is not None:
                stack.push_async_callback(run.off, auth.write_back, run.settings.home, account,
                                          run.session.jar)
            await stack.enter_async_context(_connected(run, _account(run)))
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
            entry = await self.entry(target.plugin, await self._signed(target))
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

    async def _signed(self, target: Target) -> Account | None:
        """The account `target` runs as: the one that worked for it last, else the plugin's
        first usable one (`auth.pick`)."""
        db = self.settings.db
        if db is None:
            return None
        found = self._accounts.get(target.plugin)
        if found is None:
            found = self._accounts[target.plugin] = asyncio.ensure_future(
                _off(_accounts, db, target.plugin))
        return auth.pick(await found, preferred=self.worked.get(store.key(target)))

    def listed(self, code: str) -> list[Account]:
        """A plugin's accounts, once read for one of its targets; none before."""
        found = self._accounts.get(code)
        return found.result() if found is not None and found.done() else []

    async def peer(self, code: str, name: str) -> PluginRun:
        """The plugin's instance for its account `name`, which a job moved to it runs on.

        Raises:
            Error: what broke that instance as it started.
        """
        account = next(found for found in self.listed(code) if found.name == name)
        entry = await self.entry(code, account)
        if entry.error is not None:
            raise entry.error
        return entry.run

    async def _refused(self, target: Target, login: LoginError) -> TargetReport:
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
                     parent: Target | None = None, inside: str | None = None,
                     turn: crawl.Turn | None = None) -> TargetReport:
        """Run one target on its plugin's instance, a discovered or linked one included; a
        target named twice in the run runs once, and the second gets the first's report.

        It waits for a place among its plugin's listings, then among the run's active
        targets (`_held`). A target a listing of its own plugin discovered runs inside that
        listing (`inside`), and takes no place of its own (`follow`).

        Args:
            target: the target, resolved.
            options: its options.
            parent: the search that found it; a target of the search's own plugin counts on
                the search's row of the run's snapshot.
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
            report = self._reports[target] = await self._run(target, options, parent, inside,
                                                             began)
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

    async def _run(self, target: Target, options: Options, parent: Target | None,
                   inside: str | None, began: crawl.Turn) -> TargetReport:

        if parent is not None and parent.plugin == target.plugin:
            self.settings.tally.adopt(target, parent)
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
        if isinstance(report.error, LoginError):
            entry.refused = report.error
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
        source, account = entry.run.source, entry.run.account
        key = (source.code, account.name if account is not None else None)
        listing = self._listing.get(key)
        if listing is None:
            browser = type(source).browser is not Browser.NONE
            listing = self._listing[key] = asyncio.Semaphore(1 if browser else HTTP_AT_ONCE)
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
                found[child] = Options(since=options.since, until=options.until)
        if not found:
            return []
        ui.info("{targets} linked from the listings", targets=text.count(len(found), "target"))
        await _off(self.engine._record, list(found.items()), follow=False)
        return await self.run(list(found.items()), depth=depth + 1)

    async def close(self) -> None:
        """End the targets handed to the run that no `run` waited for (a search's found
        targets', say), then leave each plugin's `connect` hook, write back its cookies and
        close its run.

        A hook whose exit fails is reported, as a `Failed` event for the plugin's account:
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
                account = Target(f"{entry.run.source.code}:", kind=auth.ACCOUNT)
                await crawl.tell(account, crawl.failure(error, account, entry.run.source),
                                 self.settings.emit)
        db = self.settings.db
        if self.switcher is not None and scheduler is not None and db is not None \
                and not self.settings.dry:
            await _off(auth.settle, db, self.switcher, scheduler.expired)


async def _off(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """`fn(*args, **kwargs)` on a thread: the engine's own blocking step (a read or write of
    `flnd.db`, a stat), so the loop never waits for it. The thread is the loop's default
    executor's, which the run shuts down at its end."""
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


def _accounts(db: store.Store, plugin: str) -> list[Account]:
    return [auth.account(row) for row in db.accounts(plugin)]


def _worked(db: store.Store) -> dict[str, str]:
    """The account that worked for each target, by key."""
    return {item: row.account for item, row in db.targets().items() if row.account}


def _planned(options: Options) -> tuple[str, ...]:
    """The categories a target's row shows before its listings start: those it names."""

    names = options.categories or ()
    return () if crawl.ALL in names else tuple(names)


def _account(run: PluginRun) -> Ctx:
    """The `Ctx` of a plugin's account, not of a target: what `connect` and `check` see."""

    return Ctx(run, Target(f"{run.source.code}:", kind=auth.ACCOUNT))


async def _logged_in(run: PluginRun) -> bool:
    """Whether the plugin's login is kept; a plugin without a login always is."""
    login = run.source.login
    if login is None:
        return True
    return bool(await login.check(_account(run)))


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
    folder = store.target_folder(run.settings.home, target)
    db = run.settings.db
    async with contextlib.aclosing(crawl.plan(run, target, options)) as planned:
        async for placed in planned:
            ui.info("{target}: {path}", target=str(target), path=placed.path)
            dest = folder.joinpath(*placed.path.split("/"))
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
async def _connected(run: PluginRun, account: Ctx) -> AsyncIterator[None]:
    """Run the plugin's `connect` hook, an async context manager, around a block, on the run's
    loop. An error in the block is handed to its exit, which may swallow it, as `async with`
    would; an error of the exit itself goes on up, for the run to report."""
    async with run.source.connect(account):
        yield
