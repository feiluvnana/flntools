"""The engine: flnd's commands as methods for CLI or scripts."""

from __future__ import annotations

from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

import flnd.targets
from flnd import job
from flnd.common import auth, net, paths
from flnd.job import TargetReport
from flnd.options import Options, RunShared, categories_by_source
from flnd.site import LoginSite, SourceRun, TargetSite, shared_browser, source_run
from flnd.errors import DiskError, Error, LoginError, NetworkError, Stopped, UsageError
from flnd.source import Range, Source, Target, _Unmatched
from flnd.sources import SOURCES
from flnd import _db, _text
from flnd._run import Stop, stopping
from flnd._events import Failed, Planned
from flnd import _steps
from flnd._ui import ui

__all__ = ["AuthInfo", "Engine", "Report", "TargetInfo", "TargetReport"]

HISTORY_TABLE = "downloads"
ACCOUNT = "account"
ALL = "all"
MAX_SYNC_NEW = 25


@dataclass(frozen=True)
class TargetInfo:
    """Saved target status and disk statistics returned by `Engine.target_ls`."""

    target: Target
    sync: bool
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


class Engine:
    """flnd's commands as methods: `resolve`, `download`, `sync`, `login`, and `logout`."""

    def __init__(
        self,
        *,
        home: Path = Path(".flnd"),
        sources: Mapping[str, type[Source]] = SOURCES,
        emit: Callable[[Any], None] | None = None,
        stop: Stop | None = None,
    ) -> None:
        """Make the engine of one home.

        Args:
            home: the home folder.
            sources: the sources it knows, by code.
            emit: where its runs send their events (`flnd._run.Run._emit`); None drops them.
            stop: the stop state its runs watch, when the caller owns it (a library run, whose
                `stop()` sets it); None makes one per command, set by Ctrl-C.
        """
        self.home, self.sources = home, sources
        self.emit: Callable[[Any], None] = emit or (lambda event: None)
        self.stop = stop

    def resolve(self, texts: Sequence[str]) -> list[Target]:
        """Resolve every target string through its owning source."""
        return [resolve(text_item, self.sources, follow=_follow) for text_item in texts]

    def download(
        self,
        targets: Sequence[Target | str],
        *,
        categories: str | Sequence[str] | None = None,
        time: Range | str | None = None,
        limit: int | None = None,
        dry: bool = False,
        sync: bool = False,
        again: bool = False,
        every: bool = False,
        keep: bool = False,
        compress: bool = False,
    ) -> Report:
        """Download each target with its saved or given options and return a `Report`.

        Args:
            targets: the targets, as text or resolved.
            categories: `--from`: the categories to take; None for none named.
            time: `--time`, as text or a `Range`; None for no limit.
            limit: at most this many posts per target, or targets per search; None for all.
            dry: plan what would be saved (`Planned` events), and write nothing.
            sync: follow the targets, so `sync` takes them later.
            again: ignore saved cursors and the stop at saved posts.
            every: whether no `categories` means every category (the library's `only=None`)
                rather than a usage error for a profile (today's CLI).
            keep: whether to save `categories` with each target, for later syncs (`follow`).
            compress: shrink each saved photo and video once it lands, and remove an archive
                once it is extracted (docs/media).

        Returns:
            One `TargetReport` per target, in order.

        Raises:
            UsageError: a bad target, category, time or limit, before anything is fetched.
            DiskError: the home can't be written; the run stopped.
            KeyboardInterrupt: Ctrl-C (or the run's stop) ended it.
        """
        if limit is not None and limit < 1:
            raise UsageError("--limit must be at least 1")
        span = Range.parse(time) if isinstance(time, str) else (time or Range())
        resolved = self._resolved(targets)
        cats_map = categories_by_source(categories, resolved, self.sources, every=every)
        _validate_targets(self.sources, categories, resolved)
        base = Options(
            since=span.since,
            until=span.until,
            limit=limit,
            again=again,
            stop_at_saved=sync and not again,
            compress=compress,
        )
        by_source = {
            code: replace(base, categories=cats_map.get(code))
            for code in {t.source for t in resolved}
        }
        with stopping(self.stop) as stop:
            if dry:
                return self._finish_report(self._dry_run(resolved, by_source, span, stop), stop)
            ui.info("Storage: {path}", path=(self.home / paths.MEDIA).resolve())
            if any(self._is_query(t) for t in resolved):
                reports = self._download_mixed(
                    resolved, by_source, span=span, sync=sync, stop=stop
                )
                return self._complete_download(resolved, reports, sync=sync, stop=stop)
            return self._download_normal(resolved, by_source, sync=sync, stop=stop, keep=keep)

    def sync(self, targets: Sequence[Target | str] = (), *, compress: bool = False) -> Report:
        """Bring synced targets up to date: fetch new posts and sum up.

        Args:
            targets: only these targets; every synced target when empty.
            compress: shrink the new files as they land, then the files these targets saved
                before that aren't done yet (docs/media).

        Returns:
            One `TargetReport` per target.
        """
        resolved = self._resolved(targets)
        chosen = [(target, replace(options, compress=compress))
                  for target, options in self._plan(resolved)]
        if not chosen:
            ui.info("nothing to sync")
            return Report(())
        _validate_targets(self.sources, None, [t for t, _ in chosen])
        with stopping(self.stop) as stop:
            results: dict[int, TargetReport] = {}
            pool_settings = RunShared(home=self.home, history={}, stop=stop, emit=self.emit)
            with shared_browser(pool_settings):
                self._sync_normal(chosen, results, pool_settings, stop)
                self._sync_queries(chosen, results, pool_settings, stop)
            if compress:
                self._compress_saved([target for target, _ in chosen], stop)
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
                raise UsageError(f"{target}: not a known target; download it first")
        for target in resolved:
            flnd.targets.set_sync(self.home, target, enabled=enabled)
        return resolved

    def target_del(self, targets: Sequence[Target | str]) -> list[Target]:
        """Remove each saved target from `targets.db` while keeping downloaded files."""
        resolved = self._resolved(targets)
        rows = flnd.targets.rows(self.home)
        for target in resolved:
            if flnd.targets.key(target) not in rows:
                raise UsageError(f"{target}: not a saved target")
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
            raise UsageError(f"unknown source '{code}'. Sources: {', '.join(self.sources)}")
        source = self.sources[code]()
        if method not in source.login_methods:
            raise UsageError(
                f"{code} logs in by {', '.join(source.login_methods)}, not {method!r}"
            )
        if code not in self.cookie_logins:
            return self._interactive_login(code, source, method)
        if method == "file":
            if path is None:
                raise UsageError("a file login needs the cookie file's path")
            file = Path(path).expanduser()
            return auth.store(
                source, auth.from_file(source, file), home=self.home, origin=file.name
            )
        if browser is None:
            raise UsageError("a browser login needs the browser to read cookies from")
        jar = auth.from_browser(source, browser=browser, profile=profile)
        return auth.store(source, jar, home=self.home, origin=auth.BROWSERS.get(browser, browser))

    def logout(self, codes: Iterable[str]) -> list[str]:
        """Remove logins for the given source codes (or `all`) and return removed codes."""
        asked = list(dict.fromkeys(codes))
        unknown = [code for code in asked if code != ALL and code not in self.sources]
        if unknown:
            raise UsageError(
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
        with stopping() as stop:
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
        sync: bool,
        stop: Stop,
        keep: bool = False,
    ) -> Report:
        """Download non-query targets in order, recording each in targets.db first.

        Args:
            resolved: the targets, in order.
            by_source: each source's options, by code.
            sync: whether to follow the targets.
            stop: the run's stop state.
            keep: whether to save each target's categories for later syncs.

        Returns:
            One `TargetReport` per target, in order.

        Raises:
            KeyboardInterrupt: the run was stopped.
        """
        rows = flnd.targets.rows(self.home)
        chosen = [
            (
                target,
                replace(
                    by_source[target.source],
                    stop_at_saved=(
                        by_source[target.source].stop_at_saved
                        or (
                            not by_source[target.source].again
                            and flnd.targets.key(target) in rows
                            and flnd.targets.sync_enabled(rows[flnd.targets.key(target)])
                        )
                    ),
                ),
            )
            for target in resolved
        ]
        flnd.targets.record(self.home, chosen, sync=sync, keep=keep)
        reports = self._run(chosen, stop=stop)
        return self._complete_download(resolved, reports, sync=sync, stop=stop, rows=rows)

    def _complete_download(
        self,
        resolved: Sequence[Target],
        reports: Sequence[TargetReport],
        *,
        sync: bool,
        stop: Stop,
        rows: Mapping[str, Any] | None = None,
    ) -> Report:
        report = self._finish_report(Report(tuple(reports)), stop)
        if not sync:
            self._warn_unsynced(resolved, rows=rows)
        return report

    def _finish_report(self, report: Report, stop: Stop) -> Report:
        if stop.soon.is_set():
            raise KeyboardInterrupt
        _log_totals(report)
        return report

    def _sync_normal(
        self,
        chosen: Sequence[tuple[Target, Options]],
        results: dict[int, TargetReport],
        pool_settings: RunShared,
        stop: Stop,
    ) -> None:
        """Sync non-query targets and store their `TargetReport` entries in `results`."""
        indices = [i for i, (target, _) in enumerate(chosen) if not self._is_query(target)]
        if not indices:
            return
        normal = [chosen[i] for i in indices]
        flnd.targets.record(self.home, normal)
        ui.info("Storage: {path}", path=(self.home / paths.MEDIA).resolve())
        run_items = [(t, replace(opts, stop_at_saved=True)) for t, opts in normal]
        reports = self._run(
            run_items, stop=stop, browser_pool=pool_settings.browser_pool
        )
        if stop.soon.is_set():
            raise KeyboardInterrupt
        for pos, rep in zip(indices, reports):
            results[pos] = rep

    def _sync_queries(
        self,
        chosen: Sequence[tuple[Target, Options]],
        results: dict[int, TargetReport],
        pool_settings: RunShared,
        stop: Stop,
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
        stop: Stop,
    ) -> Report:
        reports: list[TargetReport] = []
        # A dry run reads the history to tell "saved" from "new", but never makes it: a home
        # without one stays untouched.
        history = self._saved_rows()
        settings = RunShared(
            home=self.home, history=history, stop=stop, dry=True, emit=self.emit,
        )
        with shared_browser(settings):
            for target in resolved:
                opts = by_source[target.source]
                reports.append(self._dry_one(settings, target, opts, span, stop))
        return Report(tuple(reports))

    def _saved_rows(self) -> dict[str, Any]:
        """Every history row by key, read once; `{}` when the home has no history yet."""
        if not (self.home / paths.HISTORY).is_file():
            return {}
        with self._history() as history:
            return dict(history.items())

    def _dry_one(
        self,
        settings: RunShared,
        target: Target,
        options: Options,
        span: Range,
        stop: Stop,
    ) -> TargetReport:
        """Run a dry-run plan for a single target."""
        source = self.sources[target.source]()
        with source_run(settings, source) as run:
            account = TargetSite(run, Target(source.code, ACCOUNT, ""))
            if not source.logged_in(account) and source.requires_login:
                failed = job.logged_out(target)
                job.tell(target, failed, self.emit)
                return TargetReport(target, (), 0, error=failed)
            try:
                with _connected(run, account):
                    _log_dry(run, target, options, span, is_query=self._is_query(target))
            except KeyboardInterrupt:
                if not stop.soon.is_set() or stop.now.is_set():
                    raise
                return TargetReport(target, (), 0, error=Stopped(job.STOPPED))
            except Exception as error:
                failed = job.failure(error, target, source)
                job.tell(target, failed, self.emit)
                return TargetReport(target, (), 0, error=failed)
            return TargetReport(target, (), 0)

    def _download_mixed(
        self,
        resolved: Sequence[Target],
        by_source: Mapping[str, Options],
        *,
        span: Range,
        sync: bool,
        stop: Stop,
    ) -> list[TargetReport]:
        """Download a mix of discovering and normal targets in caller order."""
        reports: list[TargetReport] = []
        pool_settings = RunShared(home=self.home, history={}, stop=stop, emit=self.emit)
        with shared_browser(pool_settings):
            for target in resolved:
                options = by_source[target.source]
                if self._is_query(target):
                    reports.extend(self._run_query(
                        target, options, span=span, sync=sync, stop=stop,
                        browser_pool=pool_settings.browser_pool,
                    ))
                    continue
                flnd.targets.record(self.home, [(target, options)], sync=sync)
                [rep] = self._run(
                    [(target, options)], stop=stop, browser_pool=pool_settings.browser_pool
                )
                reports.append(rep)
        return reports

    def _run_query(
        self,
        target: Target,
        options: Options,
        *,
        span: Range,
        sync: bool,
        stop: Stop,
        browser_pool: Any = None,
    ) -> list[TargetReport]:
        """Discover targets from a discovering route and download each one."""
        source = self.sources[target.source]()
        flnd.targets.record(self.home, [(target, options)], sync=sync)
        reports: list[TargetReport] = []
        with self._history() as history, self._targets() as saved_targets, \
                ui.group(drawn=False) as group:
            settings = RunShared(
                home=self.home, history=history, targets=saved_targets,
                stop=stop, browser_pool=browser_pool, emit=self.emit,
            )
            board = job.Board(
                group, subject_width=len(str(target)),
                label_width=self._label_width([(target, options)]), parent=target,
                emit=self.emit,
            )
            with (_steps.attached(settings), shared_browser(settings),
                  source_run(settings, source) as run):
                if err := _require_auth(source, run, target):
                    return [err]
                self._discover_and_run(
                    run, target, options, board, reports, span=span, stop=stop,
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
        span: Range,
        stop: Stop,
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
                        child_opts = replace(options, limit=None, query=target.value)
                        rep = self._run_child(run, target, child, board, child_opts, stop)
                        if rep.error is None:
                            seen_keys.append(flnd.targets.key(child))
                        reports.append(rep)
        except KeyboardInterrupt:
            if not stop.soon.is_set() or stop.now.is_set():
                raise
            reports.append(TargetReport(target, (), 0, error=Stopped(job.STOPPED)))
        except Exception as error:
            failed = job.failure(error, target, run.source)
            job.tell(target, failed, self.emit)
            reports.append(TargetReport(target, (), 0, error=failed))
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
        stop: Stop,
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
        stop: Stop,
        browser_pool: Any = None,
    ) -> TargetReport:
        """Run a dry-run plan for a single target."""
        source = self.sources[target.source]()
        rows = flnd.targets.rows(self.home)
        seen_keys = set(flnd.targets.seen(rows, target))
        known_keys = set(rows)
        with self._history() as history, self._targets() as saved_targets, \
                ui.group(drawn=False) as group:
            settings = RunShared(
                home=self.home, history=history, targets=saved_targets,
                stop=stop, browser_pool=browser_pool, emit=self.emit,
            )
            board = job.Board(
                group, subject_width=len(str(target)),
                label_width=self._label_width([(target, options)]), parent=target,
                emit=self.emit,
            )
            with (_steps.attached(settings), shared_browser(settings),
                  source_run(settings, source) as run):
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
        stop: Stop,
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
            return TargetReport(target, tuple(written), skipped, error=Stopped(job.STOPPED))
        except Exception as error:
            failed = True
            ended = job.failure(error, target, run.source)
            job.tell(target, ended, self.emit)
            return TargetReport(target, tuple(written), skipped, error=ended)
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
        stop: Stop,
        written: list[Path],
        seen_to_mark: list[str],
    ) -> tuple[int, Error | Stopped | None]:
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
            ui.info("not followed; flnd follow keeps it up to date")

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
                    categories=flnd.targets.only(rows.get(flnd.targets.key(target), {})),
                    query=flnd.targets.query_name(rows.get(flnd.targets.key(target), {})),
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
                    except UsageError as error:
                        ui.debug(
                            "{folder}: not synced: {error}",
                            folder=f"{code}/{folder.name}",
                            error=error,
                        )

    def _run(
        self,
        chosen: Sequence[tuple[Target, Options]],
        *,
        stop: Stop,
        browser_pool: Any = None,
    ) -> list[TargetReport]:
        """Run each target sequentially 1 by 1, grouped by site session."""
        groups: dict[str, list[int]] = {}
        for number, (target, _) in enumerate(chosen):
            groups.setdefault(target.source, []).append(number)
        reports: dict[int, TargetReport] = {}
        with self._history() as history, self._targets() as saved, ui.group(drawn=False) as rows:
            run_settings = RunShared(
                home=self.home, history=history, targets=saved,
                stop=stop, browser_pool=browser_pool, emit=self.emit,
            )
            board = job.Board(
                rows,
                subject_width=max((len(str(target)) for target, _ in chosen), default=0),
                label_width=self._label_width(chosen), emit=self.emit,
            )
            work = [
                (
                    self.sources[code](),
                    [_Planned(number, *chosen[number]) for number in numbers],
                )
                for code, numbers in groups.items()
            ]
            with _steps.attached(run_settings), shared_browser(run_settings):
                overall = rows.add("Targets", total=len(chosen))
                for item in work:
                    try:
                        _run_site(run_settings, board, reports, item)
                    except (KeyboardInterrupt, DiskError):
                        raise
                    except BaseException as error:
                        _fail_unreached(item, error, reports, board)
                    overall.advance(len(item[1]))
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

    def _compress_saved(self, targets: Sequence[Target], stop: Stop) -> None:
        """Compress (and extract) what `targets` saved before and isn't done yet.

        One read of history picks the files under the targets' folders; a done file costs no
        `stat`. The chain's lanes do the work, and closing it waits for them.

        Args:
            targets: the synced targets, whose folders bound what is taken.
            stop: the run's Ctrl-C signal; after it, queued files are left for the next sync.
        """
        folders = [paths.target_folder(self.home, target) for target in targets]
        with self._history() as history:
            settings = RunShared(home=self.home, history=history, stop=stop, emit=self.emit)
            with _steps.attached(settings) as chain:
                queued = chain.backlog(history.items(), folders)
                if queued:
                    ui.info("compressing {files} saved before",
                            files=_text.count(queued, "file"))

    def _history(self) -> AbstractContextManager[_db.Table[Any]]:
        return paths.table(self.home / paths.HISTORY, HISTORY_TABLE)

    def _targets(self) -> AbstractContextManager[_db.Table[Any]]:
        return paths.table(self.home / paths.TARGETS, flnd.targets.TARGETS_TABLE)


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
        failed = job.logged_out(target)
        job.tell(target, failed, run.settings.emit)
        return TargetReport(target, (), 0, error=failed)
    if not source.logged_in(account):
        ui.warn("{code}: not logged in, using public access; run flnd login {code}",
                code=source.code)
    return None


