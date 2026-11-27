"""What a plugin declares, the types it yields and reads, `Ctx`, and its helpers.

SPEC.md#for-developers explains it.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import functools
import inspect
import re
import string
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
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from types import MappingProxyType, MethodType
from typing import Any, ClassVar, Protocol, TypeVar
from urllib.parse import parse_qs, unquote, urlsplit

from flnd.errors import (
    Blocked,
    Changed,
    Error,
    Hidden,
    LoginError,
    NetworkError,
    NotFound,
    PluginError,
    SiteError,
    UsageError,
)
from flnkit import text
from flnkit.dig import dig
from flnkit.html import Document, Element

__all__ = [
    "Blocked", "Browser", "Category", "Changed", "Clock", "Cookies", "Ctx",
    "DateLike", "Document", "Element", "Error", "Fetch", "File", "Hidden", "Landed", "Login",
    "LoginError", "NetworkError", "NotFound", "Part", "Plugin", "PluginError", "Post", "Range",
    "Request", "Response", "Route", "Settings", "SiteError", "Stream", "Target", "UsageError",
    "best", "cookies", "dig", "headline", "meta", "numbered", "page_info", "route",
]

# Hosts that are the same site as their bare domain: desktop, mobile and web variants.
SITE_PREFIXES = ("www.", "m.", "mobile.", "web.", "mbasic.", "touch.")
# What `{name}` matches without a regex: a user name as the sites spell them, one leading `@`
# dropped, since people paste names both ways.
NAME = r"[\w.]+"
# Where a generated form shows a `*`: any one segment, which help can't name.
ANY = "…"
# The fields the engine fills for every file, which a template may use beside `fields`.
TEMPLATE_FIELDS = (
    "plugin", "target", "category", "post", "num", "count", "index", "extension", "path",
    "date", "query", "dir", "file",
)
# The fields a `Post` may not set: the engine's own, so a plugin never overwrites one.
RESERVED = frozenset(TEMPLATE_FIELDS) | {"id", "files", "headers", "listed", "pinned"}
# The ways a cookie site's login is read: a cookies.txt file, given by its path.
COOKIE_METHODS = ("file",)
# What a `File` slot holds when it isn't given; None for the rest.
FILE_DEFAULTS = {"mirrors": (), "ranged": False, "keep": False}
_FORMATTER = string.Formatter()

T = TypeVar("T")
# What `Post(date=)` and `Post(listed=)` take; the engine reads each once (`flnkit.clock.day`).
DateLike = datetime.date | int | float | str | None


# ---------------------------------------------------------------------------------------------
# Values: targets, logins, requests and answers
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """One thing to download, as people write it: `ins:example_user`, `mht?romance` or a link.

    A route or category yields one to run it as its own target. `Plugin.resolve` gives one
    with `kind` set to the route that reads it, and its text written `code:value`. Two targets
    are equal when their text and kind are.

    Args:
        text: the target as typed.
        kind: the route that reads it; "" until it is resolved.
    """

    text: str
    kind: str = field(default="", kw_only=True)

    @property
    def plugin(self) -> str:
        """The plugin's code, such as "ins"; "" for a link or bare text."""
        return _split_target(self.text)[0]

    @property
    def value(self) -> str:
        """What it points to: the text after `code:` or `code?`, or the whole link."""
        return _split_target(self.text)[2]

    @property
    def query(self) -> bool:
        """Whether it is a query (`code?words`) rather than `code:value`."""
        return _split_target(self.text)[1]

    def __str__(self) -> str:
        plugin, query, value = _split_target(self.text)
        if not plugin:
            return value
        if not value:
            return plugin
        return f"{plugin}{'?' if query else ':'}{value}"


@functools.lru_cache(maxsize=4096)
def _split_target(written: str) -> tuple[str, bool, str]:
    """`written` as (code, whether a query, value); a link or bare text has no code."""
    written = written.strip()
    head = written.split("?", 1)[0].split("/", 1)[0]
    if "://" in head or written.lower().startswith("http"):
        return "", False, written
    found = re.match(r"([a-z][a-z0-9]*)([:?])", written)
    if found is None:
        return "", False, written
    return found[1], found[2] == "?", written[found.end():]


@dataclass(frozen=True)
class LoggedIn:
    """A plugin's login, as the logins folder keeps it.

    Args:
        plugin: the plugin's code.
        status: "ok", "expires in N days" or "expired".
        file: the login's file.
        expires: when its login cookies expire, as ISO 8601 UTC; None when they don't.
    """

    plugin: str
    status: str
    file: str
    expires: str | None = None


@dataclass(frozen=True, kw_only=True)
class Request:
    """One HTTP request: what `ctx.fetch` and `ctx.pages` send, and what `sign` adds to.

    Args:
        url: the whole URL.
        method: "GET", "POST", ...
        params: the query, added to the URL's own; None for none.
        body: bytes or text as they are, anything else as JSON; None for no body.
        headers: headers of its own, besides the client's; None for none.
        check: raise for a refusal; False returns the refusal's response to read.
    """

    url: str = field(kw_only=False)
    method: str = "GET"
    params: Mapping[str, Any] | None = None
    body: Any = None
    headers: Mapping[str, str] | None = None
    check: bool = True

    def replace(self, **changes: Any) -> Request:
        """A copy with some fields changed, such as `request.replace(headers={...})`."""
        return dataclasses.replace(self, **changes)


class Response(Protocol):
    """One HTTP answer, as `ctx.fetch`, `ctx.pages` and `blocked` see it."""

    @property
    def status(self) -> int:
        """The status code."""
        ...

    @property
    def url(self) -> str:
        """The URL it came from, after redirects."""
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """Its headers, by lowercase name."""
        ...

    @property
    def content(self) -> bytes:
        """The body's bytes."""
        ...

    @property
    def text(self) -> str:
        """The body as text, in the charset its `Content-Type` names, or UTF-8."""
        ...

    @property
    def json(self) -> Any:
        """The body parsed as JSON; `Changed` when it isn't JSON."""
        ...

    @property
    def ok(self) -> bool:
        """Whether the status is 2xx."""
        ...

    @property
    def html(self) -> Document:
        """The body parsed as an HTML document."""
        ...


@dataclass(frozen=True)
class Landed:
    """Where `ctx.open` ended up: the tab's URL after redirects, and the page's status.

    Args:
        url: the URL the tab shows.
        status: the main document's status code.
    """

    url: str
    status: int


@dataclass(frozen=True)
class Stream:
    """One quality of a video or an audio track, as a manifest or an API lists them.

    Args:
        url: where it is.
        height: its height in pixels; 0 when unknown or for audio.
        width: its width in pixels; 0 when unknown or for audio.
        bitrate: its bits a second; 0 when unknown.
        audio: the URL of the audio to join with it; None when it has its own sound.
    """

    url: str
    height: int = 0
    width: int = 0
    bitrate: int = 0
    audio: str | None = None


@dataclass(frozen=True)
class Range:
    """The run's `when`: two days, both included; None is no limit on that side.

    Args:
        since: the first day kept.
        until: the last day kept.
    """

    since: datetime.date | None = None
    until: datetime.date | None = None

    def __contains__(self, value: object) -> bool:
        """Whether a date is in range; an undated post (None, or text that isn't a date) is."""
        # Imported here: flnkit.clock brings in asyncio, which `import flnd.plugin` needn't.
        from flnkit import clock

        found = clock.day(value)
        if found is None:
            return True
        return not ((self.since and found < self.since) or (self.until and found > self.until))


class Clock(Protocol):
    """The run's clock, as `ctx.time`: faked by the test kit, so a plugin never reads `time`."""

    def now(self) -> float:
        """Monotonic seconds, for measuring how long something took."""
        ...

    def wall(self) -> float:
        """Unix seconds now."""
        ...

    def day(self) -> datetime.date:
        """Today's UTC day."""
        ...


