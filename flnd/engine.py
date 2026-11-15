"""The engine: flnd's commands as methods for CLI or scripts."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit

import httpx

import flnd.query  # noqa: F401
import flnd.targets
from flnd import job, repair
from flnd.common import auth, compress, net, paths
from flnd.common.compress import Compressor
from flnd.job import TargetReport
from flnd.options import Options, RunShared, categories_by_source
from flnd.site import LoginSite, SourceRun, TargetSite, shared_browser, source_run
from flnd.source import Range, Source, Target, TargetError
from flnd.sources import SOURCES
from shared import cli, fs, store, text, ui, worker

__all__ = ["AuthInfo", "Engine", "Error", "Report", "TargetInfo", "TargetReport"]

HISTORY_TABLE = "downloads"
ACCOUNT = "account"
ALL = "all"
MAX_SYNC_NEW = 25


class Error(Exception):
    """Base error for engine operations."""


@dataclass(frozen=True)
class TargetInfo:
    """Saved target status and disk statistics returned by `Engine.target_ls`."""

    target: Target
    sync: bool
    shrink: bool
    at: str
    files: int
    size: int
    seen: int
    categories: dict[str, str]
    query: bool


@dataclass(frozen=True)
class AuthInfo:
    """Saved authentication state for one source returned by `Engine.auth_ls`."""

    code: str
    name: str
    state: str
    origin: str
    expiring: list[tuple[str, float]]


@dataclass(frozen=True)
class Report:
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


@dataclass(frozen=True)
class _Planned:
    """One target in a source's sequential batch."""

    number: int
    target: Target
    options: Options
    tally: repair.Tally | None


