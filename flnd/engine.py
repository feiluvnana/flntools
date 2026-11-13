"""The engine: flnd's five commands as methods, each giving the CLI or a script what it printed before.

`Engine` is the whole engine API (plan/phase-6/redesign.md, B1): `resolve` reads targets,
`download` and `sync` run them and return a `Report`, `login` and `logout` keep the logins. Every
decision a command makes is here, where a test reaches it without a terminal: reading the options'
text (`Range.parse`), refusing `--from` for a single post, each target's saved
settings (flnd/settings.py) and applying a changed one to the files already saved, `sync`'s repair
and tally (flnd/repair.py). flnd/cli.py only parses, asks `login`'s questions and prints.

How a run goes: targets are grouped by source only; each source gets one worker and its HTTP
clients, and runs its targets one after another through the job (flnd/job.py), each with its own
saved settings, while different sources run at the same time. It never asks a question, except
`shared.browser`'s one, to download Chromium, before any work starts.

Every `download` and `sync` writes a log of its own, `.flnd/log/<date>_<time>_<command>.log`, at
debug whatever the screen shows (plan/phase-7/layout.md): its start, each target's phases, every
line and row, the totals and the exit status. Logs older than `LOG_DAYS` are deleted as a run starts.

    engine = Engine(home=Path(".flnd"))
    report = engine.download(["ins:example_user", "https://x.com/example_handle"], categories="reels,tagged",
                             time="2026-01-01,", shrink=True)
    report = engine.sync()
    exit_status = 0 if report.ok else 1
"""

from __future__ import annotations

import inspect
import itertools
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from types import CodeType
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit

import httpx

from flnd import job, repair, settings
from flnd.job import TargetReport
from flnd.common import auth, compress, files, net
from flnd.common.compress import Compressor
from flnd.session import Options, Settings, single_targets, unknown_categories
from flnd.site import LoginSite, SourceRun, TargetSite, source_run
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

# What in a source's code means it can drive a browser: the `Site` members and helpers that open
# or use a page (`Site.page`, `goto`, `handoff`, `fetch(in_page=True)`, `watch_responses`,
# `meta_results`, `scroll`), and the `Page` methods that listen to responses or requests.
BROWSER_NAMES = frozenset({"page", "goto", "handoff", "in_page", "watch_responses", "meta_results",
                           "on_response", "on_request", "scroll", "browser"})


@dataclass(frozen=True)
class Report:
    """What `download` or `sync` did, one `TargetReport` per target in the order given: `0 if report.ok else 1`.

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
        """The targets that failed, in order: their route or a category raised, or their source couldn't start."""
        return tuple(target.target for target in self.targets if target.error is not None)

    @property
    def ok(self) -> bool:
        """Whether no target failed: the command's exit status is 0 when it is, and 1 otherwise."""
        return not self.failed


