"""`FakeSession`: a `Session` for testing sources without the network or a browser.

It records `save`, `write`, requests and visits, answers `fetch_json` and `fetch_html` from
fixtures, and feeds `on_response` handlers through `respond`. pyright checks that it matches
the `Session` protocol (see tests/typing/usage.py).
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Callable, Mapping, MutableMapping, Sequence
from unittest import mock

import httpx

from flnd.net import Download
from flnd.session import Options, Session, check_category, selected, wanted
from flnd.sources import SOURCES
from flnd.sources.base import FetchError, Source
from flnd.target import Target
from shared import selector
from shared._browser import Browser, Match, Page
from shared._selector import Selection

# A fixture: data for a URL, a FetchError to raise, or a function of (url, request) giving either.
Answer = Any


class FakeResponse:
    """What a handler gets as `response`: only its URL."""

    def __init__(self, url: str):
        self.url = url


class FakeSession:
    """A `Session` whose answers come from `json` and `html`, keyed by URL without its query."""

    def __init__(self, target: Target, *, source: type[Source] | None = None, cookies: Mapping[str, str] | None = None,
                 json: Mapping[str, Answer] | None = None, html: Mapping[str, str] | None = None,
                 options: Options = Options(), limit: int | None = None, page_html: str = "",
                 pages: Mapping[str, str] | None = None, on_scroll: Callable[[FakeSession], Any] | None = None):
        self.target = target
        self.folder: str | None = ""
        self.options = options
        # The target's source, for its categories; a test source can be given.
        self.source = source or SOURCES[target.source]
        self.limit = limit
        # The page's HTML: by the URL last visited, or else `page_html`.
        self.page_html = page_html
        self.pages = dict(pages or {})
        self.on_scroll = on_scroll
        self.saved: list[dict[str, Any]] = []
        self.written: dict[str, str] = {}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.visited: list[str] = []
        self.messages: list[str] = []
        self.challenges = 0
        self.state: MutableMapping[str, Any] = {}
        self.secrets: MutableMapping[str, str] = {}
        self.client: Any = None
        # Whether `ask` and `choose` can ask; a test of a login sets it.
        self.interactive = False
        self._cookies = dict(cookies or {})
        self._json = dict(json or {})
        self._html = dict(html or {})
        self._handlers: list[tuple[Callable[[Any], bool], Callable[..., Any]]] = []
        self._raw_page = mock.MagicMock()

    @property
    def cookies(self) -> Mapping[str, str]:
        return self._cookies

    @property
    def categories(self) -> tuple[str, ...]:
        return selected(self.options, self.source)

    @property
    def since(self) -> datetime.date | None:
        return self.options.since

    @property
    def until(self) -> datetime.date | None:
        return self.options.until

    def wants(self, category: str | None = None, date: datetime.date | None = None) -> bool:
        return wanted(self.categories, self.options, category=category, date=date)

    @property
    def http(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(599)))

    def fetch_json(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
                   soft: bool = False, **request: Any) -> Any:
        self.requests.append((method, url, {"headers": dict(headers or {}), **request}))
        answer = self._answer(self._json, url, request)
        if isinstance(answer, FetchError):
            if soft and answer.status in (401, 403, 404):
                return None
            raise answer
        return answer

    def fetch_html(self, url: str, **request: Any) -> Selection:
        self.requests.append((request.pop("method", "GET"), url, request))
        answer = self._answer(self._html, url, request)
        if isinstance(answer, FetchError):
            raise answer
        return selector.select(answer)

    def select(self, value: Any) -> Selection:
        return selector.select(value)

    def open_page(self, url: str) -> Page:
        self.visited.append(url)
        return self.page

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        self.visited.append(url)
        return True

    def on_response(self, match: Match, handler: Callable[[Session, Any, Any], Any], *,
                    lines: bool = False) -> Callable[[], None]:
        return self._listen(match, handler)

    def on_request(self, match: Match, handler: Callable[[Session, Any], Any]) -> Callable[[], None]:
        return self._listen(match, handler)

    def respond(self, url: str, data: Any) -> None:
        """Feed `data` to every handler whose match takes `url`, as the browser would."""
        response = FakeResponse(url)
        for matches, handler in list(self._handlers):
            if matches(response):
                handler(self, data, response)

    def scroll(self, *, by: str | Callable[[Any], Any] | None = None, count: Callable[[], int] | None = None,
               more: Callable[[], bool] | None = None, idle: int = 4, delay: float = 2.0, max: int = 200) -> int:
        if self.on_scroll is not None:
            self.on_scroll(self)
        return 1

    def wait_for_selector(self, css: str, *, timeout: float = 30.0) -> None:
        pass

    def wait_for_url(self, pattern: Any, *, timeout: float = 30.0) -> None:
        pass

    def wait_for_load(self, *, idle: bool = False, timeout: float = 30.0) -> None:
        pass

    def handoff(self, *, until: Callable[[], bool], message: str, poll: float = 1.0,
                timeout: float | None = None) -> bool:
        return until()

    @property
    def page(self) -> Page:
        url = self.visited[-1] if self.visited else "about:blank"
        self._raw_page.content.return_value = self.pages.get(url, self.page_html)
        self._raw_page.url = url
        return Page(self._raw_page, mock.MagicMock())

    @property
    def browser(self) -> Browser:
        raise RuntimeError("FakeSession has no browser")

    def save(self, source: Download, name: str, *, category: str, date: datetime.date | None = None,
             folder: str | None = None, key: str | None = None, password: str | None = None,
             size: int | None = None, headers: Mapping[str, str] | None = None, audio: str | None = None) -> bool:
        check_category(self.source(), category)
        folder = self.folder if folder is None else folder
        if folder is None or self.full or not self.wants(category=category, date=date):
            return False
        path = f"{folder}/{name}" if folder else name
        if any(item["path"] == path or (key and item["key"] == key) for item in self.saved):
            return False
        self.saved.append({"source": source, "path": path, "category": category, "date": date, "key": key,
                           "password": password, "size": size, "headers": dict(headers or {}), "audio": audio})
        return True

    def write(self, name: str, text: str) -> bool:
        if self.folder is None:
            return False
        self.written[f"{self.folder}/{name}" if self.folder else name] = text
        return True

    @property
    def full(self) -> bool:
        return self.limit is not None and len(self.saved) >= self.limit

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        raise AssertionError(f"a source asked during a download: {message}")

    def choose(self, message: str, options: Sequence[str] | Mapping[str, str]) -> str:
        raise AssertionError(f"a source asked during a download: {message}")

    def info(self, message: str, **fields: Any) -> None:
        self.messages.append(message.format(**fields))

    def warn(self, message: str, **fields: Any) -> None:
        self.messages.append("! " + message.format(**fields))

    def challenged(self) -> None:
        self.challenges += 1

    def paths(self) -> list[str]:
        """Where each saved file would land, under the target's folder."""
        return [item["path"] for item in self.saved]

    def _listen(self, match: Match, handler: Callable[..., Any]) -> Callable[[], None]:
        entry = (_matcher(match), handler)
        self._handlers.append(entry)
        return lambda: self._handlers.remove(entry) if entry in self._handlers else None

    def _answer(self, answers: Mapping[str, Answer], url: str, request: dict[str, Any]) -> Any:
        answer = answers.get(url.split("?")[0], FetchError(f"HTTP 404 for {url}", status=404))
        return answer(url, request) if callable(answer) else answer


def _matcher(match: Match) -> Callable[[Any], bool]:
    if isinstance(match, str):
        return lambda response: match in response.url
    if isinstance(match, re.Pattern):
        return lambda response: match.search(response.url) is not None
    return match
