"""The resolver: a target's text read into its plugin, value, kind and categories.

SPEC.md#targets explains it. It imports only the plugin a target names.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple
from urllib.parse import parse_qs, unquote, urlsplit

from flnd.errors import PluginError, UsageError

__all__ = ["Manifest", "Resolved", "entry", "resolve"]

# Host prefixes that are the same site: `m.example.com` is `example.com`.
PREFIXES = ("www.", "m.", "mobile.", "web.", "mbasic.", "touch.")
# What a placeholder without its own regex matches: a user name, after one optional `@`.
NAME = r"@?(?P<{}>[\w.]+)"
# Where a link no plugin claims goes.
WEB = "web"
# A link's `user:pw@`: never part of a key, a message or a log line.
USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]*@")


class Resolved(NamedTuple):
    """A target read from its text.

    Attributes:
        code: the plugin's code, lowercased.
        value: what the route's pattern (or its value template) made of the text.
        kind: the route's name.
        categories: the categories to take; empty for a route without any, and for a search
            or a `discovers` route with none named, whose found targets take their own.
        query: a search (`code?query`).
        named: whether the text named its categories (`|reels`, `|all`), which a run keeps
            for the target; False when they are the stored or the route's own.
        link: the value as typed, when it holds a link's `user:pw@`, which `value` (so the
            key) leaves out: what the route reads; "" when they are the same.
    """

    code: str
    value: str
    kind: str
    categories: tuple[str, ...]
    query: bool = False
    named: bool = False
    link: str = ""

    @property
    def key(self) -> str:
        """`code:value`, or `code?query` for a search; categories are not part of it.

        A single post's value starts with its kind (`ins:post/C1abc`), so it never clashes with
        a user's key."""
        return f"{self.code}{'?' if self.query else ':'}{self.value}"


# ---------------------------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Url:
    host: str
    parts: tuple[str, ...]
    query: Mapping[str, str]


class _Pattern:
    """One route or `unsupported` pattern, compiled from a plugin's text.

    Args:
        text: `exm:{user}`, `exm?{words}`, `example.com/p/{id:\\w+}` or `example.com/watch?v={id}`.
        template: the value made from the captures (`"{user}_{post}"`); None takes the only one.

    Raises:
        PluginError: the pattern is malformed.
    """

    def __init__(self, text: str, template: str | None = None):
        self.text, self.template = text, template
        found = re.match(r"[a-z][a-z0-9]*(?=[:?])", text)
        body = text[found.end():].removeprefix(":") if found else text
        path, mark, query = _split(body, "?", once=True)
        segments = [segment for segment in _split(path, "/") if segment]
        first = segments[0] if segments else ""
        self.host = _bare(first.lower()) if "." in first and not set(first) & set("{}*") else None
        self.search = self.host is None and not segments and mark == "?"
        self.names: list[str] = []
        if self.search:
            self.segments = [_compile(query, text, self.names)]
            self.keys: list[tuple[str, re.Pattern[str]]] = []
            self.literals = 0
            return
        if self.host is None and mark:
            raise PluginError(f"pattern {text!r}: only a link pattern can name a query")
        parts = segments[1:] if self.host is not None else ["/".join(segments)]
        self.segments = [_compile(part, text, self.names) for part in parts]
        pairs = [_split(pair, "=", once=True) for pair in _split(query, "&")] if mark else []
        self.keys = [(key, _compile(value, text, self.names)) for key, _, value in pairs]
        # How specific it is: the host, each literal segment and each query key count one; a code
        # pattern none.
        self.literals = 0 if self.host is None else 1 + len(self.keys) + sum(
            1 for part in parts if not set(part) & set("{}*"))
        if template is None and len(self.names) > 1:
            raise PluginError(f"pattern {text!r}: two placeholders need a value template")
        if template is not None and not set(re.findall(r"{(\w+)}", template)) <= set(self.names):
            raise PluginError(f"pattern {text!r}: {template!r} names a placeholder it lacks")

    def match(self, text: str, url: _Url | None) -> str | None:
        """The value when `text` (after `code:`) or `url` matches; None when it doesn't."""
        if self.host is None:
            found = [self.segments[0].fullmatch(text)] if url is None else [None]
        elif url is None or url.host != self.host or len(url.parts) < len(self.segments):
            return None
        else:
            # The first path parts must match; the parts after them are allowed.
            found = [regex.fullmatch(part) for regex, part in zip(self.segments, url.parts)]
            found += [regex.fullmatch(url.query[key]) if key in url.query else None
                      for key, regex in self.keys]
        if not all(found):
            return None
        groups = {name: value for match in found if match
                  for name, value in match.groupdict().items()}
        if self.template is not None:
            return self.template.format_map(groups)
        return groups.get(self.names[0], "") if self.names else ""

    def form(self) -> str:
        """The pattern as people write it: `exm:<user>`, `https://www.example.com/<user>/`."""
        shown = _shown(self.text)
        if self.host is None:
            return shown
        www = "www." if self.host.count(".") == 1 else ""
        return f"https://{www}{shown}{'' if '?' in shown else '/'}"