# ---------------------------------------------------------------------------------------------
# What a listing yields: posts and their files
# ---------------------------------------------------------------------------------------------

class Part:
    """One stretch of a `Fetch` file: where it starts, where it stops, and where bytes go.

    Args:
        sink: writes the bytes, in order, from `offset`.
        offset: where the stretch starts, in bytes from the file's start.
        end: where it stops, exclusive; None means the file's end.
        sized: told the file's whole size, when the fetch learns it.

    Raises:
        ValueError: `offset` is negative, or `end` is before it.
    """

    def __init__(self, sink: Callable[[bytes], Awaitable[None]], *, offset: int = 0,
                 end: int | None = None, sized: Callable[[int], None] | None = None):
        if offset < 0 or (end is not None and end < offset):
            raise ValueError(f"a part runs from {offset} to {end}")
        self.offset = offset
        self.end = end
        self._sink = sink
        self._sized = sized
        self._at = offset

    async def write(self, data: bytes) -> None:
        """Write the next bytes of the stretch.

        Raises:
            ValueError: they run past `end`; the fetch must stop there.
        """
        if self.end is not None and self._at + len(data) > self.end:
            raise ValueError(f"{len(data)} bytes at {self._at} run past the part's end, "
                             f"{self.end}")
        await self._sink(data)
        self._at += len(data)

    def total(self, size: int) -> None:
        """Say the whole file's size in bytes, once the fetch knows it.

        Raises:
            ValueError: `size` isn't a whole number from 0.
        """
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"a file's size is a whole number of bytes, not {size!r}")
        if self._sized is not None:
            self._sized(size)


# A file without a URL: fetches its `Part`, from `part.offset` until `part.end`.
Fetch = Callable[[Part], Awaitable[None]]


@dataclass(init=False, frozen=True, slots=True)
class File:
    """One file of a post: a URL, mirror URLs, or a fetch function, and how to save it.

    `File.text` and `File.stream` make the two files that have none of the three.
    `File.frames` makes a URL file the engine turns into a video.

    Args:
        url: its URL.
        mirrors: URLs of the same file, tried in order.
        fetch: an async function that writes the file's `Part`, for a file with no URL.
        audio: a DASH audio stream to join with the video; with `mirrors`, only with the first.
        name: the name to save it as, instead of the one `filename` makes. A name with its
            own extension (`cover_12.png`) keeps it; a stem (`cover_12`) gets its extension
            from the content type, else from the URL.
        size: its size in bytes, when the site says it.
        password: an archive's password: it is unpacked with it once saved.
        unpack: where an archive goes once saved. None (the default) extracts it beside itself,
            into a folder of its name, and keeps it. `"here"` extracts it into the post's
            folder, lifts a lone top folder's contents up one level, never replaces a file
            already there (a clash is saved as `name (2).ext`), and deletes the archive after a
            successful extract; a failed extract keeps it.
        ranged: `fetch` can start and stop anywhere, so the engine may split it into parts.

    Raises:
        PluginError: not exactly one of `url`, `mirrors` and `fetch`, or an option that doesn't
            fit the one given.
    """

    url: str | None
    mirrors: tuple[str, ...]
    fetch: Fetch | None
    audio: str | None
    name: str | None
    size: int | None
    password: str | None
    unpack: str | None
    ranged: bool
    # A text file's text, written as UTF-8.
    content: str | None
    # A segmented stream's playlist URL, and the headers its segments are fetched with.
    playlist: str | None
    headers: Mapping[str, str] | None
    # A zip of frames' (file name, milliseconds) pairs in play order, and whether the zip stays.
    delays: tuple[tuple[str, int], ...] | None
    keep: bool

    def __init__(self, url: str | None = None, *, mirrors: Sequence[str] = (),
                 fetch: Fetch | None = None, audio: str | None = None, name: str | None = None,
                 size: int | None = None, password: str | None = None,
                 unpack: str | None = None, ranged: bool = False):
        if isinstance(mirrors, str):
            raise PluginError("File(mirrors=...) takes a list of URLs, not one; "
                              "give one URL as File(url)")
        given = [label for label, value in (("url", url), ("mirrors", mirrors), ("fetch", fetch))
                 if value]
        if len(given) != 1:
            raise PluginError("File takes exactly one of url, mirrors and fetch, not "
                              f"{' and '.join(given) or 'none'}")
        if url is not None and not isinstance(url, str):
            raise PluginError(f"File(url=...) takes a URL, not {url!r}")
        if not all(isinstance(mirror, str) and mirror for mirror in mirrors):
            raise PluginError(f"File(mirrors=...) takes URLs, not {mirrors!r}")
        if fetch is not None and not callable(fetch):
            raise PluginError(f"File(fetch=...) takes an async function, not {fetch!r}")
        if audio is not None and fetch is not None:
            raise PluginError("File(audio=...) joins a URL's video with its audio; a fetch file "
                              "has none")
        if ranged and fetch is None:
            raise PluginError("File(ranged=True) is for a fetch file; the engine splits a URL's "
                              "file itself")
        if unpack not in (None, "here"):
            raise PluginError(f"File(unpack=...) is \"here\" or None, not {unpack!r}")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise PluginError(f"File(size=...) is a whole number of bytes, not {size!r}")
        self._fill(url=url, mirrors=tuple(mirrors), fetch=fetch, audio=audio, name=name,
                   size=size, password=password, unpack=unpack, ranged=ranged)

    def _fill(self, **values: Any) -> None:
        """Set every field, from its own constructors only: `values`, and the defaults."""
        for known in dataclasses.fields(self):
            object.__setattr__(self, known.name,
                               values.get(known.name, FILE_DEFAULTS.get(known.name)))

    @classmethod
    def text(cls, name: str, text: str) -> File:
        """A text file the plugin writes itself, such as `info.md`, saved atomically as UTF-8.

        Args:
            name: its name in the post's folder, extension included.
            text: what it holds.
        """
        made = cls.__new__(cls)
        made._fill(name=name, content=text)
        return made

    @classmethod
    def dash(cls, manifest: str | None, *, height: int | None = None) -> File | None:
        """The best video and best audio of an inline DASH manifest (MPD XML), as one file.

        Args:
            manifest: the manifest's XML; None or "" for none.
            height: the tallest video wanted, in pixels; None takes the tallest there is.

        Returns:
            `File(video, audio=audio)`; None when there is no manifest, it isn't XML, it is
            segmented, or it lacks a video at or under `height` or an audio with an https URL.
        """
        streams = _manifest_streams(manifest)
        if streams is None:
            return None
        video = best(streams[0], height=height)
        audio = best(streams[1])
        if video is None or audio is None:
            return None
        return cls(video.url, audio=audio.url)

    @classmethod
    def stream(cls, playlist: str, *, headers: Mapping[str, str] | None = None) -> File:
        """A segmented stream: an HLS `.m3u8` or DASH `.mpd` playlist, fetched in segments.

        Args:
            playlist: the playlist's URL.
            headers: headers for the playlist and each segment, such as a Referer.

        Raises:
            PluginError: `playlist` isn't an http or https URL.
        """
        if not isinstance(playlist, str) or not playlist.lower().startswith(("http://",
                                                                             "https://")):
            raise PluginError(f"File.stream takes a playlist's URL, not {playlist!r}")
        made = cls.__new__(cls)
        made._fill(playlist=playlist, headers=_frozen(headers))
        return made

    @classmethod
    def frames(cls, url: str, delays: Sequence[tuple[str, int]], *, keep: bool = False) -> File:
        """A zip of numbered frames, saved as one MP4 that the engine's ffmpeg makes.

        Args:
            url: the zip's URL.
            delays: (frame file name, milliseconds) in play order, each name in the zip.
            keep: also keep the zip, never extracted, beside the video as `<name>.zip`.

        Raises:
            PluginError: `url` isn't an http or https URL, `delays` is empty, or a pair isn't
                a name and a positive whole number of milliseconds.
        """
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise PluginError(f"File.frames takes the zip's URL, not {url!r}")
        pairs = []
        for pair in delays if not isinstance(delays, str) else ():
            if not (isinstance(pair, (tuple, list)) and len(pair) == 2
                    and isinstance(pair[0], str)):
                raise PluginError(f"File.frames takes (name, milliseconds) pairs, not {pair!r}")
            wait = pair[1]
            if not isinstance(wait, int) or isinstance(wait, bool) or wait <= 0:
                raise PluginError(f"File.frames: a delay is a positive whole number of "
                                  f"milliseconds, not {wait!r}")
            pairs.append((pair[0], wait))
        if not pairs:
            raise PluginError("File.frames needs at least one (name, milliseconds) pair")
        made = cls.__new__(cls)
        made._fill(url=url, delays=tuple(pairs), keep=bool(keep))
        return made


