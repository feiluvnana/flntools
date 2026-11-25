"""The plugin manager: the plugin folder, install and loading.

SPEC.md#for-developers explains it."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import traceback
from collections.abc import Collection, Iterator, Mapping, MutableMapping
from pathlib import Path
from typing import Any

from flnd.engine import resolve
from flnd.errors import PluginError, UsageError
from flnd.plugin import API, CODE, OLD_NAMES, Plugin
from flnd.plugins import _manifest
from flnd.ui.log import ui
from flnkit import clock, files

__all__ = [
    "BUILT_IN",
    "FILE",
    "MODULE_PREFIX",
    "PLUGINS",
    "Registry",
    "SCRIPT",
    "VARIABLE",
    "folder",
    "version",
    "where",
]

# Where a plugin came from, as `flnd plugin list` shows it.
BUILT_IN, FILE, SCRIPT = "built-in", "file", "script"
# The variable naming the plugin folder, which replaces the default one.
VARIABLE = "FLND_PLUGINS"
# The index of what is installed, in the plugin folder.
INDEX = "plugins.json"
VERSION = 1
# A plugin's module name: one no real package has, so a plugin never replaces a module.
MODULE_PREFIX = "_flnd_plugin_"
# A home folder's name, and a cookie file's ending, casefolded: never read, copied or installed.
HOME = ".flnd"
COOKIES = "_cookies.txt"
# Opening a file: never through a link, and never waiting on a pipe.
OPEN = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(
    os, "O_BINARY", 0)
HINT = "install the plugin's own .py file or package folder"


def folder() -> Path:
    """The plugin folder: `$FLND_PLUGINS`, else the platform's config folder.

    Raises:
        RuntimeError: the tests are running (`FLND_TEST_NO_PROFILES`) and `$FLND_PLUGINS`
            isn't set, so the real folder would be used.
    """
    chosen = os.environ.get(VARIABLE)
    if chosen:
        return Path(chosen).expanduser()
    from flnd.engine.config import NO_PROFILES

    if os.environ.get(NO_PROFILES):
        raise RuntimeError(f"{NO_PROFILES} is set: set ${VARIABLE} to a test's folder")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "flnd" / "plugins"
    return Path.home() / ".config" / "flnd" / "plugins"


def where(cls: type[Plugin]) -> str | None:
    """The installed `.py` file or package folder `cls` came from; None for built-ins or `add`."""
    if not cls.__module__.startswith(MODULE_PREFIX):
        return None
    module = sys.modules.get(cls.__module__.split(".", 1)[0])
    if module is None:
        return None
    paths = getattr(module, "__path__", None)
    if paths:
        return str(paths[0])
    located = getattr(module, "__file__", "")
    return str(located) if located else None


def version(cls: type[Plugin]) -> str:
    """An installed plugin module's `__version__`, or `""` when it has none."""
    module = sys.modules.get(cls.__module__.split(".", 1)[0])
    found = getattr(module, "__version__", "") if module is not None else ""
    return found if isinstance(found, str) else ""