def _split(text: str, separator: str, *, once: bool = False) -> list[str]:
    """`text` split at `separator` outside `{…}`; `once` splits once, as `str.partition`.

    A backslash escapes the character after it, so `\\{` is a literal brace.
    """
    parts, depth, start, escaped = [], 0, 0, False
    for index, character in enumerate(text):
        if escaped or character == "\\":
            escaped = not escaped
            continue
        depth += {"{": 1, "}": -1}.get(character, 0)
        if character == separator and depth == 0:
            if once:
                return [text[:index], separator, text[index + 1:]]
            parts.append(text[start:index])
            start = index + 1
    return [text, "", ""] if once else [*parts, text[start:]]


def _compile(segment: str, pattern: str, names: list[str]) -> re.Pattern[str]:
    """One segment as a regex for a whole URL part, adding its placeholders' names to `names`."""
    regex, index = "", 0
    while index < len(segment):
        character = segment[index]
        if character == "\\" and index + 1 < len(segment):
            regex, index = regex + re.escape(segment[index + 1]), index + 2
            continue
        if character == "}":
            raise PluginError(f"pattern {pattern!r}: a '}}' with no '{{'")
        if character == "*":
            regex, index = regex + "[^/]*", index + 1
            continue
        if character != "{":
            regex, index = regex + re.escape(character), index + 1
            continue
        depth, end, escaped = 0, index, False
        for end in range(index, len(segment)):
            if escaped or segment[end] == "\\":
                escaped = not escaped
                continue
            depth += {"{": 1, "}": -1}.get(segment[end], 0)
            if depth == 0:
                break
        name, _, custom = segment[index + 1:end].partition(":")
        if depth or not name.isidentifier():
            raise PluginError(
                f"pattern {pattern!r}: {segment[index:end + 1]!r} isn't a placeholder")
        names.append(name)
        regex += f"(?P<{name}>{custom})" if custom else NAME.format(name)
        index = end + 1
    try:
        return re.compile(regex)
    except re.error as error:
        raise PluginError(f"pattern {pattern!r}: {error}") from None


def _shown(text: str) -> str:
    """A pattern as help shows it: `{name:regex}` as `<name>`, `*` as `…`."""
    out, depth, start, escaped = "", 0, 0, False
    for index, character in enumerate(text):
        if escaped or character == "\\":
            out += character if escaped and depth == 0 else ""
            escaped = not escaped
        elif character == "{":
            start = index if depth == 0 else start
            depth += 1
        elif character == "}":
            depth -= 1
            out += f"<{text[start + 1:index].partition(':')[0]}>" if depth == 0 else ""
        elif depth == 0:
            out += "…" if character == "*" else character
    return out


def _bare(host: str) -> str:
    """A host without `PREFIXES`, however many it has: `www.m.example.com` is `example.com`."""
    while (stripped := next((host[len(p):] for p in PREFIXES if host.startswith(p)), host)) != host:
        host = stripped
    return host


# ---------------------------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Route:
    name: str
    patterns: tuple[_Pattern, ...]
    # Each category's name, and whether it is the account's own (taken by default).
    categories: tuple[tuple[str, bool], ...] = ()
    single: bool = False
    discovers: bool = False
    shown: str | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.categories)


@dataclass(frozen=True)
class _Plugin:
    code: str
    domains: tuple[str, ...]
    routes: tuple[_Route, ...]
    unsupported: tuple[tuple[_Pattern, str], ...]

    @property
    def categories(self) -> tuple[str, ...]:
        """Every route's categories, once each, in order: what a search may name."""
        return tuple(dict.fromkeys(name for route in self.routes for name in route.names))

    @property
    def forms(self) -> str:
        forms = []
        for route in self.routes:
            shown = [pattern.form() for pattern in route.patterns]
            forms += [route.shown, *shown[1:]] if route.shown else shown
        return ", ".join(dict.fromkeys(forms))

    def on(self, host: str) -> bool:
        """Whether `host`, as it is, is one of the domains or under one, so a domain that starts
        with a prefix word (`mobile.de`) is claimed too: `www.mobile.de` is under it."""
        return any(host == domain or host.endswith("." + domain) for domain in self.domains)


