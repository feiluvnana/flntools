"""HTTP for scripts: `fetch` a page, `download` a file, or hold a `Client` for a session.

    r = http.fetch("https://example.com/api", body={"q": "x"})   # on a GET, a dict is the query
    r.json; r.text; r.html; r.status; r.ok; r.headers; r.url; r.content
    r = http.fetch(url, check=False)                              # no raise on 4xx/5xx
    with http.Client(rate=2.0, retry=http.Retry(3, statuses=(429, 503)), timeout=30) as c:
        c.fetch(url, method="POST", body={"a": 1})                # a dict or list is JSON
        c.download([mirror1, mirror2], "a.zip", progress=row)     # a part, resumed, size-checked
    with http.fake({"https://example.com/api": {"ok": True}}): ...

`body` is the one way to send data: on a GET or HEAD a dict becomes the query string; on any
other method a dict or list is JSON; bytes or str go as they are. A Client's `retry`, `timeout`
and `stop` cover every call it makes, and the module's `fetch` and `download` use a default one.

Failures are `flnd.errors` classes, classified here by the policy of `docs/errors`:
- a 404 or 410 is `NotFound`, never retried;
- a 429 is `Blocked`, with the site's wait (`Retry-After` or its reset), waited out and retried;
- a 5xx is a retryable `SiteError`, and any other 4xx a `SiteError` that isn't retried; each
  keeps its `status` and `response`;
- a timeout, a reset or a cut-off transfer is a retryable `NetworkError`, and a malformed
  request one that isn't;
- a write the disk refuses is a `DiskError`, and a set `stop` raises `Stopped`.
Every message names the URL redacted (`flnd._text.redact`), never its query or user info.
"""

from __future__ import annotations

import atexit
import email.utils
import functools
import http.cookiejar
import math
import os
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import (
    TYPE_CHECKING, Any, Callable, Iterator, Mapping, Sequence, TypeVar, Union, cast,
)

import msgspec

from flnd import _time
from flnd import _cookies
from flnd import _files
from flnd import _text
from flnd.errors import Blocked, Changed, Error, NetworkError, NotFound, SiteError, Stopped, disk
from flnd._ui import ui

try:
    import httpx
except ImportError:
    raise ImportError("flnd._http needs httpx: pip install httpx") from None

if TYPE_CHECKING:
    from flnd._html import Selection

__all__ = ["Client", "Response", "Retry", "download", "fake", "fetch"]

PathLike = Union[str, "os.PathLike[str]"]
A = TypeVar("A")

CONNECTION_ERRORS = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)
SECRET_HEADERS = ("authorization", "cookie", "proxy-authorization")
# The shortest secret `_scrub` replaces in a message; anything shorter is no secret.
SHORT_SECRET = 4
QUERY_METHODS = ("GET", "HEAD")
FIRST_WAIT = 1.0
MAX_WAIT = 8.0
# The longest Retry-After or rate-limit reset a client waits out, in seconds.
MAX_BACKOFF = 3600.0
# A reset larger than this is a Unix time, not a number of seconds.
EPOCH = 1_000_000_000.0
# A file this big, on a server that serves ranges, comes in SEGMENTS parallel parts.
SEGMENTED = 64 << 20
SEGMENTS = 4
# One token at most: a rate is a floor between two requests to a host, never a burst after a lull.
BURST = 1.0
# The statuses that mean the item is gone for good: never retried, recorded as gone.
GONE = (404, 410)


@dataclass(frozen=True)
class Retry:
    """How many tries a client makes in all, and which statuses and methods it tries again."""

    times: int = 3
    statuses: tuple[int, ...] = field(default=(429, 500, 502, 503, 504), kw_only=True)
    methods: tuple[str, ...] = field(
        default=("GET", "HEAD", "OPTIONS", "PUT", "DELETE"), kw_only=True,
    )

    def __post_init__(self) -> None:
        if self.times < 1:
            raise ValueError(f"times must be at least 1, not {self.times}")