class Manager:
    """The installed plugins: what `plugins.json` says of each, and each class once imported.

    Reading the manifest imports nothing; a plugin is imported by `load`, on first use.

    Args:
        where: the plugin folder; None takes `folder()`.
        built_in: the built-ins' codes, which no plugin may take.
    """

    def __init__(self, where: str | os.PathLike[str] | None = None, *,
                 built_in: Collection[str] = ()) -> None:
        self.folder = Path(where) if where is not None else folder()
        self.built_in = frozenset(built_in)
        # Said once each, for the caller to show: an unlisted file, an uninstall.
        self.warnings: list[str] = []
        # Code -> why it is disabled, with the file and line.
        self.disabled: dict[str, str] = {}
        self._classes: dict[str, type[Plugin]] = {}
        self._index: dict[str, dict[str, Any]] | None = None

    @property
    def manifest(self) -> dict[str, Any]:
        """`plugins.json`'s data, `{"version": 1, "plugins": {code: entry}}`, read once.

        A file or package in the folder without an entry is ignored, with a warning, and so is
        an entry with a built-in's code.
        """
        if self._index is None:
            self._index = self._read()
            for code in sorted(self.built_in & set(self._index)):
                del self._index[code]
                self.warnings.append(f"{self.folder / INDEX} lists {code}, a built-in plugin's "
                                     "code; that entry is ignored")
            for path in self._found():
                if path.name.removesuffix(".py") in self.built_in:
                    self.warnings.append(f"{path} has a built-in plugin's code; it is ignored")
                elif path.name.removesuffix(".py") not in self._index:
                    self.warnings.append(f"{path} isn't installed: flnd plugin install {path}")
        return {"version": VERSION, "plugins": self._index}

    def install(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        """Check a plugin's `.py` file or package folder, copy it in, and record its entry.

        It is copied to a temporary folder first, and everything is read from that copy: its
        code, its API, and what is imported and kept. So the bytes checked are the bytes kept.
        An installed code is replaced.

        Returns:
            Its entry in `plugins.json`.

        Raises:
            UsageError: not a `.py` file or a package; a link, a hard link, a special file, a
                home or a cookie file, or a package holding one; a built-in's code.
            PluginError: its API can't be read without running it, or isn't this flnd's; it
                fails to import, has no or several plugin classes, or fails their checks.
        """
        source = Path(path).expanduser()
        package = _refused(source)
        index = self.manifest["plugins"]
        with tempfile.TemporaryDirectory(prefix="flnd-install-") as temporary:
            copy = Path(temporary) / ("plugin" if package else "plugin.py")
            _copy(source, copy, source)
            # Checked again as copied, so nothing swapped in meanwhile is kept.
            _refused(copy, shown=source)
            guess = _literal(_sources(copy), "code")
            guess = guess if isinstance(guess, str) and CODE.fullmatch(guess) else "plugin"
            cls = _checked(copy, guess, shown=source, keep=False)
            # A plain copy, which `_checked` has seen is a code: paths are made from it alone.
            code = str.__str__(cls.code)
            if code in self.built_in:
                raise UsageError(f"{code} is a built-in plugin's code",
                                 hint=f"set another code in {source}")
            name = code if package else f"{code}.py"
            entry = _entry(cls, name, _digest(copy, source), installed=_stamp())
            self._place(copy, name)
        self._forget(code)
        index[code] = entry
        self._write()
        return entry

    def uninstall(self, code: str) -> dict[str, Any]:
        """Remove an installed plugin's file or folder, and its entry.

        Its targets stay, and a warning says so.

        Returns:
            The entry it had.

        Raises:
            UsageError: a built-in, or no plugin installed with that code.
        """
        if code in self.built_in:
            raise UsageError(f"{code} is built in; it can't be uninstalled")
        index = self.manifest["plugins"]
        if not _is_code(code) or code not in index:
            raise UsageError(f"no plugin named {code} is installed",
                             hint=f"installed: {', '.join(sorted(index)) or 'none'}")
        for name in (f"{code}.py", code):
            _remove(self.folder / name)
        entry = index.pop(code)
        self._write()
        self._forget(code)
        self.warnings.append(f"{code} is uninstalled; its targets stay")
        return entry

    def load(self, code: str) -> type[Plugin]:
        """Import an installed plugin once, checking it again if its files changed.

        A plugin that fails is disabled alone, with its own file and line.

        Raises:
            UsageError: a built-in's code, or no plugin installed with that code.
            PluginError: it is disabled; the error says why.
        """
        if code in self._classes:
            return self._classes[code]
        if code in self.built_in:
            raise UsageError(f"{code} is built in; plugins.json's entry for it is ignored")
        if code in self.disabled:
            raise PluginError(f"{code} is disabled: {self.disabled[code]}")
        index = self.manifest["plugins"]
        entry = index.get(code) if _is_code(code) else None
        if entry is None:
            raise UsageError(f"no plugin named {code} is installed")
        try:
            file = entry.get("file")
            if file not in (code, f"{code}.py"):
                raise PluginError(f"its entry names {file!r}, not {code}.py",
                                  hint=f"flnd plugin install {self.folder / code}.py")
            path = self.folder / file
            # Walks it without following links, so an edit that adds one disables it.
            digest = _digest(path, path)
            cls = _checked(path, code, shown=path, keep=True)
            if cls.code != code:
                raise PluginError(f"its plugin's code is {cls.code!r}, not {code!r}",
                                  hint=f"flnd plugin install {path}")
            if digest != entry.get("sha256"):
                index[code] = _entry(cls, file, digest,
                                     installed=str(entry.get("installed", _stamp())))
                self._write()
        except Exception as error:  # any failure disables this plugin alone
            _unload(MODULE_PREFIX + code)
            hint = getattr(error, "hint", "")
            self.disabled[code] = f"{error}; {hint}" if hint else str(error)
            self.warnings.append(f"{code}: disabled: {self.disabled[code]}")
            raise PluginError(f"{code} is disabled: {error}", hint=hint) from error
        self._classes[code] = cls
        return cls

    # -----------------------------------------------------------------------------------------

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads((self.folder / INDEX).read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            self.warnings.append(f"{self.folder / INDEX} can't be read ({error}); "
                                 "install the plugins again")
            return {}
        if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(
                data.get("plugins"), dict):
            self.warnings.append(f"{self.folder / INDEX} isn't version {VERSION}; "
                                 "install the plugins again")
            return {}
        return {code: dict(entry) for code, entry in data["plugins"].items()
                if CODE.fullmatch(str(code)) and isinstance(entry, dict)}

    def _write(self) -> None:
        text = json.dumps(self.manifest, indent=2, sort_keys=True) + "\n"
        files.write(self.folder / INDEX, text)

    def _found(self) -> list[Path]:
        """The `.py` files and package folders in the plugin folder, by name."""
        try:
            entries = sorted(self.folder.iterdir())
        except OSError:
            return []
        return [path for path in entries if not path.name.startswith(("_", "."))
                and ((path.suffix == ".py" and path.is_file())
                     or (path / "__init__.py").is_file())]

    def _place(self, copy: Path, name: str) -> None:
        """Put the checked copy in the folder as `name`, in place of either form of its code."""
        if not _is_code(name.removesuffix(".py")):
            raise PluginError(f"{name!r} isn't named by a plugin's code")
        self.folder.mkdir(parents=True, exist_ok=True)
        dest = self.folder / name
        if copy.is_dir():
            new, old = self.folder / f".{name}.new", self.folder / f".{name}.old"
            _remove(new)
            _remove(old)
            shutil.copytree(copy, new, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
            if dest.exists() or dest.is_symlink():
                os.replace(dest, old)
            os.replace(new, dest)
            _remove(old)
            _remove(self.folder / f"{name}.py")
        else:
            files.write(dest, copy.read_bytes())
            _remove(self.folder / name.removesuffix(".py"))

    def _forget(self, code: str) -> None:
        """Drop what was loaded of `code`, so the next `load` imports the new files."""
        self._classes.pop(code, None)
        self.disabled.pop(code, None)
        _unload(MODULE_PREFIX + code)


class Registry(MutableMapping[str, "type[Plugin]"]):
    """Built-in and installed plugins by code, each imported only when first asked for.

    Built-ins come from `flnd.plugins._manifest` and never touch the plugin folder; installed
    plugins come from `Manager`. Resolving reads `_manifest` and `plugins.json` without
    importing any plugin.
    """

    def __init__(self) -> None:
        self._overrides: dict[str, type[Plugin]] = {}
        self._built_in: dict[str, type[Plugin]] = {}
        self._removed: set[str] = set()
        self._mgr: Manager | None = None
        self._stamp: tuple[Path, int | None] | None = None
        self._warned = 0

    def manager(self) -> Manager:
        """The `Manager` of the current plugin folder, refreshed when `plugins.json` changes."""
        target = folder()
        try:
            mtime: int | None = (target / INDEX).stat().st_mtime_ns
        except OSError:
            mtime = None
        if self._mgr is None or self._stamp != (target, mtime):
            self._mgr = Manager(target, built_in=_manifest.PLUGINS)
            self._stamp = (target, mtime)
            self._warned = 0
        self.flush(self._mgr)
        return self._mgr

    def flush(self, mgr: Manager) -> None:
        """Log any warnings `mgr` recorded since the last call."""
        if mgr is not self._mgr:
            return
        while self._warned < len(mgr.warnings):
            ui.warn("{warning}", warning=mgr.warnings[self._warned])
            self._warned += 1

    def built_in_codes(self) -> list[str]:
        """The built-in codes (in order) plus any test overrides, without touching disk."""
        known = [code for code in _manifest.PLUGINS if code not in self._removed]
        return known + [code for code in self._overrides if code not in _manifest.PLUGINS]

    def manifest(self) -> dict[str, Mapping[str, Any]]:
        """Manifest entries for built-ins and overrides, importing no built-in plugin."""
        entries: dict[str, Mapping[str, Any]] = {
            code: entry for code, entry in _manifest.PLUGINS.items()
            if code not in self._removed
        }
        for code, cls in self._overrides.items():
            entries[code] = resolve.entry(cls)
        return entries

    def installed(self, code: str | None = None) -> dict[str, Mapping[str, Any]]:
        """Installed entries from `plugins.json`, without importing any plugin."""
        mgr = self.manager()
        index = mgr.manifest["plugins"]
        self.flush(mgr)
        if code is not None:
            return {code: index[code]} if code in index and code not in self._overrides else {}
        return {name: entry for name, entry in index.items() if name not in self._overrides}

    def __getitem__(self, code: str) -> type[Plugin]:
        if code in self._overrides:
            return self._overrides[code]
        if code in _manifest.PLUGINS and code not in self._removed:
            if code not in self._built_in:
                entry = _manifest.PLUGINS[code]
                module = importlib.import_module(str(entry["module"]))
                self._built_in[code] = getattr(module, str(entry["cls"]))
            return self._built_in[code]
        mgr = self.manager()
        try:
            return mgr.load(code)
        except UsageError:
            raise KeyError(code) from None
        finally:
            self.flush(mgr)

    def __setitem__(self, code: str, cls: type[Plugin]) -> None:
        if code in _manifest.PLUGINS and cls is self._built_in.get(code):
            self._removed.discard(code)
            self._overrides.pop(code, None)
            return
        self._removed.discard(code)
        self._overrides[code] = cls

    def __delitem__(self, code: str) -> None:
        if code in self._overrides:
            del self._overrides[code]
            if code in _manifest.PLUGINS:
                self._removed.add(code)
            return
        if code in _manifest.PLUGINS and code not in self._removed:
            self._removed.add(code)
            return
        raise KeyError(code)

    def clear(self) -> None:
        """Clear overrides and temporarily hide built-ins (for `unittest.mock.patch.dict`)."""
        self._overrides.clear()
        self._removed = set(_manifest.PLUGINS)

    def __iter__(self) -> Iterator[str]:
        yield from self.built_in_codes()
        mgr = self.manager()
        index = mgr.manifest["plugins"]
        self.flush(mgr)
        for code in sorted(index):
            if code in _manifest.PLUGINS or code in self._overrides:
                continue
            try:
                mgr.load(code)
            except PluginError:
                continue
            finally:
                self.flush(mgr)
            yield code

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, code: object) -> bool:
        if code in self._overrides or (code in _manifest.PLUGINS and code not in self._removed):
            return True
        if not isinstance(code, str):
            return False
        mgr = self.manager()
        try:
            mgr.load(code)
        except (UsageError, PluginError):
            return False
        finally:
            self.flush(mgr)
        return True

    def copy(self) -> dict[str, type[Plugin]]:
        """Every working plugin as a plain dictionary, importing those not loaded yet."""
        return dict(self.items())

    def __repr__(self) -> str:
        return f"<flnd.engine.plugins.Registry {', '.join(self.built_in_codes())}>"


PLUGINS: Registry = Registry()


# ---------------------------------------------------------------------------------------------
# What may be installed: judged by names and file types, then read only through `_read`
# ---------------------------------------------------------------------------------------------

def _refused(source: Path, *, shown: Path | None = None) -> bool:
    """Refuse what may never be installed, from its names and types alone; True for a package."""
    shown = shown or source
    for path in (source, source.absolute(), source.resolve()):
        if _forbidden(path.name) or HOME in (part.casefold() for part in path.parts):
            raise UsageError(f"{shown} is a home or cookie file; never a plugin", hint=HINT)
    try:
        info = os.lstat(source)
    except OSError as error:
        raise UsageError(f"{shown} can't be read: {error.strerror}", hint=HINT) from None
    if stat.S_ISDIR(info.st_mode) and (source / "__init__.py").is_file():
        _tree(source, shown)
        return True
    if not stat.S_ISLNK(info.st_mode) and not (stat.S_ISREG(info.st_mode)
                                               and source.suffix == ".py"):
        raise UsageError(f"{shown} is neither a .py file nor a folder holding __init__.py",
                         hint=HINT)
    _tree(source, shown)
    return False


def _forbidden(name: str) -> bool:
    folded = name.casefold()
    return folded == HOME or folded.endswith(COOKIES)


def _tree(root: Path, shown: Path) -> list[tuple[Path, int, int]]:
    """The regular files of a plugin file or package, with their device and inode, by name."""
    found: list[tuple[Path, int, int]] = []

    def judge(path: Path, info: os.stat_result) -> None:
        inside = path.relative_to(root)
        named = shown / inside if inside.parts else shown
        what = (f"{shown} holds {named}, " if inside.parts else f"{shown} is ")
        if inside.parts and _forbidden(path.name):
            raise UsageError(what + "a home or cookie file", hint=HINT)
        if stat.S_ISLNK(info.st_mode):
            raise UsageError(what + f"a link to {os.readlink(path)}",
                             hint="install the file itself; flnd copies a plugin in, "
                                  "never links it")
        if stat.S_ISDIR(info.st_mode):
            for entry in sorted(os.scandir(path), key=lambda entry: entry.name):
                if entry.name != "__pycache__":
                    judge(Path(entry.path), entry.stat(follow_symlinks=False))
        elif not stat.S_ISREG(info.st_mode):
            raise UsageError(what + "a special file, not a regular one", hint=HINT)
        elif info.st_nlink > 1:
            raise UsageError(what + "a file with other hard links, which its name can't "
                             "speak for", hint="install a copy of it")
        else:
            found.append((path, info.st_dev, info.st_ino))

    judge(root, os.lstat(root))
    return found


def _read(path: Path, device: int, inode: int, shown: Path) -> bytes:
    """A file's bytes, only if it is still the regular file `_tree` judged."""
    try:
        fd = os.open(path, OPEN)
    except OSError as error:
        raise UsageError(f"{shown} changed while it was installed: {error.strerror}",
                         hint=HINT) from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1
                or (info.st_dev, info.st_ino) != (device, inode)):
            raise UsageError(f"{shown} changed while it was installed", hint=HINT)
        with os.fdopen(os.dup(fd), "rb") as stream:
            return stream.read()
    finally:
        os.close(fd)


def _copy(source: Path, dest: Path, shown: Path) -> None:
    """Copy a plugin file or package, reading each file only through `_read`."""
    for path, device, inode in _tree(source, shown):
        inside = path.relative_to(source)
        target = dest / inside if inside.parts else dest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_read(path, device, inode, shown / inside if inside.parts else shown))


