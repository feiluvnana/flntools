"""HTTP for any program on an asyncio loop: a `Session`, paced `Client`s, and downloads into parts.

What it offers, for any caller:
- `Session`: one curl_cffi `AsyncSession` (libcurl's multi interface, Chrome's TLS and HTTP/2
  fingerprint), its default headers, its cookies, and a `prepare` hook that may add headers to
  each request before it goes out (a signature, a CSRF token).
- `Limits`: how hard the session may press each host. A *paced* host gets a token bucket at a
  rate in requests per second; any other host gets a `Cap` on its transfers at once, which
  adapts to what the host can take (AIMD, below). `Limits.load` is a read-only snapshot of
  each host's cap and transfers in flight, and `Limits.pauses` of each wait before a host's
  next request, with its reason. `Limits.suspended`, when set, holds every new request.
- `Client`: a session with limits, a retry policy and a stop event. `fetch` sends one request;
  `call` paces and retries any coroutine (a page loaded in a browser, a request sent by a page);
  `fill` streams a file, or the first of its mirrors that works, into a `flnd._files.Part`.
- `follow(url)`: where a short link leads, from one HEAD request (blocking, for any thread).
- `fake(routes)`: answers every request in the block from routes or a handler, never the
  network; `reply(...)` builds an answer for a handler.

    limits = Limits(paced=lambda host: host.endswith("example.com"), rate=2.0,
                    caps={"cdn.example.com": 4})
    session = Session(headers={"User-Agent": agent}, cookies=jar, prepare=sign,
                      handles=limits.total)
    client = Client(session, limits=limits, stop=event)
    page = await client.fetch("https://example.com/api", body={"q": "x"})   # page.json
    with _files.part(dest, resume=True) as held:
        headers = await client.fill([mirror1, mirror2], held, progress=row)
    await session.close()

Who calls it: flnd's site (`flnd.site`) builds one session per source and two clients over it,
and flnd's transfers (`flnd._transfer`) fill a post's files; the browser (`flnd._browser`) shapes
a request its page sent as a `Response`. It calls curl_cffi, `flnd._files` (parts), `flnd._time`
(the clock, so tests wait on a fake one), `flnd._cookies`, `flnd._text` (redaction) and
`flnd.errors`. curl_cffi is imported on the first request, so importing this module is cheap.

The main flow of one request (`Client.call`):
1. take a slot for the URL's host from `Limits`: a token from a paced host's bucket, or a place
   under another host's cap, held for the whole transfer;
2. build the request (`body` is a query on a GET, JSON for a dict or list on any other method)
   and let the session's `prepare` hook add its headers, before every try;
3. send it through curl_cffi, or the active `fake`, and stream the answer;
4. classify a failure by the policy below, wait, and try again, or raise.

The retry policy (docs/errors):
- a timeout, a reset, a cut-off transfer or a 5xx is tried again up to `RETRIES` more times,
  after jittered waits of about 1, 2, 4 and 8 s; only methods that are safe to repeat are;
- a 429 is `Blocked`: its `Retry-After`, or the site's rate-limit reset, is waited out (up to an
  hour), a paced host's bucket is held and its rate halved until `CLEAN` clean requests; the
  `ROUNDS`-th 429 in a row raises it;
- a 404 or 410 is `NotFound`, any other 4xx a `SiteError`; neither is tried again;
- a malformed URL, a name that doesn't resolve or a certificate that fails is a `NetworkError`
  that isn't tried again; a write the disk refuses is a `DiskError`; a set `stop` is `Stopped`.
A host under a cap also tells its `Cap` how each try went:
- it starts at `cap` (8 by default) transfers at once, and is halved on a 429, a 503, a reset, a
  timeout or a cut-off transfer: once per round of transfers, so a burst of failures that
  started together halves it once, not once each;
- every `STEP` clean transfers it measures the host's throughput (the cap times the bytes those
  transfers moved over the seconds they took), and adds one place, up to `most` (32 by
  default), while each step still raised the throughput by at least half of what one more
  place would add on a host that scales;
- a step that didn't is taken back, and the cap holds for `HOLD` clean transfers before it
  probes again; a window in which the host wasn't kept busy (a place stood free with nobody
  waiting) says nothing about the cap, and is dropped.
The session's handles (`Limits.total`) leave room for `SPREAD` hosts at their most at once, and
every host with a cap of its own, so curl doesn't hold transfers below the caps.

Every message names the URL redacted (`flnd._text.redact`), and any secret the request carried
(a cookie, a token, a query value) is scrubbed from a library's error text.

Rules a change must keep:
- A session belongs to one asyncio loop: every coroutine here runs on it, and nothing here
  blocks it. File writes go to one writer thread, `BUFFER` bytes at a time, so a slow disk never
  stalls the loop (and the browser and every other transfer on it); see docs/performance.
- A write in flight on the writer thread always finishes before its transfer gives the part
  back, even when the transfer is cancelled: the part's descriptor is closed only after it.
- `Limits`, its buckets and its caps are used from the loop's thread only, so they need no
  lock.
"""

from __future__ import annotations

import asyncio
import email.message
import email.utils
import http.cookiejar
import inspect
import math
import random
import re
import urllib.request
from collections import deque
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
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import msgspec

from flnd import _cookies, _files, _text, _time
from flnd._ui import ui
from flnd.errors import Blocked, Changed, Error, NetworkError, NotFound, SiteError, Stopped, disk

if TYPE_CHECKING:
    from flnd._html import Document

__all__ = [
    "Cap", "Client", "Headers", "Limits", "Load", "Pause", "Place", "Reply", "Request",
    "Response", "Session", "URL", "fake", "follow", "reply",
]

A = TypeVar("A")

