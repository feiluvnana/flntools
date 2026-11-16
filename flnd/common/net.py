"""flnd's HTTP: the source's httpx client, its cookies, and downloads.

A download (`download`) goes through one part (`flnd._files.part`): it is opened and locked once,
before any request, so a file another run is saving raises `BusyError` at once, and every
mirror, retry and DASH merge of the file then writes into that same part. Its content is
checked through the part's descriptor, it gets its post's date as its modified time, and it is
renamed under the name its content calls for: one rename, one `fstat`, and no second open.
"""

from __future__ import annotations

import atexit
import datetime
import email.utils
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, TypeVar

import httpx

from flnd.common import paths
from flnd.errors import Error, SiteError
from flnd.source import Fetch, File
from flnd import _files
from flnd import _time as clock, _http as http, _text as text, _tools
from flnd._ui import ui

# Tries for one `fetch` function of a file, or one DASH merge, before its error is raised.
ATTEMPTS = 3
# A saved file's modified time is noon UTC of its post's day.
NOON = 12
# Extensions that name one format, so a `.jpeg` holding a JPEG keeps its name.
SAME_EXTENSIONS = {".jpeg": ".jpg", ".tiff": ".tif", ".m4v": ".mp4"}

T = TypeVar("T")

# The headers `download` is collecting on this thread, which the client's response hook fills.
_seen = threading.local()


def client(*, user_agent: str | None, jar: Iterable[dict],
           sign: Callable[[httpx.Request], Any]) -> httpx.Client:
    """An httpx client for one source with user agent, cookies, and `sign` hook."""
    defaults = httpx.Headers({"User-Agent": user_agent} if user_agent else {})
    return httpx.Client(
        headers=defaults, cookies=cookie_jar(jar), timeout=30.0, follow_redirects=True,
        event_hooks={"request": [sign], "response": [_record]},
    )


def cookie_jar(jar: Iterable[dict]) -> httpx.Cookies:
    """A `flnd._cookies.Jar`, or any cookie dicts in its shape, as `httpx.Cookies`."""
    cookies = httpx.Cookies()
    for cookie in jar:
        cookies.set(
            cookie["name"], cookie["value"], domain=cookie.get("domain", ""),
            path=cookie.get("path") or "/",
        )
    return cookies


def download(client: http.Client, file: File, dest: Path, *, label: str,
             headers: Mapping[str, str] | None = None, kind: str | None = None,
             received: MutableMapping[str, str] | None = None,
             stop: threading.Event | None = None,
             day: datetime.date | None = None) -> _files.Saved:
    """Save one `File` under a progress bar, trying mirrors in order.

    Args:
        client: the source's client.
        file: what to fetch: a URL, mirrors, a `fetch` function, or a DASH pair.
        dest: where it lands; the extension may change to what its content calls for.
        label: the progress bar's label.
        headers: extra request headers.
        kind: what the file must be (`paths.IMAGE`, …, `paths.SNIFF`); None checks nothing.
        received: filled with the file response's headers.
        stop: set to stop the transfer.
        day: the post's day, which becomes the file's modified time (noon UTC); None uses the
            server's `Last-Modified`, else leaves the time of the write.

    Returns:
        Where the file landed, with its size and modified time.

    Raises:
        BusyError: another run is saving this file; nothing was sent.
        Error: the last mirror failed.
    """
    if file.url is None:
        raise ValueError("download needs a URL or a fetch function")
    halt = stop if stop is not None else getattr(client, "_stop", None)
    mirrors: list[str | Fetch] = (
        [file.url] if isinstance(file.url, str) or callable(file.url) else list(file.url))
    with _files.part(dest, resume=True) as held:
        if day is not None:
            held.mtime = noon(day)
        for number, mirror in enumerate(mirrors, start=1):
            try:
                _try_mirror(client, mirror, file, held, label, headers, kind, received, halt)
                break
            # The last mirror's failure is the file's; an earlier one's is a warning, and the
            # next mirror is tried. A `Stopped` is a KeyboardInterrupt, so it is never caught.
            except Exception as error:
                if number == len(mirrors):
                    raise
                reason = error
            # The next mirror may serve other bytes, so it starts from an empty part.
            held.reset()
            ui.warn("mirror {n} failed: {reason}; trying next", n=number, reason=reason)
    assert held.saved is not None
    return held.saved


def noon(day: datetime.date) -> float:
    """Noon UTC of `day`, in epoch seconds: the modified time of a file posted that day."""
    moment = datetime.datetime.combine(day, datetime.time(NOON), tzinfo=datetime.timezone.utc)
    return moment.timestamp()


