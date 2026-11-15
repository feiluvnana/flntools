"""Writing a source: declarations for site extractors and target routes.

A source is a `Source` subclass defining `code`, `domains`, and `@target` routes.
Routes return context for `@category` methods, which yield `Post` and `File` objects.
The engine handles pacing, authentication, rate limits, downloads, compression, and history.

See `tests/example_source.py` for a complete reference implementation.
"""

from __future__ import annotations

import datetime
import inspect
import re
import string
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from types import MappingProxyType, MethodType
from typing import (TYPE_CHECKING, Any, Callable, ClassVar, Iterable, Iterator, Mapping,
                    MutableMapping, Protocol, Sequence, Union, overload)
from urllib.parse import parse_qs, unquote, urlsplit

from shared import text, ui

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from shared import browser, http

__all__ = ["Category", "DateLike", "Error", "Fetch", "FetchError", "File", "Login", "LoginError",
           "Post", "Range", "Route", "Site", "Source", "Target", "TargetError", "day", "fill",
           "pages", "target"]

# Hosts that are the same site as their bare domain: desktop, mobile and web variants.
SITE_PREFIXES = ("www.", "m.", "mobile.", "web.", "mbasic.", "touch.")
# What `{name}` matches without a regex: a user name, as the sites spell them, with one leading `@`
# dropped since people paste names both ways.
NAME = r"[\w.]+"
# Where a generated form shows a `*`: any one segment, which help can't name.
ANY = "…"
# The format fields the engine sets for every file, which a post's own `**fields` may not use:
# `category` (the category's name), `num` (the file's place in its post, from 0), `count` (the
# post's files), `index` ("" for a one-file post, else "_<num>"), `extension` (".jpg", the one the
# file is named with before its content is checked; "" when the content decides) and `path` (the
# file's path under the target's folder, which is what a history key was when a source gave none).
ENGINE_FIELDS = frozenset({"category", "num", "count", "index", "extension", "path"})
# What a format drops beside a field that came out empty: `{date}_{id}` without a date is `<id>`.
SEPARATORS = "_- .:,"
# The oldest year a post can be from: the sites flnd reads all started later. A timestamp before
# it is a mistake for another number, such as an id read as seconds.
FIRST_YEAR = 2004
# Seconds since 1970 pass this in 5138; a larger number is milliseconds (13 digits today, 10 for
# seconds).
MILLISECONDS = 1e11
# What `Range.parse` says about any value it can't read: the usage error of `--time`.
TIME_USAGE = (
    "--time takes <since>,<until>, such as 2026-01-01,2026-06-30; use * or leave a side empty "
    "for no limit"
)
# Consecutive already-saved posts that end a `newest_first` listing early.
SAVED_STREAK = 5

# A file without a URL: `fetch(dest, progress)` writes to `dest`, reporting `progress(done, total)`
# in bytes. It may return a coroutine (tlg's SDK), which the engine runs on the source's loop.
Fetch = Callable[["Path", Callable[..., None]], Any]
# What `Post(date=)` takes; `day()` reads every one of them.
DateLike = Union[datetime.date, int, float, str, None]


class Error(Exception):
    """The base of the errors a source raises: `except flnd.source.Error` catches all three."""


class FetchError(Error):
    """A request that failed: `status` is the refusing HTTP status, or None (no response or JSON).

        try:
            data = site.fetch(url).json
        except FetchError as error:
            if error.status == 403: ...

    Args:
        message: what failed, shown to the person.
        status: the HTTP status that refused the request; None when there was no response.
        response: the refusing response, body and all, so a source can read the site's reason
            from it (Instagram's "please wait a few minutes" comes as a 400 or 401 with JSON);
            None when there was none.
    """

    def __init__(
        self, message: str, *, status: int | None = None, response: http.Response | None = None
    ):
        super().__init__(message)
        self.status = status
        self.response = response


class LoginError(Error):
    """A login failure with next steps: `LoginError("tlg: run flnd login tlg")`."""


class TargetError(Error, ValueError):
    """A target that can't be read, with what to write instead.

        raise TargetError("ins: story links aren't supported; give the profile")

    Args:
        message: the whole message, starting with the source's code when one is known.
        url: a link on the source's site that no route matched. The engine follows it once with a
            HEAD request and reads where it leads; None (the default) means the refusal is final.
    """

    def __init__(self, message: str, *, url: str | None = None):
        super().__init__(message)
        self.url = url


@dataclass(frozen=True)
class Target:
    """One thing to download: `Target("ins", "user", "example_user")`.

        target = Target("tik", "video", "7301234567890123456")
        str(target)  # "tik:7301234567890123456"
    """

    # The source's code, such as "ins".
    source: str
    # What it points to: its route's name (`user`, `post`, `video` or `chat`).
    kind: str
    # Clean form, the captured placeholder: names target folder and starts history keys.
    value: str
    # Whether this target is a query (`code?query`) rather than `code:value`.
    query: bool = field(default=False, compare=False, repr=False)

    def __str__(self) -> str:
        # The account a login and `connect` see has no value, and reads as its source's code.
        if not self.value:
            return self.source
        separator = "?" if (self.query or self.kind == "query") else ":"
        return f"{self.source}{separator}{self.value}"


@dataclass(frozen=True)
class _Url:
    """A URL on a source's site: host without prefixes, path parts, and query."""

    host: str
    parts: tuple[str, ...]
    query: Mapping[str, str]


