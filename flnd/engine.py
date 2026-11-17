"""The engine: every flnd command as a method of `Engine`, and target resolution.

What it holds: `Engine`, whose methods are the commands (`download`, `sync`, `retry`,
`target_ls`, `target_sync`, `auth_ls`, `login`, `logout`); `resolve` and `owner`,
which turn a target's text into a `Target` of the right source; and the reports a command
returns (`Report`, one `flnd.job.TargetReport` per target), with `TargetInfo` and `AuthInfo` for
the reads behind `fl.targets()` (`flnd ls`) and `fl.logins`.

Who calls it: `flnd._library.Flnd` alone (the CLI reaches it through the library). A command
that downloads makes its `Engine` inside a `flnd.Run`, on the run's worker thread, with the
run's event sink and stop state; a read or a quick change (`targets`, `logins`, `unfollow`,
`login`, `logout`) makes one on the caller's thread. Nothing in a source calls it.

What it calls: `flnd.options` to check a command's options before any request, `flnd._home` for
`flnd.db` and the logins, `flnd.site` for each source's run (`source_run`) and the run's one
browser and loop (`shared_browser`), `flnd._steps` for the step chain after each file, and
`flnd.job` for each target.

How one command that downloads goes (`download`):
1. Check the options (`--limit`, `--when`, `--only`) and resolve every target, so a usage
   error comes before anything is fetched or written.
2. Open the command's `runs` row in `flnd.db` (`_recording`), which each failure names.
3. Record each target's row (`Home.record`), followed when the command follows.
4. Group the targets by source, in the order given, and run each group inside one
   `source_run` (`_run_site`): its login is checked, its `connect` hook runs once, and each
   target goes through `job.download` in turn.
5. Download the targets the listings yielded (`_found`), up to `FOLLOW_DEPTH` links deep.
6. Close the `runs` row with the count of saved files, and return a `Report`.

A search (a target whose route `discovers`, such as `mht?words`) lists the targets it finds and
runs each one (`_run_query`); its sync takes only the found targets it hadn't seen
(`_sync_query`), at most `MAX_SYNC_NEW` a run.

Rules a change must keep:
- Every check that can fail on what the user typed runs before the first request, and fails
  with a `UsageError` (exit code 2).
- A target's failure ends that target alone. A refused login ends its source's later targets
  (`_run_site`); a full disk (`DiskError`) ends the whole run; Ctrl-C ends it with
  `KeyboardInterrupt` once the reports are in.
- The commands run on one thread: the run's worker thread for a library run, or the caller's.
  Each `flnd.db` access opens the home for that call (`_home.open`), so no connection is shared
  across threads; the job's download tasks use the run's own connection, held in `RunShared`.
- A dry run (`dry=True`) reads `flnd.db` and the disk, and writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flnd import _home, _net, _steps, _text, job
from flnd._events import Failed, Planned
from flnd._run import Stop, stopping
from flnd._ui import ui
from flnd.errors import DiskError, Error, LoginError, NetworkError, Stopped, UsageError
from flnd.job import TargetReport
from flnd.options import Options, Retry, RunShared, categories_by_source, validate
from flnd.site import LoginSite, SourceRun, TargetSite, shared_browser, source_run
from flnd.source import Cookies, Range, Source, Target, _Unmatched
from flnd.sources import SOURCES

__all__ = ["AuthInfo", "Engine", "Report", "TargetInfo", "TargetReport"]

# The kind of the pseudo-target a source's account `Site` is made for (`_account`).
ACCOUNT = "account"
# What `logout` takes for every source.
ALL = "all"
# How many links deep the targets that listings yield are followed (`Engine._found`).
FOLLOW_DEPTH = 2
# Targets a search's sync downloads at most per run, among those it hadn't seen: a search that
# suddenly finds hundreds (a popular tag) is taken a slice per sync, never all at once.
MAX_SYNC_NEW = 25


@dataclass(frozen=True)
class TargetInfo:
    """A known target's state and its saved files, as `Engine.target_ls` reads them."""

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
    """flnd's commands as methods: `resolve`, `download`, `sync`, `retry`, `login`, `logout`."""

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
        # The `runs` row of the command going on, which its failures name; None between them.
        self.run: int | None = None

    def resolve(self, texts: Sequence[str]) -> list[Target]:
        """Resolve every target string through its owning source (see `resolve`).

        Args:
            texts: the targets as typed: `<code>:<value>`, `<code>?<query>` or a link.

        Returns:
            One `Target` per text, in order.

        Raises:
            UsageError: a text names no known source, or its source can't read it.
            NetworkError: a short link couldn't be followed.
        """
        return [resolve(text_item, self.sources, follow=_follow) for text_item in texts]

    def download(
        self,
        targets: Sequence[Target | str],
        *,
        categories: str | Sequence[str] | None = None,
        time: Range | None = None,
        limit: int | None = None,
        dry: bool = False,
        sync: bool = False,
        again: bool = False,
        keep: bool = False,
    ) -> Report:
        """Download each target with its saved or given options and return a `Report`.

        Args:
            targets: the targets, as text or resolved.
            categories: `--only`: the categories to take; None for none named.
            time: `--when`, as a `Range`; None for no limit.
            limit: at most this many posts per target, or targets per search; None for all.
            dry: plan what would be saved (`Planned` events), and write nothing.
            sync: follow the targets, so `sync` takes them later.
            again: ignore saved cursors and the stop at saved posts.
            keep: whether to save `categories` with each target, for later syncs (`follow`).

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
        resolved = self._resolved(targets)
        cats_map = categories_by_source(categories, resolved, self.sources)
        _validate_targets(self.sources, categories, resolved)
        base = Options(
            since=span.since,
            until=span.until,
            limit=limit,
            again=again,
            stop_at_saved=sync and not again,
        )
        by_source = {
            code: replace(base, categories=cats_map.get(code))
            for code in {t.source for t in resolved}
        }
        with stopping(self.stop) as stop:
            if dry:
                return self._finish_report(self._dry_run(resolved, by_source, span, stop), stop)
            ui.info("Storage: {path}", path=(self.home / _home.MEDIA).resolve())
            with self._recording("download", resolved) as ended:
                if any(self._is_query(t) for t in resolved):
                    reports = self._download_mixed(
                        resolved, by_source, span=span, sync=sync, stop=stop
                    )
                    report = self._complete_download(resolved, reports, sync=sync, stop=stop)
                else:
                    report = self._download_normal(
                        resolved, by_source, sync=sync, stop=stop, keep=keep)
                ended.append(report)
                return report

    def sync(self, targets: Sequence[Target | str] = (), *, time: Range | None = None) -> Report:
        """Bring synced targets up to date: fetch new posts and sum up.

        Args:
            targets: only these targets; every synced target when empty.
            time: the days every target takes posts from (`[defaults] when` in config.toml`);
                None for every day.

        Returns:
            One `TargetReport` per target.
        """
        resolved = self._resolved(targets)
        span = time or Range()
        chosen = [(target, replace(options, since=span.since, until=span.until))
                  for target, options in self._plan(resolved)]
        if not chosen:
            ui.info("nothing to sync")
            return Report(())
        _validate_targets(self.sources, None, [t for t, _ in chosen])
        with stopping(self.stop) as stop, \
                self._recording("sync", [t for t, _ in chosen]) as ended:
            results: dict[int, TargetReport] = {}
            pool_settings = RunShared(home=self.home, stop=stop, emit=self.emit)
            with shared_browser(pool_settings):
                self._sync_normal(chosen, results, pool_settings, stop)
                self._sync_queries(chosen, results, pool_settings, stop)
            report = Report(tuple(results[i] for i in range(len(chosen))))
            ended.append(report)
            return self._finish_report(report, stop)

    def retry(self) -> Report:
        """Retry every failure the home keeps, however many runs failed it (`flnd retry`).

        Each target with failed files lists each failed post again, from the cursor of the page
        it was on, and downloads the files that failed, from `(target, post, file)`: never a
        stored URL (flnd/docs/errors). A target whose listing failed whole is downloaded again, its
        failed files first. A gone file (`NotFound`) isn't retried; `--again` asks for those.

        Returns:
            One `TargetReport` per target retried; none when nothing failed.

        Raises:
            KeyboardInterrupt: Ctrl-C (or the run's stop) ended it.
        """
        with _home.open(self.home) as db:
            failed = db.failed()
            rows = db.targets()
        chosen: list[tuple[Target, Options]] = []
        for item, whole in failed.items():
            row = rows.get(item)
            # A target a search found has no row of its own: its key resolves it.
            recorded = self._recorded({item: row}) if row is not None else []
            try:
                target = recorded[0] if recorded else self._one(item)
            except Error as error:
                ui.warn("{target}: can't retry: {error}", target=item, error=error)
                continue
            if target.source not in self.sources or self._is_query(target):
                continue
            # A failed file may be in any category, so a retry lists the ones it failed in; a
            # target that failed whole is downloaded as its last run would have been.
            options = (Options(categories=row.only if row else None, stop_at_saved=True)
                       if whole else Options(retry=Retry.ONLY))
            chosen.append((target, options))
        if not chosen:
            ui.info("nothing to retry")
            return Report(())
        with stopping(self.stop) as stop, \
                self._recording("retry", [target for target, _ in chosen]) as ended:
            report = Report(tuple(self._run(chosen, stop=stop)))
            ended.append(report)
            return self._finish_report(report, stop)

    def target_ls(self) -> list[TargetInfo]:
        """Every known target with its state and saved files, all read from `flnd.db`.

        Returns:
            The recorded targets, then the folders in `media/` no row names.
        """
        with _home.open(self.home) as db:
            rows = db.targets()
            recorded = self._recorded(rows)
            all_targets = recorded + list(self._unrecorded(recorded, rows))
            return [self._target_info(target, rows, db) for target in all_targets]

    def target_sync(self, targets: Sequence[Target | str], *, enabled: bool) -> list[Target]:
        """Turn `sync` on or off for each known target (`fl.unfollow` turns it off).

        A known target is one with a row in `flnd.db`, or a folder under `media/` that no row
        names (a target downloaded before targets were recorded), which gets a row here. Every
        target is checked before any row changes, so one unknown target changes nothing.

        Args:
            targets: the targets, as text or resolved.
            enabled: True to sync them from now on, False to stop.

        Returns:
            The resolved targets, in order.

        Raises:
            UsageError: a target isn't known; download it first.
        """
        resolved = self._resolved(targets)
        rows = self._rows()
        recorded = self._recorded(rows)
        unrecorded = set(self._unrecorded(recorded, rows))
        for target in resolved:
            if _home.key(target) not in rows and target not in unrecorded:
                raise UsageError(f"{target}: not a known target; download it first")
        with _home.open(self.home) as db:
            for target in resolved:
                db.follow(target, enabled)
        return resolved

    def auth_ls(self) -> list[AuthInfo]:
        """Each source's login state, where its login came from, and its cookies expiring soon.

        It reads the vault only; nothing asks a site whether the login still works.

        Returns:
            One `AuthInfo` per source, in the order of `sources`: "logged in", "api key only"
            (a key with no cookies) or "not logged in".
        """
        items: list[AuthInfo] = []
        for code, source_cls in self.sources.items():
            logged = _home.logged_in(self.home, code)
            api_key = not logged and _home.api_key_saved(self.home, code)
            state = "logged in" if logged else ("api key only" if api_key else "not logged in")
            items.append(AuthInfo(
                code=code,
                name=source_cls.name,
                state=state,
                origin=_home.origin(self.home, code) if logged else "",
                expiring=_home.expiring(self.home, source_cls()) if logged else [],
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
        """Log in to a source by one of its login's `methods` and return a status message.

        A cookie login (`flnd.source.cookies`) reads a browser's cookie store or a cookie file,
        checks that it holds the login's cookies, and seals it in the vault. Any other login
        runs the source's own `start` hook, interactively (a phone code, a browser window).

        Args:
            code: the source's code.
            method: one of its login's `methods`, such as "browser" or "file".
            browser: for "browser": which browser's cookies to read (a key of `browsers`).
            profile: for "browser": the browser profile's name; None for the default one.
            path: for "file": the cookie file, in Netscape `cookies.txt` form.

        Returns:
            What was saved, for the CLI to print; "" for an interactive login, which prints
            its own lines.

        Raises:
            UsageError: an unknown source, one with no login, a method it doesn't take, or a
                method's missing `browser` or `path`.
            LoginError: the cookies found don't prove a login.
        """
        if code not in self.sources:
            raise UsageError(f"unknown source '{code}'. Sources: {', '.join(self.sources)}")
        source = self.sources[code]()
        if source.login is None:
            raise UsageError(f"{code} needs no login")
        if method not in source.login.methods:
            raise UsageError(
                f"{code} logs in by {', '.join(source.login.methods)}, not {method!r}"
            )
        if code not in self.cookie_logins:
            return self._interactive_login(code, source, method)
        if method == "file":
            if path is None:
                raise UsageError("a file login needs the cookie file's path")
            file = Path(path).expanduser()
            return _home.store(
                source, _home.from_file(source, file), home=self.home, origin=file.name
            )
        if browser is None:
            raise UsageError("a browser login needs the browser to read cookies from")
        jar = _home.from_browser(source, browser=browser, profile=profile)
        return _home.store(source, jar, home=self.home, origin=_home.BROWSERS.get(browser, browser))

    def logout(self, codes: Iterable[str]) -> list[str]:
        """Remove the saved logins of the given sources (`flnd logout`).

        Every code is checked before any login is removed.

        Args:
            codes: source codes, or `all` for every source; a repeat counts once.

        Returns:
            The codes whose login was removed; a source that had none is left out.

        Raises:
            UsageError: a code names no known source.
        """
        asked = list(dict.fromkeys(codes))
        unknown = [code for code in asked if code != ALL and code not in self.sources]
        if unknown:
            raise UsageError(
                f"unknown source '{unknown[0]}'. Sources: {', '.join(self.sources)}"
            )
        chosen = list(self.sources) if ALL in asked else asked
        return [code for code in chosen if _home.logout(self.home, code)]

    @property
    def logins(self) -> dict[str, str]:
        """Map each source code with saved credentials to its login state."""
        found: dict[str, str] = {}
        for code in self.sources:
            if _home.logged_in(self.home, code):
                found[code] = "logged in"
            elif _home.api_key_saved(self.home, code):
                found[code] = "api key saved"
        return found

    @property
    def cookie_logins(self) -> tuple[str, ...]:
        """Source codes that authenticate with browser or file cookies."""
        return tuple(code for code, source in self.sources.items()
                     if isinstance(source.login, Cookies))

    def _interactive_login(self, code: str, source: Source, method: str) -> str:
        """Run a source's own login hook (`Login.start`), with a terminal it may prompt on.

        The hook gets a `LoginSite` of a run of its own, with `interactive` on, so it may ask
        for a code or open a browser window. Its Ctrl-C ends the login, not a download. Only a
        login the hook saved has its origin recorded.

        Args:
            code: the source's code.
            source: the source, made from it.
            method: the login method the hook is given.

        Returns:
            "": the hook prints its own lines.
        """
        with stopping() as stop:
            settings = RunShared(home=self.home, stop=stop, interactive=True)
            with source_run(settings, source) as run:
                assert source.login is not None
                source.login.start(LoginSite(run), method)
            if _home.logged_in(self.home, code):
                _home.record_origin(self.home, code, method)
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
        """Download non-query targets in order, recording each in flnd.db first.

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
        rows = self._rows()
        chosen = []
        for target in resolved:
            options = by_source[target.source]
            # A followed target stops at its saved posts, unless told to list them again.
            row = rows.get(_home.key(target))
            followed = row is not None and row.follow and not options.again
            chosen.append((target, replace(
                options, stop_at_saved=options.stop_at_saved or followed)))
        self._record(chosen, follow=sync, keep=keep)
        reports = self._run(chosen, stop=stop)
        return self._complete_download(resolved, reports, sync=sync, stop=stop, rows=rows)

    def _complete_download(
        self,
        resolved: Sequence[Target],
        reports: Sequence[TargetReport],
        *,
        sync: bool,
        stop: Stop,
        rows: Mapping[str, _home.TargetRow] | None = None,
    ) -> Report:
        """End a download: raise for Ctrl-C, log the totals, and say when a target isn't synced.

        Args:
            resolved: the command's targets.
            reports: one report per target.
            sync: whether the command followed them, so no reminder is needed.
            stop: the run's stop state.
            rows: the target rows read before the run; None reads them again.

        Returns:
            The command's `Report`.

        Raises:
            KeyboardInterrupt: Ctrl-C ended the run.
        """
        report = self._finish_report(Report(tuple(reports)), stop)
        if not sync:
            self._warn_unsynced(resolved, rows=rows)
        return report

    def _finish_report(self, report: Report, stop: Stop) -> Report:
        """Raise `KeyboardInterrupt` when the run was stopped, else log its totals and return it.

        A stopped run raises only here, once every target has its report, so the `runs` row
        still ends with the files saved before the stop.

        Args:
            report: the command's report.
            stop: the run's stop state.

        Returns:
            `report`, unchanged.

        Raises:
            KeyboardInterrupt: Ctrl-C (or the run's stop) ended the run.
        """
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
        """Sync the plain targets of `chosen` (not searches), each stopping at saved posts.

        Args:
            chosen: every target of the sync, with its options; searches are left out here.
            results: filled with each plain target's report, by its index in `chosen`.
            pool_settings: the sync's shared settings, whose browser every source reuses.
            stop: the run's stop state.

        Raises:
            KeyboardInterrupt: Ctrl-C stopped the run; the searches aren't synced then.
        """
        indices = [i for i, (target, _) in enumerate(chosen) if not self._is_query(target)]
        if not indices:
            return
        normal = [chosen[i] for i in indices]
        self._record(normal)
        ui.info("Storage: {path}", path=(self.home / _home.MEDIA).resolve())
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
        """Sync the searches of `chosen`, one after another, after the plain targets.

        Args:
            chosen: every target of the sync, with its options; plain targets are left out.
            results: filled with each search's report, by its index in `chosen`.
            pool_settings: the sync's shared settings, whose browser every source reuses.
            stop: the run's stop state.

        Raises:
            KeyboardInterrupt: Ctrl-C stopped the run after a search's report was kept.
        """
        indices = [i for i, (target, _) in enumerate(chosen) if self._is_query(target)]
        if not indices:
            return
        query_chosen = [chosen[i] for i in indices]
        self._record(query_chosen)
        for pos, (target, options) in zip(indices, query_chosen):
            results[pos] = self._sync_query(
                target, options, stop=stop, browser_pool=pool_settings.browser_pool
            )
            if stop.soon.is_set():
                raise KeyboardInterrupt

    def _target_info(
        self, target: Target, rows: Mapping[str, _home.TargetRow], db: _home.Home,
    ) -> TargetInfo:
        """A target's state and its saved files' count and bytes, from `flnd.db` alone.

        A target with a row has its counts there, kept by each run; a folder with no row (a
        target downloaded before targets were recorded) is counted in one query, never by
        walking its folder.

        Args:
            target: the target.
            rows: every target row, by key.
            db: the home's database, for a target with no row.

        Returns:
            What `flnd ls` shows of it.
        """
        row = rows.get(_home.key(target))
        is_query = self._is_query(target)
        if is_query:
            files, size = 0, 0
        elif row is not None:
            files, size = row.files, row.bytes
        else:
            files, size = db.count(_home.key(target))
        complete = row.complete if row is not None else {}
        cursors = row.cursors if row is not None else {}
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
            # A folder no row names was downloaded before following existed: it syncs.
            sync=row.follow if row is not None else True,
            at=row.last_sync if row is not None else "",
            files=files,
            size=size,
            seen=len(row.seen) if is_query and row is not None else 0,
            categories=categories,
            query=is_query,
        )

    def _is_query(self, target: Target) -> bool:
        """Whether `target`'s route discovers other targets (a search) rather than posts."""
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
        """Plan every target without saving anything (`--dry`), one `Planned` event per file.

        Args:
            resolved: the targets, in order.
            by_source: each source's options, by code.
            span: `--when`, which a search filters what it finds by.
            stop: the run's stop state.

        Returns:
            One report per target, with no files written; a failed plan carries its error.
        """
        reports: list[TargetReport] = []
        # A dry run reads `flnd.db` to tell "saved" from "new", but never writes it: a home
        # without one stays untouched.
        with _home.open(self.home) as db:
            settings = RunShared(home=self.home, db=db, stop=stop, dry=True, emit=self.emit,
                                 resolve=self._one)
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
        stop: Stop,
    ) -> TargetReport:
        """Plan one target of a dry run inside its own `source_run`.

        A failure ends this target's plan alone, as a failed report and a `Failed` event; the
        first Ctrl-C ends it as stopped, and the second goes on up.

        Args:
            settings: the dry run's shared settings (`dry` on, the database read-only).
            target: the target to plan.
            options: its options.
            span: `--when`, for a search.
            stop: the run's stop state.

        Returns:
            Its report: no files, and the error that ended the plan, if any.

        Raises:
            KeyboardInterrupt: the second Ctrl-C, or a stop that isn't the run's.
        """
        source = self.sources[target.source]()
        with source_run(settings, source) as run:
            if not _logged_in(run) and _required(source):
                failed = job.logged_out(target)
                job.tell(target, failed, self.emit)
                return TargetReport(target, (), 0, error=failed)
            try:
                with _connected(run, _account(run)):
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
        """Download targets of which some are searches, in the order given, on one browser.

        A plain target runs alone through `_run` (its own source run); a search runs through
        `_run_query`, which downloads each target it finds.

        Args:
            resolved: the targets, in order.
            by_source: each source's options, by code.
            span: `--when`, which a search filters what it finds by.
            sync: whether to follow the targets (a search is followed, not what it finds).
            stop: the run's stop state.

        Returns:
            The reports: one per plain target, and for a search one per target it found (and
            one for the search itself when it failed or was stopped).
        """
        reports: list[TargetReport] = []
        pool_settings = RunShared(home=self.home, stop=stop, emit=self.emit)
        with shared_browser(pool_settings):
            for target in resolved:
                options = by_source[target.source]
                if self._is_query(target):
                    reports.extend(self._run_query(
                        target, options, span=span, sync=sync, stop=stop,
                        browser_pool=pool_settings.browser_pool,
                    ))
                    continue
                self._record([(target, options)], follow=sync)
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
        """Download a search: list the targets it finds, then download each one.

        The search's row is recorded first (followed when `sync`), so a later sync finds it.
        Its found targets share the search's board rows (`Board.parent`).

        Args:
            target: the search.
            options: its options; `limit` bounds how many targets it takes.
            span: `--when`, which the search filters what it finds by.
            sync: whether to follow the search.
            stop: the run's stop state.
            browser_pool: the run's browser; None makes one.

        Returns:
            One report per target found; only the search's own when it needs a login it lacks.
        """
        source = self.sources[target.source]()
        self._record([(target, options)], follow=sync)
        reports: list[TargetReport] = []
        with _home.open(self.home) as db, ui.group(drawn=ui._progress_enabled()) as group:
            settings = RunShared(
                home=self.home, db=db, run=self.run,
                stop=stop, browser_pool=browser_pool, emit=self.emit, resolve=self._one,
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
        """List a search's targets and download each as it comes, then keep what it saw.

        Each found target that downloads without error is added to the search's `seen` keys
        in `flnd.db`, so a later sync of the search skips it. A failure of the listing itself
        is one more report, for the search; the first Ctrl-C stops it after the target going
        on, and the second goes on up.

        Args:
            run: the search's source run, connected for the listing.
            target: the search.
            options: its options; the found targets get them with no limit, and the search's
                text as their `query` (for the `{query}` folder field).
            board: the progress rows the found targets share.
            reports: filled with a report per target downloaded, and one for a failure.
            span: `--when`, which the search filters what it finds by.
            stop: the run's stop state.

        Raises:
            KeyboardInterrupt: the second Ctrl-C, or a stop that isn't the run's.
        """
        seen_keys: list[str] = []
        try:
            with _written_back(run), _connected(run, _account(run)):
                with closing(job.discover(
                    run, target, limit=options.limit, time_range=span, seen=()
                )) as found:
                    for child in found:
                        if stop.soon.is_set():
                            break
                        child_opts = replace(options, limit=None, query=target.value)
                        rep = self._run_child(run, target, child, board, child_opts, stop)
                        if rep.error is None:
                            seen_keys.append(_home.key(child))
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
            if seen_keys and run.settings.db is not None:
                run.settings.db.seen(target, seen_keys)

    def _run_child(
        self,
        run: SourceRun,
        parent: Target,
        child: Target,
        board: job.Board,
        child_opts: Options,
        stop: Stop,
    ) -> TargetReport:
        """Download one target a search found.

        A target of the search's own source reuses the search's run (its session and client);
        one of another source (a link the search found) gets a run of its own through `_run`.

        Args:
            run: the search's source run.
            parent: the search.
            child: the target it found.
            board: the search's progress rows.
            child_opts: the found target's options.
            stop: the run's stop state.

        Returns:
            The found target's report.
        """
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
        """Sync one search: download the targets it finds that it hadn't seen before.

        A found target is new when it is neither in the search's `seen` keys nor a target the
        home knows already (followed or downloaded on its own).

        Args:
            target: the search.
            options: its saved options.
            stop: the run's stop state.
            browser_pool: the sync's browser; None makes one.

        Returns:
            The search's report: the files its new targets saved, the targets skipped, and the
            first error, if any.
        """
        source = self.sources[target.source]()
        rows = self._rows()
        row = rows.get(_home.key(target))
        seen_keys = set(row.seen if row is not None else ())
        known_keys = set(rows)
        with _home.open(self.home) as db, ui.group(drawn=ui._progress_enabled()) as group:
            settings = RunShared(
                home=self.home, db=db, run=self.run,
                stop=stop, browser_pool=browser_pool, emit=self.emit, resolve=self._one,
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
        """Run a search's sync inside its source run, and keep the keys it saw, however it ends.

        Args:
            run: the search's source run.
            target: the search.
            options: its saved options.
            board: the progress rows its found targets share.
            seen_keys: the keys of the targets earlier syncs saw.
            known_keys: the keys of every target the home has a row for.
            stop: the run's stop state.

        Returns:
            The search's report; a failed listing or a stop is its `error`, and else the first
            found target's error.

        Raises:
            KeyboardInterrupt: the second Ctrl-C, or a stop that isn't the run's.
        """
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
            if seen_to_mark and run.settings.db is not None:
                run.settings.db.seen(target, seen_to_mark)
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
        """List a search's targets and download the new ones, at most `MAX_SYNC_NEW` of them.

        A target seen before (or known to the home) is skipped and marked seen. A new one is
        downloaded, and marked seen only when it downloaded without error, so a failed one is
        tried again on the next sync.

        Args:
            run: the search's source run.
            target: the search.
            options: its saved options.
            board: the progress rows its found targets share.
            seen_keys: the keys of the targets earlier syncs saw; the listing stops at them.
            known_keys: the keys of every target the home has a row for.
            stop: the run's stop state.
            written: filled with the paths the new targets saved.
            seen_to_mark: filled with the keys to add to the search's `seen`.

        Returns:
            The targets and files skipped, and the first found target's error (None when
            none failed).
        """
        skipped, new_count, child_failed = 0, 0, None
        with _written_back(run), _connected(run, _account(run)):
            with closing(job.discover(run, target, time_range=Range(), seen=seen_keys)) as found:
                for child in found:
                    if stop.soon.is_set():
                        break
                    child_key = _home.key(child)
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
        self, resolved: Sequence[Target], *, rows: Mapping[str, _home.TargetRow] | None = None,
    ) -> None:
        """Remind the user how to keep a target up to date, when one of them isn't followed."""
        saved = self._rows() if rows is None else rows
        if any((row := saved.get(_home.key(t))) is None or not row.follow for t in resolved):
            ui.info("not followed; flnd follow keeps it up to date")

    def _recorded(self, rows: Mapping[str, _home.TargetRow]) -> list[Target]:
        """The targets of `flnd.db`'s rows, rebuilt from each row's source, kind and value.

        A row of a source that is gone (an uninstalled plugin), or with no kind (a row an old
        home migrated without one), is left out. No source's `resolve` runs, so this costs no
        import of a source and no request.

        Args:
            rows: target rows, by key.

        Returns:
            The targets, in the rows' order; a search is written `<code>?<query>`.
        """
        return [
            Target(f"{row.source}{'?' if self._is_query_kind(row.source, row.kind) else ':'}"
                   f"{row.value}", kind=row.kind)
            for row in rows.values()
            if row.kind and row.source in self.sources
        ]

    def _rows(self) -> dict[str, _home.TargetRow]:
        """Every target `flnd.db` knows, by key; none for a home that has no database yet."""
        with _home.open(self.home) as db:
            return db.targets()

    def _record(self, chosen: Sequence[tuple[Target, Options]], **how: Any) -> None:
        """Remember the targets of a run in `flnd.db`; `how` is `Db.record`'s options."""
        with _home.open(self.home) as db:
            db.record(chosen, **how)

    @contextmanager
    def _recording(self, command: str, targets: Sequence[Target]) -> Iterator[list[Report]]:
        """A `runs` row for a command that downloads, ended with what it saved.

        Args:
            command: the command's name.
            targets: its targets; the first three are named in the row.

        Yields:
            A list the command appends its report to, for the row's count of saved files.
        """
        named = " ".join([command, *map(str, targets[:3])])
        # Each run's logs and samples are kept for a month (flnd/docs/storage).
        _home.sweep(self.home)
        with _home.open(self.home) as db:
            self.run = db.start(named if len(targets) <= 3 else f"{named} …")
        ended: list[Report] = []
        try:
            yield ended
        finally:
            run, self.run = self.run, None
            with _home.open(self.home) as db:
                db.end(run, saved=sum(report.files for report in ended))

    def _is_query_kind(self, code: str, kind: str) -> bool:
        """Whether the route of `kind` in source `code` takes a query (`<code>?<query>`)."""
        route = next((r for r in self.sources[code].routes if r.kind == kind), None)
        return route is not None and route.is_query

    def _resolved(self, targets: Sequence[Target | str]) -> list[Target]:
        """Each target resolved: a `Target` as it is, text through `resolve` (logged at -vv).

        Args:
            targets: the targets, as text or resolved.

        Returns:
            The resolved targets, in order.

        Raises:
            UsageError: a text can't be resolved.
        """
        resolved: list[Target] = []
        for given in targets:
            target = given if isinstance(given, Target) else self.resolve([given])[0]
            ui.debug(
                "resolve {given}: {target} ({kind})", given=given, target=target, kind=target.kind
            )
            resolved.append(target)
        return resolved

    def _plan(self, targets: Sequence[Target]) -> list[tuple[Target, Options]]:
        """Which targets a sync takes, each with the options its row keeps.

        With no targets named, a sync takes every followed row, then every folder under
        `media/` no row names (a target downloaded before following existed, which syncs).

        Args:
            targets: the targets named; empty for every synced target.

        Returns:
            Each target with its saved categories and search text.
        """
        rows = self._rows()
        if not targets:
            followed = {item: row for item, row in rows.items() if row.follow}
            recorded = self._recorded(rows)
            targets = self._recorded(followed) + list(self._unrecorded(recorded, rows))
        planned = []
        for target in targets:
            row = rows.get(_home.key(target))
            planned.append((target, Options(categories=row.only if row else None,
                                            query=row.query if row else None)))
        return planned

    def _unrecorded(
        self, known: Sequence[Target], rows: Mapping[str, _home.TargetRow] | None = None,
    ) -> Iterator[Target]:
        """The targets whose folders are under `media/` but which no row of `known` names.

        A folder a search's found target saved into belongs to the search (its `seen` keys),
        so it isn't yielded on its own. A folder no source can read as a target is logged at
        -vv and passed over. It lists one folder per source, never a target's files.

        Args:
            known: the targets the home's rows name.
            rows: those rows, by key, for the searches' `seen` keys; None for none.

        Yields:
            Each unrecorded target, resolved by its source, in folder-name order per source.
        """
        seen = {_home.target_folder(self.home, t) for t in known if not self._is_query(t)}
        media = self.home / _home.MEDIA
        saved_rows = rows or {}
        for target in known:
            row = saved_rows.get(_home.key(target))
            if self._is_query(target) and row is not None:
                for child_key in row.seen:
                    if ":" in child_key:
                        seen.add(_home.target_folder(self.home, Target(child_key)))
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
        depth: int = 0,
    ) -> list[TargetReport]:
        """Run each target in order, grouped by source; then the targets their listings found.

        Args:
            chosen: the targets, each with its options.
            stop: the run's stop state.
            browser_pool: the run's browser; None makes one for these targets.
            depth: how many links deep these targets are (`_found`); 0 for the run's own.

        Returns:
            One report per target of `chosen`, in order, then one per target found (`_found`).
        """
        groups: dict[str, list[int]] = {}
        for number, (target, _) in enumerate(chosen):
            groups.setdefault(target.source, []).append(number)
        reports: dict[int, TargetReport] = {}
        with _home.open(self.home) as db, ui.group(drawn=ui._progress_enabled()) as rows:
            run_settings = RunShared(
                home=self.home, db=db, run=self.run,
                stop=stop, browser_pool=browser_pool, emit=self.emit, resolve=self._one,
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
        done = [reports[number] for number in range(len(chosen))]
        return done + self._found(chosen, done, stop=stop, browser_pool=browser_pool, depth=depth)

    def _found(self, chosen: Sequence[tuple[Target, Options]], reports: Sequence[TargetReport],
               *, stop: Stop, browser_pool: Any, depth: int) -> list[TargetReport]:
        """Download the targets the run's listings yielded (`TargetReport.found`), once each.

        A post that links to another site hands the link on this way. Each found target is
        resolved by its own source and downloaded with every category, after the run's own
        targets; what those find in turn is followed up to `FOLLOW_DEPTH` links deep, so two
        posts linking to each other can't loop.

        Args:
            chosen: the targets that ran, with their options.
            reports: their reports, in order.
            stop: the run's stop state.
            browser_pool: the run's browser.
            depth: how many links deep these reports are.

        Returns:
            The found targets' reports, in the order they were found.
        """
        if depth >= FOLLOW_DEPTH or stop.soon.is_set():
            return []
        ran = {target for target, _ in chosen}
        found: dict[Target, Options] = {}
        for (target, options), report in zip(chosen, reports):
            for item in report.found:
                try:
                    child = item if item.kind else self._one(str(item))
                except Error as error:
                    ui.warn("{target}: can't follow {link}: {error}", target=str(target),
                            link=str(item), error=error)
                    continue
                if child in ran or child in found:
                    continue
                found[child] = Options(since=options.since, until=options.until)
        if not found:
            return []
        ui.info("{targets} linked from the listings", targets=_text.count(len(found), "target"))
        self._record(list(found.items()), follow=False)
        return self._run(list(found.items()), stop=stop, browser_pool=browser_pool,
                         depth=depth + 1)

    def _one(self, text: str) -> Target:
        """Resolve one target's text on any source, following a short link once."""
        return resolve(text, self.sources, follow=_follow)

    def _label_width(self, chosen: Sequence[tuple[Target, Options]]) -> int:
        """The width the board pads category labels to, so its rows line up.

        Args:
            chosen: the run's targets; their kinds and every category of their sources count.

        Returns:
            The longest label's length; 0 for no targets.
        """
        labels = [target.kind for target, _ in chosen]
        for code in {target.source for target, _ in chosen}:
            labels += [
                category.name
                for route in self.sources[code].routes
                for category in route.categories
            ]
        return max(map(len, labels), default=0)

def _validate_targets(
    sources: Mapping[str, type[Source]],
    categories: str | Sequence[str] | None,
    resolved: Sequence[Target],
) -> None:
    """Check `--only` and every folder template against the targets (`options.validate`)."""
    validate(sources, categories, resolved)


def _account(run: SourceRun) -> TargetSite:
    """The `Site` of a source's account, not of a target: what `connect` and `check` see."""
    return TargetSite(run, Target(f"{run.source.code}:", kind=ACCOUNT))


def _logged_in(run: SourceRun) -> bool:
    """Whether the source's login is kept; a source without a login always is."""
    login = run.source.login
    return login is None or login.check(_account(run))


def _required(source: Source) -> bool:
    """Whether the source lists nothing without a login."""
    return source.login is not None and source.login.required


def _require_auth(source: Source, run: SourceRun, target: Target) -> TargetReport | None:
    """Check a search's login before it lists: fail it when required, else warn when missing.

    Args:
        source: the search's source.
        run: its source run, whose account `Site` the login checks.
        target: the search.

    Returns:
        A failed report (after its `Failed` event) when the source needs a login it lacks;
        None when the search may go on.
    """
    if not _logged_in(run) and _required(source):
        failed = job.logged_out(target)
        job.tell(target, failed, run.settings.emit)
        return TargetReport(target, (), 0, error=failed)
    if not _logged_in(run):
        ui.warn("{code}: not logged in, using public access; run flnd login {code}",
                code=source.code)
    return None


def _log_dry(
    run: SourceRun, target: Target, options: Options, span: Range, *, is_query: bool,
) -> None:
    """List what a download of `target` would save: a log line and a `Planned` event each.

    A file is "saved" when its row or its file is already there, else "new". A
    search plans each target it finds, with no post and no path.

    Args:
        run: the source's run, whose settings hold the database and the event sink.
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
    folder = _home.target_folder(run.settings.home, target)
    db = run.settings.db
    for placed in job.plan(run, target, options):
        ui.info("{target}: {path}", target=str(target), path=placed.path)
        dest = folder.joinpath(*placed.path.split("/"))
        saved = ((db is not None and db.held(f"{run.source.code}:{placed.key}") is not None)
                 or dest.is_file())
        emit(Planned(target, placed.post.id, dest, "saved" if saved else "new"))


def _log_totals(report: Report) -> None:
    """Log a command's totals and its exit status at -v, for a log read after the run.

    Args:
        report: the command's report.
    """
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
    """Read one target's text and return it resolved by its source.

    1. Find the source (`_split_target`): by the code before `:` or `?`, or by a link's host.
    2. Let it resolve the rest (`Source.resolve`): the first route whose pattern matches.
    3. A link no route reads (`_Unmatched`, such as a short link) is followed once, when
       `follow` is given, and the link it leads to is resolved instead, by whichever source
       owns that one.

    Args:
        target_text: the target as typed; spaces around it are ignored.
        sources: the sources to resolve with, by code.
        follow: where a link leads (one HEAD request); None to never follow one.

    Returns:
        The resolved target, with its `kind` set.

    Raises:
        UsageError: the text is empty, names no known source, or no route reads it.
        NetworkError: a short link couldn't be followed.
    """
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
    """Split a target's text into its source and what that source resolves.

    The forms, in the order they are tried:
    - a link (`https://…`): the source whose domains it is on, and the whole link;
    - `<code>?<query>`, a `?` before any `:` and `/`: the source, and `?<query>`;
    - `<code>:<value>`: the source, and the value;
    - anything else (`example.com/example_user`): the source whose domains it is on.

    Args:
        cleaned: the target's text, stripped.
        sources: the sources, by code.

    Returns:
        The source, made from its class, and the text it resolves.

    Raises:
        UsageError: an unknown code, an empty value, or a text no source's domains own.
    """
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
    """The source whose domains own a link or a bare `host/path`, made.

    Args:
        cleaned: the target's text, stripped.
        sources: the sources, by code.

    Returns:
        The owning source.

    Raises:
        UsageError: no source's domains own it; the message lists each `<code>:` form.
    """
    found = owner(cleaned, sources)
    if found is None:
        choices = ", ".join(f"{code}:{cleaned}" for code in sources)
        raise UsageError(f'which source is "{cleaned}"? Write it as {choices}')
    return found()


def owner(target_text: str, sources: Mapping[str, type[Source]]) -> type[Source] | None:
    """Return the source whose domains `target_text` belongs to, or None.

    A built-in source's domains come from the manifest, so matching a link imports only the
    source that owns it. The built-ins are asked first: only a link none of them owns asks the
    other sources, which imports every plugin (`flnd.sources.Registry`).

    Args:
        target_text: a link, or a target written without its source.
        sources: the sources to ask, by code.

    Returns:
        The class of the source whose domains the link is on; None when no source's are.
    """
    from flnd.sources import _manifest

    for code, known in _manifest.SOURCES.items():
        if code in sources and _on(target_text, known["domains"]):  # type: ignore[arg-type]
            return sources[code]
    for code in sources:
        cls = None if code in _manifest.SOURCES else sources.get(code)
        if cls is not None and _on(target_text, cls.domains):
            return cls
    return None


def _on(target_text: str, domains: Sequence[str]) -> bool:
    """Whether a link (or a `host/path` with no scheme) is on one of `domains`, or under one.

    Args:
        target_text: the link; a text with no `/` and no scheme is never on a domain.
        domains: the domains, such as `example.com`; a subdomain matches too.

    Returns:
        True when the host is one of them or under one.
    """
    if not domains or ("/" not in target_text and not _is_url(target_text)):
        return False
    url = target_text if _is_url(target_text) else f"https://{target_text}"
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _is_url(target_text: str) -> bool:
    return target_text.lower().startswith(("http://", "https://"))


def _follow(url: str) -> str:
    """Where a short link leads, from one HEAD request (`_net.follow`).

    Args:
        url: the short link.

    Returns:
        The URL its redirects end at.

    Raises:
        NetworkError: no answer came; the message names the link redacted, since a link's
            query may carry a signature.
    """
    try:
        return _net.follow(url)
    except NetworkError as error:
        raise NetworkError(f"can't follow {_text.redact(url)}: {error}",
                           retryable=error.retryable) from None


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
        if not _logged_in(run) and _required(source):
            for planned in group:
                failed = job.logged_out(planned.target)
                job.tell(planned.target, failed, run_settings.emit)
                reports[planned.number] = TargetReport(planned.target, (), 0, error=failed)
            return
        if not _logged_in(run):
            ui.warn(
                "{code}: not logged in, using public access; run flnd login {code}",
                code=source.code,
            )
        with _written_back(run), _connected(run, _account(run)):
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
    """Write the cookies a site refreshed back to the vault when the block ends, however."""
    try:
        yield
    finally:
        _home.write_back(run.settings.home, run.source.code, run.session.jar)


@contextmanager
def _connected(run: SourceRun, account: Any) -> Iterator[None]:
    """Run the source's `connect` hook around a block, keeping what it gives as `run.client`.

    `connect` returns a context manager, plain or async (Telegram's Telethon client is async).
    A plain one is entered on this thread. An async one is entered and left on the run's loop
    (`run.run_async`), since its client lives there; an error in the block is handed to its
    `__aexit__`, which may swallow it, as `async with` would.

    Args:
        run: the source's run.
        account: the account `Site` the hook gets (`_account`).

    Yields:
        Nothing; the client is on `run.client` for the block.
    """
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
