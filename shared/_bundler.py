"""Build a script into a folder that runs on a machine without Python, through PyInstaller.

`build` runs PyInstaller in this process with a generated command line (no .spec file to keep), in
a temporary work folder, adds `shared`'s lazily loaded modules as hidden imports, then copies the
data folders beside the program. It wraps PyInstaller's `run` and exposes no `.raw`: a script that
needs more than these options calls PyInstaller itself.

    from shared import bundler
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

import shared

__all__ = ["Error", "build"]

PathLike = Union[str, "os.PathLike[str]"]

# The optional packages shared's modules import inside functions or by name, each added whenever it
# is installed, so a bundle never misses one an extra provides. compression.zstd is the stdlib's on
# Python 3.14 and later, and imageio-ffmpeg's hook collects the ffmpeg binary. shared's own private
# modules need no entry: each lazy module is a hidden import below, and PyInstaller follows the
# plain imports inside its functions (`media`'s `from shared import _media_video`) to the rest.
PACKAGES = ("playwright.sync_api", "cryptography", "pyzipper", "py7zr", "parsel", "jmespath", "jsonpath", "telethon",
            "compression.zstd", "PIL", "pi_heif", "imageio_ffmpeg")
LAST_LINES = 15


class Error(shared.Error):
    """The base of every error `shared.bundler` raises."""


def build(entry: PathLike, *, name: str, hidden: Iterable[str] = (), data: Iterable[PathLike] = (),
          dest: PathLike = "dist") -> Path:
    """Build `entry` into the one-folder bundle `dest/name`, and return that folder.

        bundler.build("tool.py", name="tool", hidden=("mypkg.plugins",), data=("assets",))   # dist/tool/

    PyInstaller can't see lazy imports, so every module `shared` loads lazily, and the packages
    those modules use, are added as hidden imports; `hidden=` adds the script's own. A one-folder
    bundle starts at once, where a one-file one unpacks itself on every run.

    Args:
        entry: the script to build, such as "flnd/__main__.py".
        name: the program's name: the bundle's folder and the executable in it (`name.exe` on Windows).
        hidden: modules the script imports in a way PyInstaller can't see, such as by name.
        data: folders copied beside the program, which finds them at `Path(sys.executable).parent / name`.
        dest: the folder the bundle is built in, relative to the working directory; an earlier
            bundle of the same name in it is replaced.

    Raises:
        ImportError: PyInstaller isn't installed (`uv sync --group dist`).
        FileNotFoundError: a `data` folder doesn't exist; checked before anything is built.
        RuntimeError: PyInstaller failed; the message ends with its last log lines.

    Slow: a build takes from half a minute to a few minutes, and needs no network.
    """
    try:
        import PyInstaller.__main__ as pyinstaller
    except ImportError:
        raise ImportError("shared.bundler needs pyinstaller: uv sync --group dist") from None
    folders = [Path(folder) for folder in data]
    for folder in folders:
        if not folder.is_dir():
            raise FileNotFoundError(f"no data folder at {folder}")
    dest = Path(dest).resolve()
    with tempfile.TemporaryDirectory() as work:
        _run(pyinstaller, _arguments(entry, name=name, hidden=hidden, dest=dest, work=Path(work)), name)
    bundle = dest / name
    for folder in folders:
        shutil.copytree(folder, bundle / folder.name, dirs_exist_ok=True)
    return bundle


def _arguments(entry: PathLike, *, name: str, hidden: Iterable[str], dest: Path, work: Path) -> list[str]:
    """PyInstaller's command line for `build`, with every lazy module of `shared` as a hidden import."""
    # The .spec file and the build cache go to a temp folder, so there is nothing to maintain or clean up.
    lazy = [f"shared._{module}" for module in shared.LAZY_MODULES if module != "bundler"]
    imports = [*lazy, *(package for package in PACKAGES if _installed(package)), *hidden]
    argv = [str(Path(entry).resolve()), "--onedir", "--noconfirm", "--log-level", "WARN", "--name", name,
            "--distpath", str(dest), "--workpath", str(work / "build"), "--specpath", str(work),
            "--paths", str(Path(shared.__file__).resolve().parent.parent)]
    for module in imports:
        argv += ["--hidden-import", module]
    # The bundler is reachable from shared's TYPE_CHECKING imports, and would bring PyInstaller along.
    return [*argv, "--exclude-module", "shared._bundler", "--exclude-module", "PyInstaller"]


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
            raise RuntimeError(f"PyInstaller could not build {name}: {error}\n{lines}".rstrip()) from None
    finally:
        logger.removeHandler(tail)


class _Tail(logging.Handler):
    """Keeps the last lines PyInstaller logs, for the error when it fails."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=LAST_LINES)

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())
