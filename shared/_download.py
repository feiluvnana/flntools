"""The downloader behind `http.download`: one file in bounded pieces over adaptive connections.

A transfer is one mirror of one call. Its first request asks for the first 2 MB, and the answer
gives the file's length. A file of 4 MB or more with a validator is cut into pieces of 2–4 MB,
which up to 8 connections pull from one queue, front first, so the part grows in order. Each
piece is its own `Range` request ending at the piece's end, so its connection stays open for the
next. A host starts at 4 download connections and gains one while that raises its throughput,
up to 8; two refusals while others receive cap it at the most seen working, for 10 minutes. A
connection with no first byte after twice the file's first wait gives its piece away; one
slower than a quarter of the file's best after 2 s reconnects. The part's `.part.json` keeps the
pieces written, for a later attempt or call; a part without a validator resumes only once its
last 64 bytes, fetched again, match. Private: callers use `http.download`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json as jsonlib
import os
import re
import secrets
import shutil
import sys
import threading
from collections import deque
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence, Union
from urllib.parse import unquote, urlsplit

import httpx

import shared
from shared import _atomic as atomic
from shared import _clock as clock
from shared import _fmt as fmt
from shared._http import (CONNECTION_ERRORS, DOWNLOAD_EXTENSION, PIECE_EXTENSION, ChangedError,
                          CorruptError, Download, Error, IncompleteError, RateLimitedError,
                          RefusedError, ServerError, SpaceError, StoppedError, _retry_after,
                          _scrub, _where)
from shared._log import log

__all__ = ["Options", "run"]

PathLike = Union[str, "os.PathLike[str]"]

MB = 1024 * 1024
# A file this big or bigger is split with `parts="auto"`: most videos are 2–15 MB, and below
# this a second connection's setup costs about what it saves.
SPLIT_MIN = 4 * MB
# A piece is a 32nd of the file, kept within IDM's 2–4 MB: big enough that a request's round
# trip is small beside its transfer, small enough that the last piece's tail is short.
PIECE_MIN, PIECE_MAX, PIECES = 2 * MB, 4 * MB, 32
# The most connections one file uses, and one host gets for downloads (Chrome's 6 is for
# sites; a CDN is built for more).
MAX_CONNECTIONS = 8
# The connections a host starts with, before its throughput says whether more help.
START_CONNECTIONS = 4
# A host's throughput is measured over 1 s windows; a window must beat the best one so far by
# 10% to earn one more connection, so noise doesn't grow the count.
GROW_WINDOW, GROW_GAIN = 1.0, 1.1
# Refusals while other connections receive before a host is capped, and how long a cap holds,
# so a long `sync` probes again (MDM's rules).
REFUSALS, FORGET = 2, 600.0
# A connection with no first byte after twice the file's first time-to-first-byte, at least
# 20 ms, gives its piece away (MDM).
STALL_FACTOR, STALL_FLOOR = 2.0, 0.02
# After 2 s of receiving, a connection slower than a quarter of the best speed any connection
# of the file sustained reconnects, when it has over 2 s of work left at its own pace (MDM).
SLOW_AFTER, SLOW_SHARE, SLOW_LEFT = 2.0, 0.25, 2.0
# Connections that spend half their time writing mean the disk is the bottleneck, and then no
# connection is judged slow: a new one would write no faster.
DISK_BOUND = 0.5
# How often, in seconds, a split file's connections are looked at for the stall rules.
WATCH_EVERY = 0.05
# A piece that failed this many times in one attempt fails the attempt.
PIECE_TRIES = 3
# Waits between attempts: the first retry at once, as most drops are one-offs (MDM), then 1 s
# doubling to 30 s.
FIRST_WAIT, MAX_WAIT = 1.0, 30.0
# The bytes a part without a validator fetches again before its end, and must match, to resume.
OVERLAP = 64
# Free space a big file needs beyond its size, for the file system's own use and the rename.
SPACE_MARGIN = 128 * MB
# FAT32 holds no file of 4 GiB or more; these are its names on Linux, macOS and Windows.
FAT_LIMIT = 4 * 1024 * MB
FAT_NAMES = frozenset({"vfat", "msdos", "fat", "fat32"})
# The digests a server may send, by name, strongest first, and hashlib's name for each.
HASHES = {"sha-512": "sha512", "sha-256": "sha256", "sha-1": "sha1", "sha": "sha1", "md5": "md5"}
# What a resumable part's sidecar adds to the part's name.
INFO_SUFFIX = ".json"
# The name of a file saved into a folder when its URL's path names none.
FALLBACK_NAME = "download"
# How often, in seconds, a wait for a host's connection looks at `stop`.
STOP_POLL = 0.25


@dataclass(frozen=True)
class Options:
    """What `http.download` was asked for beyond its URL and `dest`; its docstring says each."""

    client: httpx.Client
    headers: Mapping[str, str] = field(default_factory=dict)
    size: int | None = None
    parts: Literal["auto"] | int = "auto"
    resume: bool = True
    attempts: int = 3
    timeout: float = 30.0
    check: Callable[[Path], Any] | None = None
    progress: Callable[[int, int | None], Any] | None = None
    label: str | None = None
    stop: threading.Event | None = None


def run(mirrors: Sequence[str], dest: PathLike, options: Options) -> Download:
    """Save one file from the first of `mirrors` that works, as `http.download` documents.

    Args:
        mirrors: the file's URLs, tried in order; at least one.
        dest: the file, or a folder to name it in from the first URL.
        options: the rest of `http.download`'s arguments.
    """
    path = _destination(dest, mirrors[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        bar = (stack.enter_context(log.progress(options.label, total=options.size, show=fmt.size))
               if options.label is not None else None)
        taken = stack.enter_context(_claimed(path))
        streams = stack.enter_context(_Streams(options.stop))
        report = _Report(bar, options.progress)
        for number, mirror in enumerate(mirrors, start=1):
            transfer = _Transfer(mirror, path, options, resume=options.resume and not taken,
                                 report=report, streams=streams)
            try:
                return transfer.run()
            # A 429: the caller's back-off decides, and another mirror is likely the same site.
            # A stop, or a full disk: no mirror would do better.
            except (RateLimitedError, StoppedError, SpaceError):
                raise
            except Exception as error:  # any other failure of a mirror but the last: try the next
                if number == len(mirrors):
                    raise
                log.debug("mirror {n} of {name} failed: {error}; trying the next", n=number,
                          name=path.name, error=error)
    raise AssertionError("unreachable: the last mirror returns or raises")


class _Gate:
    """One host's download connections: how many may be open, learned from how the host answers."""

    def __init__(self) -> None:
        """A host not seen yet: 4 connections, no cap."""
        self.limit = START_CONNECTIONS
        self.active = 0
        self.receiving = 0
        # The most connections seen receiving at once, which a cap falls back to.
        self.peak = 0
        self.cap: int | None = None
        self.cap_until = 0.0
        self.refusals = 0
        # The best window's bytes per second, and whether a connection was wanted in this one.
        self.best = 0.0
        self.wanted = False
        self.window_start: float | None = None
        self.window_bytes = 0
        # Every download thread of the process asks the same host's gate.
        self.changed = threading.Condition()

    def allowed(self) -> int:
        """How many connections may be open now; a cap 10 minutes old is forgotten here."""
        with self.changed:
            if self.cap is not None and clock.now() >= self.cap_until:
                self.cap, self.refusals, self.best = None, 0, 0.0
            return self.limit if self.cap is None else min(self.limit, self.cap)

    def take(self) -> bool:
        """Open one connection if the host has room now; False, and wanting one, if not."""
        with self.changed:
            if self.active < self.allowed():
                self.active += 1
                return True
            self.wanted = True
            return False

    def wait(self, streams: _Streams, url: str) -> None:
        """Open one connection, waiting for room; `stop` ends the wait with `StoppedError`."""
        with self.changed:
            while not self.take():
                self.changed.wait(STOP_POLL)
                streams.check(url)

    def give(self) -> None:
        """Close one connection, making room for a waiting one."""
        with self.changed:
            self.active -= 1
            self.changed.notify()

    def started(self) -> None:
        """Count a connection that began receiving a body."""
        with self.changed:
            self.receiving += 1
            self.peak = max(self.peak, self.receiving)

    def ended(self) -> None:
        """Count a connection whose body ended, however it ended."""
        with self.changed:
            self.receiving -= 1

    def received(self, count: int) -> None:
        """Count bytes received; at each window's end, add a connection while throughput rises."""
        with self.changed:
            now = clock.now()
            if self.window_start is None:
                self.window_start = now
            self.window_bytes += count
            elapsed = now - self.window_start
            if elapsed < GROW_WINDOW:
                return
            rate = self.window_bytes / elapsed
            self.window_start, self.window_bytes = now, 0
            ceiling = MAX_CONNECTIONS if self.cap is None else self.cap
            if self.wanted and self.limit < ceiling and rate > self.best * GROW_GAIN:
                self.limit += 1
                self.changed.notify()
            self.best = max(self.best, rate)
            self.wanted = False

    def refused(self) -> bool:
        """Count a refusal; True when others were receiving, so it is the host's limit speaking."""
        with self.changed:
            if self.receiving == 0:
                return False
            self.refusals += 1
            if self.refusals >= REFUSALS:
                self.cap = max(1, self.peak)
                self.cap_until = clock.now() + FORGET
                self.limit = min(self.limit, self.cap)
            return True


