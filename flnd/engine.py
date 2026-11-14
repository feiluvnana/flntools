"""The engine: flnd's five commands as methods for CLI or scripts.

`Engine` is the engine API: `resolve` reads targets, `download` and `sync` run them
and return a `Report`, while `login` and `logout` manage credentials.

Targets are grouped by source; each source runs sequentially while different sources
run concurrently. Each run writes a timestamped log in `.flnd/log/`.

    engine = Engine(home=Path(".flnd"))
    report = engine.download(
        ["ins:example_user"], categories="reels", time="2026-01-01,", shrink=True
    )
    report = engine.sync()
    exit_status = 0 if report.ok else 1
"""

from __future__ import annotations

import itertools
import signal
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit

import httpx

import flnd.query  # noqa: F401
import flnd.targets
from flnd import job, repair
from flnd.job import TargetReport
from flnd.common import auth, compress, net, paths
from flnd.common.compress import Compressor
from flnd.options import Options, RunShared, single_targets, unknown_categories
from flnd.site import LoginSite, SourceRun, TargetSite, shared_browser, source_run
from flnd.source import Range, Source, Target, TargetError
from flnd.sources import SOURCES
from shared import browser, kv, log, worker

__all__ = ["Engine", "Report", "TargetReport"]

HISTORY_TABLE = "downloads"
# The target a source's login and `connect` see: the account, not anything to download.
ACCOUNT = "account"
# How long a run's log is kept, in days (user, 2026-11-13); older ones go as a run starts.
LOG_DAYS = 30
# What `logout` takes for every source.
ALL = "all"
# Maximum new targets started per synced query on one `flnd sync`.
MAX_SYNC_NEW = 25


@dataclass(frozen=True)
class Report:
    """What `download` or `sync` did, one `TargetReport` per target in run order.

        report = engine.download(["ins:example_user"])
        print(report.files, [str(target) for target in report.failed])
    """

    # One per target, in the order they were given (or recorded, for a `sync` of every target).
    targets: tuple[TargetReport, ...]

    @property
    def files(self) -> int:
        """How many files were written, over every target."""
        return sum(len(target.written) for target in self.targets)

    @property
    def failed(self) -> tuple[Target, ...]:
        """The targets that failed, in order: route or category error, or missing files."""
        return tuple(target.target for target in self.targets if target.error is not None)

    @property
    def ok(self) -> bool:
        """Whether no target failed: exit status is 0 when True, and 1 otherwise."""
        return not self.failed