@dataclass(init=False, frozen=True, slots=True)
class Post:
    """One post a listing yields: its id, its files, its dates, and fields for the templates.

    Args:
        id: the post's id as the site has it; `{post}` in the templates.
        files: its files in order, or an async function giving them, called only once the post
            is known to be new (a lazy post).
        date: when it was posted, which names its files; None for an undated post.
        listed: the date its listing is ordered by; None means `date`.
        headers: headers for downloading each of its files, such as a CDN's Referer.
        pinned: a post the site shows above its date order; saved like any other.
        owner: whose post it is, as a user name; read only on a `single` route, which files it
            under `media/<code>/<owner>/`. None: the post's id names the folder.
        category: its category by the site's data (`posts`, `reels`); read only on a `single`
            route, with an `owner`.
        **fields: the site's own fields (`caption`, `title`, ...), for templates.

    Raises:
        PluginError: no id, a field the engine sets, `files` holding something not a `File`,
            or `pinned` not True or False.
    """

    id: str
    files: tuple[File, ...] | Callable[[], Awaitable[Sequence[File]]]
    date: DateLike
    # `listed` as given; the property falls back to `date`.
    _listed: DateLike
    headers: Mapping[str, str] | None
    pinned: bool
    owner: str | None
    category: str | None
    fields: Mapping[str, Any]

    def __init__(self, id: str | int,
                 files: Sequence[File] | Callable[[], Awaitable[Sequence[File]]] = (), *,
                 date: DateLike = None, listed: DateLike = None,
                 headers: Mapping[str, str] | None = None, pinned: bool = False,
                 owner: str | None = None, category: str | None = None, **fields: Any):
        if id is None or not str(id).strip():
            raise PluginError(f"Post({id!r}): a post needs an id, the one the site gives it")
        if not isinstance(pinned, bool):
            raise PluginError(f"Post {id}: pinned is True or False, not {pinned!r}")
        reserved = sorted(RESERVED & fields.keys())
        if reserved:
            raise PluginError(f"Post {id}: {', '.join(reserved)} is the engine's; name the "
                              "site's field otherwise")
        if not callable(files):
            if isinstance(files, (str, bytes)) or not isinstance(files, Iterable):
                raise PluginError(f"Post {id}: files is a list of File, or an async function "
                                  f"giving them, not {files!r}")
            files = tuple(files)
            stray = next((item for item in files if not isinstance(item, File)), None)
            if stray is not None:
                raise PluginError(f"Post {id}: its files hold {stray!r}; wrap each in File(...)")
        # Frozen: its own constructor is the one place that sets its fields.
        for name, value in (("id", str(id)), ("files", files), ("date", date),
                            ("_listed", listed), ("headers", _frozen(headers)),
                            ("pinned", pinned), ("owner", str(owner) if owner else None),
                            ("category", str(category) if category else None),
                            ("fields", MappingProxyType(dict(fields)))):
            object.__setattr__(self, name, value)

    @property
    def listed(self) -> DateLike:
        """The date the listing is ordered by: `listed`, or `date` when it wasn't given."""
        return self.date if self._listed is None else self._listed

    @property
    def lazy(self) -> bool:
        """Whether its files are a function, called only once the post is known to be new."""
        return callable(self.files)

    def replace(self, **changes: Any) -> Post:
        """A copy with `id`, `files`, `date`, `listed`, `headers`, `pinned`, `owner`, `category`
        or fields changed."""
        return Post(
            changes.pop("id", self.id), changes.pop("files", self.files),
            date=changes.pop("date", self.date), listed=changes.pop("listed", self._listed),
            headers=changes.pop("headers", self.headers),
            pinned=changes.pop("pinned", self.pinned), owner=changes.pop("owner", self.owner),
            category=changes.pop("category", self.category), **{**self.fields, **changes},
        )


# ---------------------------------------------------------------------------------------------
# Routes and categories
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Link:
    """A URL on a plugin's site: its bare host, its path parts and its query."""

    host: str
    parts: tuple[str, ...]
    query: Mapping[str, str]


class _Pattern:
    """One compiled pattern: a link pattern matches a `_Link`, a code pattern the text after it."""

    def __init__(self, written: str | tuple[str, str]):
        if isinstance(written, tuple) and len(written) == 2 and all(
                isinstance(part, str) for part in written):
            pattern, template = written
        elif isinstance(written, str):
            pattern, template = written, None
        else:
            raise PluginError(f"pattern {written!r}: write a pattern, or a (pattern, value) "
                              "pair such as ('example.com/c/{chat:\\\\d+}', '-100{chat}')")
        self.text, self.template = pattern, template
        prefix = None if _is_url(pattern) else re.match(r"[a-z][a-z0-9]*(?=[:?])", pattern)
        # A code pattern starts with the plugin's code (`exm:{user}`); matching reads past it.
        self.code = prefix[0] if prefix else None
        body = pattern[prefix.end():] if prefix else pattern
        self.body = body[1:] if body.startswith(":") else body
        path, mark, query = _split(self.body, "?", once=True)
        segments = [segment for segment in _split(path, "/") if segment]
        first = segments[0] if segments else ""
        # The first segment is a host when it has a dot and no placeholder.
        self.host = (_bare_host(first.lower())
                     if "." in first and not set(first) & set("{}*") else None)
        self.is_query = self.host is None and not segments and mark == "?"
        if self.host is None and mark and not self.is_query:
            raise PluginError(f"pattern {pattern!r}: only a link pattern can name a query")
        self.names: list[str] = []
        if self.is_query:
            self.regexes: list[re.Pattern[str]] = []
            self.keys: list[list[str]] = []
            self.query = [("", _compile(query, pattern, self.names))]
        else:
            self.segments = segments[1:] if self.host is not None else ["/".join(segments)]
            self.keys = ([_split(pair, "=", once=True) for pair in _split(query, "&")]
                         if mark else [])
            if any(not key or not value for key, _, value in self.keys):
                raise PluginError(f"pattern {pattern!r}: write a query as ?key={{name}}")
            self.regexes = [_compile(segment, pattern, self.names) for segment in self.segments]
            self.query = [(key, _compile(value, pattern, self.names))
                          for key, _, value in self.keys]
        # How specific it is: its literal segments and query keys, the host being one; a code
        # pattern has none.
        self.literals = 0 if self.host is None else 1 + len(self.keys) + sum(
            1 for segment in self.segments if not set(segment) & set("{}*"))

    def match(self, written: str, link: _Link | None) -> dict[str, str] | None:
        """The captures when `written` or `link` matches; None when it doesn't.

        A link pattern matches a link on its host whose first path parts match its segments,
        later parts allowed, so `example.com/{user}` also reads `example.com/example_user/tagged`.
        """
        if self.is_query:
            if link is not None or not written.startswith("?"):
                return None
            return {self.names[0]: written[1:]}
        if self.host is None:
            if link is not None or written.startswith("?"):
                return None
            found = self.regexes[0].fullmatch(written)
            return None if found is None else found.groupdict()
        if link is None or link.host != self.host or len(link.parts) < len(self.regexes):
            return None
        found = [regex.fullmatch(part) for regex, part in zip(self.regexes, link.parts)]
        found += [regex.fullmatch(link.query[key]) if key in link.query else None
                  for key, regex in self.query]
        if not all(found):
            return None
        captures: dict[str, str] = {}
        for match in found:
            captures.update(match.groupdict())  # type: ignore[union-attr]
        return captures

    def value(self, captures: Mapping[str, str]) -> str:
        """The target's value from a match: the template filled, or the one placeholder."""
        if self.template is not None:
            return self.template.format_map(captures)
        return captures[self.names[0]] if self.names else ""

    def form(self, code: str) -> str:
        """The pattern as people write it: `exm:<user>` or `https://www.example.com/<user>/`."""
        if self.is_query:
            return f"{code}?<{self.names[0]}>"
        path = "/".join(_shown(segment) for segment in self.segments)
        if self.host is None:
            return f"{code}:{path}"
        # As a browser's address bar shows it: www. on a bare domain, a slash after a folder.
        host = f"www.{self.host}" if self.host.count(".") == 1 else self.host
        query = "&".join(f"{key}={_shown(value)}" for key, _, value in self.keys)
        slash = "" if query or "." in path.rsplit("/", 1)[-1] else "/"
        return f"https://{host}/{path}{slash if path else ''}" + (f"?{query}" if query else "")


