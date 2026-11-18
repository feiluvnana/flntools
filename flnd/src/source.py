"""Source API 1: what a site's source is written against, built in or a plugin.

A source answers one question: what posts does this target have? It declares what it is, and
implements a few well-named hooks; flnd does everything else (the disk, the history, retries,
pacing, concurrency, the early stops, resuming). `src/docs/writing-a-source` walks through one.

    from flnd.source import File, Post, Source, target

    class Example(Source):
        code = "exm"
        domains = ("example.com",)
        api = 1

        @target("exm:{user}", "example.com/{user}", newest_first=True)
        def photos(self, site, user):
            for page in site.pages(f"https://api.example.com/users/{user}/photos", cursor="next"):
                for item in page["items"]:
                    yield Post(item["id"], [File(item["url"])], date=item["time"])

What it holds:
- `Source`, the class a source subclasses, with its declarations (`code`, `domains`, `api`,
  `login`, `pace`, `limits`, the `folder`, `filename` and `key` templates) and its hooks
  (`sign`, `connect`, `date`, `resolve`);
- `target` (routes) and `Route.category` (the parts of a target), which make `Route` and
  `Category`;
- what a listing yields: `Post`, `File`, and `Target` for a target to download as if typed;
- `Site`, the handle every route and category acts through, and `Feed`, a browser feed;
- `Login`, how a source logs in, and `cookies(...)`, the login of the cookie sites;
- `Request`, what the `sign` hook may add headers to;
- `dig`, a JSON path reader; `Range`, `--when`'s days; and the errors a source raises.

Who calls it: the sources, the engine (`flnd.engine.core`, `flnd.engine.job`, `flnd.engine.site`),
the test kit
(`flnd.testing`), and the library, which re-exports `Target`. What it calls: the `flnkit.html`
library (the HTML reader and `dig`), `flnkit.text` (template fields), `flnkit.clock` (reading a
date) and `flnd.errors`.

How a source is used, from its class to its files:
1. Python makes the class. `Source.__init_subclass__` checks every declaration and derives
   `routes`, `forms`, `categories` and `single_kinds` from the `@target` methods, so a mistake
   raises `SourceError` at import, naming the attribute and the fix.
2. A person types a target. The engine finds the source by its code or by a link's domain, and
   `Source.resolve` matches the text against the routes' patterns: the result is a `Target`
   whose `kind` names the route that reads it.
3. Once per run, the engine enters the context manager `Source.connect` gives (for a site with
   its own client) and offers what it yields as `site.client`.
4. The engine calls the route as `route(site, value)`. A route without categories yields
   `Post`s (or `Target`s, for a search); a route with categories returns a context that each
   category, called as `category(site, context)`, turns into `Post`s.
5. For each post, the engine skips what `--when` and the history exclude, calls a lazy post's
   files function, names each `File` from the templates, and downloads it. `Source.sign` sees
   every request to the source's domains before each try; `Source.date` dates a post the
   listing left undated.

The rules a change must keep:
- This module imports nothing of flnd but the library modules and `flnd.errors`: it is what a
  source sees, never the engine (tests/test_layers.py, rule 2).
- A mistake in a source fails when its class is made (`Source.__init_subclass__`), with a
  `SourceError` naming the source, the attribute and the fix. Never halfway through a run.
- One spelling: the source API has no aliases. A name of the old API (`needs`, `prepare`,
  `concurrency`, …) is refused with the new spelling in its message.
- `API` is the major version. A change that breaks a written source raises it.
"""

from __future__ import annotations

import datetime
import inspect
import re
from collections.abc import (
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import nullcontext
from dataclasses import dataclass, field
from types import MappingProxyType, MethodType
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Protocol,
)
from urllib.parse import parse_qs, unquote, urlsplit

from flnd.errors import (
    Blocked,
    Changed,
    Error,
    LoginError,
    NetworkError,
    NotFound,
    SiteError,
    SourceError,
    UsageError,
)
from flnkit import clock, html, text

if TYPE_CHECKING:
    from pathlib import Path

    from flnkit import net

__all__ = [
    "API", "Blocked", "Category", "Changed", "Cookies", "DateLike", "Error", "Feed", "Fetch",
    "File",
    "Login", "LoginError", "NetworkError", "NotFound", "Post", "Range", "Request", "Route", "Site",
    "SiteError", "Source", "SourceError", "Target", "UsageError", "cookies", "dig", "target",
]

# The source API's major version: a source declares the one it was written for (`api = 1`).
API = 1
# Hosts that are the same site as their bare domain: desktop, mobile and web variants.
SITE_PREFIXES = ("www.", "m.", "mobile.", "web.", "mbasic.", "touch.")
# What `{name}` matches without a regex: a user name, as the sites spell them, with one leading `@`
# dropped since people paste names both ways.
NAME = r"[\w.]+"
# Where a generated form shows a `*`: any one segment, which help can't name.
ANY = "…"
# What a source's code looks like: what targets start with (`exm:`), and its cookie slot's name.
CODE = re.compile(r"[a-z][a-z0-9]{1,15}")
# The fields the engine sets for every file, which a template may use beside the source's own
# `fields`: the source's code, the target's value, the category, the post's id, the file's place
# in its post (`num`, from 0), the post's file count, `index` ("" for a one-file post, else
# "_<num>"), the file's extension, its path under the target's folder (what a key is when a
# source gives none), the post's day, and a search's text.
TEMPLATE_FIELDS = (
    "source", "target", "category", "post", "num", "count", "index", "extension", "path",
    "date", "query",
)
# The fields a `Post` may not set: the engine's own, so a source never overwrites one by accident.
RESERVED = frozenset(TEMPLATE_FIELDS) | {"id", "files", "headers"}
# Names of source API 0, refused with how API 1 spells them.
OLD_NAMES = {
    "needs": 'login = cookies("<name>", ...)',
    "requires_login": 'login = cookies("<name>", required=True)',
    "login_methods": "a Login subclass's `methods`",
    "logged_in": "a Login subclass's `check(site)`",
    "concurrency": 'limits = {"<host>": <files at once>}',
    "prepare": "sign(self, request)",
}
# How a browser source may scroll its feeds: like a person, or as fast as the page loads.
SCROLLS = ("human", "fast")

# A file without a URL: `fetch(dest, progress)` writes to `dest`, reporting `progress(done, total)`
# in bytes. It may return a coroutine (tlg's SDK), which the engine runs on the run's loop.
Fetch = Callable[["Path", Callable[..., None]], Any]
# What `Post(date=)` takes: the engine reads every one of these once (`flnkit.clock.day`).
DateLike = datetime.date | int | float | str | None


# ---------------------------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True, init=False)
class Target:
    """One thing to download, as people write it: `Target("ins:example_user")`.

    A source yields `Target(text)` from a search or a post that links elsewhere, and the engine
    downloads it as if it had been typed. The text may be `code:value`, `code?query` or a link,
    on any source's site: a link to another site is handed to that site's source.

    Resolving it (`Source.resolve`) gives the same target with its `kind` set: the name of the
    route that reads it. The engine compares and stores resolved targets; two resolved targets are
    equal when their source, kind and value are.

    Args:
        text: the target, as typed: `ins:example_user`, `mht?romance` or a link.
        kind: the route that reads it, set by resolution; "" for a target not resolved yet.

    Example:
        >>> found = Target("ins:example_user", kind="user")
        >>> found.source, found.value, str(found)
        ('ins', 'example_user', 'ins:example_user')
        >>> Target("https://www.tiktok.com/@example_user").source
        ''
    """

    # The source's code, such as "ins"; "" for a link no source has read yet.
    source: str
    # The route that reads it (`user`, `post`, `video`, `chat`); "" until it is resolved.
    kind: str
    # What it points to: the captured value, which names the target's folder and starts its keys;
    # for a link not resolved yet, the whole link.
    value: str
    # Whether it is a query (`code?query`) rather than `code:value`.
    query: bool = field(default=False, compare=False, repr=False)

    def __init__(self, text: str, *, kind: str = "") -> None:
        source, query, value = _split_target(text)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "query", query)

    def __str__(self) -> str:
        if not self.source:
            return self.value
        # The account a login and `connect` see has no value, and reads as its source's code.
        if not self.value:
            return self.source
        return f"{self.source}{'?' if self.query else ':'}{self.value}"


def _split_target(text: str) -> tuple[str, bool, str]:
    """`text` as (code, whether a query, value); a link or bare text has no code.

    `ins:example_user` is ("ins", False, "example_user"), `mht?romance` is ("mht", True,
    "romance"), and `https://x.com/example_user` is ("", False, the link): `https:` is a scheme,
    not a code, and a link's source is found by resolving it.

    Args:
        text: the target as typed.

    Returns:
        The source's code ("" when there is none), whether it is a query, and the value.
    """
    text = text.strip()
    if "://" in text.split("?", 1)[0].split("/", 1)[0] or text.lower().startswith("http"):
        return "", False, text
    found = re.match(r"([a-z][a-z0-9]*)([:?])", text)
    if found is None:
        return "", False, text
    return found[1], found[2] == "?", text[found.end():]


# ---------------------------------------------------------------------------------------------
# Routes and categories
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Url:
    """A URL on a source's site: host without prefixes, path parts, and query."""

    host: str
    parts: tuple[str, ...]
    query: Mapping[str, str]


