"""The JSON responses a page receives, as an iterator: the scroll fallback's way to read a feed.

`watch_responses` subscribes to the target's tab (`site.page.on_response`) immediately,
so a subsequent `site.goto` misses nothing. Iterating it yields each matching response's
JSON as it arrives. With `scroll=True`, it scrolls (`Page.scroll`) until new responses arrive
or the feed ends (10 s at the bottom with nothing new). Closing it stops the subscription.

`request` holds the request of the response last yielded, enabling pagination replays.

    with watch_responses(site, "graphql", scroll=True) as responses:
        if site.goto(f"https://www.example.com/{user}/"):
            for data in responses:
                yield from posts_of(data)
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, Any, Iterator

from shared import browser

if TYPE_CHECKING:
    from flnd.source import Site

__all__ = ["Responses", "watch_responses"]

# The longest wait, in seconds, for the network to go quiet when not scrolling: a page's own
# requests settle well within it, and a page that never settles has sent what it will.
QUIET = 10.0


class Responses:
    """The iterator `watch_responses` returns: matching response JSON until the feed ends."""

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
        self._scroll = scroll
        # (data, Playwright response) pairs, oldest first; the handler runs inside Playwright's
        # waits on this same thread, so no lock is needed.
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
        """Yield arrived responses, scrolling or waiting for network quiet as configured."""
        page = self._site.page
        try:
            while True:
                yield from self.arrived()
                if not self._scroll:
                    try:
                        page.wait(idle=True, timeout=QUIET)
                    # A page that never goes quiet (polling, live feed) has sent its responses.
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
        """What has arrived and is waiting, oldest first, without scrolling or waiting for more."""
        while self._waiting:
            data, response = self._waiting.popleft()
            # A fake tab in the tests hands no response; a real one always does.
            self.request = getattr(response, "request", None)
            yield data


def watch_responses(
    site: Site, match: browser.Match, *, scroll: bool = False, lines: bool = False,
) -> Responses:
    """Watch the target's tab for JSON responses whose URL matches."""
    return Responses(site, match, scroll=scroll, lines=lines)
