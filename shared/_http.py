"""HTTP for scripts: `http.fetch(url)`, `http.download(url, dest)`, and `http.Client`."""

from __future__ import annotations

import atexit
import email.utils
import http.cookiejar
import json as jsonlib
import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import (TYPE_CHECKING, Any, Callable, Iterator, Literal, Mapping, Sequence, TypeVar,
                    Union, overload)

import shared
from shared import _clock as clock
from shared import _cookies as cookies
from shared import _fmt as fmt
from shared._log import log
from shared._pace import EPOCH, MAX_BACKOFF, Host, Pace

try:
    import httpx
except ImportError:
    raise ImportError("shared.http needs httpx: pip install 'flntools[http]'") from None

if TYPE_CHECKING:  # `selector` loads on first `html`; JSON scripts skip parsel
    from shared._selector import Selection

__all__ = [
    "Client", "Error", "IncompleteError", "RateLimitedError", "RefusedError", "Response",
    "StoppedError", "download", "fetch",
]

PathLike = Union[str, "os.PathLike[str]"]
# What `Client.call`'s `send` returns.
A = TypeVar("A")

# Chrome's own limit of connections to one host, so a script fetching in parallel looks to a
# site like one browser, not a crowd. It counts requests in flight through this module, per host
# and port, across every thread and client in the process.
MAX_PER_HOST = 6
# The statuses `soft=True` turns into None: "not logged in", "not allowed" and "not there", the
# three a script asks about a profile or a post that may be private or gone.
SOFT_STATUSES = frozenset({401, 403, 404})
# What httpx raises when a connection drops or times out; worth another attempt. Others, such as
# an invalid URL or too many redirects, would fail the same way again.
CONNECTION_ERRORS = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)
# Headers whose values are secrets, kept out of every message.
SECRET_HEADERS = ("authorization", "cookie", "proxy-authorization")
# Waits before the 2nd, 3rd, … attempt, in seconds: 1 s, then 2 s, then 4 s, up to 8 s. Short,
# since `fetch` retries only a dead connection or a 5xx, which a moment usually cures; a site
# that wants a longer pause says so with a 429, and that is the caller's to wait out.
FIRST_WAIT = 1.0
MAX_WAIT = 8.0

# The request extension every request of `download` carries, so a caller's transport can tell a
# download's requests from others; flnd's sends them once, since `download` retries on its own.
DOWNLOAD_EXTENSION = "shared.http.download"
# The request extension a split file's pieces carry, which `Transport` sends over HTTP/1.1:
# HTTP/2 would put every piece on one TCP connection, sharing one congestion window.
PIECE_EXTENSION = "shared.http.piece"
# The most connections `parts` may ask for on one file.
MAX_PARTS = 8
# Idle connections `Transport` keeps open for reuse: 8 per host across a run's few CDNs.
KEEP_ALIVE = 64
# A client's wait of this many seconds or more is a countdown row: a slow run explains itself.
SHOWN_WAIT = 15.0
# Methods safe to resend over HTTP/1.1 after an HTTP/2 connection drops.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

class Error(shared.Error):
    """The base of every error `shared.http` raises; also what a failed connection raises."""


class _RequestError(Error):
    """A malformed request or protocol error that retrying cannot fix."""


class RefusedError(Error, shared.RefusedError):
    """A 4xx other than 429: the site said no.

    Args:
        status: the HTTP status, such as 403.
        message: the text shown; names the method, the URL without its query and the status.
        response: the refusing response with its body, when there was one to keep, so a caller
            can read why the site said no (Instagram's "please wait" comes as a 400 with a JSON
            body); None otherwise. It is never shown in the message.
    """

    def __init__(self, status: int, message: str = "", *, response: Response | None = None):
        super().__init__(message or f"HTTP {status}")
        self.status = status
        self.response = response


