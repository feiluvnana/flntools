"""The session: the only way a source acts. `Session` is the interface; `TargetSession` is the engine's.

    def run(self, session: Session) -> None:
        data = session.fetch_json("https://api.example/feed")
        session.save(data["url"], "a.jpg", category="posts", date=posted)
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Mapping, MutableMapping, Protocol, Sequence, TypeVar

import httpx

from flnd.common import auth, files, net
from flnd.common.compress import KINDS, Choice, Compressor, choice_of
from flnd.common.target import Target
from flnd.sources.base import FetchError, Source
from shared import atomic, fmt, kv, log, prompt, theme

if TYPE_CHECKING:
    from shared import worker
    from shared._browser import Browser, Match, Page, Subscription
    from shared._cookies import Jar
    from shared._selector import Selection

T = TypeVar("T")
# `--from all` takes every category of every source.
ALL = "all"
# A 401, 403 or 404 is "not there for us", which `soft=True` turns into None.
SOFT_STATUSES = (401, 403, 404)


class Session(Protocol):
    """What a source can read and do for one target; see engine.md for each member.

        session.fetch_json(url); session.open_page(url); session.save(url, "a.jpg", category="posts")
    """

    target: Target
    # Where `save` puts files, under the target's folder; None ignores saves (late responses).
    folder: str | None

    @property
    def cookies(self) -> Mapping[str, str]: ...
    @property
    def categories(self) -> tuple[str, ...]: ...
    @property
    def since(self) -> datetime.date | None: ...
    @property
    def until(self) -> datetime.date | None: ...
    def wants(self, category: str | None = None, date: datetime.date | None = None) -> bool: ...

    @property
    def http(self) -> httpx.Client: ...
    def fetch_json(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
                   soft: bool = False, **request: Any) -> Any: ...
    def fetch_html(self, url: str, **request: Any) -> Selection: ...
    def select(self, value: Any) -> Selection: ...

    def open_page(self, url: str) -> Page: ...
    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool: ...
    def on_response(self, match: Match, handler: Callable[[Session, Any, Any], Any], *,
                    lines: bool = False) -> Callable[[], None]: ...
    def on_request(self, match: Match, handler: Callable[[Session, Any], Any]) -> Callable[[], None]: ...
    def scroll(self, *, within: Any = None, count: Callable[[], int] | None = None,
               more: Callable[[], bool] | None = None, idle: float = 10.0, delay: float = 3.0,
               max: int | None = None) -> int: ...
    def wait_for_selector(self, css: str, *, timeout: float = 30.0) -> None: ...
    def wait_for_url(self, pattern: Any, *, timeout: float = 30.0) -> None: ...
    def wait_for_load(self, *, idle: bool = False, timeout: float = 30.0) -> None: ...
    def handoff(self, *, until: Callable[[], bool], message: str, poll: float = 1.0,
                timeout: float | None = None) -> bool: ...
    @property
    def page(self) -> Page: ...
    @property
    def browser(self) -> Browser: ...

    @property
    def client(self) -> Any: ...

    def save(self, source: net.Download, name: str, *, category: str, date: datetime.date | None = None,
             folder: str | None = None, key: str | None = None, size: int | None = None,
             headers: Mapping[str, str] | None = None, audio: str | None = None) -> bool: ...
    def write(self, name: str, text: str) -> bool: ...

    @property
    def state(self) -> MutableMapping[str, Any]: ...
    @property
    def secrets(self) -> MutableMapping[str, str]: ...

    @property
    def interactive(self) -> bool: ...
    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str: ...
    def choose(self, message: str, options: Sequence[str] | Mapping[str, str]) -> str: ...
    def info(self, message: str, **fields: Any) -> None: ...
    def warn(self, message: str, **fields: Any) -> None: ...
    def challenged(self) -> None: ...


@dataclass(frozen=True)
class Options:
    """What one run downloads and how it compresses it: `Options(categories=("reels",), image=Choice("webp", "mid"))`.

    The same for every target of one `Engine.run`; `flnd download` and `flnd sync` run targets
    whose saved settings differ separately. It is hashable, so it can key those groups.
    """

    # `--from`: None (or "all") means every category of each source.
    categories: tuple[str, ...] | None = None
    # `--time`'s two days, both included; None is no limit on that side.
    since: datetime.date | None = None
    until: datetime.date | None = None
    # `--image`, `--video` and `--audio`: how each new file of the kind is compressed; None keeps it as sent.
    image: Choice | None = None
    video: Choice | None = None
    audio: Choice | None = None


def selected(options: Options, source: type[Source]) -> tuple[str, ...]:
    """The categories `--from` takes from one source, in the source's order.

        selected(Options(categories=("reels", "media")), Instagram)     # ("reels",)

    Without `--from`, or with `all`, it is every category. A name the source doesn't have is
    ignored; when none of the names is the source's, it takes every category, as without `--from`.
    """
    names = options.categories
    if names is None or ALL in names:
        return source.categories
    return tuple(name for name in source.categories if name in names) or source.categories


def unknown_categories(names: Sequence[str], sources: Sequence[type[Source]]) -> str | None:
    """Why `--from names` can't be used with these sources, listing the names per source; None when it can."""
    known = {category for source in sources for category in source.categories}
    unknown = [name for name in names if name != ALL and name not in known]
    if not unknown:
        return None
    lists = "; ".join(f"{source.code}: {', '.join(source.categories)}" for source in sources)
    return f"no source here has {', '.join(map(repr, unknown))}: use all, or {lists}"