class Engine:
    """flnd's commands as methods: `resolve`, `download`, `sync`, `login` and `logout`.

        engine = Engine(home=Path(".flnd"))
        report = engine.download(["tik:example_creator"], categories="videos")
        engine.login("ins", "browser", browser="chrome")

    Targets of one source run one after another; different sources run at the same time.
    `download` and `sync` never ask questions; a source's own `login` asks through its `Login`.
    """

    def __init__(self, *, home: Path = Path(".flnd"), sources: Mapping[str, type[Source]] = SOURCES):
        """Make an engine; nothing is read or started until a method is called.

        Args:
            home: flnd's folder, which holds the downloads, the history, the logins and the saved settings.
            sources: the sources by code; tests give their own.

        A run has no download limit: it ends when the sources have nothing more, or on Ctrl-C.
        """
        self.home, self.sources = home, sources

    def resolve(self, texts: Sequence[str]) -> list[Target]:
        """Every target as its source reads it, stopping at the first bad one with `TargetError`.

            targets = engine.resolve(["ins:@example_user", "https://vm.tiktok.com/ZMabc/"])

        Args:
            texts: the targets as people write them: `code:value`, or a link.

        Raises:
            TargetError: a text is empty, names no source, or its source can't read it; it is a
                `ValueError`, with what to write instead.

        Only short links make a request, one HEAD each.
        """
        return [resolve(text, self.sources, follow=_follow) for text in texts]

    def download(self, targets: Sequence[Target | str], *, categories: str | Sequence[str] | None = None,
                 time: Range | str = Range(), shrink: bool | None = None) -> Report:
        """Download each target, with its saved `--shrink` or the one given, and say what was done.

            report = engine.download(["ins:example_user"], categories="reels,tagged", time="2026-01-01,",
                                     shrink=True)

        Args:
            targets: `Target`s, or texts that `resolve` reads; a text that can't be read raises
                before anything starts.
            categories: `--from`: names as a comma list, or a sequence of them; "all", or None,
                takes every category. Refused for a single post's link, which is downloaded whole.
            time: `--time`: a `Range`, or its text ("2026-01-01,2026-06-30"); `Range()` is no limit.
            shrink: `--shrink`: True shrinks each photo, video and audio file at media's balanced
                level in its own format, False (`--shrink off`) stops it, and None keeps the
                target's saved setting.

        Returns:
            One `TargetReport` per target, in the order given, each with the saved setting it used.

        Raises:
            ValueError: an option's text can't be read, `--from` names no category of these
                targets' sources or is given for a single post, or a target
                can't be read (`TargetError`); each message is what the CLI shows as a usage error.
                Raised before any file but the run log is read or written, and before any request but a short link's.
            OSError: `targets.db` or the history can't be read or written.

        `--shrink` given here is saved for each target, and used by every later `download` and
        `sync` of it; `--from` and `--time` are never saved. Turning it on shrinks the target's
        files already saved before anything is downloaded, so a folder is never half shrunk;
        that is slow, and replaces originals with their smaller copies. It is saved only once
        that is done, so a shrink cut short (Ctrl-C, a crash, a full disk) is finished by the
        next `download --shrink`. Turning it off touches no file.
        """
        with self._logged("download", targets=targets, categories=categories, time=time, shrink=shrink):
            # Every option is read before anything starts, in the CLI's order, so the first bad
            # one is the error.
            span = Range.parse(time) if isinstance(time, str) else time
            resolved = self._resolved(targets)
            names = self._categories(categories, resolved)
            options = Options(categories=names, since=span.since, until=span.until)
            log.info("Storage: {path}", path=(self.home / files.MEDIA).resolve())
            rows = settings.rows(self.home)
            chosen: list[tuple[Target, Options]] = []
            shown: list[str] = []
            turned_on: list[Target] = []
            for target in resolved:
                saved = settings.shrink(rows.get(settings.key(target), {}))
                shown.append(settings.announce(target, saved) if shrink is None else "")
                given = saved if shrink is None else shrink
                chosen.append((target, replace(options, shrink=given)))
                if shrink and not saved:
                    turned_on.append(target)
            settings.record(self.home, [pair for pair in chosen if pair[0] not in turned_on])
            if turned_on:
                self._apply(turned_on, dict(chosen))
            reports = self._run(chosen)
            _end_if_stopped()
            report = Report(tuple(replace(report, settings=text)
                                  for report, text in zip(reports, shown)))
            _log_totals(report)
            return report

    def sync(self, targets: Sequence[Target | str] = ()) -> Report:
        """Bring downloaded targets up to date: repair what was lost, fetch what's new, and sum it up.

            report = engine.sync()                              # every target downloaded so far
            report = engine.sync(["ins:example_user"])

        Args:
            targets: only these, as `Target`s or texts; when empty, every target in `targets.db`
                and every folder under `media/<code>/` that a row doesn't name.

        Returns:
            One `TargetReport` per target, with its repaired and missing counts.

        Raises:
            ValueError: a target can't be read (`TargetError`), before anything starts.
            OSError: the history or `targets.db` can't be opened or written.

        It changes no setting and takes each source's default categories. For each target, on its
        source's worker while other sources list: leftover `.part` files are removed and files
        that are missing, empty or broken are forgotten (broken ones deleted, which can't be
        undone); then what is new or was lost is downloaded with the target's saved settings.
        Then one line per target and the totals are logged. The history is read once per sync.
        The fetch makes network requests and can take hours.
        """
        with self._logged("sync", targets=targets):
            chosen = self._plan(self._resolved(targets))
            # Recorded again in today's form, changing no setting: a Phase 5 row is migrated here.
            settings.record(self.home, chosen)
            shown = [settings.announce(target, options.shrink) for target, options in chosen]
            folders = [files.target_folder(self.home, target) for target, _ in chosen]
            with self._history() as history:
                index = repair.index(history, folders)
            tallies = [repair.Tally(str(target), folder, index[folder])
                       for (target, _), folder in zip(chosen, folders)]
            log.info("Storage: {path}", path=(self.home / files.MEDIA).resolve())
            chosen = [(target, replace(options, stop_at_saved=True)) for target, options in chosen]
            reports = self._run(chosen, tallies=tallies)
            _end_if_stopped()
            with self._history() as history:
                for tally in tallies:
                    repair.count(history, tally)
            repair.summary(tallies)
            report = Report(tuple(replace(report, repaired=tally.repaired, missing=tally.missing,
                                          settings=text)
                                  for report, tally, text in zip(reports, tallies, shown)))
            _log_totals(report)
            return report

    def login(self, code: str, method: str, *, browser: str | None = None, profile: str | None = None,
              path: Path | str | None = None) -> str:
        """Log in to a source by one of its `login_methods`, and return the "logged in" message.

            engine.login("ins", "browser", browser="chrome")      # "ins: logged in from Chrome (14 cookies)"
            engine.login("ins", "file", path="~/Downloads/cookies.txt")
            engine.login("tlg", "qr")                              # asks through the source's `Login`

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

        Every credential is sealed in the vault, `.flnd/metadata/secrets.db`; nothing else is written.
        """
        if code not in self.sources:
            raise ValueError(f"unknown source '{code}'. Sources: {', '.join(self.sources)}")
        source = self.sources[code]()
        if method not in source.login_methods:
            raise ValueError(f"{code} logs in by {', '.join(source.login_methods)}, not {method!r}")
        if code not in self.cookie_logins:
            # A source with its own login (tlg) asks its own questions, through an asking `Login`.
            with source_run(Settings(home=self.home, history={}, interactive=True), source) as run:
                source.login(LoginSite(run), method)
            return ""
        if method == "file":
            if path is None:
                raise ValueError("a file login needs the cookie file's path")
            file = Path(path).expanduser()
            return auth.store(source, auth.from_file(source, file), home=self.home, origin=file.name)
        if browser is None:
            raise ValueError("a browser login needs the browser to read cookies from")
        jar = auth.from_browser(source, browser=browser, profile=profile)
        return auth.store(source, jar, home=self.home, origin=auth.BROWSERS.get(browser, browser))

    def logout(self, codes: Iterable[str]) -> list[str]:
        """Remove the logins of these sources, or of every one for `["all"]`, and return the codes that had one.

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
        """Each source with something saved, by code: "logged in", or "api key saved" for an API key alone.

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
        """The browsers a cookie login reads, by `login`'s `browser=` name, each with the name a person knows.

            engine.browsers        # {"chrome": "Chrome", "firefox": "Firefox", …}
        """
        return dict(auth.BROWSERS)

    @property
    def cookie_logins(self) -> tuple[str, ...]:
        """The codes of the sources that log in with cookies (`browser` or `file`), not with their own login.

            engine.cookie_logins   # ("tik", "fbk", "ins", "twt", "loi")

        `login` asks such a source for a browser and profile, or a file; any other runs its own.
        """
        return tuple(code for code, source in self.sources.items() if source.login is Source.login)

    def _resolved(self, targets: Sequence[Target | str]) -> list[Target]:
        """Each target, a text read by `resolve`, each written to the run log as it was read."""
        resolved = []
        for given in targets:
            target = given if isinstance(given, Target) else self.resolve([given])[0]
            log.debug("resolve {given}: {target} ({kind})", given=given, target=target, kind=target.kind)
            resolved.append(target)
        return resolved

    @contextmanager
    def _logged(self, command: str, **given: Any) -> Iterator[None]:
        """Write the block's run to its own log in `.flnd/log/`, after deleting the logs older than `LOG_DAYS`.

        The log opens with the version, the command and the options as given, and ends with the
        exit status the CLI gives: 2 for a usage error (`ValueError`), 130 for Ctrl-C, 1 for any
        other exception, each with its cause; a finished run writes its own (`_log_totals`).
        """
        from flnd import __version__

        folder = self.home / files.LOGS
        _prune(folder)
        stem = f"{datetime.now():%Y-%m-%d_%H%M%S}_{command}"
        # Two runs started in one second each get a log of their own: the second is `…_download-2.log`.
        path = next(path for number in itertools.count(1)
                    if not (path := folder / (f"{stem}.log" if number == 1 else f"{stem}-{number}.log")).exists())
        with log.record(path):
            options = ", ".join(f"{name}={value!r}" for name, value in given.items() if value not in (None, (), Range()))
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

    def _categories(self, categories: str | Sequence[str] | None, targets: Sequence[Target]) -> tuple[str, ...] | None:
        """`--from`'s names, each once, checked against these targets; None without `--from`.

        Raises:
            ValueError: no source here has a name, or a target is a single post.
        """
        if categories is None:
            return None
        items = [categories] if isinstance(categories, str) else list(categories)
        names = tuple(dict.fromkeys(name.strip() for item in items for name in item.split(",") if name.strip()))
        sources = list(dict.fromkeys(self.sources[target.source] for target in targets)) or list(self.sources.values())
        if problem := unknown_categories(names, sources) or single_targets(targets, self.sources):
            raise ValueError(problem)
        return names

    def _plan(self, targets: Sequence[Target]) -> list[tuple[Target, Options]]:
        """Each target to sync, with its saved settings and each source's default categories; nothing is written.

        Args:
            targets: only these; when empty, every recorded target and every unrecorded folder
                under `media/<code>/`, in that order.
        """
        rows = settings.rows(self.home)
        if not targets:
            targets = [Target(code, row["kind"], value) for key, row in rows.items()
                       for code, value in [key.split(":", 1)] if code in self.sources]
            targets += list(self._unrecorded(targets))
        return [(target, Options(shrink=settings.shrink(rows.get(settings.key(target), {})))) for target in targets]

    def _unrecorded(self, known: Sequence[Target]) -> Iterator[Target]:
        """Folders under `media/<code>/` no row names: downloaded before Phase 5 recorded settings."""
        seen = {files.target_folder(self.home, target) for target in known}
        media = self.home / files.MEDIA
        for code in self.sources:
            for folder in sorted((media / code).iterdir()) if (media / code).is_dir() else ():
                if folder.is_dir() and folder not in seen:
                    try:
                        yield self.resolve([f"{code}:{folder.name}"])[0]
                    except TargetError as error:
                        log.warn("{folder}: not synced: {error}", folder=f"{code}/{folder.name}", error=error)

    def _apply(self, targets: Sequence[Target], chosen: Mapping[Target, Options]) -> None:
        """Shrink the files already saved of each target `download --shrink` turns it on for, then save its setting.

        Args:
            targets: the targets whose `--shrink` this download turns on.
            chosen: each target's options, as `record` takes them.

        Raises:
            OSError: the history or `targets.db` can't be written.
        """
        folders = {target: files.target_folder(self.home, target) for target in targets}
        compressor = Compressor()
        with self._history() as history:
            index = repair.index(history, list(folders.values()))
            for target, folder in folders.items():
                compress.apply(history, index[folder], compressor, label=str(target))
                # Saved only now that every file is shrunk: saved before, a shrink cut short would never
                # be finished, since the next `download --shrink` would see no change.
                settings.record(self.home, [(target, chosen[target])])

    def _run(self, chosen: Sequence[tuple[Target, Options]], *,
             tallies: Sequence[repair.Tally] = ()) -> list[TargetReport]:
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
        if any(needs_browser(self.sources[target.source], target, options)
               for target, options in chosen):
            _offer_browser()
        groups: dict[str, list[int]] = {}
        for number, (target, _) in enumerate(chosen):
            groups.setdefault(target.source, []).append(number)
        # Filled by every source's worker at once, each under its own targets' numbers, and read
        # only once they have all ended: one assignment per key needs no lock.
        reports: dict[int, TargetReport] = {}
        with self._history() as history, self._targets() as saved, _stop_waits_on_ctrl_c(), \
                log.group("download") as rows:
            run_settings = Settings(home=self.home, history=history, targets=saved)
            work = [(self.sources[code](),
                     [_Planned(number, *chosen[number], rows.add(str(chosen[number][0])),
                               tallies[number] if tallies else None)
                      for number in numbers])
                    for code, numbers in groups.items()]
            for result in worker.as_completed(partial(_run_site, run_settings, reports), work,
                                              concurrency=max(len(work), 1)):
                if not result.ok:
                    _fail_unreached(result.item, result.error, reports)
        return [reports[number] for number in range(len(chosen))]

    def _history(self) -> kv.Store:
        """The download history, opened for a `with` block."""
        return kv.open(self.home / files.HISTORY, table=HISTORY_TABLE)

    def _targets(self) -> kv.Store:
        """`targets.db`, opened once for a run's `with` block."""
        return kv.open(self.home / files.TARGETS, table=settings.TARGETS_TABLE)


