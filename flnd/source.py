"""What a source is written against: the `Source` base class, its routes, and the errors it raises.

A source is one class. Its fields say what the site is (`code`, `domains`, `needs`, …), its routes
say which targets it reads, and `run(session)` downloads one of them. A route is a method marked
`@target(*patterns)`: the method's name is the target's kind, and each pattern is one form people
write. The default `Source.resolve` reads a target's text through the routes:
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

    from flnd.source import Source, Target, TargetError, target

    class Example(Source):
        code, name, domains = "exm", "Example", ("example.com",)
        unsupported = {"example.com/stories": "story links aren't supported; give the profile"}

        @target("example.com/p/{code:[\\w-]+}")
        def post(self, session, code): ...

        @target("{user}", "example.com/{user}")
        def user(self, session, user): ...

        def run(self, session): ...

    Example().resolve("https://www.example.com/example_user/?hl=en")   # Target("exm", "user", "example_user")
    Example.forms   # ("exm:<user>", "https://www.example.com/p/<code>/", "https://www.example.com/<user>/")

Phase 6, step 5: routes only read targets. The engine still calls `run(session)` with the target a
route gave, and a route's method body isn't called. Step 6 gives routes their bodies and adds
`Post`, `File`, `Site`, `Login` and `pages` here; it also renames `session` to `site`.
"""

from __future__ import annotations

import inspect
import re
from abc import ABC, abstractmethod
from contextlib import nullcontext
from types import MethodType
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Mapping, Union
from urllib.parse import parse_qs, urlsplit

from flnd.common.pace import Pace

if TYPE_CHECKING:
    import httpx

    from flnd.session import Session

__all__ = ["Error", "FetchError", "LoginError", "Pace", "Route", "Source", "Target", "TargetError", "target"]

# Hosts that are the same site as their bare domain: the desktop, mobile and old mobile sites.
SITE_PREFIXES = ("www.", "m.", "mobile.")
# What `{name}` matches without a regex: a user name, as the sites spell them, with one leading `@`
# dropped since people paste names both ways.
NAME = r"[\w.]+"
# Where a generated form shows a `*`: any one segment, which help can't name.
ANY = "…"

# Seconds before a retry, or a function of (error, attempt) giving them; None means the usual wait.
Wait = Union[float, Callable[[BaseException, int], Union[float, None]]]


class Error(Exception):
    """The base of the errors a source raises: `except flnd.source.Error` catches all three."""


class FetchError(Error):
    """A request that failed: `status` is the refusing HTTP status, or None (no response, or not JSON).

        try:
            data = session.fetch_json(url)
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


class Route:
    """A method that reads one kind of target, made by `@target(...)`; its name is the kind.

        Instagram.routes[0].kind       # "post"
        Instagram.routes[0].patterns   # ("instagram.com/p/{code:[\\w-]+}", …)

    Phase 6, step 5: only the patterns are used; the method's body is called from step 6 on.
    """

    def __init__(self, function: Callable[..., Any], patterns: tuple[str, ...], *, shown: str | None):
        """Compile `patterns` for `function`, raising `TypeError` for a mistake in them.

        Args:
            function: the decorated method, `(self, session, value)`.
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
        parameters = [parameter.name for parameter in inspect.signature(function).parameters.values()]
        if len(parameters) != 3:
            raise TypeError(f"route {self.kind}: take (self, session, value), not ({', '.join(parameters)})")
        for pattern in self._patterns:
            if len(pattern.names) != 1:
                # One value is the target's identity: its folder and its history keys.
                raise TypeError(f"route {self.kind}: pattern {pattern.text!r} needs exactly one placeholder, "
                                f"not {len(pattern.names)}")
            if pattern.names[0] != parameters[2]:
                raise TypeError(f"route {self.kind}: pattern {pattern.text!r} captures {pattern.names[0]!r}, "
                                f"but the method's value parameter is {parameters[2]!r}")

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
        def post(self, session, code): ...

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


class Source(ABC):
    """One site flnd downloads from: what it declares, its routes, and `run`.

        class Instagram(Source):
            code, name, domains = "ins", "Instagram", ("instagram.com",)
            needs = ("sessionid",)
            unsupported = {"instagram.com/stories": "story links aren't supported; give the profile"}

            @target("{user}", "instagram.com/{user}")
            def user(self, session, user): ...

            def run(self, session): session.save(url, "a.jpg", category="posts")

    Every subclass is listed in `flnd.sources.SOURCES`. When the class is created, its routes
    are collected in declaration order and checked: each pattern's host must be one of
    `domains`, and no pattern may appear twice. A mistake raises `TypeError` at import.
    """

    # The prefix in a target and the cookie file name, such as "ins".
    code: ClassVar[str]
    # Shown in help, such as "Instagram".
    name: ClassVar[str]
    # The site's domains, short-link hosts included: for bare URLs, and the cookies a login keeps.
    domains: ClassVar[tuple[str, ...]]
    # `{pattern: message}`: links on the site that are refused with their own message, tried before
    # any route, such as Instagram's story links. A pattern here has no placeholder.
    unsupported: ClassVar[Mapping[str, str]] = {}
    # The routes, collected from the `@target` methods when the class is created, in declaration order.
    routes: ClassVar[tuple[Route, ...]] = ()
    # The forms help and refusals list: generated from the routes when the class is created.
    forms: ClassVar[tuple[str, ...]] = ()
    # What `--from` can take from this source; without `--from`, a target gets every one.
    categories: ClassVar[tuple[str, ...]] = ()
    # Target kinds that are one post, downloaded whole (ins "post", tik "video"). A post has no
    # categories (tagged or reposts describe a profile's tab, not a post), so `--from` with one is
    # a usage error rather than a filter that silently drops every file.
    single_kinds: ClassVar[tuple[str, ...]] = ()
    # Cookie names, any one of which proves a login.
    needs: ClassVar[tuple[str, ...]] = ()
    requires_login: ClassVar[bool] = False
    login_methods: ClassVar[tuple[str, ...]] = ("browser", "file")
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
        """Collect and check the class's routes and `unsupported`, and generate its forms.

        Raises:
            TypeError: a pattern on a host outside `domains`, a pattern given twice, a placeholder in
                `unsupported`.
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

    @abstractmethod
    def run(self, session: Session) -> Any:
        """Scrape one target, reporting each file with `session.save`. It may be `async def`."""

    def headers(self, cookies: Mapping[str, str]) -> Mapping[str, str]:
        """Extra headers for every request, built from the login's `{name: value}` cookies."""
        return {}

    def sign(self, request: httpx.Request) -> httpx.Request:
        """Sign a request; it runs before every attempt, retries included, so timestamps stay fresh."""
        return request

    def login(self, session: Session, method: str) -> None:
        """Log in by `method`, for a source with its own way (tlg).

        Cookie sources keep this default, and the engine's `browser` and `file` methods log in.
        A login that fails raises `LoginError`, which `flnd login` reports.
        """
        raise NotImplementedError(f"{self.code} has no login method {method!r}")

    def logged_in(self, session: Session) -> bool:
        """Whether a login is stored: by default, one of `needs` is among the saved cookies."""
        return not self.needs or any(name in session.cookies for name in self.needs)

    def connect(self, session: Session) -> Any:
        """A context manager, or an async one, giving `session.client`; entered once per run."""
        return nullcontext()


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
