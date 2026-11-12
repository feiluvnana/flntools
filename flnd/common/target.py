"""What a hand-written `resolve` reads a target with, until its source is ported to routes.

Phase 6, step 5 reads targets through `flnd.source`'s routes, and `Target` and `TargetError`
live there. A source that still writes its own `resolve` (twt, tik, fbk, loi and tlg, until each
is ported) uses these three helpers; each port drops its import, and the last one deletes this file.

    found = link("https://www.instagram.com/example_user/?hl=en", ("instagram.com",))
    user = name(found.parts[0]) if found else name(text)
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple, Sequence
from urllib.parse import parse_qs, urlsplit

from flnd.source import TargetError

if TYPE_CHECKING:
    from flnd.source import Source

# Hosts that are the same site as their bare domain.
SITE_PREFIXES = ("www.", "m.", "mobile.")
NAME = re.compile(r"@?([\w.]+)")


class Link(NamedTuple):
    """A URL on one source's site: the host without `www.`, the path's parts, and the query."""

    host: str
    parts: list[str]
    query: dict[str, str]


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
    return TargetError(f"{source.code}: can't read {text!r}; write it like {', '.join(source.forms)}")


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))