def _end_if_stopped() -> None:
    """Raise `KeyboardInterrupt` once a first Ctrl-C stopped the run: the CLI exits 130."""
    # What finished is recorded by now; the command ends as Ctrl-C always does.
    if net.stopping.is_set():
        raise KeyboardInterrupt


def _log_totals(report: Report) -> None:
    """Write a finished run's totals and exit status to its log: 1 when a target failed, else 0."""
    log.debug("totals: {files} written, {skipped} skipped, {failed} of {targets} failed; exit {status}",
              files=report.files, skipped=sum(target.skipped for target in report.targets),
              failed=len(report.failed), targets=len(report.targets), status=0 if report.ok else 1)


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


def resolve(text: str, sources: Mapping[str, type[Source]], *, follow: Callable[[str], str] | None = None) -> Target:
    """Read one target as people write it, and return it resolved by its source.

        resolve("ins:@example_user", SOURCES)                     # Target("ins", "user", "example_user")
        resolve("https://x.com/example_handle", SOURCES)          # Target("twt", "user", "example_handle")

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
    code, colon, value = cleaned.partition(":")
    if colon and not _is_url(cleaned):
        code = code.strip().lower()
        if code not in sources:
            raise TargetError(f"unknown source '{code}'. Sources: {', '.join(sources)}")
        source, cleaned = sources[code](), value.strip()
        if not cleaned:
            raise TargetError(f"{source.code}: can't read {value!r}; write it like {', '.join(source.forms)}")
    else:
        found = owner(cleaned, sources)
        if found is None:
            choices = ", ".join(f"{code}:{cleaned}" for code in sources)
            raise TargetError(f'which source is "{cleaned}"? Write it as {choices}')
        source = found()
    try:
        return source.resolve(cleaned)
    except TargetError as miss:
        # Only a link no route matched carries `url`; a refusal (an `unsupported` link) is final.
        if follow is None or miss.url is None:
            raise
        url = miss.url
    # Followed once only: a second miss is the refusal, listing the forms.
    return source.resolve(follow(url))


def owner(text: str, sources: Mapping[str, type[Source]]) -> type[Source] | None:
    """The source whose domains a URL belongs to, or None: `owner("https://x.com/example_handle", SOURCES)` is Twitter.

    Args:
        text: the text as typed; only a URL, with or without a scheme, can have an owner.
        sources: the sources to look through, in order; the first whose domain matches wins.
    """
    return next((source for source in sources.values() if _on(text, source.domains)), None)


def _on(text: str, domains: Sequence[str]) -> bool:
    """Whether `text` is a URL, with or without a scheme, on one of `domains` or their subdomains."""
    # Without a scheme, only a slash tells a link (instagram.com/example_user) from a name (example.user).
    if not domains or ("/" not in text and not _is_url(text)):
        return False
    host = (urlsplit(text if _is_url(text) else f"https://{text}").hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


@contextmanager
def _stop_waits_on_ctrl_c() -> Iterator[None]:
    """Make the first Ctrl-C a graceful stop and the second an immediate one, in every thread (D8).

    The first sets `net.stopping` and raises nothing: every rate-limit wait ends at once, each job
    stops listing and lets its running downloads finish (flnd/job.py), and the command raises
    `KeyboardInterrupt` once they have. The second sets `net.stopping_now`, which stops the
    downloads at their next chunk with their resumable parts kept, and does what Ctrl-C always does.
    """
    net.stopping.clear()
    net.stopping_now.clear()
    # Python sets signal handlers only on the main thread, so from any other thread (a script's worker)
    # Ctrl-C keeps its default behaviour rather than raising here.
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
        # A Python handler is callable and is called as it would have been; SIG_DFL and SIG_IGN are not,
        # so the Ctrl-C raises KeyboardInterrupt itself, as it does by default.
        if callable(previous):
            previous(number, frame)
        else:
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def needs_browser(source: type[Source], target: Target, options: Options) -> bool:
    """Whether downloading `target` can open a browser, read from the code it would run.

        needs_browser(Instagram, Target("ins", "user", "example_user"), Options())     # True

    Args:
        source: the target's source.
        target: the target; its kind names its route.
        options: the run's options, whose `--from` decides which categories run.

    The code read is the route and its wanted categories, and every function and method they name
    that lives in the source's module or in `flnd.sources.common`, followed as far as it goes. It
    needs a browser when any of them names one of `BROWSER_NAMES`. So no `Source` field says it, and
    it can't drift from the code: a source that calls `site.goto` asks, and one that only calls
    `site.fetch` doesn't. It reads names, not what runs, so a branch never taken still counts: it
    may ask once when no page ends up opening.
    """
    route = next((route for route in source.routes if route.kind == target.kind), None)
    if route is None:
        return False
    starts: list[Any] = [route.function]
    if route.categories:
        categories = job.wanted(source, route, options)
        starts = [route.function, *(getattr(source, category.name) for category in categories)] \
            if categories else []
    return any(_names_browser(function, source) for function in starts)


def _names_browser(function: Any, source: type[Source]) -> bool:
    """Whether `function`, or what it calls in the source's module or `flnd.sources.common`, names a browser member."""
    home = source.__module__
    seen: set[CodeType] = set()
    stack: list[tuple[CodeType, dict[str, Any]]] = []

    def follow(found: Any) -> None:
        # A function, and the functions it closes over (a method made by a factory calls those).
        if not inspect.isfunction(found):
            return
        stack.append((found.__code__, found.__globals__))
        for cell in found.__closure__ or ():
            try:
                inner = cell.cell_contents
            # A cell not filled yet (a name assigned after the function was made) holds nothing to read.
            except ValueError:
                continue
            if inspect.isfunction(inner):
                stack.append((inner.__code__, inner.__globals__))

    follow(function)
    while stack:
        code, scope = stack.pop()
        if code in seen:
            continue
        seen.add(code)
        # Names are globals and attributes (`site.goto`); a keyword argument (`in_page=`) is a constant.
        keywords = {name for constant in code.co_consts if isinstance(constant, tuple)
                    for name in constant if isinstance(name, str)}
        if BROWSER_NAMES & (set(code.co_names) | keywords):
            return True
        stack += [(constant, scope) for constant in code.co_consts if isinstance(constant, CodeType)]
        for name in code.co_names:
            for found in (scope.get(name), getattr(source, name, None)):
                module = getattr(found, "__module__", None) or ""
                if module == home or module.startswith("flnd.sources.common"):
                    follow(found)
    return False


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
    """Where a short link leads, from one HEAD request; a network failure is a `TargetError` naming the link."""
    # Only a network failure is turned into the target's message; any other error is a bug and propagates.
    try:
        return net.follow(url)
    except httpx.HTTPError as error:
        raise TargetError(f"can't follow {url}: {error}") from None


