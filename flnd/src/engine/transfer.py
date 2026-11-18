"""flnd's transfers: one post's `File` into its part, by URL, mirrors, DASH pair or `fetch`.

src/docs/architecture explains it.

Who calls it: a target's downloads (`flnd.engine.job.Downloads`), one coroutine per file on the
run's loop. What it calls: the source's `flnkit.net.Client` for URLs, `flnkit.files.part` for
the part, `flnkit.files` for ffmpeg, and `flnkit.files` to tell that a file is the media it
should be. It is flnd's side, not a library: it knows a source's `File` and `Fetch` (`flnd.source`),
and counts each transfer's bytes on its file's track (`flnd.engine.track`).

The flow of one file (`save`):
1. open and lock `<dest>.part` once, before any request, so a file another run is saving raises
   `BusyError` at once (`flnkit.files`);
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

from flnd.errors import Error, SiteError
from flnd.source import Fetch, File
from flnd.ui.log import ui
from flnkit import clock, files, media, net, text

__all__ = ["noon", "same_extension", "save", "served_day"]

# Tries for one `fetch` function of a file, or one DASH merge, before its error is raised.
ATTEMPTS = 3
# A saved file's modified time is noon UTC of its post's day.
NOON = 12
# Extensions that name one format, so a `.jpeg` holding a JPEG keeps its name.
SAME_EXTENSIONS = {".jpeg": ".jpg", ".tiff": ".tif", ".m4v": ".mp4"}

T = TypeVar("T")


async def save(client: net.Client, file: File, dest: Path, *, label: str,
               headers: Mapping[str, str] | None = None, kind: str | None = None,
               received: MutableMapping[str, str] | None = None,
               stop: threading.Event | None = None,
               day: datetime.date | None = None,
               track: Any | None = None) -> files.Saved:
    """Save one `File` under a progress bar, trying mirrors in order.

    Args:
        client: the source's downloading client.
        file: what to fetch: a URL, mirrors, a `fetch` function, or a DASH pair.
        dest: where the file is saved; the extension may change to what its content calls for.
        label: the progress bar's label.
        headers: extra request headers.
        kind: what the file must be; None checks nothing.
        received: filled with the file response's headers.
        stop: an event to stop the transfer; None uses the client's.
        day: the post's day, which becomes the file's modified time; None uses the server's 
            last-modified header, or the time of the write.
        track: the file's lifecycle row; None uses a transient bar.

    Returns:
        The location of the saved file, with its size and modified time.

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
                audio = file.audio if number == 1 else None
                await _mirror(client, mirror, file, held, label, headers, kind, received, halt,
                              audio=audio, track=track)
                break
            # The last mirror's failure is the file's; an earlier one's is a warning, and the
            # next mirror is tried, but a 429 is the scheduler's to wait out on this one. A
            # `Stopped` is a KeyboardInterrupt, so it is never caught.
            except Exception as error:
                if number == len(mirrors) or getattr(error, "status", None) == 429:
                    raise
                reason = error
            # The next mirror may serve other bytes, so it starts from an empty part.
            held.reset()
            ui.warn("mirror {n} failed: {reason}; trying next", n=number, reason=reason)
    assert held.saved is not None
    return held.saved