@dataclass(frozen=True, eq=False)
class Response:
    """One HTTP response: status, URL, headers, and body as text, JSON or HTML."""

    status: int
    url: str
    headers: Mapping[str, str]
    content: bytes = field(default=b"", repr=False)
    _raw: httpx.Response | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _Headers(self.headers))

    @property
    def raw(self) -> httpx.Response:
        """Underlying `httpx.Response`."""
        if self._raw is not None:
            return self._raw
        return self._cached("raw", lambda: httpx.Response(
            self.status, headers=dict(self.headers), content=self.content,
        ))

    @property
    def ok(self) -> bool:
        """Whether the status is 2xx."""
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        """The body as text, in charset `Content-Type` names, or UTF-8."""
        charset = _charset(self.headers.get("content-type", ""))
        return self._cached("text", lambda: self.content.decode(charset, "replace"))

    @property
    def json(self) -> Any:
        """The body parsed as JSON. Raises `flnd.errors.Changed` when it isn't JSON."""
        return self._cached("json", self._json)

    @property
    def html(self) -> Selection:
        """The body as a `flnd._html` `Selection`."""
        from flnd import _html

        return self._cached("html", lambda: _html.parse(self.text))

    def _cached(self, name: str, compute: Callable[[], Any]) -> Any:
        key = f"_cached_{name}"
        if key not in self.__dict__:
            self.__dict__[key] = compute()
        return self.__dict__[key]

    def _json(self) -> Any:
        try:
            return msgspec.json.decode(self.content)
        except msgspec.DecodeError as error:
            msg = f"expected JSON from {_text.redact(self.url)}, got {self.text[:80]!r}: {error}"
            raise Changed(msg, status=self.status, response=self) from None


def fetch(url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
          body: Any = None, check: bool = True) -> Response:
    """Send one request with the default client and return its `Response`."""
    return _client().fetch(url, method=method, headers=headers, body=body, check=check)


def download(url: str | Sequence[str], dest: PathLike, *,
             headers: Mapping[str, str] | None = None, resume: bool = True,
             progress: Any = None) -> Path:
    """Download `url`, or the first mirror that works, with the default client."""
    return _client().download(url, dest, headers=headers, resume=resume, progress=progress)


