"""flnd's HTTP: the client factory, the one retry layer, signing, backing off, and atomic downloads.

API requests are retried by the client's transport. File downloads skip that, and `download`
retries them itself, including errors raised while the body streams, so a dead connection
costs three attempts in all. A 429 is not an attempt: the source waits it out and tries again.
"""

from __future__ import annotations

import email.utils
import os
import random
import threading
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar, Union

import httpx

from flnd.sources.base import FetchError, Wait
from shared import fmt, log

ATTEMPTS = 3
MAX_RETRY_AFTER = 60.0
# A source that keeps answering 429, or showing challenges, waits twice as long each time, up to 15 min.
FIRST_BACKOFF = 30.0
MAX_BACKOFF = 15 * 60.0
# Seconds added to a rate limit's reset time, so a clock a little behind the server's isn't refused again.
RESET_SLACK = 5.0
CHUNK_SIZE = 64 * 1024
# The request extension that sends a request once, for downloads that retry on their own.
ONE_ATTEMPT = "flnd.one_attempt"
CONNECTION_ERRORS = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)

T = TypeVar("T")
# A file to save: a URL, mirror URLs tried in order, or `fetch(dest, progress)` for media without a URL.
Fetch = Callable[[Path, Callable[..., None]], Any]
Download = Union[str, Sequence[str], Fetch]

# Waits read time through these, so tests can replace them.
_sleep = time.sleep
_clock = time.monotonic
_random = random.Random()


# Set when Ctrl-C is pressed, so every thread waiting out a rate limit stops at once.
stopping = threading.Event()


class Backoff:
    """Pauses one source after a 429 or a challenge, as long as it takes; a rate limit never stops it.

        backoff = Backoff("ins"); backoff.hit("rate limited"); backoff.wait_turn()

    Each wait is the server's `Retry-After` when it gave one, or else 30 s, 60 s, 120 s, 240 s, …
    up to 15 min, give or take 20%. A request that goes through starts again at 30 s.
    """

    def __init__(self, code: str):
        self.code = code
        self.strikes = 0
        self._resume_at = 0.0
        self._lock = threading.Lock()
        self._showing = False

    def hit(self, reason: str, *, retry_after: float | None = None) -> None:
        """Count one rate limit or challenge, and pause the source; see the class."""
        with self._lock:
            now = _clock()
            # Downloads running together see the same 429; that is one wait, not one per file.
            if now < self._resume_at:
                return
            self.strikes += 1
            steps = min(FIRST_BACKOFF * 2 ** (self.strikes - 1), MAX_BACKOFF)
            seconds = retry_after if retry_after is not None else steps * _random.uniform(0.8, 1.2)
            self._resume_at = now + seconds
        log.warn("{code}: {reason}; waiting {time} (attempt {n})", code=self.code, reason=reason,
                 time=fmt.duration(round(seconds)), n=self.strikes)

    def clear(self) -> None:
        """A request went through, so the next 429 starts again at the first wait."""
        self.strikes = 0

    def wait_turn(self) -> None:
        """Sleep until the current pause, if any, is over; one thread shows the countdown.

        Ctrl-C (`stopping`) ends the wait with `KeyboardInterrupt`.
        """
        if self._resume_at <= _clock():
            return
        with self._lock:
            show, self._showing = not self._showing, True
        try:
            if show:
                total = self._resume_at - _clock()
                with log.progress(f"{self.code}: waiting", total=total, show=_seconds) as bar:
                    self._sleep_out(lambda left: bar.update(total - left))
            else:
                self._sleep_out(lambda left: None)
        finally:
            if show:
                self._showing = False

    def _sleep_out(self, tick: Callable[[float], None]) -> None:
        # Short sleeps, so the countdown moves and Ctrl-C is seen within a second.
        while (left := self._resume_at - _clock()) > 0:
            if stopping.is_set():
                raise KeyboardInterrupt
            tick(left)
            _sleep(min(1.0, left))


def client(*, user_agent: str | None, headers: Mapping[str, str], jar: list[dict],
           sign: Callable[[httpx.Request], httpx.Request], wait: Wait, backoff: Backoff) -> httpx.Client:
    """An httpx client for one source: its user agent, headers and cookies, signing and retries.

        http = net.client(user_agent=None, headers={}, jar=[], sign=lambda r: r, wait=1.0, backoff=Backoff("ins"))

    Without a `user_agent`, requests send httpx's own.
    """
    defaults = httpx.Headers({"User-Agent": user_agent} if user_agent else {})
    defaults.update(dict(headers))
    transport = Transport(_base_transport(), sign=sign, wait=wait, backoff=backoff)
    return httpx.Client(headers=defaults, cookies=cookie_jar(jar), timeout=30.0, follow_redirects=True,
                        transport=transport)


