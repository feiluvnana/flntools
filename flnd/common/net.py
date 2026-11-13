"""flnd's HTTP: the client factory, the one retry layer, signing, backing off, and downloads.

API requests are retried by the client's transport. A file's transfer is `shared.http.download`'s
(resume, ranges, the per-host limit, its own three attempts), whose requests the transport sends
once, signed and after the source's back-off. What stays here is flnd's own: mirrors tried in
order and logged, the media check (`files.checked`) and naming a file by its content, the DASH
audio merge, and a 429, which pauses the whole source and is then waited out on the same mirror,
never costing an attempt. `download` resumes every file (redesign B6) and splits a big one into
ranges only on the CDNs in `SPLIT_HOSTS` (D6).
"""

from __future__ import annotations

import email.utils
import os
import random
import threading
import time
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal, Mapping, MutableMapping, Sequence, TypeVar, Union
from urllib.parse import urlsplit

import httpx

from flnd.common import files
from flnd.common.files import NotMediaError
from flnd.source import FetchError, Fetch, Wait
from shared import atomic, fmt, http, log

# Tries for one request, or for one `fetch` function of a file, before its error is raised.
ATTEMPTS = 3
# The longest a server's Retry-After is honoured on a 5xx retry; a longer one is cut to this.
MAX_RETRY_AFTER = 60.0
# A source that keeps answering 429, or showing challenges, waits twice as long each time, up to 15 min.
FIRST_BACKOFF = 30.0
MAX_BACKOFF = 15 * 60.0
# A 429's headers the run log keeps: the wait, and the sites' own rate headers (`x-rate-limit-*`,
# `x-ratelimit-*`, `ratelimit-*`); never a cookie or an authorization header.
RATE_HEADERS = frozenset({"retry-after"})
RATE_PREFIXES = ("x-rate-limit-", "x-ratelimit-", "ratelimit-")
# Seconds added to a rate limit's reset time, so a clock a little behind the server's isn't refused again.
RESET_SLACK = 5.0
# The request extension `shared.http.download` puts on its requests, which the transport sends
# once: `download` retries on its own, and resumes, so another layer of retries would only
# multiply the attempts.
ONE_ATTEMPT = "shared.http.download"
# The hosts whose CDNs passed a live check of `Range` and 4 connections at once (redesign D6), so
# a big file from them downloads in ranges; a host matches itself and its subdomains. Every other
# host gets one connection per file. Each entry is a CDN that passed its check by hand; one that
# failed, or that no listing ever served a file from, stays out with the reason.
SPLIT_HOSTS: frozenset[str] = frozenset({
    # live check passed 2026-11-12: 206, 4 ranges identical (fbk's videos and ins's files)
    "fbcdn.net",
    # live check passed 2026-11-12: 206, 4 ranges identical
    "pbs.twimg.com",
    # live check passed 2026-11-12: 206, 4 ranges identical
    "video.twimg.com",
    # live check passed 2026-11-12: 206, 4 ranges identical (loi's archive mirror)
    "loicloud.com",
    # cdninstagram.com: not checked, ins served every file from fbcdn.net hosts, never from this one.
    # tiktokcdn.com: not checked, tik showed a captcha on the profile listing, so no file URL was reached.
})
# The errors worth another attempt: the connection failed or timed out, not the server's answer.
CONNECTION_ERRORS = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)
# Extensions that name one format, so a `.jpeg` holding a JPEG keeps its name.
SAME_EXTENSIONS = {".jpeg": ".jpg", ".tiff": ".tif", ".m4v": ".mp4"}

T = TypeVar("T")
# A file to save: a URL, mirror URLs tried in order, or `fetch(dest, progress)` (`flnd.source.Fetch`)
# for media without a URL.
Download = Union[str, Sequence[str], Fetch]

# Waits read time through these, so tests can replace them.
_sleep = time.sleep
_clock = time.monotonic
_random = random.Random()


# Set by the first Ctrl-C (redesign D8): every thread waiting out a rate limit stops at once, and
# the job stops listing and lets the running downloads finish.
stopping = threading.Event()
# Set by a second Ctrl-C: downloads stop at their next chunk, keeping their resumable parts.
stopping_now = threading.Event()
# The headers `download` is collecting on this thread, which the transport fills from each
# file response it sends (see `_recording`); None while no download is running here.
_seen = threading.local()