def single_targets(targets: Sequence[Target], sources: Mapping[str, type[Source]]) -> str | None:
    """Why `--from` can't be used with these targets, naming the first single post; None when it can.

        single_targets([Target("ins", "post", "C1abc")], SOURCES)     # "--from applies to profiles and chats; …"

    A post has no categories: tagged or reposts describe a profile's tab, not a post. Filtering
    one by `--from` would only ever drop every file, so the command refuses it before it starts.
    """
    for target in targets:
        if target.kind in sources[target.source].single_kinds:
            return (f"--from applies to profiles and chats; {target} is one post, downloaded whole: "
                    "give it without --from")
    return None


def wanted(categories: tuple[str, ...], options: Options, *, category: str | None = None,
           date: datetime.date | None = None) -> bool:
    """Whether an item of this category and date is kept; an item without a date is always kept."""
    if category is not None and category not in categories:
        return False
    if isinstance(date, datetime.datetime):
        date = date.date()
    if date is None:
        return True
    return not ((options.since and date < options.since) or (options.until and date > options.until))


@dataclass
class Settings:
    """What one run shares across every source: flnd's folder, the options, the history and the compressing."""

    home: Path
    options: Options
    history: MutableMapping[str, Any]
    interactive: bool = False
    # One for the whole run, so videos encode one at a time across sources and each error warns once.
    compressor: Compressor = field(default_factory=Compressor)


