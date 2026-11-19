"""HTTP for any program on an asyncio loop: a Session, a Client, downloads into parts.

src/docs/errors explains it."""

from __future__ import annotations

import asyncio
import email.message
import email.utils
import http.cookiejar
import inspect
import math
import re
import time
import urllib.request
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import msgspec

from flnkit import clock, files, html, text
from flnkit.errors import (
    Blocked,
    Changed,
    Error,
    NetworkError,
    NotFound,
    SiteError,
    Stopped,
    disk,
)
from flnkit.text import redact

__all__ = [
    "Client", "Headers", "Meter", "Reply", "Request", "Response", "Session", "URL", "fake",
    "follow", "refusal", "reply",
]


# The longest Retry-After or rate-limit reset a refusal carries, in seconds.
MAX_BACKOFF = 3600.0
# A reset larger than this is a Unix time, not a number of seconds.
EPOCH = 1_000_000_000.0
# Transfers at once a session has handles for, by default; more wait for a handle. curl makes
# a handle only when one is needed, so room that is never used costs nothing.
HANDLES = 260
# Seconds before a connection counts as dead: to connect, or with no byte received.
TIMEOUT = 30.0
# The file system's writes go to the writer thread this many bytes at a time. Each hand-off to
# a thread waits for the GIL when it comes back, which on a busy loop can take milliseconds, so
# fewer, larger pieces pay: 4 MiB moved 1 GiB 15% faster than 1 MiB (src/docs/performance).
BUFFER = 4 << 20
# The most bytes of one transfer that wait in memory for the disk before curl pauses it.
HELD = 8 << 20
# Pieces curl gives in one call are queued as one, up to this many bytes.
BATCH = 256 << 10
# Redirects followed for one request, as curl_cffi's default allows.
REDIRECTS = 30
# The statuses that send a request on to their `Location`.
REDIRECTING = frozenset({301, 302, 303, 307, 308})
# The caller's headers a redirect to another host carries, as a browser's would: none that
# names who is asking (a cookie, credentials, a token of the site's).
CARRIED = frozenset({"accept", "accept-language", "range", "if-range", "referer"})
# What describes a body, dropped with it when a redirect turns a request into a GET.
BODY_HEADERS = ("content-type", "content-length", "content-encoding")
# The statuses that mean the item is gone for good: never retried, recorded as gone.
GONE = (404, 410)
QUERY_METHODS = ("GET", "HEAD")
SECRET_HEADERS = ("authorization", "cookie", "proxy-authorization")
# The shortest secret `_scrub` replaces in a message; anything shorter is no secret.
SHORT_SECRET = 4
# libcurl's error codes that another try can't mend (https://curl.se/libcurl/c/libcurl-errors.html):
# an unsupported protocol (1), a malformed URL (3), a proxy or host name that doesn't resolve
# (5, 6), too many redirects (47) and a certificate that fails (60).
FINAL_CURL_CODES = frozenset({1, 3, 5, 6, 47, 60})
# The fingerprint curl_cffi sends: Chrome's TLS handshake and HTTP/2 settings.
IMPERSONATE = "chrome"


# ---------------------------------------------------------------------------------------------
# Requests and responses
# ---------------------------------------------------------------------------------------------


class URL(str):
    """A URL that is still a `str`, with the parts a `prepare` hook or a fake reads.

        url = URL("https://example.com/api?q=1")
        url.host, url.path, url.params        # "example.com", "/api", {"q": "1"}
    """

    __slots__ = ()

    @property
    def host(self) -> str:
        """The host name, lower case; "" when there is none."""
        return (urlsplit(self).hostname or "").lower()

    @property
    def path(self) -> str:
        """The path, "/" when empty."""
        return urlsplit(self).path or "/"

    @property
    def params(self) -> dict[str, str]:
        """The query as `{name: value}`; the first value wins for a name given twice."""
        found: dict[str, str] = {}
        for name, value in parse_qsl(urlsplit(self).query, keep_blank_values=True):
            found.setdefault(name, value)
        return found


