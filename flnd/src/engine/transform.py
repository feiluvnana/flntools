"""`flnd transform`: a command run over saved files, each replaced in place when it succeeds.

SPEC.md#transform explains it.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flnd.engine import jobs, store
from flnd.engine.events import Failed, Planned, Saved, Skipped
from flnd.engine.jobs import Job, Kind
from flnd.errors import DiskError, Error, Stopped, ToolError, UsageError
from flnd.plugin import Target
from flnkit import files

__all__ = ["Item", "items", "run", "split"]

# The two placeholders, filled in one pass, so a name holding `{out}` stays literal.
PLACEHOLDER = re.compile(r"\{(in|out)\}")
# How much of the program's standard error is kept, for the failure's last line.
TAIL = 4096
# The temporary file is the original's name with this before its extension.
MARK = ".flnd"


@dataclass(frozen=True)
class Item:
    """One file to transform: where it is, and the row it belongs to."""

    key: str
    target: Target
    path: Path
    post: str
    category: str
    day: datetime.date | None


def split(command: str) -> list[str]:
    """The command as an argument list, split once like a POSIX shell; no shell ever runs it.

    Raises:
        UsageError: the text is not a command, lacks `{in}` or `{out}`, or names no program.
    """
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    if os.name == "nt":
        lexer.escape = ""
    try:
        argv = list(lexer)
    except ValueError as error:
        raise UsageError(f"transform: {error}: {command!r}") from None
    if not argv:
        raise UsageError("transform needs a command", hint='"ffmpeg -y -i {in} {out}"')
    for name in ("in", "out"):
        if not any(f"{{{name}}}" in argument for argument in argv):
            raise UsageError(f"transform: the command has no {{{name}}}: {command!r}",
                             hint='the input is {in} and the temporary result is {out}')
    if shutil.which(argv[0]) is None:
        raise UsageError(f"transform: {argv[0]}: no such program",
                         hint="give a program on PATH, or its full path")
    return argv


def items(rows: Iterable[store.FileRow], targets: Mapping[str, Target], kinds: Sequence[str],
          only: set[str] | None) -> list[Item]:
    """The files of `rows` of the wanted kinds, in row order: each row's file, and the files an
    archive of it made (`FileRow.extracted`); a file that isn't there is left out.

    Args:
        rows: the rows that name files on disk.
        targets: each target's key and its `Target`; one not here is made from its key.
        kinds: the kinds wanted, from `files.IMAGE`, `files.VIDEO` and `files.AUDIO`.
        only: the keys of the targets to take; None for all.
    """
    found: list[Item] = []
    for row in rows:
        if only is not None and row.target not in only:
            continue
        target = targets.get(row.target) or Target(row.target)
        paths = [row.path, *(json.loads(row.extracted) if row.extracted else [])]
        for name in paths:
            path = Path(name)
            if path.is_file() and kind_of(path) in kinds:
                found.append(Item(row.key, target, path, row.post_id, row.category, row.day))
    return found


def kind_of(path: Path) -> str | None:
    """What a file is: its name's kind, else the kind of what its first bytes say."""
    found = files.kind_of(path.name)
    if found is None and (extension := files.sniff(path)):
        found = files.kind_of(f"file{extension}")
    return found


async def run(*, db: store.Store, scheduler: jobs.Scheduler, argv: Sequence[str], command: str,
              found: Sequence[Item], dry: bool, workers: int, stop: Any,
              emit: Callable[[Any], Any], tally: Any) -> None:
    """Transform the files, `workers` at once, and say what became of each as an event.

    A file's events are `Saved` (replaced), `Skipped` (this transform made these bytes
    already), `Failed` (left as it was) or, with `dry`, `Planned`. A first Ctrl-C starts no
    more files; a second kills the programs that run.

    Raises:
        Stopped: the second Ctrl-C killed a program.
    """
    state = _Run(db, scheduler, argv, stop, emit, tally, dry)
    scheduler.lane(Kind.EXEC, workers)
    state.id = await state.call(db.transform_id if dry else db.transform, command)
    for item in found:
        tally.add(item.target)
        tally.count(item.target, item.category, found=1, queued=1)
    try:
        results = await asyncio.gather(*(
            scheduler.run(Job(kind=Kind.EXEC, work=lambda item=item: state.file(item),
                              target=str(item.target)))
            for item in found), return_exceptions=True)
    finally:
        if state.id is not None and not dry:
            await state.call(db.transform_end, state.id, skipped=state.skipped)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    if state.stopped:
        raise Stopped("stopped while a program ran")