class _Pattern:
    """One compiled pattern: a link pattern matches a `_Url`, a code pattern the text after `code:`.

    Args:
        text: the pattern as the route gives it, such as `example.com/{user}` or `exm:{user}`.

    Raises:
        SourceError: the pattern is malformed (see `_tokens` and `_compile`).
    """

    def __init__(self, text: str):
        self.text = text
        # A code pattern starts with the source's code (`exm:{user}`, `exm?{words}`), which
        # `_check_patterns` compares with the source's; matching reads the text after it.
        prefix = None if _is_url(text) else re.match(r"[a-z][a-z0-9]*(?=[:?])", text)
        self.code = prefix[0] if prefix else None
        body = text[prefix.end():] if prefix else text
        body = body[1:] if body.startswith(":") else body
        # The pattern without its code, as duplicates are compared.
        self.body = body
        path, mark, query = _split(body, "?", once=True)
        segments = [segment for segment in _split(path, "/") if segment]
        first = segments[0] if segments else ""
        # The first segment is a host when it has a dot and no placeholder; else it's a code
        # pattern.
        self.host = (
            _bare_host(first.lower())
            if "." in first and not set(first) & set("{}*")
            else None
        )
        self.is_query = self.host is None and not segments and mark == "?"
        if self.host is None and mark and not self.is_query:
            raise SourceError(f"pattern {text!r}: only a link pattern can name a query")
        self.names: list[str] = []
        if self.is_query:
            self.segments: list[str] = []
            self.keys: list[list[str]] = []
            self.regexes: list[re.Pattern[str]] = []
            self.query = [("", _compile(query, text, self.names))]
            self.literals = 0
            return
        self.segments = segments[1:] if self.host is not None else ["/".join(segments)]
        self.keys = [_split(pair, "=", once=True) for pair in _split(query, "&")] if mark else []
        if any(not key or not value for key, _, value in self.keys):
            raise SourceError(f"pattern {text!r}: write a query as ?key={{name}}")
        # Placeholder names in order: a route's pattern has one, an `unsupported` pattern none.
        self.regexes = [_compile(segment, text, self.names) for segment in self.segments]
        self.query = [(key, _compile(value, text, self.names)) for key, _, value in self.keys]
        # How specific the pattern is: literal segments, the host being one. A code pattern has
        # none.
        self.literals = (self.host is not None) + sum(
            1 for s in self.segments if self.host is not None and not set(s) & set("{}*")
        )

    def match(self, text: str, url: _Url | None) -> str | None:
        """The captured value when `text` or `url` matches; "" for a pattern with no placeholder.

        A query pattern matches only text starting with `?`; a code pattern only text that is
        not a link; a link pattern only a link on its host whose first path parts match its
        segments, later parts allowed, so `example.com/{user}` also reads
        `example.com/example_user/tagged`.

        Args:
            text: the target's text after `code:` (or `?query` for a query), or a bare link.
            url: `text` parsed as a link on the source's domains; None when it isn't one.

        Returns:
            The first placeholder's capture; "" when the pattern has none (an `unsupported`
            one); None when it doesn't match.
        """
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
        """The pattern as people write it, each placeholder in angle brackets.

        Args:
            code: the source's code, which a code or query pattern's form starts with.

        Returns:
            `exm:<user>`, `exm?<words>` or `https://www.example.com/<user>/`.
        """
        if self.is_query:
            return f"{code}?<{self.names[0]}>"
        path = "/".join(_shown(segment) for segment in self.segments)
        if self.host is None:
            return f"{code}:{path}"
        # Help shows what a browser's address bar does: www. on a bare domain, a closing slash on
        # a folder.
        host = f"www.{self.host}" if self.host.count(".") == 1 else self.host
        query = "&".join(f"{key}={_shown(value)}" for key, _, value in self.keys)
        slash = "" if query or "." in path.rsplit("/", 1)[-1] else "/"
        return f"https://{host}/{path}{slash if path else ''}" + (f"?{query}" if query else "")


class Category:
    """One part of a route's target, made by `@<route>.category`: `posts`, `stories`, `reels`.

    Its method takes `(self, site, context)`: `context` is what the route returned. It yields
    `Post`s, and may be `def` or `async def`.

    Args:
        function: the method.
        newest_first: the listing comes newest first, so a sync may stop at old posts.
        folder: the folder template of its files, instead of the source's `folder`.
        key: the history key template of its files, instead of the source's `key`.

    Raises:
        SourceError: the method doesn't take `(self, site, context)`.

    Example:
        >>> Category(lambda self, site, profile: [], newest_first=True).newest_first
        True
    """

    def __init__(self, function: Callable[..., Any], *, newest_first: bool = False,
                 folder: str | None = None, key: str | None = None):
        self.function = function
        # The method's name: what `--only` and help write, and the attribute the engine calls.
        self.name = function.__name__
        self.newest_first = newest_first
        self.folder, self.key = folder, key
        parameters = list(inspect.signature(function).parameters)
        if len(parameters) != 3:
            raise SourceError(
                f"category {self.name}: take (self, site, context), not ({', '.join(parameters)})"
            )

    def __repr__(self) -> str:
        return f"Category({self.name!r}, newest_first={self.newest_first})"


class Route:
    """A method that reads one kind of target, made by `@target(...)`; its name is the kind.

    A route without categories yields the target's posts itself, or yields `Target`s (a search).
    A route with categories returns a context instead, which each of its categories receives;
    `@route.category` adds one.

    Args:
        function: the method, taking `(self, site, value)`.
        patterns: what it reads: link patterns (`example.com/{user}`) and code patterns
            (`exm:{user}`, `exm?{words}`), each with one placeholder named like the method's
            value parameter.
        newest_first: its own listing (a route without categories) comes newest first.
        shown: the form help shows instead of the first pattern's, for a route whose targets
            `resolve` reads (tlg's chats); None shows the patterns.
        discovers: it yields targets, not posts; a query route always does. `--only` then
            applies to the targets it finds.
        folder: the folder template of its files, instead of the source's `folder`.

    Raises:
        SourceError: a pattern is malformed, has other than one placeholder, or names it
            differently from the method's value parameter; or the method takes other parameters.

    Example:
        >>> route = Route(lambda self, site, user: None, ("exm:{user}", "example.com/{user}"))
        >>> route.forms("exm")
        ['exm:<user>', 'https://www.example.com/<user>/']
    """

    def __init__(
        self, function: Callable[..., Any], patterns: tuple[str, ...], *,
        newest_first: bool = False, shown: str | None = None, discovers: bool = False,
        folder: str | None = None,
    ):
        self.function = function
        self.kind = function.__name__
        self.patterns = patterns
        self.newest_first = newest_first
        self.shown = shown
        self.folder = folder
        self._patterns = [_Pattern(pattern) for pattern in patterns]
        self.is_query = any(pattern.is_query for pattern in self._patterns)
        self.discovers = self.is_query or discovers
        self._categories: list[Category] = []
        parameters = list(inspect.signature(function).parameters)
        if len(parameters) != 3:
            raise SourceError(
                f"route {self.kind}: take (self, site, value), not ({', '.join(parameters)})"
            )
        for pattern in self._patterns:
            if len(pattern.names) != 1:
                # One value is the target's identity: its folder and its history keys.
                raise SourceError(
                    f"route {self.kind}: pattern {pattern.text!r} needs exactly one placeholder, "
                    f"not {len(pattern.names)}"
                )
            if pattern.names[0] != parameters[2]:
                raise SourceError(
                    f"route {self.kind}: pattern {pattern.text!r} captures {pattern.names[0]!r}, "
                    f"but the method's value parameter is {parameters[2]!r}"
                )

    @property
    def categories(self) -> tuple[Category, ...]:
        """The route's categories, in declaration order; empty if it yields posts itself."""
        return tuple(self._categories)

    def category(self, function: Callable[..., Any] | None = None, /, *, newest_first: bool = False,
                 folder: str | None = None, key: str | None = None) -> Any:
        """Mark a method as one of this route's categories, with or without options.

            @profile.category
            def stories(self, site, profile): ...

            @profile.category(newest_first=True)
            def posts(self, site, profile): ...

        Args:
            function: the method, when used without options.
            newest_first: the listing comes newest first, so a sync may stop at old posts.
            folder: the folder template of its files, instead of the source's `folder`.
            key: the history key template of its files, instead of the source's `key`.

        Returns:
            The method itself, so the class keeps it as a plain method.

        Raises:
            SourceError: a category of the same name is declared twice.

        Example:
            >>> class Example(Source):
            ...     code, domains, api = "exm", ("example.com",), 1
            ...     @target("exm:{user}")
            ...     def profile(self, site, user):
            ...         return {"user": user}
            ...     @profile.category(newest_first=True)
            ...     def posts(self, site, profile):
            ...         yield Post("p1")
            >>> Example.profile.categories
            (Category('posts', newest_first=True),)
            >>> Example.categories
            ('posts',)
        """
        def register(method: Callable[..., Any]) -> Callable[..., Any]:
            made = Category(method, newest_first=newest_first, folder=folder, key=key)
            if any(known.name == made.name for known in self._categories):
                raise SourceError(f"route {self.kind}: category {made.name!r} is declared twice")
            self._categories.append(made)
            return method

        return register(function) if function is not None else register

    def __set_name__(self, owner: type, name: str) -> None:
        """Take the attribute's name as the kind: the method's, unless `type()` built the class."""
        self.kind = name

    def __get__(self, instance: object, owner: type | None = None) -> Any:
        """The route itself on the class; the plain method, bound, on an instance."""
        return self if instance is None else MethodType(self.function, instance)

    def forms(self, code: str) -> list[str]:
        """Each pattern as people write it, for source `code`; `shown` replaces the first.

        Args:
            code: the source's code.

        Returns:
            The forms, such as `["exm:<user>", "https://www.example.com/<user>/"]`.

        Example:
            >>> route = Route(lambda self, site, id: None, ("example.com/p/{id:[A-Za-z0-9]+}",))
            >>> route.forms("exm")
            ['https://www.example.com/p/<id>/']
            >>> Route(lambda self, site, chat: None, ("exm:{chat:.+}",), shown="exm:<title>"
            ...       ).forms("exm")
            ['exm:<title>']
        """
        forms = [pattern.form(code) for pattern in self._patterns]
        if self.shown is not None:
            forms[:1] = [self.shown]
        return forms