def cookie_jar(jar: Iterable[dict]) -> httpx.Cookies:
    """A cookie jar in the `cookies` module's shape as `httpx.Cookies`, keeping domains and paths."""
    cookies = httpx.Cookies()
    for cookie in jar:
        cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""), path=cookie.get("path") or "/")
    return cookies


class Transport(httpx.BaseTransport):
    """Signs every attempt, retries a dead connection or a 5xx twice, and waits out every 429.

    A 429 pauses the whole source through its `Backoff`, then the request is sent again. When
    the attempts run out, the last response is returned, so the caller sees its status.
    """

    def __init__(self, inner: httpx.BaseTransport, *, sign: Callable[[httpx.Request], httpx.Request],
                 wait: Wait, backoff: Backoff):
        self.inner, self.sign, self.wait, self.backoff = inner, sign, wait, backoff

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.extensions.get(ONE_ATTEMPT):
            self.backoff.wait_turn()
            return self.inner.handle_request(self.sign(request))
        attempt = partial(patiently, partial(self._attempt, request), self.backoff)
        try:
            return retrying(attempt, wait=self.wait, retryable=(*CONNECTION_ERRORS, RetryableStatus))
        except RetryableStatus as failed:
            return failed.response

    def close(self) -> None:
        self.inner.close()

    def _attempt(self, request: httpx.Request) -> httpx.Response:
        return checked(self.inner.handle_request(self.sign(request)), self.backoff)