class Category:
    """One part of a route's target (`posts`, `reels`, `tagged`), made by `@<route>.category`.

    Its method is an `async def` generator taking `(self, ctx, profile)`, where `profile` is
    what the route returned, and yields `Post`s and `Target`s.

    Args:
        function: the method.
        own: the account's own content, taken by default; False is other people's, taken only
            with `|all` or when named.
        url: a template over `profile`: the page the engine opens ahead in a warm tab.
        newest_first: its listing comes newest first, so a sync may stop at old posts.
        folder: its files' folder template, instead of the route's or the plugin's.
        key: its files' history key template, instead of the plugin's.

    Raises:
        PluginError: the method doesn't take `(self, ctx, profile)`, or `url` is malformed.
    """

    def __init__(self, function: Callable[..., Any], *, own: bool = True, url: str | None = None,
                 newest_first: bool = True, folder: str | None = None, key: str | None = None):
        self.function = function
        self.name = function.__name__
        self.own, self.url, self.newest_first = own, url, newest_first
        self.folder, self.key = folder, key
        parameters = list(inspect.signature(function).parameters)
        if len(parameters) != 3:
            raise PluginError(f"category {self.name}: take (self, ctx, profile), not "
                              f"({', '.join(parameters)})")
        if url is not None:
            try:
                text.fields(url)
            except ValueError as error:
                raise PluginError(f"category {self.name}: url {url!r}: {error}") from None

    def link(self, profile: Mapping[str, Any]) -> str:
        """`url` filled from `profile`; "" when there is none or a key `profile` lacks."""
        if self.url is None:
            return ""
        try:
            return _FORMATTER.vformat(self.url, (), profile)
        except (KeyError, IndexError, AttributeError, TypeError, ValueError):
            return ""

    def __repr__(self) -> str:
        return f"Category({self.name!r}, own={self.own}, newest_first={self.newest_first})"


class Route:
    """A method that reads one kind of target, made by `@route(...)`; its name is the kind.

    Args:
        function: the method, taking `(self, ctx, value)`.
        patterns: link and code patterns, each with one placeholder named like the method's
            value parameter, or `(pattern, value template)` pairs.
        newest_first: its own listing (with no categories) comes newest first.
        single: it reads one post; it is never followed.
        discovers: its listing yields targets, run as their own; a query route always does.
        folder: its files' folder template, instead of the plugin's.
        shown: the form help shows instead of the first pattern's, for custom text.

    Raises:
        PluginError: a malformed pattern, a placeholder named unlike the value parameter, the
            wrong parameters, or `single` with `discovers`.
    """

    def __init__(self, function: Callable[..., Any], patterns: tuple[str | tuple[str, str], ...],
                 *, newest_first: bool = True, single: bool = False, discovers: bool = False,
                 folder: str | None = None, shown: str | None = None):
        self.function = function
        self.kind = function.__name__
        self.patterns = patterns
        self.newest_first, self.single, self.folder, self.shown = (
            newest_first, single, folder, shown)
        if shown is not None and (not isinstance(shown, str) or not shown.strip()):
            raise PluginError(f"route {self.kind}: shown is the form help shows, such as "
                              f"'exm:<title>', not {shown!r}")
        self._patterns = [_Pattern(pattern) for pattern in patterns]
        self.is_query = any(pattern.is_query for pattern in self._patterns)
        self.discovers = self.is_query or discovers
        self._categories: list[Category] = []
        parameters = list(inspect.signature(function).parameters)
        if len(parameters) != 3:
            raise PluginError(f"route {self.kind}: take (self, ctx, value), not "
                              f"({', '.join(parameters)})")
        if single and self.discovers:
            raise PluginError(f"route {self.kind}: a single route reads one post, so it "
                              "discovers no targets; drop single or discovers")
        for pattern in self._patterns:
            if pattern.template is not None:
                continue
            if len(pattern.names) != 1:
                raise PluginError(f"route {self.kind}: pattern {pattern.text!r} needs exactly "
                                  f"one placeholder, not {len(pattern.names)}; give a "
                                  "(pattern, value) pair to combine several")
            if pattern.names[0] != parameters[2]:
                raise PluginError(f"route {self.kind}: pattern {pattern.text!r} captures "
                                  f"{pattern.names[0]!r}, but the method's value parameter is "
                                  f"{parameters[2]!r}")

    @property
    def categories(self) -> tuple[Category, ...]:
        """Its categories, in declaration order; empty if it yields posts itself."""
        return tuple(self._categories)

    def category(self, function: Callable[..., Any] | None = None, /, *, own: bool = True,
                 url: str | None = None, newest_first: bool | None = None,
                 folder: str | None = None, key: str | None = None) -> Any:
        """Mark a method as one of this route's categories, with or without options.

        Args:
            function: the method, when used without options.
            own: the account's own content (the default selection), or False for others'.
            url: a template over `profile` for the page opened ahead in a warm tab.
            newest_first: its listing comes newest first; None takes the route's.
            folder: its files' folder template.
            key: its files' history key template.

        Returns:
            The method itself, so the class keeps it as a plain method.

        Raises:
            PluginError: a category of the same name is declared twice on the route.
        """
        def register(method: Callable[..., Any]) -> Callable[..., Any]:
            made = Category(method, own=own, url=url, folder=folder, key=key,
                            newest_first=self.newest_first if newest_first is None
                            else newest_first)
            if any(known.name == made.name for known in self._categories):
                raise PluginError(f"route {self.kind}: category {made.name!r} is declared twice")
            self._categories.append(made)
            return method

        return register(function) if function is not None else register

    def __set_name__(self, owner: type, name: str) -> None:
        """Take the attribute's name as the kind."""
        self.kind = name

    def __get__(self, instance: object, owner: type | None = None) -> Any:
        """The route itself on the class; the plain method, bound, on an instance."""
        return self if instance is None else MethodType(self.function, instance)

    def forms(self, code: str) -> list[str]:
        """Each pattern as people write it, for plugin `code`; `shown` replaces the first."""
        forms = [pattern.form(code) for pattern in self._patterns]
        if self.shown is not None:
            forms[:1] = [self.shown]
        return forms


