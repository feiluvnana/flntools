"""One HTTP request at a time: `http.fetch(url)` gives a `Response`, or raises a typed error.

How it works: `fetch` sends one request through httpx, on the caller's `httpx.Client` or on a
module-level one made on first use, and reads the whole body. Each attempt first takes one of the
6 slots its host has in this process, and gives it back as soon as the body is read, so no more
than 6 requests to one host are ever in flight, whichever thread or client sends them. A dead
connection or a 5xx is sent again, up to `attempts` tries in all, after a short wait; a 429 is
raised at once as `RateLimitedError`, never slept on, so the caller's back-off decides how long
to wait. Any other 4xx raises `RefusedError`, or with `soft=True` a 401, 403 or 404 returns
None. What a request carries (cookies, signing, pacing, back-off) is left to the caller's client.

    from shared import http

    page = http.fetch("https://example.com/items", params={"page": 2})
    page.status, page.headers["content-type"], page.json["items"]
    page.html.css("a::attr(href)").all
    if (profile := http.fetch("https://example.com/example_user", soft=True)) is None:
        ...                                     # a 401, 403 or 404

    with httpx.Client(cookies=jar, headers={"User-Agent": "..."}) as client:
        http.fetch("https://example.com/api", client=client, method="POST", json={"id": 1})

A `Response` is a frozen value made from the body already read: it holds no connection, so
there is nothing to close, and it has no `.raw`, since `browser.Page.fetch` (and later flnd's
`site.fetch`) returns the same type from other libraries. This module wraps httpx; the `http` extra brings it,
and parsel for `Response.html`.

No message or log line here holds a cookie or an `Authorization` value: messages name the method,
the URL without its query, and the status, and any text from httpx is scrubbed of the request's
secret header values first.

Threads: `fetch` may be called from many threads at once, sharing the module's client or one of
the caller's (httpx's `Client` is safe to share between threads). The per-host slots are one
`threading.BoundedSemaphore` per host, made under a lock, so every thread sees the same 6.
"""

from __future__ import annotations

import email.utils
import http.cookiejar
import json as jsonlib
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal, Mapping, overload

import shared
from shared._log import log

try:
    import httpx
except ImportError:
    raise ImportError("shared.http needs httpx: pip install 'flntools[http]'") from None

if TYPE_CHECKING:  # `selector` loads on the first `html`, so a JSON-only script never imports parsel
    from shared._selector import Selection

__all__ = [
    "ChangedError", "Error", "IncompleteError", "RateLimitedError", "RefusedError", "Response",
    "ServerError", "fetch",
]

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

# Waits sleep through this, so tests can replace it.
_sleep: Callable[[float], None] = time.sleep


class Error(shared.Error):
    """The base of every error `shared.http` raises; also what a failed connection raises."""


class RefusedError(Error, shared.RefusedError):
    """A 4xx other than 429: the site said no.

    Args:
        status: the HTTP status, such as 403.
        message: the text shown; names the method, the URL without its query and the status.
    """

    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"HTTP {status}")
        self.status = status


class RateLimitedError(Error):
    """A 429: the site asks to slow down. Raised at once, never slept on, so the caller backs off.

    Not a `RefusedError`, so `except http.RefusedError` doesn't swallow a rate limit by mistake.

    Args:
        retry_after: the seconds the site's `Retry-After` header asks to wait, or None without one.
        message: the text shown.
    """

    def __init__(self, retry_after: float | None, message: str = ""):
        super().__init__(message or "HTTP 429")
        self.status = 429
        self.retry_after = retry_after


class ServerError(Error):
    """A 5xx that was still there after every attempt: the site is down or broken for now.

    Args:
        status: the HTTP status, such as 503.
        message: the text shown.
    """

    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"HTTP {status}")
        self.status = status


class IncompleteError(Error, shared.BrokenError):
    """A body that ended before its `Content-Length`. Raised by `http.download`, built in Phase 6 step 8."""


class ChangedError(Error, shared.BrokenError):
    """A file that changed on the server during a resume, and whose restart failed too.

    Raised by `http.download`, built in Phase 6 step 8.
    """


