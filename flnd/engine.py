"""The engine: resolves targets, groups them by source, and owns everything sources share.

    engine = Engine(home=Path(".flnd"))
    total = engine.run(engine.resolve(["ins:example_user", "https://x.com/example_handle"]), Options())
"""

from __future__ import annotations

import inspect
import signal
import threading
import time
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from types import CodeType
from typing import Any, Callable, Iterator, Mapping, MutableMapping, Sequence
from urllib.parse import urlsplit

import httpx

from flnd import job
from flnd.common import net
from flnd.session import Options, Settings, TargetSession
from flnd.site import LoginSite, SourceRun, TargetSite, source_run
from flnd.source import Source, Target, TargetError
from flnd.sources import SOURCES
from shared import browser, kv, log, worker

__all__ = ["Engine"]

HISTORY_FILE = "history.db"
HISTORY_TABLE = "downloads"
# The target a source's login and `connect` see: the account, not anything to download.
ACCOUNT = "account"

# What in a source's code means it can drive a browser: the `Site` members and helpers that open
# or use a page (`Site.page`, `goto`, `handoff`, `fetch(in_page=True)`, `watch_responses`,
# `meta_results`), and, for a bridged source, the session's own (`open_page`, `on_response`, `scroll`).
BROWSER_NAMES = frozenset({"page", "goto", "handoff", "in_page", "watch_responses", "meta_results",
                           "open_page", "on_response", "on_request", "scroll", "browser"})

# Pauses between targets read time through this, so tests can replace it.
_sleep = time.sleep

Row = Any