def route(*patterns: str | tuple[str, str], newest_first: bool = True, single: bool = False,
          discovers: bool = False, folder: str | None = None,
          shown: str | None = None) -> Callable[[Callable[..., Any]], Route]:
    """Mark a method as a route: the targets it reads, as patterns; its name is their kind.

    Args:
        *patterns: link patterns (`example.com/{user}`), code patterns (`exm:{user}`,
            `exm?{words}`), or `(pattern, value template)` pairs. A placeholder without a regex
            matches a user name, a leading `@` dropped; `*` matches any one path segment.
        newest_first: its own listing comes newest first, so a sync may stop at old posts.
        single: it reads one post (a post or a video link), which is never followed.
        discovers: its listing yields `Target`s, run as their own targets.
        folder: its files' folder template, instead of the plugin's.
        shown: the form help shows instead of the first pattern's.

    Returns:
        A decorator making the `Route`.
    """
    return lambda function: Route(function, patterns, newest_first=newest_first, single=single,
                                  discovers=discovers, folder=folder, shown=shown)


# ---------------------------------------------------------------------------------------------
# Logging in
# ---------------------------------------------------------------------------------------------

class Login:
    """How a plugin logs in, assigned as `login = MyLogin()`; a cookie site uses `cookies(...)`.

    A subclass sets `methods` and implements `start`; `check` is optional.

    Args:
        required: whether the plugin can list nothing without a login.
    """

    # The ways `flnd auth login <code> <method>` accepts.
    methods: ClassVar[tuple[str, ...]] = ()
    # The cookies, any one of which proves a login; empty for a login that keeps none.
    names: tuple[str, ...] = ()
    # A regular expression a login cookie's whole name may match instead; None for no pattern.
    pattern: re.Pattern[str] | None = None

    def __init__(self, *, required: bool = True) -> None:
        self.required = required

    async def start(self, ctx: Ctx, method: str) -> None:
        """Log in by `method`, asking through `ctx.ask`, and keep what it got in `ctx.secrets`.

        Raises:
            LoginError: the site refused, or the person gave up.
        """
        raise NotImplementedError(f"{type(self).__name__}.start")

    def proves(self, name: str) -> bool:
        """Whether a cookie of this name proves a login: one of `names`, or a full `pattern` match.

        The one place that decides; the engine asks here for expiry, refusals and status.
        """
        return name in self.names or (
            self.pattern is not None and self.pattern.fullmatch(name) is not None)

    @property
    def label(self) -> str:
        """The login cookies as people read them: `sid, token` or `a name matching .+_user`."""
        parts = list(self.names)
        if self.pattern is not None:
            parts.append(f"a name matching {self.pattern.pattern}")
        return ", ".join(parts)

    async def check(self, ctx: Ctx) -> bool:
        """Whether the plugin is logged in; by default, whether a cookie proves it."""
        return any(self.proves(name) for name in ctx.cookies)


class Cookies(Login):
    """The login of a cookie site, made by `cookies(...)`: logged in when one of `names` is set
    or a cookie's whole name matches `match`.

    Args:
        *names: the cookies, any one of which proves a login.
        match: a regular expression, fully matched against cookie names.
        required: whether the plugin can list nothing without it.

    Raises:
        PluginError: neither names nor a match, or a match that isn't a regular expression.
    """

    methods: ClassVar[tuple[str, ...]] = COOKIE_METHODS

    def __init__(self, *names: str, match: str | None = None, required: bool = False) -> None:
        super().__init__(required=required)
        if (not names and match is None) or not all(
                isinstance(name, str) and name for name in names):
            raise PluginError('cookies() takes the cookie names that prove a login: '
                              'cookies("sessionid")')
        self.names = tuple(names)
        if match is not None:
            try:
                self.pattern = re.compile(match)
            except (re.error, TypeError) as error:
                raise PluginError(f"cookies(match={match!r}) isn't a regular expression: "
                                  f"{error}") from None

    async def start(self, ctx: Ctx, method: str) -> None:
        """Never called: the engine reads a cookie login itself.

        Raises:
            UsageError: always.
        """
        raise UsageError(f"{method}: cookies are read from a cookies.txt file")


def cookies(*names: str, match: str | None = None, required: bool = False) -> Cookies:
    """The login of a cookie site: logged in when one of these cookies is set.

    Args:
        *names: the cookies, any one of which proves a login.
        match: a regular expression; a cookie whose whole name matches it proves a login too.
        required: whether the plugin can list nothing without them.
    """
    return Cookies(*names, match=match, required=required)


# ---------------------------------------------------------------------------------------------
# What a plugin declares beside its routes
# ---------------------------------------------------------------------------------------------

class Browser(enum.Enum):
    """Which browser a plugin's tabs need."""

    # HTTP only: no tab.
    NONE = "none"
    # Google Chrome, else Playwright's Chromium.
    ANY = "any"