def target(*patterns: str, newest_first: bool = False, shown: str | None = None,
           discovers: bool = False, folder: str | None = None) -> Callable[..., Route]:
    """Mark a method as a route: the targets it reads, as patterns; its name is their kind.

        @target("exm:{user}", "example.com/{user}", newest_first=True)
        def photos(self, site, user): ...

    Args:
        *patterns: link patterns (`example.com/{user}`, `example.com/p/{id:\\w+}`) and code
            patterns (`exm:{user}`, `exm?{words}`). A placeholder without a regex matches a
            user name, a leading `@` dropped; `*` matches any one path segment.
        newest_first: the route yields posts newest first, so a sync may stop at old posts.
            Without it a sync reads the whole listing: slower, never wrong.
        shown: the form help shows for a route `Source.resolve` reads instead of a pattern.
        discovers: the route yields targets rather than posts (a query route always does).
        folder: the folder template of its files, instead of the source's `folder`.

    Returns:
        A decorator making the `Route`.

    Example:
        >>> class Example(Source):
        ...     code, domains, api = "exm", ("example.com",), 1
        ...     @target("exm:{user}", "example.com/{user}", newest_first=True)
        ...     def photos(self, site, user):
        ...         yield Post("p1", [File(f"https://cdn.example.com/{user}/p1.jpg")])
        >>> Example.photos.kind, Example.photos.newest_first
        ('photos', True)
        >>> Example.forms
        ('exm:<user>', 'https://www.example.com/<user>/')
    """
    return lambda function: Route(
        function, patterns, newest_first=newest_first, shown=shown, discovers=discovers,
        folder=folder,
    )


# ---------------------------------------------------------------------------------------------
# What a listing yields
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True, init=False)
class File:
    """One file of a post: a URL (or mirrors, or a fetch function), and how to download it.

        File("https://cdn.example.com/a.jpg")
        File([mirror_a, mirror_b], name="loi-5930.7z", password=key)  # extracted beside it
        File(video_url, audio=audio_url)                            # joined into one video
        File.text("info.md", "# Title\\n")                          # written, not downloaded

    The engine names it from the source's templates, gives it the extension its content calls
    for, and numbers it by its place in the post.

    Args:
        url: a URL, mirror URLs tried in order, or a `Fetch` function `fetch(dest, progress)`
            for media without a URL (Telegram's).
        audio: the URL of a separate audio track, downloaded at once and joined with the video
            by ffmpeg without re-encoding; None when the video has its own sound.
        name: the file's name as the site gives it, extension included, which the templates
            must not change (an archive's, a document's). None names it by `filename`.
        size: its size in bytes, when the site says it; None when unknown.
        password: an archive's password, when the site gives one: the archive is extracted with
            it once downloaded. None for none.

    Raises:
        SourceError: `url` is none of those.

    Example:
        >>> File("https://cdn.example.com/a.jpg", size=2048).size
        2048
        >>> File(["https://a.example.com/x.7z", "https://b.example.com/x.7z"]).url[1]
        'https://b.example.com/x.7z'
    """

    # A URL, mirrors tried in order, or a `Fetch` function; None for a text file.
    url: str | tuple[str, ...] | Fetch | None
    # A video's separate audio track, or None.
    audio: str | None
    # The name as the site gives it, or None for the templates' name.
    name: str | None
    # The size in bytes, or None.
    size: int | None
    # A text file's text, written as UTF-8; None for a file that is downloaded.
    content: str | None
    # An archive's password, or None.
    password: str | None

    def __init__(self, url: str | Sequence[str] | Fetch, *, audio: str | None = None,
                 name: str | None = None, size: int | None = None,
                 password: str | None = None):
        if isinstance(url, str) or callable(url):
            sources: Any = url
        elif isinstance(url, Sequence) and url and all(isinstance(mirror, str) for mirror in url):
            sources = tuple(url)
        else:
            raise SourceError(f"File takes a URL, mirror URLs or a fetch function, not {url!r}")
        _assign(self, url=sources, audio=audio, name=name, size=size, content=None,
                password=password)

    @classmethod
    def text(cls, name: str, text: str) -> File:
        """A text file the source writes itself, such as `info.md`; saved atomically.

        It has no history row, so it is written again each time its post is downloaded.

        Args:
            name: the file's name in its post's folder, extension included.
            text: its text, saved as UTF-8 with `\\n` line endings on every platform.

        Returns:
            The file.

        Example:
            >>> File.text("info.md", "# Title\\n").content
            '# Title\\n'
        """
        made = cls.__new__(cls)
        _assign(made, url=None, audio=None, name=name, size=None, content=text, password=None)
        return made

    @classmethod
    def dash(cls, manifest: str | None) -> File | None:
        """The best video and audio of a single-file DASH manifest, as one `File(video, audio=…)`.

        Meta's and others' videos come as a DASH manifest (an MPD, XML) listing each stream at
        each quality. The best video is the tallest (then the highest bitrate) and the best audio
        the highest bitrate; the engine downloads both and joins them without re-encoding.

        Args:
            manifest: the MPD's XML text; None or "" for none.

        Returns:
            The file; None when there is no manifest, it isn't XML, it streams in segments
            (`SegmentTemplate`, `SegmentList`), or it lacks a video or an audio stream with an
            https URL. A source then falls back to the site's progressive URL.

        Example:
            >>> File.dash("not xml") is None
            True
        """
        streams = _best_streams(manifest)
        if streams is None:
            return None
        video, audio = streams
        # The joined file takes the video's extension: a WebM pair stays WebM, the rest is MP4.
        return cls(video, audio=audio)


@dataclass(frozen=True, init=False)
class Post:
    """One post a listing yields: its id, its files, its date, and fields for the templates.

        Post(item["code"], [File(url) for url in urls], date=item["taken_at"], album=title)
        Post(code, lambda: files_of(site, code))      # listed only when the post isn't saved yet

    Args:
        id: the post's id as the site shows it; `{post}` in the templates, and the start of its
            files' history keys.
        files: the post's files in order, or a function returning them, called only for a post
            that passes `--when` and isn't saved yet, so a source never fetches the pages of a
            post flnd already has.
        date: when it was posted: a `date`, a `datetime`, Unix seconds or milliseconds, or an ISO
            8601 string. The engine reads it once; None for an undated post, which `--when`
            keeps.
        headers: headers for downloading each of its files, such as a CDN's Referer.
        **fields: the site's own fields: `caption`, `author`, `title`, `tags`, `album`, … They
            feed the templates (when the source lists them in `fields`), the sidecars and
            search.

    Raises:
        SourceError: a field is one the engine sets (`post`, `category`, `num`, …).

    Example:
        >>> post = Post(9001, [File("https://cdn.example.com/a.jpg")], album="Trip")
        >>> post.id, post.fields["album"], post.lazy
        ('9001', 'Trip', False)
    """

    # The id, as text.
    id: str
    # The files, or a function giving them.
    files: tuple[File, ...] | Callable[[], Iterable[File]]
    # The date as given; the engine reads it.
    date: DateLike
    # Headers for downloading its files, or None.
    headers: Mapping[str, str] | None
    # The site's fields, read-only.
    fields: Mapping[str, Any]
    # The category the post was yielded by, set by the engine; "" for a route's own posts.
    category: str

    def __init__(self, id: str | int, files: Sequence[File] | Callable[[], Iterable[File]] = (), *,
                 date: DateLike = None, headers: Mapping[str, str] | None = None, **fields: Any):
        reserved = sorted(RESERVED & fields.keys())
        if reserved:
            are = "is a field" if len(reserved) == 1 else "are fields"
            raise SourceError(
                f"Post: {', '.join(reserved)} {are} the engine sets; "
                "name the site's field otherwise"
            )
        _assign(self, id=str(id), files=files if callable(files) else tuple(files), date=date,
                headers=_frozen(headers), fields=MappingProxyType(dict(fields)), category="")

    def replace(self, **changes: Any) -> Post:
        """A copy with `id`, `files`, `date`, `headers` or fields changed.

        Args:
            **changes: the new values; a name that isn't one of the four is a field.

        Returns:
            The copy, in the same category.

        Example:
            >>> Post("1", album="a").replace(album="b").fields["album"]
            'b'
        """
        made = Post(
            changes.pop("id", self.id), changes.pop("files", self.files),
            date=changes.pop("date", self.date), headers=changes.pop("headers", self.headers),
            **{**self.fields, **changes},
        )
        _assign(made, category=self.category)
        return made

    def filed(self, category: str) -> Post:
        """A copy filed under `category`, which only the engine sets, as it lists the category.

        A source never calls it: a post's category is whichever category method yielded it.
        The engine files each post as it reads the listing, so `{category}` in the templates
        and the history know where it came from.

        Args:
            category: the category's name, such as `posts`; "" for a route's own posts.

        Returns:
            The copy; the post itself is left as it was.

        Example:
            >>> post = Post("p1")
            >>> post.filed("stories").category, post.category
            ('stories', '')
        """
        made = self.replace()
        _assign(made, category=category)
        return made

    @property
    def lazy(self) -> bool:
        """Whether its files are a function, called only once the post is known to be wanted."""
        return callable(self.files)


