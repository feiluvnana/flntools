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
size, the server's digest and then the caller's `check`, and renames it onto `dest` through
`atomic`. The part's small `.part.json` (the URL, the validator, the size and the pieces
written) is kept through a dropped connection, a Ctrl-C or a crash, and the next attempt, or
the next call for the same `dest`, asks only for the rest. A file of 4 MB or more is fetched in
2–4 MB pieces on up to 8 connections (`shared._download` holds how).

    http.download("https://cdn.example/v.mp4", "videos/")               # videos/v.mp4
    http.download([mirror_a, mirror_b], "f.7z", client=client, progress=bar.update,
                  check=lambda path: media.open(path).check())

No message or log line here holds a cookie or an `Authorization` value: messages name the method,
the URL without its query, and the status, and any text from httpx is scrubbed of the request's
secret header values first.

Threads: `fetch` and `download` may be called from many threads at once, sharing the module's client
or one of the caller's (httpx's `Client` is safe to share between threads). `fetch`'s per-host slots
are one `threading.BoundedSemaphore` per host, made under a lock, so every thread sees the same 6.
"""

from __future__ import annotations

import email.utils
import http.cookiejar
import json as jsonlib
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import (TYPE_CHECKING, Any, Callable, Iterator, Literal, Mapping, Sequence, TypeVar,
                    Union, overload)

import shared
from shared import _clock as clock
from shared import _fmt as fmt
from shared._log import log
from shared._pace import EPOCH, Host, Pace

try:
    import httpx
except ImportError:
    raise ImportError("shared.http needs httpx: pip install 'flntools[http]'") from None

if TYPE_CHECKING:  # `selector` loads on the first `html`, so a JSON-only script never imports parsel
    from shared._selector import Selection

__all__ = [
    "ChangedError", "Client", "CorruptError", "Download", "Error", "IncompleteError", "Pace",
    "RateLimitedError", "RefusedError", "Response", "ServerError", "SpaceError", "StoppedError",
    "Transport", "download", "fetch",
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
# The longest `Retry-After` a CDN's 429 is waited out for in `Client.download`; a longer one is
# more likely a misconfigured CDN than a real five-minute ban.
MAX_RETRY_AFTER = 300.0
# Idle connections `Transport` keeps open for reuse: 8 per host across a run's few CDNs.
KEEP_ALIVE = 64
# A client's wait of this many seconds or more is a countdown row: a slow run explains itself.
SHOWN_WAIT = 15.0

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
        retry_after: the seconds the site's `Retry-After` header asks to wait, else until its rate
            headers' reset (`x-rate-limit-reset`, X's), or None without either.
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


class StoppedError(Error):
    """A download stopped because its caller set `stop`; a resumable part is kept for later."""


class IncompleteError(Error, shared.BrokenError):
    """A download whose body ended before its `Content-Length`, or whose file isn't the size it should be.

    `http.download` tries again, resuming, up to its `attempts`, and raises it only after the last.
    """


class CorruptError(Error, shared.BrokenError):
    """A downloaded file whose bytes don't match the digest its server sent.

    `http.download` fetches it again from the start, up to its `attempts`, and raises it only
    after the last.
    """


class SpaceError(Error):
    """A download refused before its first byte: the disk lacks room, or FAT32 can't hold it."""


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


@dataclass(frozen=True)
class Download:
    """A file `download` saved: where it is, the URL it came from, and that response's headers.

        saved = http.download("https://cdn.example/v.mp4", "videos/")
        saved.path, saved.headers.get("last-modified")
    """

    # The path written: `dest`, the file in the `dest` folder, or the path `check` returned.
    path: Path
    # The URL the file came from, after any redirects.
    url: str
    # The headers of the response that started this call's transfer, read-only with lowercase
    # names; empty when an earlier call had already fetched every byte.
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _Headers(self.headers))


@overload
def fetch(url: str, *, method: str = ..., client: httpx.Client | None = ...,
          headers: Mapping[str, str] | None = ..., params: Mapping[str, Any] | None = ...,
          data: Any = ..., json: Any = ..., soft: Literal[False] = ..., attempts: int = ...,
          timeout: float = ..., redirects: bool | None = ...) -> Response: ...
@overload
def fetch(url: str, *, method: str = ..., client: httpx.Client | None = ...,
          headers: Mapping[str, str] | None = ..., params: Mapping[str, Any] | None = ...,
          data: Any = ..., json: Any = ..., soft: Literal[True], attempts: int = ...,
          timeout: float = ..., redirects: bool | None = ...) -> Response | None: ...
@overload
def fetch(url: str, *, method: str = ..., client: httpx.Client | None = ...,
          headers: Mapping[str, str] | None = ..., params: Mapping[str, Any] | None = ...,
          data: Any = ..., json: Any = ..., soft: bool, attempts: int = ...,
          timeout: float = ..., redirects: bool | None = ...) -> Response | None: ...
