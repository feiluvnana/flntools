"""The Chrome extension backend for `Ctx`'s browser verbs; src/docs/extension."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

from flnd.engine import extension
from flnd.engine.config import PluginConfig
from flnd.errors import Blocked, Stopped, UsageError
from flnd.plugin import Browser, Plugin
from flnkit import browser, clock, cookies, net

__all__ = ["ExtensionFeed", "ExtensionTabs", "extension_cookies", "uses_chrome"]

WAIT = browser.WAIT
IDLE = browser.IDLE
QUIET = browser.QUIET
REPEATS = browser.REPEATS
HANDOFF_POLL = browser.HANDOFF_POLL
_NOT_STARTED = object()
_SCROLL = 500
_GONE = object()


def uses_chrome(plugin: Plugin, config: PluginConfig) -> bool:
    """Whether the plugin's tabs run in the user's Chrome through the extension."""
    kind = type(plugin).browser
    if kind is Browser.NONE:
        return False
    if kind is Browser.CHROME:
        return True
    return (config.browser or "chromium") == "chrome"


def _pattern(match: Any) -> str:
    """A regex string for `capture.start` from a feed's `match`."""
    if isinstance(match, re.Pattern):
        return match.pattern
    if callable(match):
        return "."
    return re.escape(str(match))


def client(run: Any) -> extension.Client:
    """The run's extension client, connected on first use."""
    found = run.settings.extension
    if isinstance(found, extension.Client):
        return found
    if found is _GONE:
        raise UsageError("flnd's Chrome extension isn't connected", hint="flnd extension install")
    try:
        made = run.loop.run(extension.connect())
    except UsageError:
        run.settings.extension = _GONE
        raise
    run.settings.extension = made
    return made


async def session_cookies(run: Any) -> list[Mapping[str, Any]]:
    """Cookies of the plugin's domains from the run's extension client."""
    if not isinstance(run.settings.extension, extension.Client):
        client(run)
    assert isinstance(run.settings.extension, extension.Client)
    rows = await run.settings.extension.call("cookies", domains=list(run.source.domains))
    return [{
        "name": row["name"], "value": row["value"], "domain": row["domain"],
        "path": row.get("path") or "/", "expires": float(row.get("expires") or 0),
        "secure": bool(row.get("secure")), "httpOnly": bool(row.get("httpOnly")),
        "sameSite": row.get("sameSite") or "Lax",
    } for row in rows]


async def extension_cookies(domains: Iterable[str]) -> cookies.Jar:
    """The login cookies on `domains` and their subdomains, from the extension's `cookies` op."""
    async with await extension.connect() as client:
        rows = await client.call("cookies", domains=list(domains))
    kept = []
    for row in rows:
        kept.append({
            "name": row["name"], "value": row["value"], "domain": row["domain"],
            "path": row.get("path") or "/", "expires": float(row.get("expires") or 0),
            "secure": bool(row.get("secure")), "httpOnly": bool(row.get("httpOnly")),
            "sameSite": row.get("sameSite") or "Lax",
        })
    return cookies.Jar(kept, error=UsageError)