@dataclass(frozen=True, kw_only=True)
class Settings:
    """A plugin's own config keys, `[plugins.<code>]`: subclass it, each field with a default.

    A subclass is made a frozen, keyword-only dataclass. Its docstring has an `Attributes:`
    line for each field, which `flnd config` shows.

    Raises:
        PluginError: a subclass's field can't be a dataclass field (a mutable default).
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        try:
            dataclass(frozen=True, kw_only=True)(cls)
        except (TypeError, ValueError) as error:
            raise PluginError(f"{cls.__qualname__}: {error}") from None


# ---------------------------------------------------------------------------------------------
# The context: the only way a plugin acts
# ---------------------------------------------------------------------------------------------

class Ctx(Protocol):
    """What a plugin's routes, categories and hooks act through, for one target.

    The engine's runs every call as a scheduler job (a page request, a tab, a call in the
    thread pool), paced, retried and held for it; the test kit's `FakeCtx` fakes it.
    """

    async def fetch(self, request: str | Request, *, bucket: str | None = None,
                    in_page: bool = False) -> Response:
        """Make one request and return its answer: a page job on the plugin's domains.

        Args:
            request: a URL, or a `Request`.
            bucket: the pace bucket it counts against; None is the host's.
            in_page: send it from the account's logged-in tab, as the page's own script would.

        Raises:
            NotFound: a 404 or 410.
            LoginError: a 401 or 403 from the plugin's domains.
            Blocked: still refused after the holds.
            SiteError: another refusal, when `request.check`.
            NetworkError: no answer, after the tries.
        """
        ...

    def pages(self, request: str | Request, *,
              cursor: str | Callable[[Response], str | None],
              send: str | Callable[[str], Mapping[str, Any] | Request] = "cursor",
              bucket: str | None = None, in_page: bool = False,
              resume: bool = False) -> AsyncIterator[Response]:
        """Each page of a paged listing, one page job each; the engine keeps and resumes it.

        Args:
            request: the first page's URL or `Request`.
            cursor: where an answer says what comes next: a `dig` path into its JSON, or a
                function returning the next cursor, None at the end.
            send: where the cursor goes in the next request: a query parameter's name, or a
                function from the cursor to more params, or to the whole next `Request` (a
                cursor in a POST body).
            bucket: the pace bucket it counts against; None is the host's.
            in_page: fetch each page from the account's logged-in tab.
            resume: when the engine's block rounds run out on a page, hold through `hold`
                (with `reset`) and ask the same cursor again, instead of ending with `Blocked`,
                if a page came since the last hold; with none, `Blocked` ends it.
        """
        ...

    async def open(self, url: str) -> Landed:
        """Load `url` in the target's tab, a tab job, and say where it landed.

        Raises:
            NotFound: the page answered 404.
            LoginError: it landed on one of the plugin's `walls`, after the wall's retries.
        """
        ...

    def feed(self, match: str | re.Pattern[str] | Callable[[str], bool], *, scroll: bool = True,
             lines: bool = False,
             cursor: Callable[[Any], tuple[bool, str | None] | None] | None = None,
             resume: bool = False, bucket: str | None = None) -> AsyncIterator[Any]:
        """The JSON of the tab's responses whose URL matches, as they arrive; a tab job.

        It listens from the call, so use it with `async with` before `click` or `open`, or
        iterate it with `async for`.

        Args:
            match: a substring of the URL, a compiled regex searched in it, or a predicate.
            scroll: scroll the tab for more when none is waiting, and end when the feed ends.
            lines: each body holds one JSON value per line.
            cursor: reads each value: `(more, cursor)` for a page of the feed, None for a value
                that says nothing about paging.
            resume: a 429 holds through `hold` (the site's wait, else the engine's; `reset`
                once a new body came since the last hold), then the tab is opened again to
                the page last opened and scrolled on, and a body read before is not given
                again; past the engine's last round it still raises `Blocked`.
            bucket: the bucket those holds count against; None holds the plugin.
        """
        ...

    def load(self, url: str, match: str | re.Pattern[str] | Callable[[str], bool], *,
             scroll: bool = True, lines: bool = False,
             cursor: Callable[[Any], tuple[bool, str | None] | None] | None = None
             ) -> AsyncIterator[Any]:
        """Open `url` with a `feed` listening, and yield `meta(document)`, then the feed's items.

        Its options are `feed`'s.
        """
        ...

    async def document(self) -> Document:
        """The tab's page now, parsed; a tab job."""
        ...

    async def warm(self, urls: Sequence[str]) -> None:
        """Start loading pages the listing opens next in warm tabs, while the run has room.

        For pages no category's `url=` names, such as an album's pages or a profile's story
        buckets; `open` of one then takes its tab. An empty URL opens nothing.
        """
        ...

    async def click(self, css: str, *,
                    timeout: float = 10.0) -> None:  # noqa: ASYNC109 (the tab job's own limit)
        """Click the first element `css` matches, once it is clickable; a tab job.

        Raises:
            NetworkError: nothing clickable matched within `timeout` seconds.
        """
        ...

    async def shown(self, css: str) -> bool:
        """Whether an element `css` matches is shown in the tab now: it exists, is displayed
        and visible, and has a size above zero."""
        ...

    async def handoff(self, message: str, *, until: Callable[[], Awaitable[bool]],
                      timeout: float | None = None) -> bool:  # noqa: ASYNC109 (a person's)
        """Show `message` and wait for a person to do a step in a visible window (a captcha).

        Args:
            message: what the person should do.
            until: True once the step is done.
            timeout: seconds before giving up; None waits as long as it takes.

        Returns:
            True when `until` came true; False when `timeout` passed first.
        """
        ...

    async def pause(self, seconds: float, reason: str) -> None:
        """Wait `seconds` on the run's clock, as a page that needs time to settle does."""
        ...

    async def hold(self, reason: str, *, seconds: float | None = None,
                   bucket: str | None = None, reset: bool = False) -> None:
        """Hold the plugin's requests for a block it read itself (a feed's, a page's, an SDK's).

        The engine holds the bucket by its block rules.

        Args:
            reason: what the site did, shown with the hold.
            seconds: how long the site asked for; None holds by the block rules.
            bucket: the bucket it holds; None is the host's.
            reset: forget earlier block rounds first, when progress was made since the last hold.
        """
        ...

    async def call(self, fn: Callable[..., T], *args: Any) -> T:
        """Run a blocking function in the thread pool, such as an SDK's, and return its value."""
        ...

    async def ask(self, message: str, *, secret: bool = False, description: str = "",
                  choices: Sequence[str] = ()) -> str:
        """Ask the person a question and return the answer.

        Args:
            message: the question.
            secret: hide what is typed, as for a password.
            description: more lines under the question.
            choices: the answers to choose from; empty for free text.

        Raises:
            UsageError: there is nobody to ask (no terminal).
        """
        ...

    def log(self, level: str, message: str, **fields: Any) -> None:
        """Write a line about the target: `level` is "debug", "info" or "warn"."""
        ...

    @property
    def target(self) -> Target:
        """The target being run."""
        ...

    @property
    def when(self) -> Range:
        """The run's `when`, for a plugin that can ask its site for a range."""
        ...

    @property
    def head(self) -> Mapping[str, Any] | None:
        """The head the crawl kept for the category being listed, as a read-only `{"id", "ts"}`.

        Only during a sync of a `newest_first` category, so a plugin may ask its site for
        less: posts at or older than the head are old. None on a first read, with `--again`,
        for a category that isn't `newest_first`, on a single route, and outside a listing.
        The engine's own stop rule still decides where the listing ends.
        """
        ...

    @property
    def time(self) -> Clock:
        """The run's clock."""
        ...

    @property
    def settings(self) -> Settings:
        """The plugin's `Settings`, read from the config file."""
        ...

    @property
    def cookies(self) -> Mapping[str, str]:
        """The login's cookies on the plugin's domains, read-only."""
        ...

    @property
    def state(self) -> MutableMapping[str, Any]:
        """The plugin's own table in `flnd.db`: JSON values, each set in one statement."""
        ...

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """The login's saved secrets (`logins/<code>.json`); writable only during a login."""
        ...

    @property
    def request(self) -> Request | None:
        """The browser request of the last item `feed` or `load` yielded; None for none."""
        ...


# ---------------------------------------------------------------------------------------------
# The plugin
# ---------------------------------------------------------------------------------------------