class Manifest:
    """What resolving needs of every plugin, read from its class the first time it is asked for.

    Only the plugin a target names, or whose domain a link is on, is imported.

    Args:
        sources: the plugin classes by code, in the order links are tried; a `Registry` imports
            a class when it is first read.
        domains: a code's domains, without importing it; None reads the class's own.
    """

    def __init__(self, sources: Mapping[str, Any],
                 domains: Callable[[str], Sequence[str]] | None = None) -> None:
        self.sources = sources
        self._domains = domains or (lambda code: sources[code].domains)
        self._compiled: dict[str, _Plugin] = {}

    def __iter__(self) -> Iterator[str]:
        return iter(self.sources)

    def get(self, code: str) -> _Plugin | None:
        """The plugin with this code, compiled; None when there is none."""
        if code not in self._compiled:
            if code not in self.sources:
                return None
            cls = self.sources[code]
            try:
                self._compiled[code] = _compiled(code, entry(cls))
            except PluginError as error:
                raise PluginError(f"plugin {code}: {error}") from None
        return self._compiled[code]

    def owner(self, host: str) -> _Plugin | None:
        """The first plugin with a domain `host` is on."""
        for code in self.sources:
            if any(host == domain or host.endswith("." + domain) for domain in self._domains(code)):
                return self.get(code)
        return None

    @property
    def codes(self) -> str:
        """Every code, for a hint: `Plugins: tik, fbk, …`."""
        return f"Plugins: {', '.join(self.sources)}"


def _compiled(code: str, entry: Mapping[str, Any]) -> _Plugin:
    routes = tuple(_route(route) for route in entry.get("routes", ()))
    unsupported = tuple((_Pattern(text), reason)
                        for text, reason in dict(entry.get("unsupported", {})).items())
    return _Plugin(code, tuple(entry.get("domains", ())), routes, unsupported)


def entry(cls: type) -> dict[str, Any]:
    """What resolving reads of a plugin class, as plain data."""
    return {
        "domains": list(cls.domains),  # type: ignore[attr-defined]
        "unsupported": dict(cls.unsupported),  # type: ignore[attr-defined]
        "routes": [
            {"name": known.kind, "single": known.single, "discovers": known.discovers,
             "shown": known.shown,
             "patterns": [list(item) if isinstance(item, tuple) else item
                          for item in known.patterns],
             "categories": [{"name": category.name, "own": category.own}
                            for category in known.categories]}
            for known in cls.routes],  # type: ignore[attr-defined]
    }


def _route(route: Mapping[str, Any]) -> _Route:
    if not route.get("name"):
        patterns = list(route.get("patterns", ()))
        raise PluginError(f"a route with the patterns {patterns} has no name")
    patterns = tuple(_pattern(item) for item in route.get("patterns", ()))
    categories = tuple((item, True) if isinstance(item, str) else
                       (item["name"], bool(item.get("own", True)))
                       for item in route.get("categories", ()))
    return _Route(route["name"], patterns, categories, bool(route.get("single")),
                  bool(route.get("discovers")) or any(p.search for p in patterns),
                  route.get("shown"))


def _pattern(item: str | Sequence[str]) -> _Pattern:
    """A pattern text, or a `[pattern, value template]` pair."""
    if isinstance(item, str):
        return _Pattern(item)
    if len(item) != 2 or not all(isinstance(part, str) for part in item):
        raise PluginError(f"pattern {list(item)!r}: write a text or a [pattern, template] pair")
    return _Pattern(item[0], item[1])


# ---------------------------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------------------------

def resolve(
    text: str,
    *,
    manifest: Manifest,
    known: Mapping[str, Sequence[str]],
    follow: Callable[[str], str] | None = None,
) -> Resolved:
    """Read one target's text: `code:value[|categories]`, `code?query[|categories]`, a link.

    Args:
        text: the target as typed; spaces around it are ignored.
        manifest: the plugins to resolve with.
        known: the stored categories of each known target, by key; a target written without
            `|categories` takes its own.
        follow: where a short link leads (one HEAD request); None never follows one.

    Returns:
        The target.

    Raises:
        UsageError: no plugin has the code, a category isn't the route's, or nothing reads it;
            its message and hint leave out any link's `user:pw@`.
    """
    try:
        return _resolve(text, manifest, known, follow)
    except UsageError as error:
        if not USERINFO.search(f"{error}\0{error.hint}"):
            raise
        raise UsageError(_bare_link(str(error)), hint=_bare_link(error.hint)) from None


