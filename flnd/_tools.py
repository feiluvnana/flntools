"""External tools: run them, find or download them, stream their output, and fake them in tests.

    r = sh.run("git", "status", "--short")          # r.code, r.out, r.err
    ff = sh.find("ffmpeg")
    for line in ff.stream("-i", src, "-progress", "pipe:1", dest): ...
    with sh.fake({"ffmpeg": lambda args: (0, "", "")}): ...
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import zipfile
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Union

from flnd import _base
from flnd import _vault as crypto
from flnd import _files as fs
from flnd import _http as http

__all__ = [
    "Error", "MissingError", "Result", "RunError", "Spec", "Tool", "UnsafeError", "fake",
    "find", "run",
]

PathLike = Union[str, "os.PathLike[str]"]

ARCH_ALIASES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
# ffmpeg and its siblings take one dash for every long option, `-version` included.
SINGLE_DASH = frozenset({"ffmpeg", "ffprobe", "ffplay"})
VERSION = re.compile(r"\d+(?:\.\d+)+")
VERSIONS_FILE = "versions.json"
# How much of a streamed tool's stderr a `RunError` keeps.
TAIL_LINES = 20


class Error(_base.Error):
    """The base of every error `flnd._tools` raises."""


class MissingError(Error, _base.MissingError):
    """The requested tool is not on PATH and has no spec for this platform."""


class UnsafeError(Error, _base.UnsafeError):
    """Downloaded tool failed its checksum check or contained an unsafe archive path."""


class RunError(Error):
    """A subprocess exited non-zero when `check=True` or timed out."""

    def __init__(self, message: str, *, code: int = 1, out: str = "", err: str = ""):
        super().__init__(message)
        self.code = code
        self.out = out
        self.err = err


@dataclass(frozen=True)
class Spec:
    """Download specification for one platform's binary or archive."""

    url: str
    sha256: str
    bin: str = ""


@dataclass(frozen=True)
class Result:
    """Captured outcome of a finished subprocess."""

    code: int
    out: str
    err: str
    args: tuple[str, ...] = ()


Handler = Union[Callable[[tuple[str, ...]], tuple[int, str, str]], Result]

