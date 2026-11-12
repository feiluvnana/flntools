"""Fakes for testing sources without the network or a browser: `FakeSite` and `collect`.

- `FakeSite` is a `flnd.source.Site` with exactly its 13 members, answering `fetch` from fixtures
  keyed by URL without its query, and recording what the source did in a `Seen`.
- `collect(source, text, …)` resolves a target, runs its route and wanted categories on a
  `FakeSite`, lists lazy files, and names and keys each file as the job would: a list of `Saved`.

pyright checks that both match their protocols (see tests/typing/usage.py).
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, MutableMapping, overload
from unittest import mock

from flnd import job
from flnd.engine import resolve
from flnd.session import Options
from flnd.source import FetchError, File, Post, Range, Source, Target
from shared import http
from shared._browser import Page

# A fixture: data for a URL, a FetchError to raise, or a function of (url, request) giving either.
Answer = Any


@dataclass
class Seen:
    """What a source did through a `FakeSite`, which keeps its own members to the 13 a `Site` has."""

    # (method, url, options) of each `fetch`, options holding `headers`, `in_page` and httpx's.
    requests: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    # Each URL `goto` loaded.
    visited: list[str] = field(default_factory=list)
    # Each `info` line, and each `warn` line after "! ".
    messages: list[str] = field(default_factory=list)
    # The reason of each `back_off`.
    back_offs: list[str] = field(default_factory=list)


class FakeSite:
    """A `Site` whose answers come from `json` and `html`, keyed by URL without its query, and which records in `seen`.

        site = FakeSite(Target("exm", "user", "example_user"), json={f"{API}/users/example_user": {"id": "42"}})
        site.fetch(f"{API}/users/example_user").json      # {"id": "42"}

    Args:
        target: the target it acts for.
        cookies: the login's cookies.
        json: data for a URL, a `FetchError` to raise, or a function of `(url, request)` giving either.
        html: page text for a URL, as `json`.
        pages: the HTML `page` shows after `goto` loaded a URL; "" for one not listed.
        options: the run's options; `time` reads `--time` from them.
        client: what `client` gives, such as a fake SDK client.
        seen: where it records; a new `Seen` when None.
    """

    def __init__(self, target: Target, *, cookies: Mapping[str, str] | None = None,
                 json: Mapping[str, Answer] | None = None, html: Mapping[str, Answer] | None = None,
                 pages: Mapping[str, str] | None = None, options: Options = Options(), client: Any = None,
                 seen: Seen | None = None):
        self.target = target
        self.client = client
        self.state: MutableMapping[str, Any] = {}
        self.secrets: MutableMapping[str, str] = {}
        self._seen = seen if seen is not None else Seen()
        self._cookies = dict(cookies or {})
        self._json, self._html, self._pages = dict(json or {}), dict(html or {}), dict(pages or {})
        self._options = options
        self._raw_page = mock.MagicMock()

    @property
    def cookies(self) -> Mapping[str, str]:
        return self._cookies

    @property
    def time(self) -> Range:
        return Range(self._options.since, self._options.until)

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
        self._seen.requests.append((method, url, {"headers": dict(headers or {}), "in_page": in_page, **request}))
        base = url.split("?")[0]
        if base in self._json:
            answer = _answer(self._json, url, request)
            if not isinstance(answer, FetchError):
                return http.Response(200, url, {"content-type": "application/json"}, jsonlib.dumps(answer).encode())
        else:
            answer = _answer(self._html, url, request)
            if not isinstance(answer, FetchError):
                return http.Response(200, url, {"content-type": "text/html"}, str(answer).encode())
        if soft and answer.status in (401, 403, 404):
            return None
        raise answer

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        self._seen.visited.append(url)
        return True

    def handoff(self, *, until: Callable[[], bool], message: str, timeout: float | None = None) -> bool:
        return until()

    @property
    def page(self) -> Page:
        url = self._seen.visited[-1] if self._seen.visited else "about:blank"
        self._raw_page.content.return_value = self._pages.get(url, "")
        self._raw_page.url = url
        return Page(self._raw_page, mock.MagicMock())

    def info(self, message: str, **fields: Any) -> None:
        self._seen.messages.append(message.format(**fields))

    def warn(self, message: str, **fields: Any) -> None:
        self._seen.messages.append("! " + message.format(**fields))

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        self._seen.back_offs.append(reason)


@dataclass(frozen=True)
class Saved:
    """One file `collect` found: where the job would save it, from where, for which post, and its history key."""

    # Its path under the target's folder, with the extension it's named with before its content
    # is checked ("" when the content decides): "posts/2026-01-02_C1abc_0.jpg".
    path: str
    # Its URL, mirrors, or fetch function; None for a text file.
    url: Any
    # The post it belongs to, its category set.
    post: Post
    # Its history key, without the source's code.
    key: str
    # The file itself.
    file: File


def collect(source: type[Source], text: str, *, json: Mapping[str, Answer] | None = None,
            html: Mapping[str, Answer] | None = None, pages: Mapping[str, str] | None = None,
            options: Options = Options(), cookies: Mapping[str, str] | None = None, client: Any = None,
            seen: Seen | None = None) -> list[Saved]:
    """What the job would download for `text`: its route and wanted categories run on a `FakeSite`.

        saved = collect(Example, "exm:example_user", json=FIXTURES, options=Options(categories=("posts",)))
        [s.path for s in saved]     # ["posts/2026-01-02_C1abc_0.jpg", "posts/2026-01-02_C1abc_1.mp4", …]

    Args:
        source: the source class.
        text: the target as people write it, `code:` or a link.
        json, html, pages, cookies, client, seen: the `FakeSite`'s, as it takes them.
        options: `--from` and `--time`.

    Returns:
        Each file in order, a file queued twice (same path or key) listed once, as the job queues it.
        Nothing is saved, so every lazy post's files are listed.

    An `async def` route or category runs on a loop of its own, as on the source's.
    """
    target = resolve(text, {source.code: source})
    site = FakeSite(target, cookies=cookies, json=json, html=html, pages=pages, options=options, client=client,
                    seen=seen)
    instance = source()
    categories = {category.name: category for route in source.routes for category in route.categories}
    loop = asyncio.new_event_loop()
    found: list[Saved] = []
    known: set[str] = set()
    try:
        for post in job.posts(instance, site, target, options, run_async=loop.run_until_complete):
            listed = list(post.files()) if callable(post.files) else list(post.files)
            for placed in job.place(instance, post, listed, category=categories.get(post.category)):
                if placed.path in known or placed.key in known:
                    continue
                known.update((placed.path, placed.key))
                found.append(Saved(placed.path, placed.file.url, post, placed.key, placed.file))
    finally:
        loop.close()
    return found


def _answer(answers: Mapping[str, Answer], url: str, request: dict[str, Any]) -> Any:
    """The fixture for `url` without its query, called when it's a function; a 404 `FetchError` for one not given."""
    answer = answers.get(url.split("?")[0], FetchError(f"HTTP 404 for {url}", status=404))
    return answer(url, request) if callable(answer) else answer
