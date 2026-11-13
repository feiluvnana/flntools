"""flnd's HTTP: the source's httpx client, its cookies, and downloads.

Pacing, retries and 429 back-offs are `shared.http.Client`'s (flnd/site.py makes a source's two);
the httpx client here only carries the source's cookies, user agent and signing. A file's transfer
is `Client.download`'s (resume, ranges, the per-host limit, its own three attempts, a 429 waited
out). What stays here is flnd's own: mirrors tried in order and logged, the media check
(`paths.checked`) and naming a file by its content, the DASH audio merge, and a source's `fetch`
function. `download` resumes every file and splits a big one into ranges only on the CDNs in
`SPLIT_HOSTS`.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from typing import (Any, Callable, Iterable, Iterator, Literal, Mapping, MutableMapping, Sequence,
                    TypeVar)
from urllib.parse import urlsplit

import httpx

from flnd.common import paths
from flnd.common.paths import NotMediaError
from flnd.source import Fetch, FetchError, File
from shared import atomic, clock, fmt, http, log

# Tries for one `fetch` function of a file, or one DASH merge, before its error is raised, 1 s,
# then 2 s apart.
ATTEMPTS = 3
# The hosts whose CDNs passed a live check of `Range` and 4 connections at once, so a big file
# from them downloads in ranges; a host matches itself and its subdomains. Every other host gets
# one connection per file. Each entry is a CDN that passed its check by hand; one that failed, or
# that no listing ever served a file from, stays out with the reason.
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
# Extensions that name one format, so a `.jpeg` holding a JPEG keeps its name.
SAME_EXTENSIONS = {".jpeg": ".jpg", ".tiff": ".tif", ".m4v": ".mp4"}

T = TypeVar("T")


# Set by the first Ctrl-C: every thread waiting out a rate limit stops at once, and the job stops
# listing and lets the running downloads finish.
stopping = threading.Event()
# Set by a second Ctrl-C: downloads stop at their next chunk, keeping their resumable parts.
stopping_now = threading.Event()
# The headers `download` is collecting on this thread, which the client's response hook fills
# from each file response (see `_recording`); None while no download is running here.
_seen = threading.local()


def client(*, user_agent: str | None, jar: Iterable[dict],
           sign: Callable[[httpx.Request], Any]) -> httpx.Client:
    """An httpx client for one source: its user agent and cookies, and `sign` on every request sent.

        raw = net.client(user_agent=None, jar=[], sign=lambda request: None)

    Args:
        user_agent: the source's user agent; None sends httpx's own.
        jar: the login's cookies.
        sign: called with each request as it is sent, retries and redirects too, to change its
            headers in place.
    """
    defaults = httpx.Headers({"User-Agent": user_agent} if user_agent else {})
    return httpx.Client(headers=defaults, cookies=cookie_jar(jar), timeout=30.0, follow_redirects=True,
                        transport=_base_transport(),
                        event_hooks={"request": [sign], "response": [_record]})


def cookie_jar(jar: Iterable[dict]) -> httpx.Cookies:
    """A `shared.cookies.Jar`, or any cookie dicts in its shape, as `httpx.Cookies`, keeping domains and paths."""
    cookies = httpx.Cookies()
    for cookie in jar:
        cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""), path=cookie.get("path") or "/")
    return cookies


def download(client: http.Client, file: File, dest: Path, *, label: str,
             headers: Mapping[str, str] | None = None, kind: str | None = None,
             received: MutableMapping[str, str] | None = None) -> Path:
    """Save one `File` under a progress bar, resuming an earlier try, and return the path written.

        net.download(run.files, File(["https://a.example/f.7z", "https://b.example/f.7z"]),
                     Path("f.7z"), label="f.7z")

    Each mirror gets three attempts, each resuming where the last stopped, and a 429 is waited
    out on the same mirror without costing one. A failed mirror is logged with why, and the next
    one tried; only the last mirror's error reaches the caller.
    """
    from shared import media

    if file.url is None:
        raise ValueError("download needs a URL or a fetch function")
    mirrors: list[str | Fetch] = (
        [file.url] if isinstance(file.url, str) or callable(file.url) else list(file.url))
    for number, mirror in enumerate(mirrors, start=1):
        try:
            if callable(mirror):
                save = partial(_save_fetch, mirror, dest, label, file.size, kind)
                final = (NotMediaError, media.UnsafeError)
                return _again(save, retryable=(Exception,), final=final)
            if file.audio is not None:
                return _save_dash(client, mirror, file.audio, dest, label, headers)
            return _save_url(client, mirror, dest, label, headers, file.size, kind, received)
        except http.StoppedError:
            raise KeyboardInterrupt from None
        except http.RefusedError as error:
            refused = FetchError(f"HTTP {error.status} for {fmt.url(str(mirror))}", status=error.status)
            if number == len(mirrors):
                raise refused from None
            reason: BaseException = refused
        except Exception as error:
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


def _base_transport() -> httpx.BaseTransport:
    """The transport under every client: HTTP/2 for small files, HTTP/1.1 for a split file's pieces."""
    return http.Transport()