class _Pattern:
    """One compiled pattern: domain pattern matches `_Url`, code pattern text after `code:`."""

    def __init__(self, text: str):
        """Split `text` into parts and compile each placeholder regex."""
        self.text = text
        path, mark, query = _split(text, "?", once=True)
        segments = [segment for segment in _split(path, "/") if segment]
        first = segments[0] if segments else ""
        # First segment with dot and no placeholder or `*` is host; else code pattern.
        self.host = (
            _bare_host(first.lower())
            if "." in first and not set(first) & set("{}*")
            else None
        )
        self.is_query = self.host is None and not segments and mark == "?"
        if self.host is None and mark and not self.is_query:
            raise TypeError(f"pattern {text!r}: only a URL pattern can name a query")
        self.names: list[str] = []
        if self.is_query:
            self.segments = []
            self.keys = []
            self.regexes = []
            self.query = [("", _compile(query, text, self.names))]
            self.literals = 0
            return
        self.segments = segments[1:] if self.host is not None else ["/".join(segments)]
        self.keys = [_split(pair, "=", once=True) for pair in _split(query, "&")] if mark else []
        if any(not key or not value for key, _, value in self.keys):
            raise TypeError(f"pattern {text!r}: write a query as ?key={{name}}")
        # Placeholder names in order: route pattern has one, unsupported pattern none.
        self.regexes = [_compile(segment, text, self.names) for segment in self.segments]
        self.query = [(key, _compile(value, text, self.names)) for key, _, value in self.keys]
        # How specific the pattern is: literal segments, host being one. Code pattern has none.
        self.literals = (self.host is not None) + sum(
            1 for s in self.segments if self.host is not None and not set(s) & set("{}*")
        )

    def match(self, text: str, url: _Url | None) -> str | None:
        """Captured value when `text` or `url` matches; "" for no placeholder."""
        if self.is_query:
            return text[1:] if url is None and text.startswith("?") else None
        if self.host is None:
            if url is not None or text.startswith("?"):
                return None
            found = self.regexes[0].fullmatch(text)
            return None if found is None else found.groupdict().get("value", "")
        if url is None or url.host != self.host or len(url.parts) < len(self.regexes):
            return None
        # The pattern's segments match the URL's first parts; the parts after them are allowed.
        found = [regex.fullmatch(part) for regex, part in zip(self.regexes, url.parts)]
        found += [
            regex.fullmatch(url.query[key]) if key in url.query else None
            for key, regex in self.query
        ]
        if not all(found):
            return None
        return next(
            (match["value"] for match in found if match and "value" in match.groupdict()), ""
        )

    def form(self, code: str) -> str:
        """The pattern as people write it, each placeholder in angle brackets."""
        if self.is_query:
            return f"{code}?<{self.names[0]}>"
        path = "/".join(_shown(segment) for segment in self.segments)
        if self.host is None:
            return f"{code}:{path}"
        # Help shows address browser bar does: www. on bare domain, closing slash on folder.
        host = f"www.{self.host}" if self.host.count(".") == 1 else self.host
        query = "&".join(f"{key}={_shown(value)}" for key, _, value in self.keys)
        slash = "" if query or "." in path.rsplit("/", 1)[-1] else "/"
        return f"https://{host}/{path}{slash if path else ''}" + (f"?{query}" if query else "")




class Category:
    """One thing a route's target can download, made by `@<route>.category`."""

    def __init__(self, function: Callable[..., Any], *, newest_first: bool = False,
                 folder: str | None = None, key: str | None = None):
        self.function = function
        # The method's name: what `--from` and help write, and the attribute the engine calls.
        self.name = function.__name__
        self.newest_first = newest_first
        self.folder, self.key = folder, key
        parameters = list(inspect.signature(function).parameters)
        if len(parameters) != 3:
            raise TypeError(
                f"category {self.name}: take (self, site, context), not ({', '.join(parameters)})"
            )

    def __repr__(self) -> str:
        return f"Category({self.name!r}, newest_first={self.newest_first})"