@asynccontextmanager
async def _held(dest: Path) -> AsyncIterator[files.Part]:
    """`flnkit.files.part(dest, resume=True, digest=True)`, opened and landed on a worker thread.

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
    manager = files.part(dest, resume=True, digest=True)
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
    moment = datetime.datetime.combine(day, datetime.time(NOON), tzinfo=datetime.UTC)
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
        when = when.replace(tzinfo=datetime.UTC)
    return when.astimezone(datetime.UTC).date()


def same_extension(one: str, other: str) -> bool:
    """Whether two extensions name one format: `.jpg` and `.jpeg` do."""
    return (
        SAME_EXTENSIONS.get(one.lower(), one.lower())
        == SAME_EXTENSIONS.get(other.lower(), other.lower())
    )


async def _mirror(
    client: net.Client, mirror: str | Fetch, file: File, held: files.Part, label: str,
    headers: Mapping[str, str] | None, kind: str | None,
    received: MutableMapping[str, str] | None, stop: threading.Event | None,
    *, audio: str | None = None, track: Any | None = None,
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
        audio: separate audio track URL for DASH merge, or None for progressive download.
        track: the file's lifecycle bar; None uses a transient bar.

    Returns:
        None, once the part holds the file.
    """
    if callable(mirror):
        attempt = partial(_save_fetch, mirror, held, label, file.size, kind, stop, track=track)
        return await _again(attempt, worth=_fetch_again)
    dash_audio = (
        audio
        if audio is not None
        else (file.audio if not isinstance(file.url, tuple) or mirror == file.url[0] else None)
    )
    if dash_audio is not None:
        return await _save_dash(client, mirror, dash_audio, held, label, headers, stop,
                                track=track)
    return await _save_url(client, mirror, held, label, headers, file.size, kind, received,
                           stop, track=track)


async def _save_url(client: net.Client, url: str, held: files.Part, label: str,
                    headers: Mapping[str, str] | None, size: int | None, kind: str | None,
                    received: MutableMapping[str, str] | None,
                    stop: threading.Event | None, *, track: Any | None = None) -> None:
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
        track: the file's lifecycle bar; None uses a transient bar.
    """
    row: _Row | Any = track.bar() if track is not None else _Row(label)
    with row:
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


async def _save_dash(client: net.Client, video: str, audio: str, held: files.Part,
                     label: str, headers: Mapping[str, str] | None,
                     stop: threading.Event | None, *, track: Any | None = None) -> None:
    """Download a DASH video and its audio stream, merged into `held`.

    Args:
        client: the source's downloading client.
        video: the video stream's URL.
        audio: the audio stream's URL.
        held: the file's locked part, which ffmpeg writes.
        label: the progress bar's label.
        headers: extra request headers.
        stop: set to stop the transfers.
        track: the file's lifecycle row; None uses a transient bar.
    """
    row: _Row | Any = (
        track.bar() if track is not None else _Row(label)
    )
    with row:
        await _again(
            partial(_dash_merge, client, video, audio, held, headers, stop, row),
            worth=_merge_again,
        )


async def _dash_merge(client: net.Client, video: str, audio: str, held: files.Part,
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
        streams = [parts.enter_context(files.part(dest.with_name(f"{dest.name}.{name}"),
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
        for stream in streams:
            stream.flush()
        if stop is not None and stop.is_set():
            raise KeyboardInterrupt
        row.caption("merging")
        # Looked up before the try: a missing ffmpeg raises here and keeps the streams.
        ffmpeg = files.ffmpeg()
        try:
            await media.join(streams[0], streams[1], held, ffmpeg)
        finally:
            # Merged or not, the streams go: a failed merge (a short stream, a container that
            # can't hold them) is tried again from new downloads.
            for stream in streams:
                stream.drop()
        row.caption("merged")


def _fetch_again(error: Exception) -> bool:
    """Whether a source's `fetch` function is worth another try after `error`.

    Disk errors and site answers are not retried here; the client's own policy covers network
    failures.

    Args:
        error: what the try raised.

    Returns:
        True to try again.
    """
    from flnd.errors import DiskError

    if isinstance(error, (SiteError, DiskError)):
        return False
    return True


def _merge_again(error: Exception) -> bool:
    """Whether a DASH download and merge is worth another try after `error`.

    A stream that came short, or a merge ffmpeg failed (it may have read a short stream), is;
    a refusal or a missing ffmpeg isn't.

    Args:
        error: what the try raised.

    Returns:
        True to try again.
    """
    return isinstance(error, files.RunError) or (isinstance(error, Error) and error.retryable)


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
                     time=text.duration(seconds), error=error)
            await clock.pause(seconds)
            number += 1


async def _save_fetch(fetch: Fetch, held: files.Part, label: str, size: int | None,
                      kind: str | None, stop: threading.Event | None, *,
                      track: Any | None = None) -> None:
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
        track: the file's lifecycle bar; None uses a transient bar.
    """
    # A fetch that resumes (Telegram's) keeps its offsets in `<part>.json`, which goes when the
    # part lands.
    held.side = True
    row: _Row | Any = track.bar() if track is not None else _Row(label)
    with row:
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


