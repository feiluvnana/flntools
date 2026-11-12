"""What a source is written against: `Source`, its routes and categories, `Post` and `File`, `Site` and `Login`.

A source is one class. Its fields say what the site is (`code`, `domains`, `pace`, the `folder`,
`filename` and `key` formats, …), its routes say which targets it reads, and its categories say
what it can download from them. The engine does the rest: it reads the target, calls the route
and the categories `--from` asked for, filters by `--time`, names and keys every file, skips
what is saved, downloads, compresses, dates the files and writes the history.

    from flnd.source import File, Post, Site, Source, target

    class Example(Source):
        code, name, domains = "exm", "Example", ("example.com",)
        folder = "{category}/{album}"               # an empty field and its separator are dropped

        @target("example.com/p/{code:[\\w-]+}")
        def post(self, site: Site, code: str):    # no categories: it yields its posts itself
            yield Post(code, [File(f"https://cdn.example.com/{code}.jpg")])

        @target("{user}", "example.com/{user}")
        def user(self, site: Site, user: str) -> dict:
            return site.fetch(f"https://api.example.com/users/{user}").json   # the context

        @user.category(newest_first=True)
        def posts(self, site: Site, profile: dict):
            for item in profile["posts"]:
                yield Post(item["id"], [File(url) for url in item["urls"]], date=item["taken_at"])

How a target is read: a route is a method marked `@target(*patterns)`; the method's name is the
target's kind, and each pattern is one form people write. The default `Source.resolve` reads a
target's text through the routes:
1. the source's `unsupported` patterns first, each refusing with its own message, unless a route
   pattern matches the same text and is more specific: it has more literal segments (the host
   counts as one; `{…}` and `*` don't), so twt's `x.com/i` refusal doesn't catch
   `x.com/i/web/status/<id>`;
2. then each route in the order the class declares it, and each route's patterns in order;
3. the first pattern that matches gives `Target(code, kind, value)`, its one placeholder's value.

A URL on the source's domains that nothing matches raises a `TargetError` carrying that URL. The
engine follows it once with a HEAD request (short links such as `vm.tiktok.com/…`) and reads where
it leads, so a source never makes a request to read a target. Help and refusals list the forms the
patterns generate, such as `ins:<user>`, so what's shown can't drift from what's accepted.

How a target is downloaded (`flnd/job.py`): a route with categories returns a context, whatever
its categories share, and each category is a method marked `@<route>.category` that takes
`(site, context)` and yields `Post`s. A route without categories yields its posts itself, and
they are filed in the target's own folder. Either may be `async def` (tlg). The engine sets each
post's `category`, reads its `date` with `day()`, and names each file from the formats (`fill`).

This module holds declarations only: the engine's `TargetSite`, `LoginSite` and the job live in
`flnd/site.py` and `flnd/job.py`, which a source never imports. Importing it loads no third-party
module.
"""

from __future__ import annotations

import datetime
import inspect
import re
import string
from contextlib import nullcontext
from dataclasses import dataclass
from types import MappingProxyType, MethodType
from typing import (TYPE_CHECKING, Any, Callable, ClassVar, Iterable, Iterator, Literal, Mapping,
                    MutableMapping, Protocol, Sequence, Union, overload)
from urllib.parse import parse_qs, urlsplit

from flnd.common.pace import Pace
from shared import fmt

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from shared import http
    from shared._browser import Page

__all__ = ["Category", "DateLike", "Error", "Fetch", "FetchError", "File", "Login", "LoginError", "Pace", "Post",
           "Range", "Route", "Site", "Source", "Target", "TargetError", "day", "fill", "pages", "target"]

# Hosts that are the same site as their bare domain: the desktop, mobile and old mobile sites.
SITE_PREFIXES = ("www.", "m.", "mobile.")
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
# Seconds since 1970 pass this in 5138; a larger number is milliseconds (13 digits today, 10 for seconds).
MILLISECONDS = 1e11

