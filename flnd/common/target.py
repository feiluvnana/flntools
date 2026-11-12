"""Targets: what to download, and how the text people paste is read.

`code:value` goes to that source's `resolve`. A bare URL finds its source by domain.
Anything else is refused, because it could belong to any source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Mapping, NamedTuple, Sequence
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from flnd.sources.base import Source

# Hosts that are the same site as their bare domain.
SITE_PREFIXES = ("www.", "m.", "mobile.")
NAME = re.compile(r"@?([\w.]+)")


class TargetError(ValueError):
    """A target that can't be read, with what to write instead."""


@dataclass(frozen=True)
class Target:
    """One thing to download: `Target("ins", "user", "example_user")`.

        target = Target("tik", "video", "7301234567890123456"); str(target)   # "tik:7301234567890123456"

    `kind` is what it points to (`user`, `post`, `video` or `chat`), and `value` its clean
    form, used for folders and the history.
    """

    source: str
    kind: str
    value: str

    def __str__(self) -> str:
        # The account a login and `connect` see has no value, and reads as its source's code.
        return f"{self.source}:{self.value}" if self.value else self.source


class Link(NamedTuple):
    """A URL on one source's site: the host without `www.`, the path's parts, and the query."""

    host: str
    parts: list[str]
    query: dict[str, str]


def resolve(text: str, sources: Mapping[str, type[Source]], *, follow: Callable[[str], str] | None = None) -> Target:
    """Read one target as people write it, and return it resolved by its source.

        resolve("ins:@example_user", SOURCES)                     # Target("ins", "user", "example_user")
        resolve("https://x.com/example_handle", SOURCES)                 # Target("twt", "user", "example_handle")

    `follow(url)` gives where a short link (`vm.tiktok.com/…`) leads; without it, short links
    reach the source as they are. Raises `TargetError` with what to write instead.
    """
    cleaned = text.strip()
    if not cleaned:
        raise TargetError("a target can't be empty")
    code, colon, value = cleaned.partition(":")
    if colon and not _is_url(cleaned):
        code = code.strip().lower()
        if code not in sources:
            raise TargetError(f"unknown source '{code}'. Sources: {', '.join(sources)}")
        source, cleaned = sources[code](), value.strip()
        if not cleaned:
            raise unreadable(source, value)
    else:
        found = owner(cleaned, sources)
        if found is None:
            raise TargetError(_which_source(cleaned, sources))
        source = found()
    if follow is not None and link(cleaned, source.short_links) is not None:
        cleaned = follow(cleaned)
    return source.resolve(cleaned)


def owner(text: str, sources: Mapping[str, type[Source]]) -> type[Source] | None:
    """The source whose domains a URL belongs to, or None."""
    for source in sources.values():
        if link(text, source.domains) is not None:
            return source
    return None


def link(text: str, domains: Sequence[str]) -> Link | None:
    """`text` as a `Link` when it is a URL on one of `domains`, with or without a scheme, else None.

        link("https://www.instagram.com/example_user/?hl=en", ("instagram.com",))
        # Link("instagram.com", ["example_user"], {"hl": "en"})
    """
    if "/" not in text and not _is_url(text):
        return None
    parsed = urlsplit(text if _is_url(text) else f"https://{text}")
    host = (parsed.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in domains):
        return None
    for prefix in SITE_PREFIXES:
        host = host.removeprefix(prefix)
    query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
    return Link(host, [part for part in parsed.path.split("/") if part], query)


def name(text: str) -> str | None:
    """A user name with its `@` removed, or None when `text` isn't one: `name("@example_handle")` is "example_handle"."""
    match = NAME.fullmatch(text)
    return match[1] if match else None


def unreadable(source: Source, text: str) -> TargetError:
    """The error for a form `source` doesn't accept, listing the ones it does."""
    return TargetError(f"{source.code}: can't read {text!r}; write it like {', '.join(source.examples)}")


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def _which_source(text: str, sources: Mapping[str, type[Source]]) -> str:
    choices = ", ".join(f"{code}:{text}" for code in sources)
    return f'which source is "{text}"? Write it as {choices}'