class RetryableStatus(Exception):
    """A 5xx response, worth another attempt."""

    def __init__(self, response: httpx.Response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response
        self.retry_after = retry_after_seconds(response.headers.get("retry-after"))


class IncompleteError(Exception):
    """A body that ended before its Content-Length."""


class RateLimited(Exception):
    """A 429: the source backs off, then sends the request again."""


def checked(response: httpx.Response, backoff: Backoff) -> httpx.Response:
    """The response when it is worth keeping; a 429 backs off and raises `RateLimited`, a 5xx `RetryableStatus`."""
    if response.status_code == 429:
        backoff.hit("rate limited", retry_after=rate_limit_wait(response.headers))
        response.close()
        raise RateLimited()
    if response.status_code >= 500:
        raise RetryableStatus(response)
    if response.status_code < 400:
        backoff.clear()
    return response


def patiently(send: Callable[[], T], backoff: Backoff) -> T:
    """Call `send` after any pause, as often as it takes while it raises `RateLimited`."""
    while True:
        backoff.wait_turn()
        try:
            return send()
        except RateLimited:
            continue


def retrying(attempt: Callable[[], T], *, wait: Wait, retryable: tuple[type[BaseException], ...],
             attempts: int = ATTEMPTS) -> T:
    """Call `attempt` until it returns, retrying `retryable` errors up to `attempts` tries in all.

        response = retrying(send, wait=1.0, retryable=(httpx.NetworkError,))
    """
    number = 1
    while True:
        try:
            return attempt()
        except retryable as error:
            if number >= attempts:
                raise
            if isinstance(error, RetryableStatus):
                error.response.close()
            seconds = retry_wait(wait, error, number)
            log.debug("retry {n} in {time}: {error}", n=number, time=fmt.duration(seconds), error=error)
            _sleep(seconds)
            number += 1


def retry_wait(wait: Wait, error: BaseException, attempt: int) -> float:
    """Seconds before the next attempt: the source's `wait`, the server's Retry-After, or doubling waits."""
    if callable(wait):
        seconds = wait(error, attempt)
        if seconds is not None:
            return seconds
    if isinstance(error, RetryableStatus) and error.retry_after is not None:
        return min(error.retry_after, MAX_RETRY_AFTER)
    base = wait if isinstance(wait, (int, float)) else 1.0
    return base * 2 ** (attempt - 1)


def rate_limit_wait(headers: Mapping[str, str]) -> float | None:
    """How long a 429 asks to wait: its Retry-After, or else its `x-rate-limit-reset` time (Twitter's)."""
    seconds = retry_after_seconds(headers.get("retry-after"))
    reset = headers.get("x-rate-limit-reset", "")
    if seconds is None and reset.isdigit():
        seconds = max(0.0, int(reset) - time.time()) + RESET_SLACK
    return seconds


def retry_after_seconds(value: str | None) -> float | None:
    """A Retry-After header as seconds: either a number or an HTTP date."""
    if not value:
        return None
    if value.strip().replace(".", "", 1).isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def download(http: httpx.Client, source: Download, dest: Path, *, label: str, backoff: Backoff,
             headers: Mapping[str, str] | None = None, wait: Wait = 1.0, size: int | None = None) -> bool:
    """Save one file atomically under a progress bar, and return whether it was written.

        net.download(http, ["https://a.example/f.7z", "https://b.example/f.7z"], Path("f.7z"), label="f.7z",
                     backoff=Backoff("loi"))

    Each mirror gets three attempts. A failed mirror is logged, and the next one tried. A refusal
    (a 4xx other than 429) on the last mirror is logged and gives False; any other failure there
    is raised. The file appears under its name only once complete.
    """
    mirrors: list[str | Fetch] = [source] if isinstance(source, str) or callable(source) else list(source)
    for number, mirror in enumerate(mirrors, start=1):
        retryable = (Exception,) if callable(mirror) else (*CONNECTION_ERRORS, RetryableStatus, IncompleteError)
        try:
            save = partial(_save_once, http, mirror, dest, label, headers, size, backoff)
            retrying(partial(patiently, save, backoff), wait=wait, retryable=retryable)
            return True
        except FetchError as error:
            if number == len(mirrors):
                log.error("{label}: {error}", label=label, error=error)
                return False
        except Exception:
            if number == len(mirrors):
                raise
        log.warn("mirror {n} failed; trying next", n=number)
    return False


def follow(url: str) -> str:
    """Where a short link leads, from one HEAD request: `follow("https://vm.tiktok.com/ZM…")`."""
    with httpx.Client(follow_redirects=True, timeout=30.0, transport=_base_transport()) as http:
        return str(http.head(url).url)


def jittered(seconds: float) -> float:
    """`seconds` give or take 40%, so pauses never repeat exactly."""
    return seconds * _random.uniform(0.6, 1.4)


def _seconds(value: float) -> str:
    return fmt.duration(round(value))


def _base_transport() -> httpx.BaseTransport:
    return httpx.HTTPTransport()


def _save_once(http: httpx.Client, mirror: str | Fetch, dest: Path, label: str,
               headers: Mapping[str, str] | None, size: int | None, backoff: Backoff) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # A random part name, so two runs never write into each other's file.
    part = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        if callable(mirror):
            _fetch_with(mirror, part, label, size)
        else:
            _fetch_url(http, mirror, part, label, headers, backoff)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _fetch_url(http: httpx.Client, url: str, part: Path, label: str, headers: Mapping[str, str] | None,
               backoff: Backoff) -> None:
    with http.stream("GET", url, headers=dict(headers or {}), extensions={ONE_ATTEMPT: True}) as response:
        checked(response, backoff)
        if response.status_code not in (200, 206):
            raise FetchError(f"HTTP {response.status_code} for {url}", status=response.status_code)
        length = response.headers.get("content-length", "")
        total = int(length) if length.isdigit() else None
        with log.progress(label, total=total, show=fmt.size) as bar:
            written = 0
            with _open_part(part) as file:
                for chunk in response.iter_bytes(CHUNK_SIZE):
                    file.write(chunk)
                    written += len(chunk)
                    # Content-Length counts bytes on the wire, which differ from the file's when compressed.
                    bar.update(response.num_bytes_downloaded or written)
            received = response.num_bytes_downloaded or written
            if total is not None and received != total:
                raise IncompleteError(f"got {received} of {total} bytes from {url}")


def _fetch_with(fetch: Fetch, part: Path, label: str, size: int | None) -> None:
    with log.progress(label, total=size, show=fmt.size) as bar:
        def progress(done: float, total: float | None = None) -> None:
            if total:
                bar.total = total
            bar.update(done)

        fetch(part, progress)


def _open_part(part: Path) -> Any:
    # O_EXCL, so the part is always new; 0644, so the saved file is readable like any download.
    return os.fdopen(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "wb")