class RateLimitedError(Error):
    """A 429: the site asks to slow down. Raised at once, never slept on, so the caller backs off.

    Not a `RefusedError`, so `except http.RefusedError` doesn't swallow a rate limit by mistake.

    Args:
        retry_after: the seconds the site's `Retry-After` header asks to wait, else until its rate
            headers' reset (`x-rate-limit-reset`, X's), or None without either.
        message: the text shown.
    """

    def __init__(self, retry_after: float | None, message: str = ""):
        super().__init__(message or "HTTP 429")
        self.status = 429
        self.retry_after = retry_after


class StoppedError(Error):
    """A download stopped because its caller set `stop`; a resumable part is kept for later."""


class IncompleteError(Error, shared.BrokenError):
    """A truncated response, 5xx error, or broken transfer."""


@dataclass(frozen=True, eq=False)
class Response:
    """One HTTP response: status, URL, headers, and body as text, JSON or HTML.

        page = http.fetch("https://example.com/items")
        page.ok, page.status, page.url            # True, 200, "https://example.com/items"
        page.headers["Content-Type"]              # names are looked up in any case
        page.json["items"], page.html.css("h1::text").first

    The same type comes from `http.fetch` and `browser.Page.fetch`.
    It never changes once made and holds no connection, so it may be kept and passed between
    threads. `text`, `json` and `html` are worked out on first read and kept, so reading one twice
    costs nothing and a response never fetched as HTML never loads parsel. Two threads reading the
    same one for the first time at once may each work it out; they get equal values.

    Two responses are equal only when they are the same object, and a response hashes by identity,
    so it can be a dict key or set member. The headers are unhashable, so a field-by-field
    hash isn't possible; compare `status`, `url` or `content` when that is what you mean.

    Args:
        status: the HTTP status, such as 200.
        url: the final URL, after any redirects.
        headers: the response headers; any mapping, kept as a read-only copy with lowercase names.
        content: the body as bytes, as sent (after any `Content-Encoding` is undone).
    """

    # The HTTP status, such as 200 or 404.
    status: int
    # The final URL, after any redirects.
    url: str
    # The headers, read-only; names are lowercase, and looking one up ignores case.
    headers: Mapping[str, str]
    # The body as bytes, decoded from any Content-Encoding.
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
        """Whether the status is 2xx, as Playwright's and fetch()'s `ok` mean it."""
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
        """The body as a `shared.selector` `Selection`, made by `selector.parse(text)`."""
        from shared import _selector

        return self._cached("html", lambda: _selector.parse(self.text))

    def _cached(self, name: str, compute: Callable[[], Any]) -> Any:
        """`compute()`, computed on first read of `name` and cached in `__dict__`.

        Kept there directly, past the frozen `__setattr__`, as `functools.cached_property` does;
        a plain property instead of that one, so the API snapshot shows each read's type.
        """
        key = f"_cached_{name}"
        if key not in self.__dict__:
            self.__dict__[key] = compute()
        return self.__dict__[key]

    def _json(self) -> Any:
        """The body parsed as JSON, raising `http.Error` when it isn't."""
        try:
            return jsonlib.loads(self.text)
        except ValueError as error:  # json's JSONDecodeError; only a body that isn't JSON raises it
            msg = f"expected JSON from {_where(self.url)}, got {self.text[:80]!r}: {error}"
            raise Error(msg) from None


@overload
def fetch(url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
          params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
          soft: Literal[False] = ..., attempts: int = ..., timeout: float = ...) -> Response: ...
@overload
def fetch(url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
          params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
          soft: Literal[True], attempts: int = ..., timeout: float = ...) -> Response | None: ...
@overload
def fetch(url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
          params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
          soft: bool, attempts: int = ..., timeout: float = ...) -> Response | None: ...
def fetch(url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
          params: Mapping[str, Any] | None = None, data: Any = None, json: Any = None,
          soft: bool = False, attempts: int = 3, timeout: float = 30.0) -> Response | None:
    """Send one HTTP request and return its `Response`, retrying a dead connection or a 5xx."""
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, not {attempts}")
    sender = _client()
    attempt = 1
    while True:
        try:
            return _send_once(
                sender, url, method=method, headers=headers, params=params, data=data, json=json,
                soft=soft, timeout=timeout,
            )
        except (RefusedError, RateLimitedError, _RequestError):
            raise
        except Error as error:
            if attempt >= attempts:
                raise
            seconds = min(FIRST_WAIT * 2 ** (attempt - 1), MAX_WAIT)
            log.debug("retry {n} of {url} in {seconds:g} s: {reason}",
                      n=attempt, url=_where(url), seconds=seconds, reason=error)
            clock.sleep(seconds)
            attempt += 1