def _digest(path: Path, shown: Path) -> str:
    """The SHA-256 of a file, or of a package's files and their names, as hex."""
    found = _tree(path, shown)
    if not path.is_dir():
        return hashlib.sha256(_read(*found[0], shown)).hexdigest()
    total = hashlib.sha256()
    for file, device, inode in found:
        total.update(file.relative_to(path).as_posix().encode() + b"\0")
        total.update(hashlib.sha256(_read(file, device, inode, shown)).digest())
    return total.hexdigest()


def _remove(path: Path) -> None:
    """Remove a file, a link or a folder; nothing when it isn't there. A link is never followed."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


# ---------------------------------------------------------------------------------------------
# What a plugin declares, read without running it
# ---------------------------------------------------------------------------------------------

def _sources(path: Path) -> list[Path]:
    """The `.py` files of a plugin file or package."""
    if not path.is_dir():
        return [path]
    return sorted(found for found in path.rglob("*.py") if "__pycache__" not in found.parts)


def _trees(paths: list[Path], shown: Path, root: Path) -> list[ast.Module]:
    """Each file parsed; a syntax error is refused here, so nothing of the plugin runs."""
    parsed = []
    for path in paths:
        try:
            parsed.append(ast.parse(path.read_bytes(), str(path)))
        except (SyntaxError, ValueError) as error:
            raise PluginError(_described(error, root, shown)) from None
    return parsed


def _plugin_classes(trees: list[ast.Module]) -> list[ast.ClassDef]:
    """The classes that subclass `flnd.plugin.Plugin` where the files say so plainly."""
    names, modules = set(), {"flnd.plugin"}
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "flnd.plugin":
                names |= {"Plugin" if alias.name == "*" else alias.asname or alias.name
                          for alias in node.names if alias.name in ("Plugin", "*")}
            elif isinstance(node, ast.ImportFrom) and node.module == "flnd":
                modules |= {alias.asname or alias.name for alias in node.names
                            if alias.name == "plugin"}
            elif isinstance(node, ast.Import):
                modules |= {alias.asname for alias in node.names
                            if alias.name == "flnd.plugin" and alias.asname}
    classes = [node for tree in trees for node in ast.walk(tree)
               if isinstance(node, ast.ClassDef)]
    imports = [(alias.name, alias.asname) for tree in trees for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) for alias in node.names if alias.asname]
    found: list[ast.ClassDef] = []
    while True:
        more = [cls for cls in classes if cls not in found and any(
            _names_plugin(base, names, modules) for base in cls.bases)]
        if not more:
            return found
        found += more
        names |= {cls.name for cls in more}
        names |= {alias for name, alias in imports if name in names}


def _names_plugin(base: ast.expr, names: set[str], modules: set[str]) -> bool:
    if isinstance(base, ast.Name):
        return base.id in names
    if isinstance(base, ast.Attribute):
        return base.attr in names or (base.attr == "Plugin" and ast.unparse(base.value) in modules)
    return False


def _scope(cls: ast.ClassDef) -> Iterator[ast.AST]:
    """Every node a class body runs in its own scope: not inside its functions or classes."""
    pending: list[ast.AST] = list(cls.body)
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                 ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))


def _literals(cls: ast.ClassDef, name: str) -> tuple[list[Any], bool]:
    """What a class body sets `name` to, as literals; False when one setting isn't plain."""
    plain: dict[int, Any] = {}
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant):
            pairs = [(node.target, node.value)]
        elif isinstance(node, ast.Assign):
            pairs = []
            for target in node.targets:
                if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple) and len(
                        target.elts) == len(node.value.elts):
                    pairs += zip(target.elts, node.value.elts)
                else:
                    pairs.append((target, node.value))
        else:
            continue
        plain.update((id(target), value.value) for target, value in pairs
                     if isinstance(target, ast.Name) and target.id == name
                     and isinstance(value, ast.Constant))
    stores = [node for node in _scope(cls) if isinstance(node, ast.Name)
              and node.id == name and isinstance(node.ctx, ast.Store)]
    return [plain[id(node)] for node in stores if id(node) in plain], all(
        id(node) in plain for node in stores)