# More tries after the first, for a failure worth one: waits of about 1, 2, 4 and 8 s between.
RETRIES = 4
FIRST_WAIT = 1.0
MAX_WAIT = 8.0
# How many 429s in a row a request meets before it raises `Blocked`.
ROUNDS = 3
# The longest Retry-After or rate-limit reset a client waits out, in seconds.
MAX_BACKOFF = 3600.0
# A reset larger than this is a Unix time, not a number of seconds.
EPOCH = 1_000_000_000.0
# After a 429, a paced host's rate stays halved for this many clean requests.
CLEAN = 20
# One token at most: a rate is a floor between two requests to a host, never a burst after a lull.
BURST = 1.0
# Transfers at once to a host that isn't paced and has no cap of its own: where its `Cap`
# starts, and the most it grows to. 8 is what one host took at once before caps adapted; 32 is
# the ceiling the real-CDN check (docs/performance, "Concurrency per host") is to confirm.
CAP = 8
MOST = 32
# A cap measures its host's throughput, and may grow by one, every STEP clean transfers.
STEP = 10
# A step must raise the throughput by this share of what one more place adds on a host that
# scales linearly (1 / the old cap), or it is taken back: a half, so noise isn't growth.
RISE = 0.5
# After a step that was taken back, clean transfers before the cap probes again.
HOLD = 100
# The reasons `Limits.pauses` gives for a wait: the site said when to come back; a refusal
# halved the pace, or a failure is waited out before it is tried again; the host's own pace.
RETRY_AFTER = "Retry-After"
BACK_OFF = "back-off"
PACE = "pace"
# How often, in seconds, a request held by `Limits.suspended` looks again.
SUSPENDED_POLL = 0.1
# Statuses that say a host is overloaded, so its cap halves (with a reset and a timeout).
OVERLOADED = (429, 503)
# Room in the session for requests to the paced hosts, on top of the caps (`Limits.total`).
PAGES = 4
# Hosts without a cap of their own that `Limits.total` leaves room for at their most at once.
# curl makes a handle only when one is needed, so room that is never used costs nothing.
SPREAD = 8
# Seconds before a connection counts as dead: to connect, or with no byte received.
TIMEOUT = 30.0
# The file system's writes go to the writer thread this many bytes at a time.
BUFFER = 1 << 20
# The most bytes of one transfer that wait in memory for the disk before curl pauses it.
HELD = 4 << 20
# A file this big, on a server that serves ranges, may come in SEGMENTS parallel ranges.
SEGMENTED = 64 << 20
SEGMENTS = 4
# Redirects followed for one request, as curl_cffi's default allows.
REDIRECTS = 30
# The statuses that mean the item is gone for good: never retried, recorded as gone.
GONE = (404, 410)
# Methods a server may receive twice without harm, so a failure of theirs is tried again.
SAFE = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
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
    """

    method: str
    url: URL
    headers: Headers = field(default_factory=Headers)
    content: bytes | None = None

    def __post_init__(self) -> None:
        # A str and a dict are taken too, as a test or a script writes them.
        self.method = self.method.upper()
        if not isinstance(self.url, URL):
            self.url = URL(self.url)
        if not isinstance(self.headers, Headers):
            self.headers = Headers(self.headers)


class _Read(Mapping[str, str]):
    """A response's headers: read-only, by name in any case.

    Args:
        headers: the headers, copied.
    """

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
        """The body parsed as JSON. Raises `flnd.errors.Changed` when it isn't JSON."""
        return self._cached("json", self._json)

    @property
    def html(self) -> Document:
        """The body parsed as HTML: a `flnd._html.Document`."""
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


# ---------------------------------------------------------------------------------------------
# Pace and caps
# ---------------------------------------------------------------------------------------------


class Bucket:
    """A token bucket: `rate` requests a second, one token deep, held by a site's wait.

    It refills by the time elapsed, so a slow caller never waits again. A `hold` (a 429's
    `Retry-After`) gives no token before its end, and a `slow` halves the rate until `CLEAN`
    clean requests have passed.

    Args:
        rate: requests a second.
    """

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, not {rate}")
        self.rate = rate
        self._tokens = BURST
        self._at: float | None = None
        self._until = 0.0
        self._slow = 0

    async def take(self, stop: Any = None) -> None:
        """Wait for a token, and take it.

        A token is taken at once, so callers waiting together queue one interval apart.

        Args:
            stop: a `threading.Event` that ends the wait.

        Raises:
            Stopped: `stop` was set while waiting.
        """
        rate = self.rate / 2 if self._slow else self.rate
        now = _time.now()
        at = now if self._at is None else self._at
        self._tokens = min(BURST, self._tokens + (now - at) * rate) - 1.0
        self._at = now
        wait = max(-self._tokens / rate, self._until - now)
        if wait > 0 and not await _time.pause(wait, stop=stop):
            raise Stopped("stopped while pacing")

    def hold(self, seconds: float) -> None:
        """Give no token for `seconds` from now: the site said when to come back."""
        self._until = max(self._until, _time.now() + seconds)

    def slow(self) -> None:
        """Halve the rate until `CLEAN` clean requests have passed: the site refused one."""
        self._slow = CLEAN

    def clean(self) -> None:
        """Count one request that went through."""
        if self._slow:
            self._slow -= 1

    def ahead(self, now: float) -> tuple[float, str]:
        """How long a request made at `now` would wait for its token, and why.

        Read only: it computes what `take` would, without taking anything.

        Args:
            now: the time, on `flnd._time`'s clock.

        Returns:
            The seconds (0 when a token is there), and `RETRY_AFTER` while a site's hold is the
            longer wait, `BACK_OFF` while the rate is halved, else `PACE`.
        """
        rate = self.rate / 2 if self._slow else self.rate
        tokens = BURST if self._at is None else min(BURST, self._tokens + (now - self._at) * rate)
        paced = max(0.0, (1.0 - tokens) / rate)
        held = self._until - now
        if held > 0 and held >= paced:
            return held, RETRY_AFTER
        return paced, BACK_OFF if self._slow else PACE


@dataclass(frozen=True)
class Pause:
    """One wait before the next request to a host, as `Limits.pauses` shows it.

    Attributes:
        host: the host.
        seconds: how long from now.
        reason: `RETRY_AFTER`, `BACK_OFF` or `PACE`.
    """

    host: str
    seconds: float
    reason: str


@dataclass(frozen=True)
class Load:
    """One host's transfers now, as `Limits.load` shows them.

    Attributes:
        cap: the transfers at once the host may have now.
        busy: the transfers in flight to it.
        waiting: the transfers waiting for a place.
        most: the most its cap may grow to.
    """

    cap: int
    busy: int
    waiting: int
    most: int


