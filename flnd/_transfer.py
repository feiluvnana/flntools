"""flnd's transfers: one post's `File` into its part, by URL, mirrors, DASH pair or `fetch`.

Who calls it: a target's downloads (`flnd.job.Downloads`), one coroutine per file on the run's
loop. What it calls: the source's `flnd._net.Client` for URLs, `flnd._files.part` for the part,
`flnd._tools` for ffmpeg, and `flnd._files` to tell that a file is the media it should be.

The flow of one file (`save`):
1. open and lock `<dest>.part` once, before any request, so a file another run is saving raises
   `BusyError` at once (`flnd._files`);
2. try each mirror in order, each into that same part (emptied between two mirrors, since they
   may serve other bytes): a URL is streamed by the client, a DASH pair is two streams joined by
   ffmpeg, and a `fetch` function writes the part itself;
3. check the content through the part's descriptor (a web page where a photo should be is a
   `SiteError`), and pick the name its content calls for (`.jpeg` stays, a PNG gets `.png`);
4. give it its post's noon as its modified time, and rename it into place: one rename, one
   `fstat`, and no second open.

Rules a change must keep:
- Every function here runs on the loop. A source's blocking `fetch` function and ffmpeg run on
  a worker thread (`asyncio.to_thread`), so they never stall the loop.
- A `Stopped` (the second Ctrl-C) is a `KeyboardInterrupt`: nothing here catches it, so the part
  is kept for the next run.
"""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import inspect
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from contextlib import ExitStack, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from flnd.errors import Error, SiteError
from flnd.source import Fetch, File
from flnd import _files, _net, _stats, _text, _time, _tools
from flnd._ui import ui

__all__ = ["noon", "same_extension", "save", "served_day"]

# Tries for one `fetch` function of a file, or one DASH merge, before its error is raised.
ATTEMPTS = 3
# A saved file's modified time is noon UTC of its post's day.
NOON = 12
# Extensions that name one format, so a `.jpeg` holding a JPEG keeps its name.
SAME_EXTENSIONS = {".jpeg": ".jpg", ".tiff": ".tif", ".m4v": ".mp4"}

T = TypeVar("T")


async def save(client: _net.Client, file: File, dest: Path, *, label: str,
               headers: Mapping[str, str] | None = None, kind: str | None = None,
               received: MutableMapping[str, str] | None = None,
               stop: threading.Event | None = None,
               day: datetime.date | None = None) -> _files.Saved:
    """Save one `File` under a progress bar, trying mirrors in order.

    Args:
        client: the source's downloading client.
        file: what to fetch: a URL, mirrors, a `fetch` function, or a DASH pair.
        dest: where it lands; the extension may change to what its content calls for.
        label: the progress bar's label.
        headers: extra request headers.
        kind: what the file must be (`_files.IMAGE`, …, `_files.SNIFF`); None checks nothing.
        received: filled with the file response's headers.
        stop: set to stop the transfer; None uses the client's.
        day: the post's day, which becomes the file's modified time (noon UTC); None uses the
            server's `Last-Modified`, else leaves the time of the write.

    Returns:
        Where the file landed, with its size and modified time.

    Raises:
        BusyError: another run is saving this file; nothing was sent.
        Error: the last mirror failed.
    """
    if file.url is None:
        raise ValueError("a download needs a URL or a fetch function")
    halt = stop if stop is not None else client.stop
    mirrors: list[str | Fetch] = (
        [file.url] if isinstance(file.url, str) or callable(file.url) else list(file.url))
    async with _held(dest) as held:
        if day is not None:
            held.mtime = noon(day)
        for number, mirror in enumerate(mirrors, start=1):
            try:
                await _mirror(client, mirror, file, held, label, headers, kind, received, halt)
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