# One gate per (scheme, host, port), made on first use under `_gates_lock`.
_gates: dict[tuple[str, str, int | None], _Gate] = {}
_gates_lock = threading.Lock()
# The destinations a download in this process is writing, so two calls for one file never
# write into the same resumable part; under `_claims_lock`, since any thread may download.
_claims: set[str] = set()
_claims_lock = threading.Lock()


def _gate(url: str) -> _Gate:
    """The download gate of `url`'s host, made on first use."""
    parsed = httpx.URL(url)
    key = (parsed.scheme, parsed.host, parsed.port)
    with _gates_lock:
        if key not in _gates:
            _gates[key] = _Gate()
        return _gates[key]


@dataclass(eq=False)
class _Segment:
    """One byte range of a file: `start` to `end` (exclusive; None to the end), `done` written."""

    start: int
    end: int | None
    done: int = 0
    # The connection writing it; None while it waits in the queue. Never saved.
    owner: object | None = field(default=None, repr=False)
    # Its failed requests in this attempt.
    failures: int = 0

    @property
    def at(self) -> int:
        """The offset of the next byte to write."""
        return self.start + self.done

    @property
    def finished(self) -> bool:
        """Whether every byte of the range is written; a range of unknown end never says so."""
        return self.end is not None and self.at >= self.end


@dataclass
class _State:
    """A part being written: where it is, where it came from, and its segments."""

    # A hidden `.<name>.<random>.part` beside `dest`.
    part: Path
    url: str
    # The strong `ETag` or the `Last-Modified` the server sent, sent back as `If-Range`.
    validator: str | None
    # The file's length from the server; None when it didn't say.
    size: int | None
    segments: list[_Segment]
    # Whether the server takes ranges, which a part without a validator needs to resume.
    ranges: bool = False

    @property
    def info(self) -> Path:
        """The sidecar `.part.json` beside the part."""
        return self.part.with_name(self.part.name + INFO_SUFFIX)

    @property
    def done(self) -> int:
        """The bytes written across every segment."""
        return sum(segment.done for segment in self.segments)

    def save(self) -> None:
        """Write the sidecar, so a crash or a later call resumes from here."""
        # The URL without its query: a signed link's token stays off the disk. A resume goes by
        # `dest` and the validator, so the URL is only there for a person reading the file.
        data = {"url": _where(self.url), "validator": self.validator, "size": self.size,
                "ranges": self.ranges,
                "segments": [[part.start, part.end, part.done] for part in self.segments]}
        atomic.write(self.info, jsonlib.dumps(data).encode("utf-8"))

    def drop(self) -> None:
        """Delete the part and its sidecar."""
        self.part.unlink(missing_ok=True)
        self.info.unlink(missing_ok=True)


class _Changed(Exception):
    """The server's file isn't the one the part holds: a 416, a range elsewhere, a bad overlap."""


class _Dropped(Exception):
    """A connection whose piece the stall rules gave to another; not a failure."""


