"""HTTP for scripts: `fetch` a page, `download` a file, or hold a `Client` for a session.

    r = http.fetch("https://example.com/api", body={"q": "x"})   # on a GET, a dict is the query
    r.json; r.text; r.html; r.status; r.ok; r.headers; r.url; r.content
    r = http.fetch(url, check=False)                              # no raise on 4xx/5xx
    with http.Client(rate=2.0, retry=http.Retry(3, statuses=(429, 503)), timeout=30) as c:
        c.fetch(url, method="POST", body={"a": 1})                # a dict or list is JSON
        c.download([mirror1, mirror2], "a.zip", progress=row)     # staged, resumed, size-checked
    with http.fake({"https://example.com/api": {"ok": True}}): ...

`body` is the one way to send data: on a GET or HEAD a dict becomes the query string; on any
other method a dict or list is JSON; bytes or str go as they are. A Client's `retry`, `timeout`
and `stop` cover every call it makes, and the module's `fetch` and `download` use a default one.
"""

from __future__ import annotations

import atexit
import email.utils
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
    TYPE_CHECKING, Any, BinaryIO, Callable, Iterator, Mapping, Sequence, TypeVar, Union, cast,
)

import msgspec

import shared
from shared import _clock as clock
from shared import _cookies as cookies
from shared import _fs as fs
from shared import _text as text
from shared._sink import Sink
from shared._ui import ui

try:
    import httpx
except ImportError:
    raise ImportError("shared.http needs httpx: pip install 'flntools[http]'") from None

if TYPE_CHECKING:
    from shared._selector import Selection

__all__ = [
    "Client", "Error", "IncompleteError", "RateLimitedError", "RefusedError", "Response", "Retry",
    "StoppedError", "download", "fake", "fetch",
]

PathLike = Union[str, "os.PathLike[str]"]
A = TypeVar("A")

CONNECTION_ERRORS = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)
SECRET_HEADERS = ("authorization", "cookie", "proxy-authorization")
QUERY_METHODS = ("GET", "HEAD")
FIRST_WAIT = 1.0
MAX_WAIT = 8.0
# The longest Retry-After or rate-limit reset a client waits out, in seconds.
MAX_BACKOFF = 3600.0
# A reset larger than this is a Unix time, not a number of seconds.
EPOCH = 1_000_000_000.0
# Writes reach the disk this many bytes at a time; `stop` and progress see every network read,
# and a dropped connection loses nothing already read.
CHUNK = 1 << 20
# A file this big, on a server that serves ranges, comes in SEGMENTS parallel parts.
SEGMENTED = 64 << 20
SEGMENTS = 4
# One token at most: a rate is a floor between two requests to a host, never a burst after a lull.
BURST = 1.0


class Error(shared.Error):
    """The base of every error `shared.http` raises; also what a failed connection raises."""


class _RequestError(Error):
    """A malformed request or protocol error that retrying cannot fix."""


class RefusedError(Error, shared.RefusedError):
    """A 4xx other than 429: the site said no."""

    def __init__(self, status: int, message: str = "", *, response: Response | None = None):
        super().__init__(message or f"HTTP {status}")
        self.status = status
        self.response = response


class RateLimitedError(Error):
    """A 429: the site asks to slow down."""

    def __init__(self, retry_after: float | None, message: str = "", *,
                 response: Response | None = None):
        super().__init__(message or "HTTP 429")
        self.status = 429
        self.retry_after = retry_after
        self.response = response


class StoppedError(Error):
    """A request or download ended because its client's `stop` was set."""