class Client:
    """A session: cookies, headers, a pace per host, and one retry and timeout for every call."""

    def __init__(self, *, cookies: _cookies.Jar | None = None,
                 headers: Mapping[str, str] | None = None, timeout: float = 30.0,
                 rate: float | None = None, retry: Retry = Retry(),
                 stop: threading.Event | None = None) -> None:
        self._raw = httpx.Client(follow_redirects=True, headers=headers, timeout=timeout,
                                 http2=True)
        if cookies is not None:
            self.cookies = cookies
        self._pace = _Pace(rate)
        self._retry = retry
        self._stop = stop

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def raw(self) -> httpx.Client:
        """Underlying `httpx.Client`."""
        return self._raw

    @property
    def cookies(self) -> _cookies.Jar:
        """Active `cookies.Jar` bound to this session."""
        return _cookies.Jar(_cookie_dict(cookie) for cookie in self._raw.cookies.jar)

    @cookies.setter
    def cookies(self, jar: _cookies.Jar) -> None:
        """Replace the client's cookies from `jar`."""
        self._raw.cookies.clear()
        for cookie in jar:
            self._raw.cookies.jar.set_cookie(_cookie_from_dict(cookie))

    def close(self) -> None:
        """Close the underlying `httpx.Client` connection pool."""
        self._raw.close()

    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              body: Any = None, check: bool = True) -> Response:
        """Send one request and return its `Response`; a 4xx or 5xx raises unless `check` is off."""
        method = method.upper()
        return self._call(url, lambda: self._send(method, url, headers, body),
                          method=method, check=check)

    def download(self, url: str | Sequence[str], dest: PathLike, *,
                 headers: Mapping[str, str] | None = None, resume: bool = True,
                 progress: Any = None) -> Path:
        """Stream `url`, or the first of several mirrors that works, through a part onto `dest`.

        Args:
            url: the file's URL, or mirrors of it tried in order.
            dest: where the file lands; its folder is made when missing.
            headers: extra request headers.
            resume: go on from the bytes a failed run kept in the part, and keep them on
                failure; False starts from 0 and keeps nothing.
            progress: a sink for the bytes as they come.

        Returns:
            `dest`.

        Raises:
            BusyError: `resume` is on and another run is saving `dest`.
            Error: the last mirror failed (see the module's list).
        """
        with _files.part(dest, resume=resume) as held:
            self.fill(url, held, headers=headers, resume=resume, progress=progress)
        return held.dest

    def fill(self, url: str | Sequence[str], held: _files.Part, *,
             headers: Mapping[str, str] | None = None, resume: bool = True,
             progress: Any = None) -> None:
        """Stream `url`, or the first of several mirrors that works, into the open part `held`.

        The caller owns the part: it decides where it lands and what happens on failure.

        Args:
            url: the file's URL, or mirrors of it tried in order.
            held: the locked part, from `flnd._files.part`.
            headers: extra request headers.
            resume: go on from the bytes the part holds, with `If-Range` on the validator its
                sidecar kept; False empties it first.
            progress: a sink for the bytes as they come.

        Raises:
            Error: the last mirror failed (see the module's list).
        """
        mirrors = [url] if isinstance(url, str) else list(url)
        if not mirrors:
            raise ValueError("download needs a URL")
        meter = _Meter(progress)
        for mirror in mirrors[:-1]:
            try:
                attempt = functools.partial(self._file, mirror, held, headers, resume, meter)
                self._call(mirror, attempt)
                return
            except Error as error:
                ui.debug("{url} failed: {reason}; trying the next mirror",
                         url=_text.redact(mirror), reason=error)
                # The next mirror may serve other bytes: a Range on this part would splice.
                held.reset()
        self._call(mirrors[-1], lambda: self._file(mirrors[-1], held, headers, resume, meter))

    def _call(self, url: str, send: Callable[[], A], *, method: str = "GET",
              check: bool = True) -> A:
        """Pace and send, trying again as `retry` allows; `check=False` returns a refusal."""
        host = _host(url)
        attempt = 1
        while True:
            self._pace.wait(host, self._stop)
            try:
                return send()
            except Error as error:
                if attempt >= self._retry.times or not _retries(self._retry, method, error):
                    return cast(A, _given_up(error, check))
                self._back_off(host, attempt, error)
            attempt += 1

    def _back_off(self, host: str, attempt: int, error: Error) -> None:
        hinted = error.wait if isinstance(error, Blocked) else None
        jittered = min(FIRST_WAIT * 2 ** (attempt - 1), MAX_WAIT) * random.uniform(0.5, 1.0)
        wait = jittered if hinted is None else hinted
        if isinstance(error, Blocked):
            ui.warn("{host}: rate limited; waiting {time}",
                    host=host, time=_text.duration(round(wait)))
        else:
            ui.debug("retry {n}/{total} in {seconds:.1f}s: {reason}",
                     n=attempt, total=self._retry.times, seconds=wait, reason=error)
        if not _time.sleep(wait, stop=self._stop):
            raise Stopped(f"{host}: stopped")

    def _send(self, method: str, url: str, headers: Mapping[str, str] | None,
              body: Any) -> Response:
        request = self._request(method, url, headers, body)
        try:
            raw = self._raw.send(request)
            try:
                raw.read()
            finally:
                raw.close()
        except CONNECTION_ERRORS as error:
            raise NetworkError(_failed(request, error), retryable=True) from error
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise NetworkError(_failed(request, error)) from error
        return _checked(_response(raw), method)

    def _request(self, method: str, url: str, headers: Mapping[str, str] | None,
                 body: Any) -> httpx.Request:
        options: dict[str, Any] = {}
        if isinstance(body, Mapping) and method in QUERY_METHODS:
            url = _with_query(url, body)
        elif isinstance(body, (Mapping, list)):
            options["json"] = body
        elif body is not None:
            options["content"] = body
        try:
            return self._raw.build_request(method, url, headers=headers, **options)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            msg = f"{method} {_text.redact(url)}: {type(error).__name__}: {error}"
            raise NetworkError(_without_userinfo(msg, url)) from error

    def _file(self, url: str, held: _files.Part, headers: Mapping[str, str] | None,
              resume: bool, meter: _Meter) -> None:
        """One try at a file: resume the part, or start over, or fetch it in segments.

        1. Start from 0 when not resuming, or when a crash left a segmented part with holes.
        2. Ask for `bytes=<size>-`, with `If-Range` on the validator the part kept, so a file
           that changed comes whole (a 200) instead of spliced.
        3. A 206 goes on from the part's end; a 200 starts it over; a 416 is a whole part or a
           restart; a big whole file on a server with ranges comes in segments.

        Args:
            url: the file.
            held: the locked part it goes into.
            headers: extra request headers.
            resume: go on from the part's bytes; False empties it first.
            meter: where the bytes are counted.
        """
        if not resume or (held.size and held.meta.get("holes")):
            held.reset()
        offset = held.size
        validator = held.meta.get("etag") if offset else None
        with self._open(url, headers, f"bytes={offset}-", validator) as response:
            status = response.status_code
            start = offset if status == 206 else 0
            total = _total_bytes(response.headers, start)
            if status != 416:
                if not start and held.size:
                    held.reset()
                held.note(etag=_validator(response.headers))
            whole = status == 206 and start == 0 and total is not None and total >= SEGMENTED
            if status != 416 and not whole:
                return self._write(url, response, held, start, total, meter)
        if status == 416:
            return self._unsatisfied(url, held, headers, offset, total, meter)
        assert total is not None
        _Segments(self, url, held, headers, meter).fetch(total)

    def _unsatisfied(self, url: str, held: _files.Part, headers: Mapping[str, str] | None,
                     offset: int, total: int | None, meter: _Meter) -> None:
        """A 416 on `Range: bytes=offset-`: the part is whole, or it starts over from 0.

        Args:
            url: the file.
            held: the part.
            headers: extra request headers.
            offset: the range's start, which was the part's size.
            total: the file's size from `Content-Range: */<total>`, or None.
            meter: where the bytes are counted.

        Raises:
            SiteError: a 416 for a range from 0, which no retry would mend.
        """
        if total == offset:
            meter.start(offset, offset)
            return
        if not offset:
            raise SiteError(f"GET {_text.redact(url)}: HTTP 416", status=416)
        self._file(url, held, headers, False, meter)

    @contextmanager
    def _open(self, url: str, headers: Mapping[str, str] | None, span: str,
              validator: str | None = None) -> Iterator[httpx.Response]:
        """Stream a GET for `span`, raising for a 4xx or 5xx other than 416.

        Args:
            url: the file.
            headers: extra request headers.
            span: the `Range` header's value.
            validator: an ETag or date for `If-Range`: the server sends the range only while the
                file still matches it, and the whole file otherwise.
        """
        extra = {"Range": span, **({"If-Range": validator} if validator else {})}
        request = self._request("GET", url, {**(headers or {}), **extra}, None)
        try:
            response = self._raw.send(request, stream=True)
            try:
                if response.status_code >= 400 and response.status_code != 416:
                    response.read()
                    _checked(_response(response), "GET")
                yield response
            finally:
                response.close()
        except CONNECTION_ERRORS as error:
            raise NetworkError(_failed(request, error), retryable=True) from error
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise NetworkError(_failed(request, error)) from error

    def _write(self, url: str, response: httpx.Response, held: _files.Part, start: int,
               total: int | None, meter: _Meter) -> None:
        """Append the response's body to the part, and fail a transfer that came short.

        Args:
            url: the file, for messages.
            response: the open streamed response.
            held: the part.
            start: where the response's bytes begin in the file.
            total: the file's size, or None when the server didn't say.
            meter: where the bytes are counted.

        Raises:
            DiskError: the part can't be written.
            NetworkError: the body ended before `total` (retryable).
            Stopped: the client's `stop` was set.
        """
        meter.start(total, start)
        try:
            for chunk in response.iter_bytes():
                self._check(url)
                held.write(chunk)
                meter.add(len(chunk))
            held.flush()
        # A full disk or a refused folder is the disk's failure, not the site's: the run stops.
        except OSError as error:
            raise disk(error, held.path) from error
        if total is not None and held.size < total:
            raise NetworkError(f"GET {_text.redact(url)}: truncated at {held.size} of {total} "
                               "bytes", retryable=True)

    def _check(self, url: str) -> None:
        if self._stop is not None and self._stop.is_set():
            raise Stopped(f"{_text.redact(url)}: stopped")


