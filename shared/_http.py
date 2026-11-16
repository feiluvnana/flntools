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
from typing import (
    TYPE_CHECKING, Any, Callable, Iterator, Literal, Mapping, Sequence, TypeVar, Union, overload,
)

import shared
from shared import _clock as clock
from shared import _cookies as cookies
from shared import _text as text
from shared._ui import ui

try:
    import httpx
except ImportError:
    raise ImportError("shared.http needs httpx: pip install 'flntools[http]'") from None

if TYPE_CHECKING:
    from shared._selector import Selection

__all__ = [
    "Client", "Error", "IncompleteError", "RateLimitedError", "RefusedError", "Response",
    "StoppedError", "download", "fetch",
]

PathLike = Union[str, "os.PathLike[str]"]
A = TypeVar("A")

SOFT_STATUSES = frozenset({401, 403, 404})
CONNECTION_ERRORS = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)
SECRET_HEADERS = ("authorization", "cookie", "proxy-authorization")
FIRST_WAIT = 1.0
MAX_WAIT = 8.0
MAX_BACKOFF = 3600.0
EPOCH = 1_000_000_000.0
CHUNK_SIZE = 65_536


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

    def __init__(self, retry_after: float | None, message: str = ""):
        super().__init__(message or "HTTP 429")
        self.status = 429
        self.retry_after = retry_after


class StoppedError(Error):
    """A download stopped because its caller set `stop`."""


class IncompleteError(Error, shared.BrokenError):
    """A truncated response, 5xx error, or broken transfer."""


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
            return jsonlib.loads(self.text)
        except ValueError as error:
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
            ui.debug("retry {n}/{total} in {seconds:g}s: {reason}",
                     n=attempt, total=attempts, seconds=seconds, reason=error)
            clock.sleep(seconds)
            attempt += 1


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


class Client:
    """Reusable HTTP session with cookies, retries, and optional rate limiting."""

    def __init__(self, *, cookies: cookies.Jar | None = None,
                 headers: Mapping[str, str] | None = None,
                 timeout: float = 30.0, rate: float | None = None) -> None:
        if rate is not None and rate <= 0:
            raise ValueError(f"rate must be positive, not {rate}")
        self._raw = httpx.Client(
            follow_redirects=True, headers=headers, timeout=timeout,
        )
        if cookies is not None:
            self.cookies = cookies
        self._interval = (1.0 / rate) if rate is not None else 0.0
        self._last_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None

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
        ), rate_attempts=attempts)

    def _pace_wait(self, host: str) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = clock.now()
            wait = (self._last_at.get(host, 0.0) + self._interval) - now
            self._last_at[host] = max(now, self._last_at.get(host, 0.0) + self._interval)
        if wait > 0 and not clock.sleep(wait, stop=self._stop):
            raise StoppedError(f"{host}: stopped")

    def _call(self, url: str, send: Callable[[], A], *, attempts: int = 1,
              rate_attempts: int | None = None) -> A:
        host = _host_name(url)
        attempt = 1
        max_rate = rate_attempts if rate_attempts is not None else attempts
        while True:
            self._pace_wait(host)
            try:
                return send()
            except RateLimitedError as error:
                wait = error.retry_after if error.retry_after is not None else 5.0
                if attempt >= max_rate and error.retry_after is None:
                    raise
                ui.warn("{host}: rate limited; waiting {time}",
                        host=host, time=text.duration(round(wait)))
                if not clock.sleep(wait, stop=self._stop):
                    raise StoppedError(f"{host}: stopped") from None
                attempt += 1
            except (RefusedError, StoppedError, _RequestError):
                raise
            except Error as error:
                if attempt >= attempts:
                    raise
                seconds = min(FIRST_WAIT * 2 ** (attempt - 1), MAX_WAIT)
                ui.debug("retry {n}/{total} in {seconds:g}s: {reason}", n=attempt,
                         total=attempts, seconds=seconds, reason=error)
                if not clock.sleep(seconds, stop=self._stop):
                    raise StoppedError(f"{host}: stopped") from None
                attempt += 1


def _host_name(url: str) -> str:
    try:
        return (httpx.URL(url).host or "").lower()
    except httpx.InvalidURL as error:
        raise _RequestError(f"{_where(url)}: InvalidURL: {error}") from error


def _send_once(
    sender: httpx.Client, url: str, *, method: str = "GET",
    headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None,
    data: Any = None, json: Any = None, soft: bool = False, timeout: float = 30.0,
) -> Response | None:
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