class _Streams:
    """A download's stop: its open responses, closed once `stop` is set, so a stalled read ends."""

    def __init__(self, stop: threading.Event | None):
        self.stop = stop
        self.responses: set[httpx.Response] = set()
        # Streams open and close on the connections' threads while the watcher reads the set.
        self.lock = threading.Lock()
        self.done = threading.Event()

    def __enter__(self) -> _Streams:
        if self.stop is not None:
            threading.Thread(target=self._watch, name="shared-http-stop", daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.done.set()

    def check(self, url: str) -> None:
        """Raise `StoppedError` once `stop` is set."""
        if self.stop is not None and self.stop.is_set():
            raise StoppedError(f"GET {_where(url)}: stopped")

    def sleep(self, seconds: float, url: str) -> None:
        """Wait between attempts on the shared clock; `stop` ends the wait with `StoppedError`."""
        if clock.sleep(seconds, stop=self.stop):
            self.check(url)

    def opened(self, response: httpx.Response) -> None:
        with self.lock:
            self.responses.add(response)

    def closed(self, response: httpx.Response) -> None:
        with self.lock:
            self.responses.discard(response)

    def _watch(self) -> None:
        """Close every open response once `stop` is set; the download's threads then see why."""
        assert self.stop is not None
        # Real waits, not `clock`: the stop and the reads it ends are real.
        while not self.done.is_set():
            if self.stop.wait(STOP_POLL):
                with self.lock:
                    responses = list(self.responses)
                for response in responses:
                    # Closing from another thread may race the reader's own close.
                    with suppress(Exception):
                        response.close()
                return


@dataclass(eq=False)
class _Stream:
    """One open response of a download, for one segment."""

    response: httpx.Response
    streams: _Streams
    segment: _Segment | None = None
    # Bytes the body starts with that are on disk already: checked, not written again.
    overlap: bytes = b""

    def close(self) -> None:
        """Close the response; safe to call twice and from another thread."""
        try:
            self.response.close()
        finally:
            self.streams.closed(self.response)

    def strip(self, chunk: bytes) -> bytes:
        """`chunk` past the overlap, once the overlap's bytes match the disk's."""
        if not self.overlap:
            return chunk
        size = min(len(self.overlap), len(chunk))
        if chunk[:size] != self.overlap[:size]:
            raise _Changed("the part's last bytes differ from the server's")
        self.overlap = self.overlap[size:]
        return chunk[size:]


class _Report:
    """Progress for one download, to the caller's callback and to the row `label` draws."""

    def __init__(self, bar: Any, progress: Callable[[int, int | None], Any] | None):
        self.bar, self.progress = bar, progress
        # Connections report from their own threads; one at a time keeps the callback from
        # running twice at once and `done` from going back.
        self.lock = threading.Lock()

    def __call__(self, done: int, total: int | None) -> None:
        """Report `done` of `total` bytes to the row and the callback, one caller at a time."""
        with self.lock:
            if self.bar is not None:
                self.bar.total = total
                self.bar.count = done
            if self.progress is not None:
                self.progress(done, total)


class _Transfer:
    """One mirror of one download: its attempts, its part, and its pieces."""

    def __init__(self, url: str, dest: Path, options: Options, *, resume: bool, report: _Report,
                 streams: _Streams):
        """One mirror's transfer; nothing is requested until `run`."""
        self.url, self.dest, self.options = url, dest, options
        self.resume, self.report, self.streams = resume, report, streams
        # `identity`: a compressed body's bytes and ranges aren't the file's, so it couldn't resume.
        self.headers = {"Accept-Encoding": "identity", **options.headers}
        self.gate = _gate(url)
        self.state: _State | None = None
        # The answer the transfer started from: its URL and headers for the `Download`, and its
        # status, since a 206's `Content-MD5` is of the piece, not the file.
        self.final_url, self.response_headers, self.status = url, httpx.Headers(), 0
        # The first request's time to its first byte, which the stall rules measure against.
        self.ttfb = 0.0
        # Where the whole file goes: `dest`, or the path `check` named.
        self.named = dest
        # Set once the server's file differed from the part's, so the part restarted.
        self.restarted = False
        # Set after a restart: later attempts fetch the file in one plain request.
        self.single = False

    def run(self) -> Download:
        """Every attempt until the file is whole and checked, then its rename."""
        if self.resume:
            self.state = _resumable(self.dest)
        attempt = 1
        while (failure := self._attempt()) is not None:
            if attempt >= self.options.attempts:
                if self.restarted:
                    raise ChangedError(f"GET {_where(self.url)}: the file changed during a resume, "
                                       f"and its restart failed: {failure}") from failure
                raise failure
            # At once the first time, as most drops are one-offs; then 1 s doubling to 30 s.
            seconds = 0.0 if attempt == 1 else min(FIRST_WAIT * 2 ** (attempt - 2), MAX_WAIT)
            log.debug("retry {n} of {url} in {seconds:g} s: {reason}", n=attempt,
                      url=_where(self.url), seconds=seconds, reason=failure)
            if seconds:
                self.streams.sleep(seconds, self.url)
            attempt += 1
        state = self.state
        assert state is not None
        # Through `atomic`, whose part moves onto the file's name in one step: a reader sees the
        # old file or the whole new one. The resumable part moves into it first, one rename too.
        with atomic.writing(self.named, replace=True) as final:
            os.replace(state.part, final)
        state.info.unlink(missing_ok=True)
        return Download(self.named, self.final_url, self.response_headers)

    def _attempt(self) -> Exception | None:
        """One attempt: None once the file is whole and checked, the failure to retry, or raise."""
        try:
            self.streams.check(self.url)
            self._transfer()
            self._verify()
            return None
        except _Changed as error:
            # The part is useless now; the next attempt starts the file again, in one request.
            self._restart()
            return Error(f"GET {_where(self.url)}: {error}")
        except CONNECTION_ERRORS as error:
            self._keep()
            failure = Error(self._scrub(f"GET {_where(self.url)}: {type(error).__name__}: {error}"))
            failure.__cause__ = error
            return failure
        except (ServerError, IncompleteError) as error:
            self._keep()
            return error
        except shared.BrokenError as error:
            # `check` or the server's hash found the file damaged; `_verify` deleted it.
            return error
        except httpx.HTTPError as error:  # an invalid URL, too many redirects: it would repeat
            self._keep()
            raise Error(self._scrub(f"GET {_where(self.url)}: {type(error).__name__}: "
                                    f"{error}")) from error
        except BaseException:
            # A 4xx, a refused check, the caller's progress, a stop or Ctrl-C: what was written
            # stays resumable, unless `_verify` already deleted it.
            self._keep()
            raise

    def _transfer(self) -> None:
        """Fetch what the part still lacks: the file from its first piece, or what is left."""
        self.gate.wait(self.streams, self.url)
        try:
            stream = self._first() if self.state is None else self._resumed()
        except BaseException:
            self.gate.give()
            raise
        if stream is None:
            self.gate.give()
            return
        state = self.state
        assert state is not None
        try:
            self.report(state.done, self.total())
        except BaseException:
            # The caller's progress raised (a Ctrl-C, say) before a connection owns the stream.
            stream.close()
            self.gate.give()
            raise
        _Pieces(self, stream).run()

    def _first(self) -> _Stream:
        """Open the file's first request, for its first piece if it may split, and start a part."""
        if self.options.parts == 1 or self.single:
            stream = self._timed(0, None)
        else:
            try:
                stream = self._timed(0, PIECE_MIN - 1)
            except _Changed:
                # A 416 to the first bytes: an empty file, which only a plain request can get.
                stream = self._timed(0, None)
        self._start(stream)
        return stream

    def _resumed(self) -> _Stream | None:
        """Open the first unfinished segment of a kept part; a whole-file answer restarts on it."""
        state = self.state
        assert state is not None
        first = next((segment for segment in state.segments if not segment.finished), None)
        if first is None:
            return None
        start = clock.now()
        stream = self._segment(first)
        self.ttfb = clock.now() - start
        self._started(stream)
        if stream.response.status_code == 200:
            # A 200 to a range: the file changed (`If-Range` didn't match) or the server ignores
            # ranges. Either way the body is the whole file, so the part restarts on it.
            log.debug("{name}: not the file the part holds; starting again", name=self.dest.name)
            self._drop()
            self.restarted = True
            self._start(stream)
        return stream

    def _timed(self, first: int, last: int | None) -> _Stream:
        """`_open`, keeping its time to the first byte and its answer for the `Download`."""
        start = clock.now()
        stream = self._open(first, last, piece=False)
        self.ttfb = clock.now() - start
        self._started(stream)
        return stream

    def _started(self, stream: _Stream) -> None:
        """Keep the URL, headers and status of the answer the transfer starts from."""
        response = stream.response
        self.final_url, self.response_headers = str(response.url), response.headers
        self.status = response.status_code

    def _start(self, stream: _Stream) -> None:
        """A new part for a fresh answer: its pieces when it splits, else one or two segments."""
        response = stream.response
        try:
            encoded = response.headers.get("content-encoding", "identity").lower() != "identity"
            end, total = _extent(response, encoded)
            # An encoded body's length and ranges count encoded bytes: neither checked nor resumed.
            validator = None if encoded else _validator(response.headers)
            ranges = not encoded and (response.status_code == 206 or
                                      response.headers.get("accept-ranges", "").lower() == "bytes")
            part = self.dest.with_name(f".{self.dest.name}.{secrets.token_hex(4)}.part")
            _room(part.parent, total if total is not None else self.options.size)
            _open_part(part, create=True).close()
            segments = [_Segment(0, end), *self._rest(end, total, validator)]
            self.state = _State(part, self.url, validator, total, segments, ranges)
            stream.segment = segments[0]
            if self._resumable():
                self.state.save()
        except BaseException:
            stream.close()
            raise

    def _rest(self, end: int | None, total: int | None, validator: str | None) -> list[_Segment]:
        """The segments after a first piece that ended at `end`: pieces, one segment, or none."""
        if end is None or end == total:
            return []
        if total is not None and self._splits(total, validator):
            return _plan(end, total)
        return [_Segment(end, total)]

    def _splits(self, total: int, validator: str | None) -> bool:
        """Whether a file of `total` bytes is fetched on several connections."""
        # Only with a validator: each piece is asked for with `If-Range`, so a file that changes
        # between two pieces is never put together from two versions.
        if validator is None or self.single or self.options.parts == 1:
            return False
        return self.options.parts != "auto" or total >= SPLIT_MIN

    def width(self) -> int:
        """How many connections this part may use at once."""
        state = self.state
        assert state is not None
        if state.size is None or not state.ranges or not self._splits(state.size, state.validator):
            return 1
        parts = self.options.parts
        return min(MAX_CONNECTIONS, parts if isinstance(parts, int) else MAX_CONNECTIONS)

    def _segment(self, segment: _Segment) -> _Stream:
        """Open the rest of `segment`; without a validator, from 64 bytes back, to match them."""
        state = self.state
        assert state is not None
        overlap = min(OVERLAP, segment.at) if state.validator is None else 0
        expected = b""
        if overlap:
            with open(state.part, "rb") as file:
                file.seek(segment.at - overlap)
                expected = file.read(overlap)
        last = None if segment.end is None or segment.end == state.size else segment.end - 1
        stream = self._open(segment.at - overlap, last, piece=self.width() > 1)
        stream.segment = segment
        if stream.response.status_code == 206:
            stream.overlap = expected
        return stream

    def _open(self, first: int, last: int | None, *, piece: bool) -> _Stream:
        """Send one GET for bytes `first` to `last` (None: to the end) and check its answer.

        A 200 to a range is returned for the caller to decide; a 206 that starts elsewhere, or a
        416, raises `_Changed`. `piece` marks a split file's piece, which goes over HTTP/1.1.
        """
        headers = dict(self.headers)
        if first or last is not None:
            headers["Range"] = f"bytes={first}-{'' if last is None else last}"
            if self.state is not None and self.state.validator is not None:
                headers["If-Range"] = self.state.validator
        extensions: dict[str, Any] = {DOWNLOAD_EXTENSION: True}
        if piece:
            extensions[PIECE_EXTENSION] = True
        client = self.options.client
        request = client.build_request("GET", self.url, headers=headers,
                                       timeout=self.options.timeout, extensions=extensions)
        response = client.send(request, stream=True)
        self.streams.opened(response)
        stream = _Stream(response, self.streams)
        try:
            self.streams.check(self.url)
            _checked(response, request, first, ranged="Range" in headers)
        except BaseException:
            stream.close()
            raise
        return stream

    def chunks(self, stream: _Stream) -> Iterator[bytes]:
        """The body's bytes as they arrive, until `stop`, which ends a stalled read too."""
        try:
            # As the bytes arrive, without a chunk size: httpx would hold back a partial chunk,
            # and lose it when the connection drops, which is when it matters.
            for chunk in stream.response.iter_bytes():
                self.streams.check(self.url)
                yield stream.strip(chunk)
        except Exception:
            # A stop closes the response under the read, which then fails in httpx's own way.
            self.streams.check(self.url)
            raise

    def _verify(self) -> None:
        """The size check, the server's hash, then the caller's `check`, on the complete part."""
        state = self.state
        assert state is not None
        got = state.part.stat().st_size
        expected = state.size if state.size is not None else self.options.size
        try:
            if expected is not None and got != expected:
                # Every segment is written, so these bytes are all the body had: only a fresh
                # download can mend it.
                raise IncompleteError(f"GET {_where(self.url)}: got {got} of {expected} bytes")
            _hash_check(state.part, self.response_headers, whole=self.status == 200)
            self.named = self.dest
            if self.options.check is not None:
                named = self.options.check(state.part)
                if isinstance(named, Path):
                    self.named = named
        except BaseException:
            # The bytes are all there, so a resume would give the same file: only a new one passes.
            self._drop()
            raise

    def _resumable(self) -> bool:
        """Whether the part may be kept: `resume`, and a validator or ranges to check it by."""
        state = self.state
        return self.resume and state is not None and (state.validator is not None or state.ranges)

    def _keep(self) -> None:
        """After a failure: save a resumable part's progress, or delete a part that can't resume."""
        state = self.state
        if state is None:
            return
        if not self._resumable():
            self._drop()
            return
        segments = state.segments
        if len(segments) == 1 and state.part.exists():
            # One segment is written in order, so the bytes on disk are exactly what arrived.
            segments[0].done = state.part.stat().st_size - segments[0].start
        state.save()

    def _drop(self) -> None:
        """Delete the part and its sidecar, so the next attempt starts afresh."""
        if self.state is not None:
            self.state.drop()
            self.state = None

    def _restart(self) -> None:
        """Start again after `_Changed`: no part, and one plain request."""
        self._drop()
        self.restarted = True
        self.single = True

    def total(self) -> int | None:
        """The progress total: the server's length, else the caller's `size`."""
        state = self.state
        return state.size if state is not None and state.size is not None else self.options.size

    def _scrub(self, text: str) -> str:
        """`text` with the caller's secret header values replaced by `***`."""
        request = self.options.client.build_request("GET", self.url, headers=self.headers)
        return _scrub(text, request)


class _Pieces:
    """One attempt's fetch of a part's unfinished segments, on one connection or several."""

    def __init__(self, transfer: _Transfer, stream: _Stream):
        """The pieces of `transfer`'s part, the first of them already open on `stream`."""
        state = transfer.state
        assert state is not None and stream.segment is not None
        self.transfer, self.state, self.gate = transfer, state, transfer.gate
        self.width = transfer.width()
        if self.width > 1:
            _recut(state)
        first = _Worker(self, stream)
        stream.segment.owner = first
        self.queue = deque(segment for segment in state.segments
                           if not segment.finished and segment is not stream.segment)
        self.workers = [first]
        # Guards the queue, the segments' owners and counts, and `done`, across connections.
        self.lock = threading.Lock()
        self.saving = threading.Lock()
        # Set by a connection that ends, so the watcher looks at once.
        self.wake = threading.Event()
        self.abort = threading.Event()
        self.failures: list[BaseException] = []
        self.done = state.done
        # The best speed any connection of the file sustained, in bytes per second.
        self.best = 0.0

    def run(self) -> None:
        """Fetch every unfinished segment, then raise the first failure that stopped it."""
        if self.width == 1:
            self.workers[0].run()
        else:
            self._watch_all()
        if self.failures:
            raise self.failures[0]

    def _watch_all(self) -> None:
        """Run the connections on threads, adding them while pieces wait, until all are done."""
        self._spawn(self.workers[0])
        try:
            while not self.abort.is_set() and not self._complete():
                self._grow()
                self.wake.wait(WATCH_EVERY)
                self.wake.clear()
                self._watch()
        except BaseException:
            # Ctrl-C, or a stop while waiting for the host: the connections stop too.
            self.abort.set()
            raise
        finally:
            self.abort.set()
            for worker in self.workers:
                # A connection still waiting for an answer the stall rules gave up on isn't
                # waited for: it writes nothing more, and gives its host's room back on its own.
                if worker.thread is not None and worker.dropped is None:
                    worker.thread.join()

    def _complete(self) -> bool:
        with self.lock:
            return all(segment.finished for segment in self.state.segments)

    def _grow(self) -> None:
        """Start a connection for each piece waiting, within the file's width and host's room."""
        alive = sum(worker.alive for worker in self.workers)
        with self.lock:
            waiting = len(self.queue)
        if not waiting:
            return
        if not alive:
            # The last connection left with pieces waiting (it failed, and the others had
            # carried on, or the stall rules took its piece): a fresh one, at once.
            self.gate.wait(self.transfer.streams, self.transfer.url)
            self._spawn(_Worker(self))
            return
        while alive < self.width and waiting and self.gate.take():
            self._spawn(_Worker(self))
            alive, waiting = alive + 1, waiting - 1

    def _spawn(self, worker: _Worker) -> None:
        if worker not in self.workers:
            self.workers.append(worker)
        worker.thread = threading.Thread(target=worker.run, name="shared-http-piece", daemon=True)
        worker.thread.start()

    def _watch(self) -> None:
        """Apply the stall rules to every connection writing a piece."""
        now = clock.now()
        closing: list[_Stream] = []
        with self.lock:
            busy = [worker for worker in self.workers
                    if worker.alive and worker.segment is not None
                    and worker.segment.owner is worker]
            for worker in busy:
                if worker.since is not None and now - worker.since >= SLOW_AFTER:
                    self.best = max(self.best, worker.speed(now))
            disk = _disk_bound(busy, now)
            others = len(busy) > 1 or self.gate.allowed() > self.gate.active
            for worker in busy:
                reason = _stall(worker, now, ttfb=self.transfer.ttfb, best=self.best, disk=disk,
                                others=others)
                if reason is not None:
                    closing += self._take_back(worker, reason)
        for stream in closing:
            # Closing from this thread races the connection's own read and close.
            with suppress(Exception):
                stream.close()

    def _take_back(self, worker: _Worker, reason: str) -> list[_Stream]:
        """Put `worker`'s piece back at the queue's front; the stream to close, for a slow one."""
        segment = worker.segment
        assert segment is not None
        log.debug("{name}: a connection was {reason}; its piece goes to another",
                  name=self.transfer.dest.name, reason=reason)
        worker.dropped = reason
        segment.owner = None
        self.queue.appendleft(segment)
        return [worker.stream] if worker.stream is not None else []

    def take(self, worker: _Worker) -> _Segment | None:
        """The next waiting piece, now `worker`'s; None when none is left or the file stopped."""
        with self.lock:
            if self.abort.is_set() or not self.queue:
                return None
            segment = self.queue.popleft()
            segment.owner = worker
            return segment

    def wrote(self, worker: _Worker, segment: _Segment, count: int) -> bool:
        """Count `count` bytes `worker` wrote at `segment`'s end; False when it isn't its piece."""
        with self.lock:
            if segment.owner is not worker:
                return False
            segment.done += count
            self.done += count
            # Under the lock, so two connections' reports never make `done` go back.
            self.transfer.report(self.done, self.transfer.total())
        self.gate.received(count)
        return True

    def finished(self, worker: _Worker, segment: _Segment) -> None:
        """Release `worker`'s finished piece and save the sidecar, so a crash keeps it."""
        with self.lock:
            if segment.owner is worker:
                segment.owner = None
        if self.width > 1 and self.transfer._resumable():
            with self.saving:
                self.state.save()

    def failed(self, worker: _Worker, error: BaseException) -> None:
        """Put a failed connection's piece back; fail the file unless the others can carry on."""
        with self.lock:
            segment = worker.segment
            if segment is not None and segment.owner is worker:
                segment.owner = None
                segment.failures += 1
                self.queue.appendleft(segment)
            if isinstance(error, _Dropped) or worker.dropped is not None:
                return
            if self._carries_on(worker, error, segment):
                log.debug("{name}: a connection failed, the others carry on: {error}",
                          name=self.transfer.dest.name, error=error)
                return
            self.failures.append(error)
            self.abort.set()

    def _carries_on(self, worker: _Worker, error: BaseException, segment: _Segment | None) -> bool:
        """Whether a connection's failure leaves the file to its other connections."""
        if self.width == 1 or (segment is not None and segment.failures >= PIECE_TRIES):
            return False
        others = any(other.alive and other is not worker for other in self.workers)
        if isinstance(error, (RateLimitedError, ServerError, httpx.ConnectError)):
            # A refusal while others receive is the host's connection limit: it may cap the host.
            self.gate.refused()
            return others
        return others and isinstance(error, (*CONNECTION_ERRORS, IncompleteError))


class _Worker:
    """One connection of a file: it takes pieces from the queue until none is left."""

    def __init__(self, pieces: _Pieces, stream: _Stream | None = None):
        self.pieces, self.transfer = pieces, pieces.transfer
        self.stream = stream
        self.segment = stream.segment if stream is not None else None
        self.thread: threading.Thread | None = None
        self.alive = True
        # When its current request went out, and when that answer came (None while waiting).
        self.sent = clock.now()
        self.first: float | None = clock.now() if stream is not None else None
        # When it began receiving, the bytes since, and the seconds spent writing them.
        self.since: float | None = None
        self.received = 0
        self.writing = 0.0
        # Why the stall rules took its piece: "stalled" or "slow"; None while it has it.
        self.dropped: str | None = None

    def speed(self, now: float) -> float:
        """Bytes per second since it began receiving."""
        elapsed = now - self.since if self.since is not None else 0.0
        return self.received / elapsed if elapsed > 0 else 0.0

    def run(self) -> None:
        """Fetch pieces until none is left; a failure goes to the file, which decides."""
        try:
            while self._next():
                self._piece()
        except BaseException as error:  # handed to the file, which raises the first fatal one
            self.pieces.failed(self, error)
        finally:
            if self.stream is not None:
                self.stream.close()
            self.alive = False
            self.pieces.gate.give()
            self.pieces.wake.set()

    def _next(self) -> bool:
        """Have an open stream for a piece of its own: the one it was given, or the queue's next."""
        while self.stream is None:
            segment = self.pieces.take(self)
            if segment is None:
                return False
            # The times first: the watcher judges a connection only once `segment` is its own.
            self.sent, self.first, self.dropped = clock.now(), None, None
            self.segment = segment
            stream = self.transfer._segment(segment)
            if stream.response.status_code != 206:
                stream.close()
                raise _Changed("a range came back as the whole file")
            self.first = clock.now()
            if segment.owner is not self:
                # The stall rules gave the piece away while this answer was on its way.
                stream.close()
                continue
            self.stream = stream
        return True

    def _piece(self) -> None:
        """Write the open stream's piece up to its end, then release it."""
        stream, segment = self.stream, self.segment
        assert stream is not None and segment is not None
        self.pieces.gate.started()
        if self.since is None:
            self.since = clock.now()
        # Opened for each piece, so a connection waiting for an answer holds no handle on the
        # part, which Windows would refuse to rename.
        try:
            with _open_part(self.pieces.state.part) as file:
                self._fill(file, stream, segment)
        finally:
            stream.close()
            self.stream = None
            self.pieces.gate.ended()
        if segment.owner is not self:
            raise _Dropped()
        if segment.end is None:
            # No length from the server: the body ending is the file ending.
            segment.end = segment.at
        elif not segment.finished:
            raise IncompleteError(f"GET {_where(self.transfer.url)}: got {segment.at} of "
                                  f"{segment.end} bytes")
        self.pieces.finished(self, segment)

    def _fill(self, file: io.FileIO, stream: _Stream, segment: _Segment) -> None:
        """Write the stream's bytes into the piece until it is whole or no longer this one's."""
        for chunk in self.transfer.chunks(stream):
            if self.pieces.abort.is_set():
                raise _Dropped()
            if not self._write(file, segment, chunk):
                return

    def _write(self, file: io.FileIO, segment: _Segment, chunk: bytes) -> bool:
        """Write `chunk` at the piece's end; False once the piece is whole or not this one's."""
        if segment.owner is not self:
            return False
        at = segment.at
        if segment.end is not None:
            chunk = chunk[: segment.end - at]
        start = clock.now()
        file.seek(at)
        _write_all(file, chunk)
        self.writing += clock.now() - start
        self.received += len(chunk)
        # Counted only now: the part's handle has no buffer, so these bytes are the system's
        # already, and a sidecar saved from here on never claims a byte a crash could lose.
        if not self.pieces.wrote(self, segment, len(chunk)):
            return False
        return not segment.finished


def _checked(response: httpx.Response, request: httpx.Request, first: int, *,
             ranged: bool) -> None:
    """Raise for a status that isn't the file, or a range that isn't the one asked for."""
    status = response.status_code
    where = f"GET {_where(request.url)}"
    if status == 429:
        retry_after = _retry_after(response.headers.get("retry-after"))
        wait = "" if retry_after is None else f", retry after {retry_after:g} s"
        raise RateLimitedError(retry_after, f"{where}: HTTP 429, rate limited{wait}")
    if status == 416 and ranged:
        raise _Changed("the server has fewer bytes than asked for (HTTP 416)")
    if status >= 500:
        raise ServerError(status, f"{where}: HTTP {status}")
    if status >= 400:
        raise RefusedError(status, f"{where}: HTTP {status}")
    if status not in (200, 206):
        raise Error(f"{where}: HTTP {status}, not a file")
    if status == 206:
        start = _range(response.headers.get("content-range", ""))[0]
        if start != first:
            raise _Changed(f"asked for byte {first}, got {start}")


def _extent(response: httpx.Response, encoded: bool) -> tuple[int | None, int | None]:
    """Where the first answer's bytes end, and the file's length; None for what it doesn't say."""
    if response.status_code == 206:
        _, end, total = _range(response.headers.get("content-range", ""))
        # A range past the file's end is answered up to its end; a server that says otherwise
        # still sent only the file.
        return (min(end, total) if end is not None and total is not None else end), total
    length = response.headers.get("content-length", "")
    total = int(length) if length.isdigit() and not encoded else None
    return total, total


def _range(content_range: str) -> tuple[int | None, int | None, int | None]:
    """`Content-Range: bytes <a>-<b>/<total>` as (a, b + 1, total); None for what doesn't parse."""
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range.strip(), re.IGNORECASE)
    if match is None:
        return None, None, None
    total = int(match[3]) if match[3].isdigit() else None
    return int(match[1]), int(match[2]) + 1, total