# Seconds before a retry, or a function of (error, attempt) giving them; None means the usual wait.
Wait = Union[float, Callable[[BaseException, int], Union[float, None]]]
# A file without a URL: `fetch(dest, progress)` writes it to `dest`, reporting `progress(done, total)`
# in bytes. It may return a coroutine (tlg's SDK), which the engine runs on the source's loop.
Fetch = Callable[["Path", Callable[..., None]], Any]
# What `Post(date=)` takes; `day()` reads every one of them.
DateLike = Union[datetime.date, int, float, str, None]


class Error(Exception):
    """The base of the errors a source raises: `except flnd.source.Error` catches all three."""


class FetchError(Error):
    """A request that failed: `status` is the refusing HTTP status, or None (no response, or not JSON).

        try:
            data = site.fetch(url).json
        except FetchError as error:
            if error.status == 403: ...

    Args:
        message: what failed, shown to the person.
        status: the HTTP status that refused the request; None when there was no response.
    """

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class LoginError(Error):
    """A login that didn't work, with what to do instead: `LoginError("tlg: run flnd login tlg")`."""


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

        target = Target("tik", "video", "7301234567890123456"); str(target)   # "tik:7301234567890123456"
    """

    # The source's code, such as "ins".
    source: str
    # What it points to: its route's name (`user`, `post`, `video` or `chat`).
    kind: str
    # Its clean form, the captured placeholder: it names the target's folder and starts its history keys.
    value: str

    def __str__(self) -> str:
        # The account a login and `connect` see has no value, and reads as its source's code.
        return f"{self.source}:{self.value}" if self.value else self.source


@dataclass(frozen=True)
class _Url:
    """A URL on a source's site: its host without `www.`, `m.` or `mobile.`, its path's parts, and its query."""

    host: str
    parts: tuple[str, ...]
    query: Mapping[str, str]


class _Pattern:
    """One compiled pattern: a domain pattern matches a `_Url`, a code pattern the text after `code:`."""

    def __init__(self, text: str):
        self.text = text
        path, mark, query = _split(text, "?", once=True)
        segments = [segment for segment in _split(path, "/") if segment]
        first = segments[0] if segments else ""
        # A first segment with a dot and no placeholder or `*` is a host; anything else is a code pattern.
        self.host = _bare_host(first.lower()) if "." in first and not set(first) & set("{}*") else None
        if self.host is None and mark:
            raise TypeError(f"pattern {text!r}: only a URL pattern can name a query")
        self.segments = segments[1:] if self.host is not None else ["/".join(segments)]
        self.keys = [_split(pair, "=", once=True) for pair in _split(query, "&")] if mark else []
        if any(not key or not value for key, _, value in self.keys):
            raise TypeError(f"pattern {text!r}: write a query as ?key={{name}}")
        # Every placeholder's name, in order: a route's pattern has exactly one, an unsupported one none.
        self.names: list[str] = []
        self.regexes = [_compile(segment, text, self.names) for segment in self.segments]
        self.query = [(key, _compile(value, text, self.names)) for key, _, value in self.keys]
        # How specific the pattern is: its literal segments, the host being one. A code pattern has none.
        self.literals = (self.host is not None) + sum(
            1 for segment in self.segments if self.host is not None and not set(segment) & set("{}*"))

    def match(self, text: str, url: _Url | None) -> str | None:
        """The captured value when `text` (or its `url`, for a domain pattern) matches; "" for no placeholder."""
        if self.host is None:
            found = self.regexes[0].fullmatch(text) if url is None else None
            return None if found is None else found.groupdict().get("value", "")
        if url is None or url.host != self.host or len(url.parts) < len(self.regexes):
            return None
        # The pattern's segments match the URL's first parts; the parts after them are allowed.
        found = [regex.fullmatch(part) for regex, part in zip(self.regexes, url.parts)]
        found += [regex.fullmatch(url.query[key]) if key in url.query else None for key, regex in self.query]
        if not all(found):
            return None
        return next((match["value"] for match in found if match and "value" in match.groupdict()), "")

    def form(self, code: str) -> str:
        """The pattern as people write it, each placeholder in angle brackets: `https://www.instagram.com/p/<code>/`."""
        path = "/".join(_shown(segment) for segment in self.segments)
        if self.host is None:
            return f"{code}:{path}"
        # Help shows the address a browser's bar does: www. on a bare domain, and a closing slash on a folder.
        host = f"www.{self.host}" if self.host.count(".") == 1 else self.host
        query = "&".join(f"{key}={_shown(value)}" for key, _, value in self.keys)
        slash = "" if query or "." in path.rsplit("/", 1)[-1] else "/"
        return f"https://{host}/{path}{slash if path else ''}" + (f"?{query}" if query else "")




class Category:
    """One thing a route's target can download, made by `@<route>.category`: `Instagram.routes[1].categories[0]`.

        @user.category(newest_first=True)
        def posts(self, site, account): ...

        Instagram.routes[1].categories[0].name        # "posts"

    Args:
        function: the decorated method, `(self, site, context)`, yielding `Post`s; it may be an
            `async def` generator.
        newest_first: whether it yields posts newest first, so the engine may stop early on
            `--time`. A wrong True silently loses posts, so it is set only where the site's order is known.
        name: the category's name, as `--from` and help write it; None is the method's name.
        folder, filename, key: formats for this category's files instead of the source's, for a
            category whose files were always named another way (a profile picture keyed by its path);
            None keeps the source's.
    """

    def __init__(self, function: Callable[..., Any], *, newest_first: bool = False, name: str | None = None,
                 folder: str | None = None, filename: str | None = None, key: str | None = None):
        self.function = function
        self.attribute = function.__name__
        self.name = name or function.__name__
        self.newest_first = newest_first
        self.folder, self.filename, self.key = folder, filename, key
        parameters = list(inspect.signature(function).parameters)
        if len(parameters) != 3:
            raise TypeError(f"category {self.name}: take (self, site, context), not ({', '.join(parameters)})")

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

    def __init__(self, function: Callable[..., Any], patterns: tuple[str, ...], *, shown: str | None):
        """Compile `patterns` for `function`, raising `TypeError` for a mistake in them.

        Args:
            function: the decorated method, `(self, site, value)`.
            patterns: the forms it reads, as `target` takes them.
            shown: the form help shows for the first pattern, or the route's only form when it has none.

        Raises:
            TypeError: a pattern doesn't compile, has no placeholder or two, or its placeholder
                isn't the method's value parameter.
        """
        self.function = function
        self.kind = function.__name__
        self.patterns = patterns
        self.shown = shown
        self._patterns = [_Pattern(pattern) for pattern in patterns]
        self._categories: list[Category] = []
        parameters = [parameter.name for parameter in inspect.signature(function).parameters.values()]
        if len(parameters) != 3:
            raise TypeError(f"route {self.kind}: take (self, site, value), not ({', '.join(parameters)})")
        for pattern in self._patterns:
            if len(pattern.names) != 1:
                # One value is the target's identity: its folder and its history keys.
                raise TypeError(f"route {self.kind}: pattern {pattern.text!r} needs exactly one placeholder, "
                                f"not {len(pattern.names)}")
            if pattern.names[0] != parameters[2]:
                raise TypeError(f"route {self.kind}: pattern {pattern.text!r} captures {pattern.names[0]!r}, "
                                f"but the method's value parameter is {parameters[2]!r}")

    @property
    def categories(self) -> tuple[Category, ...]:
        """The route's categories, in the order the class declares them; empty for a route that yields posts itself."""
        return tuple(self._categories)

    @overload
    def category(self, function: Callable[..., Any], /) -> Callable[..., Any]: ...
    @overload
    def category(self, /, *, newest_first: bool = False, name: str | None = None, folder: str | None = None,
                 filename: str | None = None, key: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...
    def category(self, function: Callable[..., Any] | None = None, /, *, newest_first: bool = False,
                 name: str | None = None, folder: str | None = None, filename: str | None = None,
                 key: str | None = None) -> Any:
        """Mark a method as one of this route's categories, with or without options.

            @user.category
            def stories(self, site, account): ...

            @user.category(newest_first=True, name="reels")
            def reel_grid(self, site, account): ...

        The `@property` / `@x.setter` idiom: the route in the class body registers the category,
        and the method is returned unchanged, so it stays an ordinary method tests can call.

        Args:
            function: the method, when the decorator is used bare (`@user.category`).
            newest_first: whether the category yields posts newest first; see `Category`.
            name: its name for `--from`, when it isn't the method's.
            folder, filename, key: formats instead of the source's for its files; see `Category`.

        Returns:
            The method itself; or, with options, a decorator that registers and returns it.

        Raises:
            TypeError: the method doesn't take `(self, site, context)`, or the route already has
                a category of that name.
        """
        def register(method: Callable[..., Any]) -> Callable[..., Any]:
            made = Category(method, newest_first=newest_first, name=name, folder=folder, filename=filename, key=key)
            if any(known.name == made.name for known in self._categories):
                raise TypeError(f"route {self.kind}: category {made.name!r} is declared twice")
            self._categories.append(made)
            return method

        return register(function) if function is not None else register

    def __set_name__(self, owner: type, name: str):
        """Take the attribute's name as the kind, which is the method's own name unless the class was built by `type()`."""
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


def target(*patterns: str, shown: str | None = None) -> Callable[[Callable[..., Any]], Route]:
    """Mark a method as a route: the targets it reads, as patterns, named by the method's kind.

        @target("instagram.com/p/{code:[\\w-]+}", "instagram.com/reel/{code:[\\w-]+}")
        def post(self, site, code): ...

    The pattern syntax, in five rules:
    1. a pattern that starts with a domain matches URLs on that host: the scheme is optional, and
       `www.`, `m.`, `mobile.`, a trailing `/`, `#…`, a query it doesn't name and any path
       segments after it are ignored;
    2. a pattern without a domain matches the text after `code:` (`{user}` reads `ins:example_user`);
    3. `{name}` is one segment, captured: a user name, `[\\w.]+`, with one leading `@` dropped;
    4. `{name:regex}` is one segment matching the regex, and `*` is any one segment (or, inside
       a segment, any text), not captured;
    5. `?key={name}` captures a query parameter.

    Each pattern has exactly one placeholder, named as the method's value parameter.

    Args:
        *patterns: the forms the route reads, tried in order. None, as `@target()`, makes a route
            only a hand-written `resolve` reaches (tlg's).
        shown: the form help shows instead of the first pattern's generated one, when that reads
            badly, such as "tik:<video id>"; for a route with no patterns, its only form.

    Returns:
        A decorator turning the method into a `Route`.

    Raises:
        TypeError: a pattern is malformed, or doesn't capture the method's value parameter. It
            fails when the class is created, so a mistake fails at import.
    """
    return lambda function: Route(function, patterns, shown=shown)


@dataclass(frozen=True, init=False)
class File:
    """One file of a post: a URL (or mirrors, or a fetch function), with what downloading it needs.

        File("https://cdn.example.com/a.jpg")
        File([mirror_a, mirror_b], name="loi-5930.7z")             # kept as the site names it
        File(video_url, audio=audio_url)                            # DASH: merged into one video
        File.text("info.md", "# Title\\n")                          # written, not downloaded

    The engine names it from the source's formats, gives it the extension its content calls for,
    and numbers it by its place in the post. Frozen.

    Args:
        url: a URL, mirror URLs tried in order, or a `Fetch` function `fetch(dest, progress)`
            for media without a URL (tlg's).
        audio: the URL of a separate audio stream (DASH), merged into the video; None when the
            video has its own sound. A file with `audio` is a video.
        headers: headers for downloading it, such as a CDN Referer; None takes its post's.
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
    # Headers for this file's download, or None for its post's.
    headers: Mapping[str, str] | None
    # The name as the site gives it, or None for the formats' name.
    name: str | None
    # The size in bytes, or None.
    size: int | None
    # A text file's text, written as UTF-8; None for a file that is downloaded.
    content: str | None

    def __init__(self, url: str | Sequence[str] | Fetch, *, audio: str | None = None,
                 headers: Mapping[str, str] | None = None, name: str | None = None, size: int | None = None):
        if isinstance(url, str) or callable(url):
            sources: Any = url
        elif isinstance(url, Sequence) and url and all(isinstance(mirror, str) for mirror in url):
            sources = tuple(url)
        else:
            raise TypeError(f"File takes a URL, mirror URLs or a fetch function, not {url!r}")
        _assign(self, url=sources, audio=audio, headers=_frozen(headers), name=name, size=size, content=None)

    @classmethod
    def text(cls, name: str, text: str) -> File:
        """A text file the source writes itself, such as loi's `info.md`: saved atomically, never downloaded.

            File.text("info.md", "# Title\\n")

        Args:
            name: the file's name in its post's folder, extension included.
            text: its text, saved as UTF-8 with `\\n` line endings on every platform.

        It has no history row, so it is written again each time its post is downloaded.
        """
        made = cls.__new__(cls)
        _assign(made, url=None, audio=None, headers=None, name=name, size=None, content=text)
        return made


@dataclass(frozen=True, init=False)
class Post:
    """One post a category yields: its id, its files, its date and the site's own fields for the formats.

        Post(item["code"], [File(url) for url in urls], date=item["taken_at"], album=title)
        Post(code, lambda: files_of(site, code))      # listed only when the post isn't saved yet

    Frozen; `replace` gives a copy with fields changed.

    Args:
        id: the post's id as the site shows it; it names the files and starts their keys.
        files: the post's files in order, or a function returning them, called only for a post
            that passes `--time` and isn't saved yet, so a post's own request costs nothing once saved.
        date: when it was posted, in any form `day()` reads (a date, a datetime, Unix seconds or
            milliseconds, a digit or ISO string); None for an undated post, which `--time` always keeps.
        headers: headers for each of its files that has none, such as one CDN Referer.
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
    # Headers for each file without its own, or None.
    headers: Mapping[str, str] | None
    # The site's fields for the formats, read-only.
    fields: Mapping[str, Any]
    # The category the post was yielded by, set by the engine; "" for a route's own posts.
    category: str

    def __init__(self, id: str | int, files: Sequence[File] | Callable[[], Iterable[File]] = (), *,
                 date: DateLike = None, headers: Mapping[str, str] | None = None, **fields: Any):
        reserved = sorted(ENGINE_FIELDS & fields.keys())
        if reserved:
            raise TypeError(f"Post: {', '.join(reserved)} {'is a field' if len(reserved) == 1 else 'are fields'} "
                            "the engine sets; name the site's own field another way")
        _assign(self, id=str(id), files=files if callable(files) else tuple(files), date=date,
                headers=_frozen(headers), fields=MappingProxyType(dict(fields)), category="")

    def replace(self, **fields: Any) -> Post:
        """A copy with fields changed: `post.replace(album=highlight.title)`, `post.replace(files=[...])`.

        Args:
            **fields: `id`, `files`, `date`, `headers`, or the site's own fields, as `Post` takes them.

        Raises:
            TypeError: a field the engine sets, as for `Post`.
        """
        made = Post(fields.pop("id", self.id), fields.pop("files", self.files), date=fields.pop("date", self.date),
                    headers=fields.pop("headers", self.headers), **{**self.fields, **fields})
        object.__setattr__(made, "category", self.category)
        return made

    @property
    def lazy(self) -> bool:
        """Whether its files are a function, called only once the post is known to be wanted."""
        return callable(self.files)


def fill(template: str, fields: Mapping[str, Any], *, safe: bool = True) -> str:
    """A format filled in from fields: a missing or empty field is "", and the separator it leaves is dropped.

        fill("{category}/{album}", {"category": "posts"})                   # "posts"
        fill("{date}_{id}{index}", {"id": "C1abc", "date": "", "index": ""})  # "C1abc"
        fill("{author}/{id} {title}", {"author": "a/b", "id": 7})            # "a_b/7"

    Args:
        template: a `str.format` string; `/` in it separates path segments.
        fields: the values; one that is missing or None counts as "".
        safe: pass each segment through `fmt.filename`, so a value from a site (a title holding
            `../` or `:`) can never leave the folder or break a name on any system. False is for
            history keys, which are never paths.

    Returns:
        The segments joined by `/`, an empty segment dropped; "" when every one came out empty.

    The separators dropped beside an empty field are `_`, `-`, space, `.`, `:` and `,`: those
    before it, or those after it when it starts its segment. A value's own text is never touched.
    """
    segments = []
    for segment in _split(template, "/"):
        text = _fill_segment(segment, fields)
        if text:
            segments.append(fmt.filename(text) if safe else text)
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

    def __contains__(self, value: object) -> bool:
        """Whether a day is in the range; None (an undated post) always is, and a datetime counts by its day."""
        if value is None:
            return True
        if isinstance(value, datetime.datetime):
            value = value.date()
        if not isinstance(value, datetime.date):
            return False
        return not ((self.since and value < self.since) or (self.until and value > self.until))


class Site(Protocol):
    """The handle every route and category acts through, for one target: 13 members.

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
        """`--time`, for a source that can ask its server for a range (tlg's `offset_date`); the engine filters anyway."""
        ...

    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              soft: Literal[False] = ..., in_page: bool = ..., **request: Any) -> http.Response: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              soft: Literal[True], in_page: bool = ..., **request: Any) -> http.Response | None: ...
    @overload
    def fetch(self, url: str, *, method: str = ..., headers: Mapping[str, str] | None = ...,
              soft: bool, in_page: bool = ..., **request: Any) -> http.Response | None: ...
    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              soft: bool = False, in_page: bool = False, **request: Any) -> http.Response | None:
        """Make one request, paced and retried, and return its `http.Response`.

            items = site.fetch(API, params={"cursor": cursor}).json
            profile = site.fetch(f"{API}/users/{user}", soft=True)      # None for a 401, 403 or 404

        Args:
            url: the URL to request.
            method: the HTTP method.
            headers: headers on top of the source's for this request; None adds none.
            soft: True returns None for a 401, 403 or 404 instead of raising.
            in_page: send it from the open page itself, with its cookies and TLS fingerprint
                (`Page.fetch`), for an API the site checks that way.
            **request: httpx's request options, such as `params`, `data`, `json` and `timeout`.

        Returns:
            The response; None only with `soft`.

        Raises:
            FetchError: no response after the retries, or a 4xx other than 429 (with `status`).
        """
        ...

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        """Load `url` in the target's tab, paced; False (with a warning) when it didn't load or answered 400 or more.

        A 429 is waited out with `back_off` and the page loaded again, as long as it takes.
        """
        ...

    def handoff(self, *, until: Callable[[], bool], message: str, timeout: float | None = None) -> bool:
        """Show `message` and wait for a person to do a step in the window (a captcha); False when `timeout` passed first."""
        ...

    @property
    def page(self) -> Page:
        """The target's tab, a `shared.browser.Page`, opened (blank) on first use; its scrolling keeps the source's pace."""
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
        """A line naming the target: `site.info("{n} stories", n=3)`."""
        ...

    def warn(self, message: str, **fields: Any) -> None:
        """A warning naming the target, like `info`."""
        ...

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        """Count one rate limit (a captcha nobody solved, a "please wait" body), and wait it out before returning.

        Args:
            reason: what the site did, shown on the countdown.
            retry_after: the seconds the site asked for; None waits 30 s, 60 s, 120 s, … up to 15 min.
        """
        ...