def _send_once(
    sender: httpx.Client, url: str, *, method: str = "GET",
    headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None,
    data: Any = None, json: Any = None, soft: bool = False, timeout: float = 30.0,
) -> Response | None:
    """Build and send one HTTP request, wrapping httpx errors in `http.Error`."""
    body: dict[str, Any] = (
        {"content": data} if isinstance(data, (bytes, bytearray, memoryview, str))
        else {"data": data}
    )
    try:
        request = sender.build_request(
            method, url, headers=headers, params=params, json=json, timeout=timeout, **body
        )
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        raise _RequestError(f"{method} {_where(url)}: {type(error).__name__}: {error}") from error
    try:
        response = _send(sender, request)
    except CONNECTION_ERRORS as error:
        msg = f"{request.method} {_where(request.url)}: {type(error).__name__}: {error}"
        raise Error(_scrub(msg, request)) from error
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        msg = f"{request.method} {_where(request.url)}: {type(error).__name__}: {error}"
        raise _RequestError(_scrub(msg, request)) from error
    return _checked(response, request.method, soft=soft)


def download(url: str | Sequence[str], dest: PathLike, *,
             headers: Mapping[str, str] | None = None, resume: bool = True,
             attempts: int = 3, timeout: float = 30.0,
             progress: Callable[[int, int | None], Any] | None = None,
             stop: threading.Event | None = None) -> Path:
    """Stream one or more URLs atomically to `dest` with resume and progress support."""
    return _download_with(
        url, dest, client=_client(), headers=headers, resume=resume, attempts=attempts,
        timeout=timeout, progress=progress, stop=stop,
    )


def _download_with(
    url: str | Sequence[str], dest: PathLike, *, client: httpx.Client,
    headers: Mapping[str, str] | None = None, resume: bool = True, attempts: int = 3,
    timeout: float = 30.0, progress: Callable[[int, int | None], Any] | None = None,
    stop: threading.Event | None = None,
) -> Path:
    """Validate options and run `_download.run` using `client`."""
    from shared import _download

    mirrors = [url] if isinstance(url, str) else list(url)
    if not mirrors:
        raise ValueError("download needs a URL")
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, not {attempts}")
    options = _download._Options(
        headers=headers, resume=resume, attempts=attempts, timeout=timeout,
        progress=progress, stop=stop,
    )
    return _download.run(mirrors, dest, client, options)


