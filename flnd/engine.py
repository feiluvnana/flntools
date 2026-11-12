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
from typing import Any, Iterator, Mapping, MutableMapping, Sequence

import httpx

from flnd.common import auth, net
from flnd.common.target import Target, TargetError, resolve
from flnd.session import Options, Settings, SourceRun, TargetSession
from flnd.sources import SOURCES
from flnd.sources.base import Source
from shared import kv, log, worker

__all__ = ["Engine"]

HISTORY_FILE = "history.db"
HISTORY_TABLE = "downloads"
# The target a source's login and `connect` see: the account, not anything to download.
ACCOUNT = "account"

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

        Each target ends its row at ok or error; one failing never stops the others, and is added to
        `failed`. A whole source that can't start (its `logged_in` or `connect` raises) fails each of
        its targets. A rate limit is waited out, however long it takes, and isn't a failure; Ctrl-C
        ends the wait.
        """
        groups: dict[str, list[Target]] = {}
        for target in targets:
            groups.setdefault(target.source, []).append(target)
        with kv.open(self.home / HISTORY_FILE, table=HISTORY_TABLE) as history, _stop_waits_on_ctrl_c(), \
                log.multi("download") as rows:
            settings = self._settings(options, history)
            work = [(self.sources[code](), [(target, rows.add(str(target))) for target in group])
                    for code, group in groups.items()]
            total = 0
            for result in worker.race(partial(_run_group, settings), work, concurrency=max(len(work), 1)):
                if result.ok:
                    total += result.value or 0
                    continue
                # The whole group stopped before or between its targets: the targets it never reached
                # failed too, or `log.multi` would end their rows at ok.
                source, group_rows = result.item
                log.error("{source}: {error}", source=source.code, error=result.error)
                for _, row in group_rows:
                    if not row.finished:
                        row.caption = str(result.error)
                        row.fail()
        self.failed += [target for _, group_rows in work for target, row in group_rows if row.outcome == "error"]
        return total

    def login(self, source: Source, method: str) -> None:
        """Run a source's own login (tlg's), with a session that may ask questions."""
        with _source_run(self._settings(Options(), {}, interactive=True), source) as run:
            source.login(TargetSession(run, Target(source.code, ACCOUNT, "")), method)

    def _settings(self, options: Options, history: MutableMapping[str, Any], *,
                  interactive: bool = False) -> Settings:
        return Settings(home=self.home, options=options, history=history, interactive=interactive)


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


def _follow(url: str) -> str:
    try:
        return net.follow(url)
    except httpx.HTTPError as error:
        raise TargetError(f"can't follow {url}: {error}") from None


def _run_group(settings: Settings, work: tuple[Source, list[tuple[Target, Row]]]) -> int:
    source, rows = work
    with _source_run(settings, source) as run:
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
def _source_run(settings: Settings, source: Source) -> Iterator[SourceRun]:
    jar = auth.load(settings.home, source.code)
    backoff = net.Backoff(source.code)
    names = {cookie["name"]: cookie["value"] for cookie in jar}
    http = net.client(user_agent=source.user_agent, headers=source.headers(names), jar=jar, sign=source.sign,
                      wait=type(source).wait, backoff=backoff)
    run = SourceRun(source, settings, jar, http, backoff)
    try:
        yield run
    finally:
        run.close()


@contextmanager
def _connected(run: SourceRun, session: TargetSession) -> Iterator[None]:
    """Enter the source's `connect` once for all its targets, sync or async, as `session.client`."""
    manager = run.source.connect(session)
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