def _save_url(client: http.Client, url: str, dest: Path, label: str,
              headers: Mapping[str, str] | None, size: int | None, kind: str | None,
              received: MutableMapping[str, str] | None) -> Path:
    """Download one URL, check it as `kind`, and save it under the name its content calls for."""
    seen: dict[str, str] = {}

    def check(part: Path) -> Path:
        # A length the server sent was checked by `http.download` already, so the file is only
        # sniffed; without one (a chunked body), it is decoded, which finds a cut-off photo.
        known = "content-length" in seen or size is not None
        # The name its content calls for, which `http.download` renames the part onto in one
        # step: a crash never leaves `a.jpg` holding a PNG, which the job would take as saved.
        return _sniffed(part, dest, known) if kind == paths.SNIFF else _as_media(part, dest, kind or "", known)

    with _Row(label) as row, _recording(seen):
        transfer = http.Transfer(
            headers=headers, size=size, parts=split_parts(url),
            check=check if kind is not None else None, progress=row.follow(), stop=stopping_now)
        saved = client.download(url, dest, transfer=transfer)
    if received is not None:
        received.update((name.lower(), value) for name, value in saved.headers.items())
    return saved.path


def _save_dash(client: http.Client, video: str, audio: str, dest: Path, label: str,
               headers: Mapping[str, str] | None) -> Path:
    """Download a DASH video and its `audio` stream, and save them merged as `dest`, trying the whole three times."""
    from shared import media

    # Both streams are only inputs, named to match PART_GLOB, so a run killed before the merge
    # leaves nothing `flnd sync`'s repair doesn't remove; deleted however the merge ends.
    streams = [dest.with_name(f".{dest.name}.{name}.part") for name in ("video", "audio")]

    def fetch(url: str, stream: Path, row: _Row) -> None:
        transfer = http.Transfer(headers=headers, parts=split_parts(url),
                                 progress=row.follow(), stop=stopping_now)
        client.download(url, stream, transfer=transfer)

    def merged(row: _Row) -> Path:
        try:
            # Both streams at once, each on its own connections, as a player fetches them; a
            # stream cut off keeps its resumable part for the next try.
            with ThreadPoolExecutor(max_workers=2) as pool:
                started = [pool.submit(fetch, url, stream, row) for url, stream in zip((video, audio), streams)]
                for running in started:
                    running.result()
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
        finally:
            for stream in streams:
                stream.unlink(missing_ok=True)

    with _Row(label) as row:
        # A stream cut short without a length to tell (a chunked body) fails the merge; it may
        # come whole next time. Other merge errors would fail the same way again.
        return _again(partial(merged, row), retryable=(media.BrokenError,))


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
            log.debug("retry {n} in {time}: {error}", n=number, time=fmt.duration(seconds),
                      error=error)
            clock.sleep(seconds)
            number += 1


def split_parts(url: str) -> Literal["auto", 1]:
    """`http.download`'s `parts` for `url`: "auto" on a host in `SPLIT_HOSTS` or under one, else 1 (D6)."""
    host = (urlsplit(url).hostname or "").lower()
    return "auto" if any(host == name or host.endswith(f".{name}") for name in SPLIT_HOSTS) else 1


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
            if kind == paths.SNIFF:
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
    time and catch nothing the size didn't. See `paths.checked` for what raises.
    """
    extension = paths.checked(part, kind, decode=not known)
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
    if extension in paths.PAGES:
        raise NotMediaError("got a web page instead of a file")
    # `kind_of` reads a name's extension, and ".mp4" alone is a name without one.
    kind = paths.kind_of(f"file{extension}") if extension else None
    if extension is None or kind is None:
        # An archive, a document, or nothing `media` knows: kept as it came.
        return dest
    paths.checked(part, kind, decode=not known)
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