class IncompleteError(Error, shared.BrokenError):
    """A truncated response, 5xx error, or broken transfer."""

    def __init__(self, message: str, *, status: int | None = None,
                 response: Response | None = None):
        super().__init__(message)
        self.status = status
        self.response = response


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
        """The body parsed as JSON. Raises `http.Error` when not JSON."""
        return self._cached("json", self._json)

    @property
    def html(self) -> Selection:
        """The body as a `shared.selector` `Selection`."""
        from shared import _selector

        return self._cached("html", lambda: _selector.parse(self.text))

    def _cached(self, name: str, compute: Callable[[], Any]) -> Any:
        key = f"_cached_{name}"
        if key not in self.__dict__:
            self.__dict__[key] = compute()
        return self.__dict__[key]

    def _json(self) -> Any:
        try:
            return msgspec.json.decode(self.content)
        except msgspec.DecodeError as error:
            msg = f"expected JSON from {text.redact(self.url)}, got {self.text[:80]!r}: {error}"
            raise Error(msg) from None


def fetch(url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
          body: Any = None, check: bool = True) -> Response:
    """Send one request with the default client and return its `Response`."""
    return _client().fetch(url, method=method, headers=headers, body=body, check=check)


def download(url: str | Sequence[str], dest: PathLike, *,
             headers: Mapping[str, str] | None = None, resume: bool = True,
             progress: Sink | None = None) -> Path:
    """Download `url`, or the first mirror that works, with the default client."""
    return _client().download(url, dest, headers=headers, resume=resume, progress=progress)


class Client:
    """A session: cookies, headers, a pace per host, and one retry and timeout for every call."""

    def __init__(self, *, cookies: cookies.Jar | None = None,
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
    def cookies(self) -> cookies.Jar:
        """Active `cookies.Jar` bound to this session."""
        return cookies.Jar(_cookie_dict(cookie) for cookie in self._raw.cookies.jar)

    @cookies.setter
    def cookies(self, jar: cookies.Jar) -> None:
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
                 progress: Sink | None = None) -> Path:
        """Stream `url`, or the first of several mirrors that works, through a stage onto `dest`."""
        mirrors = [url] if isinstance(url, str) else list(url)
        if not mirrors:
            raise ValueError("download needs a URL")
        target = Path(dest)
        meter = _Meter(progress)
        with fs.stage(target, keep=resume) as part:
            for mirror in mirrors[:-1]:
                try:
                    self._call(mirror, lambda: self._file(mirror, part, headers, resume, meter))
                    return target
                except StoppedError:
                    raise
                except Error as error:
                    ui.debug("{url} failed: {reason}; trying the next mirror",
                             url=text.redact(mirror), reason=error)
                    # The next mirror may serve other bytes: a Range on this stage would splice.
                    part.unlink(missing_ok=True)
            self._call(mirrors[-1], lambda: self._file(mirrors[-1], part, headers, resume, meter))
        return target

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
        hinted = getattr(error, "retry_after", None)
        jittered = min(FIRST_WAIT * 2 ** (attempt - 1), MAX_WAIT) * random.uniform(0.5, 1.0)
        wait = jittered if hinted is None else hinted
        if isinstance(error, RateLimitedError):
            ui.warn("{host}: rate limited; waiting {time}",
                    host=host, time=text.duration(round(wait)))
        else:
            ui.debug("retry {n}/{total} in {seconds:.1f}s: {reason}",
                     n=attempt, total=self._retry.times, seconds=wait, reason=error)
        if not clock.sleep(wait, stop=self._stop):
            raise StoppedError(f"{host}: stopped")

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
            raise Error(_failed(request, error)) from error
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise _RequestError(_failed(request, error)) from error
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
            msg = f"{method} {text.redact(url)}: {type(error).__name__}: {error}"
            raise _RequestError(_without_userinfo(msg, url)) from error

    def _file(self, url: str, part: Path, headers: Mapping[str, str] | None, resume: bool,
              meter: _Meter) -> None:
        """One try at a file: resume the stage, or start over, or fetch it in segments."""
        offset = part.stat().st_size if resume and part.exists() else 0
        with self._open(url, headers, f"bytes={offset}-") as response:
            status = response.status_code
            start = offset if status == 206 else 0
            total = _total_bytes(response.headers, start)
            whole = status == 206 and start == 0 and total is not None and total >= SEGMENTED
            if status != 416 and not whole:
                return self._write(url, response, part, start, total, meter)
        if status == 416:
            return self._unsatisfied(url, part, headers, offset, total, meter)
        assert total is not None
        _Segments(self, url, part, headers, meter).fetch(total)

    def _unsatisfied(self, url: str, part: Path, headers: Mapping[str, str] | None,
                     offset: int, total: int | None, meter: _Meter) -> None:
        """A 416 on `Range: bytes=offset-`: the stage is whole, or it is dropped and restarted."""
        if total == offset:
            part.touch()
            meter.start(offset, offset)
            return
        if not offset:
            raise RefusedError(416, f"GET {text.redact(url)}: HTTP 416")
        part.unlink()
        self._file(url, part, headers, False, meter)

    @contextmanager
    def _open(self, url: str, headers: Mapping[str, str] | None,
              span: str) -> Iterator[httpx.Response]:
        """Stream a GET for `span`, raising for a 4xx or 5xx other than 416."""
        request = self._request("GET", url, {**(headers or {}), "Range": span}, None)
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
            raise IncompleteError(_failed(request, error)) from error
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise _RequestError(_failed(request, error)) from error

    def _write(self, url: str, response: httpx.Response, part: Path, start: int,
               total: int | None, meter: _Meter) -> None:
        meter.start(total, start)
        done = start
        with part.open("ab" if start else "wb", buffering=CHUNK) as handle:
            for chunk in response.iter_bytes():
                self._check(url)
                handle.write(chunk)
                done += len(chunk)
                meter.add(len(chunk))
        if total is not None and done < total:
            raise IncompleteError(f"GET {text.redact(url)}: truncated at {done} of {total} bytes")

    def _check(self, url: str) -> None:
        if self._stop is not None and self._stop.is_set():
            raise StoppedError(f"{text.redact(url)}: stopped")