class Route:
    """A method that reads one kind of target, made by `@target(...)`; its name is the kind.

        Instagram.routes[0].kind       # "post"
        Instagram.routes[0].patterns   # ("instagram.com/p/{code:[\\w-]+}", …)
        Instagram.routes[1].categories # (Category("posts", …), Category("reels", …), …)

    A route without categories yields the target's posts itself. A route with categories
    returns a context, which every category it has receives; `@route.category` adds one.
    """

    def __init__(
        self, function: Callable[..., Any], patterns: tuple[str, ...], *, shown: str | None,
        discovers: bool = False,
        from_: Iterable[str] | None = None, folder: str | None = None,
    ):
        """Compile `patterns` for `function`, raising `TypeError` for a mistake in them."""
        self.function = function
        self.kind = function.__name__
        self.patterns = patterns
        self.shown = shown
        # What `--from` may name here and the target's folder template; `targets.validate`
        # checks both before any work. `from_` defaults to the categories.
        self._from = None if from_ is None else frozenset(from_)
        self.folder = folder
        self._patterns = [_Pattern(pattern) for pattern in patterns]
        self.is_query = any(pattern.is_query for pattern in self._patterns)
        self.discovers = self.is_query or discovers
        self._categories: list[Category] = []
        parameters = [
            parameter.name for parameter in inspect.signature(function).parameters.values()
        ]
        if len(parameters) != 3:
            raise TypeError(
                f"route {self.kind}: take (self, site, value), not ({', '.join(parameters)})"
            )
        for pattern in self._patterns:
            if len(pattern.names) != 1:
                # One value is the target's identity: its folder and its history keys.
                raise TypeError(
                    f"route {self.kind}: pattern {pattern.text!r} needs exactly one placeholder, "
                    f"not {len(pattern.names)}"
                )
            if pattern.names[0] != parameters[2]:
                raise TypeError(
                    f"route {self.kind}: pattern {pattern.text!r} captures {pattern.names[0]!r}, "
                    f"but the method's value parameter is {parameters[2]!r}"
                )

    @property
    def categories(self) -> tuple[Category, ...]:
        """The route's categories, in declaration order; empty if it yields posts itself."""
        return tuple(self._categories)

    @property
    def from_(self) -> frozenset[str]:
        """The category names `--from` accepts for this route; empty for a single post."""
        if self._from is not None:
            return self._from
        return frozenset(category.name for category in self._categories)

    @overload
    def category(self, function: Callable[..., Any], /) -> Callable[..., Any]: ...
    @overload
    def category(self, /, *, newest_first: bool = False, folder: str | None = None,
                 key: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...
    def category(self, function: Callable[..., Any] | None = None, /, *, newest_first: bool = False,
                 folder: str | None = None, key: str | None = None) -> Any:
        """Mark a method as one of this route's categories, with or without options.

            @user.category
            def stories(self, site, account): ...

            @user.category(newest_first=True)
            def reels(self, site, account): ...
        """
        def register(method: Callable[..., Any]) -> Callable[..., Any]:
            made = Category(method, newest_first=newest_first, folder=folder, key=key)
            if any(known.name == made.name for known in self._categories):
                raise TypeError(f"route {self.kind}: category {made.name!r} is declared twice")
            self._categories.append(made)
            return method

        return register(function) if function is not None else register

    def __set_name__(self, owner: type, name: str):
        """Take attribute name as kind, which is method name unless class was built by `type()`."""
        self.kind = name

    def __get__(self, instance: object, owner: type | None = None) -> Any:
        """The route itself on the class; the plain method, bound, on an instance."""
        return self if instance is None else MethodType(self.function, instance)

    def forms(self, code: str) -> list[str]:
        """Each pattern as people write it, for source `code`; `shown` replaces the first.

            Instagram.routes[1].forms("ins")   # ["ins:<user>", "https://www.instagram.com/<user>/"]
        """
        forms = [pattern.form(code) for pattern in self._patterns]
        if self.shown is not None:
            forms[:1] = [self.shown]
        return forms


def target(*patterns: str, shown: str | None = None,
           discovers: bool = False, from_: Iterable[str] | None = None,
           folder: str | None = None) -> Callable[[Callable[..., Any]], Route]:
    """Mark a method as a route: the targets it reads, as patterns, named by the method's kind."""
    return lambda function: Route(
        function, patterns, shown=shown, discovers=discovers,
        from_=from_, folder=folder,
    )


@dataclass(frozen=True, init=False)
class File:
    """One file of a post: a URL (or mirrors, or a fetch function), and download settings.

        File("https://cdn.example.com/a.jpg")
        File([mirror_a, mirror_b], name="loi-5930.7z")    # kept as the site names it
        File(video_url, audio=audio_url)                            # DASH: merged into one video
        File.text("info.md", "# Title\\n")                          # written, not downloaded

    The engine names it from the source's formats, gives it the extension its content calls for,
    and numbers it by its place in the post. Frozen.

    Args:
        url: a URL, mirror URLs tried in order, or a `Fetch` function `fetch(dest, progress)`
            for media without a URL (tlg's).
        audio: the URL of a separate audio stream (DASH), merged into the video; None when the
            video has its own sound. A file with `audio` is a video.
        name: the file's name as the site already gives it, extension included, which the
            formats must not change (loi's archives, tlg's documents). None names it by the
            source's `filename` format.
        size: its size in bytes, when the site says it; None when unknown.

    Raises:
        TypeError: `url` is none of those.
    """

    # A URL, mirrors tried in order, or a `Fetch` function; None for a text file.
    url: str | tuple[str, ...] | Fetch | None
    # A DASH video's separate audio stream, or None.
    audio: str | None
    # The name as the site gives it, or None for the formats' name.
    name: str | None
    # The size in bytes, or None.
    size: int | None
    # A text file's text, written as UTF-8; None for a file that is downloaded.
    content: str | None

    def __init__(self, url: str | Sequence[str] | Fetch, *, audio: str | None = None,
                 name: str | None = None, size: int | None = None):
        if isinstance(url, str) or callable(url):
            sources: Any = url
        elif isinstance(url, Sequence) and url and all(isinstance(mirror, str) for mirror in url):
            sources = tuple(url)
        else:
            raise TypeError(f"File takes a URL, mirror URLs or a fetch function, not {url!r}")
        _assign(self, url=sources, audio=audio, name=name, size=size, content=None)

    @classmethod
    def text(cls, name: str, text: str) -> File:
        """A text file written by the source itself (e.g. `info.md`): saved atomically.

            File.text("info.md", "# Title\\n")

        Args:
            name: the file's name in its post's folder, extension included.
            text: its text, saved as UTF-8 with `\\n` line endings on every platform.

        It has no history row, so it is written again each time its post is downloaded.
        """
        made = cls.__new__(cls)
        _assign(made, url=None, audio=None, name=name, size=None, content=text)
        return made


@dataclass(frozen=True, init=False)
class Post:
    """One post a category yields: its id, its files, its date and fields for the formats.

        Post(item["code"], [File(url) for url in urls], date=item["taken_at"], album=title)
        Post(code, lambda: files_of(site, code))      # listed only when the post isn't saved yet

    Frozen; `replace` gives a copy with fields changed.

    Args:
        id: the post's id as the site shows it; it names the files and starts their keys.
        files: the post's files in order, or a function returning them, called only for a post
            that passes `--time` and isn't saved yet, saving requests for saved posts.
        date: when it was posted, in any form `day()` reads (a date, a datetime, Unix seconds,
            milliseconds, a digit or ISO string); None for undated posts, which `--time` keeps.
        headers: headers for downloading each of its files, such as a CDN Referer.
        **fields: the site's own fields for the formats, such as `album`, `title` or `author`.

    Raises:
        TypeError: a field is one the engine sets (`category`, `num`, `count`, `index`,
            `extension`, `path`), so a source can never overwrite one by accident.
    """

    # The id, as text.
    id: str
    # The files, or a function giving them.
    files: tuple[File, ...] | Callable[[], Iterable[File]]
    # The date as given, read by `day()`.
    date: DateLike
    # Headers for downloading its files, or None.
    headers: Mapping[str, str] | None
    # The site's fields for the formats, read-only.
    fields: Mapping[str, Any]
    # The category the post was yielded by, set by the engine; "" for a route's own posts.
    category: str

    def __init__(self, id: str | int, files: Sequence[File] | Callable[[], Iterable[File]] = (), *,
                 date: DateLike = None, headers: Mapping[str, str] | None = None, **fields: Any):
        reserved = sorted(ENGINE_FIELDS & fields.keys())
        if reserved:
            msg = "is a field" if len(reserved) == 1 else "are fields"
            raise TypeError(
                f"Post: {', '.join(reserved)} {msg} the engine sets; name the site field otherwise"
            )
        _assign(self, id=str(id), files=files if callable(files) else tuple(files), date=date,
                headers=_frozen(headers), fields=MappingProxyType(dict(fields)), category="")

    def replace(self, **fields: Any) -> Post:
        """A copy with fields changed: `post.replace(album=highlight.title)`."""
        made = Post(
            fields.pop("id", self.id), fields.pop("files", self.files),
            date=fields.pop("date", self.date), headers=fields.pop("headers", self.headers),
            **{**self.fields, **fields},
        )
        _assign(made, category=self.category)
        return made

    def filed(self, category: str) -> Post:
        """A copy filed under `category`, which only the engine sets, as it lists the category."""
        made = self.replace()
        _assign(made, category=category)
        return made

    @property
    def lazy(self) -> bool:
        """Whether its files are a function, called only once the post is known to be wanted."""
        return callable(self.files)


def fill(template: str, fields: Mapping[str, Any], *, safe: bool = True) -> str:
    """Format a template from fields, dropping separators around missing or empty fields.

        fill("{category}/{album}", {"category": "posts"})                   # "posts"
        fill("{date}_{id}{index}", {"id": "C1abc", "date": "", "index": ""})  # "C1abc"
        fill("{author}/{id} {title}", {"author": "a/b", "id": 7})            # "a_b/7"

    Passes path segments through `text.clean.filename` when `safe` is True.
    """
    segments = []
    for segment in _split(template, "/"):
        piece = _fill_segment(segment, fields)
        if piece:
            segments.append(text.clean.filename(piece) if safe else piece)
    return "/".join(segments)


def day(value: Any) -> datetime.date | None:
    """The UTC day of any date a site gives, or None when it isn't one or can't be right.

        day(1767312000)                 # date(2026, 1, 2): Unix seconds
        day("1767312000000")            # the same, as milliseconds in a digit string
        day("2026-01-02 01:58:16")      # ISO 8601
        day(datetime(2026, 1, 2, 23, tzinfo=timezone(timedelta(hours=-5))))   # date(2026, 1, 3)

    Args:
        value: a `date`, a `datetime` (naive is read as UTC), Unix seconds or milliseconds as an
            int, float or digit string, an ISO 8601 string, or None.

    Returns:
        The day in UTC; None for None, text that is none of those, a number too large for a date,
        and any day before 2004 or after next year, which are mistakes for another number.
    """
    if isinstance(value, bool) or value is None:
        return None
    found: datetime.date | None = None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            value = int(text)
        else:
            try:
                value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if isinstance(value, datetime.datetime):
        found = (value.astimezone(datetime.timezone.utc) if value.tzinfo else value).date()
    elif isinstance(value, datetime.date):
        found = value
    elif isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) >= MILLISECONDS else value
        try:
            found = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).date()
        # Each platform refuses a huge timestamp its own way: OverflowError, OSError or ValueError.
        except (OverflowError, OSError, ValueError):
            return None
    if found is None or not FIRST_YEAR <= found.year <= datetime.date.today().year + 1:
        return None
    return found


