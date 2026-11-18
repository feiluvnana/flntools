"""`Flnd` and `open`: the library a script uses, over the engine every command runs on.

src/docs/library explains it.

    with flnd.open("~/Media/flnd") as fl:
        for event in fl.download("ins:example_user/reels"):
            ...

Who calls it: scripts and the CLI (`src/cli.py`), both through `flnd.open`, so the command
line can do nothing a script can't. What it calls: `flnd.engine.core.Engine` for every command,
`flnd.engine.home` (the home's `flnd.db`) for the reads, and the home's `config.toml` for settings
past the five options a call takes.

How a call flows:
1. The method checks its arguments at once: targets are resolved with the categories they
   name, `when` and `limit` are read, and a mistake raises `flnd.UsageError` before anything
   is fetched.
2. A command that downloads (`download`, `follow`, `sync`, `retry`) returns a `flnd.Run` that
   hasn't started. Its work is a closure that makes an `Engine` with the run's event sink and
   stop state, and calls the engine's command; the run starts it on its own thread when read.
3. A read (`targets`, `files`, `logins`, `status`) or a quick change (`unfollow`, `login`,
   `logout`) runs on the caller's thread and returns plain rows from `flnd.engine.events`.

Rules a change must keep:
- Importing this module imports nothing heavy: the engine, curl_cffi, Playwright and Rich load on
  the first call that needs them, so `import flnd` stays fast.
- Options are keyword-only and at most five per call; a setting past that belongs in the
  config file (`flnd.engine.options.config`). An option left as None takes the file's setting
  (`[defaults] when`), so a value given for one call always wins.
- No callbacks: a run reports through its events alone.
"""

from __future__ import annotations

import datetime
import os
import threading
import weakref
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flnd.engine.events import Checked, Failure, Installed, Login, Record, Status, Tracked
from flnd.engine.run import Run
from flnd.errors import UsageError

if TYPE_CHECKING:
    from flnd.engine.core import Chosen, Engine
    from flnd.engine.options import Config
    from flnd.engine.store import FailureRow
    from flnd.plugin import Plugin, Target

__all__ = ["Flnd", "open"]