@contextmanager
def fake(routes: Mapping[str, Any]) -> Iterator[None]:
    """Answer every request in the block from `routes`, and never the network.

    A route maps a URL, or a prefix ending in `*`, to a dict or list (JSON), bytes or str (a body
    that serves ranges), an int (that status), a `Response`, or a callable taking the
    `httpx.Request`. A URL no route matches raises `NetworkError`, as an unknown host would.
    """
    transport = httpx.MockTransport(_Router(routes))
    original = httpx.Client._transport_for_url
    httpx.Client._transport_for_url = lambda self, url: transport
    try:
        yield
    finally:
        httpx.Client._transport_for_url = original


class _Pace:
    """A token bucket per host: `rate` requests a second, one token deep."""

    def __init__(self, rate: float | None):
        if rate is not None and rate <= 0:
            raise ValueError(f"rate must be positive, not {rate}")
        self._rate = rate
        self._hosts: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, stop: threading.Event | None) -> None:
        if self._rate is None:
            return
        with self._lock:
            now = _time.now()
            tokens, at = self._hosts.get(host, (BURST, now))
            # A token is taken at once, so threads waiting together queue one interval apart.
            tokens = min(BURST, tokens + (now - at) * self._rate) - 1.0
            self._hosts[host] = (tokens, now)
        if tokens < 0 and not _time.sleep(-tokens / self._rate, stop=stop):
            raise Stopped(f"{host}: stopped")