@dataclass(frozen=True)
class Range:
    """`--time`'s two days, both included; None is no limit on that side.

        if post_day in site.time: ...           # an undated post (None) is always in it
        Range(since=date(2026, 1, 1)).until     # None
    """

    # The first day kept, or None.
    since: datetime.date | None = None
    # The last day kept, or None.
    until: datetime.date | None = None

    @classmethod
    def parse(cls, text: str) -> Range:
        """Read `--time`'s value, `<since>,<until>`, each side a day or `*` for no limit.

            Range.parse("2026-01-01,")               # Range(since=date(2026, 1, 1), until=None)
            Range.parse("*,2026-06-30")              # Range(since=None, until=date(2026, 6, 30))

        Args:
            text: the value as given on the command line.

        Raises:
            ValueError: there is no comma, a side isn't a day, or since is after until;
                the message is `TIME_USAGE`.
        """
        since, comma, until = text.partition(",")
        if not comma or "," in until:
            raise ValueError(TIME_USAGE)
        try:
            days = [_side(side) for side in (since, until)]
        except ValueError:
            raise ValueError(TIME_USAGE) from None
        if days[0] and days[1] and days[0] > days[1]:
            raise ValueError(TIME_USAGE)
        return cls(days[0], days[1])

    def __contains__(self, value: object) -> bool:
        """Whether a day is in range; None (undated post) always is, datetime counts by day."""
        if value is None:
            return True
        if isinstance(value, datetime.datetime):
            value = value.date()
        if not isinstance(value, datetime.date):
            return False
        return not ((self.since and value < self.since) or (self.until and value > self.until))


def _side(text: str) -> datetime.date | None:
    """One side of `--time`: YYYY-MM-DD day, or None for `*` or empty; `ValueError` otherwise."""
    text = text.strip()
    if text in ("", "*"):
        return None
    return datetime.datetime.strptime(text, "%Y-%m-%d").date()