class Engine:
    """flnd's commands as methods: `resolve`, `download`, `sync`, `login`, and `logout`."""

    def __init__(
        self,
        *,
        home: Path = Path(".flnd"),
        sources: Mapping[str, type[Source]] = SOURCES,
    ) -> None:
        self.home, self.sources = home, sources

    def resolve(self, texts: Sequence[str]) -> list[Target]:
        """Resolve every target string through its owning source."""
        return [resolve(text_item, self.sources, follow=_follow) for text_item in texts]

    def download(
        self,
        targets: Sequence[Target | str],
        *,
        categories: str | Sequence[str] | None = None,
        time: Range | str = Range(),
        shrink: bool | None = None,
        limit: int | None = None,
        dry: bool = False,
        sync: bool = False,
        again: bool = False,
    ) -> Report:
        """Download each target with its saved or given options and return a `Report`."""
        if limit is not None and limit < 1:
            raise cli.UsageError("--limit must be at least 1")
        span = Range.parse(time) if isinstance(time, str) else time
        resolved = self._resolved(targets)
        cats_map = categories_by_source(categories, resolved, self.sources)
        _validate_targets(self.sources, categories, resolved)
        base = Options(since=span.since, until=span.until, limit=limit, again=again)
        by_source = {
            code: replace(base, categories=cats_map.get(code))
            for code in {t.source for t in resolved}
        }
        with cli.stopping() as stop:
            if dry:
                return self._finish_report(self._dry_run(resolved, by_source, span, stop), stop)
            ui.info("Storage: {path}", path=(self.home / paths.MEDIA).resolve())
            if any(self._is_query(t) for t in resolved):
                reports = self._download_mixed(
                    resolved, by_source, shrink=shrink, span=span, sync=sync, stop=stop
                )
                return self._complete_download(resolved, reports, sync=sync, stop=stop)
            return self._download_normal(resolved, by_source, shrink=shrink, sync=sync, stop=stop)

    def sync(self, targets: Sequence[Target | str] = ()) -> Report:
        """Bring synced targets up to date: repair lost files and fetch new posts."""
        resolved = self._resolved(targets)
        chosen = self._plan(resolved)
        if not chosen:
            ui.info("nothing to sync")
            return Report(())
        _validate_targets(self.sources, None, [t for t, _ in chosen])
        with cli.stopping() as stop, fs.lock(self.home / paths.METADATA / ".lock"):
            results: dict[int, TargetReport] = {}
            pool_settings = RunShared(home=self.home, history={}, stop=stop)
            with shared_browser(pool_settings):
                self._sync_normal(chosen, results, pool_settings, stop)
                self._sync_queries(chosen, results, pool_settings, stop)
            return self._finish_report(Report(tuple(results[i] for i in range(len(chosen)))), stop)

    def target_ls(self) -> list[TargetInfo]:
        """Return status and disk statistics for every known target."""
        rows = flnd.targets.rows(self.home)
        recorded = self._recorded(rows)
        all_targets = recorded + list(self._unrecorded(recorded, rows))
        return [self._target_info(target, rows) for target in all_targets]

    def target_sync(self, targets: Sequence[Target | str], *, enabled: bool) -> list[Target]:
        """Enable or disable `sync` for each known target."""
        resolved = self._resolved(targets)
        rows = flnd.targets.rows(self.home)
        recorded = self._recorded(rows)
        unrecorded = set(self._unrecorded(recorded, rows))
        for target in resolved:
            if flnd.targets.key(target) not in rows and target not in unrecorded:
                raise cli.UsageError(f"{target}: not a known target; download it first")
        for target in resolved:
            flnd.targets.set_sync(self.home, target, enabled=enabled)
        return resolved

    def target_del(self, targets: Sequence[Target | str]) -> list[Target]:
        """Remove each saved target from `targets.db` while keeping downloaded files."""
        resolved = self._resolved(targets)
        rows = flnd.targets.rows(self.home)
        for target in resolved:
            if flnd.targets.key(target) not in rows:
                raise cli.UsageError(f"{target}: not a saved target")
        for target in resolved:
            flnd.targets.delete(self.home, target)
        return resolved

    def auth_ls(self) -> list[AuthInfo]:
        """Return login state, origin, and expiring cookies for each source."""
        items: list[AuthInfo] = []
        for code, source_cls in self.sources.items():
            logged = auth.logged_in(self.home, code)
            api_key = not logged and auth.api_key_saved(self.home, code)
            state = "logged in" if logged else ("api key only" if api_key else "not logged in")
            items.append(AuthInfo(
                code=code,
                name=source_cls.name,
                state=state,
                origin=auth.origin(self.home, code) if logged else "",
                expiring=auth.expiring(self.home, source_cls()) if logged else [],
            ))
        return items

    def login(
        self,
        code: str,
        method: str,
        *,
        browser: str | None = None,
        profile: str | None = None,
        path: Path | str | None = None,
    ) -> str:
        """Log in to a source by one of its `login_methods` and return a status message."""
        if code not in self.sources:
            raise cli.UsageError(f"unknown source '{code}'. Sources: {', '.join(self.sources)}")
        source = self.sources[code]()
        if method not in source.login_methods:
            raise cli.UsageError(
                f"{code} logs in by {', '.join(source.login_methods)}, not {method!r}"
            )
        if code not in self.cookie_logins:
            return self._interactive_login(code, source, method)
        if method == "file":
            if path is None:
                raise cli.UsageError("a file login needs the cookie file's path")
            file = Path(path).expanduser()
            return auth.store(
                source, auth.from_file(source, file), home=self.home, origin=file.name
            )
        if browser is None:
            raise cli.UsageError("a browser login needs the browser to read cookies from")
        jar = auth.from_browser(source, browser=browser, profile=profile)
        return auth.store(source, jar, home=self.home, origin=auth.BROWSERS.get(browser, browser))

    def logout(self, codes: Iterable[str]) -> list[str]:
        """Remove logins for the given source codes (or `all`) and return removed codes."""
        asked = list(dict.fromkeys(codes))
        unknown = [code for code in asked if code != ALL and code not in self.sources]
        if unknown:
            raise cli.UsageError(
                f"unknown source '{unknown[0]}'. Sources: {', '.join(self.sources)}"
            )
        chosen = list(self.sources) if ALL in asked else asked
        return [code for code in chosen if auth.logout(self.home, code)]

    @property
    def logins(self) -> dict[str, str]:
        """Map each source code with saved credentials to its login state."""
        found: dict[str, str] = {}
        for code in self.sources:
            if auth.logged_in(self.home, code):
                found[code] = "logged in"
            elif auth.api_key_saved(self.home, code):
                found[code] = "api key saved"
        return found

    @property
    def browsers(self) -> dict[str, str]:
        """Supported browsers mapping `browser=` key to display label."""
        return dict(auth.BROWSERS)

    @property
    def cookie_logins(self) -> tuple[str, ...]:
        """Source codes that authenticate with browser or file cookies."""
        return tuple(code for code, source in self.sources.items() if source.login is Source.login)

    def _interactive_login(self, code: str, source: Source, method: str) -> str:
        with cli.stopping() as stop:
            settings = RunShared(home=self.home, history={}, stop=stop, interactive=True)
            with source_run(settings, source) as run:
                source.login(LoginSite(run), method)
            if auth.logged_in(self.home, code):
                auth.record_origin(self.home, code, method)
        return ""

    def _download_normal(
        self,
        resolved: Sequence[Target],
        by_source: Mapping[str, Options],
        *,
        shrink: bool | None,
        sync: bool,
        stop: cli.Stopping,
    ) -> Report:
        """Download non-query targets with metadata locking and optional history sweep."""
        with fs.lock(self.home / paths.METADATA / ".lock"):
            rows = flnd.targets.rows(self.home)
            chosen: list[tuple[Target, Options]] = []
            shown: list[str] = []
            turned_on: list[Target] = []
            for target in resolved:
                saved = flnd.targets.shrink(rows.get(flnd.targets.key(target), {}))
                shown.append(flnd.targets.announce(target, saved) if shrink is None else "")
                given = saved if shrink is None else shrink
                chosen.append((target, replace(by_source[target.source], shrink=given)))
                if shrink and not saved:
                    turned_on.append(target)
            flnd.targets.record(
                self.home, [pair for pair in chosen if pair[0] not in turned_on], sync=sync
            )
            if turned_on:
                self._apply(turned_on, dict(chosen), sync=sync)
            reports = self._run(chosen, stop=stop)
            final = [replace(rep, settings=txt) for rep, txt in zip(reports, shown)]
            return self._complete_download(resolved, final, sync=sync, stop=stop, rows=rows)

    def _complete_download(
        self,
        resolved: Sequence[Target],
        reports: Sequence[TargetReport],
        *,
        sync: bool,
        stop: cli.Stopping,
        rows: Mapping[str, Any] | None = None,
    ) -> Report:
        report = self._finish_report(Report(tuple(reports)), stop)
        if not sync:
            self._warn_unsynced(resolved, rows=rows)
        return report

    def _finish_report(self, report: Report, stop: cli.Stopping) -> Report:
        if stop.soon.is_set():
            raise KeyboardInterrupt
        _log_totals(report)
        return report

    def _sync_normal(
        self,
        chosen: Sequence[tuple[Target, Options]],
        results: dict[int, TargetReport],
        pool_settings: RunShared,
        stop: cli.Stopping,
    ) -> None:
        """Sync non-query targets and store their `TargetReport` entries in `results`."""
        indices = [i for i, (target, _) in enumerate(chosen) if not self._is_query(target)]
        if not indices:
            return
        normal = [chosen[i] for i in indices]
        flnd.targets.record(self.home, normal)
        shown = [flnd.targets.announce(target, opts.shrink) for target, opts in normal]
        folders = [paths.target_folder(self.home, target) for target, _ in normal]
        with self._history() as history:
            index = repair.index(history, folders)
        tallies = [
            repair.Tally(str(target), folder, index[folder])
            for (target, _), folder in zip(normal, folders)
        ]
        ui.info("Storage: {path}", path=(self.home / paths.MEDIA).resolve())
        run_items = [(t, replace(opts, stop_at_saved=True)) for t, opts in normal]
        reports = self._run(
            run_items, stop=stop, tallies=tallies, browser_pool=pool_settings.browser_pool
        )
        if stop.soon.is_set():
            raise KeyboardInterrupt
        with self._history() as history:
            for tally in tallies:
                repair.count(history, tally)
        repair.summary(tallies)
        for pos, rep, tally, text in zip(indices, reports, tallies, shown):
            results[pos] = replace(
                rep, repaired=tally.repaired, missing=tally.missing, settings=text
            )

    def _sync_queries(
        self,
        chosen: Sequence[tuple[Target, Options]],
        results: dict[int, TargetReport],
        pool_settings: RunShared,
        stop: cli.Stopping,
    ) -> None:
        """Sync query targets and store their `TargetReport` entries in `results`."""
        indices = [i for i, (target, _) in enumerate(chosen) if self._is_query(target)]
        if not indices:
            return
        query_chosen = [chosen[i] for i in indices]
        flnd.targets.record(self.home, query_chosen)
        for pos, (target, options) in zip(indices, query_chosen):
            results[pos] = self._sync_query(
                target, options, stop=stop, browser_pool=pool_settings.browser_pool
            )
            if stop.soon.is_set():
                raise KeyboardInterrupt

    def _target_info(self, target: Target, rows: Mapping[str, Any]) -> TargetInfo:
        """Build a `TargetInfo` summary for `target` from stored rows and disk stats."""
        row = rows.get(flnd.targets.key(target), {})
        is_query = self._is_query(target)
        files, size = (0, 0) if is_query else _folder_stats(paths.target_folder(self.home, target))
        complete = flnd.targets.listed(rows, target)
        cursors = flnd.targets.cursors(rows, target)
        categories = (
            {}
            if is_query
            else {
                cat: (
                    "complete" if cat in complete
                    else ("interrupted" if cat in cursors else "incomplete")
                )
                for cat in self.sources[target.source].categories
            }
        )
        return TargetInfo(
            target=target,
            sync=flnd.targets.sync_enabled(row),
            shrink=flnd.targets.shrink(row),
            at=str(row.get("at", "")),
            files=files,
            size=size,
            seen=len(flnd.targets.seen(rows, target)) if is_query else 0,
            categories=categories,
            query=is_query,
        )

    def _is_query(self, target: Target) -> bool:
        source = self.sources[target.source]
        route = next((r for r in source.routes if r.kind == target.kind), None)
        return route is not None and route.discovers

    def _dry_run(
        self,
        resolved: Sequence[Target],
        by_source: Mapping[str, Options],
        span: Range,
        stop: cli.Stopping,
    ) -> Report:
        reports: list[TargetReport] = []
        settings = RunShared(home=self.home, history={}, stop=stop, dry=True)
        with shared_browser(settings):
            for target in resolved:
                opts = by_source[target.source]
                reports.append(self._dry_one(settings, target, opts, span, stop))
        return Report(tuple(reports))

    def _dry_one(
        self,
        settings: RunShared,
        target: Target,
        options: Options,
        span: Range,
        stop: cli.Stopping,
    ) -> TargetReport:
        """Run a dry-run plan for a single target."""
        source = self.sources[target.source]()
        with source_run(settings, source) as run:
            account = TargetSite(run, Target(source.code, ACCOUNT, ""))
            if not source.logged_in(account) and source.requires_login:
                ui.error("{target}: not logged in; run flnd auth login", target=str(target))
                return TargetReport(target, (), 0, error="not logged in; run flnd auth login")
            try:
                with _connected(run, account):
                    _log_dry(run, target, options, span, is_query=self._is_query(target))
            except KeyboardInterrupt:
                if not stop.soon.is_set() or stop.now.is_set():
                    raise
                return TargetReport(target, (), 0, error=job.STOPPED)
            except Exception as error:
                ui.error("{target}: {error}", target=str(target), error=error)
                return TargetReport(target, (), 0, error=str(error))
            return TargetReport(target, (), 0)

    def _download_mixed(
        self,
        resolved: Sequence[Target],
        by_source: Mapping[str, Options],
        *,
        shrink: bool | None,
        span: Range,
        sync: bool,
        stop: cli.Stopping,
    ) -> list[TargetReport]:
        """Download a mix of query and non-query targets in caller order."""
        reports: list[TargetReport] = []
        pool_settings = RunShared(home=self.home, history={}, stop=stop)
        with fs.lock(self.home / paths.METADATA / ".lock"), shared_browser(pool_settings):
            for target in resolved:
                options = by_source[target.source]
                if self._is_query(target):
                    reports.extend(self._run_query(
                        target, options, shrink=shrink, span=span, sync=sync, stop=stop,
                        browser_pool=pool_settings.browser_pool,
                    ))
                    continue
                rows = flnd.targets.rows(self.home)
                saved = flnd.targets.shrink(rows.get(flnd.targets.key(target), {}))
                shown = flnd.targets.announce(target, saved) if shrink is None else ""
                chosen_opts = replace(options, shrink=saved if shrink is None else shrink)
                if shrink and not saved:
                    self._apply([target], {target: chosen_opts}, sync=sync)
                else:
                    flnd.targets.record(self.home, [(target, chosen_opts)], sync=sync)
                [rep] = self._run(
                    [(target, chosen_opts)], stop=stop, browser_pool=pool_settings.browser_pool
                )
                reports.append(replace(rep, settings=shown))
        return reports

    def _run_query(
        self,
        target: Target,
        options: Options,
        *,
        shrink: bool | None,
        span: Range,
        sync: bool,
        stop: cli.Stopping,
        browser_pool: Any = None,
    ) -> list[TargetReport]:
        """Discover targets from a query and download each one."""
        source = self.sources[target.source]()
        rows = flnd.targets.rows(self.home)
        saved_shrink = flnd.targets.shrink(rows.get(flnd.targets.key(target), {}))
        query_shrink = saved_shrink if shrink is None else shrink
        flnd.targets.record(self.home, [(target, replace(options, shrink=query_shrink))], sync=sync)
        reports: list[TargetReport] = []
        with self._history() as history, self._targets() as saved_targets, ui.group() as group:
            settings = RunShared(
                home=self.home, history=history, targets=saved_targets,
                stop=stop, browser_pool=browser_pool,
            )
            board = job.Board(
                group, subject_width=len(str(target)),
                label_width=self._label_width([(target, options)]), parent=target,
            )
            with shared_browser(settings), source_run(settings, source) as run:
                if err := _require_auth(source, run, target):
                    return [err]
                self._discover_and_run(
                    run, target, options, board, reports,
                    query_shrink=query_shrink, span=span, stop=stop,
                )
        return reports

    def _discover_and_run(
        self,
        run: SourceRun,
        target: Target,
        options: Options,
        board: job.Board,
        reports: list[TargetReport],
        *,
        query_shrink: bool,
        span: Range,
        stop: cli.Stopping,
    ) -> None:
        """Discover query children, download each, and record the query's seen set."""
        seen_keys: list[str] = []
        account = TargetSite(run, Target(run.source.code, ACCOUNT, ""))
        try:
            with _written_back(run), _connected(run, account):
                with closing(job.discover(
                    run, target, limit=options.limit, time_range=span, seen=()
                )) as found:
                    for child in found:
                        if stop.soon.is_set():
                            break
                        child_opts = replace(
                            options, shrink=query_shrink, limit=None, query=target.value
                        )
                        rep = self._run_child(run, target, child, board, child_opts, stop)
                        if rep.error is None:
                            seen_keys.append(flnd.targets.key(child))
                        reports.append(rep)
        except KeyboardInterrupt:
            if not stop.soon.is_set() or stop.now.is_set():
                raise
            reports.append(TargetReport(target, (), 0, error=job.STOPPED))
        except Exception as error:
            ui.error("{target}: {error}", target=str(target), error=error)
            ui.debug("{target}: {chain}", target=str(target), chain=job.causes(error))
            reports.append(TargetReport(target, (), 0, error=str(error)))
        finally:
            board.end(target, failed=any(r.error is not None for r in reports))
            if seen_keys:
                flnd.targets.mark_seen(self.home, target, seen_keys)

    def _run_child(
        self,
        run: SourceRun,
        parent: Target,
        child: Target,
        board: job.Board,
        child_opts: Options,
        stop: cli.Stopping,
    ) -> TargetReport:
        if child.source == parent.source:
            return job.download(run, child, board, child_opts)
        [rep] = self._run(
            [(child, child_opts)], stop=stop, browser_pool=run.settings.browser_pool
        )
        return rep

    def _sync_query(
        self,
        target: Target,
        options: Options,
        *,
        stop: cli.Stopping,
        browser_pool: Any = None,
    ) -> TargetReport:
        """Run a dry-run plan for a single target."""
        source = self.sources[target.source]()
        rows = flnd.targets.rows(self.home)
        seen_keys = set(flnd.targets.seen(rows, target))
        known_keys = set(rows)
        with self._history() as history, self._targets() as saved_targets, ui.group() as group:
            settings = RunShared(
                home=self.home, history=history, targets=saved_targets,
                stop=stop, browser_pool=browser_pool,
            )
            board = job.Board(
                group, subject_width=len(str(target)),
                label_width=self._label_width([(target, options)]), parent=target,
            )
            with shared_browser(settings), source_run(settings, source) as run:
                if err := _require_auth(source, run, target):
                    return err
                return self._sync_query_run(
                    run, target, options, board, seen_keys, known_keys, stop
                )

    def _sync_query_run(
        self,
        run: SourceRun,
        target: Target,
        options: Options,
        board: job.Board,
        seen_keys: set[str],
        known_keys: set[str],
        stop: cli.Stopping,
    ) -> TargetReport:
        """Run discovery and download for a synced query target."""
        written: list[Path] = []
        skipped, child_failed, seen_to_mark, failed = 0, None, [], False
        try:
            skipped, child_failed = self._sync_query_items(
                run, target, options, board, seen_keys, known_keys, stop, written, seen_to_mark
            )
        except KeyboardInterrupt:
            if not stop.soon.is_set() or stop.now.is_set():
                raise
            failed = True
            return TargetReport(target, tuple(written), skipped, error=job.STOPPED)
        except Exception as error:
            failed = True
            ui.error("{target}: {error}", target=str(target), error=error)
            ui.debug("{target}: {chain}", target=str(target), chain=job.causes(error))
            return TargetReport(target, tuple(written), skipped, error=str(error))
        finally:
            board.end(target, failed=failed or child_failed is not None)
            if seen_to_mark:
                flnd.targets.mark_seen(self.home, target, seen_to_mark)
        return TargetReport(target, tuple(written), skipped, error=child_failed)

    def _sync_query_items(
        self,
        run: SourceRun,
        target: Target,
        options: Options,
        board: job.Board,
        seen_keys: set[str],
        known_keys: set[str],
        stop: cli.Stopping,
        written: list[Path],
        seen_to_mark: list[str],
    ) -> tuple[int, str | None]:
        """Download newly discovered query children and return `(new_count, first_error)`."""
        skipped, new_count, child_failed = 0, 0, None
        account = TargetSite(run, Target(run.source.code, ACCOUNT, ""))
        with _written_back(run), _connected(run, account):
            with closing(job.discover(run, target, time_range=Range(), seen=seen_keys)) as found:
                for child in found:
                    if stop.soon.is_set():
                        break
                    child_key = flnd.targets.key(child)
                    if child_key in seen_keys or child_key in known_keys:
                        seen_to_mark.append(child_key)
                        skipped += 1
                        continue
                    if new_count >= MAX_SYNC_NEW:
                        break
                    new_count += 1
                    child_opts = replace(options, limit=None, query=target.value)
                    rep = self._run_child(run, target, child, board, child_opts, stop)
                    written.extend(rep.written)
                    skipped += rep.skipped
                    if rep.error is None:
                        seen_to_mark.append(child_key)
                    else:
                        child_failed = rep.error
        return skipped, child_failed

    def _warn_unsynced(
        self, resolved: Sequence[Target], *, rows: Mapping[str, Any] | None = None,
    ) -> None:
        saved = flnd.targets.rows(self.home) if rows is None else rows
        if any(
            not flnd.targets.sync_enabled(saved.get(flnd.targets.key(t), {"sync": False}))
            for t in resolved
        ):
            ui.info("not synced; add --sync to keep it up to date")

    def _recorded(self, rows: Mapping[str, Any]) -> list[Target]:
        return [
            Target(code, row["kind"], value, query=self._is_query_kind(code, row["kind"]))
            for key, row in rows.items()
            if ":" in key and isinstance(row, dict) and "kind" in row
            for code, value in [key.split(":", 1)]
            if code in self.sources
        ]

    def _is_query_kind(self, code: str, kind: str) -> bool:
        route = next((r for r in self.sources[code].routes if r.kind == kind), None)
        return route is not None and route.is_query

    def _resolved(self, targets: Sequence[Target | str]) -> list[Target]:
        resolved: list[Target] = []
        for given in targets:
            target = given if isinstance(given, Target) else self.resolve([given])[0]
            ui.debug(
                "resolve {given}: {target} ({kind})", given=given, target=target, kind=target.kind
            )
            resolved.append(target)
        return resolved

    def _plan(self, targets: Sequence[Target]) -> list[tuple[Target, Options]]:
        """Resolve which targets to sync and attach their stored options."""
        rows = flnd.targets.rows(self.home)
        if not targets:
            synced_rows = flnd.targets.synced(rows)
            recorded = self._recorded(rows)
            targets = self._recorded(synced_rows) + list(self._unrecorded(recorded, rows))
        return [
            (
                target,
                Options(
                    shrink=flnd.targets.shrink(row := rows.get(flnd.targets.key(target), {})),
                    query=flnd.targets.query_name(row),
                ),
            )
            for target in targets
        ]

    def _unrecorded(
        self, known: Sequence[Target], rows: Mapping[str, Any] | None = None,
    ) -> Iterator[Target]:
        """Yield targets found on disk under `media/` that are not in `known`."""
        seen = {paths.target_folder(self.home, t) for t in known if not self._is_query(t)}
        media = self.home / paths.MEDIA
        saved_rows = rows or {}
        for target in known:
            if self._is_query(target):
                for child_key in flnd.targets.seen(saved_rows, target):
                    if ":" in child_key:
                        code, val = child_key.split(":", 1)
                        seen.add(paths.target_folder(self.home, Target(code, "", val)))
        for code in self.sources:
            for folder in sorted((media / code).iterdir()) if (media / code).is_dir() else ():
                if folder.is_dir() and folder not in seen:
                    try:
                        yield self.resolve([f"{code}:{folder.name}"])[0]
                    except TargetError as error:
                        ui.debug(
                            "{folder}: not synced: {error}",
                            folder=f"{code}/{folder.name}",
                            error=error,
                        )

    def _apply(
        self, targets: Sequence[Target], chosen: Mapping[Target, Options], *, sync: bool = False,
    ) -> None:
        folders = {target: paths.target_folder(self.home, target) for target in targets}
        compressor = Compressor()
        with self._history() as history:
            index = repair.index(history, list(folders.values()))
            for target, folder in folders.items():
                compress.apply(history, index[folder], compressor, label=str(target))
                flnd.targets.record(self.home, [(target, chosen[target])], sync=sync)

    def _run(
        self,
        chosen: Sequence[tuple[Target, Options]],
        *,
        stop: cli.Stopping,
        tallies: Sequence[repair.Tally] = (),
        browser_pool: Any = None,
    ) -> list[TargetReport]:
        """Group targets by site and run each site in parallel via `worker.Pool`."""
        groups: dict[str, list[int]] = {}
        for number, (target, _) in enumerate(chosen):
            groups.setdefault(target.source, []).append(number)
        reports: dict[int, TargetReport] = {}
        with self._history() as history, self._targets() as saved, ui.group() as rows:
            run_settings = RunShared(
                home=self.home, history=history, targets=saved,
                stop=stop, browser_pool=browser_pool,
            )
            board = job.Board(
                rows,
                subject_width=max((len(str(target)) for target, _ in chosen), default=0),
                label_width=self._label_width(chosen),
            )
            work = [
                (
                    self.sources[code](),
                    [
                        _Planned(number, *chosen[number], tallies[number] if tallies else None)
                        for number in numbers
                    ],
                )
                for code, numbers in groups.items()
            ]
            with shared_browser(run_settings):
                overall = rows.add("Targets", total=len(work))
                with worker.Pool(
                    concurrency=max(len(work), 1), progress=overall.part(), stop=stop.soon
                ) as pool:
                    for item in work:
                        pool.submit(partial(_run_site, run_settings, board, reports), item)
                    for result in pool.as_completed():
                        if not result.ok:
                            _fail_unreached(result.item, result.error, reports, board)
        return [reports[number] for number in range(len(chosen))]

    def _label_width(self, chosen: Sequence[tuple[Target, Options]]) -> int:
        labels = [target.kind for target, _ in chosen]
        for code in {target.source for target, _ in chosen}:
            labels += [
                category.name
                for route in self.sources[code].routes
                for category in route.categories
            ]
        return max(map(len, labels), default=0)

    def _history(self) -> store.Store:
        return store.open(self.home / paths.HISTORY, table=HISTORY_TABLE)

    def _targets(self) -> store.Store:
        return store.open(self.home / paths.TARGETS, table=flnd.targets.TARGETS_TABLE)