# The handlers of the innermost `fake` block; a list so threads the block starts see it too.
_fakes: list[Mapping[str, Handler]] = []
_found: dict[str, Tool] = {}
_versions_lock = threading.Lock()


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
    ) -> Result:
        """Invoke this tool with `args`."""
        return run(self.path, *args, cwd=cwd, env=env, check=check, timeout=timeout)

    def stream(
        self,
        *args: str | os.PathLike[str],
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Iterator[str]:
        """Yield this tool's stdout lines as they come; `RunError` after a non-zero exit."""
        argv = _argv(self.path, *args)
        faked = _faked(argv)
        if faked is not None:
            yield from faked.out.splitlines()
            _checked(faked, check=True)
            return
        yield from _stream(argv, cwd=cwd, env=env, timeout=timeout)

    @property
    def version(self) -> str:
        """The first version number `<tool> --version` prints, or "" when it prints none."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return _parse_version(self)
        cache = _cache_root() / VERSIONS_FILE
        key = os.fspath(self.path.resolve())
        with _versions_lock:
            known = _read_versions(cache)
            entry = known.get(key)
            if isinstance(entry, dict) and entry.get("mtime") == mtime:
                return str(entry.get("version", ""))
            version = _parse_version(self)
            known[key] = {"mtime": mtime, "version": version}
            # A read-only cache costs a spawn next time, not the answer.
            with suppress(fs.Error):
                fs.write(cache, json.dumps(known, indent=1, sort_keys=True))
        return version


def run(
    *args: str | os.PathLike[str],
    cwd: PathLike | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> Result:
    """Run `args` and return its `Result`, raising `RunError` on a non-zero exit when `check`."""
    argv = _argv(*args)
    faked = _faked(argv)
    if faked is not None:
        return _checked(faked, check=check)
    try:
        done = subprocess.run(
            argv, cwd=_cwd(cwd), env=_env(env), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except FileNotFoundError as error:
        raise MissingError(f"{argv[0]}: executable not found") from error
    except subprocess.TimeoutExpired as error:
        raise RunError(f"{argv[0]} timed out after {timeout}s", code=-1) from error
    except OSError as error:
        raise Error(f"{argv[0]}: {error}") from error
    result = Result(done.returncode, done.stdout or "", done.stderr or "", argv)
    return _checked(result, check=check)


def find(name: str, *, specs: Mapping[tuple[str, str], Spec] | None = None) -> Tool:
    """Find `name` on PATH, or download, verify and cache the `Spec` for this platform."""
    if _fakes:
        if name in _fakes[-1]:
            return Tool(Path(name), name)
        raise MissingError(f"{name}: not found; not in sh.fake")
    if name in _found:
        return _found[name]
    on_path = shutil.which(name)
    tool = Tool(Path(on_path), name) if on_path else _download(name, specs)
    _found[name] = tool
    return tool


@contextmanager
def fake(handlers: Mapping[str, Handler]) -> Iterator[None]:
    """Answer every tool run in the block from `handlers`, by tool name, without a subprocess."""
    _fakes.append(handlers)
    try:
        yield
    finally:
        _fakes.pop()


def _argv(*args: str | os.PathLike[str]) -> tuple[str, ...]:
    """`args` as strings, refusing an empty command."""
    if not args:
        raise ValueError("run needs a command")
    return tuple(os.fspath(arg) for arg in args)


def _faked(argv: tuple[str, ...]) -> Result | None:
    """The result the innermost `fake` gives `argv`, or None outside a `fake` block."""
    if not _fakes:
        return None
    path = PurePosixPath(argv[0].replace("\\", "/"))
    name = path.stem if path.suffix.lower() == ".exe" else path.name
    handler = _fakes[-1].get(name)
    if handler is None:
        raise MissingError(f"{name}: not found; not in sh.fake")
    if isinstance(handler, Result):
        return replace(handler, args=argv)
    code, out, err = handler(argv)
    return Result(code, out, err, argv)


def _checked(result: Result, *, check: bool) -> Result:
    """`result`, or a `RunError` when `check` and it exited non-zero."""
    if check and result.code != 0:
        detail = (result.err or result.out).strip() or f"exit status {result.code}"
        raise RunError(
            f"{result.args[0]} failed ({result.code}): {detail}",
            code=result.code, out=result.out, err=result.err,
        )
    return result


def _cwd(cwd: PathLike | None) -> str | None:
    return os.fspath(cwd) if cwd is not None else None


def _env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    """The environment with `env` laid over this process's, or None to inherit it."""
    return {**os.environ, **env} if env else None


def _stream(
    argv: tuple[str, ...], *, cwd: PathLike | None, env: Mapping[str, str] | None,
    timeout: float | None,
) -> Iterator[str]:
    """Run `argv`, yielding stdout lines while a thread keeps stderr's tail."""
    try:
        process = subprocess.Popen(
            argv, cwd=_cwd(cwd), env=_env(env), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as error:
        raise MissingError(f"{argv[0]}: executable not found") from error
    except OSError as error:
        raise Error(f"{argv[0]}: {error}") from error
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    # A full stderr pipe would block the tool, so a thread drains it while stdout is read.
    drain = threading.Thread(target=tail.extend, args=(process.stderr,), daemon=True)
    drain.start()
    expired = threading.Event()
    # The lines come from a blocking read, so a timer kills the tool when time runs out.
    killer = threading.Timer(timeout or 0, lambda: (expired.set(), process.kill()))
    if timeout is not None:
        killer.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            yield line.rstrip("\r\n")
        code = process.wait()
    finally:
        killer.cancel()
        _end(process, drain)
    if expired.is_set():
        raise RunError(f"{argv[0]} timed out after {timeout}s", code=-1, err="".join(tail))
    _checked(Result(code, "", "".join(tail), argv), check=True)


def _end(process: subprocess.Popen[str], drain: threading.Thread) -> None:
    """Kill a tool its reader left early, and close its pipes."""
    if process.poll() is None:
        process.kill()
    process.wait()
    drain.join()
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()


def _parse_version(tool: Tool) -> str:
    """Run the tool's version flag and pick the first version number from what it prints."""
    flag = "-version" if tool.name in SINGLE_DASH else "--version"
    result = tool.run(flag, check=False, timeout=30)
    found = VERSION.search(result.out) or VERSION.search(result.err)
    return found.group(0) if found else ""


def _read_versions(cache: Path) -> dict[str, object]:
    """The cached versions by binary path, or none when the file is missing or broken."""
    try:
        known = json.loads(cache.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return known if isinstance(known, dict) else {}


def _download(name: str, specs: Mapping[tuple[str, str], Spec] | None) -> Tool:
    """Download, verify and cache `name` from the spec for this platform."""
    key = _platform_key()
    spec = specs.get(key) if specs else None
    if spec is None:
        raise MissingError(f"{name}: not on PATH and no download for {key[0]}/{key[1]}")
    bin_name = spec.bin or (f"{name}.exe" if key[0] == "windows" else name)
    target = _cache_root() / name / spec.sha256[:12] / Path(bin_name).name
    target.parent.mkdir(parents=True, exist_ok=True)
    # Two threads finding one tool download it once.
    with fs.lock(target.parent):
        if not target.is_file():
            _provision(spec, bin_name, target)
    return Tool(target, name)


def _platform_key() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    return system, ARCH_ALIASES.get(machine, machine)


def _cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "flntools" / "tools"


def _provision(spec: Spec, bin_name: str, target: Path) -> None:
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