class Site(Protocol):
    """The handle every route and category acts through, for one target.

        data = site.fetch(f"{API}/users/{user}", params={"count": 50}).json
        if site.goto(f"https://www.example.com/{user}/"):
            title = site.page.document.css("h1::text").first
        site.back_off("asked to wait a few minutes")

    The engine's is `flnd.site.TargetSite`; tests use `tests.fakes.FakeSite`. Every request to the
    source's own `domains` waits its turn under the source's `pace`, and every rate limit is
    waited out here, so a source never sleeps or retries itself.
    """

    @property
    def target(self) -> Target:
        """The target being downloaded."""
        ...

    @property
    def cookies(self) -> Mapping[str, str]:
        """The login's cookies, `{name: value}`, read-only; empty without a login."""
        ...

    @property
    def time(self) -> Range:
        """`--time`, for a source querying a range (tlg's `offset_date`); engine filters too."""
        ...

    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              in_page: bool = False, **request: Any) -> http.Response:
        """Make one request, paced and retried, and return its `http.Response`.

            items = site.fetch(API, params={"cursor": cursor}).json

        Raises `FetchError` on refusal or error.
        """
        ...

    def try_fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
                  in_page: bool = False, **request: Any) -> http.Response | None:
        """Like `fetch`, returning None for a 401, 403 or 404 instead of raising `FetchError`.

            profile = site.try_fetch(f"{API}/users/{user}")
        """
        ...

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        """Load `url` in target tab, paced; False when it didn't load or answered 400 or more.

        A 429 is waited out with `back_off` and the page loaded again, as long as it takes.
        """
        ...

    def handoff(
        self, *, until: Callable[[], bool], message: str, timeout: float | None = None
    ) -> bool:
        """Show `message` and wait for a person to do a step in the window, such as a captcha.

        Args:
            until: returns True once the step is done; polled about once a second.
            message: what the person should do, shown with a spinner.
            timeout: seconds before giving up; None waits as long as it takes.

        Returns:
            True when `until` came true; False when `timeout` passed first.
        """
        ...

    @property
    def page(self) -> browser.Page:
        """The target's tab, a `shared.browser.Page`, opened on first use; scrolling is paced."""
        ...

    @property
    def client(self) -> Any:
        """What the source's `connect` gave, such as an SDK client; None without one."""
        ...

    @property
    def state(self) -> MutableMapping[str, Any]:
        """The source's own store, kept between runs."""
        ...

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """The source's secrets, encrypted."""
        ...

    def info(self, message: str, **fields: Any) -> None:
        """A line naming the target: `site.info("{n} stories", n=3)`.

        Args:
            message: the text, with `{name}` fields filled in from `fields`.
            **fields: the values the message's fields take.
        """
        ...

    def warn(self, message: str, **fields: Any) -> None:
        """A warning naming the target, like `info`.

        Args:
            message: the text, with `{name}` fields filled in from `fields`.
            **fields: the values the message's fields take.
        """
        ...

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        """Count one rate limit and wait it out before returning.

        Args:
            reason: what the site did, shown on the countdown.
            retry_after: the seconds the site asked for; None waits 30 s up to 15 min.
        """
        ...


class Login(Protocol):
    """The handle a source login asks through (tlg's QR code and phone), in `flnd login` only.

        code = login.ask("Code", description="Sent to your phone")
        login.secrets["session"] = session_string
    """

    @property
    def interactive(self) -> bool:
        """Whether `ask` can ask: in a terminal."""
        ...

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        """Ask a question with `prompt.ask`'s names, and return what was typed.

        Args:
            message: the question.
            secret: hide what is typed, as for a password.
            description: more lines under the question.
        """
        ...

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """The source's secrets, encrypted, where a login keeps what it got."""
        ...

    def info(self, message: str, **fields: Any) -> None:
        """A line naming the source, such as `login.info("Signed in as {name}", name=name)`.

        Args:
            message: the text, with `{name}` fields filled in from `fields`.
            **fields: the values the message's fields take.
        """
        ...


def pages(fetch: Callable[[str | None], Any], cursor: Callable[[Any], str | None]) -> Iterator[Any]:
    """Each page of a paginated API: `fetch(None)`, then `fetch(c)` while `c` is new.

        for page in pages(
            lambda c: site.fetch(API, params=variables(c)).json, lambda page: page["next"]
        ):
            yield from posts_of(page)

    Args:
        fetch: one page, given the cursor; None asks for the first.
        cursor: the next cursor a page gives; None or "" when it is the last.

    It stops on an empty cursor and on one seen before, which a site sends at its end or when it
    loops, so a feed is never read forever. The first call in a category the job lists starts from
    the cursor an interrupted run saved, and tells the job the cursor of each page it reads.
    """
    resume: Resume | None = getattr(_listing, "resume", None)
    if resume is not None and resume.taken:
        resume = None
    if resume is not None:
        resume.taken = True
    start = resume.start if resume is not None else None
    page = _first_page(fetch, start, resume)
    seen: set[str] = set()
    while True:
        yield page
        following = cursor(page)
        if not following or following in seen:
            return
        seen.add(following)
        if resume is not None:
            # The cursor of the page being fetched and read: a run cut short reads it again, and
            # the posts it already saved are skipped.
            resume.cursor = following
        page = fetch(following)


class Resume:
    """One category's listing for `pages`: where it starts, and the cursor of the page it reads.

        with listing(Resume("c42", label="ins:example_user: posts")):
            ...
    """

    def __init__(
        self,
        start: str | None = None,
        *,
        label: str = "",
        saved: Callable[[str, datetime.date | None], bool] | None = None,
    ):
        """Set up a category listing's resume cursor, label, and saved-post predicate."""
        # The cursor an interrupted run saved; None starts from the newest.
        self.start = start
        # Names the listing in the line that says a saved cursor was refused.
        self.label = label
        # The cursor of the page being read, for the job to save if the listing is cut short.
        self.cursor: str | None = start
        # Whether a `pages` call took this listing already; only the first one resumes.
        self.taken = False
        # Whether the site refused `start`, so the listing began again from the newest.
        self.refused = False
        self._saved = saved
        self._streak = 0

    def caught_up(self, post_id: str, *, posted: Any = None) -> bool:
        """Whether `post_id` completes a streak of 5 already-saved posts for this listing."""
        if self._saved is None:
            return False
        if self._saved(post_id, day(posted)):
            self._streak += 1
        else:
            self._streak = 0
        return self._streak >= SAVED_STREAK