@dataclass
class SourceRun:
    """One source's resources for a whole run: its cookies, HTTP client, back-off, client and stores."""

    source: Source
    settings: Settings
    jar: Jar
    http: httpx.Client
    backoff: net.Backoff
    client: Any = None
    _state: kv.Store | None = field(default=None, repr=False)
    _secrets: auth.Vault | None = field(default=None, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _loop_thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def state(self) -> kv.Store:
        if self._state is None:
            self._state = kv.open(self.settings.home / "sources.db", table=self.source.code)
        return self._state

    @property
    def secrets(self) -> auth.Vault:
        if self._secrets is None:
            self._secrets = auth.secrets(self.settings.home, self.source.code)
        return self._secrets

    def run_async(self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on this source's own event loop, started on first use, and wait for it.

        One loop for the whole source, so an SDK client made by an async `connect` is used on
        the loop it belongs to, by `run` and by every `fetch` download alike.
        """
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                                 name=f"flnd-{self.source.code}")
            self._loop_thread.start()
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def on_loop(self) -> bool:
        """Whether the caller is running on this source's own event loop."""
        return self._loop_thread is threading.current_thread()

    def close(self) -> None:
        self.http.close()
        for store in (self._state, self._secrets):
            if store is not None:
                store.close()
        if self._loop is not None and self._loop_thread is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join()
            self._loop.close()


class TargetSession:
    """The engine's `Session` for one target; `finish()` waits for its downloads."""

    def __init__(self, run: SourceRun, target: Target):
        self.target = target
        self.folder: str | None = ""
        self._run = run
        self._source = run.source
        self._settings = run.settings
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._pool: worker.Pool | None = None
        self._encodes: worker.Pool | None = None
        self._listeners: list[_Listener] = []
        self._categories = selected(run.settings.options, type(run.source))
        # Every path and history key queued so far, so one file is never written twice.
        self._queued: set[Path | str] = set()
        self._handover: queue.SimpleQueue[_File | None] | None = None
        self._handover_thread: threading.Thread | None = None

    @property
    def cookies(self) -> Mapping[str, str]:
        """The login's cookies as a read-only `{name: value}`."""
        return MappingProxyType({cookie["name"]: cookie["value"] for cookie in self._run.jar})

    @property
    def categories(self) -> tuple[str, ...]:
        """The source's categories `--from` asked for."""
        return self._categories

    @property
    def since(self) -> datetime.date | None:
        return self._settings.options.since

    @property
    def until(self) -> datetime.date | None:
        return self._settings.options.until

    def wants(self, category: str | None = None, date: datetime.date | None = None) -> bool:
        """Whether `--from` and `--time` keep an item of this category and date.

            if not session.wants(category="highlights"): return     # not asked for: skip the work
            if not session.wants(date=posted): break                # before --time's since: stop scrolling

        An item without a date is kept whatever the range.
        """
        return wanted(self._categories, self._settings.options, category=category, date=date)

    @property
    def http(self) -> httpx.Client:
        """The source's raw httpx client, with its headers, cookies, signing and retries."""
        return self._run.http

    def fetch_json(self, url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None,
                   soft: bool = False, **request: Any) -> Any:
        """Request `url` and parse its JSON, raising `FetchError` on a failure.

            data = session.fetch_json("https://x.com/i/api/graphql/…", params={"variables": "…"})

        `headers` override the source's. With `soft=True`, a 401, 403 or 404 gives None.
        """
        response = self._send(method, url, headers, request, soft=soft)
        if soft and response.status_code in SOFT_STATUSES:
            return None
        try:
            return response.json()
        except ValueError:
            content_type = response.headers.get("content-type", "no content type")
            raise FetchError(f"expected JSON from {url}, got {content_type}: {response.text[:200]!r}") from None

    def fetch_html(self, url: str, **request: Any) -> Selection:
        """Request `url` and return the page as a `selector.Selection`, raising `FetchError` on a failure.

            title = session.fetch_html("https://www.tiktok.com/embed/v2/1").css("title").text
        """
        from shared import selector

        method = request.pop("method", "GET")
        return selector.parse(self._send(method, url, request.pop("headers", None), request))

    def select(self, value: Any) -> Selection:
        """`selector.parse(value)`: a selection over HTML, JSON text, or data already parsed."""
        from shared import selector

        return selector.parse(value)

    def open_page(self, url: str) -> Page:
        """Open a tab at `url`, connecting this target's browser with the source's settings on first use.

            session.open_page("https://www.instagram.com/example_user/")

        Handlers added with `on_response` and `on_request` are attached before it navigates.
        """
        self._open(url)
        return self.page

    def goto(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        """Go to `url` in the current tab, opening one when there is none; False when it didn't load."""
        if self._page is None:
            return self._open(url, idle=idle, timeout=timeout)
        return _loaded(self._page.goto(url, idle=idle, timeout=timeout))

    def on_response(self, match: Match, handler: Callable[[Session, Any, Any], Any], *,
                    lines: bool = False) -> Callable[[], None]:
        """Call `handler(session, data, response)` for each matching JSON response; returns a stop function.

            stop = session.on_response("graphql", self.on_feed)

        It may be called before `open_page`, so no response of the first load is missed.
        """
        return self._listen(_Listener("response", match, lambda data, response: handler(self, data, response),
                                      lines))

    def on_request(self, match: Match, handler: Callable[[Session, Any], Any]) -> Callable[[], None]:
        """Call `handler(session, request)` for each matching request; returns a stop function, like `on_response`."""
        return self._listen(_Listener("request", match, lambda request: handler(self, request)))

    def scroll(self, *, within: Any = None, count: Callable[[], int] | None = None,
               more: Callable[[], bool] | None = None, idle: float = 10.0, delay: float = 3.0,
               max: int | None = None) -> int:
        """`page.scroll` under a `scroll <folder>` spinner.

        `within` is the element that scrolls (a CSS selector or a locator; None is the page),
        `more` is asked before each scroll and False stops, and `max` is the most scrolls.
        `idle` is the seconds at the bottom with no progress that end the feed, and `delay` the
        longest wait for progress after one scroll.
        """
        steps = [0]

        def until() -> bool:
            return (max is not None and steps[0] >= max) or (more is not None and not more())

        def on_step() -> None:
            steps[0] += 1
            bar.advance()

        with log.progress(f"scroll {self.folder or self.target}", show=_show_steps) as bar:
            return self.page.scroll(within=within, until=until, count=count, idle=idle, wait=delay,
                                    on_step=on_step)

    def wait_for_selector(self, css: str, *, timeout: float = 30.0) -> None:
        """`page.wait(css)` on the current tab: `session.wait_for_selector("article")`."""
        self.page.wait(css, timeout=timeout)

    def wait_for_url(self, pattern: Any, *, timeout: float = 30.0) -> None:
        """`page.wait(url=pattern)` on the current tab: `session.wait_for_url("**/reels/")`."""
        self.page.wait(url=pattern, timeout=timeout)

    def wait_for_load(self, *, idle: bool = False, timeout: float = 30.0) -> None:
        """`page.wait()` on the current tab, for the load or with `idle` a quiet network: `session.wait_for_load(idle=True)`."""
        self.page.wait(idle=idle, timeout=timeout)

    def handoff(self, *, until: Callable[[], bool], message: str, poll: float = 1.0,
                timeout: float | None = None) -> bool:
        """`page.handoff` on the current tab, for a captcha only a person can solve; False on timeout."""
        return self.page.handoff(until=until, message=message, poll=poll, timeout=timeout)

    @property
    def page(self) -> Page:
        """The current tab. Raises `RuntimeError` before `open_page`."""
        if self._page is None:
            raise RuntimeError(f"{self._source.code}: no page is open; call session.open_page(url) first")
        return self._page

    @property
    def browser(self) -> Browser:
        """This target's browser. Raises `RuntimeError` before `open_page`."""
        if self._browser is None:
            raise RuntimeError(f"{self._source.code}: no browser is open; call session.open_page(url) first")
        return self._browser

    @property
    def client(self) -> Any:
        """What the source's `connect` gave, such as an SDK client; None without one."""
        return self._run.client

    def save(self, source: net.Download, name: str, *, category: str, date: datetime.date | None = None,
             folder: str | None = None, key: str | None = None, size: int | None = None,
             headers: Mapping[str, str] | None = None, audio: str | None = None) -> bool:
        """Queue one file, and return False when it is skipped.

            session.save(url, f"{day}_{post_id}.jpg", category="posts", date=posted, headers={"Referer": profile})

        `source` is a URL, mirror URLs tried in order, or `fetch(dest, progress)`. It lands in
        `<downloads>/<code>/<target>/<folder>/<name>`. It is skipped when filtered out by
        `--from`/`--time`, already saved or queued, or when the folder is None. The history row is
        keyed by `key`, which defaults to `folder/name`. A `category` the source doesn't declare
        raises `ValueError`. `audio` is a video's separate audio stream (DASH), merged into it. A
        photo, video or audio file is renamed to the extension its content calls for, and
        compressed when the run's `--image`, `--video` or `--audio` asks; an archive or any other
        file is saved as the site sends it.
        """
        check_category(self._source, category)
        kind = files.kind_of(name)
        if audio is not None and kind != files.VIDEO:
            raise RuntimeError(f"{self._source.code}: audio= is for videos, not {name}")
        folder = self.folder if folder is None else folder
        if folder is None or not self.wants(category=category, date=date):
            return False
        dest = self._path(folder, name)
        label = dest.relative_to(self._target_dir()).as_posix()
        history_key = f"{self._source.code}:{key or label}"
        if dest in self._queued or history_key in self._queued or self._saved_path(dest, history_key):
            log.info("skip {name}", name=label)
            return False
        self._queued.update((dest, history_key))
        self._submit(_File(source, dest, label, history_key, size, headers, kind, audio))
        return True

    def write(self, name: str, text: str) -> bool:
        """Save a text file, such as loi's `info.md`, in the current folder, atomically; False when it is None.

            session.write("info.md", "# Title\\n")

        Args:
            name: the file name in the current folder.
            text: the file's text, saved as UTF-8 with `\\n` line endings on every platform.

        Returns:
            True when written; False when `folder` is None.
        """
        if self.folder is None:
            return False
        dest = self._path(self.folder, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Through a `.<name>.<random>.part`, which `flnd sync`'s repair removes if a run dies mid-write.
        # Written as bytes, so every "\n" stays "\n" on Windows too.
        atomic.write(dest, text.encode("utf-8"))
        return True

    @property
    def state(self) -> MutableMapping[str, Any]:
        """A `kv` table for this source, kept between runs in `.flnd/sources.db`."""
        return self._run.state

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """This source's secrets, encrypted in `.flnd/secrets.db`."""
        return self._run.secrets

    @property
    def interactive(self) -> bool:
        """Whether `ask` and `choose` can ask: during `flnd login`, in a terminal."""
        return self._settings.interactive and theme.screen.interactive

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        """Ask a question with `prompt.ask`, during `flnd login` only, and return what was typed.

            code = session.ask("Code", description="Sent to your phone")

        Args:
            message: the question.
            secret: hide what is typed, as for a password; the done line shows only the message.
            description: more lines under the question.

        Raises:
            RuntimeError: not during `flnd login` in a terminal; `flnd download` never asks.
        """
        self._check_interactive()
        return prompt.ask(message, secret=secret, description=description)

    def choose(self, message: str, options: Sequence[str] | Mapping[str, str]) -> str:
        """Ask to pick one option with `prompt.choose`, during `flnd login` only, and return it.

            method = session.choose("Log in with", {"Phone number": "phone", "QR code": "qr"})

        Args:
            message: the question.
            options: the options; a mapping shows its keys and returns the value picked.

        Raises:
            RuntimeError: not during `flnd login` in a terminal, as for `ask`.
        """
        self._check_interactive()
        return prompt.choose(message, options)

    def info(self, message: str, **fields: Any) -> None:
        """A `log.info` line naming the target: `session.info("{tab}: {posts} listed", tab="posts", posts=fmt.count(3, "post"))`."""
        log.info("{target}: " + message, target=str(self.target), **fields)

    def warn(self, message: str, **fields: Any) -> None:
        """A `log.warn` line naming the target, like `info`."""
        log.warn("{target}: " + message, target=str(self.target), **fields)

    def challenged(self) -> None:
        """Report a challenge (a captcha nobody solved): the source pauses, as after a 429."""
        self._run.backoff.hit("challenged")
        self._run.backoff.wait_turn()

    def finish(self, error: BaseException | None = None) -> int:
        """Close the browser, and wait for the downloads and their encodes (or drop queued ones after `error`).

        It returns how many files were written, after logging each failed one.
        """
        if self._browser is not None:
            self._run.http.cookies.update(net.cookie_jar(self._browser.cookies))
            self._browser.close()
            self._browser = self._page = None
        self._end_handover(dropping=error is not None)
        # Downloads queue video encodes, so their pool ends first; the target waits for both.
        for name in ("_pool", "_encodes"):
            pool: worker.Pool | None = getattr(self, name)
            if pool is None:
                continue
            pool.__exit__(type(error) if error else None, error, error.__traceback__ if error else None)
            # The pool keeps failures in its results instead of raising, so each one is named here.
            for result in pool.results:
                if not result.ok:
                    item = result.item[0] if isinstance(result.item, tuple) else result.item
                    log.error("{file}: {kind} {error}", file=item, kind=type(result.error).__name__,
                              error=result.error)
        if self._pool is None:
            return 0
        return sum(1 for result in self._pool.results if result.value is True)

    def _send(self, method: str, url: str, headers: Mapping[str, str] | None, request: dict, *,
              soft: bool = False) -> httpx.Response:
        if self._browser is not None:
            # Cookies the site set in the browser reach the client too, as a logged-in page would send them.
            self._run.http.cookies.update(net.cookie_jar(self._browser.cookies))
        try:
            response = self._run.http.request(method, url, headers=dict(headers or {}), **request)
        except httpx.HTTPError as error:
            raise FetchError(f"{type(error).__name__} {error} for {url}".strip()) from None
        if response.status_code >= 400 and not (soft and response.status_code in SOFT_STATUSES):
            raise FetchError(f"HTTP {response.status_code} for {url}", status=response.status_code)
        return response

    def _open(self, url: str, *, idle: bool = False, timeout: float = 30.0) -> bool:
        if self._browser is None:
            self._browser = self._connect()
        self._page = self._browser.open()
        for listener in self._listeners:
            listener.attach(self._page)
        return _loaded(self._page.goto(url, idle=idle, timeout=timeout))

    def _listen(self, listener: _Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        if self._page is not None:
            listener.attach(self._page)

        def stop() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)
            listener.detach()

        return stop

    def _connect(self) -> Browser:
        from shared import browser

        source, settings = self._source, self._settings
        # Visible (open's default), so a person can solve a captcha in it; no locale or timezone, so
        # the browser keeps the computer's own, which match where the connection comes from. Without
        # Google Chrome, open asks once to download Chromium, or raises naming Chrome.
        opened = browser.open(viewport=source.viewport, args=source.browser_args, stealth=source.stealth)
        opened.add_cookies(self._run.jar)
        return opened

    def _target_dir(self) -> Path:
        return files.target_folder(self._settings.home, self.target)

    def _path(self, folder: str, name: str) -> Path:
        # Every part goes through fmt.filename, so a name from a site can never leave the target's folder.
        parts = [fmt.filename(part) for part in folder.split("/") if part]
        return self._target_dir().joinpath(*parts, fmt.filename(name))

    def _saved_path(self, dest: Path, history_key: str) -> Path | None:
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        row = self._settings.history.get(history_key)
        found = Path(row.get("path", "")) if isinstance(row, dict) else None
        return found if found is not None and found.is_file() else None

    def _downloads(self) -> worker.Pool:
        if self._pool is None:
            from shared import worker

            self._pool = worker.pool(concurrency=self._source.concurrency, label=str(self.target))
        return self._pool

    def _submit(self, file: _File) -> None:
        pool = self._downloads()
        if not self._run.on_loop():
            pool.submit(self._download, file)
            return
        # On the source's loop, a full pool would wait for downloads that need this very loop, forever,
        # so a thread of this target's waits for room instead.
        if self._handover is None:
            self._handover = queue.SimpleQueue()
            self._handover_thread = threading.Thread(target=self._hand_over, args=(pool, self._handover),
                                                     daemon=True, name=f"flnd-{self._source.code}-saves")
            self._handover_thread.start()
        self._handover.put(file)

    def _hand_over(self, pool: worker.Pool, waiting: queue.SimpleQueue[_File | None]) -> None:
        while (file := waiting.get()) is not None:
            pool.submit(self._download, file)

    def _end_handover(self, *, dropping: bool) -> None:
        if self._handover is None or self._handover_thread is None:
            return
        while dropping:
            try:
                self._handover.get_nowait()
            except queue.Empty:
                break
        self._handover.put(None)
        self._handover_thread.join()
        self._handover = self._handover_thread = None

    def _download(self, file: _File) -> bool:
        source = file.source
        if callable(source):
            source = self._sync_fetch(source)
        # A DASH video without ffmpeg fails here with media's own error, which names what to install.
        saved = net.download(self._run.http, source, file.dest, label=file.label, backoff=self._run.backoff,
                             headers=file.headers, wait=type(self._source).wait, size=file.size, kind=file.kind,
                             audio=file.audio)
        if saved is None:
            return False
        row = {"path": str(saved), "size": saved.stat().st_size,
               "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
        self._settings.history[file.key] = row
        choice = choice_of(self._settings.options, file.kind)
        if choice is None or file.kind is None:
            return True
        if KINDS[file.kind].one_at_a_time:
            # A video waits for the run's one encode at a time on a pool of its own, so downloads go on.
            self._encoder().submit(self._compress, file.key, file.kind, row, choice)
        else:
            self._compress(file.key, file.kind, row, choice)
        return True

    def _encoder(self) -> worker.Pool:
        if self._encodes is None:
            from shared import worker

            self._encodes = worker.pool(concurrency=1)
        return self._encodes

    def _compress(self, key: str, kind: str, row: dict, choice: Choice) -> None:
        """Compress one saved file with the run's choice, and record its fields on its history row."""
        fields = self._settings.compressor.compress(Path(row["path"]), kind, choice)
        # None (no ffmpeg, encoder or extra here) leaves the row without "compressed", so a later
        # download that applies the setting, once that is installed, compresses it.
        if fields is not None:
            self._settings.history[key] = {**row, **fields}

    def _sync_fetch(self, fetch: net.Fetch) -> net.Fetch:
        def call(dest: Path, progress: Callable[..., None]) -> Any:
            outcome = fetch(dest, progress)
            return self._run.run_async(outcome) if inspect.iscoroutine(outcome) else outcome

        return call

    def _check_interactive(self) -> None:
        if not self._settings.interactive:
            code = self._source.code
            raise RuntimeError(f"{code}: flnd download never asks questions; run flnd login first")


@dataclass
class _File:
    """One queued download. It prints as `folder/name`, so a failed download names its file."""

    source: net.Download
    dest: Path
    label: str
    key: str
    size: int | None
    headers: Mapping[str, str] | None
    kind: str | None = None
    audio: str | None = None

    def __str__(self) -> str:
        return self.label


@dataclass
class _Listener:
    """A handler the session attaches to every tab it opens, until it is stopped."""

    event: str
    match: Match
    callback: Callable[..., Any]
    lines: bool = False
    subscriptions: list[Subscription] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        if self.event == "response":
            self.subscriptions.append(page.on_response(self.match, self.callback, lines=self.lines))
        else:
            self.subscriptions.append(page.on_request(self.match, self.callback))

    def detach(self) -> None:
        for subscription in self.subscriptions:
            subscription.stop()
        self.subscriptions.clear()


def check_category(source: Source, category: str) -> None:
    """Raise `ValueError` when a source saves into a category it doesn't declare: a mistake in the source."""
    if category not in source.categories:
        raise ValueError(f"{source.code}: unknown category {category!r}; declare it in categories "
                         f"({', '.join(source.categories)})")


def _show_steps(steps: float) -> str:
    return f"{steps:g} steps"


def _loaded(status: int | None) -> bool:
    # Page.goto gives the main response's status, None when nothing loaded; an error page
    # (400 and up) is not the page asked for. A later step waits out a 429 here instead.
    return status is not None and status < 400
