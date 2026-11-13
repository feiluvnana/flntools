"""HTTP for scripts: `http.fetch(url)` gives a `Response`, and `http.download(url, dest)` saves a file.

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
`site.fetch`) returns the same type from other libraries. This module wraps httpx; the `http`
extra brings it, and parsel for `Response.html`.

`download` streams one file into a hidden `.<name>.<random>.part` beside `dest`, checks its
size and then the caller's `check`, and renames it onto `dest` through `atomic`. A server that
sends an `ETag` or a `Last-Modified` makes the part resumable: its small `.part.json` (the URL,
that validator, the size and the ranges written) is kept through a dropped connection, a Ctrl-C
or a crash, and the next attempt, or the next call for the same `dest`, asks only for the rest,
with `Range` and `If-Range`. A file of 16 MB or more from a server that takes ranges is fetched
in up to 4 ranges at once, into one preallocated part, using only the host's free slots.

    http.download("https://cdn.example/v.mp4", "videos/")               # videos/v.mp4
    http.download([mirror_a, mirror_b], "f.7z", client=client, progress=bar.update,
                  check=lambda path: media.open(path).check())

No message or log line here holds a cookie or an `Authorization` value: messages name the method,
the URL without its query, and the status, and any text from httpx is scrubbed of the request's
secret header values first.

Threads: `fetch` and `download` may be called from many threads at once, sharing the module's client
or one of the caller's (httpx's `Client` is safe to share between threads). The per-host slots are one
`threading.BoundedSemaphore` per host, made under a lock, so every thread sees the same 6.
"""

from __future__ import annotations

import email.utils
import http.cookiejar
import io
import json as jsonlib
import os
import re
import secrets
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Iterator, Literal, Mapping, Sequence, Union, overload
from urllib.parse import unquote, urlsplit

import shared
from shared import _atomic as atomic
from shared import _clock as clock
from shared import _fmt as fmt
from shared._log import log

try:
    import httpx
except ImportError:
    raise ImportError("shared.http needs httpx: pip install 'flntools[http]'") from None

if TYPE_CHECKING:  # `selector` loads on the first `html`, so a JSON-only script never imports parsel
    from shared._selector import Selection

__all__ = [
    "ChangedError", "Error", "IncompleteError", "RateLimitedError", "RefusedError", "Response",
    "ServerError", "download", "fetch",
]

PathLike = Union[str, "os.PathLike[str]"]

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

# A file this big or bigger is split into ranges with `parts="auto"` (redesign B6): below it, the
# second connection's setup costs about what it saves, and CDNs cap speed per connection mostly
# for big videos and archives.
SPLIT_MIN = 16 * 1024 * 1024
# The most ranges `parts="auto"` splits one file into, and only within the host's free slots.
MAX_PARTS = 4
# How many bytes a split download writes between two saves of its ranges into `.part.json`; a
# crash loses at most this much of each range, which the next call fetches again.
SAVE_EVERY = 4 * 1024 * 1024
# The request extension every request of `download` carries, so a caller's transport can tell a
# download's requests from others; flnd's sends them once, since `download` retries on its own.
DOWNLOAD_EXTENSION = "shared.http.download"
# What a resumable part's sidecar adds to the part's name.
INFO_SUFFIX = ".json"
# The name of a file `download` saves into a folder when its URL's path names none.
FALLBACK_NAME = "download"

class Error(shared.Error):
    """The base of every error `shared.http` raises; also what a failed connection raises."""


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
    """A download whose body ended before its `Content-Length`, or whose file isn't the size it should be.

    `http.download` tries again, resuming, up to its `attempts`, and raises it only after the last.
    """


class ChangedError(Error, shared.BrokenError):
    """A file that changed on the server during a resume, and whose restart failed too.

    `http.download` restarts a file the server says changed (a 200 to a `Range`, or a 416); this
    is raised when that restart then fails on every attempt left, chained from the last error.
    """