def caught_up(post_id: str, *, posted: Any = None) -> bool:
    """Whether `post_id` hits the current listing's 5-saved-post early-stop streak."""
    resume = getattr(_listing, "resume", None)
    return False if resume is None else resume.caught_up(post_id, posted=posted)


@contextmanager
def listing(resume: Resume | None) -> Iterator[None]:
    """Make `resume` the listing that `pages` calls on this thread report to, for the block."""
    previous = getattr(_listing, "resume", None)
    _listing.resume = resume
    try:
        yield
    finally:
        _listing.resume = previous


def _first_page(fetch: Callable[[str | None], Any], start: str | None,
                resume: Resume | None) -> Any:
    """The first page: from `start` if any, else from the newest; a refused `start` is dropped."""
    if not start or resume is None:
        return fetch(None)
    try:
        return fetch(start)
    except FetchError as error:
        # A cursor gone stale (a 4xx) is the site's answer for good; anything else is a failure.
        if error.status is None or not 400 <= error.status < 500:
            raise
    ui.info("{label}: the cursor an interrupted run saved was refused; listing from the newest",
             label=resume.label)
    resume.refused, resume.cursor = True, None
    return fetch(None)


# The listing the job is reading on this thread, which `pages` resumes and reports to.
_listing = threading.local()