class _Meter:
    """Where a download stands on its sink, moved from several threads at once.

    The sink is anything with a settable `total` and an `advance(n)`: a `ui.Row`, or the job's
    per-file stream; None reports nothing.
    """

    def __init__(self, sink: Any):
        self._sink = sink
        self._at = 0
        self._lock = threading.Lock()

    def start(self, total: int | None, at: int) -> None:
        if self._sink is None:
            return
        with self._lock:
            self._sink.total = total
            # A try that starts over moves the sink back by what the last one wrote.
            self._sink.advance(at - self._at)
            self._at = at

    def add(self, n: int) -> None:
        if self._sink is None:
            return
        with self._lock:
            self._sink.advance(n)
            self._at += n


class _Segments:
    """One file fetched as SEGMENTS ranges in parallel, each written at its own offset.

    The ranges land in the part itself. Until they all arrive the part has holes, so its
    sidecar says so first (`holes`): a crash then makes the next run start over instead of
    taking a part of the right size for a whole one.

    Args:
        client: the client whose `_open` sends each range.
        url: the file.
        held: the part the ranges land in.
        headers: extra request headers.
        meter: where the bytes are counted.
    """

    def __init__(self, client: Client, url: str, held: _files.Part,
                 headers: Mapping[str, str] | None, meter: _Meter):
        self.client, self.url, self.headers, self.meter = client, url, headers, meter
        self.held = held
        self.failed = threading.Event()
        self.written: list[int] = []

    def fetch(self, total: int) -> None:
        """Fetch every segment; on a failure keep the first one's bytes, which run from 0.

        Args:
            total: the file's size.

        Raises:
            Error: the first segment's failure, once every segment has stopped.
        """
        self.meter.start(total, 0)
        self.held.note(holes=True)
        self.held.flush_note()
        size = -(-total // SEGMENTS)
        spans = [(start, min(start + size, total) - 1) for start in range(0, total, size)]
        self.written = [0] * len(spans)
        with ThreadPoolExecutor(len(spans)) as pool:
            runs = [pool.submit(self.one, index, span) for index, span in enumerate(spans)]
        errors = [error for run in runs if (error := run.exception()) is not None]
        if errors:
            # Only the first segment runs on from byte 0, so a resume streams the rest from there.
            self.held.cut(self.written[0])
            self.held.note(holes=None)
            raise errors[0]
        self.held.size = total

    def one(self, index: int, span: tuple[int, int]) -> None:
        """Fetch one segment, and fail it when it came short.

        Args:
            index: which segment, from 0.
            span: its first and last byte, inclusive.

        Raises:
            NetworkError: the server sent no range, or a short one (retryable).
        """
        start, end = span
        try:
            with self.client._open(self.url, self.headers, f"bytes={start}-{end}") as response:
                if response.status_code != 206:
                    raise NetworkError(f"GET {_text.redact(self.url)}: no range {start}-{end}",
                                       retryable=True)
                if not self.copy(index, start, response):
                    return
            if self.written[index] != end - start + 1:
                raise NetworkError(f"GET {_text.redact(self.url)}: range {start}-{end} "
                                   f"ended at {self.written[index]} bytes", retryable=True)
        except BaseException:
            self.failed.set()
            raise

    def copy(self, index: int, start: int, response: httpx.Response) -> bool:
        """Write a segment as it comes.

        Args:
            index: which segment, from 0.
            start: its first byte in the file.
            response: its open streamed response.

        Returns:
            True when it came whole; False when it stopped because another segment failed.

        Raises:
            DiskError: the part can't be written.
        """
        for chunk in response.iter_bytes():
            self.client._check(self.url)
            # The first segment's bytes are kept for a resume, so it alone runs on.
            if index and self.failed.is_set():
                return False
            try:
                self.held.write_at(start + self.written[index], chunk)
            except OSError as error:
                raise disk(error, self.held.path) from error
            self.written[index] += len(chunk)
            self.meter.add(len(chunk))
        return True


class _Router:
    """`fake`'s transport handler: the route for a request's URL, as an `httpx.Response`."""

    def __init__(self, routes: Mapping[str, Any]):
        self.exact = {url: route for url, route in routes.items() if not url.endswith("*")}
        prefixes = [(url[:-1], route) for url, route in routes.items() if url.endswith("*")]
        self.prefixes = sorted(prefixes, key=lambda pair: -len(pair[0]))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(userinfo=b""))
        route = self.exact.get(url, self.exact.get(url.split("?", 1)[0]))
        if route is None:
            route = next((answer for prefix, answer in self.prefixes if url.startswith(prefix)),
                         None)
        if route is None:
            raise NetworkError(f"{request.method} {_text.redact(url)}: no route in http.fake")
        if callable(route):
            route = route(request)
        return _answer(route, request)