# The home when none is given: `$FLND_HOME`, else `.flnd` in the current folder.
HOME_VARIABLE = "FLND_HOME"
DEFAULT_HOME = ".flnd"
# The kinds `files(kind=...)` filters by.
KINDS = ("image", "video", "audio")


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
        self._added: dict[str, type[Plugin]] = {}
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
        when: str | None = None, limit: int | None = None, dry: bool = False,
    ) -> Run:
        """Download what the targets have now, once: nothing is followed for later syncs.

        Args:
            targets: one target or several: `"ins:example_user"`, a link, or a `Target`. The
                categories go in the target (`"ins:example_user/reels,posts"`, `/all`);
                without them, a known target takes its stored ones, else the plugin's own.
                A single post has none, so naming one is a usage error.
            when: the days to take posts from: `"2026"`, `"2026-06"`, `"2026-06-15"`,
                `"2026-01..2026-03"` or `"30d"`; `""` for every day; None for the home's
                `[defaults] when` in config.toml, which is every day unless it says otherwise.
            limit: at most this many posts per target, or targets per search; None for all.
            dry: plan instead: the run yields a `Planned` event per file and writes nothing.

        Returns:
            The `Run`, not started yet: iterate it, or call `wait()`.

        Raises:
            UsageError: a target, category, `when` or `limit` is wrong, or the home's
                config.toml is.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:  # doctest: +ELLIPSIS
            ...     fl.download("ins:example_user", when="tomorrow")
            Traceback (most recent call last):
            ...
            flnkit.errors.UsageError: when takes a year (2026), a month (2026-06), ...
        """
        from flnd.engine.options import _when

        found = _config(self.home)
        span = _when(found.when if when is None else when)
        if limit is not None and limit < 1:
            raise UsageError("limit must be at least 1")
        resolved = self._checked(targets)
        again = found.again if self._again is None else self._again
        return self._run(f"download {_names(resolved)}", lambda engine: engine.run_download(
            resolved, time=span, limit=limit, dry=dry, again=again))

    def follow(
        self, targets: str | Target | Sequence[str | Target], *, when: str | None = None,
    ) -> Run:
        """Follow the targets, so `sync` keeps them up to date, then download what they have.

        The categories a target names (`"ins:example_user/reels"`) are kept with it, and
        later syncs take those alone; following again with others replaces them. `when`
        bounds this first download only.

        Args:
            targets: one target or several, as `download` takes them.
            when: the days this first download takes posts from, as `download` reads it;
                None for the home's `[defaults] when`.

        Returns:
            The `Run` of the first download, not started yet.

        Raises:
            UsageError: a target, category or `when` is wrong.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:  # doctest: +ELLIPSIS
            ...     fl.follow("zzz:example_user")
            Traceback (most recent call last):
            ...
            flnkit.errors.UsageError: no plugin named zzz
        """
        from flnd.engine.options import _when

        found = _config(self.home)
        span = _when(found.when if when is None else when)
        resolved = self._checked(targets)
        again = found.again if self._again is None else self._again
        return self._run(f"follow {_names(resolved)}", lambda engine: engine.run_download(
            resolved, time=span, sync=True, keep=True, again=again))

    def sync(self, targets: str | Target | Sequence[str | Target] = (),) -> Run:
        """Download what is new for every followed target, or for the ones named.

        The home's `[defaults] when` in config.toml, when it names days, bounds every sync too.

        Args:
            targets: the followed targets to sync; empty for every one.

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
        from flnd.engine.options import _when

        found = _config(self.home)
        span = _when(found.when)
        resolved = self._engine().chosen(_listed(targets))
        named = f"sync {_names(resolved)}" if resolved else "sync"
        return self._run(named, lambda engine: engine.run_sync(resolved, time=span))

    def retry(self) -> Run:
        """Download again every file the home's failures name, however many runs failed it.

        A download or sync already retries a target's failed files first, on at most 3 runs
        each; this takes every one, at once. Each failed post is listed again through its
        source, for fresh URLs, and only its failed files are downloaded: a retry never uses a
        stored URL, which `flnd.db` keeps redacted (src/docs/errors). A target whose listing failed
        whole is downloaded again. A gone file (a 404 or 410) isn't retried; `again=True`
        asks for those.

        Returns:
            The `Run`, not started yet. With nothing to retry, it ends at once with an empty
            report; `report.retried` counts the files it brought back.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.retry().wait().retried
            0
        """
        return self._run("retry", lambda engine: engine.run_retry())

    @property
    def status(self) -> Status | None:
        """The last run that downloaded, with the failures it left: what `flnd status` shows.

        A failure that a later run saved is left out, so this is what still needs doing.

        Returns:
            The `Status`; None when the home has never downloaded.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.status is None
            True
        """
        from flnd.engine import store

        with store.open(self.home) as db:
            run = db.last_run()
            rows = db.failures(run.id) if run is not None else []
        if run is None:
            return None
        return Status(run.command, run.started, run.ended, saved=run.saved,
                      failures=tuple(_failure(row) for row in rows))

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
            flnkit.errors.UsageError: ins:example_user: not a known target; download it first
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
            if source is not None and info.target.plugin != source:
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
        from flnd.engine import store
        from flnd.engine.options import _period

        first = None if since is None else _period(since)[0]
        owner = None
        if target is not None:
            [resolved] = self._resolve(target)
            owner = store.key(resolved)
        with store.open(self.home) as db:
            rows = db.records(target=owner, kind=kind)
        found = []
        for row in rows:
            record = Record(row.key, Path(row.path), row.size, post=row.post_id,
                            category=row.category, date=row.day, at=row.at)
            if first is not None and (_day(record) or first) < first:
                continue
            found.append(record)
        return found

    def check(self, *, fix: bool = False) -> Checked:
        """Compare the history with the disk: files missing, unknown, or of the wrong size.

        It reads every row and walks the home's `media/` folder once; nothing is fetched.
        `fix=True` makes the history match the disk, and never deletes, moves or downloads a
        file: a missing file's row goes, and a file of the wrong size gets its row's size and
        hash from the disk. An unknown file is never adopted, so a home with one isn't `ok`,
        fixed or not (src/docs/storage, "Checking a home").

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
        from flnd.engine import store

        with store.open(self.home) as db:
            return store.check(self.home, db, fix=fix)


    # -----------------------------------------------------------------------------------------
    # Logins and sources
    # -----------------------------------------------------------------------------------------

    def login(self, source: str, how: str) -> Login:
        """Log in to a source, and save the login, encrypted, in the home.

        Args:
            source: the source's code (`"ins"`).
            how: a browser to read its cookies from (`"chrome"`, `"firefox"`, `"edge"`,
                `"brave"`, `"chromium"`, `"safari"` on macOS), the path of a cookies.txt or
                JSON export, or the source's own way (`"qr"`, `"phone"` for tlg), which asks
                its questions in the terminal.

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
            flnkit.errors.UsageError: ins logs in by a browser (chrome, ...) or a cookie file's ...
        """
        from flnd.engine.home import BROWSERS

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
        from flnd.engine import plugins
        from flnd.plugins import PLUGINS

        logins = self._engine().logins
        found = PLUGINS.plugins
        by_code = {plugin.code: plugin for plugin in found if not plugin.error}
        rows = [_installed(PLUGINS[code], by_code.get(code), logins) for code in PLUGINS]
        rows += [Installed(plugin.code, plugin.code, plugin.origin, where=plugin.where,
                           version=plugin.version, disabled=plugin.error)
                 for plugin in found if plugin.error]
        rows += [_installed(cls, None, logins, origin=plugins.SCRIPT)
                 for cls in self._added.values()]
        return rows

    def add(self, source: type[Plugin]) -> None:
        """Add a plugin for this `Flnd` alone: its targets resolve and download like a
        built-in's.

        It comes after the built-ins and the installed plugins, so a code one of them has is
        refused, never replaced.

        Args:
            source: the `flnd.plugin.Plugin` subclass.

        Raises:
            UsageError: it isn't one, or its code is taken.

        Example:
            >>> import flnd
            >>> from flnd.plugin import Plugin, Post, route
            >>> class Example(Plugin):
            ...     code, name, domains, api = "exa", "Example", ("example.com",), 2
            ...     @route("exa:{user}")
            ...     async def user(self, ctx, user):
            ...         yield Post("p1")
            >>> with flnd.open("example-home") as fl:
            ...     fl.add(Example)
            ...     fl.targets(source="exa")
            []
        """
        from flnd.plugin import Plugin

        if not (isinstance(source, type) and issubclass(source, Plugin)):
            raise UsageError("add takes a flnd.plugin.Plugin subclass")
        if source.code in self._sources():
            taken = self._sources()[source.code]
            raise UsageError(f"{source.code}: the code is taken by {taken.__module__}."
                             f"{taken.__qualname__}", hint="give the class another code")
        self._added[source.code] = source

    # -----------------------------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------------------------

    def _sources(self) -> Mapping[str, type[Plugin]]:
        """The built-in plugins and the installed ones, then the ones `add` added.

        A view, not a copy: a built-in plugin is imported only when its code is looked up, so
        resolving `ins:example_user` imports Instagram's module and no other.

        Returns:
            The plugins by code; an added plugin comes first when looked up.
        """
        from collections import ChainMap

        from flnd.plugins import PLUGINS

        return ChainMap(self._added, PLUGINS)

    def _source(self, code: str) -> type[Plugin]:
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
        from flnd.engine.core import Engine

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
        from flnd.plugin import Target

        given = [targets] if isinstance(targets, (str, Target)) else list(targets)
        engine = self._engine()
        return [item if isinstance(item, Target) else engine.resolve([item])[0]
                for item in given]

    def _checked(self, targets: Any) -> list[Chosen]:
        """Targets resolved with their categories, before a run is made.

        Args:
            targets: one target or several, as text or resolved.

        Returns:
            The targets, resolved, in order.

        Raises:
            UsageError: no target, a target no plugin reads, or a category its route lacks.
        """
        resolved = self._engine().chosen(_listed(targets))
        if not resolved:
            raise UsageError("give at least one target")
        return resolved

    def _run(self, name: str, command: Any) -> Run:
        """A `Run` whose loop makes this home's engine and awaits `command` with it.

        The engine is made on the run's loop, with the run's sink and stop state, so the run
        alone decides when the work starts and how it stops.

        Args:
            name: what the run does, for its warning.
            command: called with the engine, giving the command to await; what it returns is
                dropped (the events tell it).

        Returns:
            The run, not started, and remembered so `close()` can stop it.
        """
        async def work(emit: Any, stop: Any) -> None:
            await command(self._engine(emit, stop))

        run = Run(work, name=name, closed=self._closed)
        self._runs.add(run)
        return run