class Source:
    """One site flnd downloads from: what it declares, its routes, and its categories.

        class Instagram(Source):
            code, name, domains = "ins", "Instagram", ("instagram.com",)
            needs = ("sessionid",)
            pace = http.Pace(1.0, budgets={"/graphql": (275, 600)})
            folder = "{category}/{album}"
            unsupported = {"instagram.com/stories": "stories not supported; give profile"}

            @target("{user}", "instagram.com/{user}")
            def user(self, site, user): return Account.load(site, user)

            @user.category(newest_first=True)
            def posts(self, site, account): yield from grid(site, account, "posts")

    Every subclass is listed in `flnd.sources.SOURCES`. When the class is created, its routes
    are collected in declaration order and checked: each pattern's host must be one of
    `domains`, and no pattern may appear twice. A mistake raises `TypeError` at import. Its
    `categories` are then its routes' categories, in declaration order, and its `single_kinds`
    the kinds of its routes without any.
    """

    # The prefix in a target and the cookie file name, such as "ins".
    code: ClassVar[str]
    # Shown in help, such as "Instagram".
    name: ClassVar[str]
    # The site's domains, short-link hosts included: for bare URLs, the cookies a login keeps, the
    # requests `prepare` sees and the ones the pace counts.
    domains: ClassVar[tuple[str, ...]]
    # `{pattern: message}`: links on the site that are refused with their own message, tried before
    # any route, such as Instagram's story links. A pattern here has no placeholder.
    unsupported: ClassVar[Mapping[str, str]] = {}
    # Routes collected from `@target` methods when class is created, in declaration order.
    routes: ClassVar[tuple[Route, ...]] = ()
    # The forms help and refusals list: generated from the routes when the class is created.
    forms: ClassVar[tuple[str, ...]] = ()
    # What `--from` can take from this source; without `--from`, a target gets every one. Derived
    # from the routes' categories when the class is created.
    categories: ClassVar[tuple[str, ...]] = ()
    # Target kinds that are one post, downloaded whole (ins "post", tik "video"): the routes without
    # categories. A post has none (tagged or reposts describe a profile's tab, not a post), so
    # `--from` with one is a usage error rather than a filter that silently drops every file.
    single_kinds: ClassVar[tuple[str, ...]] = ()
    # Cookie names, any one of which proves a login.
    needs: ClassVar[tuple[str, ...]] = ()
    requires_login: ClassVar[bool] = False
    login_methods: ClassVar[tuple[str, ...]] = ("browser", "file")
    # Minimum seconds between requests to this source's own domains.
    pace: ClassVar[float] = 0.5
    # Where a post's files go under the target's folder, what each is named (the content adds the
    # extension) and what its history row is keyed by: formats over a post's fields and the
    # engine's (see `fill` and `ENGINE_FIELDS`). A category may give its own.
    folder: ClassVar[str] = "{category}"
    filename: ClassVar[str] = "{date}_{id}{index}"
    key: ClassVar[str] = "{id}_{num}"
    fields: ClassVar[tuple[str, ...]] = ()
    # For this source's HTTP requests only; the browser always keeps its own.
    user_agent: ClassVar[str | None] = None
    browser: ClassVar[bool] = False
    stealth: ClassVar[bool] = True
    # Files of one target downloading at once: CDNs are built for many, and the host's own
    # connection limit in `shared.http` still holds.
    concurrency: ClassVar[int] = 8
    # `unsupported`, compiled when the class is created.
    _refusals: ClassVar[tuple[tuple[_Pattern, str], ...]] = ()

    def __init_subclass__(cls, **kwargs: Any):
        """Collect and check routes and unsupported patterns, and derive forms and categories.

        Raises:
            TypeError: a pattern outside `domains`, a pattern given twice, a placeholder in
                `unsupported`, or categories declared as both field and methods.
        """
        super().__init_subclass__(**kwargs)
        # A subclass keeps its base's routes; one of the same name replaces it in place.
        routes = {route.kind: route for route in cls.routes}
        routes.update(
            (value.kind, value) for value in vars(cls).values() if isinstance(value, Route)
        )
        cls.routes = tuple(routes.values())
        cls._refusals = tuple(
            (_Pattern(pattern), message) for pattern, message in cls.unsupported.items()
        )
        if any(pattern.names for pattern, _ in cls._refusals):
            raise TypeError(
                f"{cls.__name__}.unsupported: pattern captures nothing; use * for any segment"
            )
        domains = getattr(cls, "domains", ())
        seen: set[str] = set()
        for pattern in [pattern for route in cls.routes for pattern in route._patterns] + \
                [pattern for pattern, _ in cls._refusals]:
            if pattern.host is not None and not any(
                pattern.host == _bare_host(domain) for domain in domains
            ):
                raise TypeError(
                    f"{cls.__name__}: pattern {pattern.text!r} isn't on its domains {domains}"
                )
            # Compared without www. and m., which matching drops anyway.
            if pattern.host is None:
                key = pattern.text
            else:
                head, sep, tail = pattern.text.partition("://")
                host_part, slash, rest = (tail if sep else head).partition("/")
                key = f"{_bare_host(host_part.lower())}{slash}{rest}"
            if key in seen:
                raise TypeError(f"{cls.__name__}: pattern {pattern.text!r} is given twice")
            seen.add(key)
        if "categories" in vars(cls) or "single_kinds" in vars(cls):
            raise TypeError(
                f"{cls.__name__}: its categories are its @<route>.category methods; "
                "declare no categories or single_kinds field"
            )
        names = [category.name for route in cls.routes for category in route.categories]
        cls.categories = tuple(dict.fromkeys(names))
        cls.single_kinds = tuple(
            route.kind for route in cls.routes if not route.categories and not route.discovers
        )
        if not cls.routes:
            return
        code = getattr(cls, "code", "")
        forms = [form for route in cls.routes for form in route.forms(code)]
        # `ins:<user>` is the form people write most, so forms after `code:` or `code?` come first.
        cls.forms = tuple(
            sorted(forms, key=lambda form: not form.startswith((f"{code}:", f"{code}?")))
        )

    def resolve(self, text: str) -> Target:
        """Read one target's text, as people write it, through the routes.

            Instagram().resolve("https://www.instagram.com/p/C1abcDEFghi/")

        Override it only for forms no pattern can say (tlg's chat titles and `t.me/c/<n>` links),
        and pass anything else on with `super().resolve(text)`.

        Args:
            text: the text after `code:`, or a bare URL on the source's domains, stripped.

        Returns:
            The target the first matching route reads.

        Raises:
            TargetError: an `unsupported` pattern matched, with its message, and no route pattern
                that matches too has more literal segments than it; or nothing matched, listing
                `forms`, with `url` set when the text is a link on the source's domains.
        """
        url = _parse(text, self.domains)
        # The most specific pattern wins (user, 2026-11-12): a refusal for a whole path (`x.com/i`)
        # shouldn't catch a longer, readable link under it (`x.com/i/web/status/<id>`), while the
        # catch-all routes (`{user}`) are less specific than any refusal and never reopen one.
        refused = next(((pattern, message) for pattern, message in self._refusals
                        if pattern.match(text, url) is not None), None)
        for route in self.routes:
            for pattern in route._patterns:
                value = pattern.match(text, url)
                if value is None:
                    continue
                if refused is None or pattern.literals > refused[0].literals:
                    if route.is_query:
                        stripped = value.strip()
                        if not stripped:
                            raise TargetError(f"{self.code}: the query is empty")
                        return Target(self.code, route.kind, stripped, query=True)
                    return Target(self.code, route.kind, value)
        if refused is not None:
            raise TargetError(f"{self.code}: {refused[1]}")
        link = None if url is None else text if _is_url(text) else f"https://{text}"
        raise TargetError(
            f"{self.code}: can't read {text!r}; write it like {', '.join(self.forms)}", url=link
        )

    def prepare(self, request: httpx.Request, cookies: Mapping[str, str], /) -> httpx.Request:
        """Add headers or signature to a request on source domains before each attempt.

            def prepare(self, request, cookies):
                request.headers["x-csrf-token"] = cookies.get("ct0", "")
                return request
        """
        return request

    def login(self, login: Login, method: str, /) -> None:
        """Log in by `method` (tlg), asking through `login` (a `Login`).

        Cookie sources keep this default, and the engine's `browser` and `file` methods log in.
        A login that fails raises `LoginError`, which `flnd login` reports.
        """
        raise NotImplementedError(f"{self.code} has no login method {method!r}")

    def connect(self, site: Site, /) -> Any:
        """A context manager, or an async one, giving `site.client`; entered once per run.

        Args:
            site: the `Site` of the source's account.

        Raises:
            LoginError: the source needs a login it doesn't have.
        """
        return nullcontext()

    def date(self, site: Site, post: Post, /) -> DateLike:
        """The day of a post its listing left undated, from its own page; None if the site can't.

        The engine asks it once per undated post as its category ends, and each `sync` asks it for
        files saved undated or with an estimated day. A source overrides it with one paced request.

        Raises:
            FetchError: the item can't be read; a 401, 403 or 429 stops asking for the run.
        """
        return None

    def logged_in(self, site: Site, /) -> bool:
        """Whether a login is stored: by default, one of `needs` is among the saved cookies.

        Args:
            site: the account's `Site`; its `cookies` and `secrets` say what the login kept.
        """
        return not self.needs or any(name in site.cookies for name in self.needs)


def _parse(text: str, domains: tuple[str, ...]) -> _Url | None:
    """`text` as a `_Url` when it's a URL on one of `domains`, with or without a scheme."""
    # Without a scheme, a slash tells a link (instagram.com/example_user) from a name.
    if "/" not in text and not _is_url(text):
        return None
    parsed = urlsplit(text if _is_url(text) else f"https://{text}")
    host = (parsed.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in domains):
        return None
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    parts = tuple(unquote(part) for part in parsed.path.split("/") if part)
    return _Url(_bare_host(host), parts, query)