class Plugin:
    """One site flnd downloads from: what it declares, its routes, categories and hooks.

    Only `code`, `name` and one route are required. Its `routes`, `forms` and `categories` are
    derived from its `@route` methods when the class is made.
    """

    # What its targets start with: 2 to 16 lowercase letters and digits.
    code: ClassVar[str]
    # Its name as people know it, shown in lists and help.
    name: ClassVar[str]
    # Its own domains: paced, signed and given its cookies; a link on one is its.
    domains: ClassVar[tuple[str, ...]] = ()
    # How it logs in; None for no login.
    login: ClassVar[Login | None] = None
    # Seconds between two page requests of one bucket.
    pace: ClassVar[float] = 0.5
    # Host -> the most files at once to it.
    limits: ClassVar[Mapping[str, int]] = MappingProxyType({})
    # Which browser its tabs need.
    browser: ClassVar[Browser] = Browser.NONE
    # Hide automation in its tabs.
    stealth: ClassVar[bool] = True
    # Load images, media and fonts in its tabs; off, flnd aborts those requests. Set it for a
    # site only after a live check shows its feeds still load without them.
    assets: ClassVar[bool] = False
    # Its tabs may be in a hidden window. The browser is hidden only while every plugin using it
    # is; a handoff (a captcha) shows it. Set it only after a live check shows the site works.
    hidden: ClassVar[bool] = False
    # The User-Agent of its HTTP requests; None keeps the client's.
    user_agent: ClassVar[str | None] = None
    # URL paths meaning "log in" or a stopped login, matched by whole path segments.
    walls: ClassVar[tuple[str, ...]] = ()
    # The template of a target's own folder under `media/<code>/`, filled per file with the same
    # fields as `folder`. A single post's `{target}` is its owner, else its id.
    root: ClassVar[str] = "{target}"
    # The templates of a file's folder under the root ("" is the root itself), its name,
    # and its history key.
    folder: ClassVar[str] = "{category}"
    filename: ClassVar[str] = "{post}_{num}"
    key: ClassVar[str] = "{post}_{num}"
    # The post fields templates may use, besides the engine's.
    fields: ClassVar[tuple[str, ...]] = ()
    # Link or code patterns it refuses, each with the reason.
    unsupported: ClassVar[Mapping[str, str]] = MappingProxyType({})
    # Its own config keys.
    Settings: ClassVar[type[Settings]] = Settings

    # Derived when the class is made: its routes in order, the forms help lists, and the names
    # of its categories.
    routes: ClassVar[tuple[Route, ...]] = ()
    forms: ClassVar[tuple[str, ...]] = ()
    categories: ClassVar[tuple[str, ...]] = ()
    _refusals: ClassVar[tuple[tuple[_Pattern, str], ...]] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Derive the class's routes, forms and categories.

        A class with no code and no route of its own is a base for other plugins, left alone
        until a subclass is made.

        Raises:
            PluginError: the class declares one of the derived fields itself.
        """
        super().__init_subclass__(**kwargs)
        for derived in ("routes", "forms", "categories"):
            if derived in vars(cls):
                raise PluginError(f"{cls.__name__}.{derived}: made from the @route methods and "
                                  "their categories; declare no such field")
        routes = {known.kind: known for known in cls.routes}
        routes.update((value.kind, value) for value in vars(cls).values()
                      if isinstance(value, Route))
        cls.routes = tuple(routes.values())
        if not getattr(cls, "code", None) and not cls.routes:
            return
        cls._refusals = tuple((_Pattern(pattern), reason)
                              for pattern, reason in cls.unsupported.items())
        names = [category.name for known in cls.routes for category in known.categories]
        cls.categories = tuple(dict.fromkeys(names))
        forms = [form for known in cls.routes for form in known.forms(cls.code)]
        # `ins:<user>` is the form people write most, so the `code:` and `code?` forms lead.
        cls.forms = tuple(sorted(
            forms, key=lambda form: not form.startswith((f"{cls.code}:", f"{cls.code}?"))))

    @classmethod
    def resolve(cls, text: str) -> Target | None:
        """Read a target's text through the routes' patterns and the `unsupported` rules.

        Routes are tried in declaration order, so a narrow route comes before a catch-all one.
        An override (for text no pattern can say) passes the rest on to `super().resolve`.

        Args:
            text: the target, with or without its `code:`, or a link on the plugin's domains.

        Returns:
            The target the first matching route reads, its `kind` set; None when none does.

        Raises:
            UsageError: an `unsupported` rule matched and no more specific route did, or a
                query is empty.
            TypeError: it was called on `Plugin` or another base, not on a plugin class.
        """
        if not isinstance(getattr(cls, "code", None), str):
            raise TypeError(f"{cls.__name__}.resolve: call it on a plugin class, such as "
                            "Example.resolve(text)")
        written = text.strip()
        # The code is matched in any case, as people type `EXM:example_user` too.
        head = written[:len(cls.code) + 1].lower()
        if head == f"{cls.code}:":
            written = written[len(cls.code) + 1:]
        elif head == f"{cls.code}?":
            written = written[len(cls.code):]
        link = _parse(written, cls.domains)
        # The most specific pattern wins: a refusal of a whole path (`x.com/i`) doesn't catch a
        # longer readable link under it, and a catch-all `{user}` never reopens a refusal.
        refused = next(((pattern, reason) for pattern, reason in cls._refusals
                        if pattern.match(written, link) is not None), None)
        for known in cls.routes:
            for pattern in known._patterns:
                captures = pattern.match(written, link)
                if captures is None:
                    continue
                if refused is not None and pattern.literals <= refused[0].literals:
                    continue
                value = pattern.value(captures)
                if not known.is_query:
                    return Target(f"{cls.code}:{value}", kind=known.kind)
                if not value.strip():
                    raise UsageError(f"{cls.code}: the query is empty")
                return Target(f"{cls.code}?{value.strip()}", kind=known.kind)
        if refused is not None:
            raise UsageError(f"{cls.code}: {refused[1]}")
        return None

    def sign(self, request: Request, cookies: Mapping[str, str]) -> Request:
        """The request with headers added, before each try of a request to the plugin's domains.

        Args:
            request: the request.
            cookies: the login's cookies on the plugin's domains.
        """
        return request

    def connect(self, ctx: Ctx) -> AbstractAsyncContextManager[None]:
        """An async context manager entered once per run around the plugin's targets."""
        return nullcontext()

    async def date(self, ctx: Ctx, post: Post) -> datetime.date | None:
        """The date of a post with none, before its files are named; None when it can't tell."""
        return None

    def blocked(self, response: Response) -> float | None:
        """Seconds to hold for an HTTP answer from the plugin's domains; None when it's no block.

        It sees `fetch`'s and `pages`' answers and `open`'s main document, never a feed item.
        """
        return None

    async def refresh(self, ctx: Ctx, post: Post) -> Post:
        """The post with fresh files, when a file's link expired or ran too slow.

        The default lists a lazy post's files again.

        Raises:
            NotFound: the post isn't lazy, so its files can't be listed again.
        """
        if not callable(post.files):
            raise NotFound(f"{self.code}: post {post.id}'s links expired, and its files can't "
                           "be listed again")
        return post.replace(files=tuple(await post.files()))


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------

def meta(document: Document) -> list[Any]:
    """The JSON in a page's `<script type="application/json">` blocks, Meta's `__bbox` unwrapped.

    Returns:
        Per block in page order: each `__bbox.result` it holds, or the block itself when it
        holds none. A block that isn't JSON is left out.
    """
    found: list[Any] = []
    for block in document.blocks():
        boxed = list(_boxed(block))
        found += boxed or [block]
    return found


def _boxed(node: Any) -> Iterator[Any]:
    """Each `__bbox.result` under `node`, depth first; a box's own result isn't searched."""
    if isinstance(node, dict):
        box = node.get("__bbox")
        if isinstance(box, dict) and "result" in box:
            yield box["result"]
            return
        for value in node.values():
            yield from _boxed(value)
    elif isinstance(node, list):
        for value in node:
            yield from _boxed(value)


def page_info(path: str) -> Callable[[Any], tuple[bool, str | None] | None]:
    """A `cursor` reader for a GraphQL `{has_next_page, end_cursor}` object at a `dig` path.

    Returns:
        A function giving `(more, cursor)` for a value with the object; None for one without.
    """
    def read(value: Any) -> tuple[bool, str | None] | None:
        info = dig(value, path)
        if not isinstance(info, Mapping) or "has_next_page" not in info:
            return None
        return bool(info["has_next_page"]), info.get("end_cursor") or None

    return read


def best(streams: Sequence[Stream], *, height: int | None = None) -> Stream | None:
    """The stream with the most pixels, then the most bits, at or under `height`.

    When any stream lacks a width, pixels can't be counted, so heights are compared instead.

    Args:
        streams: the qualities to choose from.
        height: the tallest wanted, in pixels; None means no cap.

    Returns:
        The best stream; None when none is at or under `height`.
    """
    fit = [stream for stream in streams if height is None or stream.height <= height]
    if not fit:
        return None
    if all(stream.width for stream in fit):
        return max(fit, key=lambda stream: (stream.width * stream.height, stream.bitrate))
    return max(fit, key=lambda stream: (stream.height, stream.bitrate))


_UNWANTED = re.compile(r"https?://\S+|[#@]\w+")


def headline(text: str | None) -> str:
    """A caption's title: its first non-empty line, without `#tags`, `@names` and http(s) URLs,
    whitespace squeezed to single spaces and trimmed; "" for None or nothing left."""
    for line in (text or "").splitlines():
        found = " ".join(_UNWANTED.sub(" ", line).split())
        if found:
            return found
    return ""


