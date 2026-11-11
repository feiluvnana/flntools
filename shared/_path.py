"""A few functions that `pathlib` lacks. Each takes a `str` or `Path` and returns a `Path`."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator, Union

PathLike = Union[str, "os.PathLike[str]"]

UNSAFE_CHARACTERS = re.compile(r'[\\/*?:"<>|\x00-\x1f\x7f]')
SEPARATOR_RUNS = re.compile(r"[\s_]+")
# Windows refuses these names with any extension, in any case.
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{kind}{n}" for kind in ("COM", "LPT") for n in range(1, 10)}


def safename(text: str, *, fallback: str = "untitled", maxlen: int = 200) -> str:
    """A file or folder name that is safe on Windows, macOS, and Linux."""
    name = UNSAFE_CHARACTERS.sub("_", text)
    name = SEPARATOR_RUNS.sub("_", name).strip("._ ")
    name = _cut_utf8(name, maxlen).strip("._ ")
    if not name:
        return fallback
    base, dot, rest = name.partition(".")
    if base.upper() in RESERVED_NAMES:
        name = f"{base}_{dot}{rest}"
    return name


def mkdir(p: PathLike) -> Path:
    """Create the folder and its parents if needed, and return it."""
    folder = Path(p)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def unique(p: PathLike) -> Path:
    """`p` when it does not exist, otherwise the first free `name (n).ext`."""
    candidate = Path(p)
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    # "bundle.tar.gz" keeps ".tar.gz" together: "bundle (1).tar.gz".
    if Path(stem).suffix == ".tar":
        stem, suffix = Path(stem).stem, ".tar" + suffix
    n = 1
    while True:
        candidate = candidate.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def expand(p: PathLike) -> Path:
    """Expand `~` and environment variables, then resolve to an absolute path."""
    return Path(os.path.expandvars(os.path.expanduser(os.fspath(p)))).resolve()


@contextmanager
def atomic(p: PathLike, mode: str = "wb", encoding: str | None = None) -> Iterator[IO]:
    """Write to `p.part`, then move it into place only when the block succeeds."""
    if "w" not in mode:
        raise ValueError(f"path.atomic needs a write mode such as 'wb' or 'w', not {mode!r}")
    target = Path(p)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    try:
        with open(part, mode, encoding=encoding) as handle:
            yield handle
        part.replace(target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _cut_utf8(text: str, maxlen: int) -> str:
    return text.encode("utf-8")[:maxlen].decode("utf-8", "ignore")