def _validate_targets(
    sources: Mapping[str, type[Source]],
    categories: str | Sequence[str] | None,
    resolved: Sequence[Target],
) -> None:
    validate = getattr(flnd.targets, "validate", None)
    if validate is not None:
        validate(sources, categories, resolved)


def _require_auth(source: Source, run: SourceRun, target: Target) -> TargetReport | None:
    account = TargetSite(run, Target(source.code, ACCOUNT, ""))
    if not source.logged_in(account) and source.requires_login:
        ui.error("{target}: not logged in; run flnd auth login", target=str(target))
        return TargetReport(target, (), 0, error="not logged in; run flnd auth login")
    if not source.logged_in(account):
        ui.warn("{code}: not logged in, using public access; run flnd auth login", code=source.code)
    return None


def _log_dry(
    run: SourceRun, target: Target, options: Options, span: Range, *, is_query: bool,
) -> None:
    if is_query:
        with closing(job.discover(run, target, limit=options.limit, time_range=span)) as found:
            for item in found:
                ui.info("{target}", target=str(item))
        return
    for placed in job.plan(run, target, options):
        ui.info("{target}: {path}", target=str(target), path=placed.path)


def _folder_stats(folder: Path) -> tuple[int, int]:
    if not folder.is_dir():
        return 0, 0
    files, size = 0, 0
    for path in folder.rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            files += 1
            size += path.stat().st_size
    return files, size