class Engine:
    """flnd's commands as methods: `resolve`, `download`, `sync`, `login` and `logout`.

        engine = Engine(home=Path(".flnd"))
        report = engine.download(["tik:example_creator"], categories="videos")
        engine.login("ins", "browser", browser="chrome")

    Targets of one source run one after another; different sources run at the same time.
    `download` and `sync` never ask questions; a source's own `login` asks through its `Login`.
    """

    def __init__(
        self,
        *,
        home: Path = Path(".flnd"),
        sources: Mapping[str, type[Source]] = SOURCES,
    ):
        """Make an engine; nothing is read or started until a method is called.

        Args:
            home: flnd's folder holding downloads, history, logins and saved targets.
            sources: the sources by code; tests give their own.

        A run has no download limit: it ends when the sources have nothing more, or on Ctrl-C.
        """
        self.home, self.sources = home, sources

    def resolve(self, texts: Sequence[str]) -> list[Target]:
        """Resolve every target as its source reads it, raising TargetError on failure.

            targets = engine.resolve(["ins:@example_user", "https://vm.tiktok.com/ZMabc/"])
        """
        return [resolve(text, self.sources, follow=_follow) for text in texts]

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
        """Download each target, with its saved `--shrink` or the one given, and say what was done.

            report = engine.download(
                ["ins:example_user"], categories="reels,tagged", time="2026-01-01,", shrink=True
            )

        Args:
            targets: `Target`s, or texts that `resolve` reads; a text that can't be read raises
                before anything starts.
            categories: `--from`: names as a comma list, or a sequence of them; "all", or None,
                takes every category. Refused for a single post's link, which is downloaded whole.
            time: `--time`: a `Range`, or its text ("2026-01-01,2026-06-30"); `Range()` is no limit.
            shrink: `--shrink`: True shrinks each photo, video and audio file at media's balanced
                level in its own format, False (`--shrink off`) stops it, and None keeps the
                target's saved setting.
            limit: `--limit`: max discovered targets on a query route, or max posts on a normal
                target; None for no limit.
            dry: `--dry`: list what would be downloaded without downloading or recording anything.
            sync: `--sync`: mark targets to be updated by `flnd sync`.
            again: `--again`: ignore the new-post-only check and saved cursors, running from start
                to finish.

        Returns:
            One `TargetReport` per target, in the order given, each with the saved setting it used.

        Raises:
            ValueError: an option's text can't be read, `--from` names no category of these
                targets' sources or is given for a single post, or a target
                can't be read (`TargetError`); each message is what the CLI shows as a usage error.
                Raised before any file but the run log is read or written.
            OSError: `targets.db` or the history can't be read or written.

        `--shrink` given here is saved for each target, and used by every later `download` and
        `sync` of it; `--from` and `--time` are never saved. Turning it on shrinks the target's
        files already saved before anything is downloaded, so a folder is never half shrunk;
        that is slow, and replaces originals with their smaller copies. It is saved only once
        that is done, so a shrink cut short (Ctrl-C, a crash, a full disk) is finished by the
        next `download --shrink`. Turning it off touches no file.
        """
        with self._logged(
            "download", targets=targets, categories=categories, time=time, shrink=shrink,
            limit=limit, dry=dry, sync=sync, again=again,
        ):
            # Every option is read before anything starts, in the CLI's order, so the first bad
            # one is the error.
            if limit is not None and limit < 1:
                raise ValueError("--limit must be at least 1")
            span = Range.parse(time) if isinstance(time, str) else time
            resolved = self._resolved(targets)
            names = self._categories(categories, resolved)
            options = Options(
                categories=names, since=span.since, until=span.until, limit=limit, again=again,
            )
            if dry:
                report = self._dry_run(resolved, options, span)
                _end_if_stopped()
                _log_totals(report)
                return report
            log.info("Storage: {path}", path=(self.home / paths.MEDIA).resolve())
            if any(self._is_query(target) for target in resolved):
                reports = self._download_mixed(
                    resolved, options, shrink=shrink, span=span, sync=sync
                )
                _end_if_stopped()
                if not sync:
                    rows = flnd.targets.rows(self.home)
                    has_unsynced = any(
                        not flnd.targets.sync_enabled(
                            rows.get(flnd.targets.key(t), {"sync": False})
                        )
                        for t in resolved if not self._is_query(t)
                    ) or any(self._is_query(t) for t in resolved)
                    if has_unsynced:
                        log.info("not synced; add --sync to keep it up to date")
                report = Report(tuple(reports))
                _log_totals(report)
                return report
            rows = flnd.targets.rows(self.home)
            chosen: list[tuple[Target, Options]] = []
            shown: list[str] = []
            turned_on: list[Target] = []
            for target in resolved:
                saved = flnd.targets.shrink(rows.get(flnd.targets.key(target), {}))
                shown.append(flnd.targets.announce(target, saved) if shrink is None else "")
                given = saved if shrink is None else shrink
                chosen.append((target, replace(options, shrink=given)))
                if shrink and not saved:
                    turned_on.append(target)
            to_record = [pair for pair in chosen if pair[0] not in turned_on]
            flnd.targets.record(self.home, to_record, sync=sync)
            if turned_on:
                self._apply(turned_on, dict(chosen), sync=sync)
            reports = self._run(chosen)
            _end_if_stopped()
            if not sync:
                after = flnd.targets.rows(self.home)
                if any(
                    not flnd.targets.sync_enabled(after.get(flnd.targets.key(t), {}))
                    for t in resolved
                ):
                    log.info("not synced; add --sync to keep it up to date")
            report = Report(tuple(replace(report, settings=text)
                                  for report, text in zip(reports, shown)))
            _log_totals(report)
            return report

    def sync(self, targets: Sequence[Target | str] = ()) -> Report:
        """Bring synced targets up to date: repair what was lost, fetch what's new, and sum it up.

            report = engine.sync()                              # every synced target
            report = engine.sync(["ins:example_user"])

        Args:
            targets: only these, as `Target`s or texts; when empty, every synced target in
                `targets.db` and every unrecorded folder under `media/<code>/`.

        Returns:
            One `TargetReport` per target, with its repaired and missing counts.

        Raises:
            ValueError: a target can't be read (`TargetError`), before anything starts.
            OSError: the history or `targets.db` can't be opened or written.
        """
        with self._logged("sync", targets=targets):
            chosen = self._plan(self._resolved(targets))
            if not chosen:
                log.info("nothing to sync")
                return Report(())
            query_indices = [i for i, (target, _) in enumerate(chosen) if self._is_query(target)]
            normal_indices = [
                i for i, (target, _) in enumerate(chosen) if not self._is_query(target)
            ]
            results: dict[int, TargetReport] = {}
            pool_settings = RunShared(home=self.home, history={})
            with shared_browser(pool_settings):
                if normal_indices:
                    normal_chosen = [chosen[i] for i in normal_indices]
                    flnd.targets.record(self.home, normal_chosen)
                    shown = [
                        flnd.targets.announce(target, options.shrink)
                        for target, options in normal_chosen
                    ]
                    folders = [
                        paths.target_folder(self.home, target) for target, _ in normal_chosen
                    ]
                    with self._history() as history:
                        index = repair.index(history, folders)
                    tallies = [repair.Tally(str(target), folder, index[folder])
                               for (target, _), folder in zip(normal_chosen, folders)]
                    log.info("Storage: {path}", path=(self.home / paths.MEDIA).resolve())
                    normal_run = [
                        (target, replace(options, stop_at_saved=True))
                        for target, options in normal_chosen
                    ]
                    reports = self._run(
                        normal_run, tallies=tallies, browser_pool=pool_settings._browser_pool,
                    )
                    _end_if_stopped()
                    with self._history() as history:
                        for tally in tallies:
                            repair.count(history, tally)
                    repair.summary(tallies)
                    for pos, rep, tally, text in zip(normal_indices, reports, tallies, shown):
                        results[pos] = replace(
                            rep, repaired=tally.repaired, missing=tally.missing, settings=text
                        )
                if query_indices:
                    query_chosen = [chosen[i] for i in query_indices]
                    flnd.targets.record(self.home, query_chosen)
                    for pos, (target, options) in zip(query_indices, query_chosen):
                        results[pos] = self._sync_query(
                            target, options, browser_pool=pool_settings._browser_pool,
                        )
                        _end_if_stopped()
            report = Report(tuple(results[i] for i in range(len(chosen))))
            _log_totals(report)
            return report

    def sync_ls(self) -> list[dict[str, Any]]:
        """Every known target with sync state, saved settings, disk size, and category status."""
        rows = flnd.targets.rows(self.home)
        recorded = [Target(code, row["kind"], value) for key, row in rows.items()
                    for code, value in [key.split(":", 1)]
                    if code in self.sources and isinstance(row, dict)]
        all_targets = recorded + list(self._unrecorded(recorded))
        items: list[dict[str, Any]] = []
        for target in all_targets:
            row = rows.get(flnd.targets.key(target), {})
            is_query = self._is_query(target)
            folder = paths.target_folder(self.home, target)
            files, size = (0, 0) if is_query else _folder_stats(folder)
            complete = flnd.targets.listed(rows, target)
            cursors = flnd.targets.cursors(rows, target)
            categories = ({} if is_query else {
                cat: (
                    "complete" if cat in complete
                    else ("interrupted" if cat in cursors else "incomplete")
                )
                for cat in self.sources[target.source].categories
            })
            items.append({
                "target": target,
                "sync": flnd.targets.sync_enabled(row),
                "shrink": flnd.targets.shrink(row),
                "at": str(row.get("at", "")),
                "files": files,
                "size": size,
                "seen": len(flnd.targets.seen(rows, target)) if is_query else 0,
                "categories": categories,
                "query": is_query,
            })
        return items

    def sync_toggle(self, targets: Sequence[Target | str]) -> list[tuple[Target, bool]]:
        """Flip each known target between synced and not, returning `(target, new_sync_state)`."""
        resolved = self._resolved(targets)
        rows = flnd.targets.rows(self.home)
        recorded = [Target(code, row["kind"], value) for key, row in rows.items()
                    for code, value in [key.split(":", 1)]
                    if code in self.sources and isinstance(row, dict)]
        unrecorded = set(self._unrecorded(recorded))
        for target in resolved:
            if flnd.targets.key(target) not in rows and target not in unrecorded:
                raise ValueError(f"{target}: not a known target; download it first")
        return [(target, flnd.targets.toggle(self.home, target)) for target in resolved]

    def auth_ls(self) -> list[dict[str, Any]]:
        """Each source's login state, origin, and cookies expired or expiring within 24 hours."""
        items: list[dict[str, Any]] = []
        for code, source_cls in self.sources.items():
            logged = auth.logged_in(self.home, code)
            api_key = not logged and auth.api_key_saved(self.home, code)
            state = "logged in" if logged else ("api key only" if api_key else "not logged in")
            items.append({
                "code": code,
                "name": source_cls.name,
                "state": state,
                "origin": auth.origin(self.home, code) if logged else "",
                "expiring": auth.expiring(self.home, source_cls()) if logged else [],
            })
        return items

    def login(
        self, code: str, method: str, *, browser: str | None = None,
        profile: str | None = None, path: Path | str | None = None,
    ) -> str:
        """Log in to a source by one of its `login_methods`, and return the "logged in" message.

            engine.login("ins", "browser", browser="chrome")      # "ins: logged in from Chrome"
            engine.login("ins", "file", path="~/Downloads/cookies.txt")
            engine.login("tlg", "qr")                              # asks through `Login`

        Args:
            code: the source's code.
            method: one of the source's `login_methods`: `browser` or `file` for a cookie source,
                or the source's own (tlg's `browser`, `qr`, `phone`).
            browser: for a cookie source's `browser`: a browser to read cookies from, such as
                "chrome" (see `auth.BROWSERS`).
            profile: that browser's profile folder; None reads its default one.
            path: for `file`: a Netscape or JSON cookie file, read once and never copied.

        Returns:
            The message to show; "" for a source with its own login, which says so itself.

        Raises:
            LoginError: the login didn't work, with what to do instead (`flnd.LoginError`).
            ValueError: the source or method is unknown, or the method's answer is missing.

        Credentials are encrypted in `.flnd/metadata/secrets.db`; nothing else is written.
        """
        if code not in self.sources:
            raise ValueError(f"unknown source '{code}'. Sources: {', '.join(self.sources)}")
        source = self.sources[code]()
        if method not in source.login_methods:
            raise ValueError(f"{code} logs in by {', '.join(source.login_methods)}, not {method!r}")
        if code not in self.cookie_logins:
            # A source with its own login (tlg) asks its own questions, through an asking `Login`.
            with source_run(RunShared(home=self.home, history={}, interactive=True), source) as run:
                source.login(LoginSite(run), method)
            if auth.logged_in(self.home, code):
                auth.record_origin(self.home, code, method)
            return ""
        if method == "file":
            if path is None:
                raise ValueError("a file login needs the cookie file's path")
            file = Path(path).expanduser()
            return auth.store(
                source, auth.from_file(source, file), home=self.home, origin=file.name
            )
        if browser is None:
            raise ValueError("a browser login needs the browser to read cookies from")
        jar = auth.from_browser(source, browser=browser, profile=profile)
        return auth.store(source, jar, home=self.home, origin=auth.BROWSERS.get(browser, browser))

    def logout(self, codes: Iterable[str]) -> list[str]:
        """Remove logins of these sources (or all for `["all"]`), returning codes that had one.

            engine.logout(["ins", "tik"])        # ["ins"] when only ins was logged in
            engine.logout(["all"])

        Args:
            codes: source codes, or "all" among them for every source.

        Returns:
            The codes whose login (cookies, a session or only an API key) was removed, in the
            sources' order for "all" and in the order given otherwise, each once.

        Raises:
            ValueError: a code isn't one of the sources.
        """
        asked = list(dict.fromkeys(codes))
        unknown = [code for code in asked if code != ALL and code not in self.sources]
        if unknown:
            raise ValueError(f"unknown source '{unknown[0]}'. Sources: {', '.join(self.sources)}")
        chosen = list(self.sources) if ALL in asked else asked
        return [code for code in chosen if auth.logout(self.home, code)]

    @property
    def logins(self) -> dict[str, str]:
        """Each source with something saved: "logged in" or "api key saved".

            engine.logins          # {"ins": "logged in", "tlg": "api key saved"}

        It creates no file, except to move a plain cookie file of an earlier version into the vault.
        """
        found = {}
        for code in self.sources:
            if auth.logged_in(self.home, code):
                found[code] = "logged in"
            elif auth.api_key_saved(self.home, code):
                found[code] = "api key saved"
        return found

    @property
    def browsers(self) -> dict[str, str]:
        """The browsers a cookie login reads, by `login`'s `browser=` name with display name.

            engine.browsers        # {"chrome": "Chrome", "firefox": "Firefox", …}
        """
        return dict(auth.BROWSERS)

    @property
    def cookie_logins(self) -> tuple[str, ...]:
        """The codes of the sources that log in with cookies (`browser` or `file`).

            engine.cookie_logins   # ("tik", "fbk", "ins", "twt", "loi")

        `login` asks such a source for a browser and profile, or a file; any other runs its own.
        """
        return tuple(code for code, source in self.sources.items() if source.login is Source.login)

    def _is_query(self, target: Target) -> bool:
        """Whether `target` resolves to a `?{query}` route."""
        source = self.sources[target.source]
        route = next((r for r in source.routes if r.kind == target.kind), None)
        return route is not None and route.is_query

    def _dry_run(self, resolved: Sequence[Target], options: Options, span: Range) -> Report:
        """List discovered targets or planned files without downloading or writing state."""
        if any(needs_browser(self.sources[target.source]) for target in resolved):
            _offer_browser()
        reports: list[TargetReport] = []
        settings = RunShared(home=self.home, history={}, dry=True)
        with _stop_waits_on_ctrl_c(), shared_browser(settings):
            for target in resolved:
                source = self.sources[target.source]()
                with source_run(settings, source) as run:
                    account = TargetSite(run, Target(source.code, ACCOUNT, ""))
                    if not source.logged_in(account) and source.requires_login:
                        log.error("{target}: not logged in; run flnd login", target=str(target))
                        reports.append(
                            TargetReport(target, (), 0, error="not logged in; run flnd login")
                        )
                        continue
                    try:
                        with _connected(run, account):
                            if self._is_query(target):
                                with closing(
                                    job.discover(run, target, limit=options.limit, time_range=span)
                                ) as found:
                                    for item in found:
                                        log.info("{target}", target=str(item))
                            else:
                                for placed in job.plan(run, target, options):
                                    log.info(
                                        "{target}: {path}", target=str(target), path=placed.path
                                    )
                    except KeyboardInterrupt:
                        if not net.stopping.is_set() or net.stopping_now.is_set():
                            raise
                        reports.append(TargetReport(target, (), 0, error=job.STOPPED))
                        continue
                    except Exception as error:
                        log.error("{target}: {error}", target=str(target), error=error)
                        reports.append(TargetReport(target, (), 0, error=str(error)))
                        continue
                    reports.append(TargetReport(target, (), 0))
        return Report(tuple(reports))

    def _download_mixed(self, resolved: Sequence[Target], options: Options, *,
                        shrink: bool | None, span: Range, sync: bool = False) -> list[TargetReport]:
        """Run a mix of query targets and normal targets, preserving target order."""
        reports: list[TargetReport] = []
        pool_settings = RunShared(home=self.home, history={})
        with shared_browser(pool_settings):
            for target in resolved:
                if self._is_query(target):
                    reports.extend(
                        self._run_query(
                            target, options, shrink=shrink, span=span, sync=sync,
                            browser_pool=pool_settings._browser_pool,
                        )
                    )
                    continue
                rows = flnd.targets.rows(self.home)
                saved = flnd.targets.shrink(rows.get(flnd.targets.key(target), {}))
                shown = flnd.targets.announce(target, saved) if shrink is None else ""
                chosen_options = replace(options, shrink=saved if shrink is None else shrink)
                if shrink and not saved:
                    self._apply([target], {target: chosen_options}, sync=sync)
                else:
                    flnd.targets.record(self.home, [(target, chosen_options)], sync=sync)
                [report] = self._run(
                    [(target, chosen_options)], browser_pool=pool_settings._browser_pool,
                )
                reports.append(replace(report, settings=shown))
        return reports

    def _run_query(self, target: Target, options: Options, *,
                   shrink: bool | None, span: Range, sync: bool = False,
                   browser_pool: Any = None) -> list[TargetReport]:
        """Discover targets from a query route and download each one sequentially."""
        source_cls = self.sources[target.source]
        if needs_browser(source_cls):
            _offer_browser()
        source = source_cls()
        reports: list[TargetReport] = []
        rows = flnd.targets.rows(self.home)
        if sync:
            saved_query_shrink = flnd.targets.shrink(rows.get(flnd.targets.key(target), {}))
            query_shrink = saved_query_shrink if shrink is None else shrink
            flnd.targets.record(
                self.home, [(target, replace(options, shrink=query_shrink))], sync=True
            )
        with self._history() as history, self._targets() as saved_targets, \
                _stop_waits_on_ctrl_c(), log.group() as log_rows:
            run_settings = RunShared(
                home=self.home, history=history, targets=saved_targets, _browser_pool=browser_pool,
            )
            board = job.Board(log_rows, subject_width=len(str(target)),
                              label_width=self._label_width([(target, options)]))
            with shared_browser(run_settings), source_run(run_settings, source) as run:
                account = TargetSite(run, Target(source.code, ACCOUNT, ""))
                if not source.logged_in(account) and source.requires_login:
                    log.error("{target}: not logged in; run flnd login", target=str(target))
                    return [TargetReport(target, (), 0, error="not logged in; run flnd login")]
                if not source.logged_in(account):
                    log.warn(
                        "{code}: not logged in, using public access; run flnd login",
                        code=source.code,
                    )
                try:
                    with _written_back(run), _connected(run, account):
                        with closing(
                            job.discover(
                                run, target, limit=options.limit, time_range=span, seen=(),
                            )
                        ) as found:
                            for child in found:
                                if net.stopping.is_set():
                                    break
                                saved_shrink = flnd.targets.shrink(
                                    rows.get(flnd.targets.key(child), {})
                                )
                                shown = (
                                    flnd.targets.announce(child, saved_shrink)
                                    if shrink is None else ""
                                )
                                child_shrink = saved_shrink if shrink is None else shrink
                                child_options = replace(options, shrink=child_shrink, limit=None)
                                flnd.targets.record(
                                    self.home, [(child, child_options)], sync=False
                                )
                                if child.source == target.source:
                                    report = job.download(run, child, board, child_options)
                                else:
                                    [report] = self._run(
                                        [(child, child_options)],
                                        browser_pool=run_settings._browser_pool,
                                    )
                                if sync and report.error is None:
                                    flnd.targets.mark_seen(
                                        self.home, target, [flnd.targets.key(child)]
                                    )
                                reports.append(replace(report, settings=shown))
                                if net.stopping.is_set():
                                    break
                except KeyboardInterrupt:
                    if not net.stopping.is_set() or net.stopping_now.is_set():
                        raise
                    reports.append(TargetReport(target, (), 0, error=job.STOPPED))
                except Exception as error:
                    log.error("{target}: {error}", target=str(target), error=error)
                    log.debug("{target}: {chain}", target=str(target), chain=job.causes(error))
                    reports.append(TargetReport(target, (), 0, error=str(error)))
        return reports

    def _sync_query(self, target: Target, options: Options, *,
                    browser_pool: Any = None) -> TargetReport:
        """Run one synced query target: skip known targets and update `seen`."""
        source_cls = self.sources[target.source]
        if needs_browser(source_cls):
            _offer_browser()
        source = source_cls()
        rows = flnd.targets.rows(self.home)
        seen_keys = set(flnd.targets.seen(rows, target))
        known_keys = set(rows)
        written: list[Path] = []
        skipped = 0
        child_failed: str | None = None
        new_count = 0
        with self._history() as history, self._targets() as saved_targets, \
                _stop_waits_on_ctrl_c(), log.group() as log_rows:
            run_settings = RunShared(
                home=self.home, history=history, targets=saved_targets, _browser_pool=browser_pool,
            )
            board = job.Board(log_rows, subject_width=len(str(target)),
                              label_width=self._label_width([(target, options)]))
            with shared_browser(run_settings), source_run(run_settings, source) as run:
                account = TargetSite(run, Target(source.code, ACCOUNT, ""))
                if not source.logged_in(account) and source.requires_login:
                    log.error("{target}: not logged in; run flnd login", target=str(target))
                    return TargetReport(target, (), 0, error="not logged in; run flnd login")
                if not source.logged_in(account):
                    log.warn(
                        "{code}: not logged in, using public access; run flnd login",
                        code=source.code,
                    )
                try:
                    with _written_back(run), _connected(run, account):
                        with closing(
                            job.discover(run, target, time_range=Range(), seen=seen_keys)
                        ) as found:
                            for child in found:
                                if net.stopping.is_set():
                                    break
                                child_key = flnd.targets.key(child)
                                if child_key in known_keys:
                                    flnd.targets.mark_seen(self.home, target, [child_key])
                                    skipped += 1
                                    continue
                                if new_count >= MAX_SYNC_NEW:
                                    break
                                new_count += 1
                                child_options = replace(options, limit=None)
                                flnd.targets.record(self.home, [(child, child_options)], sync=False)
                                known_keys.add(child_key)
                                if child.source == target.source:
                                    report = job.download(run, child, board, child_options)
                                else:
                                    [report] = self._run(
                                        [(child, child_options)],
                                        browser_pool=run_settings._browser_pool,
                                    )
                                written.extend(report.written)
                                skipped += report.skipped
                                if report.error is None:
                                    flnd.targets.mark_seen(self.home, target, [child_key])
                                else:
                                    child_failed = report.error
                                if net.stopping.is_set():
                                    break
                except KeyboardInterrupt:
                    if not net.stopping.is_set() or net.stopping_now.is_set():
                        raise
                    return TargetReport(target, tuple(written), skipped, error=job.STOPPED)
                except Exception as error:
                    log.error("{target}: {error}", target=str(target), error=error)
                    log.debug("{target}: {chain}", target=str(target), chain=job.causes(error))
                    return TargetReport(target, tuple(written), skipped, error=str(error))
        return TargetReport(target, tuple(written), skipped, error=child_failed)

    def _resolved(self, targets: Sequence[Target | str]) -> list[Target]:
        """Each target, a text read by `resolve`, each written to the run log as it was read."""
        resolved = []
        for given in targets:
            target = given if isinstance(given, Target) else self.resolve([given])[0]
            log.debug(
                "resolve {given}: {target} ({kind})",
                given=given, target=target, kind=target.kind,
            )
            resolved.append(target)
        return resolved

    @contextmanager
    def _logged(self, command: str, **given: Any) -> Iterator[None]:
        """Write the block's run to a log in `.flnd/log/`, deleting logs older than `LOG_DAYS`.

        The log opens with the version, the command and the options as given, and ends with the
        exit status the CLI gives: 2 for a usage error (`ValueError`), 130 for Ctrl-C, 1 for any
        other exception, each with its cause; a finished run writes its own (`_log_totals`).
        """
        from flnd import __version__

        folder = self.home / paths.LOGS
        _prune(folder)
        stem = f"{datetime.now():%Y-%m-%d_%H%M%S}_{command}"
        path = next(
            folder / (f"{stem}.log" if n == 1 else f"{stem}-{n}.log")
            for n in itertools.count(1)
            if not (folder / (f"{stem}.log" if n == 1 else f"{stem}-{n}.log")).exists()
        )
        with log.record(path):
            options = ", ".join(
                f"{name}={value!r}" for name, value in given.items()
                if value not in (None, (), Range())
                and not (name in ("dry", "sync", "again") and value is False)
            )
            log.debug("flnd {version} {command}: {options}", version=__version__, command=command,
                      options=options or "no options")
            try:
                yield
            except ValueError as error:
                log.debug("exit 2: {error}", error=error)
                raise
            except KeyboardInterrupt:
                log.debug("exit 130: stopped")
                raise
            except BaseException as error:
                log.debug("exit 1: {chain}", chain=job.causes(error))
                raise

    def _categories(
        self, categories: str | Sequence[str] | None, targets: Sequence[Target],
    ) -> tuple[str, ...] | None:
        """`--from`'s names, each once, checked against these targets; None without `--from`.

        Raises:
            ValueError: no source here has a name, or a target is a single post.
        """
        if categories is None:
            return None
        items = [categories] if isinstance(categories, str) else list(categories)
        names = tuple(
            dict.fromkeys(
                name.strip() for item in items for name in item.split(",") if name.strip()
            )
        )
        sources = (
            list(dict.fromkeys(self.sources[target.source] for target in targets))
            or list(self.sources.values())
        )
        if problem := unknown_categories(names, sources) or single_targets(targets, self.sources):
            raise ValueError(problem)
        return names

    def _plan(self, targets: Sequence[Target]) -> list[tuple[Target, Options]]:
        """Each target to sync with saved settings and default categories; nothing is written.

        Args:
            targets: only these; when empty, every recorded target and every unrecorded folder
                under `media/<code>/`, in that order.
        """
        rows = flnd.targets.rows(self.home)
        if not targets:
            synced_rows = flnd.targets.synced(rows)
            recorded = [Target(code, row["kind"], value) for key, row in rows.items()
                        for code, value in [key.split(":", 1)]
                        if code in self.sources and isinstance(row, dict)]
            targets = [Target(code, row["kind"], value) for key, row in synced_rows.items()
                       for code, value in [key.split(":", 1)] if code in self.sources]
            targets += list(self._unrecorded(recorded))
        return [
            (target, Options(shrink=flnd.targets.shrink(rows.get(flnd.targets.key(target), {}))))
            for target in targets
        ]

    def _unrecorded(self, known: Sequence[Target]) -> Iterator[Target]:
        """Folders under `media/<code>/` not recorded in targets database."""
        seen = {
            paths.target_folder(self.home, target)
            for target in known if not self._is_query(target)
        }
        media = self.home / paths.MEDIA
        for code in self.sources:
            for folder in sorted((media / code).iterdir()) if (media / code).is_dir() else ():
                if folder.is_dir() and folder not in seen:
                    try:
                        yield self.resolve([f"{code}:{folder.name}"])[0]
                    except TargetError as error:
                        log.warn(
                            "{folder}: not synced: {error}",
                            folder=f"{code}/{folder.name}", error=error,
                        )

    def _apply(self, targets: Sequence[Target], chosen: Mapping[Target, Options], *,
               sync: bool | None = None) -> None:
        """Shrink saved files for targets with `--shrink` enabled, then save target settings.

        Args:
            targets: the targets whose `--shrink` this download turns on.
            chosen: each target's options, as `record` takes them.
            sync: `--sync` flag to record alongside `--shrink`.

        Raises:
            OSError: the history or `targets.db` can't be written.
        """
        folders = {target: paths.target_folder(self.home, target) for target in targets}
        compressor = Compressor()
        with self._history() as history:
            index = repair.index(history, list(folders.values()))
            for target, folder in folders.items():
                compress.apply(history, index[folder], compressor, label=str(target))
                # Saved only now that every file is shrunk so interrupted shrinks resume next run.
                flnd.targets.record(self.home, [(target, chosen[target])], sync=sync)

    def _run(self, chosen: Sequence[tuple[Target, Options]], *,
             tallies: Sequence[repair.Tally] = (),
             browser_pool: Any = None) -> list[TargetReport]:
        """Download each target with its own options, and give each one's report in order.

            [report] = engine._run([(Target("ins", "user", "example_user"), Options())])

        `tallies`, one per target for `sync`, repairs each target's folder just before its
        listing; a target that lost a file then lists to its end.

        One source's targets run one after another, each source on a worker of its own, so
        different sites download side by side and none sees more requests than alone. When a
        target can open a browser and Google Chrome is missing, the "download Chromium?"
        question is asked once, before anything starts.

        One target failing never stops the others; a source that can't start (its `logged_in`
        or `connect` raises) fails each of its targets. A rate limit is waited out and isn't a
        failure. The first Ctrl-C stops gracefully: listing stops, the running downloads
        finish, no target starts, and each one not done ends as stopped (`job.STOPPED`); this
        then returns with `net.stopping` still set. The second raises `KeyboardInterrupt`.
        """
        if any(needs_browser(self.sources[target.source]) for target, _ in chosen):
            _offer_browser()
        groups: dict[str, list[int]] = {}
        for number, (target, _) in enumerate(chosen):
            groups.setdefault(target.source, []).append(number)
        # Filled by every source's worker at once, each under its own targets' numbers, and read
        # only once they have all ended: one assignment per key needs no lock.
        reports: dict[int, TargetReport] = {}
        with self._history() as history, self._targets() as saved, _stop_waits_on_ctrl_c(), \
                log.group() as rows:
            run_settings = RunShared(
                home=self.home, history=history, targets=saved, _browser_pool=browser_pool,
            )
            board = job.Board(rows, subject_width=max((len(str(target)) for target, _ in chosen),
                                                      default=0),
                              label_width=self._label_width(chosen))
            work = [(self.sources[code](),
                     [_Planned(number, *chosen[number], tallies[number] if tallies else None)
                      for number in numbers])
                    for code, numbers in groups.items()]
            with shared_browser(run_settings):
                for result in worker.as_completed(partial(_run_site, run_settings, board, reports),
                                                  work, concurrency=max(len(work), 1)):
                    if not result.ok:
                        _fail_unreached(result.item, result.error, reports, board)
        return [reports[number] for number in range(len(chosen))]

    def _label_width(self, chosen: Sequence[tuple[Target, Options]]) -> int:
        """The widest status-line label of these targets: a category's name or a route's kind."""
        labels = [target.kind for target, _ in chosen]
        for code in {target.source for target, _ in chosen}:
            labels += [category.name for route in self.sources[code].routes
                       for category in route.categories]
        return max(map(len, labels), default=0)

    def _history(self) -> kv.Store:
        """The download history, opened for a `with` block."""
        return kv.open(self.home / paths.HISTORY, table=HISTORY_TABLE)

    def _targets(self) -> kv.Store:
        """`targets.db`, opened once for a run's `with` block."""
        return kv.open(self.home / paths.TARGETS, table=flnd.targets.TARGETS_TABLE)


