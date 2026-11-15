"""Scroll and response-watching helpers for browser-driven feeds."""

from __future__ import annotations

import json
import re
from collections import deque
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from shared import browser

if TYPE_CHECKING:
    from flnd.source import Site

__all__ = ["Responses", "warm_tabs", "watch_responses"]

# Longest wait in seconds for network quiet when not scrolling.
QUIET = 10.0


class Responses:
    """Iterator over matching JSON responses on a target's browser tab."""

    def __init__(
        self,
        site: Site,
        match: browser.Match,
        *,
        scroll: bool = False,
        lines: bool = False,
    ):
        """Subscribe to matching responses on the target's page."""
        self._site = site
        self._match = match
        self._scroll = scroll
        self._lines = lines
        self._closed = False
        self._waiting: deque[tuple[Any, Any]] = deque()
        self.request: Any = None
        push = getattr(site, "_push_tab", None)
        if callable(push):
            push(self, scroll=scroll)
        self._page = site.page
        self._subscribe(self._page)
        self._items = self._iterate()

    def _subscribe(self, page: browser.Page) -> None:
        """Attach the response listener to `page.raw`."""
        raw = getattr(page, "raw", None)
        if raw is not None and callable(getattr(raw, "on", None)):
            raw.on("response", self._on_response)

    def _unsubscribe(self, page: browser.Page) -> None:
        """Detach the response listener from `page.raw`."""
        raw = getattr(page, "raw", None)
        if raw is not None and callable(getattr(raw, "remove_listener", None)):
            raw.remove_listener("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        """Buffer matching JSON bodies from `response`."""
        if not _matches(self._match, getattr(response, "url", "")):
            return
        try:
            body = response.text()
        except Exception:
            return
        self._enqueue(response, body)

    def _enqueue(self, response: Any, body: str) -> None:
        """Parse `body` (or NDJSON lines) and append each JSON payload."""
        pieces = [line for line in body.splitlines() if line.strip()] if self._lines else [body]
        for piece in pieces:
            try:
                parsed = json.loads(piece)
            except ValueError:
                continue
            self._waiting.append((parsed, response))

    def _adopt(self, page: browser.Page) -> None:
        """Move the active response subscription onto `page` after a tab switch."""
        if self._closed or page is self._page:
            return
        self._unsubscribe(self._page)
        self._page = page
        take_warm = getattr(self._site, "_take_warm_responses", None)
        if callable(take_warm):
            captured: Any = take_warm(page)
            for response, body in captured:
                if _matches(self._match, getattr(response, "url", "")):
                    self._enqueue(response, body)
        self._subscribe(page)

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        return next(self._items)

    def __enter__(self) -> Responses:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop watching; a second call does nothing."""
        if self._closed:
            return
        self._closed = True
        self._unsubscribe(self._page)
        pop = getattr(self._site, "_pop_tab", None)
        if callable(pop):
            pop(self)

    def _iterate(self) -> Iterator[Any]:
        """Yield arrived responses, scrolling or waiting for network quiet as configured."""
        try:
            while True:
                yield from self.arrived()
                if not self._scroll:
                    try:
                        self._page.wait(idle=True, timeout=QUIET)
                    except browser.WaitError:
                        pass
                    yield from self.arrived()
                    return
                self._page.scroll(until=lambda: bool(self._waiting))
                if not self._waiting:
                    return
        finally:
            self.close()

    def arrived(self) -> Iterator[Any]:
        """Yield waiting responses oldest first without scrolling or waiting for more."""
        while self._waiting:
            data, response = self._waiting.popleft()
            self.request = getattr(response, "request", None)
            yield data


def warm_tabs(site: Site, urls: Iterable[str]) -> None:
    """Open and begin loading `urls` in background tabs inside `site`'s browser window."""
    warm = getattr(site, "_warm_tabs", None)
    if callable(warm):
        warm(urls)


def _matches(match: browser.Match, url: str) -> bool:
    """Whether `url` matches a `watch_responses` filter."""
    if isinstance(match, str):
        return match in url
    if isinstance(match, re.Pattern):
        return match.search(url) is not None
    return bool(match(url))


def watch_responses(
    site: Site, match: browser.Match, *, scroll: bool = False, lines: bool = False,
) -> Responses:
    """Watch the target's tab for JSON responses whose URL matches."""
    return Responses(site, match, scroll=scroll, lines=lines)