def _log_dry(
    run: SourceRun, target: Target, options: Options, span: Range, *, is_query: bool,
) -> None:
    """List what a download of `target` would save: a log line and a `Planned` event each.

    A file is "saved" when its history row or its file is already there, else "new". A
    search plans each target it finds, with no post and no path.

    Args:
        run: the source's run, whose settings hold the history and the event sink.
        target: the target to plan.
        options: its options (categories, limit, time).
        span: the time range a search filters by.
        is_query: whether the target is a search.
    """
    emit = run.settings.emit
    if is_query:
        with closing(job.discover(run, target, limit=options.limit, time_range=span)) as found:
            for item in found:
                ui.info("{target}", target=str(item))
                emit(Planned(item, "", None, "new"))
        return
    folder = paths.target_folder(run.settings.home, target)
    for placed in job.plan(run, target, options):
        ui.info("{target}: {path}", target=str(target), path=placed.path)
        dest = folder.joinpath(*placed.path.split("/"))
        saved = f"{run.source.code}:{placed.key}" in run.settings.history or dest.is_file()
        emit(Planned(target, placed.post.id, dest, "saved" if saved else "new"))


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
        raise UsageError("a target can't be empty")
    source, body = _split_target(cleaned, sources)
    try:
        return source.resolve(body)
    except _Unmatched as miss:
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
            raise UsageError(f"unknown source '{code}'. Sources: {', '.join(sources)}")
        return sources[code](), f"?{cleaned[qmark + 1:].strip()}"
    if colon != -1:
        code, value = cleaned[:colon].strip().lower(), cleaned[colon + 1:]
        if code not in sources:
            raise UsageError(f"unknown source '{code}'. Sources: {', '.join(sources)}")
        src, body = sources[code](), value.strip()
        if not body:
            forms = ", ".join(src.forms)
            raise UsageError(f"{src.code}: can't read {value!r}; write it like {forms}")
        return src, body
    return _owner_source(cleaned, sources), cleaned