def _folder_stats(folder: Path) -> tuple[int, int]:
    """Count non-hidden files and total bytes under `folder`."""
    if not folder.is_dir():
        return 0, 0
    files, size = 0, 0
    for path in folder.rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            files += 1
            size += path.stat().st_size
    return files, size


def _end_if_stopped() -> None:
    """Raise `KeyboardInterrupt` once a first Ctrl-C stopped the run: the CLI exits 130."""
    # What finished is recorded by now; the command ends as Ctrl-C always does.
    if net.stopping.is_set():
        raise KeyboardInterrupt


def _log_totals(report: Report) -> None:
    """Write a finished run's totals and exit status to its log: 1 when a target failed, else 0."""
    log.debug(
        "totals: {files} written, {skipped} skipped, {failed} of {targets} failed; exit {status}",
        files=report.files, skipped=sum(target.skipped for target in report.targets),
        failed=len(report.failed), targets=len(report.targets), status=0 if report.ok else 1,
    )


def _prune(folder: Path) -> None:
    """Delete the run logs in `folder` last written more than `LOG_DAYS` ago."""
    limit = time.time() - LOG_DAYS * 24 * 3600
    for path in folder.glob("*.log") if folder.is_dir() else ():
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        # Gone already (another run pruned it), or not removable here: the next run tries again.
        except OSError:
            pass