class Cap:
    """Transfers at once to one host, adapted to what the host can take (AIMD).

        cap = Cap(8, most=32)
        round = await cap.take()        # waits while `cap.value` transfers are in flight
        try:
            ...                         # the transfer
            cap.done(size, seconds)     # it went through: its bytes, and how long it took
        except Error:                   # for a 429, a 503, a reset or a timeout only:
            cap.failed(round)           # halve
        finally:
            cap.give()

    How the cap moves:
    1. It starts at `start` and stays between 1 and `most`.
    2. A failure halves it, rounded down, but once per round: `take` returns the round a
       transfer started in, and only a failure of the current round halves it (and starts the
       next). Transfers that started together and fail together halve it once.
    3. Every `STEP` clean transfers close a window. Its throughput is what Little's law gives a
       host kept busy: the cap times the bytes those transfers moved over the seconds they took,
       each from its place to its last byte. Bytes over the wall time of so few transfers would
       depend on where in their run the window closed; this doesn't.
    4. A window in which a place stood free with nobody waiting (`give` found no one) wasn't
       bound by the cap, so it says nothing about it: it is dropped.
    5. Otherwise, if the window before it grew the cap, this one judges that step: a rate that
       didn't rise by `RISE` of what one more place adds on a host that scales (1 / the old
       cap) takes the step back, and holds the cap for `HOLD` clean transfers.
    6. Else the rate is kept for the next judgement and, unless held or at `most`, the cap
       grows by one.
    A failure also forgets the rate and any hold, so growth starts afresh from the halved cap.

    Used from the loop's thread only. Places are handed over in the order they were asked for.

    Args:
        start: transfers at once to begin with; more than `most` starts at `most`.
        most: the most transfers at once it grows to.
    """

    def __init__(self, start: int, *, most: int) -> None:
        if start < 1 or most < 1:
            raise ValueError(f"a cap needs a start and a most of 1 or more, not {start}, {most}")
        self.most = most
        self.value = min(start, most)
        self.busy = 0
        self._waiting: deque[asyncio.Future[None]] = deque()
        # The round transfers start in; a failure of the current round halves and ends it.
        self._round = 0
        # The open window: its clean transfers, their bytes and seconds, and whether a place
        # stood free in it.
        self._clean = 0
        self._bytes = 0
        self._seconds = 0.0
        self._slack = False
        # The throughput at `value` from the last window that counted, in bytes a second.
        self._rate: float | None = None
        # Whether the last window ended by growing the cap, so the next one judges that step.
        self._grown = False
        # Clean transfers left before a cap whose step was taken back probes again.
        self._held = 0

    @property
    def waiting(self) -> int:
        """Transfers waiting for a place."""
        return len(self._waiting)

    async def take(self) -> int:
        """Wait for a place, and take it.

        Returns:
            The round the transfer starts in, for `failed`.
        """
        if self.busy < self.value and not self._waiting:
            self.busy += 1
            return self._round
        place: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiting.append(place)
        try:
            await place
        except asyncio.CancelledError:
            if place.done() and not place.cancelled():
                # The place was handed over just as the waiter was cancelled: pass it on.
                self.give()
            else:
                self._waiting.remove(place)
            raise
        return self._round

    def give(self) -> None:
        """Give a place back, to the first transfer waiting when the cap allows."""
        self.busy -= 1
        if not self._waiting:
            self._slack = True
        self._wake()

    def done(self, size: int, seconds: float) -> None:
        """Count a clean transfer; the `STEP`-th closes the window.

        Args:
            size: the bytes it moved; 0 for none.
            seconds: how long it held its place.
        """
        self._clean += 1
        self._bytes += size
        self._seconds += seconds
        if self._held:
            self._held -= 1
        if self._clean >= STEP:
            self._judge()

    def failed(self, round: int) -> None:
        """A transfer that started in `round` met an overloaded host: halve, once a round.

        Args:
            round: what `take` returned for it.
        """
        if round != self._round:
            return
        self._round += 1
        self.value = max(1, self.value // 2)
        self._rate, self._grown, self._held = None, False, 0
        self._open()

    def _judge(self) -> None:
        """Close the window: drop it, take its step back, or grow (the class's steps 4 to 6)."""
        rate = self.value * self._bytes / self._seconds if self._seconds > 0 else None
        grown, self._grown = self._grown, False
        slack = self._slack
        self._open()
        if slack or rate is None:
            return
        if grown and self._rate is not None and \
                rate <= self._rate * (1 + RISE / (self.value - 1)):
            self.value -= 1
            self._held = HOLD
            return
        self._rate = rate
        if self._held or self.value >= self.most:
            return
        self.value += 1
        self._grown = True
        self._wake()

    def _open(self) -> None:
        """Open a new window."""
        self._clean = self._bytes = 0
        self._seconds = 0.0
        self._slack = False

    def _wake(self) -> None:
        """Hand free places to the transfers waiting, first come first served."""
        while self._waiting and self.busy < self.value:
            place = self._waiting.popleft()
            if not place.done():
                self.busy += 1
                place.set_result(None)


class Limits:
    """How hard a session may press each host: a bucket for paced hosts, a cap for the others.

    Each paced host has a bucket of its own, so a site's page host and its API host keep their
    own pace, and a 429 from one doesn't slow the other. Every other host has a `Cap` of its
    own, which adapts to what the host takes.

        limits = Limits(paced=lambda host: host.endswith("example.com"), rate=2.0,
                        caps={"cdn.example.com": 4})
        async with limits.slot("cdn.example.com") as place:
            ...                     # at most 4 transfers to that host at once
            place.done(size)        # a clean transfer, for the cap's measure

    A client that doesn't pace (`Client(pace=False)`, for downloads) treats a paced host as any
    other: under its cap. So a site's media served from its own domain isn't held to its pace.

    Args:
        paced: whether a host is paced: its requests share its bucket at `rate`, with no cap.
        rate: requests a second to the paced hosts; None paces nothing.
        caps: the most transfers at once to a host and its subdomains, by host name. Each such
            host starts at `cap`, or at its own when that is lower, and grows no further.
        cap: transfers at once a host starts at.
        most: the most transfers at once a host not in `caps` grows to; `most=cap` keeps every
            such host at `cap` until a failure halves it.
    """

    def __init__(self, *, paced: Callable[[str], bool] = lambda host: False,
                 rate: float | None = None, caps: Mapping[str, int] | None = None,
                 cap: int = CAP, most: int = MOST) -> None:
        self._paced = paced
        self.rate = rate
        self._buckets: dict[str, Bucket] = {}
        self.caps = dict(caps or {})
        self.cap = cap
        self.most = max(cap, most)
        self._caps: dict[str, Cap] = {}
        # Waits a client is sitting out before trying a host again, by a key of their own:
        # (host, until, reason). `holding` adds and removes them.
        self._held: dict[object, tuple[str, float, str]] = {}
        # When set (a `threading.Event`), no new request starts until it is cleared: each
        # `slot` waits first. The transfers in flight go on. None never holds.
        self.suspended: Any = None

    @property
    def total(self) -> int:
        """Transfers at once in all, for the session's handles: `SPREAD` hosts at `most`, every
        host in `caps` at its own, and room for the paced hosts."""
        return PAGES + SPREAD * self.most + sum(self.caps.values())

    @property
    def load(self) -> Mapping[str, Load]:
        """A snapshot of each capped host's cap, and its transfers in flight and waiting."""
        return MappingProxyType({
            host: Load(cap.value, cap.busy, cap.waiting, cap.most)
            for host, cap in self._caps.items()
        })

    @property
    def pauses(self) -> tuple[Pause, ...]:
        """A snapshot of each wait before a host's next request, the longest first.

        A paced host's bucket gives the time to its next token and why (a site's hold, a halved
        rate, the pace); a client waiting out a 429 or a failure gives what is left of it. A
        host with nothing to wait for isn't listed.
        """
        now = _time.now()
        found = [Pause(host, seconds, reason)
                 for host, bucket in self._buckets.items()
                 for seconds, reason in [bucket.ahead(now)] if seconds > 0]
        found += [Pause(host, until - now, reason)
                  for host, until, reason in self._held.values() if until > now]
        return tuple(sorted(found, key=lambda pause: -pause.seconds))

    @contextmanager
    def holding(self, host: str, seconds: float, reason: str) -> Iterator[None]:
        """Show a wait before trying `host` again in `pauses`, for the block.

        Args:
            host: the host waited for.
            seconds: how long the wait is, from now.
            reason: why: `RETRY_AFTER` or `BACK_OFF`.

        Yields:
            Nothing; the wait shows until the block ends.
        """
        key = object()
        self._held[key] = (host, _time.now() + seconds, reason)
        try:
            yield
        finally:
            del self._held[key]

    @asynccontextmanager
    async def slot(self, host: str, stop: Any = None, *,
                   pace: bool = True) -> AsyncIterator[Place]:
        """Hold a place for one request to `host` for the block.

        Args:
            host: the host, lower case.
            stop: a `threading.Event` that ends a wait for a token.
            pace: whether a paced host takes a token; False puts it under its cap instead.

        Yields:
            The place: tell it how the try went (`done`, `failed`), for the host's cap. A
            block that tells it nothing counts for nothing.

        Raises:
            Stopped: `stop` was set while waiting for a token, or while `suspended`.
        """
        # A suspension is a person's (a key pressed), so it is waited out in real time, even
        # under a test's fake clock, whose waits would pass at once and never let the loop run.
        while self.suspended is not None and self.suspended.is_set():
            if stop is not None and stop.is_set():
                raise Stopped("stopped while suspended")
            await asyncio.sleep(SUSPENDED_POLL)
        if pace and self._paced(host):
            bucket = self._bucket(host)
            if bucket is not None:
                await bucket.take(stop)
            yield Place(None, 0, 0.0)
            return
        cap = self._caps.get(host)
        if cap is None:
            cap = self._caps[host] = Cap(self.cap, most=self._most(host))
        round = await cap.take()
        try:
            yield Place(cap, round, _time.now())
        finally:
            cap.give()

    def blocked(self, host: str, wait: float | None) -> None:
        """A 429 from `host`: hold a paced host's bucket for `wait`, and halve its rate.

        Args:
            host: the host that answered 429, lower case; one that isn't paced is left alone.
            wait: the seconds the site asked for; None holds nothing, but still halves.
        """
        bucket = self._bucket(host)
        if bucket is not None:
            if wait:
                bucket.hold(wait)
            bucket.slow()

    def clean(self, host: str) -> None:
        """A request to `host` went through."""
        bucket = self._bucket(host)
        if bucket is not None:
            bucket.clean()

    def _most(self, host: str) -> int:
        """The most transfers at once to `host`: its own cap, a parent domain's, or `most`."""
        for name, value in self.caps.items():
            if host == name or host.endswith(f".{name}"):
                return value
        return self.most

    def _bucket(self, host: str) -> Bucket | None:
        """A paced host's bucket, made on first use.

        Args:
            host: the host, lower case.

        Returns:
            Its bucket; None for a host that isn't paced, or when nothing is.
        """
        if not self.rate or not self._paced(host):
            return None
        bucket = self._buckets.get(host)
        if bucket is None:
            bucket = self._buckets[host] = Bucket(self.rate)
        return bucket


@dataclass
class Place:
    """A place `Limits.slot` holds: what a try tells its host's cap, when it has one.

    Attributes:
        cap: the host's cap; None for a paced host, whose bucket learns from `Limits` instead.
        round: the cap's round the place was taken in.
        began: when the place was taken, on `flnd._time`'s clock.
    """

    cap: Cap | None
    round: int
    began: float

    def done(self, size: int) -> None:
        """The try went through, moving `size` bytes."""
        if self.cap is not None:
            self.cap.done(size, _time.now() - self.began)

    def failed(self, error: BaseException) -> None:
        """The try failed: halve the cap when `error` says the host is overloaded.

        Overloaded is a 429 or a 503, or a retryable `NetworkError`: a reset, a timeout or a
        transfer cut short. Any other failure (a 404, a 500, a URL that is wrong) leaves the
        cap alone, since fewer transfers at once wouldn't mend it.

        Args:
            error: what the try raised.
        """
        if self.cap is None:
            return
        if getattr(error, "status", None) in OVERLOADED or (
                isinstance(error, NetworkError) and error.retryable):
            self.cap.failed(self.round)


# ---------------------------------------------------------------------------------------------
# The session: curl_cffi, cookies, and the fake
# ---------------------------------------------------------------------------------------------


@dataclass
class _Answer:
    """An answer whose body is still coming: what `Session.send` gives back.

    Attributes:
        status: the HTTP status.
        url: where it came from, after redirects.
        headers: the response's headers, lower case, repeated ones joined by ", ".
        chunks: the body, as it arrives; a cut connection raises a retryable `NetworkError`.
        close: stops the transfer; call it when done, read whole or not.
        set_cookies: each `Set-Cookie` header's value, for the session's jar.
    """

    status: int
    url: str
    headers: Mapping[str, str]
    chunks: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]
    set_cookies: list[str] = field(default_factory=list)

    async def read(self) -> Response:
        """Read the rest of the body and close the answer.

        Returns:
            The whole answer, as a `Response`.
        """
        try:
            body = b"".join([chunk async for chunk in self.chunks])
        finally:
            await self.close()
        return Response(self.status, self.url, self.headers, body)


