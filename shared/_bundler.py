"""Build a script into a folder that runs on a machine without Python, through PyInstaller.

`build` runs PyInstaller in this process with a generated command line (no .spec file to keep), in
a temporary work folder, adds `shared`'s lazily loaded modules and installed extras as hidden
imports, then copies the data folders beside the program.

    from shared import bundler
    bundler.build("flnd/__main__.py", name="flnd")    # dist/flnd/flnd, or flnd.exe on Windows

Needs PyInstaller on first use, from flntools' `dist` group (`uv sync --group dist`).
"""

# PyInstaller is only in the `dist` group, so in the dev environment pyright finds its bundled stubs
# but not its source; that is expected here, not a missing dependency.
# pyright: reportMissingModuleSource=false

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import os
import re
import shutil
import tempfile
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Union

import shared

__all__ = ["BuildError", "Error", "MissingError", "build"]

PathLike = Union[str, "os.PathLike[str]"]

LAST_LINES = 15


class Error(shared.Error):
    """The base of every error `shared.bundler` raises."""


class MissingError(Error, shared.MissingError):
    """PyInstaller isn't installed (`uv sync --group dist`)."""


class BuildError(Error, RuntimeError):
    """PyInstaller failed to build the bundle."""


def build(entry: PathLike, *, name: str, hidden: Sequence[str] = (),
          data: Sequence[PathLike] = (), dest: PathLike = "dist") -> Path:
    """Build `entry` into the one-folder bundle `dest/name`, and return that folder.

        bundler.build("tool.py", name="tool", hidden=("mypkg.plugins",), data=("assets",))
    """
    if isinstance(hidden, (str, bytes)):
        raise Error("hidden must be a sequence of module names, not a single str")
    if isinstance(data, (str, bytes, os.PathLike)):
        raise Error("data must be a sequence of paths, not a single path")
    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        raise MissingError("shared.bundler needs pyinstaller: uv sync --group dist") from None
    folders = [Path(folder) for folder in data]
    for folder in folders:
        if not folder.is_dir():
            raise Error(f"no data folder at {folder}")
    dest = Path(dest).resolve()
    with tempfile.TemporaryDirectory() as work:
        args = _arguments(entry, name=name, hidden=hidden, dest=dest, work=Path(work))
        _run(pyinstaller, args, name)
    bundle = dest / name
    for folder in folders:
        shutil.copytree(folder, bundle / folder.name, dirs_exist_ok=True)
    return bundle


def _arguments(
    entry: PathLike, *, name: str, hidden: Sequence[str], dest: Path, work: Path
) -> list[str]:
    """PyInstaller command line for `build`, with lazy `shared` modules as hidden imports."""
    # .spec file and build cache go to a temp folder, so there is nothing to clean up.
    lazy = [f"shared._{module}" for module in shared.LAZY_MODULES if module != "bundler"]
    imports = [*lazy, *(pkg for pkg in _extra_packages() if _installed(pkg)), *hidden]
    argv = [
        str(Path(entry).resolve()), "--onedir", "--noconfirm", "--log-level", "WARN",
        "--name", name, "--distpath", str(dest), "--workpath", str(work / "build"),
        "--specpath", str(work), "--paths", str(Path(shared.__file__).resolve().parent.parent),
    ]
    for module in imports:
        argv += ["--hidden-import", module]
    # Bundler is reachable from shared's TYPE_CHECKING imports and would bring PyInstaller along.
    return [*argv, "--exclude-module", "shared._bundler", "--exclude-module", "PyInstaller"]


def _extra_packages() -> list[str]:
    """Top-level import names for optional dependencies declared in flntools' extras."""
    try:
        reqs = importlib.metadata.requires("flntools") or ()
    except importlib.metadata.PackageNotFoundError:
        reqs = ()
    extra_dists = {
        _norm_dist(req.split(";", 1)[0])
        for req in reqs
        if ";" in req and "extra ==" in req.split(";", 1)[1]
    }
    by_dist: dict[str, list[str]] = {}
    for pkg, dists in importlib.metadata.packages_distributions().items():
        for dist in dists:
            by_dist.setdefault(_norm_dist(dist), []).append(pkg)
    found: list[str] = []
    for dist in sorted(extra_dists):
        for pkg in sorted(by_dist.get(dist, ())):
            if not pkg.startswith("_") and pkg not in found:
                found.append(pkg)
    if _installed("compression.zstd"):
        found.append("compression.zstd")
    return found


def _norm_dist(spec: str) -> str:
    name = re.split(r"[<>=!~;\[\s]", spec, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _run(pyinstaller: Any, argv: list[str], name: str) -> None:
    tail = _Tail()
    logger = logging.getLogger("PyInstaller")
    logger.addHandler(tail)
    try:
        pyinstaller.run(argv)
    except (Exception, SystemExit) as error:
        if not (isinstance(error, SystemExit) and error.code in (0, None)):
            lines = "\n".join(tail.lines)
            msg = f"PyInstaller could not build {name}: {error}\n{lines}".rstrip()
            raise BuildError(msg) from None
    finally:
        logger.removeHandler(tail)


class _Tail(logging.Handler):
    """Keeps the last lines PyInstaller logs, for the error when it fails."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=LAST_LINES)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())