def _log_totals(report: Report) -> None:
    ui.debug(
        "totals: {files} written, {skipped} skipped, {failed} of {targets} failed; exit {status}",
        files=report.files,
        skipped=sum(target.skipped for target in report.targets),
        failed=len(report.failed),
        targets=len(report.targets),
        status=0 if report.ok else 1,
    )


def resolve(
    target_text: str,
    sources: Mapping[str, type[Source]],
    *,
    follow: Callable[[str], str] | None = None,
) -> Target:
    """Read one target string and return it resolved by its source."""
    cleaned = target_text.strip()
    if not cleaned:
        raise TargetError("a target can't be empty")
    source, body = _split_target(cleaned, sources)
    try:
        return source.resolve(body)
    except TargetError as miss:
        if follow is None or miss.url is None:
            raise
        followed = follow(miss.url)
        if not followed or followed == miss.url:
            raise
        redirected = owner(followed, sources)
        return (redirected() if redirected is not None else source).resolve(followed)


def _split_target(cleaned: str, sources: Mapping[str, type[Source]]) -> tuple[Source, str]:
    """Resolve `cleaned` into `(source_instance, target_text)`."""
    if _is_url(cleaned):
        return _owner_source(cleaned, sources), cleaned
    qmark, colon = cleaned.find("?"), cleaned.find(":")
    if qmark != -1 and (colon == -1 or qmark < colon) and "/" not in cleaned[:qmark]:
        code = cleaned[:qmark].strip().lower()
        if code not in sources:
            raise TargetError(f"unknown source '{code}'. Sources: {', '.join(sources)}")
        return sources[code](), f"?{cleaned[qmark + 1:].strip()}"
    if colon != -1:
        code, value = cleaned[:colon].strip().lower(), cleaned[colon + 1:]
        if code not in sources:
            raise TargetError(f"unknown source '{code}'. Sources: {', '.join(sources)}")
        src, body = sources[code](), value.strip()
        if not body:
            forms = ", ".join(src.forms)
            raise TargetError(f"{src.code}: can't read {value!r}; write it like {forms}")
        return src, body
    return _owner_source(cleaned, sources), cleaned