class ExtensionTabs:
    """A target's tabs in the user's Chrome; `flnkit.browser.Tabs` calls this from a thread."""

    def __init__(self, run: Any, *, scroll: str = browser.HUMAN, budget: Any = None) -> None:
        if scroll not in browser.SCROLLS:
            raise ValueError(f"scroll must be one of {', '.join(browser.SCROLLS)}, not {scroll!r}")
        self._run = run
        self._client = client(run)
        self._loop = run.loop.loop
        self.scroll = scroll
        self._budget = budget
        self._held = 0
        self._tab: int | None = None
        self._stack: list[int] = []
        self._warmed: dict[str, tuple[int, browser.Visit | None]] = {}
        self._pending: deque[str] = deque()
        self._feeds: list[tuple[Any, bool]] = []
        self._buffers: dict[int, list[tuple[str, bytes]]] = {}

    def run(self, awaitable: Any) -> Any:
        return browser.run_on(self._loop, awaitable)

    @property
    def opened(self) -> bool:
        return self._tab is not None or bool(self._stack)

    @property
    def headless(self) -> bool:
        return False

    def _active(self) -> int:
        if self._stack:
            return self._stack[-1]
        if self._tab is None:
            self._tab = self.run(self._open("about:blank", active=False))
        return self._tab

    @property
    def page(self) -> ExtensionPage:
        return ExtensionPage(self, self._active())

    async def _open(self, url: str, *, active: bool) -> int:
        opened = await self._client.call("tabs.open", url=url, active=active)
        return int(opened["tab"])

    def warm(self, urls: Iterable[str]) -> None:
        for url in urls:
            if url and url not in self._warmed and url not in self._pending:
                self._pending.append(url)
        self._fill()

    def _fill(self) -> None:
        _ = self.page
        if self._pending:
            self.run(self._filling())

    async def _filling(self) -> None:
        starts: list[str] = []
        while self._pending:
            if self._budget is not None and not self._budget.warm:
                break
            url = self._pending.popleft()
            if url in self._warmed or url in starts:
                continue
            if self._budget is not None:
                await self._budget.take()
                self._held += 1
            starts.append(url)
        opened = await asyncio.gather(*(self._start_warm(url) for url in starts),
                                      return_exceptions=True)
        failed: BaseException | None = None
        for url, result in zip(starts, opened):
            if isinstance(result, BaseException):
                failed = failed or result
                continue
            self._warmed[url] = result
        if failed is not None:
            raise failed

    async def _start_warm(self, url: str) -> tuple[int, browser.Visit | None]:
        tab = await self._open(url, active=False)
        return tab, browser.Visit(200, url, {})

    def take_warm(self, url: str) -> tuple[ExtensionPage, browser.Visit | None]:
        found = self._warmed.pop(url, None)
        if found is None:
            return self.page, None
        tab, visit = found
        self._switch(tab)
        return ExtensionPage(self, tab), visit

    def _switch(self, tab: int) -> None:
        if self._stack:
            old = self._stack[-1]
            self._stack[-1] = tab
        else:
            old, self._tab = self._tab, tab
        if old is not None and old != tab and self._feeds:
            self.run(self._client.call("tabs.close", tab=old))
        if self._feeds:
            self._feeds[-1][0].adopt(tab)

    def take_buffer(self, tab: int, matches: Callable[[str], bool]
                      ) -> Iterable[tuple[str, bytes]]:
        rows = self._buffers.pop(tab, [])
        return [(url, body) for url, body in rows if matches(url)]

    def push(self, feed: Any, *, scroll: bool) -> None:
        pushed = scroll and any(scrolls for _, scrolls in self._feeds)
        if pushed:
            self._stack.append(self._active())
        self._feeds.append((feed, scroll))

    def pop(self, feed: Any) -> None:
        for index in range(len(self._feeds) - 1, -1, -1):
            if self._feeds[index][0] is feed:
                _, pushed = self._feeds.pop(index)
                if pushed and self._stack:
                    old = self._stack.pop()
                    self.run(self._client.call("tabs.close", tab=old))
                return

    def handoff(self, *, until: Callable[[], bool], message: str, timeout: float | None,
                stop: Any) -> bool:
        if until():
            return True
        clock.sleep(0)
        deadline = None if timeout is None else clock.now() + timeout
        while not until():
            if deadline is not None and clock.now() >= deadline:
                return False
            if not clock.sleep(HANDOFF_POLL, stop=stop):
                raise Stopped("stopped while waiting for a person")
        return True

    def close(self) -> None:
        tabs = {self._tab, *self._stack, *(tab for tab, _ in self._warmed.values())}
        self._tab, self._stack, self._warmed = None, [], {}
        for tab in sorted(t for t in tabs if t is not None):
            with contextlib.suppress(Exception):
                self.run(self._client.call("tabs.close", tab=tab))
        if self._budget is not None:
            for _ in range(self._held):
                self._budget.give()
            self._held = 0


