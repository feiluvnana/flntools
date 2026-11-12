"""A page's own pagination request, sent again from the page with another cursor: replay.

Instagram's and Facebook's GraphQL requests carry tokens the page computes (`fb_dtsg`, `lsd`,
`doc_id`, and the relay variables a query needs that day), which no built-in table can keep up
with. So a source scrolls a tab only until the page sends its first next-page request, keeps that
request (`Responses.request`), and from then on sends the same request itself, from the logged-in
tab (`site.fetch(…, in_page=True)`), changing only the cursor in its `variables`. Each page is then
one small JSON request instead of a scroll that loads every image and video preview.

How it works: `Replay.of` reads the request's URL, method, body and the headers worth sending again
(the `x-…` ones, which carry the tokens, and `Content-Type`), and finds its `variables`: in the
form body of a POST, or in the query of a GET. It refuses a request whose variables don't hold the
cursor's name, which isn't a pagination request. `send` puts a cursor in and sends it in page,
paced, its 429 waited out by the site. What a response means (a page, a throttle, a refusal) is
the source's to read; so is falling back to scrolling when a replay is refused.

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

# The form field, or query parameter, that holds a Relay query's variables as JSON, on both Meta sites.
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
        """The replay of a request the page sent, or None when it isn't a pagination request that can be replayed.

            replay = Replay.of(responses.request, cursor="cursor")

        Args:
            request: Playwright's `Request` (its `url`, `method`, `post_data` and `headers`), or
                None, which gives None.
            cursor: the name of the variable that holds the cursor.

        Returns:
            None when there is no request, when it isn't a GET or a POST, or when its `variables`
            are missing, aren't a JSON object, or don't name `cursor`. Then the source scrolls on.
        """
        if request is None:
            return None
        method = str(getattr(request, "method", "") or "").upper()
        url = str(getattr(request, "url", "") or "")
        if method not in ("GET", "POST") or not url:
            return None
        form = tuple(parse_qsl(str(getattr(request, "post_data", None) or ""), keep_blank_values=True)) \
            if method == "POST" else ()
        raw = dict(getattr(request, "headers", None) or {})
        headers = {name: value for name, value in raw.items()
                   if name.lower().startswith(KEPT_PREFIX) or name.lower() in KEPT}
        made = cls(url, method, form, headers, cursor)
        variables = made._variables()
        return made if variables is not None and cursor in variables else None

    def send(self, site: Site, cursor: str) -> http.Response:
        """Send the request again from the target's tab, with `cursor` as its cursor, and return the response.

            data = replay.send(site, end_cursor).json

        Args:
            site: the target's site; its tab sends it, paced, a 429 waited out.
            cursor: the cursor the last page gave.

        Raises:
            FetchError: a refusal (with its `status` and `response`), or a request the page couldn't send.
        """
        variables = {**(self._variables() or {}), self.cursor: cursor}
        encoded = json.dumps(variables, separators=(",", ":"))
        if self.method == "POST":
            form = [(name, encoded if name == VARIABLES else value) for name, value in self.form]
            return site.fetch(self.url, method="POST", headers=self.headers, data=urlencode(form), in_page=True)
        parts = urlsplit(self.url)
        query = [(name, encoded if name == VARIABLES else value)
                 for name, value in parse_qsl(parts.query, keep_blank_values=True)]
        return site.fetch(urlunsplit(parts._replace(query=urlencode(query))), headers=self.headers, in_page=True)

    def _variables(self) -> dict[str, Any] | None:
        """The request's variables as a dict, from its form or its query; None when it has none that are an object."""
        pairs = self.form if self.method == "POST" else parse_qsl(urlsplit(self.url).query, keep_blank_values=True)
        text = next((value for name, value in pairs if name == VARIABLES), None)
        if text is None:
            return None
        try:
            variables = json.loads(text)
        # Variables that aren't JSON can't take a cursor: the request isn't one to replay.
        except ValueError:
            return None
        return variables if isinstance(variables, dict) else None