@asynccontextmanager
async def _held(dest: Path) -> AsyncIterator[_files.Part]:
    """`flnd._files.part(dest, resume=True, digest=True)`, opened and landed on a worker thread.

    The part hashes what it is written (SHA-256), for dedupe and `check`.

    Opening and locking a part, and renaming it into place, are file-system calls of 50 to
    150 µs each, more on a busy disk; on the loop they would run one at a time and hold up every
    transfer. On worker threads they overlap, as the transfers do.

    Args:
        dest: where the file lands.

    Yields:
        The locked part.

    Raises:
        BusyError: another run is saving this file.
        DiskError: the part can't be opened, locked or renamed.
    """
    manager = _files.part(dest, resume=True, digest=True)
    held = await _through(manager.__enter__)
    try:
        yield held
    except BaseException as error:
        # The manager keeps the part and raises `error` again (or a `DiskError`).
        await _through(manager.__exit__, type(error), error, error.__traceback__)
        raise
    await _through(manager.__exit__, None, None, None)


async def _through(call: Callable[..., T], *args: Any) -> T:
    """`call(*args)` on a worker thread, waited for to its end even through a cancel.

    A part's file calls must finish before anything else touches its descriptor, so a cancel
    waits for them, then goes on.

    Args:
        call: a blocking function.
        args: its arguments.

    Returns:
        What it returned.
    """
    work = asyncio.ensure_future(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        raise


def noon(day: datetime.date) -> float:
    """Noon UTC of `day`, in epoch seconds: the modified time of a file posted that day."""
    moment = datetime.datetime.combine(day, datetime.time(NOON), tzinfo=datetime.timezone.utc)
    return moment.timestamp()


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


async def _mirror(
    client: _net.Client, mirror: str | Fetch, file: File, held: _files.Part, label: str,
    headers: Mapping[str, str] | None, kind: str | None,
    received: MutableMapping[str, str] | None, stop: threading.Event | None,
) -> None:
    """Download one mirror (fetch function, DASH pair, or URL) into the part `held`.

    Args:
        client: the source's downloading client.
        mirror: a URL or a `fetch` function.
        file: the file, for its size and DASH audio.
        held: the file's locked part.
        label: the progress bar's label.
        headers: extra request headers.
        kind: what the file must be, or None.
        received: filled with the response's headers.
        stop: set to stop the transfer.

    Returns:
        None, once the part holds the file.
    """
    if callable(mirror):
        attempt = partial(_save_fetch, mirror, held, label, file.size, kind, stop)
        return await _again(attempt, worth=_fetch_again)
    if file.audio is not None:
        return await _save_dash(client, mirror, file.audio, held, label, headers, stop)
    return await _save_url(client, mirror, held, label, headers, file.size, kind, received,
                           stop)


async def _save_url(client: _net.Client, url: str, held: _files.Part, label: str,
                    headers: Mapping[str, str] | None, size: int | None, kind: str | None,
                    received: MutableMapping[str, str] | None,
                    stop: threading.Event | None) -> None:
    """Download one URL into `held`, date it, and check it as `kind`.

    Args:
        client: the source's downloading client.
        url: the file.
        held: the file's locked part.
        label: the progress bar's label.
        headers: extra request headers.
        size: the size the source gave, or None.
        kind: what the file must be, or None to check nothing.
        received: filled with the response's headers.
        stop: set to stop the transfer.
    """
    with _Row(label, host=urlsplit(url).hostname or "", resumed=held.size) as row:
        seen = await client.fill(url, held, headers=headers, progress=row.follow(stop))
    if received is not None:
        received.update(seen)
    if held.mtime is None:
        served = served_day(seen.get("last-modified"))
        if served is not None:
            held.mtime = noon(served)
    if kind is not None:
        # Its first bytes are read (and the buffered tail written) off the loop.
        await _through(_place, held, kind, "content-length" in seen or size is not None)


async def _save_dash(client: _net.Client, video: str, audio: str, held: _files.Part,
                     label: str, headers: Mapping[str, str] | None,
                     stop: threading.Event | None) -> None:
    """Download a DASH video and its audio stream, merged into `held`.

    Args:
        client: the source's downloading client.
        video: the video stream's URL.
        audio: the audio stream's URL.
        held: the file's locked part, which ffmpeg writes.
        label: the progress bar's label.
        headers: extra request headers.
        stop: set to stop the transfers.
    """
    with _Row(label, host=urlsplit(video).hostname or "") as row:
        await _again(
            partial(_dash_merge, client, video, audio, held, headers, stop, row),
            worth=_merge_again,
        )


async def _dash_merge(client: _net.Client, video: str, audio: str, held: _files.Part,
                      headers: Mapping[str, str] | None, stop: threading.Event | None,
                      row: _Row) -> None:
    """Fetch DASH video and audio streams at once and merge them into `held`.

    Each stream is a part of its own beside the file (`<name>.video.part`), locked like any
    other, so a failed try resumes it and a sweep clears it. The streams never land: once
    merged they are dropped.

    Args:
        client: the source's downloading client.
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
        # Both streams at once, on the loop; the first failure cancels the other, whose part
        # keeps what it had for the next try.
        fills = [asyncio.ensure_future(client.fill(url, stream, headers=headers,
                                                   progress=row.follow(stop)))
                 for url, stream in zip((video, audio), streams)]
        try:
            await asyncio.gather(*fills)
        except BaseException:
            for running in fills:
                running.cancel()
            await asyncio.gather(*fills, return_exceptions=True)
            raise
        if stop is not None and stop.is_set():
            raise KeyboardInterrupt
        row.caption("merging")
        ffmpeg = _tools.ffmpeg()
        fmt = "webm" if dest.suffix.lower() == ".webm" else "mp4"
        flags = ["-movflags", "+faststart"] if fmt == "mp4" else []
        try:
            # ffmpeg truncates and rewrites the locked part in place (`-y`), by its name. It
            # runs on a worker thread, so the loop goes on with other transfers meanwhile.
            await asyncio.to_thread(
                ffmpeg.run,
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


async def _again(attempt: Callable[[], Awaitable[T]], *,
                 worth: Callable[[Exception], bool]) -> T:
    """Await `attempt()` up to `ATTEMPTS` times while what it raises is `worth` another try.

    The tries wait 1, then 2 s between them. A `KeyboardInterrupt` (`Stopped`) is never caught.

    Args:
        attempt: makes one try; what it returns is returned.
        worth: whether an exception the try raised is worth another; when it isn't, or the
            tries are used up, the exception is raised.

    Returns:
        What the try that went through returned.
    """
    number = 1
    while True:
        try:
            return await attempt()
        except Exception as error:
            if number >= ATTEMPTS or not worth(error):
                raise
            seconds = 2.0 ** (number - 1)
            ui.debug("retry {n} in {time}: {error}", n=number,
                     time=_text.duration(seconds), error=error)
            await _time.pause(seconds)
            number += 1


async def _save_fetch(fetch: Fetch, held: _files.Part, label: str, size: int | None,
                      kind: str | None, stop: threading.Event | None) -> None:
    """Have `fetch(path, progress)` write into the part `held`, then check it as `kind`.

    An `async def` fetch (Telegram's) runs on the loop, where its SDK lives; a plain one runs on
    a worker thread, since it may block.

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
        progress = partial(row.stoppable, stop=stop)
        if inspect.iscoroutinefunction(fetch):
            await fetch(held.path, progress)
        else:
            outcome = await asyncio.to_thread(fetch, held.path, progress)
            if inspect.isawaitable(outcome):
                await outcome
    if kind is not None:
        await _through(_place, held, kind, size is not None)


def _place(held: _files.Part, kind: str, known: bool) -> None:
    """Point `held` at the name its content calls for, or drop it when it isn't `kind`.

    A whole file that fails its check would fail again on resume, so its part is removed.

    Args:
        held: the finished part; its first bytes are read through its descriptor.
        kind: `_files.SNIFF`, or what the file must be (`_files.IMAGE`, …).
        known: whether the server or the source gave the size.

    Raises:
        SiteError: the content is a web page where media was expected.
    """
    try:
        head = held.head()
        if kind == _files.SNIFF:
            held.dest = _sniffed(head, held.dest, known)
        else:
            held.dest = _as_media(head, held.dest, kind, known)
    except BaseException:
        held.drop()
        raise


def _as_media(head: bytes, dest: Path, kind: str, known: bool) -> Path:
    """Where a photo or video starting with `head` goes; raises on a web page."""
    extension = checked(head, kind, decode=not known)
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
    extension = _files.sniff(head)
    if extension in _files.PAGES:
        raise not_media("a file")
    # `kind_of` reads a name's extension, and ".mp4" alone is a name without one.
    kind = _files.kind_of(f"file{extension}") if extension else None
    if extension is None or kind is None:
        # An archive, a document, or nothing `sniff` knows: kept as it came.
        return dest
    checked(head, kind, decode=not known)
    return dest.with_name(dest.name + extension)


class _Row:
    """One file's progress (or DASH video and audio's), opened on its first response.

    The row only counts (`drawn=False`): a run's progress is shown from its events, and from
    the live stats, which read each row in flight (`label`, `host`, `resumed`, `bar`) on the loop.

    Its streams report from the loop, and a blocking `fetch` function from its worker thread,
    so a lock guards the bar.

    Args:
        label: the bar's label, the file's path under its target.
        host: where the file comes from, for the live stats; "" for a source's `fetch`.
        resumed: the bytes its part already held from an earlier run (a resume); 0 for none.
    """

    def __init__(self, label: str, *, host: str = "", resumed: int = 0):
        self.label, self.stack, self.base = label, ExitStack(), 0.0
        self.host, self.resumed = host, resumed
        self.bar: Any = None
        self._lock = threading.Lock()

    def __enter__(self) -> _Row:
        # Entered and left on the loop, where the live stats read it.
        self.stack.enter_context(_stats.tracked(_stats.TRANSFER, self))
        return self

    def __exit__(self, *error: Any) -> None:
        self.stack.__exit__(*error)

    def start(self, total: float | None) -> None:
        """Open the bar for a `fetch` function, or count a new try on from where it is.

        Args:
            total: the file's size, or None when unknown.
        """
        with self._lock:
            if self.bar is None:
                self.bar = self.stack.enter_context(
                    ui.progress(self.label, total=total, unit="bytes", drawn=False),
                )
                return
            self.base = self.bar.count
            self.bar.total = (
                self.base + total if total is not None and self.bar.total is not None else None
            )

    def update(self, done: float, total: float | None = None) -> None:
        """Set where a `fetch` function stands.

        Args:
            done: bytes written by this try.
            total: the file's size, when the function learned it.
        """
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
        """Open the bar on a stream's first total, or swap the stream's `old` total for `new`.

        Args:
            old: the stream's total before, or None.
            new: its total now, or None when unknown (the bar's total is then unknown too).
        """
        with self._lock:
            if self.bar is None:
                self.bar = self.stack.enter_context(
                    ui.progress(self.label, total=new, unit="bytes", drawn=False),
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
    """One stream's progress sink on a shared `_Row`.

    Args:
        row: the file's bar.
        stop: an event that stops the stream at its next chunk (a second Ctrl-C).
    """

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


# What a web page where media was expected says to do: such a page is most often a login wall.
NOT_MEDIA_HINT = "it is usually a login wall: run flnd login <source>, then try again"


def not_media(noun: str) -> SiteError:
    """The error for a web page or JSON where media was expected: a login wall or error page.

    It isn't retried: the same request gets the same page until something changes.

    Args:
        noun: what was expected, as the message says it ("a photo", "a file").

    Returns:
        A `SiteError` saying so, with a hint to log in again.
    """
    return SiteError(f"got a web page instead of {noun}", hint=NOT_MEDIA_HINT)


def checked(path: Path | bytes, kind: str, *, decode: bool = True) -> str | None:
    """The extension a media file calls for, once validated not a web page.

    Args:
        path: the file, or its first 64 bytes.
        kind: what it must be (`IMAGE`, `VIDEO`, `AUDIO`), named in the error.
        decode: unused, kept for the callers' signature.

    Returns:
        The extension its content calls for, or None when unknown.

    Raises:
        SiteError: the content is a web page or JSON.
    """
    del decode
    extension = _files.sniff(path)
    if extension in _files.PAGES:
        raise not_media(_files.NOUNS.get(kind, kind))
    return extension
