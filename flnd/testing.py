"""The test kit for sources: run one on fixtures, with no network, no browser and no clock.

    from flnd.testing import FakeSite, check, collect

    check(Example)                                      # the class, and its routes' examples
    saved = collect(Example, "exm:example_user", json=FIXTURES)
    assert saved[0].path == "posts/2026-01-02_p1_0.jpg"

It is one of flnd's three public modules, next to `flnd` (the library) and `flnd.source` (the
source API), and it is how a plugin's own tests are written.

What it holds:
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
(`flnd.engine.job.posts` and `flnd.engine.job.place`, `flnd.engine.core.resolve`,
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
import json as jsonlib
import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from flnd.kit import clock
from flnd.source import (
    File,
    NotFound,
    Post,
    Range,
    SiteError,
    Source,
    SourceError,
    Target,
    UsageError,
)

if TYPE_CHECKING:
    import datetime

    from flnd.engine.options import Options
    from flnd.kit import net

__all__ = ["Collected", "FakeSite", "Seen", "check", "collect", "fake_clock"]

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
    """What a source did through a `FakeSite`: its requests, pages, scrolls and messages.

    Example:
        >>> site = FakeSite(json={"https://api.example.com/me": {"id": 1}})
        >>> site.fetch("https://api.example.com/me", params={"x": 1}).json
        {'id': 1}
        >>> method, url, options = site.seen.requests[0]
        >>> method, url, options["params"]
        ('GET', 'https://api.example.com/me', {'x': 1})
    """

    # (method, url, options) of each `fetch`: options hold `headers`, `in_page` and the call's
    # own (`params`, `body`).
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
        from flnd.kit import net

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
        from flnd.kit import html

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
        from flnd.kit import html

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
            flnd.errors.UsageError: exm: a run can't ask 'Code'
        """
        raise UsageError(f"{self.target.source}: a run can't ask {message!r}")

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
    reads it, `flnd.kit.browser._Cursors`); without `scroll` it asks once and ends, as the engine's
    feed waits once for quiet.

    Args:
        site: the site whose `feeds` fixture it reads.
        match: what a response's URL must match.
        scroll: whether it scrolls for more.
        cursor: the source's cursor reader, or None.
    """

    def __init__(self, site: FakeSite, match: Any, *, scroll: bool = False,
                 cursor: Any = None) -> None:
        from flnd.kit import browser

        self._site = site
        self._matches = browser._matcher(match)
        self._scroll = scroll
        self._cursors = browser._Cursors(cursor)
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


def collect(source: type[Source], target: str, *, site: FakeSite | None = None,
            json: Mapping[str, Answer] | None = None, html: Mapping[str, Answer] | None = None,
            only: Sequence[str] | None = None) -> list[Collected]:
    """What a download of `target` would save: its route and categories run on a `FakeSite`.

    The target is resolved by the source's routes, its route and wanted categories listed,
    each lazy post's files listed, and every file named and keyed by the engine's own code.
    Nothing is downloaded or saved. An `async def` route or category runs on a loop of its own.

    Args:
        source: the source class.
        target: the target as people write it: `exm:example_user`, or a link.
        site: the site to run on, for fixtures past `json` and `html` (pages, feeds, cookies,
            a client, `--when`); None makes one from `json` and `html`.
        json: the JSON fixtures, by URL, when `site` is None.
        html: the text fixtures, by URL, when `site` is None.
        only: the categories to list, as `--only` names them; None lists every one.

    Returns:
        Each file in order. A file queued twice (the same path or key) is listed once, and a
        lazy post whose first file is queued already isn't listed again, as a download does.

    Raises:
        UsageError: the source's routes don't read `target`, or `only` names a category it
            doesn't have.
        Error: whatever the source raised, classified as a download would.

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

    chosen = site if site is not None else FakeSite(json=json, html=html)
    categories = tuple(only) if only is not None else None
    if categories is not None:
        unknown = sorted(set(categories) - set(source.categories))
        if unknown:
            raise UsageError(f"{source.code}: no category {', '.join(unknown)}; it has "
                             f"{', '.join(source.categories) or 'none'}")
    options = Options(categories=categories, since=chosen.time.since, until=chosen.time.until)
    return _collect(source, target, chosen, options)


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
    from flnd.engine import job
    from flnd.engine.core import resolve

    target = resolve(text, {source.code: source})
    site.target = target
    instance = source()
    categories = {category.name: category
                  for route in source.routes for category in route.categories}
    loop = asyncio.new_event_loop()
    found: list[Collected] = []
    known: set[str] = set()
    try:
        for post in job.posts(instance, site, target, options,
                              run_async=loop.run_until_complete):
            if post.lazy and _first_key(instance, post, categories.get(post.category)) in known:
                # A download skips a lazy post whose first file is queued, before listing it.
                continue
            listed = list(post.files()) if callable(post.files) else list(post.files)
            for placed in job.place(instance, post, listed, category=categories.get(post.category)):
                if placed.path in known or placed.key in known:
                    continue
                known.update((placed.path, placed.key))
                found.append(Collected(placed.path, placed.file.url, post, placed.key,
                                       placed.file))
    finally:
        loop.close()
    return found