class Login(Protocol):
    """The handle a source's own `login` asks through (tlg's QR code and phone), during `flnd login` only.

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
        """A line naming the source."""
        ...


def pages(fetch: Callable[[str | None], Any], cursor: Callable[[Any], str | None]) -> Iterator[Any]:
    """Each page of a paginated API: `fetch(None)`, then `fetch(c)` while the last page gives a cursor `c` not seen before.

        for page in pages(lambda c: site.fetch(API, params=variables(c)).json, lambda page: page["next"]):
            yield from posts_of(page)

    Args:
        fetch: one page, given the cursor; None asks for the first.
        cursor: the next cursor a page gives; None or "" when it is the last.

    It stops on an empty cursor and on one seen before, which a site sends at its end or when it
    loops, so a feed is never read forever.
    """
    seen: set[str] = set()
    page = fetch(None)
    while True:
        yield page
        following = cursor(page)
        if not following or following in seen:
            return
        seen.add(following)
        page = fetch(following)


class Source:
    """One site flnd downloads from: what it declares, its routes, and its categories.

        class Instagram(Source):
            code, name, domains = "ins", "Instagram", ("instagram.com",)
            needs = ("sessionid",)
            pace = Pace(1.0, budgets={"/graphql": (275, 600)})
            folder = "{category}/{album}"
            unsupported = {"instagram.com/stories": "story links aren't supported; give the profile"}

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
    # The routes, collected from the `@target` methods when the class is created, in declaration order.
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
    # How fast its own domains may be asked: `Pace(floor, budgets)`, or a number for the floor in seconds.
    pace: ClassVar[Pace | float] = 0.5
    # Where a post's files go under the target's folder, what each is named (the content adds the
    # extension) and what its history row is keyed by: formats over a post's fields and the
    # engine's (see `fill` and `ENGINE_FIELDS`). A category may give its own.
    folder: ClassVar[str] = "{category}"
    filename: ClassVar[str] = "{date}_{id}{index}"
    key: ClassVar[str] = "{id}_{num}"
    # For this source's HTTP requests only; the browser always keeps its own.
    user_agent: ClassVar[str | None] = None
    viewport: ClassVar[tuple[int, int]] = (1280, 900)
    browser_args: ClassVar[tuple[str, ...]] = ()
    stealth: ClassVar[bool] = True
    # Files downloading at once, and seconds between two targets (with ±40% jitter).
    concurrency: ClassVar[int] = 4
    pause: ClassVar[float] = 5.0
    wait: ClassVar[Wait] = 1.0
    # `unsupported`, compiled when the class is created.
    _refusals: ClassVar[tuple[tuple[_Pattern, str], ...]] = ()

    def __init_subclass__(cls, **kwargs: Any):
        """Collect and check the class's routes and `unsupported`, generate its forms, and derive its categories.

        Raises:
            TypeError: a pattern on a host outside `domains`, a pattern given twice, a placeholder in
                `unsupported`, or categories both declared as a field and as methods.
        """
        super().__init_subclass__(**kwargs)
        # A subclass keeps its base's routes; one of the same name replaces it in place.
        routes = {route.kind: route for route in cls.routes}
        routes.update((value.kind, value) for value in vars(cls).values() if isinstance(value, Route))
        cls.routes = tuple(routes.values())
        cls._refusals = tuple((_Pattern(pattern), message) for pattern, message in cls.unsupported.items())
        if any(pattern.names for pattern, _ in cls._refusals):
            raise TypeError(f"{cls.__name__}.unsupported: a pattern there captures nothing; use * for any segment")
        domains = getattr(cls, "domains", ())
        seen: set[str] = set()
        for pattern in [pattern for route in cls.routes for pattern in route._patterns] + \
                [pattern for pattern, _ in cls._refusals]:
            if pattern.host is not None and not any(pattern.host == _bare_host(domain) for domain in domains):
                raise TypeError(f"{cls.__name__}: pattern {pattern.text!r} isn't on its domains {domains}")
            # Compared without www. and m., which matching drops anyway.
            key = pattern.text if pattern.host is None else _bare_host(pattern.text.lower())
            if key in seen:
                raise TypeError(f"{cls.__name__}: pattern {pattern.text!r} is given twice")
            seen.add(key)
        if "categories" in vars(cls) or "single_kinds" in vars(cls):
            raise TypeError(f"{cls.__name__}: its categories are its @<route>.category methods; "
                            "declare no categories or single_kinds field")
        names = [category.name for route in cls.routes for category in route.categories]
        cls.categories = tuple(dict.fromkeys(names))
        cls.single_kinds = tuple(route.kind for route in cls.routes if not route.categories)
        if not cls.routes:
            return
        code = getattr(cls, "code", "")
        forms = [form for route in cls.routes for form in route.forms(code)]
        # `ins:<user>` is the form people write most, so the forms after `code:` come first.
        cls.forms = tuple(sorted(forms, key=lambda form: not form.startswith(f"{code}:")))

    def resolve(self, text: str) -> Target:
        """Read one target's text, as people write it, through the routes.

            Instagram().resolve("https://www.instagram.com/p/C1abcDEFghi/")   # Target("ins", "post", "C1abcDEFghi")

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
                    return Target(self.code, route.kind, value)
                break
        if refused is not None:
            raise TargetError(f"{self.code}: {refused[1]}")
        link = None if url is None else text if _is_url(text) else f"https://{text}"
        raise TargetError(f"{self.code}: can't read {text!r}; write it like {', '.join(self.forms)}", url=link)

    def prepare(self, request: httpx.Request, cookies: Mapping[str, str], /) -> httpx.Request:
        """Add headers to and sign a request to the source's own `domains`, before every attempt; never called for a CDN.

            def prepare(self, request, cookies):
                request.headers["x-csrf-token"] = cookies.get("ct0", "")
                return request

        Args:
            request: the request about to be sent; change it in place or return another.
            cookies: the login's cookies as they are now, `{name: value}`, so a rotated one is seen.

        Returns:
            The request to send. It runs again for each retry, so a timestamp in a signature stays fresh.
        """
        return request

    def login(self, login: Login, method: str, /) -> None:
        """Log in by `method`, for a source with its own way (tlg), asking through `login` (a `Login`).

        Cookie sources keep this default, and the engine's `browser` and `file` methods log in.
        A login that fails raises `LoginError`, which `flnd login` reports.

        Args:
            login: the `Login` handle.
            method: one of the source's `login_methods`.
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

    def logged_in(self, site: Site, /) -> bool:
        """Whether a login is stored: by default, one of `needs` is among the saved cookies.

        Args:
            site: the account's `Site`; its `cookies` and `secrets` say what the login kept.
        """
        return not self.needs or any(name in site.cookies for name in self.needs)