def _try_mirror(
    client: http.Client, mirror: str | Fetch, file: File, held: _files.Part, label: str,
    headers: Mapping[str, str] | None, kind: str | None,
    received: MutableMapping[str, str] | None, stop: threading.Event | None,
) -> None:
    """Download one mirror (fetch callback, DASH pair, or URL) into the part `held`.

    Args:
        client: the source's client.
        mirror: a URL or a `fetch` function.
        file: the file, for its size and DASH audio.
        held: the file's locked part.
        label: the progress bar's label.
        headers: extra request headers.
        kind: what the file must be, or None.
        received: filled with the response's headers.
        stop: set to stop the transfer.
    """
    if callable(mirror):
        save = partial(_save_fetch, mirror, held, label, file.size, kind, stop)
        return _again(save, worth=_fetch_again)
    if file.audio is not None:
        return _save_dash(client, mirror, file.audio, held, label, headers, stop)
    return _save_url(client, mirror, held, label, headers, file.size, kind, received, stop)


_follow_client: httpx.Client | None = None
_follow_lock = threading.Lock()


def _close_follow() -> None:
    global _follow_client
    with _follow_lock:
        if _follow_client is not None:
            _follow_client.close()
            _follow_client = None


def follow(url: str) -> str:
    """Where a short link leads, from one HEAD request: `follow("https://vm.tiktok.com/ZM…")`."""
    global _follow_client
    # HEAD, since only final URL is needed, not body. Plain transport without signing or retry.
    with _follow_lock:
        if _follow_client is None:
            _follow_client = httpx.Client(follow_redirects=True, timeout=30.0)
            atexit.register(_close_follow)
        client = _follow_client
    return str(client.head(url).url)


def _save_url(client: http.Client, url: str, held: _files.Part, label: str,
              headers: Mapping[str, str] | None, size: int | None, kind: str | None,
              received: MutableMapping[str, str] | None,
              stop: threading.Event | None) -> None:
    """Download one URL into `held`, date it, and check it as `kind`.

    Args:
        client: the source's client.
        url: the file.
        held: the file's locked part.
        label: the progress bar's label.
        headers: extra request headers.
        size: the size the source gave, or None.
        kind: what the file must be, or None to check nothing.
        received: filled with the response's headers.
        stop: set to stop the transfer.
    """
    seen: dict[str, str] = {}
    with _Row(label) as row, _recording(seen):
        client.fill(url, held, headers=headers, progress=row.follow(stop))
    if received is not None:
        received.update(seen)
    if held.mtime is None:
        served = served_day(seen.get("last-modified"))
        if served is not None:
            held.mtime = noon(served)
    if kind is not None:
        _place(held, kind, "content-length" in seen or size is not None)


def _save_dash(client: http.Client, video: str, audio: str, held: _files.Part, label: str,
               headers: Mapping[str, str] | None, stop: threading.Event | None) -> None:
    """Download a DASH video and its audio stream, merged into `held`.

    Args:
        client: the source's client.
        video: the video stream's URL.
        audio: the audio stream's URL.
        held: the file's locked part, which ffmpeg writes.
        label: the progress bar's label.
        headers: extra request headers.
        stop: set to stop the transfers.
    """
    with _Row(label) as row:
        _again(
            partial(_dash_merge, client, video, audio, held, headers, stop, row),
            worth=_merge_again,
        )