class _Run:
    """The state of one run of a transform: what each file's job needs."""

    def __init__(self, db: store.Store, scheduler: jobs.Scheduler, argv: Sequence[str],
                 stop: Any, emit: Callable[[Any], Any], tally: Any, dry: bool) -> None:
        self.db, self.scheduler, self.argv, self.stop = db, scheduler, argv, stop
        self.emit, self.tally, self.dry = emit, tally, dry
        self.id: int | None = None
        self.skipped = 0
        # Whether the second Ctrl-C killed a program.
        self.stopped = False

    async def call(self, function: Callable[..., Any], *args: Any, **options: Any) -> Any:
        """A read or write of `flnd.db` on a `CALL` thread."""
        return await self.scheduler.on(Kind.CALL, lambda: function(*args, **options))

    async def thread(self, function: Callable[..., Any], *args: Any) -> Any:
        """A blocking step on the transform's lane."""
        return await self.scheduler.on(Kind.EXEC, function, *args)

    def count(self, item: Item, **steps: int) -> None:
        self.tally.count(item.target, item.category, **steps)

    async def file(self, item: Item) -> None:
        """One file: skip it, plan it, or run the command and replace it; never raises an
        error of the file's own."""
        if self.stop.soon.is_set():
            self.count(item, queued=-1)
            return
        self.count(item, queued=-1, downloading=1)
        before = b""
        try:
            before = await self.thread(files.digest, item.path)
            seen = None if self.id is None else await self.call(
                self.db.transform_seen, self.id, before, str(item.path))
            if seen is not None:
                await self.skip(item, seen)
            elif self.dry:
                self.count(item, downloading=-1)
                await self.emit(Planned(item.target, item.post, item.path, "new"))
            else:
                await self.replace(item, before)
        except Error as error:
            self.count(item, downloading=-1, failed=1)
            if self.id is not None and not self.dry:
                await self.call(self.db.transform_failed, self.id, str(item.path), item.key,
                                before)
            await self.emit(Failed(item.target, error, item.post, item.path))
        except Stopped:
            self.count(item, downloading=-1)
            self.stopped = True

    async def skip(self, item: Item, seen: str) -> None:
        """The file holds what this transform made: finish a row a crash left pending."""
        if self.dry:
            self.count(item, downloading=-1)
            await self.emit(Planned(item.target, item.post, item.path, "saved"))
            return
        if seen == "pending":
            await self.finish(item, await self.thread(files.digest, item.path))
        self.skipped += 1
        self.count(item, downloading=-1, skipped=1)
        await self.emit(Skipped(item.target, item.category, item.post, item.path, "done"))

    async def replace(self, item: Item, before: bytes) -> None:
        """Run the command to a temporary file, record it, then swap it in whole."""
        made, after, size = await self.thread(_make, self.argv, item.path, self.stop.now)
        try:
            await self.call(self.db.transform_before, self.id, str(item.path), before, after,
                            item.key)
            await self.thread(_swap, made, item.path)
        except BaseException:
            made.unlink(missing_ok=True)
            raise
        await self.finish(item, after)
        self.count(item, downloading=-1, saved=1, bytes=size)
        await self.emit(Saved(item.target, item.category, item.post, item.path, item.day, size))

    async def finish(self, item: Item, digest: bytes) -> None:
        """The file is the new one: its transform row is done and its own row follows it."""
        size, mtime = await self.call(store.stamp, item.path)
        await self.call(self.db.transform_done, self.id, str(item.path), item.key, size=size,
                        mtime=mtime, digest=digest)


def _make(argv: Sequence[str], path: Path, stop: threading.Event) -> tuple[Path, bytes, int]:
    """Run the command with `{out}` a temporary file beside `path`, and check what it made.

    Returns:
        The temporary file, its SHA-256 and its size; it has `path`'s mode and modified time.

    Raises:
        ToolError: the program didn't start, exited non-zero, or made no file or an empty one.
        Stopped: `stop` was set while it ran; the program is killed.
    """
    made = path.with_name(f".{path.stem}{MARK}{path.suffix}")
    made.unlink(missing_ok=True)
    values = {"in": str(path), "out": str(made)}
    filled = [PLACEHOLDER.sub(lambda found: values[found[1]], argument) for argument in argv]
    try:
        code, said = _spawn(filled, stop)
        if code:
            raise ToolError(f"{argv[0]} exited {code} on {path.name}"
                            f"{': ' + said if said else ''}")
        try:
            size = made.stat().st_size
        except FileNotFoundError:
            raise ToolError(f"{argv[0]} made no {{out}} for {path.name}") from None
        if not size:
            raise ToolError(f"{argv[0]} made an empty {{out}} for {path.name}")
        found = path.stat()
        shutil.copymode(path, made)
        os.utime(made, ns=(found.st_atime_ns, found.st_mtime_ns))
        return made, files.digest(made), size
    except BaseException:
        made.unlink(missing_ok=True)
        raise


def _swap(made: Path, path: Path) -> None:
    """Put the new file in the place of the old, atomically."""
    try:
        files.swap(made, path)
    except OSError as error:
        raise DiskError(f"{path}: can't replace it: {error.strerror or error}") from error


def _spawn(argv: Sequence[str], stop: threading.Event) -> tuple[int, str]:
    """Run a program with no shell, standard output dropped; its exit code and the last line
    it wrote to standard error. Killed, with its children, when `stop` is set."""
    # A group of its own, so a stop kills what the program started too.
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, **group)
    except OSError as error:
        raise ToolError(f"{argv[0]}: {error.strerror or error}") from error
    tail = bytearray()

    def read() -> None:
        while chunk := proc.stderr.read1(TAIL):
            tail[:] = (tail + chunk)[-TAIL:]
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    while (code := _wait(proc)) is None:
        if stop.is_set():
            _kill(proc)
            proc.wait()
            raise Stopped("stopped while a program ran")
    reader.join(1)
    lines = tail.decode("utf-8", "replace").strip().splitlines()
    return code, lines[-1] if lines else ""


def _wait(proc: subprocess.Popen[bytes]) -> int | None:
    """The program's exit code, or None when it still runs after a tenth of a second."""
    try:
        return proc.wait(0.1)
    except subprocess.TimeoutExpired:
        return None


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill the program and everything it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()