@dataclass(frozen=True, eq=False)
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

    Two responses are equal only when they are the same object, and a response hashes by identity,
    so it can be a dict key or a set member. The headers are an unhashable mapping, so a field-by-field
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
    # Built once, so every attempt sends the same method, URL, headers and body.
    sender = client if client is not None else _client()
    request = sender.build_request(method, url, headers=headers, params=params, data=data, json=json,
                                   timeout=timeout)
    # One pass per attempt: a dead connection or a 5xx waits and goes round again; anything else returns or raises.
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
        clock.sleep(seconds)
        attempt += 1


def download(url: str | Sequence[str], dest: PathLike, *, client: httpx.Client | None = None,
             headers: Mapping[str, str] | None = None, size: int | None = None,
             parts: Literal["auto"] | int = "auto", resume: bool = True, attempts: int = 3,
             timeout: float = 30.0, check: Callable[[Path], Any] | None = None,
             progress: Callable[[int, int | None], Any] | None = None, label: str | None = None) -> Path:
    """Save one file from the web, resuming and splitting where the server allows, and return its path.

        http.download("https://cdn.example/v.mp4", "videos/")                # videos/v.mp4
        http.download([mirror_a, mirror_b], "f.7z", client=client, progress=bar.update,
                      check=lambda path: media.open(path).check())

    Args:
        url: the file's URL, or mirror URLs tried in order: a mirror that fails for any reason
            but a 429 is logged at debug level and the next one tried.
        dest: the file to write, replaced if it exists; or a folder (one that exists, or a path
            ending in "/"), and the name is then the URL's last path part, made safe by
            `fmt.filename`. Missing folders are made.
        client: an `httpx.Client` the caller owns, with its cookies, headers, signing and
            pacing; None uses this module's client, which follows redirects and sends no cookies.
            Every request carries the `"shared.http.download"` extension, so the client's
            transport can tell a download's requests from others.
        headers: headers for every request, over the client's own, such as a `Referer`.
        size: the file's size in bytes when the caller knows it: the progress total until the
            server says, and the size checked when the server sends no length. None when unknown.
        parts: "auto" splits a file of 16 MB or more into up to 4 ranges; 1 never splits; a
            number n splits any file into up to n ranges (at most 6). Only a file whose server
            sends `Accept-Ranges: bytes` and a length is split, and only into the host's free slots.
        resume: True keeps a resumable part (one whose server sent a strong `ETag` or a
            `Last-Modified`) and its `.part.json` through any failure, Ctrl-C included, and
            resumes an earlier call's part for the same `dest`, whatever its URL was then (the
            validator, sent as `If-Range`, says whether it is still the same file). False starts
            afresh each time and leaves no part behind.
        attempts: how many tries in all for a dead connection, a 5xx, a body cut short, or a
            `check` that raises `shared.BrokenError`, at least 1; each try resumes where the
            last stopped. Waits between them are 1 s, 2 s, 4 s, … up to 8 s.
        timeout: seconds each request may wait to connect, and between bytes read.
        check: called with the complete part's path before it is renamed onto `dest`, such as
            a media check; raising `shared.BrokenError` (a damaged file) costs an attempt and
            downloads it again from the start, and any other exception is raised at once. When
            it returns a `Path`, the part is renamed onto that path instead of `dest`, in the one
            rename (a file named by its content: `a.jpg` holding a PNG saved as `a.png`); the
            path must be in `dest`'s folder. None checks only the size.
        progress: called as `progress(done, total)` in bytes as the file arrives, `total` None
            while unknown; never from two threads at once. What it raises stops the download
            and is raised, keeping a resumable part, so a Ctrl-C there can stop it.
        label: the text of a progress row this function draws itself, such as the file's name;
            None draws none.

    Returns:
        The path written: `dest`, the file in the `dest` folder, or the path `check` returned.

    Raises:
        RateLimitedError: a 429, at once and never slept on, with the `Retry-After` seconds; a
            resumable part is kept, so calling again resumes.
        RefusedError: any other 4xx from the last mirror, such as an expired link's 403; a
            resumable part is kept, so a call with a fresh link resumes.
        ServerError: a 5xx from the last mirror on every attempt.
        IncompleteError: the last mirror's body still ended early after every attempt.
        ChangedError: the file changed on the server during a resume, and its restart failed.
        Error: the last mirror's connection failed on every attempt, or a request couldn't be
            sent (an invalid URL, too many redirects).
        ValueError: no URL, `attempts` below 1, or `parts` neither "auto" nor a number from 1.
        Whatever `check` or `progress` raised.

    It may be called from many threads at once. Every request waits for one of its host's 6 slots
    and holds it until its body is read, and the ranges of a split file take free slots only. Two
    calls for one `dest` in this process never share a part; two processes might, so don't.
    """
    mirrors = [url] if isinstance(url, str) else list(url)
    if not mirrors:
        raise ValueError("download needs a URL")
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, not {attempts}")
    if parts != "auto" and (isinstance(parts, bool) or not isinstance(parts, int) or parts < 1):
        raise ValueError(f'parts must be "auto" or a number from 1, not {parts!r}')
    # The name is settled from the first mirror's URL, so every mirror writes the same file.
    path = _destination(dest, mirrors[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    sender = client if client is not None else _client()
    # The progress row and the claim on `path` last for the whole call, across every mirror.
    with ExitStack() as stack:
        bar = stack.enter_context(log.progress(label, total=size, show=fmt.size)) if label is not None else None
        taken = stack.enter_context(_claimed(path))
        for number, mirror in enumerate(mirrors, start=1):
            transfer = _Transfer(sender, mirror, path, headers=headers, size=size, parts=parts,
                                 resume=resume and not taken, attempts=attempts, timeout=timeout,
                                 check=check, report=_Report(bar, progress))
            try:
                return transfer.run()
            except RateLimitedError:  # the caller's back-off decides; another mirror is likely the same site
                raise
            except Exception as error:  # any other failure of a mirror that isn't the last: the next one may work
                if number == len(mirrors):
                    raise
                log.debug("mirror {n} of {name} failed: {error}; trying the next", n=number, name=path.name,
                          error=error)
    raise AssertionError("unreachable: the last mirror returns or raises")


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
        raise RefusedError(status, f"{where}: HTTP {status}", response=response)
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
    return max(0.0, when.timestamp() - clock.wall())


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


# `download`'s internals. A transfer is one mirror of one call. Its state is a part file and a
# list of segments, each a byte range of the file and how much of it is written: one segment
# for the whole file unless it is split. A resumable state is saved beside the part as its
# `.part.json`, so a later attempt, or a later call for the same `dest`, picks it up.

# The destinations a `download` in this process is writing, so two calls for one file never
# write into the same resumable part; under `_claims_lock`, since any thread may download.
_claims: set[str] = set()
_claims_lock = threading.Lock()


@dataclass
class _Segment:
    """One byte range of a file being downloaded: `start` to `end` (exclusive; None to the end), `done` written."""

    # The first byte of the range.
    start: int
    # One past the last byte of the range; None when the server gave no length, so it runs to the end.
    end: int | None
    # Bytes of the range written so far, counted from `start`.
    done: int = 0

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

    # The part file being written, a hidden `.<name>.<random>.part` beside `dest`.
    part: Path
    # The URL it came from; the sidecar keeps it without its query.
    url: str
    # The strong `ETag` or the `Last-Modified` the server sent, sent back as `If-Range`; None
    # when it sent neither, and then the part can't be resumed.
    validator: str | None
    # The file's length from the server; None when it didn't say.
    size: int | None
    # The byte ranges of the file, one unless it is split, each with how much of it is written.
    segments: list[_Segment]

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
                "segments": [[segment.start, segment.end, segment.done] for segment in self.segments]}
        atomic.write(self.info, jsonlib.dumps(data).encode("utf-8"))

    def drop(self) -> None:
        """Delete the part and its sidecar."""
        self.part.unlink(missing_ok=True)
        self.info.unlink(missing_ok=True)


class _Changed(Exception):
    """The server's file isn't the one the part holds: a 416, a range that starts elsewhere, or a range sent whole."""


class _Stopped(Exception):
    """A segment stopped because another one failed; that one's error is what's raised."""


@dataclass
class _Stream:
    """One open response of a download and the host slot it holds, both released by `close`."""

    # The open response, whose body is read as it arrives.
    response: httpx.Response
    # The host slot this stream holds; released even when closing the response fails.
    slot: threading.BoundedSemaphore

    def close(self) -> None:
        """Close the response and give the host slot back."""
        try:
            self.response.close()
        finally:
            self.slot.release()


class _Report:
    """Progress for one download, to the caller's callback and to the row `label` draws."""

    def __init__(self, bar: Any, progress: Callable[[int, int | None], Any] | None):
        self.bar, self.progress = bar, progress
        # The ranges of a split file report from their own threads; one at a time keeps the
        # callback from running twice at once and `done` from going back.
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
    """One mirror of one `download`: its attempts, its part, and its ranges; see `download`."""

    def __init__(self, client: httpx.Client, url: str, dest: Path, *, headers: Mapping[str, str] | None,
                 size: int | None, parts: Literal["auto"] | int, resume: bool, attempts: int, timeout: float,
                 check: Callable[[Path], Any] | None, report: _Report):
        """One mirror's transfer, with the options `download` was given; nothing is requested until `run`.

        Args:
            client: the client every request of this mirror is sent on.
            url: this mirror's URL.
            dest: the file to write, as `download` resolved it from its `dest`.
            headers: headers for every request, over the client's own; None for none.
            size: the file's size in bytes when the caller knows it, else None.
            parts: "auto", 1, or a number of ranges, as `download` documents.
            resume: whether a resumable part may be kept and resumed.
            attempts: how many tries in all, at least 1.
            timeout: seconds each request may wait to connect, and between bytes.
            check: the caller's check on the complete part, or None.
            report: the progress sink shared by every mirror of the call.
        """
        self.client, self.url, self.dest = client, url, dest
        self.headers = dict(headers or {})
        self.size, self.parts, self.resume, self.attempts, self.timeout = size, parts, resume, attempts, timeout
        self.check, self.report = check, report
        self.state: _State | None = None
        # Where the whole file goes: `dest`, or the path `check` named.
        self.named = dest
        # Set once the server's file turned out to differ from the part's, so the part restarted.
        self.restarted = False
        # Set after a split went wrong: later attempts fetch the file in one range.
        self.single = False
        # Host slots taken for a fresh split's extra ranges, handed to them as they start.
        self.reserved: list[threading.BoundedSemaphore] = []

    def run(self) -> Path:
        """Every attempt until the file is whole and checked, then the rename; `download` says what raises."""
        if self.resume:
            self.state = _resumable(self.dest)
        attempt = 1
        while True:
            try:
                self._transfer()
                self._verify()
                break
            except _Changed as error:
                # The part is useless now; the next attempt starts the file again, in one range.
                self._restart()
                failure: Exception = Error(f"GET {_where(self.url)}: {error}")
            except CONNECTION_ERRORS as error:
                self._keep()
                failure = Error(self._scrub(f"GET {_where(self.url)}: {type(error).__name__}: {error}"))
                failure.__cause__ = error
            except (ServerError, IncompleteError) as error:
                self._keep()
                failure = error
            except shared.BrokenError as error:
                # `check` found the file damaged (`_verify` deleted it): fetched again from the start.
                failure = error
            except httpx.HTTPError as error:  # an invalid URL, too many redirects: another try fails the same way
                self._keep()
                raise Error(self._scrub(f"GET {_where(self.url)}: {type(error).__name__}: {error}")) from error
            except BaseException:
                # A 429 or another 4xx, a check that refused the file, the caller's progress, or
                # Ctrl-C: what was written stays resumable, unless `_verify` already deleted it.
                self._keep()
                raise
            if attempt >= self.attempts:
                if self.restarted:
                    raise ChangedError(f"GET {_where(self.url)}: the file changed during a resume, and its "
                                       f"restart failed: {failure}") from failure
                raise failure
            seconds = min(FIRST_WAIT * 2 ** (attempt - 1), MAX_WAIT)
            log.debug("retry {n} of {url} in {seconds:g} s: {reason}", n=attempt, url=_where(self.url),
                      seconds=seconds, reason=failure)
            clock.sleep(seconds)
            attempt += 1
        state = self.state
        assert state is not None
        # Through `atomic`, whose part moves onto the file's name in one step: a reader sees the
        # old file or the whole new one. The resumable part is moved into it first, which is one
        # rename too and leaves it a part: nothing appears under a final name before it is right.
        with atomic.writing(self.named, replace=True) as final:
            os.replace(state.part, final)
        state.info.unlink(missing_ok=True)
        return self.named

    # One attempt.

    def _transfer(self) -> None:
        """Fetch what the part still lacks: the whole file on a fresh start, or each unfinished range."""
        if self.state is None:
            stream = self._open(None)
            self._start(stream, split=not self.single)
        else:
            first = next((segment for segment in self.state.segments if not segment.finished), None)
            if first is None:
                return
            stream = self._open(first)
            if stream.response.status_code == 200:
                # A 200 to a range: the file changed (`If-Range` didn't match) or the server
                # ignores ranges. Either way the body is the whole file, so the part restarts on it.
                log.debug("{name}: not the file the part holds; starting again", name=self.dest.name)
                self._drop()
                self.restarted = True
                self._start(stream, split=False)
        state = self.state
        assert state is not None
        first = next(segment for segment in state.segments if not segment.finished)
        try:
            self.report(state.done, self._total())
        except BaseException:
            # The caller's progress raised (a Ctrl-C, say) before `_fill_all` owns the stream:
            # its host slot, and any reserved for ranges, go back here.
            stream.close()
            self._release()
            raise
        self._fill_all(first, stream)

    def _start(self, stream: _Stream, *, split: bool) -> None:
        """A new part for a whole-file response, split into ranges when the server and the host's free slots allow."""
        response = stream.response
        try:
            encoded = response.headers.get("content-encoding", "identity").lower() != "identity"
            length = response.headers.get("content-length", "")
            # An encoded body's length counts encoded bytes, and the file holds decoded ones; its
            # ranges are of the encoded bytes too, so such a body is neither checked nor resumed.
            total = int(length) if length.isdigit() and not encoded else None
            validator = None if encoded else _validator(response.headers)
            part = self.dest.with_name(f".{self.dest.name}.{secrets.token_hex(4)}.part")
            # Only a file with a validator is split: each range is asked for with `If-Range`, so a
            # file that changes between two ranges is never put together from two versions.
            count = self._split_count(response, total) if split and validator is not None else 1
            with _open_part(part, create=True) as file:
                if count > 1 and total is not None:
                    # Every range writes at its own offset into one file of the full length.
                    file.truncate(total)
            if count > 1 and total is not None:
                # Ceiling division, so equal ranges cover every byte; the last one may be shorter.
                step = -(-total // count)
                segments = [_Segment(start, min(start + step, total)) for start in range(0, total, step)]
            else:
                segments = [_Segment(0, total)]
            self.state = _State(part, self.url, validator, total, segments)
            if self._resumable():
                self.state.save()
        except BaseException:
            stream.close()
            self._release()
            raise

    def _split_count(self, response: httpx.Response, total: int | None) -> int:
        """How many ranges a fresh file is fetched in, reserving a free host slot for each past the first."""
        if self.parts == 1 or total is None or response.headers.get("accept-ranges", "").lower() != "bytes":
            return 1
        if isinstance(self.parts, int):
            wanted = min(self.parts, MAX_PER_HOST, total)
        else:  # "auto"
            wanted = MAX_PARTS if total >= SPLIT_MIN else 1
        # The slot of the URL asked for, as `_open` takes it, even when the answer came from a redirect.
        slot = _slot(httpx.URL(self.url))
        # Free slots only: a split never makes another download, or a fetch, wait for its host.
        while len(self.reserved) < wanted - 1 and slot.acquire(blocking=False):
            self.reserved.append(slot)
        return 1 + len(self.reserved)

    def _fill_all(self, first: _Segment, stream: _Stream) -> None:
        """Write every unfinished segment: `first` from `stream` here, the others on free host slots, or after it."""
        state = self.state
        assert state is not None
        helpers: list[tuple[_Segment, threading.BoundedSemaphore]] = []
        serial: list[_Segment] = []
        for segment in state.segments:
            if segment is first or segment.finished:
                continue
            slot = self.reserved.pop() if self.reserved else None
            if slot is None:
                free = _slot(httpx.URL(self.url))
                slot = free if free.acquire(blocking=False) else None
            if slot is None:
                serial.append(segment)
            else:
                helpers.append((segment, slot))
        self._release()
        # When one segment fails, the others stop at their next chunk, and the first failure is raised.
        abort = threading.Event()
        failures: list[BaseException] = []
        # Ranges save the sidecar from their own threads; one at a time.
        saving = threading.Lock()

        def fill(segment: _Segment, given: _Stream | None, slot: threading.BoundedSemaphore | None) -> None:
            try:
                self._fill(segment, given, slot, abort, saving)
            except _Stopped:
                pass
            except BaseException as error:  # handed to the calling thread, which raises the first one
                failures.append(error)
                abort.set()

        threads = [threading.Thread(target=fill, args=(segment, None, slot), name="shared-http-range", daemon=True)
                   for segment, slot in helpers]
        for thread in threads:
            thread.start()
        try:
            fill(first, stream, None)
            for segment in serial:
                if abort.is_set():
                    break
                fill(segment, None, None)
        except BaseException:
            # Only a Ctrl-C between two ranges lands here; the ranges still running stop too.
            abort.set()
            raise
        finally:
            for thread in threads:
                thread.join()
            if len(state.segments) > 1 and self._resumable():
                state.save()
        if failures:
            raise failures[0]

    def _fill(self, segment: _Segment, stream: _Stream | None, slot: threading.BoundedSemaphore | None,
              abort: threading.Event, saving: threading.Lock) -> None:
        """Write one segment from `stream`, or from a range request of its own, up to its end."""
        state = self.state
        assert state is not None
        if stream is None:
            stream = self._open(segment, slot=slot)
            if stream.response.status_code != 206:
                stream.close()
                raise _Changed("a range came back as the whole file")
        try:
            unsaved = 0
            with _open_part(state.part) as file:
                file.seek(segment.at)
                # As the bytes arrive, without a chunk size: httpx would hold back a partial
                # chunk, and lose it when the connection drops, which is when it matters.
                for chunk in stream.response.iter_bytes():
                    if abort.is_set():
                        raise _Stopped()
                    if segment.end is not None:
                        # The first range of a fresh split reads the whole file's body, up to its own end.
                        chunk = chunk[: segment.end - segment.at]
                    _write_all(file, chunk)
                    # Counted only now: the part's handle has no buffer, so these bytes are the
                    # system's already, and a sidecar that any range saves from here on never
                    # claims a byte a crash, even kill -9, could lose (a power cut still could).
                    segment.done += len(chunk)
                    unsaved += len(chunk)
                    self.report(state.done, self._total())
                    if len(state.segments) > 1 and unsaved >= SAVE_EVERY and self._resumable():
                        with saving:
                            state.save()
                        unsaved = 0
                    if segment.finished:
                        break
            if segment.end is None:
                # No length from the server: the body ending is the file ending.
                segment.end = segment.at
            elif not segment.finished:
                raise IncompleteError(f"GET {_where(self.url)}: got {state.done} of {state.size} bytes")
        finally:
            stream.close()

    def _open(self, segment: _Segment | None, *, slot: threading.BoundedSemaphore | None = None) -> _Stream:
        """Send one GET for the whole file (`segment` None) or for the rest of `segment`, and check its status.

        The stream holds a host slot (`slot`, or one it waits for) until it is closed. A 200 to a
        range is returned for the caller to decide; a 206 that starts elsewhere, or a 416, raises
        `_Changed`.
        """
        # `identity`: a compressed body's bytes and ranges aren't the file's, so it couldn't resume.
        headers = {"Accept-Encoding": "identity", **self.headers}
        state = self.state
        if segment is not None and state is not None:
            # Open-ended (`bytes=<n>-`) up to the file's end, as a resume asks; a split's inner
            # ranges name their last byte.
            last = "" if segment.end is None or segment.end == state.size else str(segment.end - 1)
            headers["Range"] = f"bytes={segment.at}-{last}"
            if state.validator is not None:
                headers["If-Range"] = state.validator
        request = self.client.build_request("GET", self.url, headers=headers, timeout=self.timeout,
                                            extensions={DOWNLOAD_EXTENSION: True})
        if slot is None:
            slot = _slot(request.url)
            slot.acquire()
        try:
            response = self.client.send(request, stream=True)
        except BaseException:
            slot.release()
            raise
        stream = _Stream(response, slot)
        try:
            self._checked(response, request, segment)
        except BaseException:
            stream.close()
            raise
        return stream

    def _checked(self, response: httpx.Response, request: httpx.Request, segment: _Segment | None) -> None:
        """Raise for a status that isn't the file, or a range that isn't the one asked for; see `_open`."""
        status = response.status_code
        where = f"GET {_where(request.url)}"
        if status == 429:
            retry_after = _retry_after(response.headers.get("retry-after"))
            wait = "" if retry_after is None else f", retry after {retry_after:g} s"
            raise RateLimitedError(retry_after, f"{where}: HTTP 429, rate limited{wait}")
        if status == 416 and segment is not None:
            raise _Changed("the server has fewer bytes than the part (HTTP 416)")
        if status >= 500:
            raise ServerError(status, f"{where}: HTTP {status}")
        if status >= 400:
            raise RefusedError(status, f"{where}: HTTP {status}")
        if status not in (200, 206):
            raise Error(f"{where}: HTTP {status}, not a file")
        if status == 206:
            start = _range_start(response.headers.get("content-range", ""))
            wanted = segment.at if segment is not None else 0
            if start != wanted:
                raise _Changed(f"asked for byte {wanted}, got {start}")

    def _verify(self) -> None:
        """The size check, then the caller's `check`, on the complete part."""
        state = self.state
        assert state is not None
        got = state.part.stat().st_size
        expected = state.size if state.size is not None else self.size
        if expected is not None and got != expected:
            # Every range is written, so the bytes that are there are all the body had: only a
            # fresh download can mend it.
            self._drop()
            raise IncompleteError(f"GET {_where(self.url)}: got {got} of {expected} bytes")
        self.named = self.dest
        if self.check is not None:
            try:
                named = self.check(state.part)
                if isinstance(named, Path):
                    self.named = named
            except BaseException:
                # The bytes are all there, so a resume would give the same file: only a new one can pass.
                self._drop()
                raise

    # The part between attempts.

    def _resumable(self) -> bool:
        """Whether the part may be kept for a later attempt or call: `resume`, and a validator to check it with."""
        return self.resume and self.state is not None and self.state.validator is not None

    def _keep(self) -> None:
        """After a failure: save a resumable part's progress for later, or delete a part that can't resume."""
        self._release()
        if self.state is None:
            return
        if not self._resumable():
            self._drop()
            return
        segments = self.state.segments
        if len(segments) == 1 and self.state.part.exists():
            # One range is written in order, so the bytes on disk are exactly what arrived.
            segments[0].done = self.state.part.stat().st_size - segments[0].start
        self.state.save()

    def _drop(self) -> None:
        """Delete the part and its sidecar, so the next attempt starts afresh."""
        if self.state is not None:
            self.state.drop()
            self.state = None

    def _restart(self) -> None:
        """Start again after `_Changed`: no part, and one range."""
        self._release()
        self._drop()
        self.restarted = True
        self.single = True

    def _release(self) -> None:
        """Give back the slots reserved for ranges that never started."""
        while self.reserved:
            self.reserved.pop().release()

    def _total(self) -> int | None:
        """The progress total: the server's length, else the caller's `size`."""
        state = self.state
        return state.size if state is not None and state.size is not None else self.size

    def _scrub(self, text: str) -> str:
        """`text` with the caller's secret header values replaced by `***`."""
        return _scrub(text, self.client.build_request("GET", self.url, headers=self.headers))


def _destination(dest: PathLike, url: str) -> Path:
    """`dest`, or the file in the folder `dest` names, named from `url`'s last path part."""
    path = Path(dest)
    if not (path.is_dir() or str(dest).endswith(("/", os.sep))):
        return path
    name = unquote(urlsplit(url).path.rstrip("/").rpartition("/")[2])
    return path / fmt.filename(name, fallback=FALLBACK_NAME)


@contextmanager
def _claimed(dest: Path) -> Iterator[bool]:
    """Claim `dest` for one call, yielding True when another call in this process holds it already."""
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
    """The newest part an earlier call left for `dest`, read from its sidecar; broken leftovers are deleted, or None."""
    # Exactly `shared.atomic`'s `.<name>.<8 hex digits>.part` and the sidecar's suffix: a prefix
    # alone would let `a.rar` adopt `.a.rar.001.<hex>.part`, a sibling's part, and delete it.
    own = re.compile(re.escape(f".{dest.name}.") + r"[0-9a-f]{8}" + re.escape(f".part{INFO_SUFFIX}"))
    try:
        names = [name for name in os.listdir(dest.parent) if own.fullmatch(name)]
    except OSError:  # no folder, or one that can't be read: nothing to resume
        return None
    infos = sorted((dest.parent / name for name in names), key=_modified, reverse=True)
    for info in infos:
        part = info.with_name(info.name[: -len(INFO_SUFFIX)])
        state: _State | None
        try:
            data = jsonlib.loads(info.read_text(encoding="utf-8"))
            segments = [_Segment(int(start), None if end is None else int(end), int(done))
                        for start, end, done in data["segments"]]
            state = _State(part, str(data["url"]), data["validator"], data["size"], segments)
        except (OSError, ValueError, KeyError, TypeError):  # a sidecar cut short, or not one of ours: not trusted
            state = None
        if state is None or not state.validator or not state.segments or not part.is_file():
            info.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            continue
        if len(state.segments) == 1:
            # One range is written in order, so the bytes on disk are what arrived, even after a crash.
            state.segments[0].done = part.stat().st_size - state.segments[0].start
        return state
    return None


def _modified(path: Path) -> float:
    """When `path` was last written; 0 for one that is gone, which then sorts last."""
    try:
        return path.stat().st_mtime
    except OSError:  # deleted since it was listed, by another call or a repair
        return 0.0


def _validator(headers: httpx.Headers) -> str | None:
    """What `If-Range` sends back: a strong `ETag`, else `Last-Modified`; None when there is neither."""
    etag = headers.get("etag", "")
    # A weak ETag (W/"…") may not be used in If-Range (RFC 9110, 13.1.5).
    if etag and not etag.startswith("W/"):
        return etag
    return headers.get("last-modified") or None


def _range_start(content_range: str) -> int | None:
    """The first byte a `Content-Range: bytes <a>-<b>/<total>` names, or None when it doesn't parse."""
    unit, _, rest = content_range.partition(" ")
    first = rest.partition("-")[0]
    return int(first) if unit.lower() == "bytes" and first.isdigit() else None


def _write_all(file: io.FileIO, data: bytes) -> None:
    """Write every byte of `data`; an unbuffered handle may take fewer at a time."""
    view = memoryview(data)
    while view:
        written = file.write(view)
        view = view[written or 0:]


def _open_part(part: Path, *, create: bool = False) -> io.FileIO:
    """Open a download's part for unbuffered writing: create it (it must be new), or write into it in place.

    Unbuffered, so a write that returned is the system's: the ranges of a split file record
    what they wrote in one shared `.part.json`, and a buffer in one range's handle would let
    another range's save claim bytes a crash then loses, leaving a hole of zeros.
    """
    # O_EXCL: a new part's name is fresh, so a file already there is another writer's. Without
    # O_TRUNC an existing part keeps its bytes, and each range seeks to its own place. Windows
    # opens files in text mode without O_BINARY, and would write every \n as \r\n.
    flags = os.O_WRONLY | (os.O_CREAT | os.O_EXCL if create else 0) | getattr(os, "O_BINARY", 0)
    # 0o644: the owner writes and others only read; the process's umask may narrow that further.
    handle = os.open(part, flags, 0o644)
    try:
        return os.fdopen(handle, "wb", buffering=0)
    # Until fdopen owns the handle, it's ours to close: an open handle keeps Windows from deleting the part.
    except BaseException:
        os.close(handle)
        raise
