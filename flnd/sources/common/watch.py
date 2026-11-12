"""The JSON responses a page receives, as an iterator: the scroll fallback's way to read a feed.

How it works: `watch_responses` subscribes to the target's tab (`site.page.on_response`) at once,
so a `site.goto` after it misses nothing of the first load. Iterating it yields each matching
response's JSON as it arrives. With `scroll=True`, once nothing is waiting it scrolls the page
(`Page.scroll`, at the source's pace) until a new response comes, and it ends when the feed does:
10 s at the bottom with nothing new. Without, it waits for the network to go quiet once, yields
what came, and ends. Closing it, or the end of a `with` block, stops the subscription.

    with watch_responses(site, "graphql", scroll=True) as responses:
        if site.goto(f"https://www.example.com/{user}/"):
            for data in responses:
                yield from posts_of(data)

`request` is the request of the response last yielded (Playwright's `Request`: its `url`, `method`,
`post_data` and `headers`), so a source can replay the page's own pagination request with another
cursor (`flnd/sources/common/replay.py`) instead of scrolling on.

It is a source helper, not a `Site` member (user, 2026-11-12): only the scroll fallbacks need it.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, Iterator

from shared import browser

if TYPE_CHECKING:
    from flnd.source import Site
    from shared._browser import Match

__all__ = ["Responses", "watch_responses"]

# The longest wait, in seconds, for the network to go quiet when not scrolling: a page's own
# requests settle well within it, and a page that never settles has sent what it will.
QUIET = 10.0


class Responses:
    """The iterator `watch_responses` returns: each matching response's JSON, until the feed ends.

    Args:
        site: the target's site, whose tab is watched.
        match: a substring of the response's URL, a compiled regex searched in it, or a predicate over it.
        scroll: scroll the page for more while nothing is waiting.
        lines: read each body as newline-delimited JSON (Facebook's), one item per line.
    """

    def __init__(self, site: Site, match: Match, *, scroll: bool = False, lines: bool = False):
        self._site = site
        self._scroll = scroll
        # (data, Playwright response) pairs, oldest first; the handler runs inside Playwright's waits,
        # on this same thread, so no lock is needed.
        self._waiting: deque[tuple[Any, Any]] = deque()
        # The request of the response last yielded; None before the first.
        self.request: Any = None
        self._subscription = site.page.on_response(
            match, lambda data, response: self._waiting.append((data, response)), lines=lines)
        self._items = self._iterate()

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
        self._subscription.stop()

    def _iterate(self) -> Iterator[Any]:
        """What has arrived, then, scrolling or once the network is quiet, what comes next; see the module."""
        page = self._site.page
        try:
            while True:
                yield from self.arrived()
                if not self._scroll:
                    try:
                        page.wait(idle=True, timeout=QUIET)
                    # A page that never goes quiet (polling, a live feed) has sent its responses by now.
                    except browser.WaitError:
                        pass
                    yield from self.arrived()
                    return
                # Stops as soon as a response is waiting; returns with none when the feed ended.
                page.scroll(until=lambda: bool(self._waiting))
                if not self._waiting:
                    return
        finally:
            self.close()

    def arrived(self) -> Iterator[Any]:
        """What has arrived and is waiting, oldest first, without scrolling or waiting for more.

            for data in responses.arrived(): ...    # the rest of a response's lines, say

        Each response's request is kept in `request` as its data is yielded. The lines of one
        response arrive together, so after one of them, the rest are here already.
        """
        while self._waiting:
            data, response = self._waiting.popleft()
            # A fake tab in the tests hands no response; a real one always does.
            self.request = getattr(response, "request", None)
            yield data


def watch_responses(site: Site, match: Match, *, scroll: bool = False, lines: bool = False) -> Responses:
    """Watch the target's tab for JSON responses whose URL matches, from now on; see the module.

        responses = watch_responses(site, "graphql", scroll=True)
        site.goto(url)
        for data in responses: ...

    Args:
        site: the target's site; its tab is opened if it isn't yet.
        match: a substring of the URL, a compiled regex searched in it, or a predicate over it.
        scroll: scroll the page for more whenever nothing is waiting, until the feed ends.
        lines: read each body as newline-delimited JSON, one item per line.

    Returns:
        An iterator of the responses' JSON, also a context manager that stops watching.

    Use it on the job's thread, as everything on a page: handlers run inside Playwright's waits.
    """
    return Responses(site, match, scroll=scroll, lines=lines)