@contextmanager
def fake(routes: Mapping[str, Any]) -> Iterator[None]:
    """Answer every request in the block from `routes`, and never the network.

    A route maps a URL, or a prefix ending in `*`, to a dict or list (JSON), bytes or str (a body
    that serves ranges), an int (that status), a `Response`, or a callable taking the
    `httpx.Request`. A URL no route matches raises `http.Error`.
    """
    transport = httpx.MockTransport(_Router(routes))
    original = httpx.Client._transport_for_url
    setattr(httpx.Client, "_transport_for_url", lambda self, url: transport)
    try:
        yield
    finally:
        setattr(httpx.Client, "_transport_for_url", original)


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
            now = clock.now()
            tokens, at = self._hosts.get(host, (BURST, now))
            # A token is taken at once, so threads waiting together queue one interval apart.
            tokens = min(BURST, tokens + (now - at) * self._rate) - 1.0
            self._hosts[host] = (tokens, now)
        if tokens < 0 and not clock.sleep(-tokens / self._rate, stop=stop):
            raise StoppedError(f"{host}: stopped")


class _Meter:
    """Where a download stands on its sink, moved from several threads at once."""

    def __init__(self, sink: Sink | None):
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
    """One file fetched as SEGMENTS ranges in parallel, each written at its own offset."""

    def __init__(self, client: Client, url: str, part: Path,
                 headers: Mapping[str, str] | None, meter: _Meter):
        self.client, self.url, self.headers, self.meter = client, url, headers, meter
        self.part = part
        # A sibling of the stage: a crash leaves holes here, never in the stage a resume reads.
        self.pieces = part.with_suffix(".seg" + part.suffix)
        self.failed = threading.Event()
        self.written: list[int] = []

    def fetch(self, total: int) -> None:
        self.meter.start(total, 0)
        with self.pieces.open("wb") as handle:
            handle.truncate(total)
        size = -(-total // SEGMENTS)
        spans = [(start, min(start + size, total) - 1) for start in range(0, total, size)]
        self.written = [0] * len(spans)
        with ThreadPoolExecutor(len(spans)) as pool:
            runs = [pool.submit(self.one, index, span) for index, span in enumerate(spans)]
        errors = [error for run in runs if (error := run.exception()) is not None]
        os.replace(self.pieces, self.part)
        if errors:
            # Only the first segment runs on from byte 0, so a resume streams the rest from there.
            with self.part.open("r+b") as handle:
                handle.truncate(self.written[0])
            raise errors[0]

    def one(self, index: int, span: tuple[int, int]) -> None:
        start, end = span
        try:
            with self.client._open(self.url, self.headers, f"bytes={start}-{end}") as response, \
                    self.pieces.open("r+b", buffering=CHUNK) as handle:
                if response.status_code != 206:
                    raise IncompleteError(f"GET {text.redact(self.url)}: no range {start}-{end}")
                handle.seek(start)
                if not self.copy(index, response, handle):
                    return
            if self.written[index] != end - start + 1:
                raise IncompleteError(f"GET {text.redact(self.url)}: range {start}-{end} "
                                      f"ended at {self.written[index]} bytes")
        except BaseException:
            self.failed.set()
            raise

    def copy(self, index: int, response: httpx.Response, handle: BinaryIO) -> bool:
        """Write segment `index` as it comes; False when another segment failed first."""
        for chunk in response.iter_bytes():
            self.client._check(self.url)
            # The first segment's bytes are kept for a resume, so it alone runs on.
            if index and self.failed.is_set():
                return False
            handle.write(chunk)
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
            raise _RequestError(f"{request.method} {text.redact(url)}: no route in http.fake")
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
        raise _RequestError(f"{text.redact(url)}: InvalidURL: {error}") from error


def _retries(retry: Retry, method: str, error: Error) -> bool:
    if isinstance(error, (StoppedError, _RequestError)):
        return False
    status = getattr(error, "status", None)
    # The server acted on nothing it answered with 429, so any method may be sent again.
    if status == 429:
        return 429 in retry.statuses
    return method in retry.methods and (status is None or status in retry.statuses)


def _given_up(error: Error, check: bool) -> Response:
    response = getattr(error, "response", None)
    if check or not isinstance(response, Response):
        raise error
    return response


def _host(url: str) -> str:
    try:
        return (httpx.URL(url).host or "").lower()
    except httpx.InvalidURL as error:
        raise _RequestError(f"{text.redact(url)}: InvalidURL: {error}") from error


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
    where = f"{method} {text.redact(response.url)}"
    if status < 400:
        return response
    if status == 429:
        retry_after = _retry_after(response.headers.get("retry-after"))
        if retry_after is None:
            retry_after = _reset(response.headers)
        wait = "" if retry_after is None else f", retry after {retry_after:g} s"
        raise RateLimitedError(retry_after, f"{where}: HTTP 429, rate limited{wait}",
                               response=response)
    if status < 500:
        raise RefusedError(status, f"{where}: HTTP {status}", response=response)
    raise IncompleteError(f"{where}: HTTP {status}", status=status, response=response)


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
        seconds = when.timestamp() - clock.wall()
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
        seconds = reset - clock.wall() if reset > EPOCH else reset
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
    msg = f"{request.method} {text.redact(str(request.url))}: {type(error).__name__}: {error}"
    return _without_userinfo(_scrub(msg, request), str(request.url))


def _without_userinfo(message: str, url: str) -> str:
    try:
        userinfo = httpx.URL(url).userinfo.decode()
    except httpx.InvalidURL:
        return message
    return message.replace(userinfo, "***") if userinfo else message


def _scrub(text: str, request: httpx.Request) -> str:
    for name in SECRET_HEADERS:
        for value in request.headers.get_list(name):
            if value:
                text = text.replace(value, "***")
                for part in value.split(";") if name == "cookie" else ():
                    secret = part.partition("=")[2].strip()
                    if len(secret) >= 4:
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