@dataclass(frozen=True)
class Range:
    """`--when`'s two days, both included; None is no limit on that side.

    A source that can ask its site for a range (Telegram's `offset_date`) reads `site.time`;
    the engine filters every post by it anyway.

    Example:
        >>> from datetime import date
        >>> span = Range(since=date(2026, 1, 1))
        >>> date(2026, 3, 1) in span, "2025-12-31" in span, None in span
        (True, False, True)
    """

    # The first day kept, or None.
    since: datetime.date | None = None
    # The last day kept, or None.
    until: datetime.date | None = None

    def __contains__(self, value: object) -> bool:
        """Whether a date is in range; an undated post (None, or text that isn't a date) is."""
        found = clock.day(value)
        if found is None:
            return True
        return not ((self.since and found < self.since) or (self.until and found > self.until))


@dataclass
class Request:
    """A request to the source's own domains, as `Source.sign` sees it before each try.

    Args:
        method: "GET", "POST", …
        url: the whole URL, its query included.
        headers: the request's headers; `sign` adds to or changes them.
        body: the body's bytes, or None.
        cookies: the login's cookies now, `{name: value}`, read-only.

    Example:
        >>> request = Request("GET", "https://api.example.com/me", {}, None, {"token": "t"})
        >>> request.headers["authorization"] = "Bearer " + request.cookies["token"]
    """

    method: str
    url: str
    headers: MutableMapping[str, str]
    body: bytes | None
    cookies: Mapping[str, str]


# ---------------------------------------------------------------------------------------------
# Logging in
# ---------------------------------------------------------------------------------------------

class Login:
    """How a source logs in: assigned once, as `login = cookies("session")` or `login = MyLogin()`.

    The cookie sites use `cookies(...)`. A site with its own flow (Telegram's QR code and phone
    number) subclasses `Login`, sets `methods`, and implements `start` and `check`:

        class TelegramLogin(Login):
            methods = ("qr", "phone")

            def start(self, site, how):
                code = site.ask("Code", description="Sent to your phone")
                site.secrets["session"] = ...

            def check(self, site):
                return "session" in site.secrets

    `start` runs only during `flnd login`; `check` runs before every download, and reads only
    what the login stored, so it never costs a request.

    Args:
        required: whether the source can't list anything without a login; a source that can
            (public profiles) warns and goes on.

    Example:
        >>> from flnd.testing import FakeSite
        >>> class CodeLogin(Login):
        ...     methods = ("code",)
        ...     def start(self, site, how):
        ...         site.secrets["session"] = site.ask("Code", secret=True)
        ...     def check(self, site):
        ...         return "session" in site.secrets
        >>> login = CodeLogin(required=True)
        >>> login.methods, login.required, login.check(FakeSite())
        (('code',), True, False)
    """

    # The ways `flnd login <code> <how>` accepts.
    methods: ClassVar[tuple[str, ...]] = ()
    # The cookies, any one of which proves a login; empty for a login that keeps none.
    names: tuple[str, ...] = ()

    def __init__(self, *, required: bool = True) -> None:
        self.required = required

    def start(self, site: Site, how: str, /) -> None:
        """Log in by `how`, asking through `site` (`site.ask`), and keep what it got.

        Args:
            site: the source's site during `flnd login`: `ask` and `interactive`, `secrets`,
                `state`, `cookies`, `info`, `warn`, `pause`, and `storage(url)`, a web app's
                local storage read from a copy of Chrome's default profile.
            how: one of `methods`.

        Raises:
            LoginError: the site refused, or the person gave up.

        Example:
            >>> class Code(Login):
            ...     methods = ("phone",)
            ...     def start(self, site, how):
            ...         site.secrets["session"] = site.ask("Code")
            >>> Code.methods
            ('phone',)
        """
        raise NotImplementedError(f"{type(self).__name__}.start")

    def check(self, site: Site, /) -> bool:
        """Whether a login is kept, from what `site.cookies` and `site.secrets` hold.

        Args:
            site: the source's site.

        Returns:
            True when logged in. It reads what is stored; it never asks the site.

        Example:
            >>> class Kept:
            ...     secrets = {}
            >>> Login().check(Kept())
            True
        """
        return True


class Cookies(Login):
    """The login of a cookie site, made by `cookies(...)`: logged in when one of `names` is there.

    `flnd login <code> chrome` reads the cookies from a browser, and `flnd login <code> <file>`
    from a cookies.txt; either is stored encrypted, and sent with every request to the source's
    domains.

    Args:
        *names: the cookies, any one of which proves a login, such as `sessionid`.
        required: whether nothing can be listed without it.

    Example:
        >>> login = cookies("sessionid", "sid_tt")
        >>> login.names, login.methods, login.required
        (('sessionid', 'sid_tt'), ('browser', 'file'), False)
    """

    methods: ClassVar[tuple[str, ...]] = ("browser", "file")

    def __init__(self, *names: str, required: bool = False) -> None:
        super().__init__(required=required)
        if not names or not all(isinstance(name, str) and name for name in names):
            raise SourceError('cookies() takes the cookie names that prove a login: '
                              'cookies("sessionid")')
        self.names = tuple(names)

    def start(self, site: Site, how: str, /) -> None:
        """Cookies are read by the engine (`flnd login <code> chrome`), never by the source.

        Args:
            site: the source's site during `flnd login`.
            how: the way asked for.

        Raises:
            UsageError: always: the engine reads the cookies itself.

        Example:
            >>> cookies("sid").start(None, "phone")
            Traceback (most recent call last):
            flnd.errors.UsageError: phone: cookies are read from a browser or a cookies.txt file
        """
        raise UsageError(f"{how}: cookies are read from a browser or a cookies.txt file")

    def check(self, site: Site, /) -> bool:
        """Whether one of `names` is among the login's cookies.

        Args:
            site: the source's site; its `cookies` are read.

        Returns:
            True when one of the cookies is there.

        Example:
            >>> class Jar:
            ...     cookies = {"sid": "1"}
            >>> cookies("sid").check(Jar())
            True
        """
        return any(name in site.cookies for name in self.names)


def cookies(*names: str, required: bool = False) -> Cookies:
    """The login of a cookie site: logged in when one of these cookies is there.

    Args:
        *names: the cookies, any one of which proves a login.
        required: whether the source can't list anything without them; by default a source
            without a login warns and reads what the site shows the public.

    Returns:
        The `Cookies` login, to assign as `login = cookies("sessionid")`.

    Example:
        >>> cookies("auth_token").names
        ('auth_token',)
    """
    return Cookies(*names, required=required)


# ---------------------------------------------------------------------------------------------
# The site
# ---------------------------------------------------------------------------------------------

class Feed(Protocol):
    """What `Site.feed` returns: the JSON of matching responses, as they arrive.

        with site.feed("/graphql", scroll=True) as pages:
            site.open(url)
            for page in pages:
                ...

    Iterate it for each body, oldest first. Leaving `with`, or the end of the iteration, stops it.

    Example:
        >>> from flnd.testing import FakeSite
        >>> site = FakeSite(feeds=[("https://www.example.com/graphql?page=1", {"items": [1]}),
        ...                        ("https://www.example.com/graphql?page=2", {"items": [2]})])
        >>> with site.feed("/graphql", scroll=True) as pages:
        ...     [page["items"] for page in pages]
        [[1], [2]]
    """

    def __iter__(self) -> Iterator[Any]:
        """The bodies, each a parsed JSON value."""
        ...

    def __next__(self) -> Any:
        """The next body; with `scroll`, scrolling the tab until one comes or the feed ends."""
        ...

    def __enter__(self) -> Feed:
        """The feed itself."""
        ...

    def __exit__(self, *exc_info: object) -> None:
        """`close`."""
        ...

    def close(self) -> None:
        """Stop watching; a second call does nothing.

        Example:
            >>> from flnd.testing import FakeSite
            >>> pages = FakeSite().feed("/graphql")
            >>> pages.close()
            >>> pages.close()
        """
        ...