def _dash_merge(client: http.Client, video: str, audio: str, held: _files.Part,
                headers: Mapping[str, str] | None, stop: threading.Event | None,
                row: _Row) -> None:
    """Fetch DASH video and audio streams in parallel and merge them into `held`.

    Each stream is a part of its own beside the file (`<name>.video.part`), locked like any
    other, so a failed try resumes it and a sweep clears it. The streams never land: once
    merged they are dropped.

    Args:
        client: the source's client.
        video: the video stream's URL.
        audio: the audio stream's URL.
        held: the file's locked part, which ffmpeg writes.
        headers: extra request headers.
        stop: set to stop the transfers.
        row: the progress bar both streams count on.

    Raises:
        KeyboardInterrupt: a second Ctrl-C came before the merge.
    """
    dest = held.dest
    with ExitStack() as parts:
        streams = [parts.enter_context(_files.part(dest.with_name(f"{dest.name}.{name}"),
                                                   resume=True))
                   for name in ("video", "audio")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            started = [
                pool.submit(client.fill, url, stream, headers=headers, progress=row.follow(stop))
                for url, stream in zip((video, audio), streams)
            ]
            for running in started:
                running.result()
        if stop is not None and stop.is_set():
            raise KeyboardInterrupt
        row.caption("merging")
        ffmpeg = _tools.ffmpeg()
        fmt = "webm" if dest.suffix.lower() == ".webm" else "mp4"
        flags = ["-movflags", "+faststart"] if fmt == "mp4" else []
        try:
            # ffmpeg truncates and rewrites the locked part in place (`-y`), by its name.
            ffmpeg.run(
                "-y", "-i", str(streams[0].path), "-i", str(streams[1].path),
                "-c", "copy", *flags, "-f", fmt, str(held.path),
            )
        finally:
            # Merged or not, the streams go: a failed merge (a short stream, a container that
            # can't hold them) is tried again from new downloads.
            for stream in streams:
                stream.drop()
        row.caption("merged")


def _fetch_again(error: Exception) -> bool:
    """Whether a source's `fetch` function is worth another try after `error`.

    Its errors are the source's own (an SDK's), so any is tried again, except the site's
    answer (a `SiteError`: a web page for a photo, a refusal), which the next try would get too.

    Args:
        error: what the try raised.

    Returns:
        True to try again.
    """
    return not isinstance(error, SiteError)


def _merge_again(error: Exception) -> bool:
    """Whether a DASH download and merge is worth another try after `error`.

    A stream that came short, or a merge ffmpeg failed (it may have read a short stream), is;
    a refusal or a missing ffmpeg isn't.

    Args:
        error: what the try raised.

    Returns:
        True to try again.
    """
    return isinstance(error, _tools.RunError) or (isinstance(error, Error) and error.retryable)


def _again(attempt: Callable[[], T], *, worth: Callable[[Exception], bool]) -> T:
    """Call `attempt` up to `ATTEMPTS` times while what it raises is `worth` another try.

    The tries wait 1, then 2 s between them. A `KeyboardInterrupt` (`Stopped`) is never caught.

    Args:
        attempt: one try; what it returns is returned.
        worth: whether an exception the try raised is worth another; when it isn't, or the
            tries are used up, the exception is raised.
    """
    number = 1
    while True:
        try:
            return attempt()
        except Exception as error:
            if number >= ATTEMPTS or not worth(error):
                raise
            seconds = 2.0 ** (number - 1)
            ui.debug("retry {n} in {time}: {error}", n=number,
                     time=text.duration(seconds), error=error)
            clock.sleep(seconds)
            number += 1


@contextmanager
def _recording(seen: dict[str, str]) -> Iterator[None]:
    """Have the client's response hook put this thread's file responses' headers into `seen`."""
    previous = getattr(_seen, "headers", None)
    _seen.headers = seen
    try:
        yield
    finally:
        _seen.headers = previous


def _record(response: httpx.Response) -> None:
    """The client's response hook: a file response's headers, for this thread's download."""
    seen = getattr(_seen, "headers", None)
    if seen is not None and response.status_code in (200, 206):
        seen.update((name.lower(), value) for name, value in response.headers.items())


def _save_fetch(fetch: Fetch, held: _files.Part, label: str, size: int | None,
                kind: str | None, stop: threading.Event | None) -> None:
    """Have `fetch(path, progress)` write into the part `held`, then check it as `kind`.

    Args:
        fetch: the source's function, given the part's path and a progress callback.
        held: the file's locked part.
        label: the progress bar's label.
        size: the size the source gave, or None.
        kind: what the file must be, or None to check nothing.
        stop: set to stop at the fetch's next progress report.
    """
    # A fetch that resumes (Telegram's) keeps its offsets in `<part>.json`, which goes when the
    # part lands.
    held.side = True
    with _Row(label) as row:
        row.start(size)
        fetch(held.path, partial(row.stoppable, stop=stop))
    if kind is not None:
        _place(held, kind, size is not None)


def _place(held: _files.Part, kind: str, known: bool) -> None:
    """Point `held` at the name its content calls for, or drop it when it isn't `kind`.

    A whole file that fails its check would fail again on resume, so its part is removed.

    Args:
        held: the finished part; its first bytes are read through its descriptor.
        kind: `paths.SNIFF`, or what the file must be (`paths.IMAGE`, …).
        known: whether the server or the source gave the size.

    Raises:
        SiteError: the content is a web page where media was expected.
    """
    try:
        head = held.head()
        if kind == paths.SNIFF:
            held.dest = _sniffed(head, held.dest, known)
        else:
            held.dest = _as_media(head, held.dest, kind, known)
    except BaseException:
        held.drop()
        raise


def _as_media(head: bytes, dest: Path, kind: str, known: bool) -> Path:
    """Where a photo or video starting with `head` goes; raises on a web page."""
    extension = paths.checked(head, kind, decode=not known)
    if extension is None or same_extension(extension, dest.suffix):
        return dest
    return dest.with_suffix(extension)


def _sniffed(head: bytes, dest: Path, known: bool) -> Path:
    """Where a file whose content decides its kind goes: media by content, else as `dest`.

    Args:
        head: the file's first bytes.
        dest: its planned path.
        known: whether its size was given.

    Returns:
        `dest` with the extension its content calls for added, or `dest` as it is.

    Raises:
        SiteError: the content is a web page.
    """
    extension = paths.sniff(head)
    if extension in paths.PAGES:
        raise paths.not_media("a file")
    # `kind_of` reads a name's extension, and ".mp4" alone is a name without one.
    kind = paths.kind_of(f"file{extension}") if extension else None
    if extension is None or kind is None:
        # An archive, a document, or nothing `sniff` knows: kept as it came.
        return dest
    paths.checked(head, kind, decode=not known)
    return dest.with_name(dest.name + extension)


def served_day(value: str | None) -> datetime.date | None:
    """The UTC day of an HTTP `Last-Modified` date.

    Args:
        value: the header's value, or None.

    Returns:
        The day, or None when the header is missing or malformed.
    """
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).date()