def _plan(start: int, total: int) -> list[_Segment]:
    """The pieces from `start` to `total`: a 32nd of the file each, 2–4 MB."""
    size = min(PIECE_MAX, max(PIECE_MIN, total // PIECES))
    return [_Segment(at, min(at + size, total)) for at in range(start, total, size)]


def _recut(state: _State) -> None:
    """Cut every unfinished segment longer than two pieces into pieces, keeping what is written."""
    assert state.size is not None
    segments: list[_Segment] = []
    for segment in state.segments:
        if segment.finished or segment.end is None or segment.end - segment.at <= 2 * PIECE_MAX:
            segments.append(segment)
            continue
        if segment.done:
            segments.append(_Segment(segment.start, segment.at, segment.done))
        segments += _plan(segment.at, segment.end)
    state.segments = segments


def _stall(worker: _Worker, now: float, *, ttfb: float, best: float, disk: bool,
           others: bool) -> str | None:
    """Why the stall rules take `worker`'s piece away ("stalled", "slow"), or None to leave it.

    Args:
        worker: a connection that owns a piece.
        now: the clock's time.
        ttfb: the file's first request's time to its first byte.
        best: the best speed any connection of the file sustained, in bytes per second.
        disk: whether the disk is the bottleneck, which spares a slow connection.
        others: whether another connection could take the piece now.
    """
    if worker.first is None:
        limit = max(STALL_FACTOR * ttfb, STALL_FLOOR)
        return "stalled" if others and now - worker.sent > limit else None
    segment = worker.segment
    if disk or worker.since is None or now - worker.since < SLOW_AFTER:
        return None
    if segment is None or segment.end is None:
        return None
    speed = worker.speed(now)
    left = segment.end - segment.at
    return "slow" if speed < best * SLOW_SHARE and left > SLOW_LEFT * speed else None


def _disk_bound(workers: list[_Worker], now: float) -> bool:
    """Whether the receiving connections spend half their time or more writing."""
    elapsed = sum(now - worker.since for worker in workers if worker.since is not None)
    return elapsed > 0 and sum(worker.writing for worker in workers) / elapsed >= DISK_BOUND


def _hash_check(part: Path, headers: httpx.Headers, *, whole: bool) -> None:
    """Compare the finished file with the strongest digest the server sent, if it sent one."""
    if headers.get("content-encoding", "identity").lower() != "identity":
        return
    digests = _digests(headers, whole=whole)
    name = next((HASHES[key] for key in HASHES if HASHES[key] in digests), None)
    if name is None:
        return
    digest = hashlib.new(name)
    with open(part, "rb") as file:
        while block := file.read(MB):
            digest.update(block)
    if digest.digest() != digests[name]:
        raise CorruptError(f"{part.name}: its {name} differs from the server's")


def _digests(headers: httpx.Headers, *, whole: bool) -> dict[str, bytes]:
    """The whole file's digests a response names, by hashlib name: `Digest`, `Repr-Digest`, …"""
    found: dict[str, bytes] = {}
    items = [item for name in ("repr-digest", "digest", "x-goog-hash")
             for value in headers.get_list(name) for item in value.split(",")]
    # A 206's `Content-MD5` is of the piece it carries; only a whole body's is the file's.
    if whole:
        items += [f"md5={value}" for value in headers.get_list("content-md5")]
    for item in items:
        algorithm, _, encoded = item.strip().partition("=")
        name = HASHES.get(algorithm.strip().lower())
        try:
            # `Repr-Digest` wraps the value in colons, a structured field's byte sequence.
            value = base64.b64decode(encoded.strip().strip(":"), validate=True)
        except (binascii.Error, ValueError):  # not base64: a digest this check can't read
            continue
        if name is not None and len(value) == hashlib.new(name).digest_size:
            found[name] = value
    return found


def _room(folder: Path, size: int | None) -> None:
    """Refuse a big file the disk can't take: it needs its size plus 128 MB, and FAT32 < 4 GiB."""
    if size is None or size < SPLIT_MIN:
        return
    if size >= FAT_LIMIT and _filesystem(folder) in FAT_NAMES:
        raise SpaceError(f"{folder} is on FAT32, which can't hold a file of 4 GiB or more "
                         f"({fmt.size(size)})")
    free = shutil.disk_usage(folder).free
    if free < size + SPACE_MARGIN:
        raise SpaceError(f"{folder}: {fmt.size(free)} free, and a {fmt.size(size)} file needs "
                         f"{fmt.size(size + SPACE_MARGIN)}")


def _filesystem(folder: Path) -> str:
    """The lowercase name of the file system `folder` is on, or "" when it can't be told."""
    try:
        if sys.platform == "win32":
            return _windows_filesystem(folder)
        if sys.platform == "darwin":
            return _mac_filesystem(folder)
        return _linux_filesystem(folder)
    except (OSError, ValueError, AttributeError):  # an odd mount table or a missing call
        return ""


def _linux_filesystem(folder: Path) -> str:
    """The type of the longest mount point holding `folder`, from `/proc/self/mounts`."""
    path, best, kind = os.path.realpath(folder), "", ""
    with open("/proc/self/mounts", encoding="utf-8") as mounts:
        for line in mounts:
            fields = line.split()
            # The table escapes a space in a mount point as \040.
            point = fields[1].replace("\\040", " ") if len(fields) > 2 else ""
            inside = path == point or path.startswith(point.rstrip("/") + "/")
            if point and inside and len(point) >= len(best):
                best, kind = point, fields[2]
    return kind.lower()


def _mac_filesystem(folder: Path) -> str:
    """`statfs`'s type name, through ctypes, as the stdlib doesn't expose it."""
    import ctypes
    import ctypes.util

    class Statfs(ctypes.Structure):
        # macOS's 64-bit-inode `struct statfs`, the only layout on Apple silicon.
        _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32),
                    ("f_blocks", ctypes.c_uint64), ("f_bfree", ctypes.c_uint64),
                    ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                    ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2),
                    ("f_owner", ctypes.c_uint32), ("f_type", ctypes.c_uint32),
                    ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                    ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
                    ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32),
                    ("f_reserved", ctypes.c_uint32 * 7)]

    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    try:
        # Intel Macs keep the old layout under `statfs` and this one under its suffixed name.
        call = libc["statfs$INODE64"]
    except AttributeError:
        call = libc.statfs
    info = Statfs()
    if call(os.fsencode(folder), ctypes.byref(info)) != 0:
        return ""
    return info.f_fstypename.decode("ascii", "replace").lower()


