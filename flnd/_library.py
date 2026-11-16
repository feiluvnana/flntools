"""`Flnd` and `open`: the library a script uses, over the engine every command runs on.

    with flnd.open("~/Media/flnd") as fl:
        for event in fl.download("ins:example_user", only=["reels"]):
            ...

Who calls it: scripts, through `flnd.open`; the CLI calls the engine directly until it is
rebuilt on this module. What it calls: `flnd.engine.Engine` for every command, `flnd._home`
(the home's `flnd.db`) for the reads, and the home's `config.toml` for settings past the five
options a call takes.

How a call flows:
1. The method checks its arguments at once: targets are resolved, `only`, `when` and `limit`
   are read, and a mistake raises `flnd.UsageError` before anything is fetched.
2. A command that downloads (`download`, `follow`, `sync`) returns a `flnd.Run` that hasn't
   started. Its work is a closure that makes an `Engine` with the run's event sink and stop
   state, and calls the engine's command; the run starts it on its own thread when read.
3. A read (`targets`, `files`, `logins`) or a quick change (`unfollow`, `login`, `logout`) runs
   on the caller's thread and returns plain rows from `flnd._events`.

Rules a change must keep:
- Importing this module imports nothing heavy: the engine, curl_cffi, Playwright and Rich load on
  the first call that needs them, so `import flnd` stays fast.
- Options are keyword-only and at most five per call; a setting past that belongs in the
  config file (`[defaults] again`, read by `_settings`).
- No callbacks: a run reports through its events alone.
"""

from __future__ import annotations

import datetime
import os
import re
import threading
import weakref
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flnd._events import Checked, Installed, Login, Record, Tracked
from flnd._run import Run
from flnd.errors import UsageError

if TYPE_CHECKING:
    from flnd.engine import Engine
    from flnd.source import Range, Site, Source, Target

__all__ = ["Flnd", "open"]

# The home when none is given: `$FLND_HOME`, else `.flnd` in the current folder.
HOME_VARIABLE = "FLND_HOME"
DEFAULT_HOME = ".flnd"
# The kinds `files(kind=...)` filters by.
KINDS = ("image", "video", "audio")
# What `when` takes; the message of its usage error.
WHEN_USAGE = ("when takes a year (2026), a month (2026-06), a day (2026-06-15), a range of "
              "those (2026-01..2026-03, 2026-01.., ..2026-03), or a span back from today "
              "(30d, 8w, 1y)")