def resolve(
    text: str, sources: Mapping[str, type[Source]], *, follow: Callable[[str], str] | None = None
) -> Target:
    """Read one target as people write it, and return it resolved by its source.

        resolve("ins:@example_user", SOURCES)             # Target("ins", "user", "example_user")
        resolve("https://x.com/example_handle", SOURCES)  # Target("twt", "user", "example_handle")

    `code:value` goes to that source's `resolve`. A bare URL finds its source by domain. Anything
    else is refused, because it could belong to any source.

    Args:
        text: one target, as typed or listed.
        sources: the sources by code.
        follow: where a link leads, from one HEAD request. A link on a source's site that no route
            matches is followed once and read again (short links such as `vm.tiktok.com/…`); None
            reads every link as it is.

    Raises:
        TargetError: the text is empty, names no source, or its source can't read it, with what
            to write instead.
    """
    cleaned = text.strip()
    if not cleaned:
        raise TargetError("a target can't be empty")
    if not _is_url(cleaned):
        qmark = cleaned.find("?")
        colon = cleaned.find(":")
        if qmark != -1 and (colon == -1 or qmark < colon) and "/" not in cleaned[:qmark]:
            code = cleaned[:qmark].strip().lower()
            if code not in sources:
                raise TargetError(f"unknown source '{code}'. Sources: {', '.join(sources)}")
            source, cleaned = sources[code](), f"?{cleaned[qmark + 1:].strip()}"
        elif colon != -1:
            code, value = cleaned[:colon].strip().lower(), cleaned[colon + 1:]
            if code not in sources:
                raise TargetError(f"unknown source '{code}'. Sources: {', '.join(sources)}")
            source, cleaned = sources[code](), value.strip()
            if not cleaned:
                raise TargetError(
                    f"{source.code}: can't read {value!r}; write it like {', '.join(source.forms)}"
                )
        else:
            source = _owner_source(cleaned, sources)
    else:
        source = _owner_source(cleaned, sources)
    try:
        return source.resolve(cleaned)
    except TargetError as miss:
        # Only a link no route matched carries `url`; a refusal (an `unsupported` link) is final.
        if follow is None or miss.url is None:
            raise
        url = miss.url
    # Followed once only: a second miss is the refusal, listing the forms.
    return source.resolve(follow(url))


