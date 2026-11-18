"""The test kit for sources: run one on fixtures, with no network, no browser and no clock.

src/docs/writing-a-source explains it.

    from flnd.testing import FakeSite, check, collect

    check(Example)                                      # the class, and its routes' examples
    saved = collect(Example, "exm:example_user", json=FIXTURES)
    assert saved[0].path == "posts/2026-01-02_p1_0.jpg"

It is one of flnd's three public modules, next to `flnd` (the library) and `flnd.source` (the
source API), and it is how a plugin's own tests are written.

What it holds:
- `FakeCtx`, a `flnd.plugin.Ctx` for a plugin on API 2 that answers from `Reply`s and
  `FakePage`s the way the engine would: it signs, holds on blocks and walls, and raises the
  engine's errors;
- `FakeSite`, a `flnd.source.Site` that answers `fetch` and `pages` from fixtures keyed by URL
  (its query left out), shows the `pages` fixture as the browser's page after `open`, hands the
  `feeds` fixture's responses to its feeds, and records what the source did in a `Seen`;
- `collect`, which resolves a target, runs its route and categories on a `FakeSite` as the
  engine does, lists each lazy post's files, and names and keys every file the way a download
  would: a `Collected` per file, nothing saved;
- `check`, which checks a source class and resolves every example target its routes'
  docstrings give;
- `fake_clock`, a clock that only moves when something sleeps on it.

Who calls it: a source's tests, flnd's own tests (`tests/fakes.py` builds on it), and the
doctests of `flnd.source`. What it calls: the engine's own naming and listing code
(`flnd.engine.crawl.posts` and `flnd.engine.crawl.place`, `flnd.engine.core.resolve`,
`flnd.engine.site.paged`), imported when
a function first needs it, so a source's test runs the same code a download does.

Rules a change must keep:
- Nothing here touches the network, a browser, a home folder or the real clock.
- A `FakeSite` has exactly the members a `Site` has, plus `seen`, `listing` (set by the engine
  while it lists a category) and what its constructor takes, so a source that passes its tests
  here uses nothing a real site lacks.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import inspect
import json as jsonlib
import re
from collections import deque
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

import flnd.engine.ctx
import flnd.plugin
from flnd.engine import jobs
from flnd.errors import Changed, NetworkError
from flnd.plugin import Document, Landed, Plugin, Request, Response
from flnd.source import (
    File,
    NotFound,
    PluginError,
    Post,
    Range,
    SiteError,
    Source,
    Target,
    UsageError,
)
from flnkit import clock

if TYPE_CHECKING:
    from flnd.engine.options import Options
    from flnkit import net

__all__ = [
    "Collected", "FakeCtx", "FakePage", "FakeSite", "Reply", "Seen", "Sent", "check",
    "collect", "fake_clock",
]

# A fixture: data for a URL, a `SiteError` to raise, or a function of `(url, request)` giving
# either; `request` holds the call's `params`, `body` and other options.
Answer = Any
# A backticked word in a route's docstring: an example target when it is one of the source's.
EXAMPLE = re.compile(r"`([^`\s]+)`")


# ---------------------------------------------------------------------------------------------
# The fake site
# ---------------------------------------------------------------------------------------------

@dataclass
class Seen:
    """What a plugin did through a `FakeCtx` (or a source through a `FakeSite`).

    A `FakeCtx` fills `requests`, `visited`, `scrolls`, `logs`, `holds` and `warmed`, and
    `collect` its `failed`; a `FakeSite` fills `messages`, `back_offs` and `warmed` instead.

    Example:
        >>> site = FakeSite(json={"https://api.example.com/me": {"id": 1}})
        >>> site.fetch("https://api.example.com/me", params={"x": 1}).json
        {'id': 1}
        >>> method, url, options = site.seen.requests[0]
        >>> method, url, options["params"]
        ('GET', 'https://api.example.com/me', {'x': 1})
    """

    # (method, url, options) of each `fetch`: options hold `headers`, `in_page` and the call's
    # own (`params`, `body`); a `FakeCtx`'s also hold `bucket`, and its headers are signed.
    requests: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    # Each URL `open` loaded.
    visited: list[str] = field(default_factory=list)
    # How many times a feed scrolled for more.
    scrolls: int = 0
    # Each `info` line, and each `warn` line after "! ".
    messages: list[str] = field(default_factory=list)
    # The reason of each `back_off`.
    back_offs: list[str] = field(default_factory=list)
    # Each URL `warm` was asked to load.
    warmed: list[str] = field(default_factory=list)
    # Each `ctx.log` line of a `FakeCtx`, as (level, text).
    logs: list[tuple[str, str]] = field(default_factory=list)
    # Each hold a `FakeCtx` took, as (reason, seconds, bucket): a block, a 429, a wall's wait.
    holds: list[tuple[str, float, str | None]] = field(default_factory=list)
    # Each post `collect` gave up on, as (post id, error): its lazy files were gone or unreadable.
    failed: list[tuple[str, Exception]] = field(default_factory=list)


class FakeSite:
    """A `flnd.source.Site` that answers from fixtures, and records what the source did.

    `fetch` answers a URL (without its query) from `json`, else from `html`; a URL in neither
    raises a 404 `NotFound`, as a missing page would. A fixture may be a `SiteError` to raise,
    or a function of `(url, request)` that gives the data or the error. The browser shows the
    `pages` fixture of the URL last opened, and every feed gets the `feeds` fixture's responses
    whose URL it matches, when it scrolls or waits. Nothing sleeps: `pause` and `back_off`
    return at once.

    Args:
        target: the target it acts for; None for one `collect` sets.
        json: the JSON fixtures, by URL.
        html: the text fixtures, by URL, for pages fetched rather than opened.
        pages: the HTML `document()` shows after `open` loaded a URL; "" for one not listed.
        feeds: (URL, JSON) responses the browser would see, in order. Each goes to the open
            feeds whose match takes its URL, the next time one scrolls or waits; one nobody
            watches for stays until somebody does.
        cookies: the login's cookies, `{name: value}`.
        time: `--when`'s days, which `time` gives.
        client: what `client` gives, such as a fake SDK client.
        state: the source's saved state, shared by sites given the same dict.
        seen: where it records; a new `Seen` when None.

    Example:
        >>> site = FakeSite(Target("exm:example_user", kind="user"),
        ...                 json={"https://api.example.com/users/example_user": {"id": "42"}})
        >>> site.fetch("https://api.example.com/users/example_user").json
        {'id': '42'}
        >>> site.fetch("https://api.example.com/nope", check=False).status
        404
    """

    def __init__(self, target: Target | None = None, *, json: Mapping[str, Answer] | None = None,
                 html: Mapping[str, Answer] | None = None,
                 pages: Mapping[str, str] | None = None, feeds: Iterable[tuple[str, Any]] = (),
                 cookies: Mapping[str, str] | None = None, time: Range = Range(),
                 client: Any = None, state: MutableMapping[str, Any] | None = None,
                 seen: Seen | None = None) -> None:
        # The target it acts for; `collect` sets it.
        self.target = target if target is not None else Target("")
        # What `connect` would have given.
        self.client = client
        # The source's state and secrets, as a real site keeps them in the home.
        self.state: MutableMapping[str, Any] = state if state is not None else {}
        self.secrets: MutableMapping[str, str] = {}
        # Whether `ask` may ask: never, in a test.
        self.interactive = False
        # The category being listed, set by the engine's listing code, which `pages` resumes.
        self.listing: Any = None
        self._seen = seen if seen is not None else Seen()
        self._cookies = dict(cookies or {})
        self._json, self._html, self._pages = dict(json or {}), dict(html or {}), dict(pages or {})
        self._time = time
        self._pending = list(feeds)
        self._feeds: list[_FakeFeed] = []

    @property
    def seen(self) -> Seen:
        """What the source did through this site so far."""
        return self._seen

    @property
    def cookies(self) -> Mapping[str, str]:
        """The login's cookies, as the constructor got them."""
        return self._cookies

    @property
    def time(self) -> Range:
        """`--when`'s days, as the constructor got them."""
        return self._time

    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              check: bool = True, in_page: bool = False, **request: Any) -> net.Response:
        """Answer one request from the fixtures, and record it in `seen.requests`.

        Args:
            url: the URL; its query is left out when the fixtures are looked up.
            method: the HTTP method, recorded.
            headers: the request's own headers, recorded.
            check: raise a fixture's error; False returns its response instead.
            in_page: whether the source asked to send it from the tab, recorded.
            **request: `params`, `body` and the like, recorded and passed to a function fixture.

        Returns:
            A 200 response with the fixture's JSON or text; with `check=False`, an error
            fixture's response.

        Raises:
            SiteError: the fixture is one (a 404 `NotFound` for a URL with no fixture).

        Example:
            >>> site = FakeSite(html={"https://www.example.com/": "<h1>Hi</h1>"})
            >>> site.fetch("https://www.example.com/?ref=1").text
            '<h1>Hi</h1>'
        """
        from flnkit import net

        self._seen.requests.append(
            (method, url, {"headers": dict(headers or {}), "in_page": in_page, **request}))
        if url.split("?")[0] in self._json:
            answer = _answer(self._json, url, request)
            if not isinstance(answer, SiteError):
                return net.Response(200, url, {"content-type": "application/json"},
                                     jsonlib.dumps(answer).encode())
        else:
            answer = _answer(self._html, url, request)
            if not isinstance(answer, SiteError):
                return net.Response(200, url, {"content-type": "text/html"},
                                     str(answer).encode())
        if not check and answer.status is not None:
            return answer.response or net.Response(answer.status, url, {})
        raise answer

    def pages(self, url: str, *, cursor: str | Callable[[Any], Any],
              params: Mapping[str, Any] | None = None,
              send: str | Callable[[Any], Mapping[str, Any]] = "cursor") -> Iterator[Any]:
        """Each page of a paged listing, through `fetch`, as the engine's `Site.pages` reads it.

        Args:
            url: the listing's URL.
            cursor: where a page says what comes next (see `flnd.source.Site.pages`).
            params: the query every request sends.
            send: where the cursor goes in the next request.

        Returns:
            The pages, each the fixture's JSON; a function fixture can tell them apart by the
            cursor in its `request["params"]`.

        Example:
            >>> pages = {"https://api.example.com/posts": lambda url, request: (
            ...     {"items": [2], "next": None} if request["params"]
            ...     else {"items": [1], "next": "c2"})}
            >>> site = FakeSite(json=pages)
            >>> [page["items"] for page in site.pages("https://api.example.com/posts",
            ...                                       cursor="next")]
            [[1], [2]]
        """
        from flnd.engine.site import paged

        return paged(self.fetch, url, cursor=cursor, params=params, send=send,
                     listing=self.listing)

    def open(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        """Load a page: `document()` then shows its `pages` fixture.

        Args:
            url: the page, recorded in `seen.visited`.
            idle: ignored: a fake page is loaded at once.
            timeout: ignored.

        Returns:
            True, always.

        Example:
            >>> site = FakeSite(pages={"https://www.example.com/": "<h1>Hi</h1>"})
            >>> site.open("https://www.example.com/"), site.url
            (True, 'https://www.example.com/')
        """
        self._seen.visited.append(url)
        return True

    @property
    def url(self) -> str:
        """The page last opened; "about:blank" before any."""
        return self._seen.visited[-1] if self._seen.visited else "about:blank"

    def wait(self, css: str | None = None, *,
             url: str | re.Pattern[str] | Callable[[str], bool] | None = None,
             idle: bool = False, timeout: float = 30.0) -> None:
        """Return at once: a fake page has everything it will ever have.

        Args:
            css: ignored.
            url: ignored.
            idle: ignored.
            timeout: ignored.

        Example:
            >>> FakeSite().wait("h1", timeout=1)
        """

    def scroll(self, *, until: Callable[[], bool] | None = None, idle: float = 2.0) -> int:
        """Scroll nothing: a fake page has no more to load.

        Args:
            until: ignored.
            idle: ignored.

        Returns:
            0 scrolls.

        Example:
            >>> FakeSite().scroll()
            0
        """
        return 0

    def feed(self, match: str | re.Pattern[str] | Callable[[str], bool], *, scroll: bool = False,
             lines: bool = False,
             cursor: Callable[[Any], tuple[bool, str | None] | None] | None = None) -> Any:
        """Watch for the `feeds` fixture's responses whose URL matches, as `Site.feed` does.

        Args:
            match: a substring of the URL, a compiled regex searched in it, or a predicate.
            scroll: ask for more (counted in `seen.scrolls`) until none comes, or `cursor`
                reads the end; without it, take what is there once.
            lines: ignored: a fixture's response is one parsed value already.
            cursor: reads each value as the engine's feed does: `(more, cursor)`, or None.

        Returns:
            The feed: iterate it, and close it (or use it in a `with`).

        Example:
            >>> site = FakeSite(feeds=[("https://www.example.com/api/posts?page=1", {"n": 1})])
            >>> with site.feed("/api/posts", scroll=True) as pages:
            ...     list(pages)
            [{'n': 1}]
        """
        return _FakeFeed(self, match, scroll=scroll, cursor=cursor)

    def warm(self, urls: Iterable[str]) -> None:
        """Record the pages the source will open next, in `seen.warmed`.

        Args:
            urls: the pages.

        Example:
            >>> site = FakeSite()
            >>> site.warm(["https://www.example.com/a"])
            >>> site.seen.warmed
            ['https://www.example.com/a']
        """
        self._seen.warmed.extend(urls)

    def document(self) -> Any:
        """The page last opened, parsed from its `pages` fixture.

        Returns:
            The page, with `css`, `first`, `script`, `blocks`, `text` and `visible`.

        Example:
            >>> site = FakeSite(pages={"https://www.example.com/": "<h1>Hi</h1>"})
            >>> site.open("https://www.example.com/")
            True
            >>> site.document().first("h1").text
            'Hi'
        """
        from flnkit import html

        # The fixture of the URL last opened, even where a subclass reports a redirect as `url`.
        last = self._seen.visited[-1] if self._seen.visited else ""
        return html.parse(self._pages.get(last, ""))

    def evaluate(self, js: str, *, arg: Any = None) -> Any:
        """Run nothing: a fake page has no script.

        Args:
            js: ignored.
            arg: ignored.

        Returns:
            None.

        Example:
            >>> FakeSite().evaluate("document.title") is None
            True
        """
        return None

    def click(self, css: str, *, timeout: float = 10.0) -> None:
        """Click nothing, at once.

        Args:
            css: ignored.
            timeout: ignored.

        Example:
            >>> FakeSite().click("button.more")
        """

    def handoff(self, *, until: Callable[[], bool], message: str,
                timeout: float | None = None) -> bool:
        """Ask nobody: whether the step is done already.

        Args:
            until: asked once.
            message: ignored.
            timeout: ignored.

        Returns:
            What `until()` says.

        Example:
            >>> FakeSite().handoff(until=lambda: True, message="solve the captcha")
            True
        """
        return until()

    @property
    async def tab(self) -> Any:
        """A fake site has no Playwright page: awaiting this raises `NotImplementedError`."""
        raise NotImplementedError("FakeSite has no Playwright page")

    def html(self, text: str | bytes) -> Any:
        """Parse HTML or JSON text, as `Site.html` does.

        Args:
            text: the text.

        Returns:
            The parsed document, as `flnd.source.Site.html` gives it.

        Example:
            >>> FakeSite().html("<p>one</p>").first("p").text
            'one'
        """
        from flnkit import html

        return html.parse(text)

    def pause(self, seconds: float, reason: str) -> None:
        """Return at once.

        Args:
            seconds: ignored.
            reason: ignored.

        Example:
            >>> FakeSite().pause(5, "the page settles")
        """

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        """Record the block in `seen.back_offs`, and return at once.

        Args:
            reason: what the site did.
            retry_after: ignored.

        Example:
            >>> site = FakeSite()
            >>> site.back_off("please wait")
            >>> site.seen.back_offs
            ['please wait']
        """
        self._seen.back_offs.append(reason)

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        """Refuse: nobody answers during a test.

        Args:
            message: the question.
            secret: ignored.
            description: ignored.

        Raises:
            UsageError: always.

        Example:
            >>> FakeSite(Target("exm:example_user")).ask("Code")
            Traceback (most recent call last):
            flnkit.errors.UsageError: exm: a run can't ask 'Code'
        """
        raise UsageError(f"{self.target.plugin}: a run can't ask {message!r}")

    def info(self, message: str, **fields: Any) -> None:
        """Record a line in `seen.messages`.

        Args:
            message: the text, with `{name}` fields.
            **fields: the fields' values.

        Example:
            >>> site = FakeSite()
            >>> site.info("{n} stories", n=3)
            >>> site.seen.messages
            ['3 stories']
        """
        self._seen.messages.append(message.format(**fields))

    def warn(self, message: str, **fields: Any) -> None:
        """Record a warning in `seen.messages`, after "! ".

        Args:
            message: the text, with `{name}` fields.
            **fields: the fields' values.

        Example:
            >>> site = FakeSite()
            >>> site.warn("{n} posts hidden", n=2)
            >>> site.seen.messages
            ['! 2 posts hidden']
        """
        self._seen.messages.append("! " + message.format(**fields))

    def _deliver(self, *, scroll: bool) -> None:
        """A feed scrolled (counted in `seen`) or waited: `_release` what is pending."""
        if scroll:
            self._seen.scrolls += 1
        self._release()

    def _release(self) -> None:
        """Hand each pending response to every open feed that matches it."""
        for url, data in list(self._pending):
            if [feed for feed in list(self._feeds) if feed._push(url, data)]:
                self._pending.remove((url, data))


class _FakeFeed:
    """What `FakeSite.feed` returns: the `feeds` fixture's matching responses, as a feed gives them.

    Iterating yields what is waiting. Then, with `scroll`, it asks the site to deliver once
    more, and ends when nothing new came, or when `cursor` read the end (as the engine's feed
    reads it, `flnkit.browser.Cursors`); without `scroll` it asks once and ends, as the engine's
    feed waits once for quiet.

    Args:
        site: the site whose `feeds` fixture it reads.
        match: what a response's URL must match.
        scroll: whether it scrolls for more.
        cursor: the source's cursor reader, or None.
    """

    def __init__(self, site: FakeSite, match: Any, *, scroll: bool = False,
                 cursor: Any = None) -> None:
        from flnkit import browser

        self._site = site
        self._matches = browser.matcher(match)
        self._scroll = scroll
        self._cursors = browser.Cursors(cursor)
        self._waiting: deque[Any] = deque()
        self._closed = False
        site._feeds.append(self)
        self._items = self._iterate()

    def _push(self, url: str, data: Any) -> bool:
        """Take one response if it matches and the feed is open.

        Args:
            url: the response's URL.
            data: its parsed JSON.

        Returns:
            Whether the feed took it.
        """
        if self._closed or not self._matches(url):
            return False
        self._waiting.append(data)
        self._cursors.read(data)
        return True

    def __iter__(self) -> _FakeFeed:
        return self

    def __next__(self) -> Any:
        return next(self._items)

    def __enter__(self) -> _FakeFeed:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop watching; a second call does nothing."""
        if not self._closed:
            self._closed = True
            self._site._feeds.remove(self)

    def _iterate(self) -> Iterator[Any]:
        """The responses, as described in the class's docstring.

        Yields:
            Each response's JSON, in the order the site sent them.
        """
        try:
            while True:
                while self._waiting:
                    yield self._waiting.popleft()
                # The site said there is no more: no scroll asks for it.
                if self._scroll and self._cursors.ended:
                    return
                self._site._deliver(scroll=self._scroll)
                if not self._scroll:
                    while self._waiting:
                        yield self._waiting.popleft()
                    return
                if not self._waiting:
                    return
        finally:
            self.close()


def _answer(answers: Mapping[str, Answer], url: str, request: dict[str, Any]) -> Any:
    """The fixture for `url` without its query, called when it's a function.

    Args:
        answers: the fixtures, by URL.
        url: the URL asked for.
        request: the call's options, which a function fixture receives.

    Returns:
        The data or the error; a 404 `NotFound` for a URL with no fixture.
    """
    answer = answers.get(url.split("?")[0], NotFound(f"HTTP 404 for {url}", status=404))
    return answer(url, request) if callable(answer) else answer


# ---------------------------------------------------------------------------------------------
# The fake context, for plugin API 2
# ---------------------------------------------------------------------------------------------

# What `ctx.log` takes as its level.
LEVELS = ("debug", "info", "warn")
# What a lazy post's files may raise that fails only that post, never its listing.
LAZY_FAILURES: tuple[type[Exception], ...] = (NotFound, Changed, flnd.errors.Hidden)
T = TypeVar("T")


@dataclass(frozen=True)
class Reply:
    """One answer of a `FakeCtx`'s `fetch` or `pages`, for a URL of its `routes`.

    Args:
        body: bytes as they are, text as UTF-8 HTML, anything else as JSON; None is no body.
        status: the status code.
        headers: its headers; a text or JSON body gets its Content-Type by itself.
        url: where it landed after redirects; "" is the URL asked for.
    """

    body: Any = None
    status: int = field(default=200, kw_only=True)
    headers: Mapping[str, str] | None = field(default=None, kw_only=True)
    url: str = field(default="", kw_only=True)


@dataclass(frozen=True)
class FakePage:
    """One page a `FakeCtx` shows after `open`: its HTML, its status, where it landed, its feeds.

    Args:
        html: what `document` parses.
        status: the main document's status code.
        url: where the tab landed after redirects; "" is the URL opened.
        feeds: the responses the page sends: a `Sent`, or a (URL, JSON) pair sent as the page
            loads. Each goes to the feeds of the tab listening then whose match takes its
            URL; one nobody hears is lost.
    """

    html: str = ""
    status: int = field(default=200, kw_only=True)
    url: str = field(default="", kw_only=True)
    feeds: Sequence[Sent | tuple[str, Any]] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class Sent:
    """One response a `FakePage` sends to the tab's feeds: as it loads, on a click or a scroll.

    Args:
        url: the response's URL, which a feed's match reads.
        body: its parsed JSON.
        click: sent when `ctx.click` clicks this CSS selector on the page; None for none.
        scroll: sent on the page's n-th scroll (from 1) by a scrolling feed; None for none.
            With neither, it is sent as the page loads.

    Raises:
        PluginError: both `click` and `scroll`, or a `scroll` under 1.
    """

    url: str
    body: Any
    click: str | None = field(default=None, kw_only=True)
    scroll: int | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.click is not None and self.scroll is not None:
            raise PluginError(f"Sent({self.url!r}): give click or scroll, not both")
        if self.scroll is not None and (isinstance(self.scroll, bool) or self.scroll < 1):
            raise PluginError(f"Sent({self.url!r}): scroll counts from 1, not {self.scroll!r}")


class FakeCtx:
    """A `flnd.plugin.Ctx` that answers from fakes, run by the engine's own scheduler.

    Each request is a page job (a file job off the plugin's domains), each `open` a tab job,
    and each `call` a call job of a real `flnd.engine.jobs.Scheduler`, on the active
    `fake_clock` or a fake clock of its own: its pace, holds, block rounds, wall waits and
    retries are the engine's, and pass at once. What the fake adds:
    - `fetch` signs a request to the plugin's domains, then answers from `routes`, by the
      whole URL or without its query: a `Reply`, or a function of the `Request` giving one.
      A URL with no route fails the test (`AssertionError`).
    - `blocked` reads each answer from the plugin's domains; seconds are a block round. A 429
      holds its bucket for `Retry-After` or `x-rate-limit-reset`.
    - Landing on one of the plugin's `walls` (`open`'s page, or a fetch redirected there) is
      the scheduler's wall: held 30 s, then 120 s, then `LoginError`.
    - With the request's `check` on, a 401 or 403 from the plugin's domains raises
      `LoginError`, a 404 or 410 `NotFound`, and another refusal `SiteError`; with it off,
      the refusal's answer is returned.
    - `open` shows the `pages` entry of the URL and returns `Landed` with its status, raising
      as a checked `fetch` does. What the page sends (`Sent`) goes to the tab's feeds
      listening then: as it loads, on a `click`, or on a feed's scroll.
    - `ask` pops `answers`: one of `choices` when it has any; `UsageError` when they run out.
    - `extension` and `storage` read their fakes; with no `extension`, `UsageError`.
    - `state` takes JSON values only, and `secrets` may be written only in `Login.start`.

    Every hold is recorded in `seen.holds`.

    Args:
        plugin: the plugin class, or an instance of it.
        target: the target the plugin acts for, as people write it; `collect` sets it.
        routes: the HTTP answers, by URL.
        pages: the pages `open` shows, by URL: a `FakePage`, or its HTML.
        account: the account's name; None runs with no login.
        cookies: the account's cookies on the plugin's domains.
        when: the run's `when`.
        answers: what `ask` answers, in order.
        state: the plugin's saved state, shared by contexts given the same dict.
        storage: the `localStorage` that `storage` reads.
        extension: the browser extension's answers, by op: a value, or a function of the
            op's arguments giving it; None is no extension connected.
        settings: the plugin's `Settings` fields that differ from their defaults.

    Raises:
        UsageError: `settings` names a field the plugin's `Settings` lacks.
    """

    def __init__(self, plugin: type[Plugin] | Plugin, target: str | flnd.plugin.Target = "", *,
                 routes: Mapping[str, Reply | Callable[[Request], Any]] | None = None,
                 pages: Mapping[str, FakePage | str] | None = None,
                 account: str | None = None, cookies: Mapping[str, str] | None = None,
                 when: flnd.plugin.Range = flnd.plugin.Range(), answers: Sequence[str] = (),
                 state: MutableMapping[str, Any] | None = None,
                 storage: Mapping[str, str] | None = None,
                 extension: Mapping[str, Any] | None = None,
                 settings: Mapping[str, Any] | None = None) -> None:
        if isinstance(plugin, type) and issubclass(plugin, Plugin):
            plugin = plugin()
        if not isinstance(plugin, Plugin) or not getattr(plugin, "code", None):
            raise PluginError(f"FakeCtx takes a flnd.plugin.Plugin class with a code, not "
                              f"{plugin!r}")
        # What the plugin did: its requests, pages, scrolls, log lines and holds.
        self.seen: Seen = Seen()
        self._plugin = plugin
        self._target = _resolved(type(plugin), target)
        self._routes, self._pages = dict(routes or {}), dict(pages or {})
        self._account = account
        self._cookies = MappingProxyType(dict(cookies or {}))
        self._when = when
        self._answers = deque(answers)
        self._state = flnd.engine.ctx.JsonState(state if state is not None else {})
        self._secrets = _secrets(plugin)
        self._storage = MappingProxyType(dict(storage or {}))
        self._extension = extension
        try:
            self._settings = type(plugin).Settings(**dict(settings or {}))
        except TypeError as error:
            raise UsageError(f"{plugin.code}: settings: {error}") from None
        self._fake = clock.Fake()
        self._jobs = _Scheduler(self.seen, pace={plugin.code: plugin.pace})
        # The tabs open: the target's, then one per lazy post's files, `date` or `refresh`.
        self._tabs = [_Tab()]
        # The last answer each URL (without its query) got, which naming a file may read.
        self._answered: dict[str, Any] = {}

    # -- HTTP --------------------------------------------------------------------------------

    async def fetch(self, request: str | Request, *, bucket: str | None = None,
                    in_page: bool = False) -> Response:
        """Answer one request from `routes`, as the engine's page (or file) job would.

        Raises:
            NotFound: a 404 or 410, while `request.check`.
            LoginError: a 401 or 403 from the plugin's domains while `request.check`, or a
                redirect to a wall after the wall's waits.
            Blocked: still refused after the block rounds.
            SiteError: another refusal, while `request.check`. A 5xx is retried with or
                without `check`; without it, the last 5xx's answer is returned.
        """
        asked = request if isinstance(request, Request) else Request(request)
        own = self._own(asked.url)

        async def attempt() -> Any:
            sent = asked
            if own and self._plugin.user_agent:
                # The engine sets the plugin's User-Agent on its own domains, before `sign`.
                sent = sent.replace(headers={**(sent.headers or {}),
                                             "User-Agent": self._plugin.user_agent})
            sent = self._plugin.sign(sent, self._cookies) if own else sent
            self.seen.requests.append((sent.method, sent.url, {
                "params": sent.params, "body": sent.body, "headers": dict(sent.headers or {}),
                "bucket": bucket, "in_page": in_page}))
            try:
                response = flnd.engine.ctx.capped(self._answer(sent))
                landed = self._own(response.url)
                flnd.engine.ctx.refused(self._plugin, response, own=landed)
                if response.url != sent.url:
                    flnd.engine.ctx.walled(self._plugin, response.url, own=landed)
                # A 5xx is the server's trouble, retried even when the plugin reads refusals;
                # a POST's never is, as in the engine.
                if sent.check or response.status >= 500:
                    return flnd.engine.ctx.checked(self._plugin, response, own=landed)
                return response
            except flnd.errors.Error as error:
                return flnd.engine.ctx.once(sent, error)

        kind = jobs.Kind.PAGE if own else jobs.Kind.FILE
        try:
            got = await self._run(kind, attempt, bucket=bucket or "",
                                  host=urlsplit(asked.url).hostname or "")
        except SiteError as error:
            # With `check` off, the last 5xx is handed back once the scheduler's tries ran out.
            if asked.check or (error.status or 0) < 500 or error.response is None:
                raise
            return error.response
        if isinstance(got, flnd.engine.ctx.Once):
            raise got.error
        return got

    def pages(self, request: str | Request, *,
              cursor: str | Callable[[Response], str | None],
              send: str | Callable[[str], Mapping[str, Any]] = "cursor",
              bucket: str | None = None, in_page: bool = False) -> AsyncIterator[Response]:
        """Each page of a paged listing, through `fetch`; it ends on a missing, empty or
        repeated cursor, or an empty page."""
        return self._pages_of(request if isinstance(request, Request) else Request(request),
                              cursor, send, bucket, in_page)

    async def _pages_of(self, first: Request, cursor: Any, send: Any, bucket: str | None,
                        in_page: bool) -> AsyncIterator[Response]:
        at: str | None = None
        seen: set[str] = set()
        while True:
            asked = first
            if at is not None:
                more = send(at) if callable(send) else {send: at}
                asked = first.replace(params={**(first.params or {}), **more})
            response = await self.fetch(asked, bucket=bucket, in_page=in_page)
            if flnd.engine.ctx.empty(response):
                return
            yield response
            following = (cursor(response) if callable(cursor)
                         else flnd.plugin.dig(response.json, cursor))
            if following is None or following == "" or str(following) in seen:
                return
            at = str(following)
            seen.add(at)

    # -- The tab -----------------------------------------------------------------------------

    async def open(self, url: str) -> Landed:
        """Show the `pages` entry of `url` in the tab, as the engine's tab job would.

        Raises:
            NotFound: the page answered 404 or 410.
            LoginError: a 401 or 403 from the plugin's domains, or a wall after its waits.
            Blocked: still refused after the block rounds.
        """
        from flnkit import net

        tab = self._tabs[-1]

        async def attempt() -> Landed:
            self.seen.visited.append(url)
            given = _lookup(self._pages, url)
            if given is None:
                raise AssertionError(f"FakeCtx has no page for {url}")
            page = FakePage(given) if isinstance(given, str) else given
            landed = page.url or url
            response = net.Response(page.status, landed, {"content-type": "text/html"},
                                    page.html.encode())
            own = self._own(landed)
            flnd.engine.ctx.refused(self._plugin, response, own=own)
            flnd.engine.ctx.walled(self._plugin, landed, own=own)
            flnd.engine.ctx.checked(self._plugin, response, own=own, every=False)
            tab.load(page)
            return Landed(landed, page.status)

        return await self._run(jobs.Kind.TAB, attempt, host=urlsplit(url).hostname or "")

    def feed(self, match: str | re.Pattern[str] | Callable[[str], bool], *, scroll: bool = True,
             lines: bool = False,
             cursor: Callable[[Any], tuple[bool, str | None] | None] | None = None
             ) -> AsyncIterator[Any]:
        """The JSON of the tab's feed responses whose URL matches, from this call on.

        It hears only what the tab's page sends after the call, as a real tab's listener
        does: use it with `async with` before `open` or `click`. When none is waiting, a
        scrolling feed scrolls the page once (counted in `seen.scrolls`), which sends the
        page's `Sent` of that scroll; a scroll that brings it nothing ends it, and so does
        `cursor` reading the end.

        Args:
            match: a substring of the URL, a compiled regex searched in it, or a predicate.
            scroll: scroll for more when none is waiting; without it, take what came.
            lines: ignored: a fake response is one parsed value already.
            cursor: reads each value: `(more, cursor)`, or None.
        """
        return _CtxFeed(self, self._tabs[-1], match, scroll=scroll, cursor=cursor)

    def load(self, url: str, match: str | re.Pattern[str] | Callable[[str], bool], *,
             scroll: bool = True, lines: bool = False,
             cursor: Callable[[Any], tuple[bool, str | None] | None] | None = None
             ) -> AsyncIterator[Any]:
        """Open `url` with a `feed` listening; yield `meta(document)`, then the feed's items."""
        return self._load(url, self.feed(match, scroll=scroll, lines=lines, cursor=cursor))

    async def _load(self, url: str, feed: Any) -> AsyncIterator[Any]:
        async with feed:
            await self.open(url)
            yield flnd.plugin.meta(await self.document())
            async for item in feed:
                yield item

    async def warm(self, urls: Sequence[str]) -> None:
        """Record each URL that isn't empty in `seen.warmed`; a fake tab loads nothing ahead."""
        self.seen.warmed.extend(url for url in urls if url)

    async def document(self) -> Document:
        """The page the tab shows, parsed; an empty one before any."""
        from flnkit import html

        page = self._tabs[-1].page
        return html.parse(page.html if page is not None else "")

    async def click(self, css: str, *,
                    timeout: float = 10.0) -> None:  # noqa: ASYNC109 (the engine's signature)
        """Click the first element `css` matches on the page: the page sends its `Sent`
        responses for that click.

        Raises:
            NetworkError: neither the page's HTML nor a `Sent` has `css`, once `timeout`
                passed on the clock.
        """
        tab = self._tabs[-1]
        sent = tab.sent(click=css)
        if not sent and (await self.document()).first(css) is None:
            self._clock().sleep(timeout)
            raise NetworkError(f"{self._plugin.code}: nothing clickable matched {css!r} in "
                               f"{timeout:g} s")
        tab.send(sent)

    async def handoff(self, message: str, *, until: Callable[[], Awaitable[bool]],
                      timeout: float | None = None) -> bool:  # noqa: ASYNC109 (a person's)
        """Ask nobody: whether `until` says the step is done already."""
        return bool(await until())

    async def extension(self, op: str, **args: Any) -> Any:
        """The `extension` fake's answer to `op`: its value, or its function of `args`.

        Raises:
            UsageError: no `extension` was given, or it has no `op`.
        """
        if self._extension is None:
            raise UsageError(f"{self._plugin.code}: the extension isn't connected",
                             hint="flnd extension install")
        if op not in self._extension:
            raise UsageError(f"{self._plugin.code}: the fake extension has no op {op!r}")
        answer = self._extension[op]
        if callable(answer):
            answer = answer(**args)
            if inspect.isawaitable(answer):
                answer = await answer
        return answer

    async def storage(self, origin: str) -> Mapping[str, str]:
        """The `storage` fake, read-only, whatever the origin."""
        return self._storage

    # -- Waits, threads, people --------------------------------------------------------------

    async def pause(self, seconds: float, reason: str) -> None:
        """Move the clock on by `seconds`."""
        self._clock().sleep(seconds)

    async def hold(self, reason: str, *, seconds: float | None = None,
                   bucket: str | None = None) -> None:
        """Take a block round, as the scheduler's `hold`, and wait it out on the fake clock,
        as the engine does.

        Raises:
            Blocked: past the last round.
        """
        with clock.use(self._clock()):
            self._jobs.hold(self._plugin.code, self._account, seconds=seconds or 0.0,
                            reason=reason, bucket=bucket)
            await self._jobs.wait(self._plugin.code, self._account, bucket=bucket)

    async def call(self, fn: Callable[..., T], *args: Any) -> T:
        """Run `fn` as a call job, once the account's holds end, on a thread as the engine's
        thread pool runs it."""
        async def work() -> Any:
            return await asyncio.to_thread(fn, *args)

        return await self._run(jobs.Kind.CALL, work)

    async def ask(self, message: str, *, secret: bool = False, description: str = "",
                  choices: Sequence[str] = ()) -> str:
        """The next of `answers`.

        Raises:
            UsageError: they ran out, as with nobody at a terminal, or the answer isn't one
                of `choices`.
        """
        if not self._answers:
            raise UsageError(f"{self._plugin.code}: nobody can answer {message!r}")
        answer = self._answers.popleft()
        if choices and answer not in choices:
            raise UsageError(f"{self._plugin.code}: {answer!r} isn't one of "
                             f"{', '.join(choices)}, for {message!r}")
        return answer

    def log(self, level: str, message: str, **fields: Any) -> None:
        """Record a line in `seen.logs`, its `{name}` fields filled.

        Raises:
            PluginError: `level` isn't "debug", "info" or "warn".
        """
        if level not in LEVELS:
            raise PluginError(f"{self._plugin.code}: log level {level!r}; use one of "
                              f"{', '.join(LEVELS)}")
        self.seen.logs.append((level, message.format(**fields) if fields else message))

    # -- What it reads -----------------------------------------------------------------------

    @property
    def target(self) -> flnd.plugin.Target:
        """The target, resolved by the plugin's routes."""
        return self._target

    @property
    def when(self) -> flnd.plugin.Range:
        """The run's `when`, as given."""
        return self._when

    @property
    def time(self) -> flnd.plugin.Clock:
        """The fake clock, as `now()`, `wall()` and `day()`."""
        return flnd.engine.ctx.Time(self._clock)

    @property
    def settings(self) -> flnd.plugin.Settings:
        """The plugin's `Settings`: its defaults, and the `settings` given."""
        return self._settings

    @property
    def account(self) -> flnd.plugin.Account | None:
        """The account named `account`; None without one."""
        if self._account is None:
            return None
        at = datetime.datetime.fromtimestamp(self._clock().wall, datetime.UTC).isoformat()
        return flnd.plugin.Account(1, self._plugin.code, self._account, "test", at,
                                   is_default=True)

    @property
    def cookies(self) -> Mapping[str, str]:
        """The cookies, read-only."""
        return self._cookies

    @property
    def state(self) -> MutableMapping[str, Any]:
        """The plugin's state, in the dict given (or one of its own): JSON values only."""
        return self._state

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """The account's secrets: empty at first, written only during `Login.start`."""
        return self._secrets

    # -- How it answers ----------------------------------------------------------------------

    @contextmanager
    def _aside(self) -> Iterator[None]:
        """A tab of its own for the block, as a lazy post's files, `date` or `refresh` get."""
        self._tabs.append(_Tab())
        try:
            yield
        finally:
            self._tabs.pop()

    def _clock(self) -> clock.Fake:
        """The active fake clock, or this context's own, looked up on each call."""
        active = clock.current()
        return active if isinstance(active, clock.Fake) else self._fake

    async def _run(self, kind: Any, work: Callable[[], Awaitable[T]], *, bucket: str = "",
                   host: str = "") -> T:
        """Run `work` as a scheduler job of `kind`, on the fake clock."""
        job = jobs.Job(kind=kind, work=work, plugin=self._plugin.code, account=self._account,
                       bucket=bucket, host=host)
        with clock.use(self._clock()):
            return await self._jobs.run(job)

    def _own(self, url: str) -> bool:
        """Whether `url` is on the plugin's domains; a path alone is."""
        if "://" not in url:
            return True
        host = (urlsplit(url).hostname or "").lower()
        return any(host == domain or host.endswith("." + domain)
                   for domain in self._plugin.domains)

    def _answer(self, request: Request) -> Any:
        """The `routes` answer to `request`, as the kit's HTTP response.

        Raises:
            AssertionError: no route answers its URL.
        """
        given = _lookup(self._routes, request.url)
        if given is None:
            raise AssertionError(f"FakeCtx has no route for {request.url}")
        if callable(given):
            given = given(request)
        response = _response(given if isinstance(given, Reply) else Reply(given), request.url)
        self._answered[_bare(request.url)] = response
        return response

class _Scheduler(jobs.Scheduler):
    """The engine's scheduler, its every hold recorded in a `Seen`."""

    def __init__(self, seen: Seen, *, pace: Mapping[str, float]) -> None:
        super().__init__(pace=pace)
        self._seen = seen

    # `_hold_for` is where every hold of the retry policy is set: a 429, a round, a wall.
    def _hold_for(self, hold: jobs.Hold, seconds: float, reason: str) -> None:
        super()._hold_for(hold, seconds, reason)
        self._seen.holds.append((reason, seconds, hold.bucket))


class _Tab:
    """One fake tab: the page it shows, how often it scrolled, and the feeds listening to it."""

    def __init__(self) -> None:
        self.page: FakePage | None = None
        self.scrolls = 0
        self.feeds: list[_CtxFeed] = []

    def load(self, page: FakePage) -> None:
        """Show `page`, sending what it sends as it loads."""
        self.page, self.scrolls = page, 0
        self.send(self.sent())

    def sent(self, *, click: str | None = None, scroll: int | None = None) -> list[Sent]:
        """What the page sends as it loads, on the click of `click`, or on scroll `scroll`."""
        found = [entry if isinstance(entry, Sent) else Sent(*entry)
                 for entry in (self.page.feeds if self.page is not None else ())]
        return [entry for entry in found if (entry.click, entry.scroll) == (click, scroll)]

    def scroll(self) -> None:
        """Scroll once, sending what the page sends on that scroll."""
        self.scrolls += 1
        self.send(self.sent(scroll=self.scrolls))

    def send(self, sent: Sequence[Sent]) -> None:
        """Hand each response to the feeds listening now; one nobody hears is lost."""
        for entry in sent:
            for listening in list(self.feeds):
                listening._push(entry.url, entry.body)


class _CtxFeed:
    """What `FakeCtx.feed` returns: the responses its tab heard that match, as an async feed."""

    def __init__(self, ctx: FakeCtx, tab: _Tab, match: Any, *, scroll: bool,
                 cursor: Any) -> None:
        from flnkit import browser

        self._ctx, self._tab = ctx, tab
        self._matches = browser.matcher(match)
        self._scroll = scroll
        self._cursors = browser.Cursors(cursor)
        self._waiting: deque[Any] = deque()
        self._done = False
        tab.feeds.append(self)

    def _push(self, url: str, data: Any) -> None:
        """Take one response if it matches and the feed is open."""
        if not self._done and self._matches(url):
            self._waiting.append(data)
            self._cursors.read(data)

    async def __aenter__(self) -> _CtxFeed:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def __aiter__(self) -> _CtxFeed:
        return self

    async def __anext__(self) -> Any:
        # A scroll that brings this feed nothing ends it, as the engine's scroll does.
        if not self._waiting and self._scroll and not self._done and not self._cursors.ended:
            self._ctx.seen.scrolls += 1
            self._tab.scroll()
        if not self._waiting:
            self.close()
            raise StopAsyncIteration
        return self._waiting.popleft()

    def close(self) -> None:
        """Stop listening; a second call does nothing."""
        self._done = True
        if self in self._tab.feeds:
            self._tab.feeds.remove(self)


def _secrets(plugin: Plugin) -> flnd.engine.ctx.Secrets:
    """The account's secrets: written only while the plugin's `Login.start` runs.

    The plugin's login object gets a `start` that runs it as the engine does
    (`flnd.engine.ctx.start`), with the engine's own flag set. What `start` begins inherits it:
    an `asyncio` task, a `ctx.call`, an `asyncio.to_thread`; a plain thread doesn't.
    """
    login = type(plugin).login
    if login is not None and not getattr(login.start, "marked", False):
        login.start = _marked(login.start)
    data: dict[str, str] = {}
    return flnd.engine.ctx.Secrets(lambda: data)


def _marked(start: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """`start`, with the engine's login flag set while it runs."""
    @functools.wraps(start)
    async def marked(context: Any, method: str) -> None:
        with flnd.engine.ctx.granted():
            await start(context, method)

    marked.marked = True  # type: ignore[attr-defined]
    return marked


def _resolved(plugin: type[Plugin], target: str | flnd.plugin.Target) -> flnd.plugin.Target:
    """`target` read by the plugin's routes; as written when none reads it."""
    if isinstance(target, flnd.plugin.Target):
        return target
    return (plugin.resolve(target) if target else None) or flnd.plugin.Target(target)


def _lookup(fakes: Mapping[str, Any], url: str) -> Any:
    """The fake for `url`: by the whole URL, else without its query; None for none."""
    if url in fakes:
        return fakes[url]
    return fakes.get(_bare(url))


def _response(reply: Reply, url: str) -> Any:
    """`reply` as the kit's HTTP response, its body encoded as `Reply` says."""
    from flnkit import net

    headers = {name.lower(): value for name, value in (reply.headers or {}).items()}
    body = reply.body
    if body is None or isinstance(body, bytes):
        content = body or b""
    elif isinstance(body, str):
        content = body.encode()
        headers.setdefault("content-type", "text/html; charset=utf-8")
    else:
        content = jsonlib.dumps(body).encode()
        headers.setdefault("content-type", "application/json")
    return net.Response(reply.status, reply.url or url, headers, content)


def _bare(url: str) -> str:
    """`url` without its query, which may hold a token."""
    return url.split("#", 1)[0].split("?", 1)[0]


# ---------------------------------------------------------------------------------------------
# What a download would save
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Collected:
    """One file `collect` found: where a download would save it, from where, and its key.

    Example:
        >>> found = Collected("posts/p1_0.jpg", "https://cdn.example.com/p1.jpg",
        ...                   Post("p1", date="2026-01-02"), "p1_0",
        ...                   File("https://cdn.example.com/p1.jpg"))
        >>> found.date
        datetime.date(2026, 1, 2)
    """

    # Its path under the target's folder, with the extension it is named with before its
    # content is checked ("" when the content decides): "posts/2026-01-02_p1_0.jpg".
    path: str
    # Its URL, mirrors or fetch function; None for a text file.
    url: Any
    # The post it belongs to, its category set.
    post: Post
    # Its history key, without the source's code.
    key: str
    # The file itself.
    file: File

    @property
    def date(self) -> datetime.date | None:
        """The post's day, read as the engine reads it; None for an undated post."""
        return clock.day(self.post.date)


def collect(plugin: type[Plugin] | type[Source], target: str, *,
            ctx: FakeCtx | FakeSite | None = None, categories: Sequence[str] | None = None,
            limit: int | None = None,
            json: Mapping[str, Answer] | None = None) -> list[Collected]:
    """What a download of `target` would save: its route and categories run on a fake.

    The target is resolved by the plugin's routes, its route and wanted categories listed,
    each lazy post's files listed, and every file named and keyed. Nothing is downloaded or
    saved. A plugin on API 2 runs on a `FakeCtx`, its listing checked as it goes: a
    `newest_first` listing's dated posts must not get newer, and every post field a template
    names must be given. An API-1 source runs on a `FakeSite`, named by the engine's own code.

    Args:
        plugin: the plugin class (or an API-1 source class).
        target: the target as people write it: `exm:example_user`, or a link.
        ctx: the fake to run on, for answers, pages, cookies and `when`: a `FakeCtx` for a
            plugin, a `FakeSite` for a source (text fixtures go in its `html`); None makes an
            empty one (a source's from `json`).
        categories: the categories to list; None lists the route's own ones (a source's:
            every one), and "all" every one.
        limit: the most posts to list, or a `discovers` route's targets; None for no limit.
            A plugin's only.
        json: an API-1 source's JSON fixtures, by URL, when `ctx` is None.

    Returns:
        Each file in order. A file queued twice (the same path or key) is listed once. A
        `Target` a listing yields isn't followed: it runs as its own target.

    Raises:
        UsageError: the routes don't read `target`, `categories` names one the route doesn't
            have, or an option doesn't fit the kind of class.
        PluginError: a listing broke one of the checks above, or yielded something else.
        Error: whatever the plugin raised.

    Example:
        >>> from flnd.source import File, Post, Source, target
        >>> class Example(Source):
        ...     code, domains, api = "exm", ("example.com",), 1
        ...     @target("exm:{user}")
        ...     def photos(self, site, user):
        ...         for item in site.fetch(f"https://api.example.com/{user}").json:
        ...             yield Post(item["id"], [File(item["url"])])
        >>> saved = collect(Example, "exm:example_user", json={
        ...     "https://api.example.com/example_user": [
        ...         {"id": "p1", "url": "https://cdn.example.com/p1.jpg"}]})
        >>> [(found.path, found.key) for found in saved]
        [('p1_0.jpg', 'p1_0')]
    """
    from flnd.engine.options import Options

    if isinstance(plugin, type) and issubclass(plugin, Plugin):
        if json is not None or isinstance(ctx, FakeSite):
            raise UsageError(f"{plugin.code}: a plugin on API 2 is given its answers by "
                             "FakeCtx(routes=..., pages=...), passed as ctx")
        return _collect_plugin(plugin, target, ctx, categories, limit)
    if limit is not None or isinstance(ctx, FakeCtx):
        raise UsageError(f"{plugin.code}: limit and a FakeCtx are for a plugin on API 2")
    chosen = ctx if ctx is not None else FakeSite(json=json)
    only = tuple(categories) if categories is not None else None
    if only is not None:
        unknown = sorted(set(only) - set(plugin.categories))
        if unknown:
            raise UsageError(f"{plugin.code}: no category {', '.join(unknown)}; it has "
                             f"{', '.join(plugin.categories) or 'none'}")
    options = Options(categories=only, since=chosen.time.since, until=chosen.time.until)
    return _collect(plugin, target, chosen, options)


def _collect_plugin(plugin: type[Plugin], text: str, ctx: FakeCtx | None,
                    categories: Sequence[str] | None, limit: int | None) -> list[Collected]:
    """`collect` for a plugin on API 2: the target resolved, then listed on `ctx`."""
    target = plugin.resolve(text)
    if target is None:
        raise UsageError(f"{plugin.code}: no route reads {text!r}")
    ctx = ctx if ctx is not None else FakeCtx(plugin)
    if type(ctx._plugin) is not plugin:
        raise UsageError(f"{plugin.code}: the FakeCtx given is {ctx._plugin.code}'s")
    ctx._target = target
    route = next(known for known in plugin.routes if known.kind == target.kind)
    run = _Listing(ctx, route, _wanted(plugin, route, categories), limit)
    with clock.use(ctx._clock()):
        return asyncio.run(run.collect())


def _wanted(plugin: type[Plugin], route: flnd.plugin.Route,
            categories: Sequence[str] | None) -> list[flnd.plugin.Category]:
    """The route's categories `categories` names, in declaration order; None takes its own.

    Raises:
        UsageError: a name isn't one of the route's categories.
    """
    from flnd.engine import crawl

    if categories is None:
        return [category for category in route.categories if category.own]
    if crawl.ALL in categories:
        return list(route.categories)
    known = [category.name for category in route.categories]
    unknown = sorted(set(categories) - set(known))
    if unknown:
        raise UsageError(f"{plugin.code}: {route.kind} has no category {', '.join(unknown)}; "
                         f"it has {', '.join(known) or 'none'}")
    return [category for category in route.categories if category.name in categories]


class _Listing:
    """One `collect` of a plugin on API 2: its route and categories listed, its files placed.

    Args:
        ctx: the fake it runs on, its target set.
        route: the target's route.
        wanted: the categories to list.
        limit: the most posts (or discovered targets) to take; None for no limit.
    """

    def __init__(self, ctx: FakeCtx, route: flnd.plugin.Route, wanted: list[flnd.plugin.Category],
                 limit: int | None) -> None:
        self.ctx, self.route, self.wanted, self.limit = ctx, route, wanted, limit
        self.plugin = ctx._plugin
        self.found: list[Collected] = []
        self._known: set[str] = set()
        self._taken = 0

    async def collect(self) -> list[Collected]:
        """List the route, or each wanted category on what the route returned."""
        ctx, kind = self.ctx, self.route.kind
        async with self.plugin.connect(ctx):
            if not self.route.categories:
                await self._list(getattr(self.plugin, kind)(ctx, ctx.target.value), None)
                return self.found
            profile = await getattr(self.plugin, kind)(ctx, ctx.target.value)
            for category in self.wanted:
                if self._full():
                    break
                await self._list(getattr(self.plugin, category.name)(ctx, profile), category)
        return self.found

    def _full(self) -> bool:
        return self.limit is not None and self._taken >= self.limit

    async def _list(self, listing: AsyncIterator[Any],
                    category: flnd.plugin.Category | None) -> None:
        """Take a listing's posts as the crawl does, checking their order as they come.

        Raises:
            PluginError: a `newest_first` listing's post is newer than one before it, or the
                listing yielded something not a `Post` or a `Target`.
        """
        from flnd.engine import crawl

        label = f"{self.plugin.code}.{category.name if category else self.route.kind}"
        newest_first = category.newest_first if category else self.route.newest_first
        when, last, older, previous = self.ctx.when, None, 0, None
        async for item in listing:
            if self._full():
                return
            if isinstance(item, flnd.plugin.Target):
                self._taken += self.route.discovers
                continue
            if not isinstance(item, flnd.plugin.Post):
                raise PluginError(f"{label}: it yielded a {type(item).__name__}; yield a Post or "
                                  "a Target")
            listed = clock.day(item.listed)
            # A pinned post sits outside the date order: neither checked nor counted.
            if newest_first and listed is not None and not item.pinned:
                if last is not None and listed > last:
                    raise PluginError(f"{label}: post {item.id} ({listed}) is newer than one "
                                      f"before it ({last}), in a newest_first listing",
                                      hint="list newest first, or set newest_first=False")
                last = listed
            if item.date not in when:
                # A post in parts (one id, yielded once per part) counts once.
                older += item.id != previous and not item.pinned and bool(
                    newest_first and when.since and listed and listed < when.since)
                previous = item.id
                if older >= crawl.OLDER_IN_A_ROW:
                    return
                continue
            older = older if item.pinned else 0
            self._taken += item.id != previous
            previous = item.id
            await self._place(item, category)

    async def _place(self, post: flnd.plugin.Post, category: flnd.plugin.Category | None) -> None:
        """List a post's files, date it by the `date` hook if it has no date, and place each."""
        # The `date` hook and a lazy post's files open their own tab, never the listing's.
        with self.ctx._aside():
            if clock.day(post.date) is None:
                found = await self.plugin.date(self.ctx, post)
                post = post.replace(date=found) if found is not None else post
            try:
                files = tuple(await post.files()) if post.lazy else post.files
            except LAZY_FAILURES as error:
                # The post's own failure, as the engine records it: the listing goes on.
                self.ctx.seen.failed.append((post.id, error))
                return
        stray = next((file for file in files if not isinstance(file, flnd.plugin.File)), None)
        if stray is not None:
            raise PluginError(f"{self.plugin.code}: post {post.id}'s files hold a "
                              f"{type(stray).__name__}; "
                              "wrap each in File(...)")
        for path, key, file in _placed(self.ctx, self.route, category, post, files):
            if path in self._known or key in self._known:
                continue
            self._known.update((path, key))
            url = file.url or file.mirrors or file.fetch or file.playlist
            self.found.append(Collected(path, url, post, key, file))


def _placed(ctx: FakeCtx, route: flnd.plugin.Route, category: flnd.plugin.Category | None,
            post: flnd.plugin.Post,
            files: Sequence[flnd.plugin.File]) -> list[tuple[str, str, flnd.plugin.File]]:
    """Each file's path under the target's folder and its history key, by the templates.

    A `File.name` with an extension keeps it. A stem (or no name) takes the engine's rule
    (`flnd.engine.crawl.planned`): its routed answer's `Content-Type`, then its URL's media
    extension, then none. The kit has no bytes, which a save checks too.

    Raises:
        PluginError: a template names a field of the plugin's that the post doesn't give.
    """
    from flnd.engine import crawl
    from flnkit import text

    plugin, target = ctx._plugin, ctx.target
    folder = next(template for template in (category and category.folder, route.folder,
                                            plugin.folder) if template is not None)
    key = category.key if category is not None and category.key is not None else plugin.key
    named = text.fields(folder) | text.fields(plugin.filename) | text.fields(key)
    missing = sorted(named & set(plugin.fields) - set(post.fields))
    if missing:
        raise PluginError(f"{plugin.code}: post {post.id} gives no {', '.join(missing)}, "
                          "which a template names",
                          hint="give the field on every post, None when it has none")
    placed, used = [], set()
    for num, file in enumerate(files):
        own = bool(crawl.own_extension(file.name))
        if own or file.content is not None:
            extension = PurePosixPath(file.name or "").suffix
        else:
            extension = crawl.planned(file, _typed(ctx, file))[0]
        values = {**post.fields, "plugin": plugin.code, "target": target.value,
                  "category": category.name if category else "", "post": post.id,
                  "num": num, "count": len(files), "index": "" if len(files) == 1 else f"_{num}",
                  "extension": extension, "date": clock.day(post.date),
                  "query": target.value if target.query else ""}
        if own or file.content is not None:
            name = text.filename(file.name or post.id)
        else:
            stem = file.name if file.name is not None else text.fill(plugin.filename, values)
            name = text.filename(stem or post.id) + extension
        where = text.fill(folder, values)
        path = "/".join(part for part in (where, name) if part)
        if path in used:
            # Files of one post may share a name (`cover.jpg`): a later one takes its number.
            name = f"{PurePosixPath(name).stem}_{num}{PurePosixPath(name).suffix}"
            path = "/".join(part for part in (where, name) if part)
        used.add(path)
        placed.append((path, text.fill(key, {**values, "path": path}, safe=False) or path,
                       file))
    return placed


def _typed(ctx: FakeCtx, file: flnd.plugin.File) -> str:
    """The `Content-Type` a routed file's answer gives; "" when its URL isn't routed, or it is
    joined or streamed.

    A `Reply` route is read as it is. A function route isn't called, since it may answer in
    turn: the answer it last gave the plugin for that URL is read, if any.
    """
    url = file.url or (file.mirrors[0] if file.mirrors else None)
    given = _lookup(ctx._routes, url) if url and not (file.audio or file.playlist) else None
    if given is None:
        return ""
    if callable(given):
        response = ctx._answered.get(_bare(url))
        if response is None:
            return ""
    else:
        response = _response(given if isinstance(given, Reply) else Reply(given), url)
    return response.headers.get("content-type", "")


def _collect(source: type[Source], text: str, site: FakeSite,
             options: Options) -> list[Collected]:
    """`collect` with the run's whole `Options`, for flnd's own tests (`tests/fakes.py`).

    Args:
        source: the source class.
        text: the target as people write it.
        site: the site to run on; its `target` is set to the resolved target.
        options: the run's options, as the engine would make them.

    Returns:
        What `collect` returns.
    """
    from flnd.engine import crawl
    from flnd.engine.core import resolve

    target = resolve(text, {source.code: source})
    site.target = target
    instance = source()
    categories = {category.name: category
                  for route in source.routes for category in route.categories}
    found: list[Collected] = []
    known: set[str] = set()

    async def listed() -> None:
        async for post in crawl.posts(instance, site, target, options, call=_now):
            if post.lazy and _first_key(instance, post, categories.get(post.category)) in known:
                # A download skips a lazy post whose first file is queued, before listing it.
                continue
            files = list(post.files()) if callable(post.files) else list(post.files)
            for placed in crawl.place(instance, post, files,
                                      category=categories.get(post.category)):
                if placed.path in known or placed.key in known:
                    continue
                known.update((placed.path, placed.key))
                found.append(Collected(placed.path, placed.file.url, post, placed.key,
                                       placed.file))

    asyncio.run(listed())
    return found


async def _now(fn: Callable[..., Any], *args: Any) -> Any:
    """A source's plain code, called at once on the loop: the fake site never waits for it."""
    return fn(*args)


def _first_key(source: Source, post: Post, category: Any) -> str | None:
    """A post's first file's key from its fields alone, as a download reads it.

    Args:
        source: the source.
        post: the post, its files not listed yet.
        category: its `Category`, whose `key` template may replace the source's; None for none.

    Returns:
        The key; None when the template needs the files' paths to be filled.
    """
    from flnd.engine import crawl
    from flnkit import text

    template = crawl._format(source, category, "key")
    if not template or crawl._fields(template) & crawl.LISTED_FIELDS:
        return None
    return text.fill(template, {**post.fields, "post": post.id, "category": post.category,
                                 "num": 0}, safe=False) or None


# ---------------------------------------------------------------------------------------------
# Checking a source
# ---------------------------------------------------------------------------------------------

def check(plugin: type[Plugin] | type[Source]) -> list[flnd.plugin.Target] | list[Target]:
    """Check a plugin class, and resolve every example target its routes' docstrings give.

    The class's declarations are checked again, as when it was made. Then the examples: in
    each route's docstring, every backticked target of the plugin (`exm:example_user`,
    `exm?words`, or a link on its domains) must resolve to that route. A form with a
    placeholder (`exm:<user>`, `{user}`) isn't an example, and is skipped.

    Args:
        plugin: the plugin class (or an API-1 source class).

    Returns:
        Each example, resolved, in the order the routes give them.

    Raises:
        PluginError: it isn't a `Plugin` (or `Source`) subclass with a code, a declaration is
            wrong, or an example doesn't resolve, or resolves to another route.

    Example:
        >>> from flnd.source import Source, target
        >>> class Example(Source):
        ...     code, domains, api = "exm", ("example.com",), 1
        ...     @target("exm:{user}", "example.com/{user}")
        ...     def user(self, site, user):
        ...         '''A profile: `exm:example_user`, or `https://www.example.com/example_user`.'''
        ...         return []
        >>> [str(found) for found in check(Example)]
        ['exm:example_user', 'exm:example_user']
    """
    from flnd.engine.core import resolve

    if not (isinstance(plugin, type) and issubclass(plugin, (Plugin, Source))
            and getattr(plugin, "code", None)):
        raise PluginError(f"check takes a flnd.plugin.Plugin subclass with a code, "
                          f"not {plugin!r}")
    if issubclass(plugin, Plugin):
        flnd.plugin._check(plugin)
    found = []
    for route in plugin.routes:
        for text in _examples(plugin, route.function.__doc__ or ""):
            try:
                resolved = (plugin.resolve(text) if issubclass(plugin, Plugin)
                            else resolve(text, {plugin.code: plugin}))
            except UsageError as error:
                raise PluginError(f"{plugin.code}.{route.kind}: the example {text!r} doesn't "
                                  f"resolve: {error}") from None
            if resolved is None:
                raise PluginError(f"{plugin.code}.{route.kind}: the example {text!r} doesn't "
                                  "resolve: no route reads it")
            if resolved.kind != route.kind:
                raise PluginError(f"{plugin.code}.{route.kind}: the example {text!r} is read "
                                  f"by the route {resolved.kind}",
                                  hint="move the example to that route's docstring, or "
                                       "change the patterns")
            found.append(resolved)
    return found


def _examples(source: type[Source] | type[Plugin], doc: str) -> list[str]:
    """The example targets in a docstring: backticked targets of the source, without a placeholder.

    Args:
        source: the source class, whose code and domains say what is one of its targets.
        doc: the docstring.

    Returns:
        The examples, in order.
    """
    found = []
    for text in EXAMPLE.findall(doc):
        # A form, not an example: a placeholder, or the code alone (`twt?` for "a search").
        if set(text) & set("<>{}…*") or text in (f"{source.code}:", f"{source.code}?"):
            continue
        own = text.startswith((f"{source.code}:", f"{source.code}?"))
        link = text.lower().startswith(("http://", "https://")) and any(
            domain in text.lower() for domain in source.domains)
        if own or link:
            found.append(text)
    return found


# ---------------------------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------------------------

@contextmanager
def fake_clock(start: float = 0.0) -> Iterator[clock.Fake]:
    """A clock that moves only when something sleeps on it, for the `with` block.

    flnd's waits (a source's `pace`, a back-off, a retry's delay) sleep on the active clock, so
    under this one they pass at once, and each wait is recorded in the clock's `slept`.

    Args:
        start: the clock's first reading, in seconds.

    Yields:
        The clock: `now` (seconds), `wall` (Unix seconds, from 2026-01-01 UTC), `slept` (each
        wait's seconds), `sleep(seconds)` and `advance(seconds)`.

    Example:
        >>> from flnkit import clock
        >>> with fake_clock() as fake:
        ...     clock.sleep(30)
        ...     fake.now, fake.slept
        True
        (30.0, [30])
    """
    with clock.use(clock.Fake(start)) as fake:
        yield fake