def _owner_source(cleaned: str, sources: Mapping[str, type[Source]]) -> Source:
    found = owner(cleaned, sources)
    if found is None:
        choices = ", ".join(f"{code}:{cleaned}" for code in sources)
        raise TargetError(f'which source is "{cleaned}"? Write it as {choices}')
    return found()


def owner(target_text: str, sources: Mapping[str, type[Source]]) -> type[Source] | None:
    """Return the source whose domains `target_text` belongs to, or None."""
    return next((source for source in sources.values() if _on(target_text, source.domains)), None)


def _on(target_text: str, domains: Sequence[str]) -> bool:
    if not domains or ("/" not in target_text and not _is_url(target_text)):
        return False
    url = target_text if _is_url(target_text) else f"https://{target_text}"
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _is_url(target_text: str) -> bool:
    return target_text.lower().startswith(("http://", "https://"))


def _follow(url: str) -> str:
    try:
        return net.follow(url)
    except httpx.HTTPError as error:
        raise TargetError(f"can't follow {url}: {error}") from None


def _run_site(
    run_settings: RunShared,
    board: job.Board,
    reports: dict[int, TargetReport],
    work: tuple[Source, list[_Planned]],
) -> None:
    """Execute all planned targets for one source site and record their reports."""
    source, group = work
    with source_run(run_settings, source) as run:
        account = TargetSite(run, Target(source.code, ACCOUNT, ""))
        if not source.logged_in(account) and source.requires_login:
            for planned in group:
                ui.error(
                    "{target}: not logged in; run flnd auth login", target=str(planned.target)
                )
                reports[planned.number] = TargetReport(
                    planned.target, (), 0, error="not logged in; run flnd auth login"
                )
            return
        if not source.logged_in(account):
            ui.warn(
                "{code}: not logged in, using public access; run flnd auth login", code=source.code
            )
        with _written_back(run), _connected(run, account):
            for planned in group:
                options = _repaired(run_settings, planned)
                reports[planned.number] = job.download(run, planned.target, board, options)