def _first_key(source: Source, post: Post, category: Any) -> str | None:
    """A post's first file's key from its fields alone, as a download reads it.

    Args:
        source: the source.
        post: the post, its files not listed yet.
        category: its `Category`, whose `key` template may replace the source's; None for none.

    Returns:
        The key; None when the template needs the files' paths to be filled.
    """
    from flnd.engine import job
    from flnd.kit import text

    template = job._format(source, category, "key")
    if not template or job._fields(template) & job.LISTED_FIELDS:
        return None
    return text.fill(template, {**post.fields, "post": post.id, "category": post.category,
                                 "num": 0}, safe=False) or None


# ---------------------------------------------------------------------------------------------
# Checking a source
# ---------------------------------------------------------------------------------------------

def check(source: type[Source]) -> list[Target]:
    """Check a source class, and resolve every example target its routes' docstrings give.

    The class's declarations were checked when it was made (a mistake raises `SourceError`
    there). This adds the examples: in each route's docstring, every backticked target of the
    source (`exm:example_user`, `exm?words`, or a link on its domains) must resolve to that
    route. A form with a placeholder (`exm:<user>`, `{user}`) isn't an example, and is skipped.

    Args:
        source: the source class.

    Returns:
        Each example, resolved, in the order the routes give them.

    Raises:
        SourceError: it isn't a `Source` subclass with a code, or an example doesn't resolve,
            or resolves to another route.

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

    if not (isinstance(source, type) and issubclass(source, Source)
            and getattr(source, "code", None)):
        raise SourceError(f"check takes a flnd.source.Source subclass with a code, "
                          f"not {source!r}")
    found = []
    for route in source.routes:
        for text in _examples(source, route.function.__doc__ or ""):
            try:
                resolved = resolve(text, {source.code: source})
            except UsageError as error:
                raise SourceError(f"{source.code}.{route.kind}: the example {text!r} doesn't "
                                  f"resolve: {error}") from None
            if resolved.kind != route.kind:
                raise SourceError(f"{source.code}.{route.kind}: the example {text!r} is read "
                                  f"by the route {resolved.kind}",
                                  hint="move the example to that route's docstring, or "
                                       "change the patterns")
            found.append(resolved)
    return found


def _examples(source: type[Source], doc: str) -> list[str]:
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
        >>> from flnd.kit import clock
        >>> with fake_clock() as clock:
        ...     clock.sleep(30)
        ...     clock.now, clock.slept
        True
        (30.0, [30])
    """
    with clock.use(clock.Fake(start)) as fake:
        yield fake
