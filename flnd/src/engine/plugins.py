"""Plugins: installed packages and files in the user's plugin folder, found lazily.

src/docs/writing-a-plugin explains it.

A plugin here is one that isn't built in. flnd finds them in this order, after the built-ins:
1. installed packages that declare an entry point in the `flnd.sources` group:
   `[project.entry-points."flnd.sources"] exm = "flnd_example:Example"`;
2. the user's plugin folder (`folder()`): `$FLND_SOURCES`, else `~/.config/flnd/sources/`
   (`%APPDATA%\\flnd\\sources\\` on Windows), holding `<code>.py` files and `<code>/` packages;
3. `Flnd.add(cls)` in a script, which the library keeps itself (`flnd._library`).

A later one with a code already taken is refused with a warning, never shadowing the first.

Who calls it: the plugin registry (`flnd.plugins.Registry`), which runs `discover` the first
time a code it doesn't know is asked for, and `load` the first time a plugin's class is needed.
What it calls: `importlib` and `importlib.metadata`, `flnd.plugin` to check what a plugin
defines, and `flnd.ui.log` for the warnings.

How loading stays lazy:
1. `discover` reads names only: an entry point's name and a file's name are the plugin's code,
   so nothing is imported to learn which codes exist. It reads the package metadata once
   (a few milliseconds) and lists one folder.
2. `load` imports one plugin, on the first use of its code: an `exm:…` target imports `exm`
   alone. A link imports plugins only when no built-in's domains match it
   (`flnd.engine.core.owner`),
   and `flnd sources` imports them all. `flnd --help` imports none.

Rules a change must keep:
- **Never load from a home.** A home is often the current folder, which may be a cloned
  repository or an unpacked archive; code there isn't the user's own. Plugins load only from
  the folder above and from installed packages. `tests/test_plugins.py` keeps a
  `<home>/sources/evil.py` that must never run.
- **Containment.** An import error, or a class that fails its checks when it is made (a newer
  plugin API, a bad pattern), disables that plugin alone, with a warning naming its file and
  line. The plugin folder is never put on `sys.path`, so a stray `json.py` in it can't shadow
  the standard library; a `<code>/` package imports its own modules relatively.
- It is not a security boundary: a plugin runs in flnd's process with the user's logins, like
  any code the user installs.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flnd.ui.log import ui

if TYPE_CHECKING:
    import flnd.plugin

__all__ = ["BUILT_IN", "FILE", "PACKAGE", "SCRIPT", "Plugin", "discover", "folder", "load"]

# Where a source came from, as `flnd sources` shows it.
BUILT_IN, PACKAGE, FILE, SCRIPT = "built-in", "package", "file", "script"
# The entry point group an installed package declares its sources in.
GROUP = "flnd.sources"
# The variable naming the plugin folder, which replaces the default one.
VARIABLE = "FLND_SOURCES"
# The name a file plugin's module gets in `sys.modules`: never a name a real package could have,
# so a plugin can't replace a module by its file name.
MODULE_PREFIX = "_flnd_plugin_"


@dataclass
class Plugin:
    """One plugin found: its code, where it came from, and, once loaded, its class or its error.

    Args:
        code: the source's code, from the entry point's or the file's name.
        origin: `PACKAGE` or `FILE`.
        where: the entry point's value (`flnd_example:Example`) or the file's or folder's path.
        version: the package's version, or a file's `__version__`; "" when it has none.
        error: why it is disabled; "" while it works (or isn't loaded yet).
        cls: its class once loaded; None before, or when it failed.

    Example:
        >>> Plugin("exm", FILE, "/plugins/exm.py").error
        ''
    """

    code: str
    origin: str
    where: str
    version: str = ""
    error: str = ""
    cls: type[flnd.plugin.Plugin] | None = None
    # How to import it: an entry point's `load`, or a file's import; None once refused.
    _load: Callable[[], Any] | None = field(default=None, repr=False)


def folder() -> Path:
    """The user's plugin folder: `$FLND_SOURCES`, else the platform's config folder.

    Returns:
        The folder, `~` expanded; it may not exist.
    """
    chosen = os.environ.get(VARIABLE)
    if chosen:
        return Path(chosen).expanduser()
    # Windows keeps per-user settings under the roaming AppData folder.
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "flnd" / "sources"
    return Path.home() / ".config" / "flnd" / "sources"


def discover(taken: Mapping[str, str]) -> list[Plugin]:
    """Every plugin, from installed packages then the plugin folder, without importing any.

    A plugin whose code is taken (by a built-in, or by a plugin found before it) is returned
    disabled, with a warning, so `flnd sources` can say why; so is a file whose name isn't a
    code.

    Args:
        taken: the codes already used, each with what uses it ("the built-in Instagram").

    Returns:
        The plugins in discovery order: packages by entry point name, then files and folders
        by name.
    """
    from flnd.plugin import CODE

    owners = dict(taken)
    found: list[Plugin] = []
    for plugin in [*_packages(), *_files(folder())]:
        if plugin.error:
            pass
        elif not CODE.fullmatch(plugin.code):
            plugin.error = (f"{plugin.where}: {plugin.code!r} isn't a plugin code; name it "
                            "2 to 16 lowercase letters and digits, what targets start with")
        elif plugin.code in owners:
            plugin.error = (f"{plugin.where}: the code {plugin.code} is taken by "
                            f"{owners[plugin.code]}; a plugin never replaces another")
        else:
            owners[plugin.code] = f"{plugin.origin} {plugin.where}"
        if plugin.error:
            plugin._load = None
            _warn(plugin)
        found.append(plugin)
    return found


def load(plugin: Plugin) -> type[flnd.plugin.Plugin] | None:
    """Import a plugin once and check what it gives; on any failure, disable it with a warning.

    Args:
        plugin: a plugin `discover` found; a second call gives what the first did.

    Returns:
        The source's class; None when it is disabled (`plugin.error` says why).
    """
    if plugin.cls is not None or plugin._load is None:
        return plugin.cls
    loader, plugin._load = plugin._load, None
    try:
        made = loader()
        plugin.cls = _checked(plugin, made)
    except Exception as error:
        plugin.error = _described(plugin, error)
        _warn(plugin)
        return None
    return plugin.cls


# ---------------------------------------------------------------------------------------------
# Finding them
# ---------------------------------------------------------------------------------------------

def _packages() -> list[Plugin]:
    """The plugins installed packages declare, by entry point name.

    Returns:
        One plugin per entry point of the `flnd.sources` group, not loaded.
    """
    from importlib import metadata

    try:
        points = metadata.entry_points(group=GROUP)
    except Exception as error:  # a broken install's metadata must not stop flnd
        ui.debug("plugins: can't read installed packages: {error}", error=error)
        return []
    found = []
    for point in sorted(points, key=lambda point: point.name):
        version = getattr(point.dist, "version", "") or ""
        found.append(Plugin(point.name, PACKAGE, point.value, version=version,
                            _load=point.load))
    return found


def _files(where: Path) -> list[Plugin]:
    """The plugins in a plugin folder: `<code>.py` files and `<code>/` packages, by name.

    Names starting with `_` or `.` are left alone (`__pycache__`, editor files), as is a folder
    without an `__init__.py`.

    Args:
        where: the plugin folder; one that doesn't exist holds none.

    Returns:
        One plugin per file or package, not loaded.
    """
    try:
        entries = sorted(where.iterdir())
    except OSError:
        return []
    found = []
    for path in entries:
        if path.name.startswith(("_", ".")):
            continue
        if path.is_file() and path.suffix == ".py":
            found.append(Plugin(path.stem, FILE, str(path), _load=_importer(path.stem, path)))
        elif path.is_dir() and (path / "__init__.py").is_file():
            found.append(Plugin(path.name, FILE, str(path), _load=_importer(path.name, path)))
    return found


def _importer(code: str, path: Path) -> Callable[[], Any]:
    """A function that imports a plugin file or package from its path, as its own module.

    Args:
        code: the plugin's code, which names its module (`_flnd_plugin_<code>`).
        path: the `.py` file, or the package's folder.

    Returns:
        The function; it returns the module, or raises what importing it raised.
    """
    def run() -> Any:
        name = MODULE_PREFIX + code
        if path.is_dir():
            spec = importlib.util.spec_from_file_location(
                name, path / "__init__.py", submodule_search_locations=[str(path)])
        else:
            spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"can't import {path}")
        module = importlib.util.module_from_spec(spec)
        # Kept in `sys.modules` while it runs, so a package's relative imports find it; taken
        # out again when it fails, so nothing half-made stays importable.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module

    return run


# ---------------------------------------------------------------------------------------------
# Checking what a plugin gives
# ---------------------------------------------------------------------------------------------

def _checked(plugin: Plugin, made: Any) -> type[flnd.plugin.Plugin]:
    """The plugin class a loaded plugin gives, checked against its code.

    An entry point names its class; a file's module is searched for the `flnd.plugin.Plugin`
    subclass it defines with its file's name as its code. The class's own checks already ran
    when it was made, so a newer plugin API or a bad pattern has failed by now.

    Args:
        plugin: the plugin.
        made: what loading gave: the entry point's object, or the file's module.

    Returns:
        The class.

    Raises:
        PluginError: it gives no plugin class, or one with another code.
    """
    import flnd.plugin
    from flnd.errors import PluginError

    if plugin.origin == FILE:
        version = getattr(made, "__version__", "")
        plugin.version = version if isinstance(version, str) else ""
        classes = [value for value in vars(made).values()
                   if inspect.isclass(value) and issubclass(value, flnd.plugin.Plugin)
                   and value is not flnd.plugin.Plugin
                   and _inside(value.__module__, made.__name__) and getattr(value, "code", None)]
        mine = [cls for cls in classes if cls.code == plugin.code]
        if mine:
            return mine[0]
        if classes:
            raise PluginError(f"its plugin's code is {classes[0].code!r}, not its file's name",
                              hint=f"name the file {classes[0].code}.py, or set "
                                   f'code = "{plugin.code}"')
        raise PluginError("it defines no flnd.plugin.Plugin subclass",
                          hint="see the writing-a-plugin page of flnd's docs")
    if not (inspect.isclass(made) and issubclass(made, flnd.plugin.Plugin)):
        raise PluginError(f"the entry point gives {made!r}, not a flnd.plugin.Plugin subclass")
    if made.code != plugin.code:
        raise PluginError(f"its plugin's code is {made.code!r}, not the entry point's name",
                          hint=f"name the entry point {made.code}, or set "
                               f'code = "{plugin.code}"')
    return made


def _inside(module: str, package: str) -> bool:
    """Whether `module` is `package` or one of its submodules (a `<code>/` plugin's own)."""
    return module == package or module.startswith(package + ".")


def _described(plugin: Plugin, error: BaseException) -> str:
    """Why a plugin failed, naming the file and line where it did: what the warning says.

    Args:
        plugin: the plugin that failed.
        error: what loading it raised.

    Returns:
        `<file>, line <n>: <what>`, with the error's hint after a semicolon when it has one.
    """
    from flnd.errors import Error

    place = _place(plugin, error)
    what = str(error) if isinstance(error, Error) else f"{type(error).__name__}: {error}"
    hint = getattr(error, "hint", None)
    text = f"{place}: {what}" if place else what
    return f"{text}; {hint}" if hint else text


def _place(plugin: Plugin, error: BaseException) -> str:
    """The file and line a plugin's error comes from: the deepest frame in its own code.

    A syntax error names its own place. Otherwise the traceback is read from the deepest
    frame up: the first frame in the plugin's file or package wins; for an installed package,
    the first frame that isn't importlib's or flnd's own (flnd raises a class's `PluginError`
    from `src/plugin.py`, but the mistake is where the class is).

    Args:
        plugin: the plugin.
        error: what loading it raised.

    Returns:
        `<file>, line <n>`; the plugin's `where` when no frame is its own.
    """
    if isinstance(error, SyntaxError) and error.filename:
        return f"{error.filename}, line {error.lineno}"
    flnd_folder = str(Path(__file__).resolve().parent)
    own = Path(plugin.where).resolve() if plugin.origin == FILE else None
    for frame in reversed(traceback.extract_tb(error.__traceback__)):
        filename = frame.filename
        if own is not None:
            # A file plugin's own path, or a module inside its `<code>/` folder.
            try:
                inside = Path(filename).resolve().is_relative_to(own)
            except (OSError, ValueError):
                inside = False
            if inside:
                return f"{filename}, line {frame.lineno}"
        elif not (filename.startswith("<") or "importlib" in filename
                  or filename.startswith(flnd_folder)):
            return f"{filename}, line {frame.lineno}"
    return plugin.where


def _warn(plugin: Plugin) -> None:
    """Say, once, that a plugin is disabled and why."""
    ui.warn("{code}: disabled: {error}", code=plugin.code, error=plugin.error)