def _answer(route: Any, request: httpx.Request) -> httpx.Response:
    if isinstance(route, httpx.Response):
        return route
    if isinstance(route, Response):
        return httpx.Response(route.status, headers=dict(route.headers), content=route.content)
    if isinstance(route, int):
        return httpx.Response(route)
    if isinstance(route, (Mapping, list)):
        return httpx.Response(200, content=msgspec.json.encode(route),
                              headers={"content-type": "application/json"})
    body = route.encode() if isinstance(route, str) else bytes(route)
    asked = re.fullmatch(r"bytes=(\d+)-(\d*)", request.headers.get("range", ""))
    if asked is None:
        return httpx.Response(200, content=body, headers={"accept-ranges": "bytes"})
    start, end = int(asked[1]), min(int(asked[2] or len(body) - 1), len(body) - 1)
    if start >= len(body):
        return httpx.Response(416, headers={"content-range": f"bytes */{len(body)}"})
    return httpx.Response(206, content=body[start:end + 1], headers={
        "accept-ranges": "bytes", "content-range": f"bytes {start}-{end}/{len(body)}",
    })


def _with_query(url: str, params: Mapping[str, Any]) -> str:
    try:
        return str(httpx.URL(url).copy_merge_params(params))
    except httpx.InvalidURL as error:
        raise NetworkError(f"{_text.redact(url)}: InvalidURL: {error}") from error