class Session:
    """One curl_cffi session on an asyncio loop, with default headers, cookies and a hook.

        session = Session(headers={"User-Agent": agent}, cookies=jar, prepare=sign)
        ...                                   # clients send through it on the loop
        await session.close()

    Cookies are kept here, not in curl: each request carries the ones its URL matches, and a
    `Set-Cookie` the answer brings is kept for the next. So a fake and the network keep them the
    same way.

    Args:
        headers: sent with every request, under the request's own.
        cookies: the cookies to start with (`flnd._cookies.Jar`, or cookie dicts in its shape).
        prepare: called with each `Request` before each try, to add headers; it may return
            another `Request`, or None to keep its argument.
        timeout: seconds to connect, and seconds with no byte received, before a transfer
            counts as dead.
        handles: transfers at once in all; more wait for a handle. curl makes a handle only
            when a transfer needs one, so a large number costs nothing until it is used; give
            `Limits.total`, so the session never holds transfers below the caps.
        proxy: the proxy every request goes through, as curl reads one
            (`http://host:port`, `socks5://host:port`, `socks5h://…` to look names up there
            too); None for none. A fake (`fake`) answers without it.
    """

    def __init__(self, *, headers: Mapping[str, str] | None = None,
                 cookies: Iterable[Mapping[str, Any]] = (),
                 prepare: Callable[[Request], Request | None] | None = None,
                 timeout: float = TIMEOUT, handles: int = PAGES + SPREAD * MOST,
                 proxy: str | None = None) -> None:
        self.headers = Headers(headers or {})
        self.prepare = prepare
        self.timeout = timeout
        self.handles = handles
        self.proxy = proxy
        self.jar = http.cookiejar.CookieJar()
        self.add(cookies)
        self._raw: Any = None

    @property
    def cookies(self) -> _cookies.Jar:
        """The session's cookies now, as a `flnd._cookies.Jar`."""
        return _cookies.Jar(_cookie_dict(cookie) for cookie in self.jar)

    @property
    def started(self) -> bool:
        """Whether curl's session was made: a request went out, so `close` has work to do."""
        return self._raw is not None

    @property
    def names(self) -> Mapping[str, str]:
        """The cookies as `{name: value}`; the last wins for a name on two domains."""
        return MappingProxyType({cookie.name: cookie.value or "" for cookie in self.jar})

    def add(self, cookies: Iterable[Mapping[str, Any]]) -> None:
        """Keep `cookies` (dicts in `flnd._cookies.Jar`'s shape), each replacing its namesake."""
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
        sent = Request(request.method, request.url, headers, request.content)
        router = _FAKES[-1] if _FAKES else None
        if router is not None:
            answer = await router.answer(sent)
        else:
            answer = await self._curl(sent)
        self._keep(answer, sent.url)
        return answer

    def _cookie_header(self, url: str) -> str:
        """The `Cookie` header the jar gives `url`, or ""."""
        probe = urllib.request.Request(_bare(url))
        self.jar.add_cookie_header(probe)
        return probe.get_header("Cookie") or ""

    def _keep(self, answer: _Answer, url: str) -> None:
        """Keep the cookies an answer set, for the requests after it.

        Args:
            answer: the answer, with its `Set-Cookie` values.
            url: the URL asked for, when the answer doesn't say where it came from.
        """
        if not answer.set_cookies:
            return
        info = email.message.Message()
        for value in answer.set_cookies:
            info["Set-Cookie"] = value
        self.jar.extract_cookies(_Info(info), urllib.request.Request(_bare(answer.url or url)))

    def _session(self) -> Any:
        """curl_cffi's `AsyncSession`, made on first use, on the running loop.

        Returns:
            The session, with Chrome's fingerprint, `handles` curl handles and the proxy.

        Raises:
            ImportError: curl_cffi isn't installed, with the command that installs it.
        """
        if self._raw is None:
            try:
                from curl_cffi.requests import AsyncSession
            except ImportError:
                raise ImportError("flnd._net needs curl_cffi: pip install curl_cffi") from None

            # `proxy` (None for none) is curl's CURLOPT_PROXY for every transfer of the session.
            self._raw = AsyncSession(max_clients=self.handles, impersonate=IMPERSONATE,
                                     proxy=self.proxy)
        return self._raw

    async def _curl(self, request: Request) -> _Answer:
        """Send `request` through curl_cffi, streaming the answer.

        Cookies are never left to curl (`discard_cookies`): the session keeps them itself.

        Args:
            request: the request, with every header it goes out with.

        Returns:
            The answer, once its headers came; its body streams in `chunks`.

        Raises:
            NetworkError: no answer came (see `_network`).
        """
        from curl_cffi import CurlError

        raw = self._session()
        try:
            answer = await raw.request(
                request.method, str(request.url), headers=dict(request.headers),
                data=request.content, stream=True, timeout=self.timeout, allow_redirects=True,
                discard_cookies=True,
            )
        except CurlError as error:
            raise _network(request, error) from None
        valve = _Valve(answer)

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in answer.aiter_content():
                    valve.took(len(chunk))
                    yield chunk
            except CurlError as error:
                raise _network(request, error) from None

        async def close() -> None:
            # Setting `quit_now` makes curl's write callback abort a transfer still running;
            # `aclose` then waits for curl to give the handle back. A paused transfer would
            # never reach that callback, so it is let go first.
            valve.open()
            if answer.quit_now is not None:
                answer.quit_now.set()
            try:
                await answer.aclose()
            except CurlError:
                pass

        pairs = [(name.lower(), value) for name, value in answer.headers.multi_items()]
        return _Answer(answer.status_code, str(answer.url), _joined(pairs), chunks(), close,
                       [value for name, value in pairs if name == "set-cookie"])