PERIOD = re.compile(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?")
SPAN = re.compile(r"(\d+)([dwy])")
# The days of each span unit: a year is 365 days, which is close enough for "the last year".
SPAN_DAYS = {"d": 1, "w": 7, "y": 365}


def open(home: str | os.PathLike[str] | None = None, *, again: bool | None = None) -> Flnd:
    """Open a home folder: the one place flnd keeps downloads, history and logins.

    Nothing is written until a command needs to: opening a new folder doesn't create it.

    Args:
        home: the home folder; `~` is expanded. None takes `$FLND_HOME`, else `.flnd` in the
            current folder.
        again: whether every `download` and `follow` lists each post again, past the stop at
            saved posts and the saved cursors, and asks again for files the site answered 404
            or 410 for (recorded as gone). None reads `again` under `[defaults]` in the home's
            `config.toml` (false without one); the CLI's `--again` sets it.

    Returns:
        The `Flnd` of that home, to use in a `with` block.

    Example:
        >>> import flnd
        >>> with flnd.open("example-home") as fl:
        ...     fl.targets()
        []
    """
    return Flnd(home, again=again)


class Flnd:
    """flnd for one home folder: each command of the CLI as a method.

    It is a context manager: leaving the `with` (or `close()`) stops any run still going, as a
    first Ctrl-C does, and waits for it. A run made but never started can't start after that.

    Args:
        home: the home folder; `~` is expanded. None takes `$FLND_HOME`, else `.flnd` in the
            current folder.
        again: as `open` reads it.

    Example:
        >>> import flnd
        >>> with flnd.Flnd("example-home") as fl:
        ...     fl.home.name
        'example-home'
    """

    def __init__(self, home: str | os.PathLike[str] | None = None, *,
                 again: bool | None = None) -> None:
        chosen = home if home is not None else os.environ.get(HOME_VARIABLE) or DEFAULT_HOME
        # The folder the home is.
        self.home = Path(chosen).expanduser()
        # Whether downloads ask again for everything; None reads the home's config.toml.
        self._again = again
        self._added: dict[str, type[Source]] = {}
        self._runs: weakref.WeakSet[Run] = weakref.WeakSet()
        self._closed = threading.Event()

    def __enter__(self) -> Flnd:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<flnd.Flnd {str(self.home)!r}>"

    def close(self) -> None:
        """Stop every run still going, as a first Ctrl-C does, and wait for each to finish.

        A run's errors aren't raised here: whoever read the run saw them. Closing twice is
        harmless.

        Example:
            >>> import flnd
            >>> fl = flnd.open("example-home")
            >>> fl.close()
        """
        self._closed.set()
        for run in list(self._runs):
            if run._thread is None:
                continue
            run.stop()
            try:
                run.wait()
            except BaseException:  # noqa: BLE001 - its reader saw it; closing must go on
                pass

    # -----------------------------------------------------------------------------------------
    # Runs
    # -----------------------------------------------------------------------------------------

    def download(
        self, targets: str | Target | Sequence[str | Target], *,
        only: str | Sequence[str] | None = None, when: str | None = None,
        limit: int | None = None, compress: bool = False, dry: bool = False,
    ) -> Run:
        """Download what the targets have now, once: nothing is followed for later syncs.

        Args:
            targets: one target or several: `"ins:example_user"`, a link, or a `Target`.
            only: the categories to take (`["reels"]`, or `"reels,posts"`); None takes every
                category. A single post has none, so `only` with one is a usage error.
            when: the days to take posts from: `"2026"`, `"2026-06"`, `"2026-06-15"`,
                `"2026-01..2026-03"` or `"30d"`; None for every day.
            limit: at most this many posts per target, or targets per search; None for all.
            compress: shrink each photo and video as it lands, when a smaller copy looks the
                same, and remove each archive once it is extracted (docs/media).
            dry: plan instead: the run yields a `Planned` event per file and writes nothing.

        Returns:
            The `Run`, not started yet: iterate it, or call `wait()`.

        Raises:
            UsageError: a target, category, `when` or `limit` is wrong.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:  # doctest: +ELLIPSIS
            ...     fl.download("ins:example_user", when="tomorrow")
            Traceback (most recent call last):
            ...
            flnd.errors.UsageError: when takes a year (2026), a month (2026-06), ...
        """
        span = _when(when)
        if limit is not None and limit < 1:
            raise UsageError("limit must be at least 1")
        resolved = self._checked(targets, only)
        again = _settings(self.home) if self._again is None else self._again
        return self._run(f"download {_names(resolved)}", lambda engine: engine.download(
            resolved, categories=only, time=span, limit=limit, dry=dry, every=True,
            compress=compress, again=again))

    def follow(
        self, targets: str | Target | Sequence[str | Target], *,
        only: str | Sequence[str] | None = None, when: str | None = None,
        compress: bool = False,
    ) -> Run:
        """Follow the targets, so `sync` keeps them up to date, then download what they have.

        `only` is kept with each target, and later syncs take those categories alone;
        following again replaces it. `when` bounds this first download only.

        Args:
            targets: one target or several, as `download` takes them.
            only: the categories to follow; None follows every category.
            when: the days this first download takes posts from, as `download` reads it.
            compress: shrink each photo and video of this first download as it lands, and
                remove each archive once it is extracted (docs/media). Later syncs compress
                only when they are asked to.

        Returns:
            The `Run` of the first download, not started yet.

        Raises:
            UsageError: a target, category or `when` is wrong.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.follow("zzz:example_user")
            Traceback (most recent call last):
            ...
            flnd.errors.UsageError: unknown source 'zzz'. Sources: tik, fbk, ins, twt, loi, tlg, mht
        """
        span = _when(when)
        resolved = self._checked(targets, only)
        again = _settings(self.home) if self._again is None else self._again
        return self._run(f"follow {_names(resolved)}", lambda engine: engine.download(
            resolved, categories=only, time=span, sync=True, every=True, keep=True,
            compress=compress, again=again))

    def sync(self, targets: str | Target | Sequence[str | Target] = (), *,
             compress: bool = False) -> Run:
        """Download what is new for every followed target, or for the ones named.

        Args:
            targets: the followed targets to sync; empty for every one.
            compress: shrink the new photos and videos as they land, then those the targets
                saved before that aren't done yet; a done file is skipped unopened (docs/media).

        Returns:
            The `Run`, not started yet. With nothing to sync, it ends at once with an empty
            report.

        Raises:
            UsageError: a target is wrong.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.sync().wait().saved
            0
        """
        resolved = self._resolve(targets)
        named = f"sync {_names(resolved)}" if resolved else "sync"
        return self._run(named, lambda engine: engine.sync(resolved, compress=compress))

    def retry(self) -> Run:
        """Download again the files the last run failed on.

        Not built yet: a failed file is retried by the next download or sync of its target.

        Returns:
            Never returns today.

        Raises:
            UsageError: always, until the home keeps a failure report.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.retry()
            Traceback (most recent call last):
            ...
            flnd.errors.UsageError: retry isn't built yet
        """
        raise UsageError("retry isn't built yet",
                         hint="download or sync the target again: its failed files go first")

    # -----------------------------------------------------------------------------------------
    # Targets and files
    # -----------------------------------------------------------------------------------------

    def unfollow(self, targets: str | Target | Sequence[str | Target]) -> list[Target]:
        """Stop following targets: `sync` leaves them out. Their files and history stay.

        Args:
            targets: one known target or several.

        Returns:
            The targets, resolved.

        Raises:
            UsageError: a target is wrong, or the home doesn't know it.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.unfollow("ins:example_user")
            Traceback (most recent call last):
            ...
            flnd.errors.UsageError: ins:example_user: not a known target; download it first
        """
        return self._engine().target_sync(self._resolve(targets), enabled=False)

    def targets(self, *, source: str | None = None) -> list[Tracked]:
        """Every target the home knows, followed or not, with its files and last sync.

        Args:
            source: only this source's targets (`"ins"`); None for every source.

        Returns:
            One `Tracked` row per target.

        Raises:
            UsageError: `source` is no source's code.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.targets(source="ins")
            []
        """
        if source is not None:
            self._source(source)
        found: list[Tracked] = []
        for info in self._engine().target_ls():
            if source is not None and info.target.source != source:
                continue
            found.append(Tracked(info.target, follow=info.sync, at=info.at, files=info.files,
                                 size=info.size, seen=info.seen,
                                 categories=dict(info.categories), query=info.query))
        return found

    def files(
        self, *, target: str | Target | None = None, kind: str | None = None,
        since: str | None = None,
    ) -> list[Record]:
        """The saved files, read from the home's `flnd.db`, oldest saved first.

        Args:
            target: only this target's files; None for every target.
            kind: only `"image"`, `"video"` or `"audio"` files; None for all.
            since: only posts from this day on, written as `when` writes one end
                (`"2026"`, `"2026-01"`, `"2026-01-15"`); a file whose post has no date
                counts by the day it was saved. None for every day.

        Returns:
            One `Record` per saved file.

        Raises:
            UsageError: `target`, `kind` or `since` is wrong.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.files(kind="video")
            []
        """
        if kind is not None and kind not in KINDS:
            raise UsageError(f"kind is one of {', '.join(KINDS)}, not {kind!r}")
        from flnd import _home

        first = None if since is None else _period(since)[0]
        owner = None
        if target is not None:
            [resolved] = self._resolve(target)
            owner = _home.key(resolved)
        with _home.open(self.home) as db:
            rows = db.records(target=owner, kind=kind)
        found = []
        for row in rows:
            record = Record(row.key, Path(row.path), row.size, post=row.post_id,
                            category=row.category, date=_home._date(row.date), at=row.at)
            if first is not None and (_day(record) or first) < first:
                continue
            found.append(record)
        return found

    def check(self, *, fix: bool = False) -> Checked:
        """Compare the history with the disk: files missing, unknown, or of the wrong size.

        It reads every row and walks the home's `media/` folder once; nothing is fetched.
        `fix=True` makes the history match the disk, and never deletes, moves or downloads a
        file: a missing file's row goes, a file of the wrong size gets its row's size and hash
        from the disk, and an unknown file is adopted, by its `(target, post, file)` key where
        a failed run recorded one (docs/storage, "Checking a home").

        Args:
            fix: also repair the history; False only reports.

        Returns:
            What was found; `checked.ok` says whether history and disk now agree.

        Raises:
            DiskError: the media folder can't be read, or `flnd.db` written.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.check().ok
            True
        """
        from flnd import _check, _home

        with _home.open(self.home) as db:
            return _check.check(self.home, db, fix=fix)

    # -----------------------------------------------------------------------------------------
    # Logins and sources
    # -----------------------------------------------------------------------------------------

    def login(self, source: str, how: str) -> Login:
        """Log in to a source, and save the login, encrypted, in the home.

        Args:
            source: the source's code (`"ins"`).
            how: a browser to read its cookies from (`"chrome"`, `"firefox"`, `"edge"`,
                `"brave"`, `"chromium"`), the path of a cookies.txt or JSON export, or the
                source's own way (`"qr"`, `"phone"` for tlg), which asks its questions in
                the terminal.

        Returns:
            The source's `Login` after it.

        Raises:
            UsageError: `source` or `how` is wrong.
            LoginError: the browser has no login for the site, or the site refused it.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:  # doctest: +ELLIPSIS
            ...     fl.login("ins", "netscape")
            Traceback (most recent call last):
            ...
            flnd.errors.UsageError: ins logs in by a browser (chrome, ...) or a cookie file's ...
        """
        from flnd._home import BROWSERS

        cls = self._source(source)
        engine = self._engine()
        if source not in engine.cookie_logins:
            methods = cls.login.methods if cls.login is not None else ()
            if how not in methods:
                raise UsageError(f"{source} logs in by {', '.join(methods) or 'nothing'}, "
                                 f"not {how!r}")
            engine.login(source, how)
        elif how in BROWSERS:
            engine.login(source, "browser", browser=how)
        elif Path(how).expanduser().is_file():
            engine.login(source, "file", path=how)
        else:
            raise UsageError(f"{source} logs in by a browser ({', '.join(BROWSERS)}) or a "
                             f"cookie file's path, not {how!r}")
        return next(login for login in self.logins if login.source == source)

    def logout(self, source: str) -> bool:
        """Remove a source's saved login.

        Args:
            source: the source's code.

        Returns:
            Whether there was a login to remove.

        Raises:
            UsageError: `source` is no source's code.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.logout("ins")
            False
        """
        self._source(source)
        return bool(self._engine().logout([source]))

    @property
    def logins(self) -> list[Login]:
        """Each source's login: its state, where it came from, and cookies that expire soon."""
        return [Login(info.code, info.name, info.state, origin=info.origin,
                      expiring=tuple((name, left) for name, left in info.expiring))
                for info in self._engine().auth_ls()]

    @property
    def sources(self) -> list[Installed]:
        """Every source: the built-ins, the plugins (disabled ones too, saying why), then `add`'s.

        Reading it imports every source and plugin, as `flnd sources` does.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     [row.origin for row in fl.sources if row.code == "ins"]
            ['built-in']
        """
        from flnd import _plugins
        from flnd.sources import SOURCES

        logins = self._engine().logins
        plugins = SOURCES.plugins
        by_code = {plugin.code: plugin for plugin in plugins if not plugin.error}
        rows = [_installed(SOURCES[code], by_code.get(code), logins) for code in SOURCES]
        rows += [Installed(plugin.code, plugin.code, plugin.origin, where=plugin.where,
                           version=plugin.version, disabled=plugin.error)
                 for plugin in plugins if plugin.error]
        rows += [_installed(cls, None, logins, origin=_plugins.SCRIPT)
                 for cls in self._added.values()]
        return rows

    def add(self, source: type[Source]) -> None:
        """Add a source for this `Flnd` alone: its targets resolve and download like a built-in's.

        It comes after the built-ins and the plugins, so a code one of them has is refused,
        never replaced.

        Args:
            source: the `flnd.source.Source` subclass.

        Raises:
            UsageError: it isn't a `Source` subclass, or its code is taken.

        Example:
            >>> import flnd
            >>> from flnd.source import Source, target
            >>> class Example(Source):
            ...     code, name, domains, api = "exa", "Example", ("example.com",), 1
            ...     @target("exa:{user}")
            ...     def user(self, site, user):
            ...         return []
            >>> with flnd.open("example-home") as fl:
            ...     fl.add(Example)
            ...     fl.targets(source="exa")
            []
        """
        from flnd.source import Source

        if not (isinstance(source, type) and issubclass(source, Source)):
            raise UsageError(f"add takes a flnd.source.Source subclass, not {source!r}")
        if source.code in self._sources():
            taken = self._sources()[source.code]
            raise UsageError(f"{source.code}: the code is taken by {taken.__module__}."
                             f"{taken.__qualname__}", hint="give the class another code")
        self._added[source.code] = source

    @contextmanager
    def site(self, source: str) -> Iterator[Site]:
        """A source's `Site`, with its saved login, pacing and retries, for a script's requests.

        Args:
            source: the source's code.

        Yields:
            The `flnd.source.Site`, open for the block.

        Raises:
            UsageError: `source` is no source's code.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl, fl.site("ins") as site:
            ...     str(site.target)
            'ins'
        """
        from flnd._run import stopping
        from flnd.options import RunShared
        from flnd.site import TargetSite, source_run
        from flnd.source import Target

        cls = self._source(source)
        with stopping() as stop:
            settings = RunShared(home=self.home, stop=stop)
            with source_run(settings, cls()) as run:
                opened = TargetSite(run, Target(f"{cls.code}:", kind="account"))
                try:
                    yield opened
                finally:
                    opened.close()

    # -----------------------------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------------------------

    def _sources(self) -> Mapping[str, type[Source]]:
        """The built-in sources, then the ones `add` added.

        A view, not a copy: a built-in source is imported only when its code is looked up, so
        resolving `ins:example_user` imports Instagram's module and no other.

        Returns:
            The sources by code; an added source comes first when looked up.
        """
        from collections import ChainMap

        from flnd.sources import SOURCES

        return ChainMap(self._added, SOURCES)

    def _source(self, code: str) -> type[Source]:
        """The source of a code, built-in or added.

        Args:
            code: the source's code (`"ins"`).

        Returns:
            The source's class.

        Raises:
            UsageError: no source has it.
        """
        sources = self._sources()
        if code not in sources:
            raise UsageError(f"unknown source {code!r}. Sources: {', '.join(sources)}")
        return sources[code]

    def _engine(self, emit: Any = None, stop: Any = None) -> Engine:
        """An engine of this home and its sources, sending events to `emit`."""
        from flnd.engine import Engine

        return Engine(home=self.home, sources=self._sources(), emit=emit, stop=stop)

    def _resolve(self, targets: str | Target | Sequence[str | Target]) -> list[Target]:
        """Each target resolved through its source; a `Target` is taken as it is.

        A short link is followed once (one request) to learn where it points.

        Args:
            targets: one target or several, as text or resolved.

        Returns:
            The targets, resolved, in order.

        Raises:
            UsageError: a text no source reads.
        """
        from flnd.source import Target

        given = [targets] if isinstance(targets, (str, Target)) else list(targets)
        engine = self._engine()
        return [item if isinstance(item, Target) else engine.resolve([item])[0]
                for item in given]

    def _checked(self, targets: Any, only: str | Sequence[str] | None) -> list[Target]:
        """Targets resolved and `only` checked against them, before a run is made.

        Args:
            targets: one target or several, as text or resolved.
            only: the categories asked for; None for every category.

        Returns:
            The targets, resolved, in order.

        Raises:
            UsageError: no target, a target no source reads, or a category none of them has.
        """
        from flnd.options import categories_by_source

        resolved = self._resolve(targets)
        if not resolved:
            raise UsageError("give at least one target")
        categories_by_source(only, resolved, self._sources(), every=True)
        return resolved

    def _run(self, name: str, command: Any) -> Run:
        """A `Run` whose worker makes this home's engine and calls `command` with it.

        The engine is made on the worker thread, with the run's sink and stop state, so the
        run alone decides when the work starts and how it stops.

        Args:
            name: what the run does, for its thread's name and its warning.
            command: called with the engine; what it returns is dropped (the events tell it).

        Returns:
            The run, not started, and remembered so `close()` can stop it.
        """
        def work(emit: Any, stop: Any) -> None:
            command(self._engine(emit, stop))

        run = Run(work, name=name, closed=self._closed)
        self._runs.add(run)
        return run


def _installed(cls: type[Source], plugin: Any, logins: Mapping[str, str], *,
               origin: str | None = None) -> Installed:
    """A working source's row for `Flnd.sources`.

    Args:
        cls: the source's class.
        plugin: its `flnd._plugins.Plugin` when it is one; None for a built-in or `add`'s.
        logins: the login state of each source with one saved (`Engine.logins`).
        origin: where it came from, when `plugin` doesn't say; None for a built-in.

    Returns:
        The row.
    """
    import flnd

    if plugin is not None:
        origin, where, version = plugin.origin, plugin.where, plugin.version
    else:
        where = f"{cls.__module__}:{cls.__qualname__}"
        version = flnd.__version__ if origin is None else ""
        origin = origin or "built-in"
    if cls.login is None:
        login = "needs no login"
    else:
        login = logins.get(cls.code, "not logged in")
    methods = cls.login.methods if cls.login is not None else ()
    return Installed(cls.code, cls.name, origin, where=where, version=version, api=cls.api,
                     login=login, domains=tuple(cls.domains), forms=tuple(cls.forms),
                     categories=tuple(cls.categories), logins=tuple(methods))


# ---------------------------------------------------------------------------------------------
# Reading options
# ---------------------------------------------------------------------------------------------

def _names(targets: Sequence[Target]) -> str:
    """The targets as a run's name shows them: the first two, then how many more."""
    shown = " ".join(map(str, targets[:2]))
    return shown if len(targets) <= 2 else f"{shown} and {len(targets) - 2} more"


def _settings(home: Path) -> bool:
    """Read the home's `config.toml` for `again`: whether a download asks again for everything.

    `[defaults] again = true` makes every download and follow list each post again, past the
    stop at saved posts and the saved cursors, and ask again for files the site answered 404 or
    410 for (recorded as gone). The CLI's `--again` does the same for one run; the library's
    calls keep their five options, so the setting lives here.

    Args:
        home: the home folder; a home without the file reads as `again = false`.

    Returns:
        The setting.

    Raises:
        UsageError: the file isn't TOML, or one of its settings has a value it doesn't take.
    """
    from flnd.options import config

    return config(home).again


def _when(text: str | None) -> Range:
    """Read `when`: a period, a range of periods, or a span back from today.

    Args:
        text: `"2026"`, `"2026-06"`, `"2026-06-15"`, `"2026-01..2026-03"` (either end may be
            left out), or `"30d"`, `"8w"`, `"1y"`; None or "" for every day.

    Returns:
        The days, both ends included.

    Raises:
        UsageError: the text is none of those, or the range ends before it starts.

    Example:
        >>> _when("2026-02")
        Range(since=datetime.date(2026, 2, 1), until=datetime.date(2026, 2, 28))
    """
    from flnd.source import Range

    text = (text or "").strip()
    if not text:
        return Range()
    if span := SPAN.fullmatch(text):
        from flnd import _time

        today = datetime.date.fromtimestamp(_time.wall())
        return Range(since=today - datetime.timedelta(days=int(span[1]) * SPAN_DAYS[span[2]]))
    first, dots, last = text.partition("..")
    if not dots:
        return Range(*_period(text))
    since = _period(first)[0] if first.strip() else None
    until = _period(last)[1] if last.strip() else None
    if since is not None and until is not None and since > until:
        raise UsageError(WHEN_USAGE)
    return Range(since, until)


def _period(text: str) -> tuple[datetime.date, datetime.date]:
    """The first and last day of a year, a month or a day written as `when` writes them.

    Args:
        text: `"2026"`, `"2026-06"` or `"2026-06-15"`.

    Returns:
        The first day and the last, the same day for a day.

    Raises:
        UsageError: the text isn't one.
    """
    found = PERIOD.fullmatch(text.strip())
    if found is None:
        raise UsageError(WHEN_USAGE)
    year, month, day = (int(part) if part else None for part in found.groups())
    try:
        if day is not None:
            first = last = datetime.date(year, month, day)  # type: ignore[arg-type]
        elif month is not None:
            first = datetime.date(year, month, 1)  # type: ignore[arg-type]
            following = datetime.date(year + (month == 12), month % 12 + 1, 1)  # type: ignore
            last = following - datetime.timedelta(days=1)
        else:
            first, last = datetime.date(year, 1, 1), datetime.date(year, 12, 31)  # type: ignore
    except ValueError:
        raise UsageError(WHEN_USAGE) from None
    return first, last


# ---------------------------------------------------------------------------------------------
# Reading saved files
# ---------------------------------------------------------------------------------------------

def _day(record: Record) -> datetime.date | None:
    """The day a record counts by for `since`: its post's, else the day it was saved.

    Args:
        record: the saved file's record.

    Returns:
        The day; None when the record has neither (an old row), which `since` lets through.
    """
    if record.date is not None:
        return record.date
    try:
        return datetime.datetime.fromisoformat(record.at).date() if record.at else None
    except ValueError:
        return None