def _owner_source(cleaned: str, sources: Mapping[str, type[Source]]) -> Source:
    """Resolve the owning `Source` for a bare URL or raise `TargetError`."""
    found = owner(cleaned, sources)
    if found is None:
        choices = ", ".join(f"{code}:{cleaned}" for code in sources)
        raise TargetError(f'which source is "{cleaned}"? Write it as {choices}')
    return found()


def owner(text: str, sources: Mapping[str, type[Source]]) -> type[Source] | None:
    """The source whose domains a URL belongs to, or None.

    For example, `owner("https://x.com/example_handle", SOURCES)` is Twitter.

    Args:
        text: the text as typed; only a URL, with or without a scheme, can have an owner.
        sources: the sources to look through, in order; the first whose domain matches wins.
    """
    return next((source for source in sources.values() if _on(text, source.domains)), None)


def _on(text: str, domains: Sequence[str]) -> bool:
    """Whether `text` is a URL on one of `domains` or their subdomains."""
    # Without a scheme, only a slash tells a link (instagram.com/example_user) from a name.
    if not domains or ("/" not in text and not _is_url(text)):
        return False
    host = (urlsplit(text if _is_url(text) else f"https://{text}").hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


@contextmanager
def _stop_waits_on_ctrl_c() -> Iterator[None]:
    """Make the first Ctrl-C a graceful stop and the second an immediate one, in every thread.

    The first sets `net.stopping` and raises nothing: every rate-limit wait ends at once, each job
    stops listing and lets its running downloads finish (flnd/job.py), and the command raises
    `KeyboardInterrupt` once they have. The second sets `net.stopping_now`, which stops the
    downloads at their next chunk with their resumable parts kept, and does what Ctrl-C always does.
    """
    net.stopping.clear()
    net.stopping_now.clear()
    # Python sets signal handlers only on the main thread, so from any other thread
    # (a script's worker) Ctrl-C keeps its default behaviour rather than raising here.
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)

    def interrupted(number: int, frame: Any) -> None:
        # A signal handler runs between two bytecodes of the main thread, which may hold the
        # screen's lock, so it only sets events; the job threads draw the rows.
        if not net.stopping.is_set():
            net.stopping.set()
            return
        net.stopping_now.set()
        # A Python handler is callable and is called as it would have been; SIG_DFL and SIG_IGN
        # are not, so the Ctrl-C raises KeyboardInterrupt itself, as it does by default.
        if callable(previous):
            previous(number, frame)
        else:
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def needs_browser(source: type[Source]) -> bool:
    """Whether downloading from `source` can open a browser, as declared by `source.browser`."""
    return source.browser