class Client:
    """Reusable HTTP session with cookies, retries, and optional rate limiting."""

    def __init__(self, *, cookies: cookies.Jar | None = None,
                 headers: Mapping[str, str] | None = None,
                 timeout: float = 30.0, rate: float | None = None) -> None:
        if rate is not None and rate <= 0:
            raise ValueError(f"rate must be positive, not {rate}")
        self._raw = httpx.Client(
            follow_redirects=True, headers=headers, timeout=timeout, transport=_Transport(),
        )
        if cookies is not None:
            self.cookies = cookies
        self._pace: Pace | None = Pace(floor=1.0 / rate) if rate is not None else None
        self._stop: threading.Event | None = None
        self._speeds: dict[str, float] = {}
        self._hosts: dict[str, Host] = {}
        self._hosts_lock = threading.Lock()

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
        self._raw.cookies.clear()
        for cookie in jar:
            self._raw.cookies.jar.set_cookie(_cookie_from_dict(cookie))

    def close(self) -> None:
        """Close the underlying `httpx.Client` connection pool."""
        self._raw.close()

    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
              soft: Literal[False] = ..., attempts: int = ...,
              timeout: float = ...) -> Response: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
              soft: Literal[True], attempts: int = ...,
              timeout: float = ...) -> Response | None: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
              soft: bool, attempts: int = ..., timeout: float = ...) -> Response | None: ...
    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              params: Mapping[str, Any] | None = None, data: Any = None, json: Any = None,
              soft: bool = False, attempts: int = 3, timeout: float = 30.0) -> Response | None:
        """Perform an HTTP request with retries; returns `None` on failure when `soft=True`."""
        if attempts < 1:
            raise ValueError(f"attempts must be at least 1, not {attempts}")
        return self._call(
            url, lambda: _send_once(
                self._raw, url, method=method, headers=headers, params=params, data=data,
                json=json, soft=soft, timeout=timeout,
            ),
            attempts=attempts,
        )

    def download(self, url: str | Sequence[str], dest: PathLike, *,
                 headers: Mapping[str, str] | None = None, resume: bool = True,
                 attempts: int = 3, timeout: float = 30.0,
                 progress: Callable[[int, int | None], Any] | None = None,
                 stop: threading.Event | None = None) -> Path:
        """Stream one or more URLs atomically to `dest` with resume and progress support."""
        first = url if isinstance(url, str) else next(iter(url), "")
        halt = stop if stop is not None else self._stop
        return self._call(first, lambda: _download_with(
            url, dest, client=self._raw, headers=headers, resume=resume, attempts=attempts,
            timeout=timeout, progress=progress, stop=halt,
        ))

    def _call(self, url: str, send: Callable[[], A], *, attempts: int = 1) -> A:
        """Run `send` in `url`'s host pace, waiting out rate limits and retrying errors."""
        host = self._host(url)
        attempt = 1
        while True:
            wait, epoch = host.turn(url)
            self._wait(host, wait)
            try:
                answer = send()
            except RateLimitedError as error:
                self._throttled(host, error.retry_after)
                continue
            except RefusedError as error:
                if error.response is not None:
                    host.record(error.response.headers)
                raise
            except (StoppedError, _RequestError):
                raise
            except Error as error:
                if attempt >= attempts:
                    raise
                seconds = min(FIRST_WAIT * 2 ** (attempt - 1), MAX_WAIT)
                log.debug("retry {n} of {url} in {seconds:g} s: {error}", n=attempt,
                          url=_where(url), seconds=seconds, error=error)
                if clock.sleep(seconds, stop=self._stop):
                    raise StoppedError(f"{host.name}: stopped") from None
                attempt += 1
                continue
            headers: Mapping[str, str] = getattr(answer, "headers", None) or {}
            host.record(headers)
            if getattr(answer, "status", None) == 429:
                retry_after = _retry_after(headers.get("retry-after"))
                self._throttled(host, retry_after if retry_after is not None else _reset(headers))
                continue
            host.clean(epoch)
            return answer

    def _host(self, url: str) -> Host:
        """The pace of `url`'s host or label, made on first use with an earlier run's speed."""
        try:
            name = (httpx.URL(url).host or "").lower()
        except httpx.InvalidURL as error:
            raise _RequestError(f"{_where(url)}: InvalidURL: {error}") from error
        pace = self._pace if self._pace is not None and self._pace.paces(name) else None
        key = pace.label if pace is not None and pace.label else name
        with self._hosts_lock:
            if key not in self._hosts:
                self._hosts[key] = Host(key, pace, speed=self._speeds.get(key, 1.0))
            return self._hosts[key]

    def _throttled(self, host: Host, retry_after: float | None,
                   reason: str = "rate limited") -> None:
        """Count a throttle on `host`, and say how long it holds; the next turn waits it out."""
        seconds = host.throttled(retry_after)
        if seconds is not None:
            log.warn("{host}: {reason}; waiting {time} (attempt {n})", host=host.name,
                     reason=reason, time=fmt.duration(round(seconds)), n=host.strikes)

    def _wait(self, host: Host, seconds: float) -> None:
        """Wait `seconds` for `host`'s turn, as a countdown row when it is long; `stop` ends it."""
        if seconds <= 0:
            return
        with self._hosts_lock:
            show = seconds >= SHOWN_WAIT and not host.showing
            host.showing = host.showing or show
        if not show:
            if clock.sleep(seconds, stop=self._stop):
                raise StoppedError(f"{host.name}: stopped")
            return
        try:
            with log.progress(f"{host.name}: waiting", total=seconds, show=_seconds,
                              wait=True) as bar:
                end = clock.now() + seconds
                # One-second steps, so the countdown moves.
                while (left := end - clock.now()) > 0:
                    bar.count = seconds - left
                    if clock.sleep(min(1.0, left), stop=self._stop):
                        raise StoppedError(f"{host.name}: stopped")
        finally:
            with self._hosts_lock:
                host.showing = False