def _installed(cls: type[Plugin], plugin: Any, logins: Mapping[str, str], *,
               origin: str | None = None) -> Installed:
    """A working source's row for `Flnd.sources`.

    Args:
        cls: the source's class.
        plugin: its `flnd.engine.plugins.Plugin` when it is one; None for a built-in or `add`'s.
        logins: the login state of each source with one saved (`Engine.logins`).
        origin: where it came from, when `plugin` doesn't say; None for a built-in.

    Returns:
        The row.
    """
    import flnd
    from flnd.engine import plugins

    if plugin is not None:
        origin, where, version = plugin.origin, plugin.where, plugin.version
    else:
        where = f"{cls.__module__}:{cls.__qualname__}"
        version = flnd.__version__ if origin is None else ""
        origin = origin or plugins.BUILT_IN
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

def _listed(targets: str | Target | Sequence[str | Target]) -> list[str | Target]:
    """One target or several, as a list."""
    from flnd.plugin import Target

    return [targets] if isinstance(targets, (str, Target)) else list(targets)


def _names(targets: Sequence[Chosen]) -> str:
    """The targets as a run's name shows them: the first two, then how many more."""
    shown = " ".join(str(found.target) for found in targets[:2])
    return shown if len(targets) <= 2 else f"{shown} and {len(targets) - 2} more"