def _offer_browser() -> None:
    """Ask, once and before any work, whether to download Chromium when Google Chrome is missing.

    `browser.check` asks only without Chrome or a Chromium downloaded before, with the live rows
    held. A no, or no terminal, is kept for the process: each target that opens a browser then
    fails with "flnd needs Google Chrome", and the others run. Without the browser extra, the
    first target that opens a browser says what to install.
    """
    try:
        _check_browser()
    # Declined, failed to download, or no Playwright: said again by each target that needs it.
    except (RuntimeError, ImportError) as error:
        log.debug("no browser: {error}", error=error)


def _check_browser() -> None:
    """`browser.check()`, through a name tests replace."""
    browser.check()


def _follow(url: str) -> str:
    """Where a short link leads, from one HEAD request.

    A network failure is a `TargetError` naming the link.
    """
    # Only a network failure is turned into the target's message; any other error propagates.
    try:
        return net.follow(url)
    except httpx.HTTPError as error:
        raise TargetError(f"can't follow {url}: {error}") from None


@dataclass(frozen=True)
class _Planned:
    """One target of a source's group: its place in the run, its options and its repair."""

    number: int
    target: Target
    options: Options
    # `sync` only: the tally its folder's repair fills, just before its listing.
    tally: repair.Tally | None


def _run_site(run_settings: RunShared, board: job.Board, reports: dict[int, TargetReport],
              work: tuple[Source, list[_Planned]]) -> None:
    """Run one source's targets, one after another, on one worker, filling `reports` as each ends.

    A target that ends is in `reports` even when a later one stops the whole group, so its report
    survives; `Engine._run` fails the ones the group never reached.
    """
    source, group = work
    with source_run(run_settings, source) as run:
        account = TargetSite(run, Target(source.code, ACCOUNT, ""))
        if not source.logged_in(account) and source.requires_login:
            for planned in group:
                log.error("{target}: not logged in; run flnd login", target=str(planned.target))
                reports[planned.number] = TargetReport(planned.target, (), 0,
                                                       error="not logged in; run flnd login")
            return
        if not source.logged_in(account):
            log.warn("{code}: not logged in, using public access; run flnd login", code=source.code)
        with _written_back(run), _connected(run, account):
            for planned in group:
                options = _repaired(run_settings.history, planned)
                reports[planned.number] = job.download(run, planned.target, board, options)