def fetch(url: str, *, method: str = "GET", client: httpx.Client | None = None,
          headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None,
          data: Any = None, json: Any = None, soft: bool = False, attempts: int = 3,
          timeout: float = 30.0, redirects: bool | None = None) -> Response | None:
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
        redirects: whether to follow redirects; None keeps the client's own choice.

    Returns:
        The response of a 1xx, 2xx or 3xx (without following redirects, the 3xx),
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
            response = _send(sender, request, redirects)
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
             progress: Callable[[int, int | None], Any] | None = None, label: str | None = None,
             stop: threading.Event | None = None) -> Download:
    """Save one file from the web, resuming and splitting where the server allows, as a `Download`.

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
        parts: "auto" fetches a file of 4 MB or more in 2–4 MB pieces on up to 8 connections;
            1 sends one plain request; a number n uses up to n connections (at most 8) on any
            file over one piece. Only a file whose server takes ranges and sends a validator and
            a length is split, and only on its host's free connections.
        resume: True keeps a resumable part and its `.part.json` through any failure, Ctrl-C
            included, and resumes an earlier call's part for the same `dest`, whatever its URL
            was then: with `If-Range` when the server sent a strong `ETag` or a `Last-Modified`,
            else once the part's last 64 bytes, fetched again, match. False starts afresh each
            time and leaves no part behind.
        attempts: how many tries in all for a dead connection, a 5xx, a body cut short, a
            digest that doesn't match, or a `check` that raises `shared.BrokenError`, at least 1;
            each try resumes where the last stopped. The first retry is at once, then 1 s, 2 s,
            4 s, … up to 30 s.
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
        stop: an event that stops the download within a second when set, between two chunks, in
            a wait for a slot or a retry, or by closing a stalled read; a resumable part is kept.

    Raises:
        RateLimitedError: a 429, at once and never slept on, with the `Retry-After` seconds; a
            resumable part is kept, so calling again resumes.
        RefusedError: any other 4xx from the last mirror, such as an expired link's 403; a
            resumable part is kept, so a call with a fresh link resumes.
        ServerError: a 5xx from the last mirror on every attempt.
        IncompleteError: the last mirror's body still ended early after every attempt.
        ChangedError: the file changed on the server during a resume, and its restart failed.
        CorruptError: the file never matched the digest its server sent.
        SpaceError: a file of 4 MB or more that the disk can't hold, before a byte is written.
        Error: the last mirror's connection failed on every attempt, or a request couldn't be
            sent (an invalid URL, too many redirects).
        StoppedError: `stop` was set.
        ValueError: no URL, `attempts` below 1, or `parts` neither "auto" nor a number from 1.
        Whatever `check` or `progress` raised.

    It may be called from many threads at once. A host's downloads share 4 connections, one
    more each time that raised its throughput, up to 8; a split file's extra connections take
    free ones only. Two calls for one `dest` in this process never share a part; two processes
    might, so don't.
    """
    # The downloader loads on the first download, so a script that only fetches never reads it.
    from shared import _download

    mirrors = [url] if isinstance(url, str) else list(url)
    if not mirrors:
        raise ValueError("download needs a URL")
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, not {attempts}")
    if parts != "auto" and (isinstance(parts, bool) or not isinstance(parts, int) or parts < 1):
        raise ValueError(f'parts must be "auto" or a number from 1, not {parts!r}')
    options = _download.Options(
        client if client is not None else _client(), headers=dict(headers or {}), size=size,
        parts=min(parts, MAX_PARTS) if isinstance(parts, int) else parts, resume=resume,
        attempts=attempts, timeout=timeout, check=check, progress=progress, label=label, stop=stop)
    return _download.run(mirrors, dest, options)