class Site(Protocol):
    """The handle every route and category acts through, for one target.

        data = site.fetch(f"{API}/users/{user}", params={"count": 50}).json
        for page in site.pages(f"{API}/users/{user}/posts", cursor="next"):
            ...
        if site.open(f"https://www.example.com/{user}/"):
            title = site.document().first("h1").text

    The engine's is `flnd.engine.site.TargetSite`; tests use `flnd.testing.FakeSite`. Every request
    to the source's own `domains` waits its turn under the source's `pace`, and a rate limit or a
    5xx is waited out and tried again here, so a source never sleeps or retries itself. A login wall
    (a 401 or 403 from the source's own domains) raises `LoginError`.

    The browser verbs (`open`, `wait`, `scroll`, `feed`, `warm`, `document`, `evaluate`,
    `click`) drive the target's tab in the run's one browser, launched the first time a target
    needs it. They are for a plain `def` route or category; an `async def` one awaits `site.tab`.

    Example:
        >>> from flnd.testing import FakeSite
        >>> site = FakeSite(Target("exm:example_user", kind="user"),
        ...                 json={"https://api.example.com/users/example_user": {"id": "42"}})
        >>> site.fetch("https://api.example.com/users/example_user").json["id"]
        '42'
        >>> site.target.value
        'example_user'
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
        """`--when`, for a source that can ask its site for a range; the engine filters too."""
        ...

    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              check: bool = True, in_page: bool = False, **request: Any) -> net.Response:
        """Make one request, paced and retried, and return its response.

            items = site.fetch(API, params={"cursor": cursor}).json
            profile = site.fetch(f"{API}/users/{user}", check=False)   # read profile.ok

        Args:
            url: the URL.
            method: the HTTP method.
            headers: headers of its own, besides the client's and the `sign` hook's.
            check: raise for a refusal; False returns the refusal's response to read.
            in_page: send it from the target's browser tab, as the page's own script would,
                for an API that only answers the site's pages.
            **request: `params` (the query, as a mapping) and `body` (bytes, text or JSON).

        Returns:
            The response: `.status`, `.ok`, `.headers`, `.text`, `.json`.

        Raises:
            NotFound: a 404 or 410.
            LoginError: a 401 or 403 from the source's own domains: a login wall.
            Blocked: a 429 still refused after its waits.
            SiteError: another refusal, with its `status` and `response`.
            NetworkError: no answer, after the tries.

        Example:
            >>> from flnd.testing import FakeSite
            >>> site = FakeSite(json={"https://api.example.com/users/example_user": {"id": "42"}})
            >>> site.fetch("https://api.example.com/users/example_user").json
            {'id': '42'}
            >>> site.fetch("https://api.example.com/users/nobody", check=False).ok
            False
        """
        ...

    def pages(self, url: str, *, cursor: str | Callable[[Any], Any],
              params: Mapping[str, Any] | None = None,
              send: str | Callable[[Any], Mapping[str, Any]] = "cursor") -> Iterator[Any]:
        """Each page of a paged JSON listing, the engine keeping and resuming its cursor.

            for page in site.pages(f"{API}/posts", params={"count": 50}, cursor="next"):
                yield from posts_of(page)

        The first request has `params` alone. Each next one adds the cursor the last page gave.
        The listing ends on a missing, empty or repeated cursor, or an empty page. When a run
        is cut short, the engine keeps the cursor of the page it was reading, and the next run of
        the same category starts there (only the first `pages` of a category resumes).

        Args:
            url: the listing's URL.
            cursor: where a page says what comes next: a `dig` path (`"next"`,
                `"data.user.media.page_info.end_cursor"`), or a function from the page to the
                cursor, returning None at the end (for a site whose last page still has one).
            params: the query every request sends.
            send: where the cursor goes in the next request: a query parameter's name, or a
                function from the cursor to more params (a GraphQL `variables` JSON).

        Returns:
            The pages, each the response's parsed JSON.

        Raises:
            The same as `fetch`. A saved cursor the site refuses (a 4xx) is dropped, and the
            listing starts again from the newest.

        Example:
            >>> import inspect
            >>> list(inspect.signature(Site.pages).parameters)
            ['self', 'url', 'cursor', 'params', 'send']
        """
        ...

    def open(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        """Load `url` in the target's tab, paced; False when it didn't load or answered 400 or more.

        Args:
            url: the page.
            idle: also wait until the network has been quiet for a moment, not only for `load`.
            timeout: seconds before the page counts as not loaded.

        Returns:
            Whether it loaded. A page that didn't is a warning line, not an error, and the tab
            stays where it was. A 429 is waited out and the page loaded again, a few times.

        Example:
            >>> from flnd.testing import FakeSite
            >>> site = FakeSite(pages={"https://www.example.com/example_user/": "<h1>Hi</h1>"})
            >>> site.open("https://www.example.com/example_user/"), site.url
            (True, 'https://www.example.com/example_user/')
        """
        ...

    @property
    def url(self) -> str:
        """The tab's URL now, after redirects; "about:blank" before a page was opened."""
        ...

    def wait(self, css: str | None = None, *, url: str | re.Pattern[str] | Callable[[str], bool]
             | None = None, idle: bool = False, timeout: float = 30.0) -> None:
        """Wait until `css` matches an element, `url` matches the tab's URL, and (`idle`) quiet.

        Args:
            css: a CSS selector; waits for a visible element to match it. None doesn't wait.
            url: a glob, a compiled regex or a predicate over the URL. None doesn't wait.
            idle: also wait until the network has been quiet for 500 ms. With neither `css` nor
                `url`, False waits for the `load` event.
            timeout: seconds for every condition together.

        Raises:
            NetworkError: `timeout` passed first; the message names what was waited for.

        Example:
            >>> from flnd.testing import FakeSite
            >>> site = FakeSite(pages={"https://www.example.com/": "<main>posts</main>"})
            >>> site.open("https://www.example.com/")
            True
            >>> site.wait("main", url="**/www.example.com/**", timeout=10)
        """
        ...

    def scroll(self, *, until: Callable[[], bool] | None = None, idle: float = 2.0) -> int:
        """Scroll the tab until its feed ends or `until()` is True, in the source's `scroll` style.

        Args:
            until: asked before each scroll; True stops.
            idle: seconds the bottom may stay quiet, after a whole wait brought nothing, before
                the feed counts as ended.

        Returns:
            How many scrolls it made.

        Example:
            >>> from flnd.testing import FakeSite
            >>> seen = []
            >>> FakeSite().scroll(until=lambda: len(seen) > 10)
            0
        """
        ...

    def feed(self, match: str | re.Pattern[str] | Callable[[str], bool], *, scroll: bool = False,
             lines: bool = False,
             cursor: Callable[[Any], tuple[bool, str | None] | None] | None = None) -> Feed:
        """Watch the tab for responses whose URL matches, and iterate their JSON.

        Open it before the page that sends the responses, or the first ones are missed. A
        scrolling feed scrolls only when no body is waiting, and ends as soon as the site says
        there is no more: `cursor` read a page with no next one, or the same cursor came back.
        Without `cursor` it ends when the page's bottom stays quiet for about 5 s.

        Args:
            match: a substring of the URL, a compiled regex searched in it, or a predicate.
            scroll: when none is waiting, scroll the tab for more, and end when the feed ends.
                Without it, the feed waits once for the network to go quiet, and ends.
            lines: each body holds one JSON value per line (NDJSON).
            cursor: called with each parsed value: `(more, cursor)` for one of the feed's
                pages, None for a value that says nothing about paging.

        Returns:
            The `Feed`, to iterate and close.

        Example:
            >>> from flnd.testing import FakeSite
            >>> site = FakeSite(feeds=[("https://www.example.com/api/posts", {"more": False})])
            >>> with site.feed("/api/posts", scroll=True,
            ...                cursor=lambda page: (page["more"], None)) as pages:
            ...     site.open("https://www.example.com/example_user/")
            ...     list(pages)
            True
            [{'more': False}]
        """
        ...

    def warm(self, urls: Iterable[str]) -> None:
        """Start loading pages the source will open next, in background tabs.

        `open` of one of them then switches to its tab at once, its responses kept. Up to six
        load at a time; a site without a browser ignores the call.

        Args:
            urls: the pages, in the order they'll be opened.

        Example:
            >>> import inspect
            >>> list(inspect.signature(Site.warm).parameters)
            ['self', 'urls']
        """
        ...

    def document(self) -> Any:
        """The tab's page now, parsed, as `html` parses it.

        Returns:
            The page, with `css`, `first`, `script`, `blocks`, `text` and `visible`; see `html`.

        Example:
            >>> from flnd.testing import FakeSite
            >>> site = FakeSite(pages={"https://www.example.com/": "<h1>Hi</h1>"})
            >>> site.open("https://www.example.com/")
            True
            >>> site.document().first("h1").text
            'Hi'
        """
        ...

    def evaluate(self, js: str, *, arg: Any = None) -> Any:
        """Run JavaScript in the tab and return its value (JSON values only).

        Args:
            js: an expression, or a function that takes `arg`.
            arg: the function's argument.

        Returns:
            The value, as JSON carries it.

        Example:
            >>> from flnd.testing import FakeSite
            >>> FakeSite().evaluate("(n) => n + 1", arg=1) is None   # a fake page runs nothing
            True
        """
        ...

    def click(self, css: str, *, timeout: float = 10.0) -> None:
        """Click the first element `css` matches, waiting for it to be clickable.

        Args:
            css: a CSS selector.
            timeout: seconds before giving up.

        Raises:
            NetworkError: nothing clickable matched within `timeout`, or the tab closed.

        Example:
            >>> from flnd.testing import FakeSite
            >>> FakeSite().click("button.more", timeout=5)
        """
        ...

    def handoff(
        self, *, until: Callable[[], bool], message: str, timeout: float | None = None
    ) -> bool:
        """Show `message` and wait for a person to do a step in the window, such as a captcha.

        Args:
            until: returns True once the step is done; polled about once a second.
            message: what the person should do.
            timeout: seconds before giving up; None waits as long as it takes.

        Returns:
            True when `until` came true; False when `timeout` passed first.

        Example:
            >>> from flnd.testing import FakeSite
            >>> FakeSite().handoff(until=lambda: True, message="solve the captcha")
            True
        """
        ...

    @property
    def tab(self) -> Awaitable[Any]:
        """For an `async def` route or category: `page = await site.tab`, Playwright's own page.

        It is outside the source API's version promise, since Playwright's API isn't flnd's to
        keep stable; plain code uses the verbs above.
        """
        ...

    def html(self, text: str | bytes) -> Any:
        """Parse an HTML page, to read its elements and the JSON it embeds.

        The page answers CSS queries: `css(query)` gives every matching element and
        `first(query)` the first one, or None. An element has `text` (its text, whitespace
        collapsed), `attr(name)` (an attribute, None when absent), and `css` and `first` of its
        own. The page also has `script(id)`, the JSON of `<script id=...>` (None when absent);
        `blocks(contains)`, the JSON of every `<script type="application/json">` holding
        `contains`; and `visible`, the text a reader sees, without scripts and styles. There is
        no XPath. A path through the decoded JSON is `dig`'s job.

            page = site.html(site.fetch(f"{SITE}/embed/{video}").text)
            data = dig(page.script("__NEXT_DATA__"), "props.pageProps.item")
            links = [a.attr("href") for a in page.css('a[href*="/video/"]')]

        Args:
            text: the page's HTML, as text or UTF-8 bytes.

        Returns:
            The parsed page.

        Raises:
            SourceError: a CSS query on it isn't valid CSS.
            Changed: `script` found a script that doesn't hold JSON.

        Example:
            >>> import inspect
            >>> list(inspect.signature(Site.html).parameters)
            ['self', 'text']
        """
        ...

    def pause(self, seconds: float, reason: str) -> None:
        """Wait `seconds`, as a page that needs time to settle does; a stop ends it.

        Args:
            seconds: how long.
            reason: why, for the people watching the run.

        Example:
            >>> from flnd.testing import FakeSite
            >>> FakeSite().pause(2, "the page settles")
        """
        ...

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        """Wait out a block the site showed in its content, such as a "please wait" answer.

        A rate limit by status (429) is waited out by `fetch` itself. This is for a block a
        source reads from the page: it waits `retry_after` (up to an hour), or, without one,
        30 s, then 2 min, then 8 min for the rounds in a row, and a fourth round in a row
        raises `Blocked`. A request that then succeeds starts the count again.

        Args:
            reason: what the site did, shown with the wait.
            retry_after: the seconds the site asked for; None waits by the rounds above.

        Raises:
            Blocked: the site kept blocking after three waits in a row.

        Example:
            >>> from flnd.testing import FakeSite
            >>> site = FakeSite()
            >>> site.back_off("please wait a few minutes", retry_after=60)
            >>> site.seen.back_offs
            ['please wait a few minutes']
        """
        ...

    @property
    def client(self) -> Any:
        """What the source's `connect` gave, such as an SDK client; None without one."""
        ...

    @property
    def state(self) -> MutableMapping[str, Any]:
        """The source's own key-value store, kept between runs in the home's `flnd.db`."""
        ...

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """The source's secrets, encrypted in the home's vault."""
        ...

    @property
    def interactive(self) -> bool:
        """Whether `ask` can ask: a login in a terminal."""
        ...

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        """Ask the person a question during `flnd login`, and return what was typed.

        Args:
            message: the question.
            secret: hide what is typed, as for a password.
            description: more lines under the question.

        Raises:
            UsageError: there is nobody to ask (not a login, or not a terminal).

        Example:
            >>> import inspect
            >>> list(inspect.signature(Site.ask).parameters)
            ['self', 'message', 'secret', 'description']
        """
        ...

    def info(self, message: str, **fields: Any) -> None:
        """A line naming the target: `site.info("{n} stories", n=3)`.

        Args:
            message: the text, with `{name}` fields filled in from `fields`.
            **fields: the values of the message's fields.

        Example:
            >>> from flnd.testing import FakeSite
            >>> site = FakeSite()
            >>> site.info("{n} stories", n=3)
            >>> site.seen.messages
            ['3 stories']
        """
        ...

    def warn(self, message: str, **fields: Any) -> None:
        """A warning naming the target, like `info`.

        Args:
            message: the text, with `{name}` fields filled in from `fields`.
            **fields: the values of the message's fields.

        Example:
            >>> from flnd.testing import FakeSite
            >>> site = FakeSite()
            >>> site.warn("{n} posts are hidden", n=2)
            >>> site.seen.messages
            ['! 2 posts are hidden']
        """
        ...