def _windows_filesystem(folder: Path) -> str:
    """The volume's file system name, from `GetVolumeInformationW`."""
    import ctypes

    kernel32 = getattr(ctypes, "windll").kernel32
    root = ctypes.create_unicode_buffer(261)
    name = ctypes.create_unicode_buffer(261)
    if not kernel32.GetVolumePathNameW(str(folder), root, len(root)):
        return ""
    if not kernel32.GetVolumeInformationW(root, None, 0, None, None, None, name, len(name)):
        return ""
    return name.value.lower()


def _destination(dest: PathLike, url: str) -> Path:
    """`dest`, or the file in the folder `dest` names, named from `url`'s last path part."""
    path = Path(dest)
    if not (path.is_dir() or str(dest).endswith(("/", os.sep))):
        return path
    name = unquote(urlsplit(url).path.rstrip("/").rpartition("/")[2])
    return path / fmt.filename(name, fallback=FALLBACK_NAME)


@contextmanager
def _claimed(dest: Path) -> Iterator[bool]:
    """Claim `dest` for one call, yielding True when another call in this process holds it."""
    key = str(dest.resolve())
    with _claims_lock:
        taken = key in _claims
        _claims.add(key)
    try:
        yield taken
    finally:
        if not taken:
            with _claims_lock:
                _claims.discard(key)