class Client:
    """One run's HTTP client: `fetch` and `download` keeping each host's pace and waiting out 429s.

        pace = http.Pace(0.5, {"/graphql": (275, 600)}, domains=("example.com",))
        with http.Client(pace=pace) as client:
            page = client.fetch("https://example.com/graphql/query")   # paced; a 429 is waited out
            client.download("https://cdn.example.com/v.mp4", "videos/")

    Each host has its own learned speed, budgets, rate-header hold and back-off, so a CDN's 429
    never slows the site's own requests; the hosts of a `Pace` with a `label` share one. A wait
    of 15 s or more is drawn as a countdown row, by one of the threads waiting for it. It may be
    used from many threads at once.

    Args:
        raw: the `httpx.Client` requests go through, with the caller's cookies, headers and
            signing, which the caller closes; None makes one like `fetch`'s own, closed by `close`.
        pace: the floor and budgets for the hosts it names; None paces no host, though rate
            headers and 429s are still honoured.
        speeds: learned speeds by host (or label), as an earlier run's `speeds` gave them.
        stop: an event that ends every wait, and every download, with `StoppedError`.
    """

    def __init__(self, raw: httpx.Client | None = None, *, pace: Pace | None = None,
                 speeds: Mapping[str, float] | None = None, stop: threading.Event | None = None):
        self.raw: httpx.Client = raw if raw is not None else _new_client()
        self.pace: Pace | None = pace
        self.stop: threading.Event | None = stop
        self._owned = raw is None
        self._speeds = dict(speeds or {})
        self._hosts: dict[str, Host] = {}
        # Two threads asking for a new host at once must get the same one.
        self._hosts_lock = threading.Lock()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def speeds(self) -> dict[str, float]:
        """Each host's learned speed, from 1 (no throttle seen) to 8, to start the next run from.

        A host given in `speeds` but not asked this run keeps the speed it was given.
        """
        with self._hosts_lock:
            return {**self._speeds, **{name: host.speed for name, host in self._hosts.items()}}

    def close(self) -> None:
        """Close the httpx client when this client made it."""
        if self._owned:
            self.raw.close()

    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
              soft: Literal[False] = ..., attempts: int = ..., timeout: float = ...,
              redirects: bool | None = ...) -> Response: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              params: Mapping[str, Any] | None = ..., data: Any = ..., json: Any = ...,
              soft: bool, attempts: int = ..., timeout: float = ...,
              redirects: bool | None = ...) -> Response | None: ...
    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              params: Mapping[str, Any] | None = None, data: Any = None, json: Any = None,
              soft: bool = False, attempts: int = 3, timeout: float = 30.0,
              redirects: bool | None = None) -> Response | None:
        """`http.fetch` on this client, after the host's turn, waiting out each 429.

            page = client.fetch("https://example.com/items", params={"page": 2})

        Raises:
            StoppedError: `stop` was set during a wait.
            RefusedError, ServerError, Error: as `http.fetch` raises them.
        """
        return self.call(url, lambda: fetch(url, method=method, client=self.raw, headers=headers,
                                            params=params, data=data, json=json, soft=soft,
                                            attempts=attempts, timeout=timeout,
                                            redirects=redirects))

    def download(self, url: str | Sequence[str], dest: PathLike, *,
                 headers: Mapping[str, str] | None = None, size: int | None = None,
                 parts: Literal["auto"] | int = "auto", resume: bool = True, attempts: int = 3,
                 timeout: float = 30.0, check: Callable[[Path], Any] | None = None,
                 progress: Callable[[int, int | None], Any] | None = None,
                 label: str | None = None, stop: threading.Event | None = None) -> Download:
        """`http.download` on this client, after the host's turn, resuming after each 429.

            client.download("https://cdn.example.com/v.mp4", "videos/", progress=bar.update)

        `stop` stops this transfer, keeping its part; None uses the client's own `stop`, which
        still ends the waits before it. A 429's `Retry-After` is waited out up to 5 minutes.

        Raises:
            StoppedError: `stop` was set.
            RefusedError, ServerError, IncompleteError, ChangedError, Error: as `http.download`.
        """
        first = url if isinstance(url, str) else next(iter(url), "")
        halt = stop if stop is not None else self.stop

        def send() -> Download:
            try:
                return download(url, dest, client=self.raw, headers=headers, size=size,
                                parts=parts, resume=resume, attempts=attempts, timeout=timeout,
                                check=check, progress=progress, label=label, stop=halt)
            except RateLimitedError as error:
                if error.retry_after is None or error.retry_after <= MAX_RETRY_AFTER:
                    raise
                raise RateLimitedError(MAX_RETRY_AFTER, str(error)) from None

        return self.call(first, send)

    def call(self, url: str, send: Callable[[], A], *, attempts: int = 1) -> A:
        """Run `send`, a request made some other way such as from a browser tab, in `url`'s pace.

            visit = client.call(url, lambda: tab.goto(url))

        It waits for the host's turn, reads the rate headers of what `send` returns, and waits
        out each 429 and sends again, as long as it takes.

        Args:
            url: the URL `send` requests, whose host's pace it keeps.
            send: makes the request once; it returns None or a value with `headers` (and a
                `status`, a 429 of which is waited out), or raises `RateLimitedError`.
            attempts: tries in all while `send` raises `ServerError` or another `Error` (not a
                refusal), 1 s, 2 s, 4 s, … apart.

        Returns:
            What `send` returned, the first time it wasn't a 429.

        Raises:
            StoppedError: `stop` was set during a wait.
            RefusedError: what `send` raised, after its rate headers are read.
            Error: what `send` raised on its last attempt.
        """
        host = self._host(url)
        attempt = 1
        while True:
            self._wait(host, host.turn(url))
            try:
                answer = send()
            except RateLimitedError as error:
                self._throttled(host, error.retry_after)
                continue
            except RefusedError as error:
                if error.response is not None:
                    host.record(error.response.headers)
                raise
            except StoppedError:
                raise
            except Error as error:
                if attempt >= attempts:
                    raise
                seconds = min(FIRST_WAIT * 2 ** (attempt - 1), MAX_WAIT)
                log.debug("retry {n} of {url} in {seconds:g} s: {error}", n=attempt,
                          url=_where(url), seconds=seconds, error=error)
                if clock.sleep(seconds, stop=self.stop):
                    raise StoppedError(f"{host.name}: stopped") from None
                attempt += 1
                continue
            headers: Mapping[str, str] = getattr(answer, "headers", None) or {}
            host.record(headers)
            if getattr(answer, "status", None) == 429:
                retry_after = _retry_after(headers.get("retry-after"))
                self._throttled(host, retry_after if retry_after is not None else _reset(headers))
                continue
            host.clean()
            return answer

    def back_off(self, url: str, *, reason: str = "rate limited",
                 retry_after: float | None = None) -> None:
        """Count a throttle seen on `url`'s host some other way, such as a block page; wait it out.

            client.back_off("https://example.com/", reason="asked to wait", retry_after=120)

        A second call while the host is held adds nothing and waits with the first.

        Args:
            url: a URL of the host that throttled.
            reason: what the host did, for the warning.
            retry_after: the seconds the host asked for; None takes the back-off's next step,
                30 s doubling up to 15 min.

        Raises:
            StoppedError: `stop` was set during the wait.
        """
        host = self._host(url)
        self._throttled(host, retry_after, reason)
        self._wait(host, host.held())

    def _host(self, url: str) -> Host:
        """The pace of `url`'s host or label, made on first use with an earlier run's speed."""
        name = (httpx.URL(url).host or "").lower()
        pace = self.pace if self.pace is not None and self.pace.paces(name) else None
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
            if clock.sleep(seconds, stop=self.stop):
                raise StoppedError(f"{host.name}: stopped")
            return
        try:
            with log.progress(f"{host.name}: waiting", total=seconds, show=_seconds) as bar:
                end = clock.now() + seconds
                # One-second steps, so the countdown moves.
                while (left := end - clock.now()) > 0:
                    bar.count = seconds - left
                    if clock.sleep(min(1.0, left), stop=self.stop):
                        raise StoppedError(f"{host.name}: stopped")
        finally:
            with self._hosts_lock:
                host.showing = False