def _repaired(history: MutableMapping[str, Any], planned: _Planned) -> Options:
    """Repair a target's folder right before its listing under `sync`, and give its options."""
    tally = planned.tally
    if tally is None or net.stopping.is_set():
        return planned.options
    log.debug("{target}: repair", target=tally.target)
    repair.repair(history, tally)
    # A target with a lost or broken file lists to the end, so a file deeper than the saved-run
    # stop is reached and saved again; the others stop at saved posts.
    return replace(
        planned.options,
        stop_at_saved=planned.options.stop_at_saved and not tally.lost,
        again=planned.options.again or bool(tally.lost),
    )


def _fail_unreached(work: tuple[Source, list[_Planned]], error: BaseException | None,
                    reports: dict[int, TargetReport], board: job.Board) -> None:
    """Fail each target of a group that stopped before or between its targets."""
    source, group = work
    log.error("{source}: {error}", source=source.code, error=error)
    for planned in group:
        # An ended line keeps its end; any other would end at ok when `log.group` closes.
        board.end(planned.target, failed=True)
        if planned.number not in reports:
            reports[planned.number] = TargetReport(planned.target, (), 0, error=str(error))


@contextmanager
def _written_back(run: SourceRun) -> Iterator[None]:
    """Write the cookies the site refreshed back to the vault, however the block ends."""
    try:
        yield
    finally:
        auth.write_back(run.settings.home, run.source.code, run.web.raw.cookies.jar)


@contextmanager
def _connected(run: SourceRun, account: Any) -> Iterator[None]:
    """Enter the source's `connect` once for all its targets, sync or async, as `site.client`."""
    manager = run.source.connect(account)
    if not hasattr(manager, "__aenter__"):
        with manager as client:
            run.client = client
            yield
        return
    run.client = run.run_async(manager.__aenter__())
    try:
        yield
    # Every error, Ctrl-C included, goes to the source's `__aexit__` first, which may suppress it
    # by returning true; if it doesn't, the error is raised again unchanged.
    except BaseException as error:
        if not run.run_async(manager.__aexit__(type(error), error, error.__traceback__)):
            raise
    else:
        run.run_async(manager.__aexit__(None, None, None))