def _bare_host(host: str) -> str:
    """A host without prefixes that are the same site: `www.m.instagram.com` is `instagram.com`."""
    while True:
        before = host
        for prefix in SITE_PREFIXES:
            host = host.removeprefix(prefix)
        if host == before:
            return host


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def _split(text: str, separator: str, *, once: bool = False) -> list[str]:
    """`text` split at `separator` outside `{…}`, allowing `/`, `?` or `{16,}` in regexes.

    With `once`, it splits at the first one only and returns `[before, separator, after]`, or
    `[text, "", ""]` when there's none, as `str.partition` does.
    """
    parts, depth, start, escaped = [], 0, 0, False
    for index, character in enumerate(text):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        depth += {"{": 1, "}": -1}.get(character, 0)
        if character == separator and depth == 0:
            if once:
                return [text[:index], separator, text[index + 1:]]
            parts.append(text[start:index])
            start = index + 1
    return [text, "", ""] if once else [*parts, text[start:]]


def _tokens(segment: str, pattern: str) -> list[tuple[str, str, str]]:
    """A segment's pieces: `("text", literal, "")`, `("any", "*", "")` or `("hole", name, regex)`.

    Args:
        segment: one path segment or query value of the pattern.
        pattern: the whole pattern, for the error message.

    Raises:
        TypeError: an unclosed `{`, a stray `}`, or a placeholder whose name isn't an identifier.
    """
    tokens, index = [], 0
    while index < len(segment):
        character = segment[index]
        if character == "\\" and index + 1 < len(segment):
            tokens.append(("text", segment[index:index + 2], ""))
            index += 2
            continue
        if character == "}":
            raise TypeError(f"pattern {pattern!r}: a '}}' with no '{{'")
        if character == "*":
            tokens.append(("any", "*", ""))
            index += 1
            continue
        if character != "{":
            tokens.append(("text", character, ""))
            index += 1
            continue
        depth, end, escaped = 0, index, False
        for end in range(index, len(segment)):
            if escaped:
                escaped = False
                continue
            if segment[end] == "\\":
                escaped = True
                continue
            depth += {"{": 1, "}": -1}.get(segment[end], 0)
            if depth == 0:
                break
        if depth:
            raise TypeError(f"pattern {pattern!r}: a '{{' with no '}}'")
        name, _, regex = segment[index + 1:end].partition(":")
        if not name.isidentifier():
            raise TypeError(f"pattern {pattern!r}: {{{name}}} isn't a placeholder name")
        tokens.append(("hole", name, regex))
        index = end + 1
    return tokens


def _compile(segment: str, pattern: str, names: list[str]) -> re.Pattern[str]:
    """One segment as a regex matching a whole URL part, adding its placeholders' names to `names`.

    Args:
        segment: one path segment or query value of the pattern.
        pattern: the whole pattern, for the error message.
        names: the names of the pattern's placeholders so far; the first one is captured as `value`.

    Raises:
        TypeError: the segment, or a placeholder's regex, is malformed.
    """
    regex = ""
    for kind, text, custom in _tokens(segment, pattern):
        if kind == "text":
            regex += re.escape(text)
        elif kind == "any":
            regex += "[^/]*"
        else:
            names.append(text)
            # Only the first placeholder is captured: a second one is refused once they're counted.
            group = "(?P<value>" if len(names) == 1 else "(?:"
            regex += f"@?{group}{NAME})" if not custom else f"{group}{custom})"
    try:
        return re.compile(regex)
    except re.error as error:
        raise TypeError(f"pattern {pattern!r}: {error}") from None


def _shown(segment: str) -> str:
    """A segment as help shows it: `{name:regex}` as `<name>`, `*` as `…`."""
    return "".join(
        {"text": text, "any": ANY}.get(kind, f"<{text}>")
        for kind, text, _ in _tokens(segment, segment)
    )


def _assign(instance: Any, **values: Any) -> None:
    """Set a frozen dataclass's fields from its own `__init__`."""
    for name, value in values.items():
        object.__setattr__(instance, name, value)


def _frozen(headers: Mapping[str, str] | None) -> Mapping[str, str] | None:
    """A read-only copy of `headers`, or None for none."""
    return MappingProxyType(dict(headers)) if headers else None


# The formatter of every format: `get_value` and `format_field` are str.format's own.
_FORMATTER = string.Formatter()


def _fill_segment(segment: str, fields: Mapping[str, Any]) -> str:
    """One path segment of a format, filled in, with dropped separators (see `fill`)."""
    # Each piece is [text, whether literal]; only literals lose separators.
    pieces: list[list[Any]] = []
    # Set by an empty field that starts the segment: the separators after it go too.
    drop_next = False
    for literal, name, spec, conversion in _FORMATTER.parse(segment):
        if literal:
            if drop_next:
                literal, drop_next = literal.lstrip(SEPARATORS), False
            pieces.append([literal, True])
        if name is None:
            continue
        value = _field(name, fields)
        text = ""
        if value is not None and value != "":
            text = _FORMATTER.format_field(_FORMATTER.convert_field(value, conversion), spec or "")
        if text:
            pieces.append([text, False])
            drop_next = False
        elif pieces and pieces[-1][1]:
            pieces[-1][0] = pieces[-1][0].rstrip(SEPARATORS)
        elif not any(piece for piece, _ in pieces):
            drop_next = True
    return "".join(piece for piece, _ in pieces)


def _field(name: str, fields: Mapping[str, Any]) -> Any:
    """A field's value, `a.b` and `a[0]` included; None when it's missing."""
    try:
        return _FORMATTER.get_field(name, (), fields)[0]
    # A missing field is "" by the formats rule, whichever way it's missing.
    except (KeyError, AttributeError, IndexError, TypeError):
        return None