class Backoff:
    """Pauses one source after a 429 or a challenge, as long as it takes; a rate limit never stops it.

        backoff = Backoff("ins"); backoff.hit("rate limited"); backoff.wait_turn()

    Each wait is the server's `Retry-After` when it gave one, or else 30 s, 60 s, 120 s, 240 s, …
    up to 15 min, give or take 20%. A request that goes through starts again at 30 s.
    """

    def __init__(self, code: str, *, on_hit: Callable[[], Any] | None = None):
        """Make a source's back-off, with no pause yet.

        Args:
            code: the source's code, which the countdown and the warning name.
            on_hit: called once for each rate limit counted, not for the ones seen during a wait,
                such as the source's pacer slowing down; None calls nothing.
        """
        self.code = code
        self.strikes = 0
        self._resume_at = 0.0
        self._lock = threading.Lock()
        self._showing = False
        self._on_hit = on_hit

    def hit(self, reason: str, *, retry_after: float | None = None) -> None:
        """Count one rate limit or challenge, and pause the source; see the class.

        Args:
            reason: what the site did, shown in the warning, such as "rate limited".
            retry_after: the seconds the site asked for; None takes the next step of the doubling waits.
        """
        with self._lock:
            now = _clock()
            # Downloads running together see the same 429; that is one wait, not one per file.
            if now < self._resume_at:
                return
            self.strikes += 1
            steps = min(FIRST_BACKOFF * 2 ** (self.strikes - 1), MAX_BACKOFF)
            seconds = retry_after if retry_after is not None else steps * _random.uniform(0.8, 1.2)
            self._resume_at = now + seconds
        # Outside the lock: the callback is the caller's, and may take its own.
        if self._on_hit is not None:
            self._on_hit()
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
                    def tick(left: float) -> None:
                        bar.count = total - left

                    self._sleep_out(tick)
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


