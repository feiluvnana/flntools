"""Fakes for testing sources without the network or a browser: `FakeSite` and `collect`.

- `FakeSite` is a `flnd.source.Site` with exactly its members, answering `fetch` from fixtures
  keyed by URL without its query, and recording what the source did in a `Seen`. Its browser
  shows the `pages` fixture for the URL last opened, and its feeds get the `feeds` fixture's
  responses when they scroll or wait (`FakeFeed`).
- `collect(source, text, …)` resolves a target, runs its route and wanted categories on a
  `FakeSite`, lists lazy files, and names and keys each file as the job would: a list of `Saved`.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, MutableMapping

import no_profiles  # noqa: F401  (refuses real browser profiles)
from flnd import _browser, _html, _net, job
from flnd.engine import resolve
from flnd.options import Options
from flnd.source import File, NotFound, Post, Range, SiteError, Source, Target


# A fixture: data for a URL, a SiteError to raise, or a function of (url, request) giving either.
Answer = Any


@dataclass
class Seen:
    """What a source did through a `FakeSite`, which keeps its own members to the ones a `Site` has."""

    # (method, url, options) of each `fetch`, options holding `headers`, `in_page` and the request's.
    requests: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    # Each URL `open` loaded.
    visited: list[str] = field(default_factory=list)
    # How many times a feed scrolled for more.
    scrolls: int = 0
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
        json: data for a URL, a `SiteError` to raise, or a function of `(url, request)` giving either.
        html: page text for a URL, as `json`.
        pages: the HTML `document()` shows after `open` loaded a URL; "" for one not listed.
        feeds: (URL, JSON) responses the site would send, in order. Each goes to every open feed whose
            match takes its URL, the next time a feed scrolls or waits; one nobody watches for stays
            until somebody does, as a tab's list comes only once it is clicked.
        options: the run's options; `time` reads `--time` from them.
        client: what `client` gives, such as a fake SDK client.
        seen: where it records; a new `Seen` when None.
    """

    def __init__(self, target: Target, *, cookies: Mapping[str, str] | None = None,
                 json: Mapping[str, Answer] | None = None, html: Mapping[str, Answer] | None = None,
                 pages: Mapping[str, str] | None = None, options: Options = Options(), client: Any = None,
                 seen: Seen | None = None, state: MutableMapping[str, Any] | None = None,
                 feeds: Any = ()):
        self.target = target
        self.client = client
        self.state: MutableMapping[str, Any] = state if state is not None else {}
        self.secrets: MutableMapping[str, str] = {}
        self._seen = seen if seen is not None else Seen()
        self._cookies = dict(cookies or {})
        self._json, self._html, self._pages = dict(json or {}), dict(html or {}), dict(pages or {})
        self._options = options
        self._pending = list(feeds)
        self._feeds: list[FakeFeed] = []

    @property
    def cookies(self) -> Mapping[str, str]:
        return self._cookies

    @property
    def time(self) -> Range:
        return Range(self._options.since, self._options.until)

    def fetch(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
              check: bool = True, in_page: bool = False, **request: Any) -> _net.Response:
        self._seen.requests.append((method, url, {"headers": dict(headers or {}), "in_page": in_page, **request}))
        base = url.split("?")[0]
        if base in self._json:
            answer = _answer(self._json, url, request)
            if not isinstance(answer, SiteError):
                return _net.Response(200, url, {"content-type": "application/json"}, jsonlib.dumps(answer).encode())
        else:
            answer = _answer(self._html, url, request)
            if not isinstance(answer, SiteError):
                return _net.Response(200, url, {"content-type": "text/html"}, str(answer).encode())
        if not check and answer.status is not None:
            return answer.response or _net.Response(answer.status, url, {})
        raise answer

    def open(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        self._seen.visited.append(url)
        return True

    @property
    def url(self) -> str:
        return self._seen.visited[-1] if self._seen.visited else "about:blank"

    def wait(self, css: str | None = None, *, url: Any = None, idle: bool = False, timeout: float = 30.0) -> None:
        pass

    def scroll(self, *, until: Callable[[], bool] | None = None, idle: float = 2.0) -> int:
        return 0

    def feed(self, match: Any, *, scroll: bool = False, lines: bool = False, cursor: Any = None) -> FakeFeed:
        return FakeFeed(self, match, scroll=scroll, cursor=cursor)

    def document(self) -> Any:
        # The page of the URL last opened, even where a subclass reports a redirect as `url`.
        return _html.parse(self._pages.get(self._seen.visited[-1] if self._seen.visited else "", ""))

    def evaluate(self, js: str, *, arg: Any = None) -> Any:
        return None

    def click(self, css: str, *, timeout: float = 10.0) -> None:
        pass

    def handoff(self, *, until: Callable[[], bool], message: str, timeout: float | None = None) -> bool:
        return until()

    @property
    async def tab(self) -> Any:
        raise NotImplementedError("FakeSite has no Playwright page")

    def pause(self, seconds: float, reason: str) -> None:
        pass

    def _deliver(self, *, scroll: bool) -> None:
        """A feed scrolled (counted in `seen`) or waited: `_release` what is pending."""
        if scroll:
            self._seen.scrolls += 1
        self._release()

    def _release(self) -> None:
        """Hand each pending response to every open feed that matches it; a test may hold them back."""
        for url, data in list(self._pending):
            if [feed for feed in list(self._feeds) if feed._push(url, data)]:
                self._pending.remove((url, data))

    def info(self, message: str, **fields: Any) -> None:
        self._seen.messages.append(message.format(**fields))

    def warn(self, message: str, **fields: Any) -> None:
        self._seen.messages.append("! " + message.format(**fields))

    def back_off(self, reason: str, *, retry_after: float | None = None) -> None:
        self._seen.back_offs.append(reason)


class FakeFeed:
    """What `FakeSite.feed` returns: the `feeds` fixture's responses whose URL matches, as `Site.feed` gives them.

    Iterating yields what is waiting; then, with `scroll`, it asks the site to deliver once more and ends when
    nothing new came, or when `cursor` read the end as the engine's feed reads it (`flnd._browser._Cursors`),
    and without `scroll` it asks once and ends, as the engine's feed waits once for quiet.
    """

    def __init__(self, site: FakeSite, match: Any, *, scroll: bool = False, cursor: Any = None):
        self._site = site
        self._matches = _browser._matcher(match)
        self._scroll = scroll
        self._cursors = _browser._Cursors(cursor)
        self._waiting: deque[Any] = deque()
        self._closed = False
        site._feeds.append(self)
        self._items = self._iterate()

    def _push(self, url: str, data: Any) -> bool:
        if self._closed or not self._matches(url):
            return False
        self._waiting.append(data)
        self._cursors.read(data)
        return True

    def __iter__(self) -> FakeFeed:
        return self

    def __next__(self) -> Any:
        return next(self._items)

    def __enter__(self) -> FakeFeed:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._site._feeds.remove(self)

    def _iterate(self) -> Any:
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
            seen: Seen | None = None, state: MutableMapping[str, Any] | None = None,
            feeds: Any = ()) -> list[Saved]:
    """What the job would download for `text`: its route and wanted categories run on a `FakeSite`.

        saved = collect(Example, "exm:example_user", json=FIXTURES, options=Options(categories=("posts",)))
        [s.path for s in saved]     # ["posts/2026-01-02_C1abc_0.jpg", "posts/2026-01-02_C1abc_1.mp4", …]

    Args:
        source: the source class.
        text: the target as people write it, `code:` or a link.
        json, html, pages, cookies, client, seen, feeds: the `FakeSite`'s, as it takes them.
        state: the site's saved state, shared by runs that pass the same dict.
        options: `--from` and `--time`.

    Returns:
        Each file in order, a file queued twice (same path or key) listed once, as the job queues it.
        Nothing is saved, so a lazy post's files are listed unless its first key is queued already.

    An `async def` route or category runs on a loop of its own, as on the source's.
    """
    target = resolve(text, {source.code: source})
    site = FakeSite(target, cookies=cookies, json=json, html=html, pages=pages, options=options, client=client,
                    seen=seen, state=state, feeds=feeds)
    instance = source()
    categories = {category.name: category for route in source.routes for category in route.categories}
    loop = asyncio.new_event_loop()
    found: list[Saved] = []
    known: set[str] = set()
    try:
        for post in job.posts(instance, site, target, options, run_async=loop.run_until_complete):
            if post.lazy and _first_key(instance, post, categories.get(post.category)) in known:
                # The job skips a lazy post whose first file is queued, before listing its paths.
                continue
            listed = list(post.files()) if callable(post.files) else list(post.files)
            for placed in job.place(instance, post, listed, category=categories.get(post.category)):
                if placed.path in known or placed.key in known:
                    continue
                known.update((placed.path, placed.key))
                found.append(Saved(placed.path, placed.file.url, post, placed.key, placed.file))
    finally:
        loop.close()
    return found


