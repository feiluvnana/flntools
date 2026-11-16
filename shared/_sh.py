"""External CLI tools: checksum-verified download, caching, and subprocess execution."""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import tarfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Union

import shared
from shared import _crypto as crypto
from shared import _fs as fs
from shared import _http as http

__all__ = [
    "Error",
    "MissingError",
    "Result",
    "RunError",
    "Spec",
    "Tool",
    "UnsafeError",
    "run",
    "tool",
]

PathLike = Union[str, "os.PathLike[str]"]

ARCH_ALIASES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class Error(shared.Error):
    """The base of every error `shared.sh` raises."""


class MissingError(Error, shared.MissingError):
    """The requested tool is not on PATH and has no spec for this platform."""


class UnsafeError(Error, shared.UnsafeError):
    """Downloaded tool failed its checksum check or contained an unsafe archive path."""


class RunError(Error):
    """A subprocess exited non-zero when `check=True` or timed out."""

    def __init__(self, message: str, *, returncode: int = 1, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class Spec:
    """Download specification for one platform's binary or archive."""

    url: str
    sha256: str
    bin: str = ""


@dataclass(frozen=True)
class Result:
    """Captured outcome of a finished subprocess."""

    returncode: int
    stdout: str
    stderr: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class Tool:
    """A resolved external executable ready to invoke."""

    path: Path
    name: str

    def run(
        self,
        *args: str | os.PathLike[str],
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        timeout: float | None = None,
        capture: bool = True,
    ) -> Result:
        """Invoke this tool with `args`."""
        return run(
            [self.path, *args], cwd=cwd, env=env, check=check, timeout=timeout, capture=capture,
        )


def run(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    cwd: PathLike | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
    capture: bool = True,
) -> Result:
    """Run a command and return its `Result`."""
    argv = tuple(os.fspath(part) for part in cmd)
    merged_env = {**os.environ, **env} if env else None
    try:
        done = subprocess.run(
            argv,
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=merged_env,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise MissingError(f"{argv[0]}: executable not found") from error
    except subprocess.TimeoutExpired as error:
        raise RunError(f"{argv[0]} timed out after {timeout}s", returncode=-1) from error
    except OSError as error:
        raise Error(f"{argv[0]}: {error}") from error
    result = Result(done.returncode, done.stdout or "", done.stderr or "", argv)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit status {result.returncode}"
        raise RunError(
            f"{argv[0]} failed ({result.returncode}): {detail}",
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result


def tool(
    name: str,
    *,
    platforms: Mapping[tuple[str, str], Spec] | None = None,
    dest: PathLike | None = None,
) -> Tool:
    """Locate `name` on PATH or download, verify, and cache the matching platform `Spec`."""
    found = shutil.which(name)
    if found:
        return Tool(Path(found), name)
    key = _platform_key()
    spec = platforms.get(key) if platforms else None
    if spec is None:
        raise MissingError(f"{name}: not on PATH and no spec for {key[0]}/{key[1]}")
    cache = Path(dest) if dest is not None else _default_cache()
    bin_name = spec.bin or (f"{name}.exe" if key[0] == "windows" else name)
    target = cache / name / spec.sha256[:12] / Path(bin_name).name
    if target.is_file():
        return Tool(target, name)
    _provision(spec, bin_name, target)
    return Tool(target, name)


def _platform_key() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    return system, ARCH_ALIASES.get(machine, machine)


def _default_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "flntools" / "tools"


def _provision(spec: Spec, bin_name: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    archive_name = PurePosixPath(spec.url.split("?", 1)[0]).name or "download"
    stage = target.parent / f".{archive_name}.download"
    try:
        http.download(spec.url, stage)
        actual = crypto.digest(stage, algorithm="sha256")
        if actual.lower() != spec.sha256.lower():
            raise UnsafeError(
                f"{archive_name}: sha256 mismatch (expected {spec.sha256}, got {actual})"
            )
        _install_artifact(stage, archive_name, bin_name, target)
    finally:
        stage.unlink(missing_ok=True)


def _install_artifact(stage: Path, archive_name: str, bin_name: str, target: Path) -> None:
    lower = archive_name.lower()
    if lower.endswith(".zip"):
        _extract_zip(stage, bin_name, target)
    elif lower.endswith((".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tar")):
        _extract_tar(stage, bin_name, target)
    else:
        fs.write(target, stage.read_bytes())
    if os.name != "nt":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _extract_zip(archive_path: Path, bin_name: str, target: Path) -> None:
    wanted = bin_name.lstrip("./")
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            member = info.filename.replace("\\", "/").lstrip("./")
            if ".." in PurePosixPath(member).parts:
                raise UnsafeError(f"unsafe zip member {info.filename!r}")
            if member == wanted or member.endswith(f"/{wanted}"):
                fs.write(target, zf.read(info))
                return
    raise MissingError(f"{bin_name!r} not found in {archive_path.name}")


def _extract_tar(archive_path: Path, bin_name: str, target: Path) -> None:
    wanted = bin_name.lstrip("./")
    with tarfile.open(archive_path) as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            clean = member.name.replace("\\", "/").lstrip("./")
            if ".." in PurePosixPath(clean).parts:
                raise UnsafeError(f"unsafe tar member {member.name!r}")
            if clean == wanted or clean.endswith(f"/{wanted}"):
                stream = tf.extractfile(member)
                if stream is None:
                    break
                fs.write(target, stream.read())
                return
    raise MissingError(f"{bin_name!r} not found in {archive_path.name}")