def _bare_link(text: str) -> str:
    """`text` with every link's `user:pw@` left out."""
    return USERINFO.sub(r"\1", text)


def _resolve(text: str, manifest: Manifest, known: Mapping[str, Sequence[str]],
             follow: Callable[[str], str] | None) -> Resolved:
    text = text.strip()
    if not text:
        raise UsageError("a target can't be empty")
    if text.lower().startswith(("http://", "https://")):
        return _link(*_listed(text, manifest), manifest, known, follow)
    found = re.match(r"([A-Za-z][A-Za-z0-9]*)\s*([:?])", text)
    if found is None:
        # A link without its scheme (`example.com/example_user`) reads as one, as today, only
        # when a plugin claims its host.
        host = text.split("/", 1)[0].lower() if "/" in text else ""
        if host and manifest.owner(host) is not None:
            return _link(*_listed(f"https://{text}", manifest), manifest, known, follow)
        choices = ", ".join(f"{code}:{text}" for code in manifest)
        raise UsageError(f'which plugin is "{text}"? Write it as {choices}')
    code, body = found[1].lower(), text[found.end():].strip()
    plugin = manifest.get(code)
    if plugin is None:
        raise UsageError(f"no plugin named {code}", hint=manifest.codes)
    if found[2] == "?":
        return _search(plugin, body, known)
    return _plain(plugin, body, known)


def _plain(plugin: _Plugin, body: str, known: Mapping[str, Sequence[str]]) -> Resolved:
    """`code:value[|categories]`."""
    if not body:
        raise UsageError(f"{plugin.code}: write a value after {plugin.code}:",
                         hint=f"Write it like {plugin.forms}")
    head, bar, tail = body.rpartition("|")
    whole = _find(plugin, body, None)
    if bar:
        hit = _find(plugin, head.strip(), None)
        if isinstance(hit, tuple):
            route, value = hit
            names = plugin.categories if route.discovers else route.names
            tokens = _tokens(tail)
            # A catch-all value (`{name:.+}`) holds a `|` unless every token is a category.
            if not isinstance(whole, tuple) or tokens is None or _named(tokens, names):
                return _made(plugin, route, value, tokens, known)
        elif not isinstance(whole, tuple):
            whole = hit
    elif not isinstance(whole, tuple):
        whole = _typed_key(plugin, body) or whole
    if isinstance(whole, str):
        raise UsageError(f"{plugin.code}: {whole}")
    if whole is None:
        _refuse_kind(plugin, body)
        raise UsageError(f"{plugin.code}: can't read {body!r}",
                         hint=f"Write it like {plugin.forms}")
    return _made(plugin, whole[0], whole[1], None, known)


def _typed_key(plugin: _Plugin, body: str) -> tuple[_Route, str] | None:
    """A single route's key typed back (`tik:short/ZMabc`): the route whose template, a
    `<kind>/{...}` value, makes `body`. Its placeholders are read as `[\\w.~%-]+`."""
    for route in plugin.routes:
        for pattern in route.patterns if route.single else ():
            if pattern.template and pattern.template.startswith(f"{route.name}/"):
                pieces = re.split(r"{\w+}", pattern.template)
                regex = r"[\w.~%-]+".join(re.escape(piece) for piece in pieces)
                if re.fullmatch(regex, body):
                    return route, body
    return None


def _refuse_kind(plugin: _Plugin, body: str) -> None:
    """Raise when `body` is `<word>/<rest>` and the word is no single-post kind of the plugin."""
    word, slash, rest = body.partition("/")
    if not (slash and rest and re.fullmatch(r"[A-Za-z]\w*", word)):
        return
    # Each single route's key as typed: its value template, `<id>` for each placeholder.
    templates = [next((p.template for p in route.patterns if p.template), f"{route.name}/{{id}}")
                 for route in plugin.routes if route.single]
    forms = " or ".join(f"{plugin.code}:{re.sub(r'{[^{}]*}', '<id>', template)}"
                        for template in templates)
    pipe = f'categories after |: "{plugin.code}:<user>|tagged"'
    raise UsageError(f"{plugin.code}:{body}: {word} isn't a post kind of {plugin.code}; "
                     f"write {forms + ', or ' if forms else ''}{pipe}")


def _search(plugin: _Plugin, body: str, known: Mapping[str, Sequence[str]]) -> Resolved:
    route = next((route for route in plugin.routes
                  if any(pattern.search for pattern in route.patterns)), None)
    if route is None:
        raise UsageError(f"{plugin.code} has no search", hint=f"Write it like {plugin.forms}")
    # A query is free text, so it splits only before a list of categories:
    # `mht?AC|DC` and `twt?url:example.com|x` stay whole.
    head, bar, tail = body.rpartition("|")
    split = bool(bar) and _named(_tokens(tail), plugin.categories)
    tokens = _tokens(tail) if split else None
    query = (head if split else body).strip()
    if not query:
        raise UsageError(f"{plugin.code}: the query is empty")
    return _made(plugin, route, query, tokens, known, query=True)