class _Valve:
    """Holds a streamed transfer while its body waits for the disk: at most `HELD` bytes.

    curl_cffi puts each chunk curl receives into the answer's queue, which has no bound: on a
    disk slower than the network, a whole file would wait there in memory. So the queue's
    `put_nowait` is wrapped: once `HELD` bytes wait, the transfer is paused
    (`curl_easy_pause`, which libcurl allows from its write callback), and once the reader
    has taken them down to `HELD / 2`, it goes on. libcurl keeps the socket open meanwhile,
    and TCP's own window holds the server back.

    All of it runs on the loop's thread: curl's callbacks are called from `socket_action` on
    the loop, and the reader is a coroutine on it.

    Args:
        answer: curl_cffi's streaming response.
    """

    def __init__(self, answer: Any) -> None:
        self._answer = answer
        self._held = 0
        self._paused = False
        queue = answer.queue
        if queue is None or answer.curl is None:
            self._put = None
            return
        self._put = queue.put_nowait

        def put(item: Any) -> None:
            if isinstance(item, bytes):
                self._held += len(item)
                if self._held >= HELD and not self._paused:
                    self._pause(True)
            self._put(item)  # type: ignore[misc]

        queue.put_nowait = put

    def took(self, size: int) -> None:
        """The reader took `size` bytes; let the transfer go on when few enough wait."""
        self._held -= size
        if self._paused and self._held <= HELD // 2:
            self._pause(False)

    def open(self) -> None:
        """Let a paused transfer go, for good: it is closing."""
        if self._paused:
            self._pause(False)

    def _pause(self, paused: bool) -> None:
        self._paused = paused
        # 1 is CURLPAUSE_RECV, 0 CURLPAUSE_CONT (curl/curl.h).
        _curl_pause(self._answer.curl._curl, 1 if paused else 0)


def _curl_pause(handle: Any, how: int) -> None:
    """libcurl's `curl_easy_pause` on a curl_cffi handle (`how` as curl/curl.h names it)."""
    from curl_cffi._wrapper import lib

    lib.curl_easy_pause(handle, how)


class _Info:
    """What `http.cookiejar` reads from a response: its headers, by `info()`.

    Args:
        message: the response's headers.
    """

    def __init__(self, message: email.message.Message) -> None:
        self._message = message

    def info(self) -> email.message.Message:
        return self._message


# ---------------------------------------------------------------------------------------------
# The client: pace, retries, downloads
# ---------------------------------------------------------------------------------------------


