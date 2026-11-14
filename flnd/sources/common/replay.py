"""A page's own pagination request, sent again from the page with another cursor: replay.

Instagram and Facebook GraphQL requests carry page-computed tokens (`fb_dtsg`, `lsd`,
`doc_id`, etc.). A source scrolls until the page sends its first next-page request, captures
it (`Responses.request`), and sends subsequent requests from the tab changing only the cursor
in its `variables` (`site.fetch(…, in_page=True)`).

`Replay.of` captures the request; `send` sends it again with a new cursor.

    with watch_responses(site, "graphql", scroll=True) as responses:
        for data in responses:
            if (replay := Replay.of(responses.request, cursor="after")) is not None:
                break
    page = replay.send(site, end_cursor).json
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from flnd.source import Site
    from shared import http

__all__ = ["Replay"]

# The form field or query parameter holding Relay variables as JSON on Meta sites.
VARIABLES = "variables"
# The headers worth sending again: the page's tokens travel in `x-…` headers (`x-fb-lsd`,
# `x-csrftoken`, `x-ig-app-id`, `x-fb-friendly-name`, …), and the body's type must stay the same.
# The browser sets the rest itself (cookies, origin, user agent), and refuses to have them set.
KEPT_PREFIX = "x-"
KEPT = ("content-type",)


@dataclass(frozen=True)
class Replay:
    """One pagination request a page sent, ready to be sent again with another cursor.

        replay = Replay.of(responses.request, cursor="after")
        data = replay.send(site, "QVFE…").json

    Made by `of`; frozen.
    """

    # The request's URL, its query included.
    url: str
    # "GET" or "POST".
    method: str
    # The form body of a POST as (name, value) pairs; empty for a GET.
    form: tuple[tuple[str, str], ...]
    # The headers sent again: the `x-…` ones and `Content-Type`.
    headers: Mapping[str, str]
    # The name of the variable that holds the cursor: "after" on Instagram, "cursor" on Facebook.
    cursor: str

    @classmethod
    def of(cls, request: Any, *, cursor: str) -> Replay | None:
        """The replay of a request the page sent, or None when it cannot be replayed."""
        if request is None:
            return None
        method = str(getattr(request, "method", "") or "").upper()
        url = str(getattr(request, "url", "") or "")
        if method not in ("GET", "POST") or not url:
            return None
        post_data = str(getattr(request, "post_data", None) or "")
        form = tuple(parse_qsl(post_data, keep_blank_values=True)) if method == "POST" else ()
        raw = dict(getattr(request, "headers", None) or {})
        headers = {name: value for name, value in raw.items()
                   if name.lower().startswith(KEPT_PREFIX) or name.lower() in KEPT}
        made = cls(url, method, form, headers, cursor)
        variables = made._variables()
        return made if variables is not None and cursor in variables else None

    def send(self, site: Site, cursor: str) -> http.Response:
        """Send the request again from the target's tab with cursor, returning the response."""
        variables = {**(self._variables() or {}), self.cursor: cursor}
        encoded = json.dumps(variables, separators=(",", ":"))
        if self.method == "POST":
            form = [(name, encoded if name == VARIABLES else value) for name, value in self.form]
            return site.fetch(
                self.url, method="POST", headers=self.headers, data=urlencode(form), in_page=True,
            )
        parts = urlsplit(self.url)
        query = [(name, encoded if name == VARIABLES else value)
                 for name, value in parse_qsl(parts.query, keep_blank_values=True)]
        target_url = urlunsplit(parts._replace(query=urlencode(query)))
        return site.fetch(target_url, headers=self.headers, in_page=True)

    def _variables(self) -> dict[str, Any] | None:
        """The request's variables as a dict, from its form or query, or None."""
        query = parse_qsl(urlsplit(self.url).query, keep_blank_values=True)
        pairs = self.form if self.method == "POST" else query
        text = next((value for name, value in pairs if name == VARIABLES), None)
        if text is None:
            return None
        try:
            variables = json.loads(text)
        # Variables that aren't JSON can't take a cursor: the request isn't one to replay.
        except ValueError:
            return None
        return variables if isinstance(variables, dict) else None
