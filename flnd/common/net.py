"""flnd's HTTP: the source's httpx client, its cookies, and downloads."""

from __future__ import annotations

import atexit
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, TypeVar

import httpx

from flnd.common import paths
from flnd.common.paths import NotMediaError
from flnd.source import Fetch, FetchError, File
from shared import clock, fs, http, sh, text, ui

# Tries for one `fetch` function of a file, or one DASH merge, before its error is raised.
ATTEMPTS = 3
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
    """A `shared.cookies.Jar`, or any cookie dicts in its shape, as `httpx.Cookies`."""
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
             stop: threading.Event | None = None) -> Path:
    """Save one `File` under a progress bar, trying mirrors in order, and return the path."""
    if file.url is None:
        raise ValueError("download needs a URL or a fetch function")
    halt = stop if stop is not None else getattr(client, "_stop", None)
    mirrors: list[str | Fetch] = (
        [file.url] if isinstance(file.url, str) or callable(file.url) else list(file.url))
    for number, mirror in enumerate(mirrors, start=1):
        try:
            return _try_mirror(
                client, mirror, file, dest, label, headers, kind, received, halt,
            )
        except http.StoppedError:
            raise KeyboardInterrupt from None
        except http.RefusedError as error:
            reason: BaseException = FetchError(
                f"HTTP {error.status} for {text.redact(str(mirror))}", status=error.status
            )
            if number == len(mirrors):
                raise reason from None
        except Exception as error:
            if number == len(mirrors):
                raise
            reason = error
        ui.warn("mirror {n} failed: {reason}; trying next", n=number, reason=reason)
    raise ValueError("download needs a URL or a fetch function")


def _try_mirror(
    client: http.Client, mirror: str | Fetch, file: File, dest: Path, label: str,
    headers: Mapping[str, str] | None, kind: str | None,
    received: MutableMapping[str, str] | None, stop: threading.Event | None,
) -> Path:
    """Download one mirror (fetch callback, DASH pair, or URL) into `dest`."""
    if callable(mirror):
        save = partial(_save_fetch, mirror, dest, label, file.size, kind, stop)
        return _again(save, retryable=(Exception,), final=(NotMediaError,))
    if file.audio is not None:
        return _save_dash(client, mirror, file.audio, dest, label, headers, stop)
    return _save_url(client, mirror, dest, label, headers, file.size, kind, received, stop)


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


def _save_url(client: http.Client, url: str, dest: Path, label: str,
              headers: Mapping[str, str] | None, size: int | None, kind: str | None,
              received: MutableMapping[str, str] | None,
              stop: threading.Event | None) -> Path:
    """Download one URL, check it as `kind`, and save it under the name its content calls for."""
    seen: dict[str, str] = {}
    with _Row(label) as row, _recording(seen):
        saved = client.download(url, dest, headers=headers, progress=row.follow(stop))
    if received is not None:
        received.update(seen)
    if kind is None:
        return saved
    known = "content-length" in seen or size is not None
    final = _sniffed(saved, dest, known) if kind == paths.SNIFF else _as_media(
        saved, dest, kind, known
    )
    if final != saved:
        os.replace(saved, final)
    return final


def _save_dash(client: http.Client, video: str, audio: str, dest: Path, label: str,
               headers: Mapping[str, str] | None, stop: threading.Event | None) -> Path:
    """Download a DASH video and its audio stream, saving merged as `dest`."""
    with _Row(label) as row:
        return _again(
            partial(_dash_merge, client, video, audio, dest, headers, stop, row),
            retryable=(http.IncompleteError, sh.RunError),
        )


def _dash_merge(client: http.Client, video: str, audio: str, dest: Path,
                headers: Mapping[str, str] | None, stop: threading.Event | None,
                row: _Row) -> Path:
    """Fetch DASH video and audio streams in parallel and merge them into `dest`."""
    streams = [dest.with_name(f".{dest.name}.{name}.stage") for name in ("video", "audio")]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            started = [
                pool.submit(client.download, url, stream, headers=headers,
                            progress=row.follow(stop))
                for url, stream in zip((video, audio), streams)
            ]
            for running in started:
                running.result()
        if stop is not None and stop.is_set():
            raise KeyboardInterrupt
        row.caption("merging")
        if dest.is_file() and dest.stat().st_size == 0:
            dest.unlink()
        ffmpeg = sh.find("ffmpeg")
        fmt = "webm" if dest.suffix.lower() == ".webm" else "mp4"
        flags = ["-movflags", "+faststart"] if fmt == "mp4" else []
        with fs.stage(dest, keep=False) as part:
            ffmpeg.run(
                "-y", "-i", str(streams[0]), "-i", str(streams[1]),
                "-c", "copy", *flags, "-f", fmt, str(part),
            )
        row.caption("merged")
        return dest
    finally:
        for stream in streams:
            stream.unlink(missing_ok=True)


def _again(attempt: Callable[[], T], *, retryable: tuple[type[BaseException], ...],
           final: tuple[type[BaseException], ...] = ()) -> T:
    """Call `attempt` up to `ATTEMPTS` times while it raises `retryable` but not `final`."""
    number = 1
    while True:
        try:
            return attempt()
        except retryable as error:
            if number >= ATTEMPTS or isinstance(error, final):
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


def _save_fetch(fetch: Fetch, dest: Path, label: str, size: int | None, kind: str | None,
                stop: threading.Event | None) -> Path:
    """Have `fetch(dest, progress)` write into a stage, check it, and rename onto `dest`."""
    # The stage outlives a failed fetch, so a fetch that resumes (Telegram's) picks it up.
    with fs.stage(dest) as part:
        with _Row(label) as row:
            row.start(size)
            fetch(part, partial(row.stoppable, stop=stop))
            try:
                final = _placed(part, dest, kind, size is not None)
            except BaseException:
                # A whole file that fails its check would fail again on resume.
                part.unlink(missing_ok=True)
                raise
        if final != dest:
            os.replace(part, final)
    return final


def _placed(part: Path, dest: Path, kind: str | None, known: bool) -> Path:
    """Where the fetched `part` goes, by `kind` and its content."""
    if kind == paths.SNIFF:
        return _sniffed(part, dest, known)
    return dest if kind is None else _as_media(part, dest, kind, known)


def _as_media(part: Path, dest: Path, kind: str, known: bool) -> Path:
    """Where a downloaded photo or video goes by content; raises on web page or corrupt."""
    extension = paths.checked(part, kind, decode=not known)
    if extension is None or same_extension(extension, dest.suffix):
        return dest
    return dest.with_suffix(extension)


def _sniffed(part: Path, dest: Path, known: bool) -> Path:
    """Where a file whose content decides its kind goes: media by content, else as `dest`."""
    extension = paths.sniff(part)
    if extension in paths.PAGES:
        raise NotMediaError("got a web page instead of a file")
    # `kind_of` reads a name's extension, and ".mp4" alone is a name without one.
    kind = paths.kind_of(f"file{extension}") if extension else None
    if extension is None or kind is None:
        # An archive, a document, or nothing `sniff` knows: kept as it came.
        return dest
    paths.checked(part, kind, decode=not known)
    return dest.with_name(dest.name + extension)


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