class Client:
    """A session with limits, a retry policy and a stop event: what fetches and downloads.

        client = Client(session, limits=limits, stop=event)
        page = await client.fetch(url)
        await client.fill(url, held, progress=row)

    Two clients may share a session and its limits: flnd lists through one that stops at the
    first Ctrl-C and downloads through one that stops at the second.

    Args:
        session: what sends.
        limits: the pace and caps per host; None limits nothing.
        retries: more tries after the first, for a failure worth one (the module's policy).
        stop: a `threading.Event` that stops waits and transfers with `Stopped`.
        segments: fetch a file of `SEGMENTED` bytes or more, on a server that serves ranges, in
            `SEGMENTS` parallel ranges. Off by default: whether it pays depends on the server.
        pace: hold requests to the paced hosts to their bucket; False caps them instead, as
            any other host (a downloading client).
    """

    def __init__(self, session: Session, *, limits: Limits | None = None,
                 retries: int = RETRIES, stop: Any = None, segments: bool = False,
                 pace: bool = True) -> None:
        if retries < 0:
            raise ValueError(f"retries must be 0 or more, not {retries}")
        self.session = session
        self.limits = limits if limits is not None else Limits()
        self.retries = retries
        self.stop = stop
        self.segments = segments
        self.pace = pace

    async def fetch(self, url: str, *, method: str = "GET",
                    headers: Mapping[str, str] | None = None, body: Any = None,
                    check: bool = True) -> Response:
        """Send one request, paced and retried, and return its `Response`.

        Args:
            url: where to.
            method: the method.
            headers: headers for this request, over the session's.
            body: on a GET or HEAD a dict is the query; on any other method a dict or list is
                JSON; bytes or str go as they are.
            check: raise for a 4xx or 5xx (the module's policy); False returns it instead.

        Returns:
            The response.

        Raises:
            Error: as the module's policy says.
        """
        method = method.upper()

        async def send() -> Response:
            answer = await self._open(_request(method, url, headers, body))
            return _checked(await answer.read(), method)

        return await self.call(url, send, method=method, check=check)

    async def call(self, url: str, send: Callable[[], Awaitable[A]], *, method: str = "GET",
                   check: bool = True, moved: Callable[[], int] | None = None) -> A:
        """Pace and send, trying again as the policy allows; `check=False` returns a refusal.

        `send` makes a new attempt each call: a request, or anything else sent to `url`'s host
        (a page loaded in a browser), which raises flnd's errors as this module does.

        Args:
            url: what the attempt is for; its host picks the pace or the cap.
            send: one attempt.
            method: the attempt's method: only the safe ones are tried again after a failure,
                but any after a 429, which the server didn't act on.
            check: False returns the response of a refusal that isn't tried again.
            moved: the bytes the attempts have moved so far, read before and after each one,
                for the host's cap to measure its throughput; None counts a `Response`'s body,
                and nothing for anything else.

        Returns:
            What the attempt that went through returned.

        Raises:
            Error: the last attempt's failure.
            Stopped: `stop` was set while waiting.
        """
        host = _host(url)
        tries = rounds = 0
        while True:
            async with self.limits.slot(host, self.stop, pace=self.pace) as place:
                before = moved() if moved is not None else 0
                try:
                    result = await send()
                except Error as error:
                    failure = error
                    place.failed(error)
                else:
                    if moved is not None:
                        place.done(moved() - before)
                    else:
                        place.done(len(result.content) if isinstance(result, Response) else 0)
                    if self.pace:
                        self.limits.clean(host)
                    return result
            if getattr(failure, "status", None) == 429:
                rounds += 1
                wait = getattr(failure, "wait", None)
                if self.pace:
                    self.limits.blocked(host, wait)
                if rounds >= ROUNDS:
                    return _given_up(failure, check)
                reason = BACK_OFF if wait is None else RETRY_AFTER
                wait = _backoff(rounds) if wait is None else wait
                ui.warn("{host}: rate limited; waiting {time}",
                        host=host, time=_text.duration(round(wait)))
            else:
                tries += 1
                if tries > self.retries or not _worth(method, failure):
                    return _given_up(failure, check)
                reason = BACK_OFF
                wait = _backoff(tries)
                ui.debug("retry {n}/{total} in {seconds:.1f}s: {reason}",
                         n=tries, total=self.retries, seconds=wait, reason=failure)
            with self.limits.holding(host, wait, reason):
                if not await _time.pause(wait, stop=self.stop):
                    raise Stopped(f"{host}: stopped")

    async def fill(self, url: str | Sequence[str], held: _files.Part, *,
                   headers: Mapping[str, str] | None = None, resume: bool = True,
                   progress: Any = None) -> Mapping[str, str]:
        """Stream `url`, or the first of several mirrors that works, into the open part `held`.

        The caller owns the part: it decides where it lands and what happens on failure.

        Args:
            url: the file's URL, or mirrors of it tried in order.
            held: the locked part, from `flnd._files.part`.
            headers: extra request headers.
            resume: go on from the bytes the part holds, with `If-Range` on the validator its
                sidecar kept; False empties it first.
            progress: a sink for the bytes as they come: anything with a settable `total` and
                an `advance(n)`; None reports nothing.

        Returns:
            The headers of the answer that filled the part, lower case.

        Raises:
            Error: the last mirror failed (see the module's policy).
        """
        mirrors = [url] if isinstance(url, str) else list(url)
        if not mirrors:
            raise ValueError("a download needs a URL")
        meter = _Meter(progress)
        for mirror in mirrors[:-1]:
            try:
                return await self.call(
                    mirror, lambda m=mirror: self._file(m, held, headers, resume, meter),
                    moved=meter.moved)
            except Error as error:
                ui.debug("{url} failed: {reason}; trying the next mirror",
                         url=_text.redact(mirror), reason=error)
                # The next mirror may serve other bytes: a Range on this part would splice.
                held.reset()
        last = mirrors[-1]
        return await self.call(last, lambda: self._file(last, held, headers, resume, meter),
                               moved=meter.moved)

    async def _open(self, request: Request) -> _Answer:
        """Prepare `request` with the session's hook, send it, and raise for its stop.

        Args:
            request: the request, before the hook.

        Returns:
            The answer, streaming.
        """
        self._check(request.url)
        prepare = self.session.prepare
        if prepare is not None:
            request = prepare(request) or request
        return await self.session.send(request)

    async def _file(self, url: str, held: _files.Part, headers: Mapping[str, str] | None,
                    resume: bool, meter: _Meter) -> Mapping[str, str]:
        """One try at a file: resume the part, or start over, or fetch it in segments.

        1. Start from 0 when not resuming, or when a crash left a segmented part with holes.
        2. Ask for `bytes=<size>-`, with `If-Range` on the validator the part kept, so a file
           that changed comes whole (a 200) instead of spliced.
        3. A 206 goes on from the part's end; a 200 starts it over; a 416 is a whole part or a
           restart; a big whole file on a server with ranges comes in segments, when on.

        Args:
            url: the file.
            held: the locked part it goes into.
            headers: extra request headers.
            resume: go on from the part's bytes; False empties it first.
            meter: where the bytes are counted.

        Returns:
            The answer's headers.
        """
        if not resume or (held.size and held.meta.get("holes")):
            held.reset()
        offset = held.size
        validator = held.meta.get("etag") if offset else None
        answer = await self._range(url, headers, f"bytes={offset}-", validator)
        try:
            status = answer.status
            start = offset if status == 206 else 0
            total = _total_bytes(answer.headers, start)
            if status != 416:
                if not start and held.size:
                    held.reset()
                held.note(etag=_validator(answer.headers))
            whole = (self.segments and status == 206 and start == 0 and total is not None
                     and total >= SEGMENTED)
            if status != 416 and not whole:
                await self._write(url, answer, held, start, total, meter)
                return answer.headers
        finally:
            await answer.close()
        if status == 416:
            await self._unsatisfied(url, held, headers, offset, total, meter)
            return answer.headers
        assert total is not None
        await _Segments(self, url, held, headers, meter).fetch(total)
        return answer.headers

    async def _unsatisfied(self, url: str, held: _files.Part,
                           headers: Mapping[str, str] | None, offset: int, total: int | None,
                           meter: _Meter) -> None:
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
        await self._file(url, held, headers, False, meter)

    async def _range(self, url: str, headers: Mapping[str, str] | None, span: str,
                     validator: str | None = None) -> _Answer:
        """Open a GET for `span`, raising for a 4xx or 5xx other than 416.

        Args:
            url: the file.
            headers: extra request headers.
            span: the `Range` header's value.
            validator: an ETag or date for `If-Range`: the server sends the range only while the
                file still matches it, and the whole file otherwise.

        Returns:
            The open answer; the caller closes it.
        """
        extra = {"Range": span, **({"If-Range": validator} if validator else {})}
        answer = await self._open(_request("GET", url, {**(headers or {}), **extra}, None))
        if answer.status >= 400 and answer.status != 416:
            _checked(await answer.read(), "GET")
        return answer

    async def _write(self, url: str, answer: _Answer, held: _files.Part, start: int,
                     total: int | None, meter: _Meter) -> None:
        """Append the answer's body to the part, and fail a transfer that came short.

        Args:
            url: the file, for messages.
            answer: the open answer.
            held: the part.
            start: where the answer's bytes begin in the file.
            total: the file's size, or None when the server didn't say.
            meter: where the bytes are counted.

        Raises:
            DiskError: the part can't be written.
            NetworkError: the body ended before `total` (retryable).
            Stopped: the client's `stop` was set.
        """
        meter.start(total, start)
        sink = _Sink(held)
        try:
            async for chunk in answer.chunks:
                self._check(url)
                await sink.add(chunk)
                meter.add(len(chunk))
        finally:
            await sink.flush(last=True)
        if total is not None and held.size < total:
            raise NetworkError(f"GET {_text.redact(url)}: truncated at {held.size} of {total} "
                               "bytes", retryable=True)

    def _check(self, url: str) -> None:
        if self.stop is not None and self.stop.is_set():
            raise Stopped(f"{_text.redact(url)}: stopped")