def _literal(paths: list[Path], name: str) -> Any:
    """The one literal the plugin classes in `paths` set `name` to; None when there isn't one."""
    try:
        trees = [ast.parse(path.read_bytes()) for path in paths]
    except (SyntaxError, ValueError):
        return None
    values = {value for cls in _plugin_classes(trees) for value in _literals(cls, name)[0]}
    return values.pop() if len(values) == 1 else None


def _api(paths: list[Path], code: str, shown: Path, root: Path) -> None:
    """Refuse a plugin unless every `api` its plugin classes set is the literal `API`."""
    classes = _plugin_classes(_trees(paths, shown, root))
    read = [_literals(cls, "api") for cls in classes]
    values = {value for found, _ in read for value in found}
    if not classes or not values or not all(plain for _, plain in read) or not all(
            type(value) is int for value in values):
        raise PluginError(f"{shown}: its plugin API can't be read without running it",
                          hint=f"set api = {API} in the body of a class that subclasses "
                               "flnd.plugin.Plugin, as a plain number")
    if max(values) > API:
        raise PluginError(f"{code} is written for plugin API {max(values)}; this flnd has {API}",
                          hint="it needs a newer flnd")
    if min(values) < API:
        used = sorted({old for cls in classes for node in _scope(cls) for old in OLD_NAMES
                       if _sets(node, old)})
        hint = "; ".join(f"{old} becomes {OLD_NAMES[old]}" for old in used) or (
            "see the writing-a-plugin page of flnd's docs")
        raise PluginError(f"{code} is written for plugin API {min(values)}; this flnd has "
                          f"{API}", hint=hint)