# ---------------------------------------------------------------------------------------------
# JSON paths
# ---------------------------------------------------------------------------------------------

# `dig` lives in the `flnkit.html` library, beside the HTML reader; this is its public name.
# Importing `html` loads neither selectolax nor msgspec.
dig = html.dig


# ---------------------------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------------------------

class Source:
    """One site flnd downloads from: what it declares, its routes, and its categories.

        class Example(Source):
            code = "exm"
            domains = ("example.com",)
            api = 1
            login = cookies("session")

            @target("exm:{user}", "example.com/{user}")
            def profile(self, site, user):
                return site.fetch(f"https://api.example.com/users/{user}").json

            @profile.category(newest_first=True)
            def posts(self, site, profile): ...

    Only `code`, `domains`, `api` and one route are required. The class is checked when it is
    made: a mistake raises `SourceError` naming the attribute and the fix, so a broken source
    never starts a run. Its `routes`, `forms`, `categories` and `single_kinds` are then
    derived from its `@target` methods.

    Where files go: `<home>/media/<code>/<target>/` is the target's folder, `folder` the folder
    under it, and `filename` the file's name without its extension, which the content decides.
    A template may use `source`, `target`, `category`, `post`, `num`, `count`, `index`,
    `extension`, `path`, `date` and `query`, and the post fields the source lists in `fields`.
    `key` is the history key of a file: change it and every saved file is downloaded again.

    A source is tested offline with `flnd.testing`: `check` checks the class, and `collect`
    runs a target on fixtures and shows where each file would be saved.

    Example:
        >>> from flnd.testing import collect
        >>> class Example(Source):
        ...     code = "exm"
        ...     domains = ("example.com",)
        ...     api = 1
        ...     @target("exm:{user}", "example.com/{user}", newest_first=True)
        ...     def photos(self, site, user):
        ...         data = site.fetch(f"https://api.example.com/users/{user}/photos").json
        ...         for item in data["items"]:
        ...             yield Post(item["id"], [File(item["url"])], date=item["time"])
        >>> Example.forms
        ('exm:<user>', 'https://www.example.com/<user>/')
        >>> saved = collect(Example, "https://www.example.com/example_user", json={
        ...     "https://api.example.com/users/example_user/photos": {"items": [
        ...         {"id": "p1", "url": "https://cdn.example.com/p1.jpg", "time": "2026-01-02"},
        ...     ]},
        ... })
        >>> [file.path for file in saved]
        ['p1_0.jpg']
    """

    # What targets start with (`exm:<user>`) and the login's slot in the vault: 2 to 16 lowercase
    # letters and digits.
    code: ClassVar[str]
    # The site's domains, short-link hosts included: whose links it reads, which requests carry
    # its cookies and its `sign` hook, and which are paced.
    domains: ClassVar[tuple[str, ...]]
    # The source API this source was written for.
    api: ClassVar[int]
    # The site's name as people know it, shown in help; the class's name by default.
    name: ClassVar[str] = ""
    # How it logs in: `cookies("session")`, a `Login` subclass's instance, or None for no login.
    login: ClassVar[Login | None] = None
    # Seconds between requests to its domains; the config file may change it.
    pace: ClassVar[float] = 0.5
    # The most files downloading at once from a host, by host name (a subdomain counts as its
    # parent): `{"cdn.example": 4}`. Each host's cap adapts, from 8 (or less) up to this, or up
    # to 32 for a host not named. A file the source fetches itself (a `fetch` function) counts
    # against its first domain.
    limits: ClassVar[Mapping[str, int]] = MappingProxyType({})
    # The templates of where a file goes and what it is named (see the class's docstring), and
    # of its history key.
    folder: ClassVar[str] = "{category}"
    filename: ClassVar[str] = "{post}_{num}"
    key: ClassVar[str] = "{post}_{num}"
    # The post fields the templates may use, besides the engine's.
    fields: ClassVar[tuple[str, ...]] = ()
    # `{pattern: message}`: links on the site refused with their own message, tried before any
    # route, such as Instagram's story links. A pattern here has no placeholder.
    unsupported: ClassVar[Mapping[str, str]] = {}
    # The User-Agent of its requests (not the browser's); None for the client's own.
    user_agent: ClassVar[str | None] = None
    # Whether its browser hides that it is automated.
    stealth: ClassVar[bool] = True
    # How the browser scrolls its feeds: "human" (wheel steps and short pauses, at most two tabs
    # at once), which sites flag less, or "fast".
    scroll: ClassVar[str] = "human"

    # Derived when the class is made: its routes in declaration order, the forms help lists, the
    # categories `--only` may name, and the kinds that are one post, downloaded whole.
    routes: ClassVar[tuple[Route, ...]] = ()
    forms: ClassVar[tuple[str, ...]] = ()
    categories: ClassVar[tuple[str, ...]] = ()
    single_kinds: ClassVar[tuple[str, ...]] = ()
    # `unsupported`, compiled.
    _refusals: ClassVar[tuple[tuple[_Pattern, str], ...]] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Check the class's declarations, and derive its routes, forms and categories.

        A class with no code and no route of its own is a base for other sources, and is left
        alone until a subclass is made.

        Raises:
            SourceError: a missing or malformed `code`, `domains` or `api`; no route; a pattern
                outside `domains` or given twice; a template naming an unknown field; a route
                with categories that yields; a wrong `login`, `pace`, `limits` or `scroll`; or a
                name of the old source API. The message names the source and the attribute.
        """
        super().__init_subclass__(**kwargs)
        for derived in ("routes", "forms", "categories", "single_kinds"):
            if derived in vars(cls):
                raise SourceError(f"{cls.__name__}.{derived}: made from the @target methods and "
                                  "their @<route>.category methods; declare no such field")
        # A subclass keeps its base's routes; one of the same name replaces it in place.
        routes = {route.kind: route for route in cls.routes}
        routes.update(
            (value.kind, value) for value in vars(cls).values() if isinstance(value, Route)
        )
        cls.routes = tuple(routes.values())
        if not getattr(cls, "code", None) and not cls.routes:
            return
        _check(cls)
        cls.name = cls.name or cls.__name__
        cls._refusals = tuple(
            (_Pattern(pattern), message) for pattern, message in cls.unsupported.items()
        )
        _check_patterns(cls)
        names = [category.name for route in cls.routes for category in route.categories]
        cls.categories = tuple(dict.fromkeys(names))
        cls.single_kinds = tuple(
            route.kind for route in cls.routes if not route.categories and not route.discovers
        )
        forms = [form for route in cls.routes for form in route.forms(cls.code)]
        # `ins:<user>` is the form people write most, so forms after `code:` or `code?` come first.
        cls.forms = tuple(
            sorted(forms, key=lambda form: not form.startswith((f"{cls.code}:", f"{cls.code}?")))
        )

    def resolve(self, text: str) -> Target:
        """Read one target's text, as people write it, through the routes.

            Instagram().resolve("https://www.instagram.com/p/C1abcDEFghi/")

        Override it only for forms no pattern can say (Telegram's chat titles and `t.me/c/<n>`
        links), and pass anything else on with `super().resolve(text)`.

        Args:
            text: the text after `code:`, or a bare link on the source's domains, stripped.

        Routes are tried in the order the class declares them, and a link pattern matches a
        link's first path parts, later parts allowed. So a narrower route (`example.com/p/{id}`)
        is declared before a catch-all one (`example.com/{user}`), or the catch-all reads its
        links first.

        Returns:
            The target the first matching route reads, its `kind` set.

        Raises:
            UsageError: an `unsupported` pattern matched, with its message, and no route pattern
                that matches too has more literal segments than it; or nothing matched, listing
                `forms`, with `url` set when the text is a link on the source's domains.

        Example:
            >>> class Example(Source):
            ...     code, domains, api = "exm", ("example.com",), 1
            ...     @target("example.com/p/{id:[A-Za-z0-9]+}")
            ...     def post(self, site, id):
            ...         yield Post(id)
            ...     @target("exm:{user}", "example.com/{user}")
            ...     def photos(self, site, user):
            ...         yield Post("p1")
            >>> Example().resolve("https://www.example.com/p/Abc123")
            Target(source='exm', kind='post', value='Abc123')
            >>> Example().resolve("@example_user")
            Target(source='exm', kind='photos', value='example_user')
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
                            raise UsageError(f"{self.code}: the query is empty")
                        return Target(f"{self.code}?{stripped}", kind=route.kind)
                    return Target(f"{self.code}:{value}", kind=route.kind)
        if refused is not None:
            raise UsageError(f"{self.code}: {refused[1]}")
        link = None if url is None else text if _is_url(text) else f"https://{text}"
        raise _Unmatched(
            f"{self.code}: can't read {text!r}; write it like {', '.join(self.forms)}", url=link
        )

    def sign(self, request: Request, /) -> None:
        """Add headers or a signature to a request to the source's domains, before each try.

            def sign(self, request):
                request.headers["x-csrf-token"] = request.cookies.get("ct0", "")

        Args:
            request: the request; change its `headers`.

        Example:
            >>> request = Request("GET", "https://api.example.com/me", {}, None, {})
            >>> Source.sign(Source(), request)
            >>> request.headers
            {}
        """

    def connect(self, site: Site, /) -> Any:
        """A context manager, or an async one, giving `site.client`; entered once per run.

        For a site with its own client (Telegram's SDK). The default gives None.

        The engine enters it before the run's first target of this source and leaves it when
        the source's last target is done, also after an error or a stop. An async context
        manager is entered on the run's event loop.

        Args:
            site: the `Site` of the source's account: its `secrets`, `state` and `cookies`.

        Returns:
            The context manager; what it yields becomes `site.client`.

        Raises:
            LoginError: the source needs a login it doesn't have.

        Example:
            >>> from contextlib import contextmanager
            >>> class Example(Source):
            ...     code, domains, api = "exm", ("example.com",), 1
            ...     @contextmanager
            ...     def connect(self, site):
            ...         client = {"session": "opened"}   # an SDK client, in a real source
            ...         try:
            ...             yield client
            ...         finally:
            ...             client["session"] = "closed"
            ...     @target("exm:{user}")
            ...     def photos(self, site, user):
            ...         yield Post("p1")
            >>> with Example().connect(None) as client:
            ...     client["session"]
            'opened'
        """
        return nullcontext()

    def date(self, site: Site, post: Post, /) -> DateLike:
        """The date of a post its listing left undated, from its own page; None if the site can't.

        The engine asks it once per undated post as its category ends, and each `sync` asks it
        for files saved undated or with an estimated day. A source overrides it with one paced
        request; the default knows nothing.

        Args:
            site: the target's site.
            post: the undated post; its `id` is what the site knows it by.

        Returns:
            The date, in any form `Post(date=)` takes; None when the site doesn't say.

        Raises:
            SiteError: the post can't be read; a 401, 403 or 429 (`Blocked`) stops asking for
                the run. A `NetworkError` or `LoginError` is taken the same way.

        Example:
            >>> from flnd.testing import FakeSite
            >>> class Example(Source):
            ...     code, domains, api = "exm", ("example.com",), 1
            ...     @target("exm:{user}")
            ...     def photos(self, site, user):
            ...         yield Post("p1")    # the listing has no dates
            ...     def date(self, site, post):
            ...         return site.fetch(f"https://api.example.com/posts/{post.id}").json["time"]
            >>> site = FakeSite(json={"https://api.example.com/posts/p1": {"time": 1767312000}})
            >>> Example().date(site, Post("p1"))
            1767312000
        """
        return None