class _Meter:
    """Where a download stands on its sink: a `total` it sets and an `advance(n)` it calls.

    The sink is anything with a settable `total` and an `advance(n)`: a `ui.Row`, or the job's
    per-file stream; None reports nothing.

    Args:
        sink: where the bytes are counted, or None.
    """

    def __init__(self, sink: Any):
        self._sink = sink
        self._at = 0
        # Every byte the download's tries received, for the host's cap (`Client.call`).
        self._moved = 0

    def moved(self) -> int:
        """The bytes every try of this download has received so far."""
        return self._moved

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
        self._moved += n
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
    """A transfer's bytes on their way to its part: gathered on the loop, written on the writer.

    Chunks (16 KiB from curl) are gathered until `BUFFER` bytes, then written on the writer
    thread while the loop goes on; the transfer waits only for its own write. A slow disk (a USB
    drive, a network share) then slows that transfer, not the loop.

    Args:
        held: the part.
        offset: where to write, for a segment (`Part.write_at`); None appends (`Part.write`).
    """

    def __init__(self, held: _files.Part, offset: int | None = None) -> None:
        self._held = held
        self._offset = offset
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

        The last piece of an appended body, under `BUFFER`, goes into the part's own buffer on
        the loop instead: a copy in memory, no system call, since that buffer holds `BUFFER`
        and the full pieces before it went to the disk. The part's landing (or its `head`)
        writes it, off the loop. A small file then costs no hop to the writer at all.

        Args:
            last: the body ended (or failed): this is its last piece.

        Raises:
            DiskError: the part can't be written.
        """
        if not self._chunks:
            return
        data = self._chunks[0] if len(self._chunks) == 1 else b"".join(self._chunks)
        self._chunks, self._size = [], 0
        if last and self._offset is None and len(data) < BUFFER:
            self._held.write(data)
            self.written += len(data)
            return
        loop = asyncio.get_running_loop()
        if self._offset is None:
            work = loop.run_in_executor(_writer_thread(), self._append, data)
        else:
            at = self._offset + self.written
            work = loop.run_in_executor(_writer_thread(), self._held.write_at, at, data)
        await _settle(work)
        self.written += len(data)

    def _append(self, data: bytes) -> None:
        self._held.write(data)
        self._held.flush()


async def _settle(work: asyncio.Future[Any]) -> None:
    """Wait for a write on the writer thread; on a cancel, still wait for it, then cancel.

    The part's descriptor is closed when its transfer ends, and a write still in flight would
    then land in whatever file reused the descriptor.

    Args:
        work: the write's future, from `run_in_executor`.

    Raises:
        DiskError: the write failed.
    """
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


class _Segments:
    """One file fetched as SEGMENTS ranges at once, each written at its own offset.

    The ranges land in the part itself. Until they all arrive the part has holes, so its
    sidecar says so first (`holes`): a crash then makes the next run start over instead of
    taking a part of the right size for a whole one.

    Args:
        client: the client whose `_range` sends each range.
        url: the file.
        held: the part the ranges land in.
        headers: extra request headers.
        meter: where the bytes are counted.
    """

    def __init__(self, client: Client, url: str, held: _files.Part,
                 headers: Mapping[str, str] | None, meter: _Meter):
        self.client, self.url, self.headers, self.meter = client, url, headers, meter
        self.held = held
        self.written: list[int] = []
        # Set when a segment fails: the others but the first stop at their next chunk.
        self.failed = False

    async def fetch(self, total: int) -> None:
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
        ran = await asyncio.gather(*(self.one(index, span) for index, span in enumerate(spans)),
                                   return_exceptions=True)
        errors = [error for error in ran if isinstance(error, BaseException)]
        if errors:
            # Only the first segment runs on from byte 0, so a resume streams the rest from there.
            self.held.cut(self.written[0])
            self.held.note(holes=None)
            raise errors[0]
        self.held.size = total

    async def one(self, index: int, span: tuple[int, int]) -> None:
        """Fetch one segment, and fail it when it came short.

        Args:
            index: which segment, from 0.
            span: its first and last byte, inclusive.

        Raises:
            NetworkError: the server sent no range, or a short one (retryable).
        """
        start, end = span
        try:
            answer = await self.client._range(self.url, self.headers, f"bytes={start}-{end}")
            sink = _Sink(self.held, start)
            try:
                if answer.status != 206:
                    raise NetworkError(f"GET {_text.redact(self.url)}: no range {start}-{end}",
                                       retryable=True)
                async for chunk in answer.chunks:
                    self.client._check(self.url)
                    # The first segment's bytes are kept for a resume, so it alone runs on.
                    if index and self.failed:
                        return
                    await sink.add(chunk)
                    self.meter.add(len(chunk))
            finally:
                try:
                    await sink.flush()
                finally:
                    self.written[index] = sink.written
                    await answer.close()
        except BaseException:
            self.failed = True
            raise
        if self.written[index] != end - start + 1:
            self.failed = True
            raise NetworkError(f"GET {_text.redact(self.url)}: range {start}-{end} "
                               f"ended at {self.written[index]} bytes", retryable=True)


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
    """`fake`'s routes: the answer for a request's URL, as an `_Answer`.

    Args:
        routes: a handler, or routes by URL (see `fake`).
    """

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
            if answer.status not in (301, 302, 303, 307, 308) or not location:
                return _streamed(answer, url, request)
            # A 303 turns any method but HEAD into a GET without a body, as curl does.
            method = "GET" if answer.status == 303 and request.method != "HEAD" else request.method
            request = Request(method, URL(urljoin(url, location)), request.headers,
                              None if method == "GET" else request.content)
        raise NetworkError(f"{request.method} {_text.redact(str(request.url))}: too many "
                           "redirects")

    async def _one(self, request: Request, url: str) -> Reply:
        """One request's answer from its route, calling (and awaiting) a callable route.

        Args:
            request: the request.
            url: its URL without user info, which the routes are keyed by.

        Returns:
            The route's answer.

        Raises:
            NetworkError: no route matches, or the route raised an `OSError`.
        """
        route = self.handler if self.handler is not None else self._route(url)
        if route is None:
            raise NetworkError(f"{request.method} {_text.redact(url)}: no route in net.fake")
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
    """A route's answer as a `Reply`; bytes or str serve `Range`, as a file server would.

    Args:
        route: what the route gave (see `fake`).
        request: the request, whose `Range` header a body answers.

    Returns:
        The answer.
    """
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
    """A `Reply` as an `_Answer`, its body given in its chunks.

    Args:
        answer: the fake's answer.
        url: where it came from.
        request: the request, named in the error a cut connection raises.

    Returns:
        The answer, streaming.
    """
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


def with_query(url: str, params: Mapping[str, Any] | None) -> str:
    """`url` with `params` added to its query; a name already there takes the new value.

    A value of None is sent empty, a bool as `true` or `false`, and a list or tuple as the name
    once per item.

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
        raise NetworkError(f"{_text.redact(url)}: invalid URL: {error}") from None
    kept = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name not in params]
    for name, value in params.items():
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
    """A `Request` for `body`: a query on a GET or HEAD, JSON for a dict or list, else as is.

    Args:
        method: the method, upper case.
        url: where to.
        headers: the request's own headers, or None.
        body: what to send (see `Client.fetch`).

    Returns:
        The request.

    Raises:
        NetworkError: the URL isn't one this client can send to.
    """
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