class _Transport(httpx.BaseTransport):
    """An httpx transport that sends split pieces over HTTP/1.1 and the rest over HTTP/2."""

    def __init__(self, **options: Any):
        options.setdefault("limits", httpx.Limits(max_keepalive_connections=KEEP_ALIVE))
        self._http1: httpx.BaseTransport = httpx.HTTPTransport(**options)
        self._http2: httpx.BaseTransport | None = (httpx.HTTPTransport(http2=True, **options)
                                                   if _has_h2() else None)
        # Hosts whose HTTP/2 failed; any thread may add one.
        self._fallen: set[str] = set()
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send `request` over HTTP/2 unless it is a piece or its host fell back."""
        host = request.url.host
        if self._http2 is None or request.extensions.get(PIECE_EXTENSION) or host in self._fallen:
            return self._http1.handle_request(request)
        try:
            return self._http2.handle_request(request)
        except (httpx.RemoteProtocolError, httpx.LocalProtocolError) as error:
            with self._lock:
                self._fallen.add(host)
            log.debug("{host}: HTTP/2 failed ({error}); HTTP/1.1 from now on", host=host,
                      error=type(error).__name__)
            if request.method.upper() not in IDEMPOTENT_METHODS:
                raise
            return self._http1.handle_request(request)

    def close(self) -> None:
        """Close both transports' connections."""
        self._http1.close()
        if self._http2 is not None:
            self._http2.close()


