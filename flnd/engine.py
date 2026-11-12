"""The engine: flnd's five commands as methods, each giving the CLI or a script what it printed before.

`Engine` is the whole engine API (plan/phase-6/redesign.md, B1): `resolve` reads targets,
`download` and `sync` run them and return a `Report`, `login` and `logout` keep the logins. Every
decision a command makes is here, where a test reaches it without a terminal: reading the options'
text (`Range.parse`, `Choice.parse`), refusing `--from` for a single post, each target's saved
settings (flnd/settings.py) and applying a changed one to the files already saved, `sync`'s repair
and tally (flnd/repair.py), and grouping targets by their settings. flnd/cli.py only parses,
asks `login`'s questions and prints.

How a run goes: targets are grouped by source; each source gets one worker, one HTTP client,
back-off and pacer, and runs its targets one after another through the job (flnd/job.py), with a
person's pause between them, while different sources run at the same time. It never asks a
question, except `shared.browser`'s one, to download Chromium, before any work starts.

    engine = Engine(home=Path(".flnd"))
    report = engine.download(["ins:example_user", "https://x.com/example_handle"], categories="reels,tagged",
                             time="2026-01-01,", image="webp,mid")
    report = engine.sync()
    exit_status = 0 if report.ok else 1
"""

from __future__ import annotations

import inspect
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from types import CodeType
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit

import httpx

from flnd import job, repair, settings
from flnd.common import auth, compress, files, labels, net
from flnd.common.compress import KINDS, Choice, Compressor
from flnd.common.labels import Labeller
from flnd.session import Options, Settings, single_targets, unknown_categories
from flnd.site import LoginSite, SourceRun, TargetSite, source_run
from flnd.source import LoginError, Range, Source, Target, TargetError
from flnd.sources import SOURCES
from shared import browser, kv, log, worker

__all__ = ["Engine", "Report", "TargetReport"]

HISTORY_FILE = "history.db"
HISTORY_TABLE = "downloads"
# The target a source's login and `connect` see: the account, not anything to download.
ACCOUNT = "account"
# What `logout` takes for every source.
ALL = "all"
# The cookie logins every source without its own login has, by `login`'s method name.
COOKIE_METHODS = ("browser", "file")

# What in a source's code means it can drive a browser: the `Site` members and helpers that open
# or use a page (`Site.page`, `goto`, `handoff`, `fetch(in_page=True)`, `watch_responses`,
# `meta_results`, `scroll`), and the `Page` methods that listen to responses or requests.
BROWSER_NAMES = frozenset({"page", "goto", "handoff", "in_page", "watch_responses", "meta_results",
                           "on_response", "on_request", "scroll", "browser"})

# Pauses between targets read time through this, so tests can replace it.
_sleep = time.sleep

Row = Any