def _resumable(dest: Path) -> _State | None:
    """The newest part an earlier call left for `dest`; broken leftovers are deleted."""
    # Exactly `shared.atomic`'s `.<name>.<8 hex digits>.part` and the sidecar's suffix: a prefix
    # alone would let `a.rar` adopt `.a.rar.001.<hex>.part`, a sibling's part, and delete it.
    own = re.compile(re.escape(f".{dest.name}.") + r"[0-9a-f]{8}" +
                     re.escape(f".part{INFO_SUFFIX}"))
    try:
        names = [name for name in os.listdir(dest.parent) if own.fullmatch(name)]
    except OSError:  # no folder, or one that can't be read: nothing to resume
        return None
    for info in sorted((dest.parent / name for name in names), key=_modified, reverse=True):
        part = info.with_name(info.name[: -len(INFO_SUFFIX)])
        state = _sidecar(info, part)
        if state is None or not (state.validator or state.ranges) or not state.segments \
                or not part.is_file():
            info.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            continue
        if len(state.segments) == 1:
            # One segment is written in order, so the bytes on disk are what arrived.
            state.segments[0].done = part.stat().st_size - state.segments[0].start
        return state
    return None


def _sidecar(info: Path, part: Path) -> _State | None:
    """The state a `.part.json` describes, or None when it can't be trusted."""
    try:
        data = jsonlib.loads(info.read_text(encoding="utf-8"))
        segments = [_Segment(int(start), None if end is None else int(end), int(done))
                    for start, end, done in data["segments"]]
        return _State(part, str(data["url"]), data["validator"], data["size"], segments,
                      bool(data.get("ranges", False)))
    except (OSError, ValueError, KeyError, TypeError):  # cut short, or not one of ours
        return None