def _first_key(source: Source, post: Post, category: Any) -> str | None:
    """A post's first file's key from its fields alone, as the job reads it; None when it needs its paths."""
    template = job._format(source, category, "key")
    if not template or job._fields(template) & job.LISTED_FIELDS:
        return None
    return job.fill(template, {**post.fields, "id": post.id, "category": post.category, "num": 0},
                    safe=False) or None


def _answer(answers: Mapping[str, Answer], url: str, request: dict[str, Any]) -> Any:
    """The fixture for `url` without its query, called when it's a function; a 404 `NotFound` for one not given."""
    answer = answers.get(url.split("?")[0], NotFound(f"HTTP 404 for {url}", status=404))
    return answer(url, request) if callable(answer) else answer


def history(home) -> dict[str, dict[str, Any]]:
    """Every saved file's row in a home's `flnd.db`, by key, with its post's file count.

    A test reads what a run saved through this, as a dict per row, the way it reads JSON.
    """
    from pathlib import Path

    import msgspec

    from flnd import _home

    with _home.open(Path(home)) as db:
        rows = {row.key: msgspec.structs.asdict(row) for row in db.records()}
        for item, row in rows.items():
            row["files"] = db.files_of_post(item)
    return rows


def targets(home) -> dict[str, Any]:
    """Every target row in a home's `flnd.db`, by key (`flnd._db.TargetRow`)."""
    from pathlib import Path

    from flnd import _home

    with _home.open(Path(home)) as db:
        return db.targets()