@dataclass(frozen=True)
class _Planned:
    """One target of a source's group: its place in the run, its options, its row and its repair."""

    number: int
    target: Target
    options: Options
    row: Any
    # `sync` only: the tally its folder's repair fills, just before its listing.
    tally: repair.Tally | None


def _run_site(run_settings: Settings, reports: dict[int, TargetReport],
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
                planned.row.fail()
                reports[planned.number] = TargetReport(planned.target, (), 0,
                                                       error="not logged in; run flnd login")
            return
        if not source.logged_in(account):
            log.warn("{code}: not logged in, using public access; run flnd login", code=source.code)
        with _written_back(run), _connected(run, account):
            for planned in group:
                options = _repaired(run_settings.history, planned)
                reports[planned.number] = job.download(run, planned.target, planned.row, options)


def _repaired(history: MutableMapping[str, Any], planned: _Planned) -> Options:
    """Repair a target's folder right before its listing under `sync`, and give its options."""
    tally = planned.tally
    if tally is None or net.stopping.is_set():
        return planned.options
    log.debug("{target}: repair", target=tally.target)
    with log.under(planned.row):
        repair.repair(history, tally)
    # A target with a lost or broken file lists to the end, so a file deeper than the saved-run
    # stop is reached and saved again; the others stop at saved posts.
    return replace(planned.options, stop_at_saved=planned.options.stop_at_saved and not tally.lost)


def _fail_unreached(work: tuple[Source, list[_Planned]], error: BaseException | None,
                    reports: dict[int, TargetReport]) -> None:
    """Fail each target of a group that stopped before or between its targets."""
    source, group = work
    log.error("{source}: {error}", source=source.code, error=error)
    for planned in group:
        # An ended row keeps its end; any other would end at ok when `log.group` closes.
        if not planned.row.finished:
            planned.row.caption = str(error)
            planned.row.fail()
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
    # Every error, Ctrl-C included, goes to the source's `__aexit__` first, which may suppress it by returning
    # true; if it doesn't, the error is raised again unchanged.
    except BaseException as error:
        if not run.run_async(manager.__aexit__(type(error), error, error.__traceback__)):
            raise
    else:
        run.run_async(manager.__aexit__(None, None, None))