def _sets(node: ast.AST, name: str) -> bool:
    """Whether a node of a class body defines or sets `name`."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name == name
    return isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store)


# ---------------------------------------------------------------------------------------------
# Importing it
# ---------------------------------------------------------------------------------------------

def _checked(path: Path, code: str, *, shown: Path, keep: bool) -> type[Plugin]:
    """Import a plugin from `path` as `_flnd_plugin_<code>`, and give its one plugin class."""
    _api(_sources(path), code, shown, path)
    name = MODULE_PREFIX + code
    with _isolated(name, keep=keep):
        try:
            module = _import(name, path)
        except Exception as error:
            raise PluginError(_described(error, path, shown),
                              hint=getattr(error, "hint", "")) from error
        found = [value for value in vars(module).values()
                 if inspect.isclass(value) and issubclass(value, Plugin) and value is not Plugin
                 and _inside_module(value.__module__, name)
                 and isinstance(getattr(value, "code", None), str)]
    if len(found) != 1:
        what = ", ".join(cls.__name__ for cls in found) or "no flnd.plugin.Plugin subclass"
        raise PluginError(f"{shown} defines {what}; a plugin defines exactly one",
                          hint="see the writing-a-plugin page of flnd's docs")
    if found[0].api != API:
        raise PluginError(f"{shown}: its api is {found[0].api!r} once it has run, not {API}",
                          hint=f"set api = {API} in the class, and never change it after")
    # The code names its file in the folder, so it is checked as it is once the module ran.
    if not _is_code(found[0].code):
        code = found[0].code
        what = repr(code) if type(code) is str else f"a {type(code).__name__}, not a plain str"
        raise PluginError(f"{shown}: its code is {what} once it has run",
                          hint='set code = "exm": 2 to 16 lowercase letters and digits, and '
                               "never change it after")
    return found[0]


def _is_code(code: object) -> bool:
    """Whether `code` is a plugin's code: the only text that becomes a path in the folder."""
    return type(code) is str and CODE.fullmatch(code) is not None