def _retries(retry: Retry, method: str, error: Error) -> bool:
    """Whether `error` is worth another try under `retry`.

    A refusal is retried by its status, as `retry.statuses` lists them. An error without one
    (a timeout, a reset, a cut-off transfer) is retried when its raiser marked it `retryable`;
    a malformed request or an unknown route never is.

    Args:
        retry: the client's policy: which statuses and methods it tries again.
        method: the request's method, upper case; only `retry.methods` are sent twice, but any
            method after a 429, which the server didn't act on.
        error: what the last try raised.

    Returns:
        True to wait and try again.
    """
    status = getattr(error, "status", None)
    # The server acted on nothing it answered with 429, so any method may be sent again.
    if status == 429:
        return 429 in retry.statuses
    if status is None:
        return error.retryable and method in retry.methods
    return method in retry.methods and status in retry.statuses


def _given_up(error: Error, check: bool) -> Response:
    response = getattr(error, "response", None)
    if check or not isinstance(response, Response):
        raise error
    return response


def _host(url: str) -> str:
    try:
        return (httpx.URL(url).host or "").lower()
    except httpx.InvalidURL as error:
        raise NetworkError(f"{_text.redact(url)}: InvalidURL: {error}") from error


def _validator(headers: Mapping[str, str]) -> str | None:
    """What `If-Range` can name a response's file by: a strong ETag, else its date, else None.

    A weak ETag (`W/"..."`) may not be used in `If-Range` (RFC 9110, 13.1.5).

    Args:
        headers: the response's headers.

    Returns:
        The validator, or None when the response has neither.
    """
    tag = headers.get("etag")
    if tag and not tag.startswith("W/"):
        return tag
    return headers.get("last-modified")


def _total_bytes(headers: Mapping[str, str], offset: int) -> int | None:
    content_range = headers.get("content-range")
    if content_range and "/" in content_range:
        tail = content_range.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    content_length = headers.get("content-length")
    if content_length and content_length.isdigit():
        return offset + int(content_length)
    return None