class Headers(MutableMapping[str, str]):
    """A request's headers, by name in lower case, so `Referer` and `referer` are one header.

    Args:
        headers: the headers to start with, as a mapping or as pairs.
    """

    def __init__(self, headers: Mapping[str, str] | Iterable[tuple[str, str]] = ()) -> None:
        self._items: dict[str, str] = {}
        pairs = headers.items() if isinstance(headers, Mapping) else headers
        for name, value in pairs:
            self[name] = value

    def __getitem__(self, name: str) -> str:
        return self._items[name.lower()]

    def __setitem__(self, name: str, value: str) -> None:
        self._items[name.lower()] = str(value)

    def __delitem__(self, name: str) -> None:
        del self._items[name.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        # Names only: a value may be a secret.
        return f"Headers({sorted(self._items)})"


@dataclass(eq=False)
class Request:
    """One request as it will be sent: what a session's `prepare` hook and a fake handler see.

    A `prepare` hook changes `headers` in place (or returns another `Request`).

    Attributes:
        method: the method, upper case.
        url: the whole URL, with its query.
        headers: the headers to send, beyond the session's defaults.
        content: the body; None for none.
        follow: whether the session follows its redirects; False gives back a redirect as it
            came, for the client to follow hop by hop (`Client._open`).
    """

    method: str
    url: URL
    headers: Headers = field(default_factory=Headers)
    content: bytes | None = None
    follow: bool = True

    def __post_init__(self) -> None:
        # A str and a dict are taken too, as a test or a script writes them.
        self.method = self.method.upper()
        if not isinstance(self.url, URL):
            self.url = URL(self.url)
        if not isinstance(self.headers, Headers):
            self.headers = Headers(self.headers)


class _Read(Mapping[str, str]):
    """A response's headers: read-only, by name in any case."""

    def __init__(self, headers: Mapping[str, str]) -> None:
        self._items = {name.lower(): value for name, value in headers.items()}

    def __getitem__(self, name: str) -> str:
        return self._items[name.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"Headers({sorted(self._items)})"


@dataclass(frozen=True, eq=False)
class Response:
    """One HTTP response: status, URL, headers, and body as text, JSON or HTML."""

    status: int
    url: str
    headers: Mapping[str, str]
    content: bytes = field(default=b"", repr=False)
    # How `html` parses the body; the kit's HTML parser unless a caller gives its own.
    html_parser: Callable[[str], Any] = field(default=html.parse, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", _Read(self.headers))

    @property
    def ok(self) -> bool:
        """Whether the status is 2xx."""
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        """The body as text, in the charset `Content-Type` names, or UTF-8."""
        charset = _charset(self.headers.get("content-type", ""))
        return self._cached("text", lambda: self.content.decode(charset, "replace"))

    @property
    def json(self) -> Any:
        """The body parsed as JSON. Raises `flnkit.errors.Changed` when it isn't JSON."""
        return self._cached("json", self._json)

    @property
    def html(self) -> Any:
        """The body parsed as HTML: a `flnkit.html.Document`."""
        return self._cached("html", lambda: self.html_parser(self.text))

    def _cached(self, name: str, compute: Callable[[], Any]) -> Any:
        key = f"_cached_{name}"
        if key not in self.__dict__:
            self.__dict__[key] = compute()
        return self.__dict__[key]

    def _json(self) -> Any:
        try:
            return msgspec.json.decode(self.content)
        except msgspec.DecodeError as error:
            msg = f"expected JSON from {redact(self.url)}, got {self.text[:80]!r}: {error}"
            raise Changed(msg, status=self.status, response=self) from None


# ---------------------------------------------------------------------------------------------
# The session: curl_cffi, cookies, and the fake
# ---------------------------------------------------------------------------------------------


@dataclass
class _Answer:
    """An answer whose body is still coming: what `Session.send` gives back."""

    status: int
    url: str
    headers: Mapping[str, str]
    chunks: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]
    set_cookies: list[str] = field(default_factory=list)

    async def read(self, *, limit: int | None = None) -> Response:
        """Read the rest of the body and close the answer.

        Args:
            limit: the most bytes the body may have; None reads any.

        Returns:
            The whole answer, as a `Response`.

        Raises:
            SiteError: the body is over `limit`; reading stops there.
        """
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in self.chunks:
                size += len(chunk)
                if limit is not None and size > limit:
                    raise SiteError(f"{redact(self.url)}: the answer is over "
                                    f"{text.size(limit)}, too big to read as a page",
                                    status=self.status)
                chunks.append(chunk)
        finally:
            await self.close()
        return Response(self.status, self.url, self.headers, b"".join(chunks))


class Session:
    """One curl_cffi session on an asyncio loop, with default headers, cookies and a hook.

        session = Session(headers={"User-Agent": agent}, cookies=jar, prepare=sign)
        ...                                   # clients send through it on the loop
        await session.close()

    Cookies are kept here, not in curl: each request carries the ones its URL matches, and a
    `Set-Cookie` the answer brings is kept for the next. So a fake and the network keep them the
    same way. The cookies an origin may get are worked out once and cached, until the jar
    changes or one of its cookies expires (`_Sent`).

    Args:
        headers: sent with every request, under the request's own.
        cookies: the cookies to start with (`flnkit.cookies.Jar`, or cookie dicts in its shape).
        prepare: called with each `Request` before each try, to add headers; it may return
            another `Request`, or None to keep its argument.
        timeout: seconds to connect, and seconds with no byte received, before a transfer
            counts as dead.
        handles: transfers at once in all; more wait for a handle. curl makes a handle only
            when a transfer needs one, so a large number costs nothing until it is used; give
            the most transfers the caller lets go at once, so the session never holds them.
        proxy: the proxy every request goes through, as curl reads one
            (`http://host:port`, `socks5://host:port`, `socks5h://…` to look names up there
            too); None for none. A fake (`fake`) answers without it.
    """

    def __init__(self, *, headers: Mapping[str, str] | None = None,
                 cookies: Iterable[Mapping[str, Any]] = (),
                 prepare: Callable[[Request], Request | None] | None = None,
                 timeout: float = TIMEOUT, handles: int = HANDLES,
                 proxy: str | None = None) -> None:
        self.headers = Headers(headers or {})
        self.prepare = prepare
        self.timeout = timeout
        self.handles = handles
        self.proxy = proxy
        self.jar = _Jar()
        self.add(cookies)
        self._raw: Any = None
        # What was worked out from the jar at version `_seen`: each origin's `_Sent`, the
        # soonest expiry in it, and `names`.
        self._seen = -1
        self._sent: dict[str, _Sent] = {}
        self._soonest: float = math.inf
        self._names: Mapping[str, str] = MappingProxyType({})

    @property
    def cookies(self) -> list[dict[str, Any]]:
        """The session's cookies now, as a `flnkit.cookies.Jar`."""
        return [_cookie_dict(cookie) for cookie in self.jar]

    @property
    def started(self) -> bool:
        """Whether curl's session was made: a request went out, so `close` has work to do."""
        return self._raw is not None

    @property
    def names(self) -> Mapping[str, str]:
        """The cookies as `{name: value}`; the last wins for a name on two domains."""
        if self._seen != self.jar.version:
            self._forget()
        return self._names

    def add(self, cookies: Iterable[Mapping[str, Any]]) -> None:
        """Keep `cookies` (dicts in `flnkit.cookies.Jar`'s shape), each replacing its namesake."""
        for cookie in cookies:
            self.jar.set_cookie(_cookie_from_dict(cookie))

    async def close(self) -> None:
        """Close curl's connections; the session can't send after this."""
        raw, self._raw = self._raw, None
        if raw is not None:
            await raw.close()

    async def send(self, request: Request) -> _Answer:
        """Send `request` with the cookies it matches, and give the answer as it streams in.

        Args:
            request: the request, already prepared.

        Returns:
            The answer, whose body is still to be read; `close` it when done.

        Raises:
            NetworkError: no answer came: retryable for a timeout or a reset, not for a URL or
                a name that is wrong.
        """
        headers = Headers(self.headers)
        headers.update(request.headers)
        if "cookie" not in headers and (cookie := self._cookie_header(request.url)):
            headers["cookie"] = cookie
        sent = Request(request.method, request.url, headers, request.content, request.follow)
        router = _FAKES[-1] if _FAKES else None
        if router is not None:
            answer = await router.answer(sent)
        else:
            answer = await self._curl(sent)
        self._keep(answer, sent.url)
        return answer

    def _cookie_header(self, url: str) -> str:
        """The `Cookie` header the jar gives `url`, or "": what `add_cookie_header` would add."""
        jar = self.jar
        if int(time.time()) >= self._soonest:
            # As `add_cookie_header` does after each request; a cookie gone moves the version.
            jar.clear_expired_cookies()
        if self._seen != jar.version:
            self._forget()
        if not self._names:
            return ""
        found = _ORIGIN.match(url)
        origin = found.group() if found else url
        sent = self._sent.get(origin)
        if sent is None:
            sent = self._sent[origin] = _Sent.of(jar, origin)
        return sent.header if sent.header is not None else sent.on(jar, url)

    def _forget(self) -> None:
        """Drop what was worked out from the jar, and read it again: it changed."""
        jar = self.jar
        with jar.lock:
            self._seen = jar.version
            cookies = list(jar)
        self._sent.clear()
        self._soonest = min((cookie.expires for cookie in cookies if cookie.expires is not None),
                            default=math.inf)
        self._names = MappingProxyType({cookie.name: cookie.value or "" for cookie in cookies})

    def _keep(self, answer: _Answer, url: str) -> None:
        """Keep the cookies an answer set, for the requests after it."""
        if not answer.set_cookies:
            return
        info = email.message.Message()
        for value in answer.set_cookies:
            info["Set-Cookie"] = value
        self.jar.extract_cookies(_Info(info), urllib.request.Request(_bare(answer.url or url)))

    def _session(self) -> _Curls:
        """The session's curl handles, made on first use."""
        if self._raw is None:
            try:
                import curl_cffi  # noqa: F401
            except ImportError:
                raise ImportError("flnkit.net needs curl_cffi: pip install curl_cffi") from None

            self._raw = _Curls(self.handles, self.timeout, self.proxy)
        return self._raw

    async def _curl(self, request: Request) -> _Answer:
        """Send `request` through curl, streaming the answer."""
        from curl_cffi import CurlError

        try:
            transfer = await self._session().open(request)
        except CurlError as error:
            raise _network(request, error) from None

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in transfer.chunks():
                    yield chunk
            except CurlError as error:
                raise _network(request, error) from None

        pairs = transfer.headers
        return _Answer(transfer.status, transfer.url, _joined(pairs), chunks(), transfer.close,
                       [value for name, value in pairs if name == "set-cookie"])


class _Curls:
    """libcurl handles on one multi handle, set up once and kept from one transfer to the next."""

    def __init__(self, handles: int, timeout: float, proxy: str | None) -> None:
        self._free = asyncio.Semaphore(handles)
        self._timeout = timeout
        self._proxy = proxy
        self._idle: list[Any] = []
        self._multi: Any = None
        self._closed = False

    async def open(self, request: Request) -> _Transfer:
        """Start `request` on a free handle, and wait for its answer's head.

        Returns:
            The transfer, its status and headers read; its body still streaming.

        Raises:
            CurlError: the transfer failed before any of the body came.
        """
        from curl_cffi.aio import AsyncCurl

        await self._free.acquire()
        try:
            if self._multi is None:
                self._multi = AsyncCurl()
            curl = self._idle.pop() if self._idle else self._new()
        except BaseException:
            self._free.release()
            raise
        transfer = _Transfer(curl, self._give)
        try:
            transfer.plain = self._ask(curl, request, transfer)
        except BaseException:
            self._give(curl, False)
            raise
        transfer.start(self._multi)
        try:
            await asyncio.shield(transfer.head)
        except asyncio.CancelledError:
            transfer.quit()
            raise
        if transfer.error is not None and not transfer.wrote:
            raise transfer.error
        return transfer

    def _new(self) -> Any:
        from curl_cffi import Curl

        curl = Curl()
        self._setup(curl)
        return curl

    def _setup(self, curl: Any) -> None:
        """Set what every request on `curl` shares, in the order curl_cffi sets it."""
        from curl_cffi import CurlOpt
        from curl_cffi.requests.exceptions import ImpersonateError
        from curl_cffi.requests.impersonate import resolve_latest_browser_type

        # curl's own cookie engine stays on, as curl_cffi has it: a cookie set by a redirect
        # goes to the redirect's target. `COOKIELIST` empties it before each request.
        curl.setopt(CurlOpt.COOKIEFILE, b"")
        curl.setopt(CurlOpt.CONNECTTIMEOUT_MS, int(self._timeout * 1000))
        curl.setopt(CurlOpt.LOW_SPEED_LIMIT, 1)
        curl.setopt(CurlOpt.LOW_SPEED_TIME, math.ceil(self._timeout))
        curl.setopt(CurlOpt.FOLLOWLOCATION, 1)
        curl.setopt(CurlOpt.MAXREDIRS, MAX_REDIRECTS)
        if self._proxy:
            curl.setopt(CurlOpt.PROXY_CREDENTIAL_NO_REUSE, 1)
            curl.setopt(CurlOpt.PROXY, self._proxy)
        curl.setopt(CurlOpt.ACCEPT_ENCODING, ACCEPT_ENCODING.encode())
        target = resolve_latest_browser_type(IMPERSONATE)
        if curl.impersonate(target, default_headers=True) != 0:
            raise ImpersonateError(f"Impersonating {target} is not supported")

    def _ask(self, curl: Any, request: Request, transfer: _Transfer) -> bool:
        """Set `request`'s own options on `curl`."""
        from curl_cffi import CurlOpt
        from curl_cffi.requests.headers import Headers as CurlHeaders
        from curl_cffi.requests.utils import requote_uri, update_header_line

        url, method, body = str(request.url), request.method, request.content or b""
        curl.setopt(CurlOpt.URL, requote_uri(url).encode())
        lines = []
        for name, value in CurlHeaders(dict(request.headers)).multi_items():
            lines.append(f"{name};" if value == "" else f"{name}: {value}")
        if body:
            update_header_line(lines, "Content-Type", "application/octet-stream")
        update_header_line(lines, "Expect", "", replace=True)
        curl.setopt(CurlOpt.HTTPHEADER, [line.encode() for line in lines])
        curl.setopt(CurlOpt.COOKIELIST, "ALL")
        curl.setopt(CurlOpt.FOLLOWLOCATION, int(request.follow))
        if self._proxy:
            # A scheme is case-insensitive, as curl_cffi's `urlparse` treats it.
            tunnel = url[:6].lower() == "https:" and not self._proxy.startswith("socks")
            curl.setopt(CurlOpt.HTTPPROXYTUNNEL, int(tunnel))
        curl.setopt(CurlOpt.WRITEFUNCTION, transfer.write)
        curl.setopt(CurlOpt.HEADERFUNCTION, transfer.line)
        if method == "GET" and not body:
            return True
        if method == "POST":
            curl.setopt(CurlOpt.POST, 1)
        elif method != "GET":
            curl.setopt(CurlOpt.CUSTOMREQUEST, method.encode())
        if method == "HEAD":
            curl.setopt(CurlOpt.NOBODY, 1)
        if body or method in ("POST", "PUT", "PATCH"):
            transfer.body = body
            curl.setopt(CurlOpt.POSTFIELDS, body)
            curl.setopt(CurlOpt.POSTFIELDSIZE, len(body))
            if method == "GET":
                curl.setopt(CurlOpt.CUSTOMREQUEST, method)
        return False

    def _give(self, curl: Any, plain: bool) -> None:
        """Take `curl` back once its transfer ended: kept as it is, or set up afresh."""
        # Frees the request's header list and drops its callbacks. curl keeps pointing at them,
        # but reads options only during a transfer, and the next one sets its own first.
        curl.clean_handles_and_buffers()
        self._free.release()
        if self._closed:
            curl.close()
            return
        if not plain:
            curl.reset()
            self._setup(curl)
        self._idle.append(curl)

    async def close(self) -> None:
        """Stop every transfer and close the handles; nothing can be sent after this."""
        self._closed = True
        if self._multi is not None:
            # Ends the transfers still running, whose handles are closed as they come back.
            await self._multi.close()
        for curl in self._idle:
            curl.close()
        self._idle.clear()


# What curl_cffi's `AsyncSession` asks for: its `max_redirects`, and `request`'s
# `accept_encoding` (Chrome's fingerprint then puts its own).
MAX_REDIRECTS = 30
ACCEPT_ENCODING = "gzip, deflate, br"
# curl's write callback returns this to abort its transfer (curl/curl.h).
WRITE_ERROR = 0xFFFFFFFF


class _Transfer:
    """One request on a kept handle: its answer's head, then its body as it streams in."""

    def __init__(self, curl: Any, give: Callable[[Any, bool], None]) -> None:
        self._curl = curl
        self._give = give
        loop = asyncio.get_running_loop()
        self._loop = loop
        self.head: asyncio.Future[None] = loop.create_future()
        self._ended = loop.create_future()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        # The pieces curl gave in one call, queued together by the loop's next turn.
        self._batch: list[bytes] = []
        self._batched = 0
        self._lines: list[bytes] = []
        self._held = 0
        self._paused = False
        self._quit = False
        self.plain = True
        self.body = b""
        self.status = 0
        self.url = ""
        self.headers: list[tuple[str, str]] = []
        self.error: BaseException | None = None
        self.wrote = False

    def start(self, multi: Any) -> None:
        """Hand the handle to the multi handle, which runs the transfer on the loop."""
        try:
            done = multi.add_handle(self._curl)
        except BaseException:
            self._give(self._curl, False)
            raise
        done.add_done_callback(self._end)

    def line(self, line: bytes) -> int:
        """curl's header callback: one line of the head (of each answer, redirects too)."""
        self._lines.append(line)
        return len(line)

    def write(self, chunk: bytes) -> int:
        """curl's write callback: a piece of the body."""
        if not self.head.done():
            self._read()
        if self._quit:
            return WRITE_ERROR
        self.wrote = True
        self._held += len(chunk)
        if self._held >= HELD and not self._paused:
            self._pause(True)
        if not self._batch:
            self._loop.call_soon(self._flush)
        self._batch.append(chunk)
        self._batched += len(chunk)
        if self._batched >= BATCH:
            self._flush()
        return len(chunk)

    def _flush(self) -> None:
        """Queue what curl's last call gave as one piece, so the reader wakes once for it."""
        batch, self._batch, self._batched = self._batch, [], 0
        if batch:
            self._queue.put_nowait(batch[0] if len(batch) == 1 else b"".join(batch))

    def _end(self, done: asyncio.Future[None]) -> None:
        # The multi handle let go of the handle before it settled `done`.
        if not done.cancelled():
            self.error = done.exception()
        if not self.head.done():
            self._read()
        self._flush()
        self._queue.put_nowait(None)
        self._give(self._curl, self.plain)
        self._ended.set_result(None)

    def _read(self) -> None:
        """Read the head: the status, the URL, and the last answer's headers, as curl_cffi does."""
        from curl_cffi import CurlInfo
        from curl_cffi.requests.headers import Headers as CurlHeaders

        self.status = int(self._curl.getinfo(CurlInfo.RESPONSE_CODE))
        self.url = self._curl.getinfo(CurlInfo.EFFECTIVE_URL).decode()
        kept: list[bytes] = []
        for line in b"".join(self._lines).splitlines():
            if not line.strip():
                continue
            if line.startswith(b"HTTP/"):
                kept = []
            elif line.startswith((b" ", b"\t")) and kept:
                kept[-1] += line
            else:
                kept.append(line)
        if kept:
            self.headers = [(name.lower(), value or "")
                            for name, value in CurlHeaders(kept).multi_items()]
        self.head.set_result(None)

    async def chunks(self) -> AsyncIterator[bytes]:
        """The body, as it comes; raises what ended the transfer, when it failed."""
        while (chunk := await self._queue.get()) is not None:
            self._took(len(chunk))
            yield chunk
        if self.error is not None:
            raise self.error

    def _took(self, size: int) -> None:
        self._held -= size
        if self._paused and self._held <= HELD // 2:
            self._pause(False)

    def quit(self) -> None:
        """Abort the transfer at its next byte: nothing more of it is read."""
        self._quit = True
        if self._paused:
            self._pause(False)

    async def close(self) -> None:
        """Stop the transfer, and wait until curl gives the handle back."""
        self.quit()
        await asyncio.shield(self._ended)

    def _pause(self, paused: bool) -> None:
        self._paused = paused
        # 1 is CURLPAUSE_RECV, 0 CURLPAUSE_CONT (curl/curl.h).
        _curl_pause(self._curl._curl, 1 if paused else 0)


def _curl_pause(handle: Any, how: int) -> None:
    """libcurl's `curl_easy_pause` on a curl_cffi handle (`how` as curl/curl.h names it)."""
    from curl_cffi._wrapper import lib

    lib.curl_easy_pause(handle, how)


class _Jar(http.cookiejar.CookieJar):
    """A cookie jar whose `version` moves on with every cookie set or cleared, by any caller."""

    def __init__(self) -> None:
        super().__init__()
        self.version = 0

    @property
    def lock(self) -> Any:
        """The jar's own lock, held while it is read or changed."""
        return self._cookies_lock

    def set_cookie(self, cookie: http.cookiejar.Cookie) -> None:
        super().set_cookie(cookie)
        self.version += 1

    def clear(self, domain: str | None = None, path: str | None = None,
              name: str | None = None) -> None:
        try:
            super().clear(domain, path, name)
        finally:
            self.version += 1


# A URL's scheme and netloc (user info and port too): all but the path that decides its cookies.
_ORIGIN = re.compile(r"[^:/?#]+://[^/?#]*")


@dataclass
class _Sent:
    """The cookies one origin may be sent, in the jar's order, at one version of the jar."""

    cookies: list[http.cookiejar.Cookie]
    header: str | None

    @classmethod
    def of(cls, jar: _Jar, origin: str) -> _Sent:
        """The cookies `jar` gives `origin`, picked by the jar's policy as `add_cookie_header`
        picks them, but for the path."""
        probe = urllib.request.Request(_bare(origin))
        policy = jar._policy
        with jar.lock:
            policy._now = jar._now = int(time.time())
            cookies = [cookie for domain, paths in jar._cookies.items()
                       if policy.domain_return_ok(domain, probe)
                       for named in paths.values() for cookie in named.values()
                       if policy.return_ok(cookie, probe)]
        if any(cookie.path != "/" for cookie in cookies):
            return cls(cookies, None)
        return cls(cookies, "; ".join(jar._cookie_attrs(list(cookies))))

    def on(self, jar: _Jar, url: str) -> str:
        """The `Cookie` header for `url`: the cookies whose path holds its path, longest first."""
        # The path as `http.cookiejar.request_path` reads it, matched as the jar's policy
        # (`DefaultCookiePolicy.path_return_ok`) matches it.
        path = http.cookiejar.escape_path(urlsplit(url).path)
        path = path if path.startswith("/") else "/" + path
        kept = [cookie for cookie in self.cookies
                if path == cookie.path or (path.startswith(cookie.path) and (
                    cookie.path.endswith("/") or path[len(cookie.path):][:1] == "/"))]
        return "; ".join(jar._cookie_attrs(kept))


class _Info:
    """What `http.cookiejar` reads from a response: its headers, by `info()`."""

    def __init__(self, message: email.message.Message) -> None:
        self._message = message

    def info(self) -> email.message.Message:
        return self._message


# ---------------------------------------------------------------------------------------------
# The client: one try of a request or a download
# ---------------------------------------------------------------------------------------------


class Client:
    """A session and a stop event: what fetches and downloads, one try at a time.

        client = Client(session, stop=event)
        page = await client.fetch(url)
        await client.fill(url, held, progress=row)

    It neither paces nor retries: the caller's scheduler decides when a try goes and whether
    another follows. Two clients may share a session: flnd lists through one that stops at the
    first Ctrl-C and downloads through one that stops at the second.

    Args:
        session: what sends.
        stop: a `threading.Event` that stops transfers with `Stopped`.
    """

    def __init__(self, session: Session, *, stop: Any = None) -> None:
        self.session = session
        self.stop = stop

    async def fetch(self, url: str, *, method: str = "GET",
                    headers: Mapping[str, str] | None = None, body: Any = None,
                    check: bool = True, limit: int | None = None) -> Response:
        """Send one request and return its `Response`.

        Args:
            url: where to.
            method: the method.
            headers: headers for this request, over the session's.
            body: on a GET or HEAD a dict is the query; on any other method a dict or list is
                JSON; bytes or str go as they are.
            check: raise for a refusal (`refusal`); False returns any answer.
            limit: the most bytes the body may have; None reads any.

        Returns:
            The response: when `check`, one whose status is under 400.

        Raises:
            Error: as the module's policy says; a refusal carries its `response`.
            SiteError: the body is over `limit`.
        """
        method = method.upper()
        answer = await self._open(_request(method, url, headers, body))
        response = await answer.read(limit=limit)
        return _checked(response, method) if check else response

    async def fill(self, url: str, held: files.Part, *,
                   headers: Mapping[str, str] | None = None, resume: bool = True,
                   progress: Any = None,
                   until: Callable[[], int | None] | None = None) -> Mapping[str, str]:
        """Stream `url` into the open part `held`: one try, going on from the bytes it holds.

        The caller owns the part: it decides where it lands and what happens on failure.

        Args:
            url: the file's URL.
            held: the locked part, from `flnkit.files.part`.
            headers: extra request headers.
            resume: go on from the bytes the part holds, with `If-Range` on the validator its
                sidecar kept; False empties it first.
            progress: a sink for the bytes as they come: anything with a settable `total` and
                an `advance(n)`, or a `Meter` of one, which keeps where the tries before
                this one left it (and its `write`); None reports nothing.
            until: where the part's bytes stop, asked at each chunk: the file's end moves
                there while the transfer goes (a segment of it split off); None is its end.

        Returns:
            The headers of the answer that filled the part, lower case.

        Raises:
            Error: the try failed (see the module's policy).
        """
        meter = progress if isinstance(progress, Meter) else Meter(progress)
        meter.until = until
        return await self._file(url, held, headers, resume, meter)

    async def span(self, url: str, held: files.Part, start: int, *,
                   headers: Mapping[str, str] | None = None, validator: str | None = None,
                   progress: Any = None,
                   until: Callable[[], int | None]) -> Mapping[str, str]:
        """Stream the bytes of `url` from `start` to `until()` into the part at their offset:
        one try at one segment of a file, beside the others.

        Args:
            url: the file's URL.
            held: the locked part, written at `start` onwards (`Part.write_at`).
            start: the first byte.
            headers: extra request headers.
            validator: an ETag or date for `If-Range`, so a file that changed comes whole (a
                200), which `progress`'s `opened` refuses.
            progress: a sink, or a `Meter` (made at `start`), as `fill` takes.
            until: where the segment ends, asked at each chunk; it may move nearer.

        Returns:
            The answer's headers, lower case.

        Raises:
            Error: the try failed (see the module's policy).
        """
        meter = progress if isinstance(progress, Meter) else Meter(progress, at=start)
        meter.until = until
        end = until()
        span = f"bytes={start}-{end - 1}" if end is not None else f"bytes={start}-"
        answer = await self._range(url, headers, span, validator)
        try:
            begin = _range_start(answer.headers) if answer.status == 206 else 0
            total = _total_bytes(answer.headers, begin)
            if meter.opened is not None:
                meter.opened(answer.status, answer.headers, begin, total)
            if answer.status != 206 or begin != start:
                raise NetworkError(f"GET {redact(url)}: HTTP {answer.status} for bytes from "
                                   f"{start}, not a range from there")
            await self._write(url, answer, held, start, total, meter, offset=start)
            return answer.headers
        finally:
            await answer.close()

    async def _open(self, request: Request) -> _Answer:
        """Prepare `request` with the session's hook, send it, and raise for its stop."""
        self._check(request.url)
        prepare = self.session.prepare
        if prepare is None:
            return await self.session.send(request)
        # A session with a hook follows redirects here, hop by hop, each hop prepared afresh
        # from the caller's headers: what the hook adds for one host (a signature, a token)
        # never follows a redirect to another, and a hop to another host carries only the
        # caller's `CARRIED` headers, never its cookie or credentials.
        host = request.url.host
        for _ in range(REDIRECTS + 1):
            hop = Request(request.method, request.url, Headers(request.headers),
                          request.content, follow=False)
            answer = await self.session.send(prepare(hop) or hop)
            location = answer.headers.get("location")
            if answer.status not in REDIRECTING or not location:
                return answer
            await answer.close()
            target = urljoin(str(request.url), location)
            if not target.lower().startswith(("http://", "https://")):
                raise NetworkError(f"{request.method} {redact(str(request.url))}: redirected "
                                   "off the web")
            request = _redirected(request, answer.status, URL(target), host)
            self._check(request.url)
        raise NetworkError(f"{request.method} {redact(str(request.url))}: too many redirects")

    async def _file(self, url: str, held: files.Part, headers: Mapping[str, str] | None,
                    resume: bool, meter: Meter) -> Mapping[str, str]:
        """One try at a file: resume the part, or start over."""
        if not resume or (held.size and "segments" in held.meta):
            # A part written in segments (or an older flnd's with holes) isn't one run of bytes.
            held.reset()
        offset = held.size
        validator = held.meta.get("etag") if offset else None
        answer = await self._range(url, headers, f"bytes={offset}-", validator)
        try:
            status = answer.status
            start = _range_start(answer.headers) if status == 206 else 0
            if status == 206 and start != offset:
                raise NetworkError(
                    f"GET {redact(url)}: Content-Range starts at {start}, not {offset}",
                    retryable=True,
                )
            total = _total_bytes(answer.headers, start)
            if status != 416:
                if not start and held.size:
                    held.reset()
                held.note(etag=_validator(answer.headers))
                if meter.opened is not None:
                    meter.opened(status, answer.headers, start, total)
            if status != 416:
                await self._write(url, answer, held, start, total, meter)
                return answer.headers
        finally:
            await answer.close()
        await self._unsatisfied(url, held, headers, offset, total, meter)
        return answer.headers

    async def _unsatisfied(self, url: str, held: files.Part,
                           headers: Mapping[str, str] | None, offset: int, total: int | None,
                           meter: Meter) -> None:
        """A 416 on `Range: bytes=offset-`: the part is whole, or it starts over from 0."""
        if total == offset:
            meter.start(offset, offset)
            return
        if not offset:
            raise SiteError(f"GET {redact(url)}: HTTP 416", status=416)
        await self._file(url, held, headers, False, meter)

    async def _range(self, url: str, headers: Mapping[str, str] | None, span: str,
                     validator: str | None = None) -> _Answer:
        """Open a GET for `span`, raising for a 4xx or 5xx other than 416."""
        extra = {"Range": span, **({"If-Range": validator} if validator else {})}
        answer = await self._open(_request("GET", url, {**(headers or {}), **extra}, None))
        if answer.status >= 400 and answer.status != 416:
            _checked(await answer.read(), "GET")
        return answer

    async def _write(self, url: str, answer: _Answer, held: files.Part, start: int,
                     total: int | None, meter: Meter, *, offset: int | None = None) -> None:
        """Append the answer's body to the part, and fail a transfer that came short."""
        meter.start(total, start)
        sink = _Sink(held, offset, write=meter.write, wrote=meter.wrote, until=meter.until)
        at, end = start, None
        try:
            async for chunk in answer.chunks:
                self._check(url)
                end = meter.until() if meter.until is not None else None
                if end is not None and at + len(chunk) >= end:
                    chunk = chunk[:max(0, end - at)]
                await sink.add(chunk)
                meter.add(len(chunk))
                at += len(chunk)
                if meter.pace is not None:
                    await meter.pace(len(chunk))
                if end is not None and at >= end:
                    break
        finally:
            await sink.flush(last=True)
        if end is not None:
            if at < end:
                raise NetworkError(f"GET {redact(url)}: truncated at {at} of {end} bytes",
                                   retryable=True)
            return
        if offset is not None:
            if total is not None and at < total:
                raise NetworkError(f"GET {redact(url)}: truncated at {at} of {total} bytes",
                                   retryable=True)
            return
        if total is not None and held.size < total:
            raise NetworkError(f"GET {redact(url)}: truncated at {held.size} of {total} "
                               "bytes", retryable=True)

    def _check(self, url: str) -> None:
        if self.stop is not None and self.stop.is_set():
            raise Stopped(f"{redact(url)}: stopped")


# What runs one blocking write of a part for a transfer: `await write(call, *args)`.
Write = Callable[..., Awaitable[Any]]


class Meter:
    """Where a download stands on its sink: a `total` it sets and an `advance(n)` it calls.

    The sink is anything with a settable `total` and an `advance(n)`: a `ui.Item`, or the job's
    per-file stream; None reports nothing. Give `Client.fill` the same meter for each try of
    one download, so a try that resumes moves the sink only by what it adds.

    Args:
        sink: where the bytes are counted, or None.
        write: runs one blocking write of the part, as `await write(call, *args)`; None uses
            this module's one writer thread.
        at: where in the file its transfer starts: a segment's start.
    """

    def __init__(self, sink: Any, *, write: Write | None = None, at: int = 0):
        self._sink = sink
        self._at = at
        # Asked at each chunk for where the bytes stop (`Client.fill`'s `until`).
        self.until: Callable[[], int | None] | None = None
        # Told of each answer before its body is written, `opened(status, headers, start,
        # total)`; it may raise to refuse it. None takes any.
        self.opened: Callable[[int, Mapping[str, str], int, int | None], None] | None = None
        # Awaited after each piece is written, with its size: where a caller keeps its
        # progress (a segment's sidecar). None waits for nothing.
        self.wrote: Callable[[int], Awaitable[None]] | None = None
        # Runs one blocking write of the part, `await write(call, *args)`, on the caller's
        # thread for the part's disk; None uses this module's one writer thread.
        self.write = write
        # Waited for after each chunk with its size, `await pace(n)`: the caller's speed
        # limit; None limits nothing.
        self.pace: Callable[[int], Awaitable[None]] | None = None

    def start(self, total: int | None, at: int) -> None:
        """A try begins: set the sink's total, and move it to where the try starts.

        Args:
            total: the file's size, or None when unknown.
            at: the byte the try starts at (what the part already holds).
        """
        if self._sink is None:
            return
        self._sink.total = total
        # A try that starts over moves the sink back by what the last one wrote.
        self._sink.advance(at - self._at)
        self._at = at

    def add(self, n: int) -> None:
        if self._sink is not None:
            self._sink.advance(n)
            self._at += n


# The one thread every part is written on, so a slow disk blocks it, never the loop.
_writer: ThreadPoolExecutor | None = None


def _writer_thread() -> ThreadPoolExecutor:
    global _writer
    if _writer is None:
        _writer = ThreadPoolExecutor(1, thread_name_prefix="flnd-writer")
    return _writer


class _Sink:
    """A transfer's bytes on their way to its part: gathered on the loop, written on a writer."""

    def __init__(self, held: files.Part, offset: int | None = None, *,
                 write: Write | None = None,
                 wrote: Callable[[int], Awaitable[None]] | None = None,
                 until: Callable[[], int | None] | None = None) -> None:
        self._held = held
        self._offset = offset
        self._run = write
        self._until = until
        self._wrote = wrote
        self._chunks: list[bytes] = []
        self._size = 0
        self.written = 0

    async def add(self, chunk: bytes) -> None:
        self._chunks.append(chunk)
        self._size += len(chunk)
        if self._size >= BUFFER:
            await self.flush()

    async def flush(self, *, last: bool = False) -> None:
        """Write what is gathered, and wait for it, even through a cancel.

        The last piece of an appended body, when it fits the part's own buffer
        (`flnkit.files.BUFFER`), goes there on the loop instead: a copy in memory, no system
        call, since the full pieces before it went to the disk. The part's landing (or its
        `head`) writes it, off the loop. A small file then costs no hop to the writer at all.
        A part in segments never does: its sidecar counts only what is on disk, for a crash.

        Args:
            last: the body ended (or failed): this is its last piece.

        Raises:
            DiskError: the part can't be written.
        """
        if not self._chunks:
            return
        data = self._chunks[0] if len(self._chunks) == 1 else b"".join(self._chunks)
        self._chunks, self._size = [], 0
        if last and self._offset is None and len(data) < files.BUFFER and (
                self._until is None or self._until() is None):
            self._held.write(data)
            self.written += len(data)
            if self._wrote is not None:
                await self._wrote(len(data))
            return
        if self._offset is None:
            call, args = self._append, (data,)
        else:
            call, args = self._held.write_at, (self._offset + self.written, data)
        if self._run is not None:
            # The caller's writer, which waits for its write to end even through a cancel.
            await self._run(call, *args)
        else:
            loop = asyncio.get_running_loop()
            await _settle(loop.run_in_executor(_writer_thread(), call, *args))
        self.written += len(data)
        if self._wrote is not None:
            await self._wrote(len(data))

    def _append(self, data: bytes) -> None:
        self._held.write(data)
        self._held.flush()


async def _settle(work: asyncio.Future[Any]) -> None:
    """Wait for a write on the writer thread; on a cancel, still wait for it, then cancel."""
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
        raise
    except OSError as error:
        raise disk(error) from error


# ---------------------------------------------------------------------------------------------
# A short link, and the fake
# ---------------------------------------------------------------------------------------------


def follow(url: str) -> str:
    """Where a short link leads, from one HEAD request: `follow("https://vm.tiktok.com/ZM…")`.

    It blocks, on a loop and a session of its own, so any thread but a loop's may call it.

    Args:
        url: the short link.

    Returns:
        The URL the redirects end at.

    Raises:
        NetworkError: no answer came.
    """

    async def head() -> str:
        session = Session()
        try:
            answer = await session.send(Request("HEAD", URL(url)))
            await answer.close()
            return answer.url
        finally:
            await session.close()

    return asyncio.run(head())


# The routers of the `fake` blocks now open, innermost last.
_FAKES: list[_Router] = []


@dataclass
class Reply:
    """An answer a fake gives: status, headers, and a body whole or in chunks.

    Attributes:
        status: the HTTP status.
        headers: the headers, as pairs: a name may come twice (`Set-Cookie`).
        body: the body, whole.
        chunks: the body in pieces, when given instead: an iterable, or an async one (which may
            wait without holding up the loop); one that raises an `OSError` part way stands for
            a connection cut there.
    """

    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    chunks: Iterable[bytes] | None = None


def reply(status: int = 200, *, json: Any = None, content: bytes | str | None = None,
          html: str | None = None, text: str | None = None,
          headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
          chunks: Iterable[bytes] | None = None) -> Reply:
    """An answer for a `fake` handler to give.

        reply(200, json={"ok": True}); reply(404); reply(200, content=b"...")
        reply(200, headers={"Content-Length": "10"}, chunks=cut(b"half"))

    Args:
        status: the HTTP status.
        json: a body encoded as JSON, with its content type.
        content: a body as it is.
        html: a body of HTML, with its content type.
        text: a body of plain text, with its content type.
        headers: more headers; a list of pairs may name one twice.
        chunks: the body in pieces; one that raises an `OSError` part way cuts the connection.

    Returns:
        The answer.
    """
    pairs = list(headers.items() if isinstance(headers, Mapping) else headers or [])
    body = b""
    kind = None
    if json is not None:
        body, kind = msgspec.json.encode(json), "application/json"
    elif html is not None:
        body, kind = html.encode(), "text/html; charset=utf-8"
    elif text is not None:
        body, kind = text.encode(), "text/plain; charset=utf-8"
    elif content is not None:
        body = content.encode() if isinstance(content, str) else bytes(content)
    if kind and not any(name.lower() == "content-type" for name, _ in pairs):
        pairs.append(("content-type", kind))
    return Reply(status, pairs, body, chunks)


@contextmanager
def fake(routes: Mapping[str, Any] | Callable[[Request], Any]) -> Iterator[None]:
    """Answer every request in the block from `routes`, and never the network.

    `routes` is a handler, called with each `Request`, or a mapping from a URL, or a prefix
    ending in `*`, to an answer. An answer is a `Reply` (`reply(...)`), a `Response`, a dict or
    list (JSON), bytes or str (a body that serves ranges), an int (that status), or a callable
    taking the `Request` and giving one of those; a handler or a callable may be async. A URL
    no route matches raises a `NetworkError`, as an unknown host would; a handler that raises an
    `OSError` (`ConnectionError`, `TimeoutError`) stands for a failed connection.

        with fake({"https://example.com/api": {"ok": True}}):
            ...

    Args:
        routes: the handler, or the routes by URL.

    Yields:
        Nothing: requests are answered while the block runs.
    """
    router = _Router(routes)
    _FAKES.append(router)
    try:
        yield
    finally:
        _FAKES.remove(router)


class _Router:
    """`fake`'s routes: the answer for a request's URL, as an `_Answer`."""

    def __init__(self, routes: Mapping[str, Any] | Callable[[Request], Any]):
        self.handler = routes if callable(routes) else None
        table = routes if isinstance(routes, Mapping) else {}
        self.exact = {url: route for url, route in table.items() if not url.endswith("*")}
        prefixes = [(url[:-1], route) for url, route in table.items() if url.endswith("*")]
        self.prefixes = sorted(prefixes, key=lambda pair: -len(pair[0]))

    async def answer(self, request: Request) -> _Answer:
        """The answer to `request`, following redirects as curl does (`REDIRECTS` at most).

        Args:
            request: the request as it was sent.

        Returns:
            The last answer, from the URL the redirects ended at.

        Raises:
            NetworkError: no route answers a URL, a handler raised an `OSError`, or the
                redirects went on past `REDIRECTS`.
        """
        for _ in range(REDIRECTS):
            url = _bare(str(request.url))
            answer = await self._one(request, url)
            location = dict((name.lower(), value) for name, value in answer.headers).get(
                "location")
            if answer.status not in REDIRECTING or not location or not request.follow:
                return _streamed(answer, url, request)
            # A 303 turns any method but HEAD into a GET without a body, as curl does.
            method = "GET" if answer.status == 303 and request.method != "HEAD" else request.method
            request = Request(method, URL(urljoin(url, location)), request.headers,
                              None if method == "GET" else request.content)
        raise NetworkError(f"{request.method} {redact(str(request.url))}: too many "
                           "redirects")

    async def _one(self, request: Request, url: str) -> Reply:
        """One request's answer from its route, calling (and awaiting) a callable route."""
        route = self.handler if self.handler is not None else self._route(url)
        if route is None:
            raise NetworkError(f"{request.method} {redact(url)}: no route in net.fake")
        try:
            while callable(route) and not isinstance(route, (Reply, Response)):
                route = route(request)
                if inspect.isawaitable(route):
                    route = await route
        except OSError as error:
            raise _network(request, error) from None
        return _reply(route, request)

    def _route(self, url: str) -> Any:
        route = self.exact.get(url, self.exact.get(url.split("?", 1)[0]))
        if route is None:
            route = next((answer for prefix, answer in self.prefixes if url.startswith(prefix)),
                         None)
        return route


def _reply(route: Any, request: Request) -> Reply:
    """A route's answer as a `Reply`; bytes or str serve `Range`, as a file server would."""
    if isinstance(route, Reply):
        return route
    if isinstance(route, Response):
        return Reply(route.status, list(route.headers.items()), route.content)
    if isinstance(route, int):
        return Reply(route)
    if isinstance(route, (Mapping, list)):
        return reply(json=route)
    body = route.encode() if isinstance(route, str) else bytes(route)
    asked = re.fullmatch(r"bytes=(\d+)-(\d*)", request.headers.get("range", ""))
    if asked is None:
        return Reply(200, [("accept-ranges", "bytes")], body)
    start, end = int(asked[1]), min(int(asked[2] or len(body) - 1), len(body) - 1)
    if start >= len(body):
        return Reply(416, [("content-range", f"bytes */{len(body)}")])
    return Reply(206, [("accept-ranges", "bytes"),
                       ("content-range", f"bytes {start}-{end}/{len(body)}")],
                 body[start:end + 1])


def _streamed(answer: Reply, url: str, request: Request) -> _Answer:
    """A `Reply` as an `_Answer`, its body given in its chunks."""
    pairs = [(name.lower(), value) for name, value in answer.headers]

    async def chunks() -> AsyncIterator[bytes]:
        given: Any = answer.chunks if answer.chunks is not None else [answer.body]
        try:
            if hasattr(given, "__aiter__"):
                async for chunk in given:
                    if chunk:
                        yield chunk
                return
            for chunk in given:
                if chunk:
                    yield chunk
        except OSError as error:
            raise _network(request, error) from None

    async def close() -> None:
        pass

    return _Answer(answer.status, url, _joined(pairs), chunks(), close,
                   [value for name, value in pairs if name == "set-cookie"])


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def with_query(url: str, params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None) -> str:
    """`url` with `params` added to its query; a name already there takes the new value.

    A value of None is sent empty, a bool as `true` or `false`, and a list or tuple as the name
    once per item. `params` may be a mapping, or a sequence of (name, value) pairs.

    Args:
        url: the URL, with or without a query.
        params: the names and values to add; None or empty leaves `url` as it is.

    Returns:
        The URL with the query.

    Raises:
        NetworkError: `url` can't be read as a URL.
    """
    if not params:
        return url
    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise NetworkError(f"{redact(url)}: invalid URL: {error}") from None
    if isinstance(params, Mapping):
        pairs = list(params.items())
        new_names = set(params)
    else:
        pairs = list(params)
        new_names = {name for name, _ in pairs}
    kept = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name not in new_names]
    for name, value in pairs:
        values = value if isinstance(value, (list, tuple)) else [value]
        kept += [(name, _query_value(item)) for item in values]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def _query_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _request(method: str, url: str, headers: Mapping[str, str] | None, body: Any) -> Request:
    """A `Request` for `body`: a query on a GET or HEAD, JSON for a dict or list, else as is."""
    content: bytes | None = None
    extra = Headers(headers or {})
    if isinstance(body, Mapping) and method in QUERY_METHODS:
        url = with_query(url, body)
    elif isinstance(body, (Mapping, list)):
        content = msgspec.json.encode(body)
        extra.setdefault("content-type", "application/json")
    elif isinstance(body, str):
        content = body.encode()
    elif body is not None:
        content = bytes(body)
    _host(url)
    return Request(method, URL(url), extra, content)


def _redirected(request: Request, status: int, url: URL, host: str) -> Request:
    """The next hop of `request` after a redirect of `status` to `url`, as a browser makes it."""
    method, content, headers = request.method, request.content, Headers(request.headers)
    if (status == 303 and method != "HEAD") or (status in (301, 302) and method == "POST"):
        method, content = "GET", None
        for name in BODY_HEADERS:
            headers.pop(name, None)
    if url.host != host:
        headers = Headers({name: value for name, value in headers.items()
                           if name.lower() in CARRIED})
    return Request(method, url, headers, content)


def _host(url: str) -> str:
    """The URL's host, lower case."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as error:
        raise NetworkError(f"{redact(url)}: invalid URL: {error}") from None
    if parts.scheme not in ("http", "https") or not host:
        raise NetworkError(f"{redact(url)}: not an http or https URL")
    return host.lower()


def _validator(headers: Mapping[str, str]) -> str | None:
    """What `If-Range` can name a response's file by: a strong ETag, else its date, else None."""
    tag = headers.get("etag")
    if tag and not tag.startswith("W/"):
        return tag
    return headers.get("last-modified")


def _range_start(headers: Mapping[str, str]) -> int:
    """The byte a 206 answer's `Content-Range` says the body starts at, or 0 when missing."""
    cr = headers.get("content-range", "")
    if cr.startswith("bytes "):
        span = cr[6:].split("/", 1)[0]
        if "-" in span:
            return int(span.partition("-")[0])
    return 0


def _total_bytes(headers: Mapping[str, str], offset: int) -> int | None:
    """The whole file's size, from `Content-Range`'s total or from where the answer starts plus
    its `Content-Length`.

    Args:
        headers: the answer's headers.
        offset: the byte the answer's body starts at.

    Returns:
        The size, or None when the server didn't say.
    """
    content_range = headers.get("content-range")
    if content_range and "/" in content_range:
        tail = content_range.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    content_length = headers.get("content-length")
    if content_length and content_length.isdigit():
        return offset + int(content_length)
    return None


def _joined(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Header pairs as one mapping, a name given twice holding both values joined by ", "."""
    joined: dict[str, str] = {}
    for name, value in pairs:
        joined[name] = f"{joined[name]}, {value}" if name in joined else value
    return joined


def _checked(response: Response, method: str) -> Response:
    """`response`, or the error its status means under the module's policy (`refusal`)."""
    error = refusal(response, method=method)
    if error is not None:
        raise error
    return response


def refusal(response: Any, *, method: str = "GET") -> Error | None:
    """The error a response's status means, or None for a status under 400.

    Args:
        response: the answer: anything with `status`, `url` and `headers`.
        method: the request's method, named in the message.

    Returns:
        `Blocked` for a 429, with the site's wait from `Retry-After` or a rate-limit reset
        header; `NotFound` for a 404 or 410; `SiteError` for any other 4xx, and a retryable one
        for a 5xx. Each carries the response. None when the status is under 400.
    """
    status = response.status
    if status < 400:
        return None
    where = f"{method} {redact(response.url)}"
    if status == 429:
        retry_after = _retry_after(response.headers.get("retry-after"))
        if retry_after is None:
            retry_after = _reset(response.headers)
        wait = "" if retry_after is None else f", retry after {retry_after:g} s"
        return Blocked(f"{where}: HTTP 429, rate limited{wait}", wait=retry_after, status=429,
                       response=response)
    if status in GONE:
        return NotFound(f"{where}: HTTP {status}", status=status, response=response)
    # A 5xx is the server's trouble, which may pass; any other 4xx is its answer.
    return SiteError(f"{where}: HTTP {status}", status=status, response=response,
                     retryable=status >= 500)


def _retry_after(value: str | None) -> float | None:
    """Seconds to wait from a `Retry-After` header: a number of seconds or an HTTP date."""
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
    # Retry-After: 0 still needs a pause, or the same host is hit in a tight loop.
    if seconds <= 0:
        seconds = 1.0
    return min(seconds, MAX_BACKOFF)


def _reset(headers: Mapping[str, str]) -> float | None:
    """Seconds to wait from a rate-limit reset header (`x-rate-limit-reset`, X's and others')."""
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


def _network(request: Request, error: BaseException) -> NetworkError:
    """The `NetworkError` a failed transfer is: retryable unless no retry could mend it."""
    code = getattr(error, "code", None)
    retryable = code not in FINAL_CURL_CODES if isinstance(code, int) else True
    # curl_cffi appends a pointer to curl's error list, which says nothing about this failure.
    reason = str(error).split(" See https://curl.se/", 1)[0].strip() or type(error).__name__
    message = f"{request.method} {redact(str(request.url))}: {reason}"
    return NetworkError(_scrub(_without_userinfo(message, str(request.url)), request),
                        retryable=retryable)


def _bare(url: str) -> str:
    """`url` without its user info, as a fake's routes and a cookie jar name it."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


def _without_userinfo(message: str, url: str | None = None) -> str:
    """`message` with the user info of `url` replaced by `***`."""
    try:
        netloc = urlsplit(url if url is not None else message).netloc
    except ValueError:
        return message
    userinfo = netloc.rpartition("@")[0] if "@" in netloc else ""
    return message.replace(userinfo, "***") if userinfo else message


def _scrub(text: str, request: Request) -> str:
    """`text` without any secret `request` carries, each replaced by `***`."""
    secrets: list[str] = []
    for name in SECRET_HEADERS:
        value = request.headers.get(name)
        if not value:
            continue
        secrets.append(value)
        if name == "cookie":
            secrets += [part.partition("=")[2].strip() for part in value.split(";")]
        else:
            secrets += value.split()[1:]
    try:
        parts = urlsplit(str(request.url))
        secrets += [value for _, value in parse_qsl(parts.query, keep_blank_values=True)]
        secrets.append(parts.password or "")
    except ValueError:
        pass
    # Longest first, so a value that holds a shorter one is replaced whole.
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= SHORT_SECRET:
            text = text.replace(secret, "***")
    return text


def _charset(content_type: str) -> str:
    """The charset a `Content-Type` names."""
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


def _cookie_dict(cookie: http.cookiejar.Cookie) -> dict[str, Any]:
    """A cookie of the jar as a dict in `flnkit.cookies.Jar`'s shape (Playwright's)."""
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
    """A cookie dict in `flnkit.cookies.Jar`'s shape as a cookie for the jar."""
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