def _modified(path: Path) -> float:
    """When `path` was last written; 0 for one that is gone, which then sorts last."""
    try:
        return path.stat().st_mtime
    except OSError:  # deleted since it was listed, by another call or a repair
        return 0.0


def _validator(headers: httpx.Headers) -> str | None:
    """What `If-Range` sends back: a strong `ETag`, else `Last-Modified`; None for neither."""
    etag = headers.get("etag", "")
    # A weak ETag (W/"…") may not be used in If-Range (RFC 9110, 13.1.5).
    if etag and not etag.startswith("W/"):
        return etag
    return headers.get("last-modified") or None


def _write_all(file: io.FileIO, data: bytes) -> None:
    """Write every byte of `data`; an unbuffered handle may take fewer at a time."""
    view = memoryview(data)
    while view:
        written = file.write(view)
        view = view[written or 0:]


def _open_part(part: Path, *, create: bool = False) -> io.FileIO:
    """Open a part unbuffered: create it (it must be new), or write into it in place.

    Unbuffered, so a write that returned is the system's: connections record what they wrote in
    one shared `.part.json`, and a buffer in one handle would let another's save claim bytes a
    crash then loses, leaving a hole of zeros.
    """
    # O_EXCL: a new part's name is fresh, so a file already there is another writer's. Without
    # O_TRUNC an existing part keeps its bytes, and each connection seeks to its own place.
    # Windows opens files in text mode without O_BINARY, and would write every \n as \r\n.
    flags = os.O_WRONLY | (os.O_CREAT | os.O_EXCL if create else 0) | getattr(os, "O_BINARY", 0)
    # 0o644: the owner writes and others only read; the process's umask may narrow that further.
    handle = os.open(part, flags, 0o644)
    try:
        return os.fdopen(handle, "wb", buffering=0)
    # Until fdopen owns the handle, it's ours to close: an open handle keeps Windows from
    # deleting the part.
    except BaseException:
        os.close(handle)
        raise