def _parse(text: str, domains: tuple[str, ...]) -> _Url | None:
    """`text` as a `_Url` when it's a URL on one of `domains` or their subdomains, with or without a scheme."""
    # Without a scheme, only a slash tells a link (instagram.com/example_user) from a name (example.user).
    if "/" not in text and not _is_url(text):
        return None
    parsed = urlsplit(text if _is_url(text) else f"https://{text}")
    host = (parsed.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in domains):
        return None
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    return _Url(_bare_host(host), tuple(part for part in parsed.path.split("/") if part), query)


def _bare_host(host: str) -> str:
    """A host without the prefixes that are the same site: `www.m.instagram.com` is `instagram.com`."""
    for prefix in SITE_PREFIXES:
        host = host.removeprefix(prefix)
    return host


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def _split(text: str, separator: str, *, once: bool = False) -> list[str]:
    """`text` split at `separator` outside `{…}`, so a placeholder's regex may hold `/`, `?` or `{16,}`.

    With `once`, it splits at the first one only and returns `[before, separator, after]`, or
    `[text, "", ""]` when there's none, as `str.partition` does.
    """
    parts, depth, start = [], 0, 0
    for index, character in enumerate(text):
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
        depth, end = 0, index
        for end in range(index, len(segment)):
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
    return "".join({"text": text, "any": ANY}.get(kind, f"<{text}>") for kind, text, _ in _tokens(segment, segment))


def _assign(instance: Any, **values: Any) -> None:
    """Set a frozen dataclass's fields from its own `__init__`, which `frozen=True` otherwise refuses."""
    for name, value in values.items():
        object.__setattr__(instance, name, value)


def _frozen(headers: Mapping[str, str] | None) -> Mapping[str, str] | None:
    """A read-only copy of `headers`, or None for none."""
    return MappingProxyType(dict(headers)) if headers else None


# The formatter of every format: `get_value` and `format_field` are str.format's own.
_FORMATTER = string.Formatter()


def _fill_segment(segment: str, fields: Mapping[str, Any]) -> str:
    """One path segment of a format, filled in, with the separators an empty field leaves dropped (see `fill`)."""
    # Each piece is [text, whether it's the format's own literal text]; only literals lose separators.
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