def _place(held: files.Part, kind: str, known: bool) -> None:
    """Point `held` at the name its content calls for, or drop it when it isn't `kind`.

    A whole file that fails its check would fail again on resume, so its part is removed.

    Args:
        held: the finished part; its first bytes are read through its descriptor.
        kind: `files.SNIFF`, or what the file must be (`files.IMAGE`, …).
        known: whether the server or the source gave the size.

    Raises:
        SiteError: the content is a web page where media was expected.
    """
    try:
        head = held.head()
        if kind == files.SNIFF:
            held.dest = _sniffed(head, held.dest, known)
        else:
            held.dest = _as_media(head, held.dest, kind, known)
    except BaseException:
        held.drop()
        raise


def _as_media(head: bytes, dest: Path, kind: str, known: bool) -> Path:
    """Where a photo or video starting with `head` goes; raises on a web page."""
    extension = checked(head, kind)
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
    extension = files.sniff(head)
    if extension in files.PAGES:
        raise not_media("a file")
    # `kind_of` reads a name's extension, and ".mp4" alone is a name without one.
    kind = files.kind_of(f"file{extension}") if extension else None
    if extension is None or kind is None:
        # An archive, a document, or nothing `sniff` knows: kept as it came.
        return dest
    checked(head, kind)
    return dest.with_name(dest.name + extension)


class _Row:
    """The progress sink of a file with no track (outside a target's run): it counts nothing,
    and stops the transfer at its next chunk after a second Ctrl-C.

    It has the surface of `flnd.engine.track._Bar`, which a tracked file's transfer counts on.

    Args:
        label: the file's path under its target.
    """

    def __init__(self, label: str):
        self.label = label

    def __enter__(self) -> _Row:
        return self

    def __exit__(self, *error: Any) -> None:
        return None

    def start(self, total: float | None) -> None:
        """A try starts; there is nothing to show."""

    def stoppable(self, done: float, total: float | None = None, *,
                  stop: threading.Event | None = None) -> None:
        """A source's `fetch` function reports; stop on a second Ctrl-C."""
        if stop is not None and stop.is_set():
            raise KeyboardInterrupt

    def follow(self, stop: threading.Event | None = None) -> _Stream:
        """A sink for one more stream, stoppable on Ctrl-C."""
        return _Stream(stop)

    def caption(self, text: str) -> None:
        """A step of the file (merging) starts; there is nothing to show."""


class _Stream:
    """One stream's progress sink with no track: `total` is kept, and `advance` only stops.

    Args:
        stop: an event that stops the stream at its next chunk (a second Ctrl-C).
    """

    def __init__(self, stop: threading.Event | None):
        self._stop = stop
        self.total: float | None = None

    def advance(self, n: float) -> None:
        if self._stop is not None and self._stop.is_set():
            raise KeyboardInterrupt


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


def checked(path: Path | bytes, kind: str) -> str | None:
    """The extension a media file calls for, once validated not a web page.

    Args:
        path: the file, or its first 64 bytes.
        kind: what it must be (`IMAGE`, `VIDEO`, `AUDIO`), named in the error.

    Returns:
        The extension its content calls for, or None when unknown.

    Raises:
        SiteError: the content is a web page or JSON.
    """
    extension = files.sniff(path)
    if extension in files.PAGES:
        raise not_media(files.NOUNS.get(kind, kind))
    return extension