def _import(name: str, path: Path) -> Any:
    """Import a file or package from its path, never putting its folder on `sys.path`."""
    if path.is_dir():
        spec = importlib.util.spec_from_file_location(
            name, path / "__init__.py", submodule_search_locations=[str(path)])
    else:
        spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"can't import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _isolated(name: str, *, keep: bool) -> Iterator[None]:
    """Import `name` afresh; a failure, or `keep=False`, leaves `sys.modules` as it was."""
    saved = {key: value for key, value in sys.modules.items() if _inside_module(key, name)}
    _unload(name)
    kept = False
    try:
        yield
        kept = keep
    finally:
        if not kept:
            _unload(name)
            sys.modules.update(saved)


def _unload(name: str) -> None:
    for key in [key for key in sys.modules if _inside_module(key, name)]:
        del sys.modules[key]


def _inside_module(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _described(error: BaseException, path: Path, shown: Path) -> str:
    """`<file>, line <n>: <what>`, at the deepest frame in the plugin's own files (S7)."""
    what = f"{type(error).__name__}: {error}" if not isinstance(error, PluginError) else (
        str(error))
    places = [(error.filename, error.lineno)] if isinstance(error, SyntaxError) else []
    places += [(frame.filename, frame.lineno)
               for frame in reversed(traceback.extract_tb(error.__traceback__))]
    for filename, line in places:
        if not filename:
            continue
        try:
            inside = Path(filename).resolve().relative_to(path.resolve())
        except (OSError, ValueError):
            continue
        return f"{shown / inside if inside.parts else shown}, line {line}: {what}"
    return f"{shown}: {what}"


def _entry(cls: type[Plugin], file: str, digest: str, *, installed: str) -> dict[str, Any]:
    """A plugin's entry in `plugins.json`: what resolving and help read without importing it."""
    return {
        "name": cls.name, **resolve.entry(cls), "api": API, "file": file, "sha256": digest,
        "installed": installed,
    }


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock.wall()))