def _repaired(run_settings: RunShared, planned: _Planned) -> Options:
    tally = planned.tally
    if tally is None or run_settings.stop.soon.is_set():
        return planned.options
    ui.debug("{target}: repair", target=tally.target)
    repair.repair(run_settings.history, tally)
    return replace(
        planned.options,
        stop_at_saved=planned.options.stop_at_saved and not tally.lost,
        again=planned.options.again or bool(tally.lost),
    )


def _fail_unreached(
    work: tuple[Source, list[_Planned]],
    error: BaseException | None,
    reports: dict[int, TargetReport],
    board: job.Board,
) -> None:
    """Execute all planned targets for one source site and record their reports."""
    source, group = work
    ui.error("{source}: {error}", source=source.code, error=error)
    for planned in group:
        board.end(planned.target, failed=True)
        if planned.number not in reports:
            reports[planned.number] = TargetReport(planned.target, (), 0, error=str(error))


@contextmanager
def _written_back(run: SourceRun) -> Iterator[None]:
    try:
        yield
    finally:
        auth.write_back(run.settings.home, run.source.code, run.web.raw.cookies.jar)


@contextmanager
def _connected(run: SourceRun, account: Any) -> Iterator[None]:
    manager = run.source.connect(account)
    if not hasattr(manager, "__aenter__"):
        with manager as client:
            run.client = client
            yield
        return
    run.client = run.run_async(manager.__aenter__())
    try:
        yield
    except BaseException as error:
        if not run.run_async(manager.__aexit__(type(error), error, error.__traceback__)):
            raise
    else:
        run.run_async(manager.__aexit__(None, None, None))
