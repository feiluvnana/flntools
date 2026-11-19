"""`Flnd` and `open`: the library a script uses, over the engine.

src/docs/library explains it."""

from __future__ import annotations

import datetime
import os
import sys
import threading
import weakref
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flnd.engine.events import Checked, Failure, Installed, Record, Status, Tracked
from flnd.errors import PluginError, UsageError
from flnd.plugin import Account

if TYPE_CHECKING:
    from flnd.engine._engine import Chosen, Engine, TargetInfo
    from flnd.engine.run import Run
    from flnd.engine.store import FailureRow
    from flnd.plugin import Ctx, Plugin, Target

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
        again: whether every `get` lists each post again, past the stop at saved posts and the
            saved cursors, and asks again for files the site answered 404 or 410 for (recorded
            as gone). None (or False) doesn't; the CLI's `--again` sets it.

    Returns:
        The `Flnd` of that home, to use in a `with` block.

    Example:
        >>> import flnd
        >>> with flnd.open("example-home") as fl:
        ...     fl.targets.list()
        []
    """
    return Flnd(home, again=again)


class Flnd:
    """flnd for one home folder: each command of the CLI as a method or a group.

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
        # The targets the home knows: list, follow, unfollow, retry, clean.
        self.targets = Targets(self)
        # The accounts: login, logout, default.
        self.auth = Auth(self)
        # The plugins: built-in, installed, added: install, uninstall.
        self.plugins = Plugins(self)
        # Chrome's extension: install, uninstall, connected, version.
        self.extension = Extension(self)
        # The settings of `config.toml`: read, set, unset.
        self.config = Config(self)
        # Whether `get` asks again for everything.
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

    def get(
        self, *targets: str | Target, limit: int | None = None, when: str | None = None,
        dry: bool = False,
    ) -> Run:
        """Run the targets: get what they have that isn't saved, and follow them.

        A target that isn't a single post is added to the followed ones, so a `get` with no
        target takes it later. The categories a target names (`"ins:example_user/reels"`) are
        kept with it; naming others replaces them.

        Args:
            targets: `"ins:example_user"`, a link, or a `Target`; none gets every followed
                target, or, when none is followed, ends at once with an empty report.
            limit: at most this many posts per target, or targets per search; None for all.
            when: the days to take posts from: `"2026"`, `"2026-06"`, `"2026-06-15"`,
                `"2026-01..2026-03"` or `"30d"`; `""` for every day; None for the home's
                `[defaults] when` in config.toml, which is every day unless it says otherwise.
            dry: plan instead: the run yields a `Planned` event per file and writes nothing.

        Returns:
            The `Run`, not started yet: iterate it, or call `wait()`.

        Raises:
            UsageError: a target, category, `when` or `limit` is wrong, or the home's
                config.toml is.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:  # doctest: +ELLIPSIS
            ...     fl.get("ins:example_user", when="tomorrow")
            Traceback (most recent call last):
            ...
            flnkit.errors.UsageError: when takes a year (2026), a month (2026-06), ...
        """
        span = self._when(when)
        if limit is not None and limit < 1:
            raise UsageError("limit must be at least 1")
        again = bool(self._again)
        if not targets and not (limit or dry or again):
            return self._run("get", lambda engine: engine.run_sync((), time=span))
        named = list(targets) or [row.target for row in self.targets if row.follow]
        if not named:
            return self._run("get", lambda engine: engine.run_sync((), time=span))
        resolved = self._checked(named)
        return self._run(f"get {_names(resolved)}", lambda engine: engine.run_download(
            resolved, time=span, limit=limit, dry=dry, sync=True, keep=True, again=again))

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
    # Files and checks
    # -----------------------------------------------------------------------------------------

    def files(
        self, *, target: str | Target | None = None, kind: str | None = None,
        since: datetime.date | str | None = None,
    ) -> list[Record]:
        """The saved files, read from the home's `flnd.db`, oldest saved first.

        Args:
            target: only this target's files; None for every target.
            kind: only `"image"`, `"video"` or `"audio"` files; None for all.
            since: only posts from this day on, a date or written as `when` writes one end
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
        from flnd.engine import config, store

        if isinstance(since, datetime.date):
            first: datetime.date | None = since
        else:
            first = None if since is None else config.when(since).since
        owner = None
        if target is not None:
            [resolved] = self._resolve([target])
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

    def add(self, plugin: type[Plugin]) -> None:
        """Add a plugin for this `Flnd` alone: its targets resolve and get like a built-in's.

        It comes after the built-ins and the installed plugins, so a code one of them has is
        refused, never replaced.

        Args:
            plugin: the `flnd.plugin.Plugin` subclass.

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
            ...     fl.targets.list(plugin="exa")
            []
        """
        from flnd.plugin import Plugin

        if not (isinstance(plugin, type) and issubclass(plugin, Plugin)):
            raise UsageError("add takes a flnd.plugin.Plugin subclass")
        if plugin.code in self._sources():
            taken = self._sources()[plugin.code]
            raise UsageError(f"{plugin.code}: the code is taken by {taken.__module__}."
                             f"{taken.__qualname__}", hint="give the class another code")
        self._added[plugin.code] = plugin

    def plugin(self, code: str, *,
               account: str | None = None) -> AbstractAsyncContextManager[Ctx]:
        """Use a plugin's account in a script, on the caller's running event loop.

        It opens the home's store and the account's cookies, starts a scheduler on the caller's
        loop (so retries, holds and walls work as in a run), and gives the plugin's `Ctx`.
        Leaving the block writes the changed cookies and the plugin's state back and closes it
        all, an error included. Only async code can use it; there is no thread.

        Args:
            code: the plugin's code (`"ins"`).
            account: the account's name; None takes the plugin's default one.

        Returns:
            An async context manager of the plugin's `Ctx`.

        Raises:
            UsageError: `code` is no plugin's code, or the home's config.toml is wrong.
            LoginError: the plugin has no such account, needs a login it lacks, or the vault's
                key is gone.

        Example:
            >>> import asyncio, flnd
            >>> async def main():
            ...     with flnd.open("example-home") as fl:
            ...         async with fl.plugin("nope"):
            ...             pass
            >>> asyncio.run(main())  # doctest: +ELLIPSIS
            Traceback (most recent call last):
            ...
            flnkit.errors.UsageError: unknown plugin 'nope'. Plugins: ...
        """
        return self._engine().script(code, account)

    # -----------------------------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------------------------

    def _sources(self) -> Mapping[str, type[Plugin]]:
        """The built-in plugins and the installed ones, then the ones `add` added."""
        from collections import ChainMap

        from flnd.engine.plugins import PLUGINS

        return ChainMap(self._added, PLUGINS)

    def _plugin(self, code: str) -> type[Plugin]:
        """The plugin of a code, built-in, installed or added."""
        sources = self._sources()
        if code not in sources:
            raise UsageError(f"unknown plugin {code!r}. Plugins: {', '.join(sources)}")
        return sources[code]

    def _when(self, when: str | None) -> Any:
        """The days a call takes posts from: `when` as given, else the home's `[defaults]
        when` (`flnd.engine.config`), checked with every other setting before any run.

        Raises:
            UsageError: `when`, or the home's config.toml, is wrong.
        """
        from flnd.engine import config

        found = config.settings(self.home, plugins=self._sources())
        return config.when(found.defaults.when if when is None else when)

    def _engine(self, emit: Any = None, stop: Any = None) -> Engine:
        """An engine of this home and its plugins, sending events to `emit`."""
        from flnd.engine._engine import Engine

        return Engine(home=self.home, sources=self._sources(), emit=emit, stop=stop)

    def _resolve(self, targets: Sequence[str | Target]) -> list[Target]:
        """Each target resolved through its plugin; a `Target` is taken as it is."""
        from flnd.plugin import Target

        engine = self._engine()
        return [item if isinstance(item, Target) else engine.resolve([item])[0]
                for item in targets]

    def _checked(self, targets: Sequence[str | Target]) -> list[Chosen]:
        """Targets resolved with their categories, before a run is made."""
        resolved = self._engine().chosen(list(targets))
        if not resolved:
            raise UsageError("give at least one target")
        return resolved

    def _run(self, name: str, command: Any) -> Run:
        """A `Run` whose loop makes this home's engine and awaits `command` with it."""
        from flnd.engine.run import Run

        async def work(emit: Any, stop: Any) -> None:
            await command(self._engine(emit, stop))

        run = Run(work, name=name, closed=self._closed)
        self._runs.add(run)
        return run


class Targets:
    """The targets a home knows: iterate it for `Tracked` rows, or change them.

    Reached as `Flnd.targets`.
    """

    def __init__(self, owner: Flnd) -> None:
        self._owner = owner

    def __iter__(self) -> Iterator[Tracked]:
        return iter(self.list())

    def list(self, *, plugin: str | None = None) -> list[Tracked]:
        """Every target the home knows, followed or not, with its files and last run.

        Args:
            plugin: only this plugin's targets (`"ins"`); None for every plugin.

        Returns:
            One `Tracked` row per target.

        Raises:
            UsageError: `plugin` is no plugin's code.
        """
        if plugin is not None:
            self._owner._plugin(plugin)
        return [_tracked(info) for info in self._owner._engine().target_ls(plugin=plugin)]

    def follow(self, *targets: str | Target) -> list[Tracked]:
        """Follow targets, so `get` with none takes them; nothing is downloaded.

        Raises:
            UsageError: a target is wrong, or is a single post.
        """
        engine = self._owner._engine()
        return [_tracked(info) for info in engine.target_follow(list(targets), on=True)]

    def unfollow(self, *targets: str | Target) -> list[Tracked]:
        """Stop following known targets. Their files and history stay.

        Raises:
            UsageError: a target is wrong, or the home doesn't know it.
        """
        engine = self._owner._engine()
        return [_tracked(info) for info in engine.target_follow(list(targets), on=False)]

    def retry(self, *targets: str | Target) -> list[Tracked]:
        """Make known targets' failed files retryable again: the next `get` tries them.

        A file the site said is gone (a 404 or 410) is never retried.

        Raises:
            UsageError: a target is wrong, or the home doesn't know it.
        """
        engine = self._owner._engine()
        return [_tracked(info) for info in engine.target_retry(list(targets))]

    def clean(self, *targets: str | Target, media: bool = False) -> list[Tracked]:
        """Forget known targets: their rows, files rows, failures, cursors, heads and gaps.

        Args:
            targets: the targets to forget.
            media: also delete their recorded files, sidecars, links, extracted folders and the
                folders left empty. Files under `media/` that no row names are never deleted.

        Returns:
            Each target as it stood when it was forgotten.

        Raises:
            UsageError: a target is wrong, or the home doesn't know it.
        """
        engine = self._owner._engine()
        return [_tracked(info) for info in engine.target_clean(list(targets), media=media)]


class Auth:
    """The accounts of a home: iterate it for `Account` rows, or log in and out.

    Reached as `Flnd.auth`.
    """

    def __init__(self, owner: Flnd) -> None:
        self._owner = owner

    def __iter__(self) -> Iterator[Account]:
        from flnd.engine import auth, store

        home = self._owner.home
        auth.seal_plaintext(home)
        with store.open(home) as db:
            return iter([auth.account(row) for row in db.accounts()])

    def login(self, plugin: str, *, method: str | None = None, path: str | None = None,
              name: str | None = None) -> Account:
        """Log an account in, and seal the login in the home's vault.

        Args:
            plugin: the plugin's code (`"ins"`).
            method: one of the plugin's login methods: a browser (`"chrome"`, `"firefox"`,
                ...), `"browser"` for a window of flnd's Chromium at the site's login page,
                `"file"` for a cookies.txt or JSON export (with `path`), or the plugin's own
                (`"qr"`, `"phone"` for tlg). None takes the only one the plugin has, or `"file"`
                when `path` is given.
            path: the cookie file, for `"file"`.
            name: the account's name; None takes the site's own name for it.

        Returns:
            The account after it.

        Raises:
            UsageError: `plugin` or `method` is wrong, or the plugin has several methods and
                none was named.
            LoginError: the browser has no login for the site, or the site refused it.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:  # doctest: +ELLIPSIS
            ...     fl.auth.login("ins", method="netscape")
            Traceback (most recent call last):
            ...
            flnkit.errors.UsageError: ins logs in by ...
        """
        from flnd.engine import auth
        from flnd.plugin import Cookies

        cls = self._owner._plugin(plugin)
        if cls.login is None:
            raise UsageError(f"{plugin} needs no login")
        methods = cls.login.methods
        if method is None:
            method = "file" if path is not None else (methods[0] if len(methods) == 1 else None)
        if method is None:
            raise UsageError(f"{plugin}: how to log in?",
                             hint=f"flnd auth login {plugin} <method>, a method of "
                                  f"{', '.join(methods)}")
        if method not in methods:
            raise UsageError(f"{plugin} logs in by {', '.join(methods)}, not {method!r}")
        if method == "file" and not (path and Path(path).expanduser().is_file()):
            raise UsageError(f"{plugin}: a file login needs the path of a cookie file",
                             hint=f"flnd auth login {plugin} file <path>")
        return auth.login(self._owner.home, cls(), method, path=path, name=name,
                          interactive=not isinstance(cls.login, Cookies))

    def logout(self, plugin: str, *, name: str | None = None) -> Account:
        """Log an account out: its secrets and its row. The default when no `name` is given.

        Returns:
            The account that was removed.

        Raises:
            UsageError: `plugin` is no plugin's code, or has no such account.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     fl.auth.logout("ins")
            Traceback (most recent call last):
            ...
            flnkit.errors.UsageError: ins: no account
        """
        from flnd.engine import auth

        self._owner._plugin(plugin)
        return auth.logout(self._owner.home, plugin, name)

    def default(self, plugin: str, name: str) -> Account:
        """Make an account the one the plugin's runs use when none is named.

        Raises:
            UsageError: `plugin` is no plugin's code, or has no such account.
        """
        from flnd.engine import auth

        self._owner._plugin(plugin)
        return auth.default(self._owner.home, plugin, name)


class Plugins:
    """The plugins a home can use: iterate it for `Installed` rows, or install one.

    Reached as `Flnd.plugins`.
    """

    def __init__(self, owner: Flnd) -> None:
        self._owner = owner

    def __iter__(self) -> Iterator[Installed]:
        """The built-ins, the installed plugins (disabled ones too, saying why), then `add`'s.

        Reading it imports every plugin, as `flnd plugin list` does.

        Example:
            >>> import flnd
            >>> with flnd.open("example-home") as fl:
            ...     [row.origin for row in fl.plugins if row.code == "ins"]
            ['built-in']
        """
        from flnd.engine import plugins

        registry = plugins.PLUGINS
        rows = [_installed(registry[code]) for code in registry.built_in_codes()]
        mgr = registry.manager()
        index = mgr.manifest["plugins"]
        registry.flush(mgr)
        for code, entry in sorted(index.items()):
            if code in registry._overrides or code in mgr.built_in:
                continue
            where = str(mgr.folder / str(entry.get("file", code)))
            try:
                cls = mgr.load(code)
            except PluginError:
                registry.flush(mgr)
                rows.append(Installed(code, str(entry.get("name", code)), plugins.FILE,
                                      where=where, api=0, disabled=mgr.disabled[code]))
            else:
                registry.flush(mgr)
                rows.append(_installed(cls, origin=plugins.FILE,
                                       where=plugins.where(cls) or where,
                                       version=plugins.version(cls)))
        rows += [_installed(cls, origin=plugins.SCRIPT)
                 for cls in self._owner._added.values()]
        return iter(rows)

    def install(self, path: str | os.PathLike[str]) -> Installed:
        """Check a plugin's `.py` file or package folder, copy it into the plugin folder, and
        record it. An installed code is replaced.

        Raises:
            UsageError: not a plugin file or package, or a built-in's code.
            PluginError: its API isn't this flnd's, or it fails its checks.
        """
        from flnd.engine.plugins import PLUGINS

        mgr = PLUGINS.manager()
        try:
            code = _code(mgr.install(path))
        finally:
            PLUGINS.flush(mgr)
        return next(row for row in self if row.code == code)

    def uninstall(self, code: str) -> Installed:
        """Remove an installed plugin. Its accounts and targets stay.

        Returns:
            The plugin's row as it was.

        Raises:
            UsageError: a built-in, or no plugin installed with that code.
        """
        from flnd.engine.plugins import PLUGINS

        found = next((row for row in self if row.code == code), None)
        mgr = PLUGINS.manager()
        try:
            mgr.uninstall(code)
        finally:
            PLUGINS.flush(mgr)
        assert found is not None
        return found


class Extension:
    """flnd's Chrome extension: install it, and ask whether it is connected.

    Reached as `Flnd.extension`.
    """

    def __init__(self, owner: Flnd) -> None:
        self._owner = owner

    def install(self) -> None:
        """Copy the extension, write its host for every Chromium browser found, say how to
        load it in Chrome, and wait up to 10 minutes for it to connect. The popup acts on this
        home, and may ask for the domains of the installed plugins.
        """
        from flnd.engine import extension

        domains = [domain for row in self._owner.plugins if row.origin != "built-in"
                   for domain in row.domains]
        extension.install(home=self._owner.home.absolute(), domains=domains)

    def uninstall(self) -> list[Path]:
        """Remove the extension, its token and its host manifests.

        Returns:
            What was removed.
        """
        from flnd.engine import extension

        return extension.uninstall()

    def host(self, origin: str | None) -> int:
        """Serve the extension on standard input and output: Chrome starts it as `flnd host`.

        Raises:
            UsageError: Chrome named another extension, or `install` hasn't written the token.
        """
        from flnd.engine import extension

        return extension.host(origin)

    @property
    def version(self) -> str | None:
        """The connected extension's version; None when it isn't connected."""
        from flnd.engine import extension

        return extension.probe()

    @property
    def connected(self) -> bool:
        """Whether the extension answers now."""
        return self.version is not None


class Config(Mapping[str, Any]):
    """The home's settings, by dotted name: `"defaults.when"`, `"plugins.ins.pace"`.

    A bare name (`"when"`) is read as one of `[defaults]`. Iterating gives every `[defaults]`
    key and the keys of each plugin a config file names. Reached as `Flnd.config`.
    """

    def __init__(self, owner: Flnd) -> None:
        self._owner = owner

    def _read(self) -> Any:
        from flnd.engine import config

        return config.settings(self._owner.home, plugins=self._owner._sources())

    def __getitem__(self, key: str) -> Any:
        from flnd.engine import config

        found = self._read()
        tables, name = config._known(key, self._owner._sources())
        if tables == ["defaults"]:
            value = getattr(found.defaults, name)
        else:
            plugin = found.plugin(tables[1])
            value = getattr(plugin, name) if name in config.PLUGINS else getattr(
                plugin.settings, name)
        return list(value) if isinstance(value, tuple) else value

    def __iter__(self) -> Iterator[str]:
        from flnd.engine import config

        found = self._read()
        yield from (f"defaults.{name}" for name in config.DEFAULTS)
        for code in found.plugins:
            yield from (f"plugins.{code}.{name}" for name in config.PLUGINS)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def where(self, key: str) -> str:
        """Where a key's value comes from: `"home"`, `"user"`, `"argument"` or `"default"`."""
        return str(self._read().origin(key))

    def default(self, key: str) -> Any:
        """A key's value when no file sets it.

        Raises:
            UsageError: flnd takes no such setting.
        """
        from flnd.engine import config

        tables, name = config._known(key, self._owner._sources())
        if tables == ["defaults"]:
            value = getattr(config.Defaults(), name)
        else:
            value = getattr(config.PluginConfig(), name, None)
        return list(value) if isinstance(value, tuple) else value

    def set(self, key: str, value: Any) -> None:
        """Set a key in `<home>/config.toml` (`defaults.exec`, in the user file), keeping the
        file's comments.

        Raises:
            UsageError: the key isn't one flnd takes, or the value is one it doesn't take; the
                file is left as it was.
        """
        from flnd.engine import config

        config.write(key, value, home=self._owner.home, user=config.user_file(),
                     plugins=self._owner._sources())

    def unset(self, key: str) -> None:
        """Remove a key from its file, so it takes the next file's value or the default.

        Raises:
            UsageError: flnd takes no such setting.
        """
        from flnd.engine import config

        config.unset(key, home=self._owner.home, user=config.user_file(),
                     plugins=self._owner._sources())


def _tracked(info: TargetInfo) -> Tracked:
    """A target's `TargetInfo` as the public `Tracked`."""
    return Tracked(info.target, follow=info.follow, at=info.at, files=info.files,
                   size=info.size, seen=info.seen, categories=info.categories,
                   complete=info.complete, query=info.query, account=info.account)


def _installed(cls: type[Plugin], *, origin: str | None = None, where: str | None = None,
               version: str = "") -> Installed:
    """A working plugin's row for `Flnd.plugins`."""
    from flnd.engine import plugins

    where = where or f"{cls.__module__}:{cls.__qualname__}"
    # flnd is imported by now: the library is reached through it.
    version = getattr(sys.modules.get("flnd"), "__version__", "") if origin is None else version
    origin = origin or plugins.BUILT_IN
    methods = cls.login.methods if cls.login is not None else ()
    return Installed(cls.code, cls.name, origin, where=where, version=version, api=cls.api,
                     domains=tuple(cls.domains), forms=tuple(cls.forms),
                     categories=tuple(cls.categories), logins=tuple(methods))


def _code(entry: Mapping[str, Any]) -> str:
    """The code of the plugin an install just recorded: its file's name without `.py`."""
    file = str(entry["file"])
    return file[:-3] if file.endswith(".py") else file


def _names(targets: Sequence[Chosen]) -> str:
    """The targets as a run's name shows them: the first two, then how many more."""
    shown = " ".join(str(found.target) for found in targets[:2])
    return shown if len(targets) <= 2 else f"{shown} and {len(targets) - 2} more"


def _failure(row: FailureRow) -> Failure:
    """A failure's row of `flnd.db` as a `Failure`, its error made again from its class."""
    from flnd import errors

    kind = getattr(errors, row.error, None)
    if not (isinstance(kind, type) and issubclass(kind, errors.Error)):
        kind = errors.Error
    return Failure(row.target, kind(row.message, hint=row.hint, target=row.target,
                                   retryable=row.retryable),
                   post=row.post, path=Path(row.file) if row.file else None,
                   attempts=row.attempts, url=row.url, at=row.at)


def _day(record: Record) -> datetime.date | None:
    """The day a record counts by for `since`: its post's, else the day it was saved."""
    if record.date is not None:
        return record.date
    try:
        return datetime.datetime.fromisoformat(record.at).date() if record.at else None
    except ValueError:
        return None