def _owner_source(cleaned: str, sources: Mapping[str, type[Source]]) -> Source:
    found = owner(cleaned, sources)
    if found is None:
        choices = ", ".join(f"{code}:{cleaned}" for code in sources)
        raise UsageError(f'which source is "{cleaned}"? Write it as {choices}')
    return found()


def owner(target_text: str, sources: Mapping[str, type[Source]]) -> type[Source] | None:
    """Return the source whose domains `target_text` belongs to, or None.

    A built-in source's domains come from the manifest, so matching a link imports only the
    source that owns it; a source added at run time is asked for its own.

    Args:
        target_text: a link, or a target written without its source.
        sources: the sources to ask, by code.

    Returns:
        The class of the source whose domains the link is on; None when no source's are.
    """
    from flnd.sources import _manifest

    for code in sources:
        known = _manifest.SOURCES.get(code)
        domains = known["domains"] if known is not None else sources[code].domains
        if _on(target_text, domains):  # type: ignore[arg-type]
            return sources[code]
    return None


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
        raise NetworkError(f"can't follow {_text.redact(url)}: {error}", retryable=True) from None


def _run_site(
    run_settings: RunShared,
    board: job.Board,
    reports: dict[int, TargetReport],
    work: tuple[Source, list[_Planned]],
) -> None:
    """Execute all planned targets for one source site and record their reports.

    A target that ends in a `LoginError` stops the source: the targets after it get the same
    error without being asked. The other sources still run.

    Args:
        run_settings: what every source of the run shares.
        board: the run's progress rows.
        reports: filled with each target's report, by its number in the run.
        work: the source, and its targets in order.

    Raises:
        DiskError: a target's files can't be written; the whole run stops.
    """
    source, group = work
    with source_run(run_settings, source) as run:
        account = TargetSite(run, Target(source.code, ACCOUNT, ""))
        if not source.logged_in(account) and source.requires_login:
            for planned in group:
                failed = job.logged_out(planned.target)
                job.tell(planned.target, failed, run_settings.emit)
                reports[planned.number] = TargetReport(planned.target, (), 0, error=failed)
            return
        if not source.logged_in(account):
            ui.warn(
                "{code}: not logged in, using public access; run flnd login {code}",
                code=source.code,
            )
        with _written_back(run), _connected(run, account):
            login: LoginError | None = None
            for planned in group:
                # A login the site refused stops this source: its other targets would hit the
                # same wall. The other sources go on, and the run exits 3 at the end.
                if login is not None:
                    board.end(planned.target, failed=True)
                    reports[planned.number] = TargetReport(planned.target, (), 0, error=login)
                    run_settings.emit(Failed(planned.target, login))
                    continue
                report = job.download(run, planned.target, board, planned.options)
                reports[planned.number] = report
                if isinstance(report.error, LoginError):
                    login = report.error
                # A full or refused disk stops the whole run: every later file would fail too.
                elif isinstance(report.error, DiskError):
                    raise report.error


def _fail_unreached(
    work: tuple[Source, list[_Planned]],
    error: BaseException,
    reports: dict[int, TargetReport],
    board: job.Board,
) -> None:
    """Fail every target of a source that broke before it could run them, one event each.

    Args:
        work: the source, and its targets in order.
        error: what broke it (a source that can't start, a bug in its `connect`).
        reports: filled with a failed report for each target not reported yet.
        board: the run's progress rows, which end failed; its sink gets one `Failed` per target.
    """
    source, group = work
    ui.debug("{source}: {error}", source=source.code, error=error)
    for planned in group:
        board.end(planned.target, failed=True)
        if planned.number not in reports:
            failed = job.failure(error, planned.target, source)
            reports[planned.number] = TargetReport(planned.target, (), 0, error=failed)
            board.emit(Failed(planned.target, failed))


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