def same_extension(one: str, other: str) -> bool:
    """Whether two extensions name one format: `.jpg` and `.jpeg` do."""
    return (
        SAME_EXTENSIONS.get(one.lower(), one.lower())
        == SAME_EXTENSIONS.get(other.lower(), other.lower())
    )


class _Row:
    """One progress bar for a file, or for DASH video and audio, opened on first response."""

    def __init__(self, label: str):
        self.label, self.stack, self.base = label, ExitStack(), 0.0
        self.bar: Any = None
        self._lock = threading.Lock()

    def __enter__(self) -> _Row:
        return self

    def __exit__(self, *error: Any) -> None:
        self.stack.__exit__(*error)

    def start(self, total: float | None) -> None:
        with self._lock:
            if self.bar is None:
                self.bar = self.stack.enter_context(
                    ui.progress(self.label, total=total, unit="bytes"),
                )
                return
            self.base = self.bar.count
            self.bar.total = (
                self.base + total if total is not None and self.bar.total is not None else None
            )

    def update(self, done: float, total: float | None = None) -> None:
        with self._lock:
            if self.bar is None:
                return
            if total:
                self.bar.total = self.base + total
            self.bar.count = self.base + done

    def stoppable(self, done: float, total: float | None = None, *,
                  stop: threading.Event | None = None) -> None:
        """Update progress for a source's `fetch` function, stopping on second Ctrl-C."""
        if stop is not None and stop.is_set():
            raise KeyboardInterrupt
        self.update(done, total)

    def follow(self, stop: threading.Event | None = None) -> _Stream:
        """A sink for one more stream on this bar, stoppable on Ctrl-C."""
        return _Stream(self, stop)

    def grow(self, old: float | None, new: float | None) -> None:
        """Open the bar on a stream's first total, or swap the stream's `old` total for `new`."""
        with self._lock:
            if self.bar is None:
                self.bar = self.stack.enter_context(
                    ui.progress(self.label, total=new, unit="bytes"),
                )
            elif new is None or self.bar.total is None:
                self.bar.total = None
            else:
                self.bar.total = self.bar.total - (old or 0) + new

    def advance(self, n: float) -> None:
        with self._lock:
            if self.bar is not None:
                self.bar.advance(n)

    def caption(self, text: str) -> None:
        with self._lock:
            if self.bar is not None:
                self.bar.label = f"{self.label} ({text})"


class _Stream:
    """One stream's progress sink on a shared `_Row`."""

    def __init__(self, row: _Row, stop: threading.Event | None):
        self._row, self._stop = row, stop
        self._total: float | None = None

    @property
    def total(self) -> float | None:
        return self._total

    @total.setter
    def total(self, value: float | None) -> None:
        self._row.grow(self._total, value)
        self._total = value

    def advance(self, n: float) -> None:
        if self._stop is not None and self._stop.is_set():
            raise KeyboardInterrupt
        self._row.advance(n)