def _tokens(tail: str) -> list[str] | None:
    """The category names after a `|`; None when there are none (`exm:example_user|`)."""
    tokens = [token.strip() for token in tail.split(",") if token.strip()]
    return tokens or None


def _named(tokens: list[str] | None, names: Sequence[str]) -> bool:
    """Whether every token is `all` or one of `names`; no token at all counts."""
    return all(token == "all" or token in names for token in tokens or ())


def _listed(text: str, manifest: Manifest) -> tuple[str, list[str] | None]:
    """A link and the categories after its last `|`, when it is a plugin's and every name in
    the list is one of the plugin's categories or `all`; else the text whole, with none."""
    head, bar, tail = text.rpartition("|")
    plugin = manifest.owner((urlsplit(head).hostname or "").lower().rstrip(".")) if bar else None
    tokens = _tokens(tail)
    if plugin is None or not tokens or not _named(tokens, plugin.categories):
        return text, None
    return head.strip(), tokens


def _link(text: str, tokens: list[str] | None, manifest: Manifest,
          known: Mapping[str, Sequence[str]], follow: Callable[[str], str] | None, *,
          typed: str | None = None) -> Resolved:
    """A link: the first plugin whose domains have its host, else `web`; its categories are
    `tokens`, else the stored or the route's own."""
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower().rstrip(".")
    plugin = manifest.owner(host)
    if plugin is None:
        web = manifest.get(WEB)
        hit = _find(web, text, None) if web is not None else None
        if not isinstance(hit, tuple):
            raise UsageError(f"no plugin reads links to {host}", hint=manifest.codes)
        return _made(web, hit[0], hit[1], None, known)
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}

    parts = tuple(unquote(part) for part in parsed.path.split("/") if part)
    hit = _find(plugin, text, _Url(_bare(host), parts, query))
    if isinstance(hit, tuple):
        # A search link keys as the search (`loi?cats`), so its key reads back.
        return _made(plugin, hit[0], hit[1], tokens, known,
                     query=any(pattern.search for pattern in hit[0].patterns))
    if isinstance(hit, str):
        raise UsageError(f"{plugin.code}: {hit}")
    # A short link (fb.watch/<code>) no route reads: where it leads, once.
    followed = follow(text) if follow is not None else ""
    if followed and followed != text:
        return _link(followed, tokens, manifest, known, None, typed=text)
    led = f" (from {typed})" if typed is not None else ""
    raise UsageError(f"{plugin.code}: can't read {text}{led}",
                     hint=f"Write it like {plugin.forms}")


def _find(plugin: _Plugin, text: str, url: _Url | None) -> tuple[_Route, str] | str | None:
    """The first route reading `text`, with its value; an `unsupported` reason; or None."""
    refused = next(((pattern, reason) for pattern, reason in plugin.unsupported
                    if pattern.match(text, url) is not None), None)
    for route in plugin.routes:
        for pattern in route.patterns:
            if pattern.search:
                continue
            value = pattern.match(text, url)
            if value is not None and (refused is None or pattern.literals > refused[0].literals):
                return route, value
    return refused[1] if refused is not None else None


def _made(plugin: _Plugin, route: _Route, value: str, tokens: list[str] | None,
          known: Mapping[str, Sequence[str]], *, query: bool = False) -> Resolved:
    """The target, its categories chosen: named, `all`, stored, or the route's own; a link's
    `user:pw@` is left out of its value, and kept in `link` for the route."""
    bare = _bare_link(value)
    found = Resolved(plugin.code, bare, route.name, (), query, link=value if bare != value else "")

    names = plugin.categories if route.discovers else route.names
    if tokens is None:
        if found.key in known:
            return found._replace(categories=tuple(known[found.key]))
        own = () if route.discovers else tuple(name for name, own in route.categories if own)
        return found._replace(categories=own)
    if not names and route.single:
        raise UsageError(f"{plugin.code}:{value} is one post, with no categories")
    for token in tokens:
        if token != "all" and token not in names:
            raise UsageError(f"{plugin.code} has no category {token or '(empty)'}",
                             hint=f"Categories: {', '.join(names) or 'none'}")
    chosen = names if "all" in tokens else tuple(name for name in names if name in tokens)
    return found._replace(categories=chosen, named=True)