class ExtensionPage:
    """One extension tab; each call goes through the shared client on the run's loop."""

    def __init__(self, tabs: ExtensionTabs, tab: int) -> None:
        self._tabs = tabs
        self._tab = tab
        self.url = ""

    async def goto(self, url: str, *, idle: bool = False,
                   timeout: float = 30.0) -> browser.Visit | None:  # noqa: ASYNC109
        old = self._tab
        opened = await self._tabs._client.call("tabs.open", url=url, active=True)
        self._tab = int(opened["tab"])
        self.url = opened.get("url") or url
        if old != self._tab:
            with contextlib.suppress(Exception):
                await self._tabs._client.call("tabs.close", tab=old)
        status = opened.get("status")
        return browser.Visit(int(status) if status is not None else 0, self.url, {})

    async def wait(self, css: str | None = None, *, url: Any = None, idle: bool = False,
                   timeout: float = 30.0) -> None:  # noqa: ASYNC109
        ends = clock.now() + timeout
        while clock.now() < ends:
            if css is not None:
                found = await self._tabs._client.call("evaluate", tab=self._tab,
                                                      script=f"!!document.querySelector({css!r})")
                if found.get("value"):
                    return
            if idle:
                await asyncio.sleep(0.5)
                return
            await asyncio.sleep(0.2)

    async def content(self) -> str:
        found = await self._tabs._client.call("document", tab=self._tab)
        self.url = found.get("url") or self.url
        return found.get("html") or ""

    async def shown(self, css: str) -> bool:
        found = await self._tabs._client.call(
            "evaluate", tab=self._tab, script=f"({browser.SHOWN})({json.dumps(css)})")
        return bool((found or {}).get("value"))

    async def click(self, css: str, *, timeout: float = 10.0) -> None:  # noqa: ASYNC109
        await self._tabs._client.call("click", tab=self._tab, css=css)

    async def fetch(self, url: str, *, method: str = "GET",
                    headers: Mapping[str, str] | None = None,
                    body: bytes | str | None = None,
                    check: bool = False) -> net.Response:  # noqa: ARG002
        script = (
            "async (u, m, h, b) => { const o = {method: m, headers: h, credentials: 'include'};"
            " if (b) o.body = b; const r = await fetch(u, o); const t = await r.text();"
            " return {status: r.status, url: r.url, headers: Object.fromEntries(r.headers),"
            " body: t}; }"
        )
        payload = {
            "url": url, "method": method, "headers": dict(headers or {}),
            "body": body.decode() if isinstance(body, bytes) else body,
        }
        found = await self._tabs._client.call(
            "evaluate", tab=self._tab,
            script=f"({script})({json.dumps(payload['url'])}, {json.dumps(payload['method'])},"
                   f" {json.dumps(payload['headers'])}, {json.dumps(payload['body'])})")
        answer = found.get("value") or {}
        content = (answer.get("body") or "").encode()
        return net.Response(int(answer.get("status") or 0), answer.get("url") or url,
                          answer.get("headers") or {}, content)

    def capture(self, accepts: Callable[[net.Response], bool]) -> ExtensionCapture:
        return ExtensionCapture(self._tabs, self._tab, accepts)


class ExtensionCapture:
    """Network bodies on one tab through the extension's `capture` ops."""

    def __init__(self, tabs: ExtensionTabs, tab: int,
                 accepts: Callable[[net.Response], bool]) -> None:
        self._tabs = tabs
        self._tab = tab
        self._accepts = accepts
        self._id: int | None = None
        self._items: deque[tuple[str, str]] = deque()
        self._metas: deque[tuple[int, dict[str, str]]] = deque()
        self._pending = False

    async def start(self, match: str) -> None:
        found = await self._tabs._client.call("capture.start", tab=self._tab, match=match)
        self._id = int(found["capture"])

    async def _pull(self) -> None:
        if self._id is None:
            return
        rows = await self._tabs._client.call("capture.read", capture=self._id)
        for row in rows:
            body = base64.b64decode(row["body"])
            status = int(row.get("status") or 200)
            headers = {str(k).lower(): str(v) for k, v in (row.get("headers") or {}).items()}
            if self._accepts(net.Response(status, row["url"], headers, body)):
                self._items.append((row["url"], body.decode("utf-8", "replace")))
                self._metas.append((status, headers))

    async def settle(self) -> None:
        await asyncio.sleep(0.1)
        await self._pull()

    async def close(self) -> None:
        if self._id is None:
            return
        try:
            await self._tabs._client.call("capture.stop", capture=self._id)
        finally:
            self._id = None

    @property
    def items(self) -> deque[tuple[str, str]]:
        return self._items