@dataclass(frozen=True)
class Response:
    """One HTTP response with its whole body read: status, URL, headers, and the body as text, JSON or HTML.

        page = http.fetch("https://example.com/items")
        page.ok, page.status, page.url            # True, 200, "https://example.com/items"
        page.headers["Content-Type"]              # names are looked up in any case
        page.json["items"], page.html.css("h1::text").first

    The same type comes from `http.fetch` and `browser.Page.fetch`, and later from flnd's `site.fetch`.
    It never changes once made and holds no connection, so it may be kept and passed between
    threads. `text`, `json` and `html` are worked out on first read and kept, so reading one twice
    costs nothing and a response never fetched as HTML never loads parsel. Two threads reading the
    same one for the first time at once may each work it out; they get equal values.

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _Headers(self.headers))

    @property
    def ok(self) -> bool:
        """Whether the status is 2xx, as Playwright's and fetch()'s `ok` mean it."""
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        """The body as text, in the charset its `Content-Type` names, or UTF-8; bad bytes become U+FFFD."""
        return self._cached("text", lambda: self.content.decode(_charset(self.headers.get("content-type", "")),
                                                                "replace"))

    @property
    def json(self) -> Any:
        """The body parsed as JSON. Raises `http.Error` when it isn't JSON, naming its first characters."""
        return self._cached("json", self._json)

    @property
    def html(self) -> Selection:
        """The body as a `shared.selector` `Selection`, made by `selector.parse(text)`.

        A body that parses as JSON gives a JSON `Selection`, as `parse` does; a page never does.
        Raises `selector.UnsafeError` for a body over 64 MB. Read it from one thread at a time.
        """
        from shared import _selector

        return self._cached("html", lambda: _selector.parse(self.text))

    def _cached(self, name: str, compute: Callable[[], Any]) -> Any:
        """`compute()`, worked out on the first read of `name` and kept in the instance's `__dict__`.

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
            raise Error(f"expected JSON from {_where(self.url)}, got {self.text[:80]!r}: {error}") from None


@overload
def fetch(url: str, *, method: str = ..., client: httpx.Client | None = ...,
          headers: Mapping[str, str] | None = ..., params: Mapping[str, Any] | None = ...,
          data: Any = ..., json: Any = ..., soft: Literal[False] = ..., attempts: int = ...,
          timeout: float = ...) -> Response: ...
@overload
def fetch(url: str, *, method: str = ..., client: httpx.Client | None = ...,
          headers: Mapping[str, str] | None = ..., params: Mapping[str, Any] | None = ...,
          data: Any = ..., json: Any = ..., soft: Literal[True], attempts: int = ...,
          timeout: float = ...) -> Response | None: ...
@overload
def fetch(url: str, *, method: str = ..., client: httpx.Client | None = ...,
          headers: Mapping[str, str] | None = ..., params: Mapping[str, Any] | None = ...,
          data: Any = ..., json: Any = ..., soft: bool, attempts: int = ...,
          timeout: float = ...) -> Response | None: ...
def fetch(url: str, *, method: str = "GET", client: httpx.Client | None = None,
          headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None,
          data: Any = None, json: Any = None, soft: bool = False, attempts: int = 3,
          timeout: float = 30.0) -> Response | None:
    """Send one HTTP request and return its `Response`, retrying a dead connection or a 5xx.

        page = http.fetch("https://example.com/items", params={"page": 2})
        http.fetch("https://example.com/api", method="POST", json={"id": 1}, client=client)
        http.fetch("https://example.com/example_user", soft=True)      # None for 401, 403, 404

    Args:
        url: the URL to request; `params` are added to its query.
        method: the HTTP method, such as "GET" or "POST".
        client: an `httpx.Client` the caller owns and closes, with its cookies, headers, signing
            and pacing; None uses this module's client, which follows redirects and sends no
            cookies.
        headers: headers for this request, over the client's own.
        params: query parameters to add to the URL.
        data: a form body (a dict) or raw bytes; None sends none.
        json: a value to send as a JSON body; None sends none.
        soft: True returns None for a 401, 403 or 404 instead of raising `RefusedError`.
        attempts: how many tries in all for a dead connection or a 5xx, at least 1. Waits between
            them are 1 s, 2 s, 4 s, … up to 8 s.
        timeout: seconds each attempt may wait to connect, and between bytes read or written;
            it overrides the client's own.

    Returns:
        The response of a 1xx, 2xx or 3xx (a client that doesn't follow redirects gets the 3xx),
        or None for a 401, 403 or 404 when `soft` is True.

    Raises:
        RateLimitedError: a 429, at once, with the `Retry-After` seconds.
        RefusedError: any other 4xx, or a 401, 403 or 404 without `soft`.
        ServerError: a 5xx on every attempt.
        Error: the connection failed on every attempt, or the request couldn't be sent at all
            (an invalid URL, too many redirects).
        ValueError: `attempts` is below 1.

    It may be called from many threads at once. Each attempt waits for one of its host's 6
    slots first, which a slow site can make wait for up to the others' `timeout`s.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, not {attempts}")
    sender = client if client is not None else _client()
    request = sender.build_request(method, url, headers=headers, params=params, data=data, json=json,
                                   timeout=timeout)
    attempt = 1
    while True:
        try:
            response = _send(sender, request)
        except CONNECTION_ERRORS as error:
            if attempt >= attempts:
                raise Error(_scrub(f"{request.method} {_where(request.url)}: {type(error).__name__}: {error} "
                                   f"(after {attempts} attempts)", request)) from error
            reason = type(error).__name__
        except httpx.HTTPError as error:  # an invalid URL, too many redirects: another try fails the same way
            raise Error(_scrub(f"{request.method} {_where(request.url)}: {type(error).__name__}: {error}",
                               request)) from error
        else:
            if response.status < 500 or attempt >= attempts:
                return _checked(response, request.method, soft=soft)
            reason = f"HTTP {response.status}"
        seconds = min(FIRST_WAIT * 2 ** (attempt - 1), MAX_WAIT)
        log.debug("retry {n} of {url} in {seconds:g} s: {reason}", n=attempt, url=_where(request.url),
                  seconds=seconds, reason=_scrub(reason, request))
        _sleep(seconds)
        attempt += 1


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


def _client() -> httpx.Client:
    """The module's own client, made on first use: it follows redirects and keeps no cookies between calls."""
    global _default
    with _client_lock:
        if _default is None:
            # A jar whose policy allows no domain keeps no cookie, so a cookie one caller's fetch
            # is sent never rides along on another caller's request to the same site.
            refuse_all = http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
            _default = httpx.Client(follow_redirects=True, cookies=http.cookiejar.CookieJar(refuse_all))
        return _default


def _slot(url: httpx.URL) -> threading.BoundedSemaphore:
    """The semaphore that counts requests in flight to `url`'s host, made on first use."""
    key = (url.scheme, url.host, url.port)
    with _slots_lock:
        if key not in _slots:
            _slots[key] = threading.BoundedSemaphore(MAX_PER_HOST)
        return _slots[key]


def _send(client: httpx.Client, request: httpx.Request) -> Response:
    """Send one attempt while holding a slot of its host, and read the whole body into a `Response`."""
    with _slot(request.url):
        response = client.send(request)
        try:
            response.read()
        finally:
            response.close()
    return Response(status=response.status_code, url=str(response.url), headers=response.headers,
                    content=response.content)


def _checked(response: Response, method: str, *, soft: bool) -> Response | None:
    """The response when it is a success or a redirect; None for a soft 401/403/404; an error otherwise."""
    status = response.status
    where = f"{method} {_where(response.url)}"
    if status < 400:
        return response
    if status == 429:
        retry_after = _retry_after(response.headers.get("retry-after"))
        wait = "" if retry_after is None else f", retry after {retry_after:g} s"
        raise RateLimitedError(retry_after, f"{where}: HTTP 429, rate limited{wait}")
    if soft and status in SOFT_STATUSES:
        return None
    if status < 500:
        raise RefusedError(status, f"{where}: HTTP {status}")
    raise ServerError(status, f"{where}: HTTP {status}")


def _retry_after(value: str | None) -> float | None:
    """A `Retry-After` header in seconds: a number of seconds, or an HTTP date from now; None without one."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:  # not a number, so maybe an HTTP date
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):  # neither form: treated as no header, as browsers do
        return None
    return max(0.0, when.timestamp() - time.time())


def _where(url: httpx.URL | str) -> str:
    """The URL without its query or fragment, which may hold a signed token, for a message."""
    url = httpx.URL(str(url))
    return str(url.copy_with(query=None, fragment=None))


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