class _Headers(Mapping[str, str]):
    """A read-only header mapping with lowercase names, looked up in any case."""

    def __init__(self, headers: Mapping[str, str]):
        self._items = MappingProxyType({name.lower(): value for name, value in headers.items()})

    def __getitem__(self, name: str) -> str:
        return self._items[name.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        # Names only: a `Set-Cookie` value is a secret.
        return f"Headers({sorted(self._items)})"


# The module's client, made on first use under `_client_lock`, since two threads may both be first.
_default: httpx.Client | None = None
_client_lock = threading.Lock()
# One semaphore per (scheme, host, port); `_slots_lock` makes sure two threads asking for a new
# host at once get the same one, not one each.
_slots: dict[tuple[str, str, int | None], threading.BoundedSemaphore] = {}
_slots_lock = threading.Lock()


def _close_default() -> None:
    """Close the module's default client at interpreter shutdown."""
    global _default
    with _client_lock:
        if _default is not None:
            _default.close()
            _default = None


def _client() -> httpx.Client:
    """Module's own client, made on first use: follows redirects and keeps no cookies."""
    global _default
    with _client_lock:
        if _default is None:
            _default = _new_client()
            atexit.register(_close_default)
        return _default


def _new_client() -> httpx.Client:
    """A client that follows redirects and keeps no cookies between calls."""
    # A jar whose policy allows no domain keeps no cookie, so a cookie one caller's fetch is sent
    # never rides along on another caller's request to the same site.
    refuse_all = http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
    return httpx.Client(follow_redirects=True, cookies=http.cookiejar.CookieJar(refuse_all),
                        transport=_Transport())


def _has_h2() -> bool:
    """Whether the `h2` package httpx needs for HTTP/2 is installed."""
    try:
        import h2  # noqa: F401
    except ImportError:  # HTTP/1.1 only, which is what httpx sends without it anyway
        return False
    return True


def _seconds(value: float) -> str:
    """A countdown row's count and total, such as "1m 30s"."""
    return fmt.duration(round(value))


def _slot(url: httpx.URL) -> threading.BoundedSemaphore:
    """The semaphore that counts requests in flight to `url`'s host, made on first use."""
    key = (url.scheme, url.host, url.port)
    with _slots_lock:
        if key not in _slots:
            _slots[key] = threading.BoundedSemaphore(MAX_PER_HOST)
        return _slots[key]


def _send(client: httpx.Client, request: httpx.Request) -> Response:
    """Send one attempt holding a host slot, and read the whole body into `Response`."""
    with _slot(request.url):
        response = client.send(request)
        try:
            response.read()
        finally:
            response.close()
    return Response(status=response.status_code, url=str(response.url), headers=response.headers,
                    content=response.content, _raw=response)


def _checked(response: Response, method: str, *, soft: bool) -> Response | None:
    """The response for success or redirect; None for soft 401/403/404; error otherwise."""
    status = response.status
    where = f"{method} {_where(response.url)}"
    if status < 400:
        return response
    if status == 429:
        retry_after = _retry_after(response.headers.get("retry-after"))
        if retry_after is None:
            retry_after = _reset(response.headers)
        wait = "" if retry_after is None else f", retry after {retry_after:g} s"
        raise RateLimitedError(retry_after, f"{where}: HTTP 429, rate limited{wait}")
    if soft and status in SOFT_STATUSES:
        return None
    if status < 500:
        raise RefusedError(status, f"{where}: HTTP {status}", response=response)
    raise IncompleteError(f"{where}: HTTP {status}")


def _retry_after(value: str | None) -> float | None:
    """A `Retry-After` header in seconds: seconds, or HTTP date from now; None without one."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:  # not a number, so maybe an HTTP date
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):  # neither form: treated as no header, as browsers do
            return None
        seconds = when.timestamp() - clock.wall()
    if not math.isfinite(seconds):
        return None
    return min(max(0.0, seconds), MAX_BACKOFF)


def _reset(headers: Mapping[str, str]) -> float | None:
    """The seconds until a 429's rate headers reset, plus a second; None without them."""
    for prefix in ("x-rate-limit-", "x-ratelimit-"):
        try:
            reset = float(headers[prefix + "reset"])
        except (KeyError, ValueError):  # missing or not a number: this spelling says nothing
            continue
        if not math.isfinite(reset):
            continue
        # A big number is a Unix time (X's), a small one seconds from now; the second is slack for
        # a clock a little behind the server's.
        seconds = reset - clock.wall() if reset > EPOCH else reset
        return min(max(0.0, seconds) + 1.0, MAX_BACKOFF)
    return None


def _where(url: httpx.URL | str) -> str:
    """The URL without its query or fragment, which may hold a signed token, for a message."""
    try:
        parsed = httpx.URL(str(url))
    except httpx.InvalidURL:
        return str(url).split("?", 1)[0].split("#", 1)[0]
    return str(parsed.copy_with(query=None, fragment=None))


def _cookie_dict(cookie: http.cookiejar.Cookie) -> dict[str, Any]:
    """Convert a stdlib `Cookie` into Playwright's dict shape for `cookies.Jar`."""
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
    """Convert a `cookies.Jar` dict into a stdlib `Cookie` for `httpx.Client`."""
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


def _scrub(text: str, request: httpx.Request) -> str:
    """`text` with every secret header value of `request` replaced by `***`."""
    for name in SECRET_HEADERS:
        for value in request.headers.get_list(name):
            if value:
                text = text.replace(value, "***")
                # A cookie header's single values too, in case only one of them is quoted.
                for part in value.split(";") if name == "cookie" else ():
                    secret = part.partition("=")[2].strip()
                    if len(secret) >= 4:
                        text = text.replace(secret, "***")
    return text


def _charset(content_type: str) -> str:
    """The charset a `Content-Type` names, if Python knows it; UTF-8 otherwise."""
    for parameter in content_type.split(";")[1:]:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip('"')
            try:
                "".encode(charset)
            except LookupError:  # a charset Python doesn't know: UTF-8, as browsers mostly do
                return "utf-8"
            return charset
    return "utf-8"