class ExtensionFeed:
    """Feed answers through the extension; mirrors `flnkit.browser.Feed` for the engine."""

    def __init__(self, tabs: ExtensionTabs, match: Any, *, scroll: bool = False,
                 lines: bool = False, cursor: Any = None) -> None:
        self._tabs = tabs
        self._matches = browser.matcher(match)
        self._scroll = scroll
        self._lines = lines
        self._cursors = browser.Cursors(cursor)
        self._closed = False
        self._waiting: deque[Any] = deque()
        tabs.push(self, scroll=scroll)
        self._page = tabs.page
        self._pattern = _pattern(match)
        self._capture = tabs.run(self._open_capture())
        self._items = self._iterate()

    def _accepts(self, response: net.Response) -> bool:
        return self._matches(response.url)

    async def _open_capture(self) -> ExtensionCapture:
        capture = self._page.capture(self._accepts)
        await capture.start(self._pattern)
        return capture

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        return next(self._items)

    def __enter__(self) -> ExtensionFeed:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def ended(self) -> bool:
        return self._cursors.ended

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._tabs.run(self._capture.close())
        finally:
            self._tabs.pop(self)

    def adopt(self, tab: int) -> None:
        self._page = ExtensionPage(self._tabs, tab)

    def _iterate(self) -> Iterator[Any]:
        try:
            while True:
                yield from self._arrived()
                if not self._scroll:
                    try:
                        self._tabs.run(self._page.wait(idle=True, timeout=QUIET))
                    except browser.WaitError:
                        pass
                    self._tabs.run(self._capture.settle())
                    yield from self._arrived()
                    return
                if not self._scroll_until_one():
                    return
        finally:
            self.close()

    def _scroll_until_one(self) -> bool:
        while not self._fill():
            if not self._cursors.ended:
                if self.scroll_step():
                    continue
            self._tabs.run(self._capture.settle())
            return self._fill()
        return True

    def scroll_step(self) -> bool:
        before = self._page_height()
        self._tabs.run(self._tabs._client.call("scroll", tab=self._page._tab, by=_SCROLL))
        after = self._page_height()
        self._tabs.run(self._capture._pull())
        return after > before or self._fill()

    def _page_height(self) -> int:
        found = self._tabs.run(self._tabs._client.call(
            "evaluate", tab=self._page._tab,
            script="document.documentElement.scrollHeight"))
        return int(found.get("value") or 0)

    def _arrived(self) -> Iterator[Any]:
        self._fill()
        while self._waiting:
            value = self._waiting.popleft()
            if isinstance(value, Blocked):
                raise value
            yield value

    def _fill(self) -> bool:
        self._tabs.run(self._capture._pull())
        while self._capture._items:
            url, body = self._capture._items.popleft()
            status, headers = self._capture._metas.popleft() if self._capture._metas else (200, {})
            self._parse(url, body, status, headers)
        return bool(self._waiting)

    def _parse(self, url: str, body: str, status: int = 200,
               headers: Mapping[str, str] | None = None) -> None:
        if status == 429:
            error = net.refusal(net.Response(status, url, headers or {}, body.encode()))
            assert error is not None
            self._waiting.append(error)
            return
        pieces = [line for line in body.splitlines() if line.strip()] if self._lines else [body]
        for piece in pieces:
            try:
                value = json.loads(piece)
            except ValueError:
                continue
            self._waiting.append(value)
            self._cursors.read(value)


def next_block(feed: ExtensionFeed, end: object) -> Any:
    """One feed item, or `end` when the feed ended; for `ctx._Feed` on a thread."""
    try:
        return next(iter(feed))
    except StopIteration:
        return end