class Transport(httpx.BaseTransport):
    """An httpx transport that sends a split file's pieces over HTTP/1.1 and the rest over HTTP/2.

        raw = httpx.Client(transport=http.Transport(), follow_redirects=True)

    Photos and small files then share one HTTP/2 connection per host instead of a handshake
    each, while a split file's pieces get a TCP connection each. A host whose HTTP/2 fails is
    sent HTTP/1.1 for the rest of the transport's life. Without the `h2` package everything goes
    over HTTP/1.1. It may be used from many threads at once.

    Args:
        options: what `httpx.HTTPTransport` takes, such as `retries` or `verify`, for both.
    """

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


def _client() -> httpx.Client:
    """The module's own client, made on first use: it follows redirects and keeps no cookies between calls."""
    global _default
    with _client_lock:
        if _default is None:
            _default = _new_client()
        return _default


def _new_client() -> httpx.Client:
    """A client that follows redirects and keeps no cookies between calls."""
    # A jar whose policy allows no domain keeps no cookie, so a cookie one caller's fetch is sent
    # never rides along on another caller's request to the same site.
    refuse_all = http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
    return httpx.Client(follow_redirects=True, cookies=http.cookiejar.CookieJar(refuse_all),
                        transport=Transport())


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


def _send(client: httpx.Client, request: httpx.Request, redirects: bool | None = None) -> Response:
    """Send one attempt while holding a slot of its host, and read the whole body into a `Response`."""
    with _slot(request.url):
        follow = httpx.USE_CLIENT_DEFAULT if redirects is None else redirects
        response = client.send(request, follow_redirects=follow)
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
        if retry_after is None:
            retry_after = _reset(response.headers)
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


def _reset(headers: Mapping[str, str]) -> float | None:
    """The seconds until a 429's rate headers reset, plus a second; None without them."""
    for prefix in ("x-rate-limit-", "x-ratelimit-"):
        try:
            reset = float(headers[prefix + "reset"])
        except (KeyError, ValueError):  # missing or not a number: this spelling says nothing
            continue
        # A big number is a Unix time (X's), a small one seconds from now; the second is slack for
        # a clock a little behind the server's.
        seconds = reset - clock.wall() if reset > EPOCH else reset
        return max(0.0, seconds) + 1.0
    return None


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