def numbered(n: int, *, suffix: str = "", width: int = 2) -> str:
    """`n` zero-padded to `width` digits, then `suffix`: `"07"`, or `"07_cover"`.

    Args:
        n: the number.
        suffix: text after it, as given, such as "_cover" or ".jpg".
        width: the fewest digits.
    """
    return f"{n:0{width}d}{suffix}"


def _manifest_streams(manifest: Any) -> tuple[list[Stream], list[Stream]] | None:
    """An inline DASH manifest's video and audio streams with https URLs; see `File.dash`."""
    import xml.etree.ElementTree as ElementTree

    if not isinstance(manifest, str) or not manifest:
        return None
    try:
        root = ElementTree.fromstring(manifest)
    except ElementTree.ParseError:
        return None
    if any(_tag(node) in ("SegmentTemplate", "SegmentList", "SegmentTimeline")
           for node in root.iter()):
        return None
    found: dict[str, list[Stream]] = {"video": [], "audio": []}
    for adaptation in (node for node in root.iter() if _tag(node) == "AdaptationSet"):
        for representation in (node for node in adaptation.iter()
                               if _tag(node) == "Representation"):
            url = _base_url(representation) or _base_url(adaptation)
            kind = _stream_kind(representation, adaptation)
            if kind in found and url.startswith("https://") and not url.endswith(
                    ("/", ".mpd", ".m4s")) and "$" not in url:
                found[kind].append(Stream(
                    url, height=_number(representation.get("height")),
                    width=_number(representation.get("width")),
                    bitrate=_number(representation.get("bandwidth"))))
    return found["video"], found["audio"]


def _tag(node: Any) -> str:
    """An XML element's tag without its namespace."""
    return node.tag.split("}")[-1]


def _base_url(node: Any) -> str:
    """The text of a manifest element's own `BaseURL` child; "" without one."""
    for child in node:
        if _tag(child) == "BaseURL" and child.text:
            return child.text.strip()
    return ""


def _stream_kind(representation: Any, adaptation: Any) -> str:
    """"video", "audio", or another kind, from a stream's MIME type or else its codecs."""
    mime = (representation.get("mimeType") or adaptation.get("mimeType")
            or representation.get("contentType") or adaptation.get("contentType") or "")
    kind = mime.split("/")[0].lower()
    if kind in ("video", "audio"):
        return kind
    codecs = (representation.get("codecs") or adaptation.get("codecs") or "").lower()
    if codecs.startswith(("avc", "mp4v", "hev", "hvc", "vp", "av01")):
        return "video"
    if codecs.startswith(("mp4a", "opus", "vorbis", "aac")):
        return "audio"
    return kind


def _number(value: str | None) -> int:
    """A manifest's number attribute; 0 when it's missing or not a number."""
    return int(value) if value and value.isdigit() else 0


def wall(url: str, walls: Sequence[str]) -> str | None:
    """The wall `url` lands on, matched by whole path segments; None for none.

    The engine and `kit.FakeCtx` both ask it, for where `open` landed and where a
    `fetch` was redirected.

    A path `p` lands on the wall `w` when `p == w` or `p` starts with `w + "/"`, a trailing
    slash on either ignored: `/challengers/` is not the wall `/challenge`.
    """
    path = urlsplit(url).path if "://" in url else url.split("?", 1)[0].split("#", 1)[0]
    path = path.rstrip("/") or "/"
    for wall in walls:
        bare = wall.rstrip("/") or "/"
        if path == bare or path.startswith(bare + "/"):
            return wall
    return None


def _parse(written: str, domains: tuple[str, ...]) -> _Link | None:
    """`written` as a `_Link` when it is a URL on one of `domains`, with or without a scheme."""
    # Without a scheme, a slash tells a link (example.com/example_user) from a name.
    if "/" not in written and not _is_url(written):
        return None
    parsed = urlsplit(written if _is_url(written) else f"https://{written}")
    host = (parsed.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in domains):
        return None
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    parts = tuple(unquote(part) for part in parsed.path.split("/") if part)
    return _Link(_bare_host(host), parts, query)


def _bare_host(host: str) -> str:
    """A host without the prefixes that are the same site: `www.m.example.com` is `example.com`."""
    while True:
        before = host
        for prefix in SITE_PREFIXES:
            host = host.removeprefix(prefix)
        if host == before:
            return host


def _is_url(written: str) -> bool:
    return written.lower().startswith(("http://", "https://"))


def _split(pattern: str, separator: str, *, once: bool = False) -> list[str]:
    """`pattern` split at `separator` outside `{…}`; a backslash escapes the next character."""
    parts, depth, start, escaped = [], 0, 0, False
    for index, character in enumerate(pattern):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        depth += {"{": 1, "}": -1}.get(character, 0)
        if character == separator and depth == 0:
            if once:
                return [pattern[:index], separator, pattern[index + 1:]]
            parts.append(pattern[start:index])
            start = index + 1
    return [pattern, "", ""] if once else [*parts, pattern[start:]]


def _tokens(segment: str, pattern: str) -> list[tuple[str, str, str]]:
    """A segment's pieces: `("text", literal, "")`, `("any", "*", "")` or `("hole", name, regex)`.

    Raises:
        PluginError: an unclosed `{`, a stray `}`, or a placeholder name that isn't one.
    """
    tokens, index = [], 0
    while index < len(segment):
        character = segment[index]
        if character == "\\" and index + 1 < len(segment):
            tokens.append(("text", segment[index:index + 2], ""))
            index += 2
            continue
        if character == "}":
            raise PluginError(f"pattern {pattern!r}: a '}}' with no '{{'")
        if character in "*{":
            end = index if character == "*" else _closing(segment, index, pattern)
            if character == "*":
                tokens.append(("any", "*", ""))
            else:
                name, _, regex = segment[index + 1:end].partition(":")
                if not name.isidentifier():
                    raise PluginError(f"pattern {pattern!r}: {{{name}}} isn't a placeholder "
                                      "name")
                tokens.append(("hole", name, regex))
            index = end + 1
            continue
        tokens.append(("text", character, ""))
        index += 1
    return tokens


def _closing(segment: str, start: int, pattern: str) -> int:
    """Where the `}` closing the `{` at `start` is, nested braces and escapes allowed."""
    depth, escaped = 0, False
    for end in range(start, len(segment)):
        if escaped:
            escaped = False
            continue
        if segment[end] == "\\":
            escaped = True
            continue
        depth += {"{": 1, "}": -1}.get(segment[end], 0)
        if depth == 0:
            return end
    raise PluginError(f"pattern {pattern!r}: a '{{' with no '}}'")


def _compile(segment: str, pattern: str, names: list[str]) -> re.Pattern[str]:
    """One segment as a regex for a whole URL part, each placeholder a group of its name."""
    regex = ""
    for kind, token, custom in _tokens(segment, pattern):
        if kind == "text":
            regex += re.escape(token)
        elif kind == "any":
            regex += "[^/]*"
        else:
            if token in names:
                raise PluginError(f"pattern {pattern!r}: {{{token}}} is named twice")
            names.append(token)
            regex += f"@?(?P<{token}>{NAME})" if not custom else f"(?P<{token}>{custom})"
    try:
        return re.compile(regex)
    except re.error as error:
        raise PluginError(f"pattern {pattern!r}: {error}") from None


def _shown(segment: str) -> str:
    """A segment as help shows it: `{name:regex}` as `<name>`, `*` as `…`."""
    return "".join({"text": token, "any": ANY}.get(kind, f"<{token}>")
                   for kind, token, _ in _tokens(segment, segment))


def _frozen(headers: Mapping[str, str] | None) -> Mapping[str, str] | None:
    """A read-only copy of `headers`; None for none."""
    return MappingProxyType(dict(headers)) if headers else None