def _download_with(
    url: str | Sequence[str], dest: PathLike, *, client: httpx.Client,
    headers: Mapping[str, str] | None = None, resume: bool = True, attempts: int = 3,
    timeout: float = 30.0, progress: Callable[[int, int | None], Any] | None = None,
    stop: threading.Event | None = None,
) -> Path:
    mirrors = [url] if isinstance(url, str) else list(url)
    if not mirrors:
        raise ValueError("download needs a URL")
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, not {attempts}")
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.part")
    last_error: Error | None = None
    for mirror in mirrors:
        try:
            _download_mirror(
                mirror, part, client=client, headers=headers, resume=resume,
                attempts=attempts, timeout=timeout, progress=progress, stop=stop,
            )
            os.replace(part, target)
            return target
        except (RefusedError, RateLimitedError, StoppedError, _RequestError):
            raise
        except Error as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _download_mirror(
    url: str, part: Path, *, client: httpx.Client,
    headers: Mapping[str, str] | None, resume: bool, attempts: int,
    timeout: float, progress: Callable[[int, int | None], Any] | None,
    stop: threading.Event | None,
) -> None:
    attempt = 1
    while True:
        if stop is not None and stop.is_set():
            raise StoppedError(f"{_where(url)}: stopped")
        try:
            _stream_once(
                url, part, client=client, headers=headers, resume=resume,
                timeout=timeout, progress=progress, stop=stop,
            )
            return
        except (RefusedError, RateLimitedError, StoppedError, _RequestError):
            raise
        except Error as error:
            if attempt >= attempts:
                raise
            seconds = min(FIRST_WAIT * 2 ** (attempt - 1), MAX_WAIT)
            ui.debug("retry {n}/{total} in {seconds:g}s: {reason}",
                     n=attempt, total=attempts, seconds=seconds, reason=error)
            if not clock.sleep(seconds, stop=stop):
                raise StoppedError(f"{_where(url)}: stopped") from None
            attempt += 1


def _stream_once(
    url: str, part: Path, *, client: httpx.Client,
    headers: Mapping[str, str] | None, resume: bool, timeout: float,
    progress: Callable[[int, int | None], Any] | None, stop: threading.Event | None,
) -> None:
    req_headers = dict(headers or {})
    offset = part.stat().st_size if (resume and part.exists()) else 0
    if offset > 0:
        req_headers["Range"] = f"bytes={offset}-"
    try:
        request = client.build_request("GET", url, headers=req_headers, timeout=timeout)
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        raise _RequestError(f"GET {_where(url)}: {type(error).__name__}: {error}") from error
    try:
        with client.stream(
            request.method, request.url, headers=request.headers, timeout=timeout,
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                fake = Response(
                    status=resp.status_code, url=str(resp.url),
                    headers=resp.headers, content=resp.content, _raw=resp,
                )
                _checked(fake, "GET", soft=False)
            appending = offset > 0 and resp.status_code == 206
            done = offset if appending else 0
            total = _total_bytes(resp.headers, done)
            mode = "ab" if appending else "wb"
            written = 0
            with part.open(mode) as handle:
                for chunk in resp.iter_bytes():
                    if stop is not None and stop.is_set():
                        raise StoppedError(f"{_where(url)}: stopped")
                    handle.write(chunk)
                    written += len(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
            if total is not None and done < total:
                raise IncompleteError(f"GET {_where(url)}: truncated at {done} of {total} bytes")
            if progress is not None and written == 0:
                progress(done, total)
    except (RefusedError, RateLimitedError, StoppedError, IncompleteError, _RequestError):
        raise
    except CONNECTION_ERRORS as error:
        msg = f"GET {_where(request.url)}: {type(error).__name__}: {error}"
        raise IncompleteError(_scrub(msg, request)) from error
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        msg = f"GET {_where(request.url)}: {type(error).__name__}: {error}"
        raise _RequestError(_scrub(msg, request)) from error


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


_default: httpx.Client | None = None
_client_lock = threading.Lock()


def _close_default() -> None:
    global _default
    with _client_lock:
        if _default is not None:
            _default.close()
            _default = None


def _client() -> httpx.Client:
    global _default
    with _client_lock:
        if _default is None:
            refuse_all = http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
            _default = httpx.Client(
                follow_redirects=True, cookies=http.cookiejar.CookieJar(refuse_all),
            )
            atexit.register(_close_default)
        return _default


def _send(client: httpx.Client, request: httpx.Request) -> Response:
    response = client.send(request)
    try:
        response.read()
    finally:
        response.close()
    return Response(
        status=response.status_code, url=str(response.url),
        headers=response.headers, content=response.content, _raw=response,
    )


def _checked(response: Response, method: str, *, soft: bool) -> Response | None:
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


def _where(url: httpx.URL | str) -> str:
    try:
        parsed = httpx.URL(str(url))
    except httpx.InvalidURL:
        return str(url).split("?", 1)[0].split("#", 1)[0]
    return str(parsed.copy_with(query=None, fragment=None))


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