class Engine:
    """Runs targets, one worker per source, with one HTTP client, back-off and history for each.

        engine = Engine(home=Path(".flnd"))
        total = engine.run(engine.resolve(["tik:example_creator"]), Options(categories=("videos",)))

    Targets of one source run one after another, with a pause between them; different sources
    run at the same time. It never asks questions.
    """

    def __init__(self, *, home: Path = Path(".flnd"), sources: Mapping[str, type[Source]] = SOURCES):
        """Make an engine; nothing is read or started until `resolve` or `run`.

        Args:
            home: flnd's folder, which holds the downloads, the history, the logins and the saved settings.
            sources: the sources by code; tests give their own.

        A run has no download limit: it ends when the sources have nothing more, or on Ctrl-C.
        """
        self.home, self.sources = home, sources
        # Every target that ended at error, over all of this engine's runs: `flnd download` and
        # `flnd sync` run one per settings group, and the command's exit status covers them all.
        self.failed: list[Target] = []

    def resolve(self, texts: Sequence[str]) -> list[Target]:
        """Every target as its source reads it, stopping at the first bad one with `TargetError`.

            targets = engine.resolve(["ins:@example_user", "https://vm.tiktok.com/ZMabc/"])

        Resolve them all before `run`, so a bad one stops everything before anything starts.
        Only short links make a request, one HEAD each.
        """
        return [resolve(text, self.sources, follow=_follow) for text in texts]

    def run(self, targets: Sequence[Target], options: Options) -> int:
        """Download every target, and return how many files were written.

            engine.run(engine.resolve(["ins:example_user"]), Options(categories=("reels",)))
            exit_status = 1 if engine.failed else 0

        Args:
            targets: the targets to download, as `resolve` gives them.
            options: what to take from each target and what to do with the files.

        Returns:
            How many files were written; one skipped as already saved or filtered out isn't counted.

        When a target's source can open a browser (`needs_browser`) and Google Chrome is missing,
        the "download Chromium?" question is asked once, before anything starts. A source with
        categories runs through flnd/job.py; one with `run(session)` through `TargetSession`.

        Each target ends its row at ok or error; one failing never stops the others, and is added to
        `failed`. A whole source that can't start (its `logged_in` or `connect` raises) fails each of
        its targets. A rate limit is waited out, however long it takes, and isn't a failure; Ctrl-C
        ends the wait.
        """
        groups: dict[str, list[Target]] = {}
        for target in targets:
            groups.setdefault(target.source, []).append(target)
        # The one question `flnd download` can ask, before any work starts (cli.md, "Chrome only").
        if any(needs_browser(self.sources[target.source], target, options) for target in targets):
            _offer_browser()
        with kv.open(self.home / HISTORY_FILE, table=HISTORY_TABLE) as history, _stop_waits_on_ctrl_c(), \
                log.group("download") as rows:
            settings = self._settings(options, history)
            work = [(self.sources[code](), [(target, rows.add(str(target))) for target in group])
                    for code, group in groups.items()]
            total = 0
            for result in worker.as_completed(partial(_run_group, settings), work, concurrency=max(len(work), 1)):
                if result.ok:
                    total += result.value or 0
                    continue
                # The whole group stopped before or between its targets: the targets it never reached
                # failed too, or `log.group` would end their rows at ok.
                source, group_rows = result.item
                log.error("{source}: {error}", source=source.code, error=result.error)
                for _, row in group_rows:
                    if not row.finished:
                        row.caption = str(result.error)
                        row.fail()
        self.failed += [target for _, group_rows in work for target, row in group_rows if row.outcome == "error"]
        return total

    def login(self, source: Source, method: str) -> None:
        """Run a source's own login (tlg's), through a `Login` that may ask questions."""
        with source_run(self._settings(Options(), {}, interactive=True), source) as run:
            if source.ported():
                source.login(LoginSite(run), method)
                return
            # step-6 bridge: removed once every source is ported.
            source.login(TargetSession(run, Target(source.code, ACCOUNT, "")), method)

    def _settings(self, options: Options, history: MutableMapping[str, Any], *,
                  interactive: bool = False) -> Settings:
        return Settings(home=self.home, options=options, history=history, interactive=interactive)


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
    """The source whose domains a URL belongs to, or None: `owner("https://x.com/example_handle", SOURCES)` is Twitter."""
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
    """Let Ctrl-C end every rate-limit wait at once, in every thread, as well as doing what it always does."""
    net.stopping.clear()
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)

    def interrupted(number: int, frame: Any) -> None:
        net.stopping.set()
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

    The code read is the route and its wanted categories (all of `run`, for a bridged source),
    and every function and method they name that lives in the source's module or in
    `flnd.sources.common`, followed as far as it goes. It needs a browser when any of them names
    one of `BROWSER_NAMES`. So no `Source` field says it, and it can't drift from the code: a source
    that calls `site.goto` asks, and one that only calls `site.fetch` doesn't. It reads names, not
    what runs, so a branch never taken still counts: it may ask once when no page ends up opening.
    """
    if not source.ported():
        # step-6 bridge: removed once every source is ported.
        starts: list[Any] = [source.run]
    else:
        route = next((route for route in source.routes if route.kind == target.kind), None)
        if route is None:
            return False
        starts = [route.function]
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
    try:
        return net.follow(url)
    except httpx.HTTPError as error:
        raise TargetError(f"can't follow {url}: {error}") from None


def _run_group(settings: Settings, work: tuple[Source, list[tuple[Target, Row]]]) -> int:
    source, rows = work
    with source_run(settings, source) as run:
        account: Any = TargetSite(run, Target(source.code, ACCOUNT, ""))
        if not source.ported():
            # step-6 bridge: removed once every source is ported.
            account = TargetSession(run, Target(source.code, ACCOUNT, ""))
        if not source.logged_in(account):
            if source.requires_login:
                for target, row in rows:
                    log.error("{target}: not logged in; run flnd login", target=str(target))
                    row.fail()
                return 0
            log.warn("{code}: not logged in, using public access; run flnd login", code=source.code)
        with _connected(run, account):
            return _run_targets(run, rows)


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
    except BaseException as error:
        if not run.run_async(manager.__aexit__(type(error), error, error.__traceback__)):
            raise
    else:
        run.run_async(manager.__aexit__(None, None, None))


def _run_targets(run: SourceRun, rows: list[tuple[Target, Row]]) -> int:
    total = 0
    for index, (target, row) in enumerate(rows):
        if index:
            # Back-to-back targets are what a script looks like, so a person's pause comes between them.
            _sleep(net.jittered(run.source.pause))
        total += _run_target(run, target, row)
    return total


def _run_target(run: SourceRun, target: Target, row: Row) -> int:
    if run.source.ported():
        return job.download(run, target, row)
    # step-6 bridge: removed once every source is ported; the rest of this function is the bridged path.
    session = TargetSession(run, target)
    try:
        outcome = run.source.run(session)
        if inspect.iscoroutine(outcome):
            run.run_async(outcome)
    except BaseException as error:
        saved = session.finish(error)
        if not isinstance(error, Exception):
            raise
        log.error("{target}: {error}", target=str(target), error=error)
        row.fail()
        return saved
    saved = session.finish()
    row.finish()
    return saved
