"""Build a script into a folder that runs on a machine without Python, through PyInstaller.

`build` runs PyInstaller in this process with a generated command line (no .spec file to keep), in
a temporary work folder, adds flnd's private modules and the stdlib's zstd as hidden imports,
then copies the data folders beside the program.

    from flnd import _bundler as bundler
    bundler.build("flnd/__main__.py", name="flnd")    # dist/flnd/flnd, or flnd.exe on Windows

Needs PyInstaller on first use, from flntools' `dist` group (`uv sync --group dist`).
"""

# PyInstaller is only in the `dist` group, so in the dev environment pyright finds its bundled stubs
# but not its source; that is expected here, not a missing dependency.
# pyright: reportMissingModuleSource=false

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import tempfile
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Union

from flnd import _base

__all__ = ["BuildError", "Error", "MissingError", "build"]

PathLike = Union[str, "os.PathLike[str]"]

LAST_LINES = 15


class Error(_base.Error):
    """The base of every error `flnd._bundler` raises."""


class MissingError(Error, _base.MissingError):
    """PyInstaller isn't installed (`uv sync --group dist`)."""


class BuildError(Error, RuntimeError):
    """PyInstaller failed to build the bundle."""


def build(entry: PathLike, *, name: str, hidden: Iterable[str] = (),
          data: Iterable[PathLike] = (), dest: PathLike = "dist") -> Path:
    """Build `entry` into the one-folder bundle `dest/name`, and return that folder.

        bundler.build("tool.py", name="tool", hidden=("mypkg.plugins",), data=("assets",))
    """
    if isinstance(hidden, (str, bytes)):
        raise Error("hidden must be a sequence of module names, not a single str")
    if isinstance(data, (str, bytes, os.PathLike)):
        raise Error("data must be a sequence of paths, not a single path")
    hidden_items = tuple(hidden)
    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        raise MissingError("flnd._bundler needs pyinstaller: uv sync --group dist") from None
    folders = [Path(folder) for folder in data]
    for folder in folders:
        if not folder.is_dir():
            raise Error(f"no data folder at {folder}")
    dest = Path(dest).resolve()
    with tempfile.TemporaryDirectory() as work:
        args = _arguments(entry, name=name, hidden=hidden_items, dest=dest, work=Path(work))
        _run(pyinstaller, args, name)
    bundle = dest / name
    for folder in folders:
        shutil.copytree(folder, bundle / folder.name, dirs_exist_ok=True)
    return bundle


def _arguments(
    entry: PathLike, *, name: str, hidden: Iterable[str], dest: Path, work: Path
) -> list[str]:
    """PyInstaller command line for `build`, with flnd's private modules as hidden imports."""
    # .spec file and build cache go to a temp folder, so there is nothing to clean up.
    imports = [*_private_modules(), *_extra_packages(), *hidden]
    argv = [
        str(Path(entry).resolve()), "--onedir", "--noconfirm", "--log-level", "WARN",
        "--name", name, "--distpath", str(dest), "--workpath", str(work / "build"),
        "--specpath", str(work), "--paths", str(Path(__file__).resolve().parent.parent),
    ]
    for module in imports:
        argv += ["--hidden-import", module]
    # Nothing imports the bundler at run time. Leaving it out keeps PyInstaller out of the bundle.
    return [*argv, "--exclude-module", "flnd._bundler", "--exclude-module", "PyInstaller"]


def _private_modules() -> list[str]:
    """The private modules of flnd as dotted names, for PyInstaller's hidden imports.

    Every `_x.py` beside this file is listed except this bundler. The list comes from the folder,
    not from a hand-kept list, so a new private module can't be left out of the bundle.

    Returns:
        Dotted module names such as `flnd._ui`, sorted.
    """
    folder = Path(__file__).resolve().parent
    names = sorted(path.stem for path in folder.glob("_*.py") if not path.stem.startswith("__"))
    return [f"flnd.{name}" for name in names if name != "_bundler"]


def _extra_packages() -> list[str]:
    """The stdlib's `compression.zstd`, if this Python has it: PyInstaller doesn't follow it."""
    return ["compression.zstd"] if _installed("compression.zstd") else []


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