def client(*, user_agent: str | None, headers: Mapping[str, str], jar: Iterable[dict],
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
    """A `shared.cookies.Jar`, or any cookie dicts in its shape, as `httpx.Cookies`, keeping domains and paths."""
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
        """Send one request, signed and after the source's pause; a file's own download gets one attempt only."""
        # `http.download` marks its requests: it retries and resumes by itself, so the transport sends them once.
        if request.extensions.get(ONE_ATTEMPT):
            self.backoff.wait_turn()
            response = self.inner.handle_request(self.sign(request))
            if response.status_code == 429:
                # The pause starts here, where Twitter's reset time is still in the headers;
                # `http.download` then raises, and `download` waits the pause out.
                self.backoff.hit("rate limited", retry_after=rate_limit_wait(response.headers))
            seen = getattr(_seen, "headers", None)
            if seen is not None and response.status_code in (200, 206):
                seen.update((name.lower(), value) for name, value in response.headers.items())
            return response
        attempt = partial(patiently, partial(self._attempt, request), self.backoff)
        try:
            return retrying(attempt, wait=self.wait, retryable=(*CONNECTION_ERRORS, RetryableStatus))
        except RetryableStatus as failed:
            return failed.response

    def close(self) -> None:
        """Close the wrapped transport, which owns the connections."""
        self.inner.close()

    def _attempt(self, request: httpx.Request) -> httpx.Response:
        return checked(self.inner.handle_request(self.sign(request)), self.backoff)


class RetryableStatus(Exception):
    """A 5xx response, worth another attempt."""

    def __init__(self, response: httpx.Response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response
        self.retry_after = retry_after_seconds(response.headers.get("retry-after"))


class RateLimited(Exception):
    """A 429: the source backs off, then sends the request again."""


def checked(response: httpx.Response, backoff: Backoff) -> httpx.Response:
    """The response when it is worth keeping; a 429 backs off and raises `RateLimited`, a 5xx `RetryableStatus`.

    Args:
        response: the response of one attempt.
        backoff: the source's `Backoff`, which a 429 pauses and a good answer clears.

    Raises:
        RateLimited: a 429, after the pause started; the caller sends the request again.
        RetryableStatus: a 5xx, worth another attempt.
    """
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
    """Call `send` after any pause, as often as it takes while it raises `RateLimited`.

    Args:
        send: one attempt, which raises `RateLimited` on a 429.
        backoff: the source's `Backoff`, whose pause is waited out before each attempt.

    A 429 costs no attempt: the wait is as long as the site asks, so it never gives up.
    """
    while True:
        backoff.wait_turn()
        try:
            return send()
        except RateLimited:
            continue


def retrying(attempt: Callable[[], T], *, wait: Wait, retryable: tuple[type[BaseException], ...],
             attempts: int = ATTEMPTS, final: tuple[type[BaseException], ...] = ()) -> T:
    """Call `attempt` until it returns, retrying `retryable` errors up to `attempts` tries in all.

        response = retrying(send, wait=1.0, retryable=(httpx.NetworkError,))

    Args:
        attempt: one try; what it returns is returned.
        wait: the source's wait between tries, as `retry_wait` reads it.
        retryable: the errors worth another try; any other error is raised at once.
        attempts: how many tries in all before the last error is raised.
        final: errors raised at once even when they are also `retryable`, because another try
            would get the same answer, such as a login wall in place of a photo.

    Raises:
        The last error, once `attempts` tries have failed, or the first one not worth another try.
    """
    number = 1
    while True:
        try:
            return attempt()
        except retryable as error:
            if number >= attempts or isinstance(error, final):
                raise
            if isinstance(error, RetryableStatus):
                error.response.close()
            seconds = retry_wait(wait, error, number)
            log.debug("retry {n} in {time}: {error}", n=number, time=fmt.duration(seconds), error=error)
            _sleep(seconds)
            number += 1


def retry_wait(wait: Wait, error: BaseException, attempt: int) -> float:
    """Seconds before the next attempt: the source's `wait`, the server's Retry-After, or doubling waits.

    Args:
        wait: the source's `wait`: seconds, or a function of `(error, attempt)` returning seconds or None.
        error: the error that failed the attempt, which may carry a Retry-After.
        attempt: the attempt that failed, from 1.

    Returns:
        The seconds to wait: the function's answer when it gives one; else the server's Retry-After,
        capped at `MAX_RETRY_AFTER`; else `wait` (1 s when it is a function) doubled for each attempt.
    """
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
    # The run log keeps the headers that say why and for how long, so a block can be understood afterwards.
    rate = {name: value for name, value in headers.items() if name.lower() in RATE_HEADERS
            or name.lower().startswith(RATE_PREFIXES)}
    log.debug("rate limited: {headers}", headers=", ".join(f"{name}: {value}" for name, value in rate.items()) or "no rate headers")
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


def download(client: httpx.Client, source: Download, dest: Path, *, label: str, backoff: Backoff,
             headers: Mapping[str, str] | None = None, wait: Wait = 1.0, size: int | None = None,
             kind: str | None = None, audio: str | None = None,
             received: MutableMapping[str, str] | None = None) -> Path:
    """Save one file under a progress bar, resuming what an earlier try left, and return the path written.

        net.download(client, ["https://a.example/f.7z", "https://b.example/f.7z"], Path("f.7z"), label="f.7z",
                     backoff=Backoff("loi"))

    Args:
        client: the source's client from `client`, which sends its cookies and signs its requests.
        source: a URL, mirror URLs tried in order, or a `fetch(dest, progress)` function that
            writes the file to `dest` and reports `progress(done, total)` in bytes.
        dest: where the file goes; with `kind`, its extension may change to match the content.
        label: the file's name on its progress bar and in log lines, such as "posts/a.jpg".
        backoff: the source's `Backoff`, which every request waits on and every 429 pauses.
        headers: extra headers for each URL request, such as a Referer; None sends only the client's.
        wait: the source's wait between attempts of a `fetch` function or a DASH merge, in
            seconds or as a function (see `retry_wait`); a URL's own attempts are `http.download`'s.
        size: the file's size in bytes when the source knows it: the bar's total until the server
            says, and the size checked when it sends none; None when unknown, and then a file
            with a `kind` is decoded to check it, unless the server sent its length.
        kind: "image", "video" or "audio" (`files.kind_of`) to check the file is media and name it
            by its content; None saves any file (an archive, a document) as it comes, under `dest`'s
            name. `files.SNIFF` lets the content decide: `dest` has no extension yet, and media gets
            the one its content calls for and is checked as media, while anything else keeps `dest`.
        audio: the URL of a separate audio stream (DASH), downloaded after the video and merged
            into `dest` with it; None when the video has its own sound.
        received: filled with the headers of the response the file came from (lowercase names),
            such as its `last-modified`, for a URL mirror; None keeps none.

    Returns:
        The path written, which differs from `dest` when `kind` renamed it.

    Raises:
        FetchError: the last mirror refused the file (a 4xx other than 429), with its `status`,
            so the job can refresh an expired link (a 403 or 410); a resumable part is kept.
        NotMediaError: with `kind`, the last mirror sent a web page or JSON, such as a login wall.
        media.UnsafeError: with `kind`, media refused the last mirror's file, such as a decompression bomb.
        media.BrokenError: with `kind`, the last mirror's file still failed `Media.check` after three tries.
        http.IncompleteError: the last mirror's body still ended early after three tries.
        http.Error: the last mirror's connection still failed after three tries.
        KeyboardInterrupt: a second Ctrl-C (`stopping_now`), which keeps a resumable part.
        Exception: whatever the last mirror's `fetch` function raised after three tries.

    Each mirror gets three attempts, each resuming where the last stopped, and a 429 is waited
    out on the same mirror without costing one. A failed mirror is logged with why, and the next
    one tried; only the last mirror's error reaches the caller. The file appears under its name
    only once complete and checked. A resumable part (from a server that sends an `ETag` or a
    `Last-Modified`) and its `.part.json` are kept through any failure, a Ctrl-C or a crash, and
    the next call for the same `dest` resumes them, even with a fresh URL; any other part is
    deleted. This makes network requests and can take minutes.
    """
    from shared import media

    # A web page or a refused file is the same answer on every try, so it fails at once (fixes #9).
    # `final` matters only for a `fetch` function, whose retryable is every Exception: a URL's
    # own attempts are `http.download`'s, which retries neither of these.
    final = (NotMediaError, media.UnsafeError)
    mirrors: list[str | Fetch] = [source] if isinstance(source, str) or callable(source) else list(source)
    # Each mirror in order: a failure is logged and the next one tried, and only the last one's error reaches the caller.
    for number, mirror in enumerate(mirrors, start=1):
        try:
            if callable(mirror):
                save = partial(_save_fetch, mirror, dest, label, size, kind)
                return retrying(partial(patiently, save, backoff), wait=wait, retryable=(Exception,), final=final)
            if audio is not None:
                return _save_dash(client, mirror, audio, dest, label, headers, backoff, wait)
            return _save_url(client, mirror, dest, label, headers, size, backoff, kind, received)
        except http.RefusedError as error:
            refused = FetchError(f"HTTP {error.status} for {fmt.url(str(mirror))}", status=error.status)
            if number == len(mirrors):
                raise refused from None
            reason: BaseException = refused
        except Exception as error:
            # Any error of a mirror that isn't the last is worth trying the next mirror for; the last
            # one's error is the caller's, which records the file as failed.
            if number == len(mirrors):
                raise
            reason = error
        log.warn("mirror {n} failed: {reason}; trying next", n=number, reason=reason)
    raise ValueError("download needs a URL or a fetch function")


def follow(url: str) -> str:
    """Where a short link leads, from one HEAD request: `follow("https://vm.tiktok.com/ZM…")`."""
    # HEAD, since only the final URL is needed, not the body. It uses a plain transport, not the source's
    # client, so the request is not signed, paced or retried.
    with httpx.Client(follow_redirects=True, timeout=30.0, transport=_base_transport()) as http:
        return str(http.head(url).url)


def jittered(seconds: float) -> float:
    """`seconds` give or take 40%, so pauses never repeat exactly."""
    return seconds * _random.uniform(0.6, 1.4)


def _seconds(value: float) -> str:
    return fmt.duration(round(value))


def _base_transport() -> httpx.BaseTransport:
    return httpx.HTTPTransport()


def _save_url(client: httpx.Client, url: str, dest: Path, label: str, headers: Mapping[str, str] | None,
              size: int | None, backoff: Backoff, kind: str | None,
              received: MutableMapping[str, str] | None) -> Path:
    """Download one URL with `http.download`, check it as `kind`, and save it under the name its content calls for."""
    seen: dict[str, str] = {}

    def check(part: Path) -> Path:
        # A length the server sent was checked by `http.download` already, so the file is only
        # sniffed; without one (a chunked body), it is decoded, which finds a cut-off photo.
        known = "content-length" in seen or size is not None
        # The name its content calls for, which `http.download` renames the part onto in one
        # step: a crash never leaves `a.jpg` holding a PNG, which the job would take as saved.
        return _sniffed(part, dest, known) if kind == files.SNIFF else _as_media(part, dest, kind or "", known)

    with _Row(label) as row:
        named = _transfer(client, url, dest, headers=headers, size=size, backoff=backoff,
                          check=check if kind is not None else None, progress=row.follow(), seen=seen)
    if received is not None:
        received.update(seen)
    return named


def _save_dash(client: httpx.Client, video: str, audio: str, dest: Path, label: str,
               headers: Mapping[str, str] | None, backoff: Backoff, wait: Wait) -> Path:
    """Download a DASH video and its `audio` stream, and save them merged as `dest`, trying the whole three times."""
    from shared import media

    # Both streams are only inputs, named to match PART_GLOB, so a run killed before the merge
    # leaves nothing `flnd sync`'s repair doesn't remove; deleted however the merge ends.
    streams = [dest.with_name(f".{dest.name}.{name}.part") for name in ("video", "audio")]
    with _Row(label) as row:
        attempt = 1
        while True:
            try:
                for url, stream in zip((video, audio), streams):
                    _transfer(client, url, stream, headers=headers, size=None, backoff=backoff, check=None,
                              progress=row.follow())
                # The merge is a stream copy by ffmpeg, which `stopping_now` can't reach; it is
                # bounded by ffmpeg's copy timeout (10 min), and the terminal's Ctrl-C reaches
                # ffmpeg too, being in the same process group, which ends it. So a second Ctrl-C
                # before it starts stops here, and one during it stops ffmpeg.
                if stopping_now.is_set():
                    raise KeyboardInterrupt
                row.caption("merging")
                # The merge writes a temporary `.<name>.<random>.part` in `dest`'s folder, so it
                # matches PART_GLOB, and `save` renames it onto `dest` once complete. `format` is
                # `dest`'s real extension, since the streams' names don't say the container. `save`
                # refuses to write over a file, so an empty one, which holds nothing and which the
                # job would download again anyway, is removed first.
                if dest.is_file() and dest.stat().st_size == 0:
                    dest.unlink()
                # `with` drops the merged file's temporary copy if `save` fails.
                with media.open(streams[0]) as clip:
                    clip.merge(audio=streams[1], format=dest.suffix).save(dest)
                return dest
            except media.BrokenError as error:
                # A stream cut short without a length to tell (a chunked body) fails the merge;
                # it may come whole next time. Other merge errors would fail the same way again.
                if attempt >= ATTEMPTS:
                    raise
                seconds = retry_wait(wait, error, attempt)
                log.debug("retry {n} in {time}: {error}", n=attempt, time=fmt.duration(seconds), error=error)
                _sleep(seconds)
                attempt += 1
            finally:
                for stream in streams:
                    stream.unlink(missing_ok=True)


def _transfer(client: httpx.Client, url: str, dest: Path, *, headers: Mapping[str, str] | None,
              size: int | None, backoff: Backoff, check: Callable[[Path], Any] | None,
              progress: Callable[[int, int | None], None], seen: dict[str, str] | None = None) -> Path:
    """`http.download` of one URL onto `dest`, as often as a 429 asks: each pause is waited out, then it resumes.

    Returns the path written: `dest`, or the one `check` returned. `seen`, when given, is filled
    with the file responses' headers, from the transport (`_recording`).
    """
    while True:
        try:
            with _recording(seen):
                saved = http.download(url, dest, client=client, headers=headers, size=size, parts=split_parts(url),
                                      check=check, progress=progress).path
        except http.RateLimitedError as error:
            # The transport started the pause from the headers; this counts it for a client
            # whose transport isn't ours. A second hit in one pause does nothing.
            backoff.hit("rate limited", retry_after=error.retry_after)
            backoff.wait_turn()
            continue
        backoff.clear()
        return saved


def split_parts(url: str) -> Literal["auto", 1]:
    """`http.download`'s `parts` for `url`: "auto" on a host in `SPLIT_HOSTS` or under one, else 1 (D6)."""
    host = (urlsplit(url).hostname or "").lower()
    return "auto" if any(host == name or host.endswith(f".{name}") for name in SPLIT_HOSTS) else 1


@contextmanager
def _recording(seen: dict[str, str] | None) -> Iterator[None]:
    """Have the transport put the headers of this thread's file responses into `seen` for the block."""
    previous = getattr(_seen, "headers", None)
    _seen.headers = seen
    try:
        yield
    finally:
        _seen.headers = previous


def _save_fetch(fetch: Fetch, dest: Path, label: str, size: int | None, kind: str | None) -> Path:
    """Have a source's `fetch(dest, progress)` write the file into a part, check it, and rename it onto `dest`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # `atomic` names the part `.<name>.<random>.part`, so two runs never write into each other's
    # files, and whatever a killed run leaves is removed by `flnd sync`'s repair (fixes #13). It
    # deletes the part however the block ends, and renames it onto `dest` only if it's still there.
    with atomic.writing(dest, replace=True) as part:
        with _Row(label) as row:
            row.start(size)
            fetch(part, row.stoppable)
            known = size is not None
            if kind == files.SNIFF:
                final = _sniffed(part, dest, known)
            else:
                final = dest if kind is None else _as_media(part, dest, kind, known)
        if final != dest:
            # Named by its content: moved there in one step, so the block's rename onto `dest` has nothing to move.
            os.replace(part, final)
    return final


def _as_media(part: Path, dest: Path, kind: str, known: bool) -> Path:
    """Where a downloaded photo or video goes, named by its content; a web page or a broken file raises.

    A file whose size matched Content-Length (`known`) is only sniffed: decoding it would cost
    time and catch nothing the size didn't. See `files.checked` for what raises.
    """
    extension = files.checked(part, kind, decode=not known)
    if extension is None or same_extension(extension, dest.suffix):
        return dest
    return dest.with_suffix(extension)


def _sniffed(part: Path, dest: Path, known: bool) -> Path:
    """Where a downloaded file whose content decides its kind goes: media by its content, anything else as `dest`.

    `dest` has no extension yet, so the content's is added to its name, never put in place of a
    part of it (`5930 a.b` stays `5930 a.b.jpg`). A web page raises `NotMediaError`, as it would
    for a photo: a post's file is never a login wall. Media is checked as `_as_media` checks it.
    """
    from shared import media

    extension = media.sniff(part)
    if extension in files.PAGES:
        raise NotMediaError("got a web page instead of a file")
    # `kind_of` reads a name's extension, and ".mp4" alone is a name without one.
    kind = files.kind_of(f"file{extension}") if extension else None
    if extension is None or kind is None:
        # An archive, a document, or nothing `media` knows: kept as it came.
        return dest
    files.checked(part, kind, decode=not known)
    return dest.with_name(dest.name + extension)


def same_extension(one: str, other: str) -> bool:
    """Whether two extensions name one format: `.jpg` and `.jpeg` do.

    Args:
        one: an extension, with its dot.
        other: the other extension, with its dot; compared case-insensitively.
    """
    return SAME_EXTENSIONS.get(one.lower(), one.lower()) == SAME_EXTENSIONS.get(other.lower(), other.lower())


class _Row:
    """One progress bar for a file, or for a DASH video and its audio, opened when the first response arrives."""

    def __init__(self, label: str):
        self.label, self.stack, self.base = label, ExitStack(), 0.0
        self.bar: Any = None

    def __enter__(self) -> _Row:
        return self

    def __exit__(self, *error: Any) -> None:
        self.stack.__exit__(*error)

    def start(self, total: float | None) -> None:
        if self.bar is None:
            self.bar = self.stack.enter_context(log.progress(self.label, total=total, show=fmt.size))
            return
        self.base = self.bar.count
        self.bar.total = self.base + total if total is not None and self.bar.total is not None else None

    def update(self, done: float, total: float | None = None) -> None:
        if total:
            self.bar.total = self.base + total
        self.bar.count = self.base + done

    def stoppable(self, done: float, total: float | None = None) -> None:
        """`update`, for a source's `fetch` function, which a second Ctrl-C stops at its next report (D8)."""
        if stopping_now.is_set():
            raise KeyboardInterrupt
        self.update(done, total)

    def follow(self) -> Callable[[int, int | None], None]:
        """A `progress(done, total)` for one more stream on this bar, which a second Ctrl-C stops."""
        started = False

        def progress(done: int, total: int | None) -> None:
            nonlocal started
            # Raised in the download's thread, so `http.download` keeps the resumable part (D8).
            if stopping_now.is_set():
                raise KeyboardInterrupt
            if not started:
                self.start(total)
                started = True
            self.update(done, total)

        return progress

    def caption(self, text: str) -> None:
        self.bar.caption = text