class _Unmatched(UsageError):
    """A target no route reads; `url` is set when it is a link on the source's domains.

    The engine catches it to follow a short link once with a HEAD request and read where it
    leads. Everyone else sees the `UsageError` it is.

    Args:
        message: the whole message, starting with the source's code.
        url: the link to follow; None when the refusal is final.
    """

    def __init__(self, message: str, *, url: str | None = None):
        super().__init__(message)
        self.url = url


# ---------------------------------------------------------------------------------------------
# Checking a source when its class is made
# ---------------------------------------------------------------------------------------------

def _check(cls: type[Source]) -> None:
    """Check a source's declarations; see `Source.__init_subclass__` for what fails.

    Args:
        cls: the source class being made.

    Raises:
        SourceError: the first mistake, worded `<code>.<attribute>: <what>; <the fix>`.
    """
    label = cls.__name__
    code = vars(cls).get("code", getattr(cls, "code", None))
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise SourceError(f"{label}.code: {'missing' if code is None else repr(code)}; set code"
                          ' = "exm": 2 to 16 lowercase letters and digits, what targets start with')
    label = code
    for name, spelled in OLD_NAMES.items():
        if hasattr(cls, name):
            raise SourceError(f"{label}.{name}: source API {API} spells it {spelled}")
    domains = getattr(cls, "domains", None)
    if (not isinstance(domains, tuple) or not domains
            or not all(isinstance(d, str) and d and "/" not in d for d in domains)):
        raise SourceError(f'{label}.domains: set domains = ("example.com",), the hosts of its '
                          "links, without a scheme or a path")
    _check_api(cls, label)
    if not cls.routes:
        raise SourceError(f'{label}: no route; mark a method with @target("{code}:{{user}}")')
    login = cls.login
    if login is not None and not isinstance(login, Login):
        raise SourceError(f'{label}.login: set login = cookies("<name>"), an instance of a '
                          "Login subclass, or None")
    if not isinstance(cls.pace, (int, float)) or isinstance(cls.pace, bool) or cls.pace < 0:
        raise SourceError(f"{label}.pace: {cls.pace!r}; set the seconds between requests, "
                          "such as 0.5")
    if not isinstance(cls.limits, Mapping) or not all(
            isinstance(host, str) and isinstance(n, int) and not isinstance(n, bool) and n > 0
            for host, n in cls.limits.items()):
        raise SourceError(f'{label}.limits: {cls.limits!r}; set {{"<host>": <files at once>}}, '
                          "each a whole number above 0")
    if cls.scroll not in SCROLLS:
        raise SourceError(f"{label}.scroll: {cls.scroll!r}; set 'human' or 'fast'")
    clash = sorted(set(cls.fields) & RESERVED)
    if clash:
        raise SourceError(f"{label}.fields: {', '.join(clash)} is the engine's; name the "
                          "site's field otherwise")
    for route in cls.routes:
        function = route.function
        if route.categories and (inspect.isgeneratorfunction(function)
                                 or inspect.isasyncgenfunction(function)):
            raise SourceError(
                f"{label}.{route.kind}: a route with categories returns what they receive, but "
                "it yields; return a value, or drop its categories"
            )
    _check_templates(cls, label)


def _check_api(cls: type[Source], label: str) -> None:
    """Check `api`: the source API the source was written for must be this flnd's.

    Args:
        cls: the source class being made.
        label: how messages name it: its code.

    Raises:
        SourceError: `api` is missing, not a whole number from 1, or another major version;
            a newer one's hint is to update flnd.
    """
    api = getattr(cls, "api", None)
    if api is None:
        raise SourceError(f"{label}.api: missing; set api = {API}, the source API it is "
                          "written for")
    if not isinstance(api, int) or isinstance(api, bool) or api < 1:
        raise SourceError(f"{label}.api: {api!r}; set api = {API}")
    if api > API:
        raise SourceError(f"{label} needs source API {api}; this flnd has {API}",
                          hint="update flnd")
    if api < API:
        raise SourceError(f"{label} was written for source API {api}; this flnd has {API}",
                          hint="see the API versions part of the writing-a-source page of "
                               "flnd's docs")