@dataclass(frozen=True)
class TargetReport:
    """What one command did for one target: `report.targets[0].written`.

        TargetReport(Target("ins", "user", "example_user"), written=(Path("…/a.jpg"),), skipped=3)
    """

    # The target, as `resolve` gave it.
    target: Target
    # Each file written, at its path after compressing.
    written: tuple[Path, ...]
    # Files already saved, or filtered out, that weren't downloaded again.
    skipped: int
    # `sync` only: files that were missing, empty or broken and were downloaded again.
    repaired: int = 0
    # `sync` only: files the history names that the site no longer lists.
    missing: int = 0
    # The saved settings this run used, as shown at its start: "--image webp,mid (saved)"; "" for none.
    settings: str = ""
    # Why the target failed, as logged; None when it didn't.
    error: str | None = None


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

    Targets of one source run one after another, with a pause between them; different sources
    run at the same time. `download` and `sync` never ask questions; a source's own `login` asks
    through its `Login`.
    """

    # What `login` raises for a login that didn't work, so a caller can catch it from the engine:
    # `except Engine.LoginError`. It is `flnd.source.LoginError`, which sources raise too.
    LoginError = LoginError

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
                 time: Range | str = Range(), image: Choice | str | None = None, video: Choice | str | None = None,
                 audio: Choice | str | None = None, classify: bool | str | None = None) -> Report:
        """Download each target, with its saved settings and the ones given on top, and say what was done.

            report = engine.download(["ins:example_user"], categories="reels,tagged", time="2026-01-01,",
                                     image="webp,mid")

        Args:
            targets: `Target`s, or texts that `resolve` reads; a text that can't be read raises
                before anything starts.
            categories: `--from`: names as a comma list, or a sequence of them; "all", or None,
                takes every category. Refused for a single post's link, which is downloaded whole.
            time: `--time`: a `Range`, or its text ("2026-01-01,2026-06-30"); `Range()` is no limit.
            image: `--image`: a `Choice`, its text ("webp,mid"), "off" to turn the saved one off,
                or None to keep the target's saved setting.
            video: `--video`, as `image`.
            audio: `--audio`, as `image`.
            classify: `--classify`: True (or "on") labels each photo and video and links it into
                the target's `labels/<label>/`, the files already saved included; "off" (or
                False) turns it off and deletes `labels/`; None keeps the saved setting.

        Returns:
            One `TargetReport` per target, in the order given, each with the saved settings it used.

        Raises:
            ValueError: an option's text can't be read, `--from` names no category of these
                targets' sources or is given for a single post, `classify` is none of the above, or a target
                can't be read (`TargetError`); each message is what the CLI shows as a usage error.
                Raised before any file is read or written, and before any request but a short link's.
            OSError: `targets.db` or the history can't be read or written.

        A setting given here is saved for each target, and used by every later `download` and
        `sync` of it; `--from` and `--time` are never saved. A setting that changes is applied to
        the target's files already saved before anything is downloaded, so a folder is never
        half one setting and half another; that is slow, and replaces originals with their
        smaller copies. It is saved only once that is done, so an apply cut short (Ctrl-C, a
        crash, a full disk) is finished by the next download with the same option. Targets
        whose settings then differ run apart, one group after another. `classify` given on labels
        the saved files that have no labels yet, even when it was on already, so files an earlier
        run couldn't label (no model then) are labelled now; the first time in a process that may
        ask to install ONNX Runtime and to download the model, in a terminal.
        """
        # Read every option before anything starts, in the CLI's order, so the first bad one is the error.
        span = Range.parse(time) if isinstance(time, str) else time
        given = _given(image=image, video=video, audio=audio)
        switch = _switch(classify)
        resolved = [target if isinstance(target, Target) else self.resolve([target])[0] for target in targets]
        names = self._categories(categories, resolved)
        options = Options(categories=names, since=span.since, until=span.until)
        log.info("Storage: {path}", path=(self.home / "downloads").resolve())
        rows = settings.rows(self.home)
        chosen: list[tuple[Target, Options]] = []
        shown: list[str] = []
        changes: list[tuple[Target, str, Choice]] = []
        for target in resolved:
            row = rows.get(settings.key(target), {})
            saved = settings.choices(row)
            on = settings.classify(row) if switch is None else switch
            shown.append(settings.announce(target, {name: choice for name, choice in saved.items()
                                                    if name not in given}, classify=on and switch is None))
            chosen.append((target, replace(_with(options, {**saved, **given}), classify=on)))
            changes += [(target, name, choice) for name, choice in given.items()
                        if choice is not None and choice != saved.get(name)]
        if switch is False:
            # `labels/` holds second names of the files; a file whose only copy is there goes back
            # first, so this loses none. The rows keep their labels.
            views = [folder for folder in (files.target_folder(self.home, target) for target in resolved)
                     if (folder / labels.VIEWS).exists()]
            if views:
                with self._history() as history:
                    index = repair.index(history, views)
                    for folder in views:
                        labels.remove(folder, index[folder])
        # The model's questions come now, before any work, as Chromium's does. A no keeps the saved
        # setting (it is this run that goes without), so the next run asks again.
        ready = _offer_labels(chosen)
        changed = {target for target, _, _ in changes} | (set(resolved) if switch and ready else set())
        settings.record(self.home, [(target, options) for target, options in chosen if target not in changed],
                        given=given)
        if changed:
            self._apply(changes, dict(chosen), given, labelled=resolved if switch else (), ready=ready)
        reports = self._fetch(_for_run(chosen, ready))
        return Report(tuple(replace(report, settings=text) for report, text in zip(reports, shown)))

    def sync(self, targets: Sequence[Target | str] = ()) -> Report:
        """Bring downloaded targets up to date: repair what was lost, fetch what's new, and sum it up.

            report = engine.sync()                              # every target downloaded so far
            report = engine.sync(["ins:example_user"])

        Args:
            targets: only these, as `Target`s or texts; when empty, every target in `targets.db`
                and every folder under `downloads/<code>/` that a row doesn't name.

        Returns:
            One `TargetReport` per target, with its repaired and missing counts.

        Raises:
            ValueError: a target can't be read (`TargetError`), before anything starts.
            OSError: the history or `targets.db` can't be opened or written.

        It changes no setting and takes each source's default categories. For each target, in
        order: leftover `.part` files are removed and files that are missing, empty or broken are
        forgotten (broken ones deleted, which can't be undone); then what is new or was lost is
        downloaded with the target's saved settings; then, with `--classify` saved, its `labels/`
        links are rebuilt from the history (missing ones made, ones whose file is gone removed;
        files already saved aren't classified again); then one line per target and the totals are
        logged. The history is read once per sync (fixes #12). The fetch makes network requests
        and can take hours.
        """
        resolved = [target if isinstance(target, Target) else self.resolve([target])[0] for target in targets]
        chosen = self._plan(resolved)
        # Recorded again in today's form, changing no setting: a Phase 5 row is migrated here.
        settings.record(self.home, chosen)
        shown = [settings.announce(target, {name: getattr(options, name) for name in settings.SAVED},
                                   classify=options.classify)
                 for target, options in chosen]
        tallies = [repair.Tally(str(target)) for target, _ in chosen]
        folders = [files.target_folder(self.home, target) for target, _ in chosen]
        views = [folder for (_, options), folder in zip(chosen, folders) if options.classify]
        ready = _offer_labels(chosen)
        with self._history() as history:
            index = repair.index(history, folders)
            for folder in views:
                # A file deleted by hand whose only copy is now its link in `labels/` goes back first,
                # so repair doesn't count it lost and the fetch doesn't download it again.
                labels.restore(folder, index[folder])
            for tally, folder in zip(tallies, folders):
                repair.repair(history, folder, index[folder], tally)
        log.info("Storage: {path}", path=(self.home / "downloads").resolve())
        reports = self._fetch(_for_run(chosen, ready), sync=True)
        with self._history() as history:
            # `labels/` is rebuilt, never trusted: links a deleted file left go, and missing ones
            # come back. It needs no model, so it runs even when classifying is off for this run.
            if views:
                rows, labeller = repair.index(history, views), Labeller()
                for folder in views:
                    labels.rebuild(folder, rows[folder], labeller)
            for tally, folder in zip(tallies, folders):
                repair.count(history, folder, tally)
        repair.summary(tallies)
        return Report(tuple(replace(report, repaired=tally.repaired, missing=tally.missing, settings=text)
                            for report, tally, text in zip(reports, tallies, shown)))

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
            Engine.LoginError: the login didn't work, with what to do instead.
            ValueError: the source or method is unknown, or the method's answer is missing.

        Every credential is sealed in the vault, `.flnd/secrets.db`; nothing else is written.
        """
        if code not in self.sources:
            raise ValueError(f"unknown source '{code}'. Sources: {', '.join(self.sources)}")
        source = self.sources[code]()
        if method not in source.login_methods:
            raise ValueError(f"{code} logs in by {', '.join(source.login_methods)}, not {method!r}")
        if code not in self.cookie_logins:
            # A source with its own login (tlg) asks its own questions, through an asking `Login`.
            with source_run(self._settings(Options(), {}, interactive=True), source) as run:
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
    def cookie_logins(self) -> tuple[str, ...]:
        """The codes of the sources that log in with cookies (`browser` or `file`), not with their own login.

            engine.cookie_logins   # ("tik", "fbk", "ins", "twt", "loi")

        `login` asks such a source for a browser and profile, or a file; any other runs its own.
        """
        return tuple(code for code, source in self.sources.items() if source.login is Source.login)

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
                under `downloads/<code>/`, in that order.
        """
        rows = settings.rows(self.home)
        if not targets:
            targets = [Target(code, row["kind"], value) for key, row in rows.items()
                       for code, value in [key.split(":", 1)] if code in self.sources]
            targets += list(self._unrecorded(targets))
        return [(target, replace(_with(Options(), settings.choices(rows.get(settings.key(target), {}))),
                                 classify=settings.classify(rows.get(settings.key(target), {}))))
                for target in targets]

    def _unrecorded(self, known: Sequence[Target]) -> Iterator[Target]:
        """Folders under `downloads/<code>/` no row names: downloaded before Phase 5 recorded settings."""
        seen = {files.target_folder(self.home, target) for target in known}
        downloads = self.home / "downloads"
        for code in self.sources:
            for folder in sorted((downloads / code).iterdir()) if (downloads / code).is_dir() else ():
                if folder.is_dir() and folder not in seen:
                    try:
                        yield self.resolve([f"{code}:{folder.name}"])[0]
                    except TargetError as error:
                        log.warn("{folder}: not synced: {error}", folder=f"{code}/{folder.name}", error=error)

    def _apply(self, changes: Sequence[tuple[Target, str, Choice]], chosen: Mapping[Target, Options],
               given: Iterable[str], *, labelled: Sequence[Target] = (), ready: bool = True) -> None:
        """Bring each changed target's files already saved to its new settings, then save its settings.

        Args:
            changes: (target, `settings.SAVED` name, new choice) for each compress setting `download` changes.
            chosen: each target's options, saved settings and given ones together, as `record` takes them.
            given: the `SAVED` names given, for `record`.
            labelled: the targets `--classify` was given for: their saved files without labels are
                labelled, and their views rebuilt.
            ready: whether `Media.labels` is ready (`_offer_labels`); without it, nothing is
                labelled and the views are only rebuilt.

        Compressing comes first, so the labels and links are of the files kept. A target with
        `--classify` saved has its views rebuilt after a compress too: a file compressed into a
        new format has a new name, and the old name's link would hold the original.

        Raises:
            OSError: the history or `targets.db` can't be written.
        """
        folders = {target: files.target_folder(self.home, target)
                   for target in [*(target for target, _, _ in changes), *labelled]}
        compressor, labeller = Compressor(), Labeller()
        with self._history() as history:
            index = repair.index(history, list(folders.values()))
            for target in folders:
                for changed, name, choice in changes:
                    if changed == target:
                        kind = next(kind for kind, spec in KINDS.items() if spec.option == name)
                        compress.apply(history, index[folders[target]], kind, choice, compressor, label=str(target))
                if chosen[target].classify:
                    # The rows again, as the compress above may have moved files and rewritten them.
                    rows = [(key, history[key]) for key, _ in index[folders[target]] if key in history]
                    if ready:
                        labels.apply(history, rows, folders[target], labeller, label=str(target))
                    else:
                        labels.rebuild(folders[target], rows, labeller)
                # Saved only now that every file has it: saved before, an apply cut short would never be
                # finished, since the next download with the same option would see no change.
                settings.record(self.home, [(target, chosen[target])], given=given)

    def _fetch(self, chosen: Sequence[tuple[Target, Options]], *, sync: bool = False) -> list[TargetReport]:
        """Run the targets, those with the same options together, and give their reports in the order of `chosen`.

        Each category that listed to its end is recorded as complete in `targets.db` (A9 rule 3);
        with `sync`, a category with that record stops after 5 saved posts in a row.
        """
        groups: dict[Options, list[int]] = {}
        for number, (_, options) in enumerate(chosen):
            groups.setdefault(options, []).append(number)
        reports: dict[int, TargetReport] = {}
        for options, numbers in groups.items():
            ran = self._run([chosen[number][0] for number in numbers], options, sync=sync)
            for number, (report, complete) in zip(numbers, ran):
                reports[number] = report
                settings.complete(self.home, report.target, complete)
            if net.stopping.is_set():
                # A first Ctrl-C stopped the run gracefully (D8): what finished is recorded above,
                # and the command ends as Ctrl-C always does, which the CLI exits 130 for.
                raise KeyboardInterrupt
        return [reports[number] for number in range(len(chosen))]

    def _run(self, targets: Sequence[Target], options: Options, *,
             sync: bool = False) -> list[tuple[TargetReport, tuple[str, ...]]]:
        """Download every target with one set of options, and give each one's report and complete categories.

            [(report, complete)] = engine._run(engine.resolve(["ins:example_user"]), Options(categories=("reels",)))

        Args:
            targets: the targets to download, as `resolve` gives them.
            options: what to take from each target and what to do with the files.
            sync: stop a `newest_first` category at saved posts when its last listing was complete.

        Returns:
            One (report, the categories that listed to their end) per target, in the order given.

        When a target's source can open a browser (`needs_browser`) and Google Chrome is missing,
        the "download Chromium?" question is asked once, before anything starts.

        Each target ends its row at ok or error; one failing never stops the others. A whole
        source that can't start (its `logged_in` or `connect` raises) fails each of its targets. A
        rate limit is waited out, however long it takes, and isn't a failure; Ctrl-C ends the wait.

        The first Ctrl-C stops gracefully (D8): listing stops, the running downloads finish,
        no target starts, and each target not done ends as stopped (`job.STOPPED`); this then
        returns, with `net.stopping` still set. The second stops at once with `KeyboardInterrupt`.
        """
        groups: dict[str, list[int]] = {}
        for number, target in enumerate(targets):
            groups.setdefault(target.source, []).append(number)
        # The one question `flnd download` can ask, before any work starts (cli.md, "Chrome only").
        if any(needs_browser(self.sources[target.source], target, options) for target in targets):
            _offer_browser()
        # Filled by every source's worker thread at once, each under its own targets' numbers, and read
        # only once they have all ended: one assignment per key needs no lock.
        done: dict[int, tuple[TargetReport, tuple[str, ...]]] = {}
        with self._history() as history, _stop_waits_on_ctrl_c(), log.group("download") as rows:
            run_settings = self._settings(options, history)
            work = [(self.sources[code](), [(number, targets[number], rows.add(str(targets[number])))
                                            for number in numbers])
                    for code, numbers in groups.items()]
            for result in worker.as_completed(partial(_run_group, run_settings, done, sync), work,
                                              concurrency=max(len(work), 1)):
                if result.ok:
                    continue
                # The whole group stopped before or between its targets: the targets it never reached
                # failed too, or `log.group` would end their rows at ok.
                source, group_rows = result.item
                log.error("{source}: {error}", source=source.code, error=result.error)
                for number, target, row in group_rows:
                    if not row.finished:
                        row.caption = str(result.error)
                        row.fail()
                    if number not in done:
                        done[number] = (TargetReport(target, (), 0, error=str(result.error)), ())
        return [done[number] for number in range(len(targets))]

    def _history(self) -> kv.Store:
        """The download history, opened for a `with` block."""
        return kv.open(self.home / HISTORY_FILE, table=HISTORY_TABLE)

    def _settings(self, options: Options, history: MutableMapping[str, Any], *,
                  interactive: bool = False) -> Settings:
        return Settings(home=self.home, options=options, history=history, interactive=interactive)


def _given(**values: Choice | str | None) -> dict[str, Choice | None]:
    """The `--image`, `--video` and `--audio` given, by name: a `Choice`, or None for off.

    A name given None is left out, so the target keeps its saved setting; text is read by
    `Choice.parse`, whose `ValueError` names the option and what each part takes.
    """
    given: dict[str, Choice | None] = {}
    for kind, value in values.items():
        if value is None:
            continue
        given[kind] = Choice.parse(value, kind=kind) if isinstance(value, str) else value
    return given


def _switch(classify: bool | str | None) -> bool | None:
    """`--classify` as given: True for on, False for off, None for not given.

    Raises:
        ValueError: anything else, with what it takes.
    """
    if classify is None or isinstance(classify, bool):
        return classify
    if classify in ("on", "off"):
        return classify == "on"
    raise ValueError(f"--classify takes on or off, not {classify!r}")


def _with(options: Options, choices: Mapping[str, Choice | None]) -> Options:
    """`options` with the saved settings in `choices`; one `choices` leaves out is None."""
    return replace(options, image=choices.get("image"), video=choices.get("video"), audio=choices.get("audio"))


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
    stops listing and lets its running downloads finish (flnd/job.py), and `Engine._fetch` raises
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
        starts = [route.function, *(getattr(source, category.attribute) for category in categories)] \
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


def _offer_labels(chosen: Sequence[tuple[Target, Options]]) -> bool:
    """Ask, once and before any work, what `--classify` needs; whether the run can classify.

    When a target runs with `--classify` (given, or saved), `media.prepare_labels` asks in a
    terminal to install the extra and to download the model when either is missing, with the live
    rows held, and loads the model. A no, no terminal or a failure warns once: the run then
    downloads without classifying, and its targets keep their saved setting. The job's own
    `Labeller` stops with a warning too, should the model fail later: a safety net only.
    """
    if not any(options.classify for _, options in chosen):
        return True
    from shared import media

    try:
        media.prepare_labels()
    # The extra or the model missing and not got: the same for every file, so said once here.
    except (ImportError, RuntimeError) as error:
        log.warn("classify is off for this run: {error}", error=error)
        return False
    return True


def _for_run(chosen: Sequence[tuple[Target, Options]], ready: bool) -> list[tuple[Target, Options]]:
    """`chosen` as the fetch runs it: `--classify` off for every target when `Media.labels` isn't ready."""
    return list(chosen) if ready else [(target, replace(options, classify=False)) for target, options in chosen]


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


# What `_run_group` fills for each target, by its place in the run: its report and its complete categories.
Done = MutableMapping[int, "tuple[TargetReport, tuple[str, ...]]"]


def _run_group(run_settings: Settings, done: Done, sync: bool,
               work: tuple[Source, list[tuple[int, Target, Row]]]) -> None:
    """Run one source's targets, one after another, on one worker, filling `done` as each ends.

    A target that ends is in `done` even when a later one stops the whole group, so its report
    survives; `Engine._run` fails the ones the group never reached.
    """
    source, rows = work
    with source_run(run_settings, source) as run:
        account = TargetSite(run, Target(source.code, ACCOUNT, ""))
        if not source.logged_in(account):
            if source.requires_login:
                for number, target, row in rows:
                    log.error("{target}: not logged in; run flnd login", target=str(target))
                    row.fail()
                    done[number] = (TargetReport(target, (), 0, error="not logged in; run flnd login"), ())
                return
            log.warn("{code}: not logged in, using public access; run flnd login", code=source.code)
        try:
            with _connected(run, account):
                _run_targets(run, rows, done, sync)
        finally:
            # The cookies the site refreshed, even when the group failed or Ctrl-C stopped it (B5).
            auth.write_back(run_settings.home, source.code, run.http.cookies.jar)


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


def _run_targets(run: SourceRun, rows: list[tuple[int, Target, Row]], done: Done, sync: bool = False) -> None:
    """Each target of one source through the job, with a person's pause between them.

    Args:
        run: the source's resources for the run.
        rows: each target's place in the run, its target and its progress row.
        done: filled with each target's report as it ends, keyed by its place.
        sync: passed on to the job; see `job.download`.
    """
    for index, (number, target, row) in enumerate(rows):
        if index and not net.stopping.is_set():
            # Back-to-back targets are what a script looks like, so a person's pause comes between them.
            _sleep(net.jittered(run.source.pause))
        if net.stopping.is_set():
            # After the first Ctrl-C no target starts; the ones left are stopped, not failed by a site.
            row.caption = "stopped"
            row.fail()
            done[number] = (TargetReport(target, (), 0, error=job.STOPPED), ())
            continue
        result = job.download(run, target, row, sync=sync)
        done[number] = (TargetReport(target, result.written, result.skipped, error=result.error), result.complete)