def _config(home: Path) -> Config:
    """The home's `config.toml`, read and checked (`flnd.engine.options.config`).

    Its `[defaults]` fill what a call leaves as None: `again` (list each post again, and ask
    again for files recorded gone), and `when`. The library's calls keep their five
    options, so the settings past them live there.

    Args:
        home: the home folder; a home without the file reads as every default.

    Returns:
        The settings.

    Raises:
        UsageError: the file isn't TOML, or one of its settings has a value it doesn't take.
    """
    from flnd.engine.options import config

    return config(home)


# ---------------------------------------------------------------------------------------------
# Reading saved files
# ---------------------------------------------------------------------------------------------

def _failure(row: FailureRow) -> Failure:
    """A failure's row of `flnd.db` as a `Failure`, its error made again from its class.

    Args:
        row: the row.

    Returns:
        The `Failure`; an error class flnd no longer has comes back as a plain `flnd.Error`.
    """
    from flnd import errors

    kind = getattr(errors, row.error, None)
    if not (isinstance(kind, type) and issubclass(kind, errors.Error)):
        kind = errors.Error
    return Failure(row.target, kind(row.message, hint=row.hint, target=row.target,
                                   retryable=row.retryable),
                   post=row.post, path=Path(row.file) if row.file else None,
                   attempts=row.attempts, url=row.url, at=row.at)


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