def _check_templates(cls: type[Source], label: str) -> None:
    """Check that every template names only the engine's fields and the source's `fields`.

    Args:
        cls: the source class being made.
        label: how messages name it: its code.

    Raises:
        SourceError: a template leaves the target's folder, can't be parsed, or names a field
            that is neither the engine's nor in `fields`.
    """
    known = (*TEMPLATE_FIELDS, *cls.fields)
    templates = [("folder", cls.folder), ("filename", cls.filename), ("key", cls.key)]
    for route in cls.routes:
        templates.append((f"{route.kind}.folder", route.folder))
        for category in route.categories:
            templates += [(f"{category.name}.folder", category.folder),
                          (f"{category.name}.key", category.key)]
    for attribute, template in templates:
        if template is None:
            continue
        # A folder stays under the target's: no absolute path, drive, backslash or `..`.
        if ("\\" in template or template.startswith("/") or re.match(r"[A-Za-z]:", template)
                or any(part in (".", "..") for part in template.split("/"))):
            raise SourceError(f"{label}.{attribute}: {template!r} leaves the target's folder; "
                              "write a relative path with '/' and no '..'")
        try:
            used = text.fields(template)
        except ValueError as error:
            raise SourceError(f"{label}.{attribute}: {template!r}: {error}") from None
        for name in sorted(used - set(known)):
            raise SourceError(f"{label}.{attribute}: unknown field {{{name}}}; fields are "
                              f"{', '.join(known)}",
                              hint=f"list a post field in {label}.fields to use it")


def _check_patterns(cls: type[Source]) -> None:
    """Check that link patterns are on the source's domains, and that none is given twice.

    Args:
        cls: the source class being made, its routes and refusals compiled.

    Raises:
        SourceError: an `unsupported` pattern captures a value, a link pattern is on another
            site, a code pattern doesn't start with the source's code, or a pattern is given
            twice.
    """
    if any(pattern.names for pattern, _ in cls._refusals):
        raise SourceError(f"{cls.code}.unsupported: a pattern captures nothing; use * for any "
                          "segment")
    seen: set[str] = set()
    route_patterns = [pattern for route in cls.routes for pattern in route._patterns]
    own = [pattern for value in vars(cls).values() if isinstance(value, Route)
           for pattern in value._patterns]
    for pattern in route_patterns + [pattern for pattern, _ in cls._refusals]:
        if pattern.host is not None and not any(
            pattern.host == _bare_host(domain) for domain in cls.domains
        ):
            raise SourceError(
                f"{cls.code}: pattern {pattern.text!r} isn't on its domains {cls.domains}"
            )
        # Only the class's own routes: a subclass with another code reads its base's patterns
        # by their text after the code, which was checked when the base was made.
        if pattern.host is None and pattern in own and pattern.code != cls.code:
            written = pattern.text[len(pattern.code or ""):]
            written = written if written.startswith(("?", ":")) else f":{written}"
            raise SourceError(f"{cls.code}: pattern {pattern.text!r} must start with the "
                              f"source's code; write it {cls.code}{written}")
        # Compared without the code, www. and m., which matching drops anyway.
        if pattern.host is None:
            key = pattern.body
        else:
            head, sep, tail = pattern.text.partition("://")
            host_part, slash, rest = (tail if sep else head).partition("/")
            key = f"{_bare_host(host_part.lower())}{slash}{rest}"
        if key in seen:
            raise SourceError(f"{cls.code}: pattern {pattern.text!r} is given twice")
        seen.add(key)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------

def _best_streams(manifest: Any) -> tuple[str, str] | None:
    """A DASH manifest's best video URL and best audio URL; see `File.dash`.

    Args:
        manifest: the MPD's XML text, or anything else (which has none).

    Returns:
        The two URLs; None when the manifest has no usable pair.
    """
    import xml.etree.ElementTree as ElementTree

    if not isinstance(manifest, str) or not manifest:
        return None
    try:
        root = ElementTree.fromstring(manifest)
    except ElementTree.ParseError:
        return None

    # Any segmented manifest cannot be downloaded as single files.
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag in ("SegmentTemplate", "SegmentList", "SegmentTimeline"):
            return None

    found: dict[str, list[tuple[int, int, str, str]]] = {"video": [], "audio": []}
    for adaptation in root.iter():
        if adaptation.tag.split("}")[-1] != "AdaptationSet":
            continue
        for representation in adaptation.iter():
            if representation.tag.split("}")[-1] != "Representation":
                continue
            mime = (representation.get("mimeType")
                    or adaptation.get("mimeType")
                    or representation.get("contentType")
                    or adaptation.get("contentType") or "")
            url = ""
            for child in representation:
                if child.tag.split("}")[-1] == "BaseURL" and child.text:
                    url = child.text.strip()
                    break
            if not url:
                for child in adaptation:
                    if child.tag.split("}")[-1] == "BaseURL" and child.text:
                        url = child.text.strip()
                        break
            if (not url or url.endswith(("/", ".mpd", ".m4s"))
                    or "$" in url):
                continue
            kind = mime.split("/")[0].lower()
            if kind not in found:
                codecs = (representation.get("codecs") or adaptation.get("codecs") or "").lower()
                if any(codecs.startswith(p) for p in ("avc", "mp4v", "hev", "hvc", "vp", "av01")):
                    kind = "video"
                elif any(codecs.startswith(p) for p in ("mp4a", "opus", "vorbis", "aac")):
                    kind = "audio"
            if kind in found and url.startswith("https://"):
                found[kind].append((_number(representation.get("height")),
                                    _number(representation.get("bandwidth")), url, mime))
    if not found["video"] or not found["audio"]:
        return None
    video = max(found["video"])[2]
    audio = max(found["audio"], key=lambda entry: entry[1])[2]
    return video, audio


def _number(text: str | None) -> int:
    """A manifest's number attribute, 0 when it's missing or not a number."""
    return int(text) if text and text.isdigit() else 0


def _parse(text: str, domains: tuple[str, ...]) -> _Url | None:
    """`text` as a `_Url` when it's a URL on one of `domains`, with or without a scheme.

    Args:
        text: a target's text, such as `example.com/example_user` or a whole link.
        domains: the source's domains; a subdomain of one counts as on it.

    Returns:
        The link's bare host, its path parts (percent-decoded) and its query (each key's first
        value); None when `text` isn't a link, or is a link to another site.
    """
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
    """A host without prefixes that are the same site: `www.m.example.com` is `example.com`.

    Args:
        host: a host name, lowercased.

    Returns:
        The host with every `SITE_PREFIXES` prefix taken off its front, however many there are.
    """
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

    A backslash escapes the character after it, so `\\{` is a literal brace.

    Args:
        text: a pattern, or one part of it.
        separator: one character: `/` between segments, `?` before a query, `&` and `=` in it.
        once: split at the first separator only.

    Returns:
        The parts. With `once`, `[before, separator, after]`, or `[text, "", ""]` when there's
        none, as `str.partition` does.
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

    Returns:
        The pieces in order. A backslash pair is one literal `text` piece; a placeholder's regex
        is "" when it has none (`{user}`), else what follows its `:` (`{id:\\w+}`).

    Raises:
        SourceError: an unclosed `{`, a stray `}`, or a placeholder whose name isn't an identifier.
    """
    tokens, index = [], 0
    while index < len(segment):
        character = segment[index]
        if character == "\\" and index + 1 < len(segment):
            tokens.append(("text", segment[index:index + 2], ""))
            index += 2
            continue
        if character == "}":
            raise SourceError(f"pattern {pattern!r}: a '}}' with no '{{'")
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
            raise SourceError(f"pattern {pattern!r}: a '{{' with no '}}'")
        name, _, regex = segment[index + 1:end].partition(":")
        if not name.isidentifier():
            raise SourceError(f"pattern {pattern!r}: {{{name}}} isn't a placeholder name")
        tokens.append(("hole", name, regex))
        index = end + 1
    return tokens


def _compile(segment: str, pattern: str, names: list[str]) -> re.Pattern[str]:
    """One segment as a regex matching a whole URL part, adding its placeholders' names to `names`.

    Args:
        segment: one path segment or query value of the pattern.
        pattern: the whole pattern, for the error message.
        names: the names of the pattern's placeholders so far; the first one is captured as `value`.

    Returns:
        The compiled regex, for `fullmatch` against one URL part (or the text after `code:`). A
        placeholder without its own regex matches `NAME`, with one optional leading `@`.

    Raises:
        SourceError: the segment, or a placeholder's regex, is malformed.
    """
    regex = ""
    for kind, token, custom in _tokens(segment, pattern):
        if kind == "text":
            regex += re.escape(token)
        elif kind == "any":
            regex += "[^/]*"
        else:
            names.append(token)
            # Only the first placeholder is captured: a second one is refused once they're counted.
            group = "(?P<value>" if len(names) == 1 else "(?:"
            regex += f"@?{group}{NAME})" if not custom else f"{group}{custom})"
    try:
        return re.compile(regex)
    except re.error as error:
        raise SourceError(f"pattern {pattern!r}: {error}") from None


def _shown(segment: str) -> str:
    """A segment as help shows it: `{name:regex}` as `<name>`, `*` as `…`."""
    return "".join(
        {"text": text, "any": ANY}.get(kind, f"<{text}>")
        for kind, text, _ in _tokens(segment, segment)
    )


def _assign(instance: Any, **values: Any) -> None:
    """Set a frozen dataclass's fields from its own constructor."""
    for name, value in values.items():
        object.__setattr__(instance, name, value)


def _frozen(headers: Mapping[str, str] | None) -> Mapping[str, str] | None:
    """A read-only copy of `headers`, or None for none."""
    return MappingProxyType(dict(headers)) if headers else None