# ---------------------------------------------------------------------------------------------
# The HTTP library, driven from a test's own thread
# ---------------------------------------------------------------------------------------------
# `flnd._net` runs on an asyncio loop. These run one call on a loop of its own (`asyncio.run`,
# which carries the test's context, so a fake clock applies), over a fresh session, inside
# whatever `_net.fake` the test opened. Each is a blocking call, as a script would make.


def fetch(url: str, *, retries: int = _net.RETRIES, stop: Any = None, limits: Any = None,
          session: _net.Session | None = None, **options: Any) -> _net.Response:
    """`Client.fetch` on a loop of its own: the response, or the error the policy raises."""
    return asyncio.run(_with_client(lambda client: client.fetch(url, **options), retries=retries,
                                    stop=stop, limits=limits, session=session))


def download(url: Any, dest: Any, *, retries: int = _net.RETRIES, stop: Any = None,
             resume: bool = True, progress: Any = None, segments: bool = False,
             headers: Mapping[str, str] | None = None) -> Any:
    """`Client.fill` into `dest`'s part on a loop of its own; returns where it landed."""
    from flnd import _files

    async def fill(client: _net.Client) -> Any:
        with _files.part(dest, resume=resume) as held:
            await client.fill(url, held, resume=resume, progress=progress, headers=headers)
        return held.dest

    return asyncio.run(_with_client(fill, retries=retries, stop=stop, segments=segments))


async def _with_client(call: Callable[[_net.Client], Any], *, retries: int, stop: Any,
                       limits: Any = None, session: _net.Session | None = None,
                       segments: bool = False) -> Any:
    own = session is None
    session = session if session is not None else _net.Session()
    try:
        client = _net.Client(session, retries=retries, stop=stop, limits=limits,
                             segments=segments)
        return await call(client)
    finally:
        if own:
            await session.close()