class _Headers(Mapping[str, str]):
    def __init__(self, headers: Mapping[str, str]):
        self._items = MappingProxyType({name.lower(): value for name, value in headers.items()})

    def __getitem__(self, name: str) -> str:
        return self._items[name.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"Headers({sorted(self._items)})"


_default: Client | None = None
_default_lock = threading.Lock()


def _close_default() -> None:
    global _default
    with _default_lock:
        if _default is not None:
            _default.close()
            _default = None


def _client() -> Client:
    global _default
    with _default_lock:
        if _default is None:
            _default = Client()
            # The module's calls share one client, so it keeps no cookie from one to the next.
            refuse_all = http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
            _default._raw.cookies = http.cookiejar.CookieJar(refuse_all)
            atexit.register(_close_default)
        return _default


def _response(raw: httpx.Response) -> Response:
    return Response(status=raw.status_code, url=str(raw.url), headers=raw.headers,
                    content=raw.content, _raw=raw)


def _checked(response: Response, method: str) -> Response:
    status = response.status
    where = f"{method} {_text.redact(response.url)}"
    if status < 400:
        return response
    if status == 429:
        retry_after = _retry_after(response.headers.get("retry-after"))
        if retry_after is None:
            retry_after = _reset(response.headers)
        wait = "" if retry_after is None else f", retry after {retry_after:g} s"
        raise Blocked(f"{where}: HTTP 429, rate limited{wait}", wait=retry_after, status=429,
                      response=response)
    if status in GONE:
        raise NotFound(f"{where}: HTTP {status}", status=status, response=response)
    # A 5xx is the server's trouble, which may pass; any other 4xx is its answer.
    raise SiteError(f"{where}: HTTP {status}", status=status, response=response,
                    retryable=status >= 500)


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - _time.wall()
    if not math.isfinite(seconds):
        return None
    return min(max(0.0, seconds), MAX_BACKOFF)


def _reset(headers: Mapping[str, str]) -> float | None:
    for prefix in ("x-rate-limit-", "x-ratelimit-"):
        try:
            reset = float(headers[prefix + "reset"])
        except (KeyError, ValueError):
            continue
        if not math.isfinite(reset):
            continue
        seconds = reset - _time.wall() if reset > EPOCH else reset
        return min(max(0.0, seconds) + 1.0, MAX_BACKOFF)
    return None


def _cookie_dict(cookie: http.cookiejar.Cookie) -> dict[str, Any]:
    return {
        "name": cookie.name,
        "value": cookie.value or "",
        "domain": cookie.domain,
        "path": cookie.path,
        "expires": -1 if cookie.expires is None else cookie.expires,
        "httpOnly": cookie.has_nonstandard_attr("HttpOnly"),
        "secure": bool(cookie.secure),
        "sameSite": "Lax",
    }


def _cookie_from_dict(cookie: Mapping[str, Any]) -> http.cookiejar.Cookie:
    domain = str(cookie.get("domain", ""))
    path = str(cookie.get("path") or "/")
    expires = cookie.get("expires")
    expiry = None if expires is None or int(expires) < 0 else int(expires)
    return http.cookiejar.Cookie(
        version=0, name=str(cookie["name"]), value=str(cookie["value"]),
        port=None, port_specified=False, domain=domain, domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."), path=path,
        path_specified=bool(cookie.get("path")), secure=bool(cookie.get("secure", False)),
        expires=expiry, discard=expiry is None, comment=None, comment_url=None,
        rest={"HttpOnly": ""} if cookie.get("httpOnly") else {}, rfc2109=False,
    )


def _failed(request: httpx.Request, error: Exception) -> str:
    msg = f"{request.method} {_text.redact(str(request.url))}: {type(error).__name__}: {error}"
    return _without_userinfo(_scrub(msg, request), str(request.url))


def _without_userinfo(message: str, url: str) -> str:
    try:
        userinfo = httpx.URL(url).userinfo.decode()
    except httpx.InvalidURL:
        return message
    return message.replace(userinfo, "***") if userinfo else message


def _scrub(text: str, request: httpx.Request) -> str:
    """`text` without any secret `request` carries, each replaced by `***`.

    A library's error text may quote what it sent, so every secret is looked for in it:
    1. each secret header's whole value (`SECRET_HEADERS`);
    2. its parts: a cookie's values, and the credentials after an `Authorization` scheme
       (`Bearer <token>`);
    3. the values of the URL's query, and the password in its user info.
    A part shorter than `SHORT_SECRET` isn't replaced: "1" or "en" would garble the message,
    and is no secret.

    Args:
        text: an error message about `request`.
        request: the request, with the headers and URL it was sent with.

    Returns:
        The message with each secret replaced.
    """
    secrets: list[str] = []
    for name in SECRET_HEADERS:
        for value in request.headers.get_list(name):
            secrets.append(value)
            if name == "cookie":
                secrets += [part.partition("=")[2].strip() for part in value.split(";")]
            else:
                secrets += value.split()[1:]
    secrets += [value for _, value in request.url.params.multi_items()]
    secrets.append(request.url.password or "")
    # Longest first, so a value that holds a shorter one is replaced whole.
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= SHORT_SECRET:
            text = text.replace(secret, "***")
    return text


def _charset(content_type: str) -> str:
    for parameter in content_type.split(";")[1:]:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip('"')
            try:
                "".encode(charset)
            except LookupError:
                return "utf-8"
            return charset
    return "utf-8"