def _backoff(attempt: int) -> float:
    """The wait before try `attempt + 1`: 1, 2, 4, 8 s at most, each jittered to half of it."""
    return min(FIRST_WAIT * 2 ** (attempt - 1), MAX_WAIT) * random.uniform(0.5, 1.0)


def _worth(method: str, error: Error) -> bool:
    """Whether `error` is worth another try of a `method` request.

    Its raiser said whether it is (`retryable`): a 5xx, a timeout or a reset is; a 4xx, a
    malformed URL or an unknown route isn't. Only methods safe to send twice are tried again.

    Args:
        method: the request's method.
        error: what the try raised.

    Returns:
        True to wait and try again.
    """
    return error.retryable and method.upper() in SAFE


def _given_up(error: Error, check: bool) -> Any:
    response = getattr(error, "response", None)
    if check or not isinstance(response, Response):
        raise error
    return response


def _host(url: str) -> str:
    """The URL's host, lower case.

    Args:
        url: the URL.

    Returns:
        Its host.

    Raises:
        NetworkError: the URL has no scheme or host this client can send to.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as error:
        raise NetworkError(f"{_text.redact(url)}: invalid URL: {error}") from None
    if parts.scheme not in ("http", "https") or not host:
        raise NetworkError(f"{_text.redact(url)}: not an http or https URL")
    return host.lower()


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
    """`response`, or the error its status means under the module's policy.

    Args:
        response: the answer.
        method: the request's method, named in the message.

    Returns:
        `response`, when its status is under 400.

    Raises:
        Blocked: a 429, with the site's wait.
        NotFound: a 404 or 410.
        SiteError: any other 4xx, or a 5xx (retryable).
    """
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
    """Seconds to wait from a `Retry-After` header: a number of seconds or an HTTP date.

    Args:
        value: the header's value, or None.

    Returns:
        The seconds, at most `MAX_BACKOFF`; None when it is missing or unreadable.
    """
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
    """Seconds to wait from a rate-limit reset header (`x-rate-limit-reset`, X's and others').

    A value past `EPOCH` is a Unix time; one below is a number of seconds. A second is added
    for clocks a little apart.

    Args:
        headers: the answer's headers.

    Returns:
        The seconds, at most `MAX_BACKOFF`; None when no header says.
    """
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


def _network(request: Request, error: BaseException) -> NetworkError:
    """The `NetworkError` a failed transfer is: retryable unless no retry could mend it.

    curl's errors carry their code (`FINAL_CURL_CODES` won't heal); a fake's `OSError` stands
    for a dropped or timed-out connection, which may.

    Args:
        request: the request, named (redacted) in the message, and whose secrets are scrubbed.
        error: what curl or the fake raised.

    Returns:
        The error to raise.
    """
    code = getattr(error, "code", None)
    retryable = code not in FINAL_CURL_CODES if isinstance(code, int) else True
    # curl_cffi appends a pointer to curl's error list, which says nothing about this failure.
    reason = str(error).split(" See https://curl.se/", 1)[0].strip() or type(error).__name__
    message = f"{request.method} {_text.redact(str(request.url))}: {reason}"
    return NetworkError(_scrub(_without_userinfo(message, str(request.url)), request),
                        retryable=retryable)


def _bare(url: str) -> str:
    """`url` without its user info, as a fake's routes and a cookie jar name it.

    Args:
        url: the URL.

    Returns:
        The URL without `user:password@`; as it was when it has none or can't be read.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


def _without_userinfo(message: str, url: str | None = None) -> str:
    """`message` with the user info of `url` replaced by `***`.

    Args:
        message: the text.
        url: the URL whose user info to look for; None reads `message` itself as one.

    Returns:
        The text without the user info.
    """
    try:
        netloc = urlsplit(url if url is not None else message).netloc
    except ValueError:
        return message
    userinfo = netloc.rpartition("@")[0] if "@" in netloc else ""
    return message.replace(userinfo, "***") if userinfo else message


def _scrub(text: str, request: Request) -> str:
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
    """The charset a `Content-Type` names.

    Args:
        content_type: the header's value.

    Returns:
        The charset, or "utf-8" when it names none Python knows.
    """
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
    """A cookie of the jar as a dict in `flnd._cookies.Jar`'s shape (Playwright's).

    Args:
        cookie: the jar's cookie.

    Returns:
        The dict.
    """
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
    """A cookie dict in `flnd._cookies.Jar`'s shape as a cookie for the jar.

    Args:
        cookie: the dict; an `expires` below 0, or none, makes a session cookie.

    Returns:
        The jar's cookie.
    """
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
