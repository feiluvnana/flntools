"""The session: the only way a source acts. `Session` is the interface; `TargetSession` is the engine's.

    def run(self, session: Session) -> None:
        data = session.fetch_json("https://api.example/feed")
        session.save(data["url"], "a.jpg", category="posts", date=posted)
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Mapping, MutableMapping, Protocol, Sequence, TypeVar

import httpx

from flnd import net, vault
from flnd.sources.base import FetchError, Source
from flnd.target import Target
from shared import fmt, kv, log, prompt

if TYPE_CHECKING:
    from shared import worker
    from shared._browser import Browser, Match, Page
    from shared._selector import Selection

T = TypeVar("T")
# `--from all` takes every category of every source.
ALL = "all"
# A 401, 403 or 404 is "not there for us", which `soft=True` turns into None.
SOFT_STATUSES = (401, 403, 404)
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz", ".zst"}


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
    def scroll(self, *, by: str | Callable[[Any], Any] | None = None, count: Callable[[], int] | None = None,
               more: Callable[[], bool] | None = None, idle: int = 4, delay: float = 2.0, max: int = 200) -> int: ...
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
             folder: str | None = None, key: str | None = None, password: str | None = None,
             size: int | None = None, headers: Mapping[str, str] | None = None) -> bool: ...
    def write(self, name: str, text: str) -> bool: ...
    @property
    def full(self) -> bool: ...

    @property
    def state(self) -> MutableMapping[str, Any]: ...
    @property
    def secrets(self) -> MutableMapping[str, str]: ...

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str: ...
    def choose(self, message: str, options: Sequence[str] | Mapping[str, str]) -> str: ...
    def info(self, message: str, **fields: Any) -> None: ...
    def warn(self, message: str, **fields: Any) -> None: ...
    def challenged(self) -> None: ...


@dataclass(frozen=True)
class Options:
    """The download options, the same for every source: `Options(categories=("reels",), since=date(2026, 1, 1))`.

    `categories` is `--from`: None means each source's `default_categories`, and "all" every one.
    """

    categories: tuple[str, ...] | None = None
    since: datetime.date | None = None
    until: datetime.date | None = None
    unpack: bool = False
    zip: bool = False


def selected(options: Options, source: type[Source]) -> tuple[str, ...]:
    """The categories `--from` takes from one source, in the source's order.

        selected(Options(categories=("reels", "media")), Instagram)     # ("reels",)

    A name the source doesn't have is ignored; when none of the names is the source's, it
    downloads its default categories, as without `--from`.
    """
    names = options.categories
    if names is None:
        return source.default_categories
    if ALL in names:
        return source.categories
    return tuple(name for name in source.categories if name in names) or source.default_categories


def unknown_categories(names: Sequence[str], sources: Sequence[type[Source]]) -> str | None:
    """Why `--from names` can't be used with these sources, listing the names per source; None when it can."""
    known = {category for source in sources for category in source.categories}
    unknown = [name for name in names if name != ALL and name not in known]
    if not unknown:
        return None
    lists = "; ".join(f"{source.code}: {', '.join(source.categories)}" for source in sources)
    return f"no source here has {', '.join(map(repr, unknown))}: use all, or {lists}"


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
    """What the engine was started with, shared by every source: folders, options and the browser's."""

    home: Path
    options: Options
    history: MutableMapping[str, Any]
    limit: int | None = None
    headless: bool = False
    locale: str | None = None
    timezone: str | None = None
    interactive: bool = False

    @property
    def downloads(self) -> Path:
        return self.home / "downloads"


@dataclass
class SourceRun:
    """One source's resources for a whole run: its cookies, HTTP client, back-off, client and stores."""

    source: Source
    settings: Settings
    jar: list[dict]
    http: httpx.Client
    backoff: net.Backoff
    client: Any = None
    _state: kv.KV | None = field(default=None, repr=False)
    _secrets: vault.Vault | None = field(default=None, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _loop_thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def state(self) -> kv.KV:
        if self._state is None:
            self._state = kv.open(self.settings.home / "sources.db", table=self.source.code)
        return self._state

    @property
    def secrets(self) -> vault.Vault:
        if self._secrets is None:
            self._secrets = vault.open(self.settings.home, self.source.code)
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
        self._listeners: list[_Listener] = []
        self._categories = selected(run.settings.options, type(run.source))
        # Every path and history key queued so far, so one file is never written twice.
        self._queued: set[Path | str] = set()

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
        """Whether `--from`, `--since` and `--until` keep an item of this category and date.

            if not session.wants(category="highlights"): return     # not asked for: skip the work
            if not session.wants(date=posted): break                # older than --since: stop scrolling

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
        return selector.select(self._send(method, url, request.pop("headers", None), request))

    def select(self, value: Any) -> Selection:
        """`selector.select(value)`: a selection over HTML, JSON text, or data already parsed."""
        from shared import selector

        return selector.select(value)

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
        return self._page.goto(url, idle=idle, timeout=timeout)

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

    def scroll(self, *, by: str | Callable[[Any], Any] | None = None, count: Callable[[], int] | None = None,
               more: Callable[[], bool] | None = None, idle: int = 4, delay: float = 2.0, max: int = 200) -> int:
        """`page.scroll` under a `scroll <folder>` spinner, stopping too once the limit is reached."""
        def keep_going() -> bool:
            return not self.full and (more is None or more())

        with log.progress(f"scroll {self.folder or self.target}", show=_show_steps) as bar:
            return self.page.scroll(by=by, count=count, more=keep_going, idle=idle, delay=delay, max=max,
                                    onscroll=bar.advance)

    def wait_for_selector(self, css: str, *, timeout: float = 30.0) -> None:
        """`page.wait_for_selector` on the current tab: `session.wait_for_selector("article")`."""
        self.page.wait_for_selector(css, timeout=timeout)

    def wait_for_url(self, pattern: Any, *, timeout: float = 30.0) -> None:
        """`page.wait_for_url` on the current tab: `session.wait_for_url("**/reels/")`."""
        self.page.wait_for_url(pattern, timeout=timeout)

    def wait_for_load(self, *, idle: bool = False, timeout: float = 30.0) -> None:
        """`page.wait_for_load` on the current tab: `session.wait_for_load(idle=True)`."""
        self.page.wait_for_load(idle=idle, timeout=timeout)

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
             folder: str | None = None, key: str | None = None, password: str | None = None,
             size: int | None = None, headers: Mapping[str, str] | None = None) -> bool:
        """Queue one file, and return False when it is skipped.

            session.save(url, f"{day}_{post_id}.jpg", category="posts", date=posted, headers={"Referer": profile})

        `source` is a URL, mirror URLs tried in order, or `fetch(dest, progress)`. It lands in
        `<downloads>/<code>/<target>/<folder>/<name>`. It is skipped when filtered out by
        `--from`/`--since`/`--until`, already saved or queued, past the limit, or when the folder
        is None. The history row is keyed by `key`, which defaults to `folder/name`. A `category`
        the source doesn't declare raises `ValueError`.
        """
        check_category(self._source, category)
        folder = self.folder if folder is None else folder
        if folder is None or self.full or not self.wants(category=category, date=date):
            return False
        dest = self._path(folder, name)
        label = dest.relative_to(self._target_dir()).as_posix()
        history_key = f"{self._source.code}:{key or label}"
        if dest in self._queued or history_key in self._queued or self._already_saved(dest, history_key):
            log.info("skip {name}", name=label)
            return False
        self._queued.update((dest, history_key))
        self._downloads().submit(self._download, _File(source, dest, label, history_key, password, size, headers))
        if self.full:
            log.info("Reached limit of {limit} downloads.", limit=self._settings.limit)
        return True

    def write(self, name: str, text: str) -> bool:
        """Save a text file, such as loi's `info.md`, in the current folder, atomically; False when it is None."""
        if self.folder is None:
            return False
        dest = self._path(self.folder, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(text)
            os.chmod(temp_path, 0o644)
            os.replace(temp_path, dest)
        except BaseException:
            Path(temp_path).unlink(missing_ok=True)
            raise
        return True

    @property
    def full(self) -> bool:
        """Whether the limit of queued files is reached."""
        limit = self._settings.limit
        return bool(limit) and self._pool is not None and self._pool.submitted >= limit

    @property
    def state(self) -> MutableMapping[str, Any]:
        """A `kv` table for this source, kept between runs in `.flnd/sources.db`."""
        return self._run.state

    @property
    def secrets(self) -> MutableMapping[str, str]:
        """This source's secrets, encrypted in `.flnd/secrets.db`."""
        return self._run.secrets

    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str:
        """Ask a question, during `flnd login` only; `flnd download` never asks, and raises `RuntimeError`."""
        self._check_interactive()
        ask = prompt.secret if secret else prompt.input
        return ask(message, description=description)

    def choose(self, message: str, options: Sequence[str] | Mapping[str, str]) -> str:
        """Ask to pick one option, during `flnd login` only, like `ask`; a mapping shows its keys."""
        self._check_interactive()
        return prompt.choice(message, options)

    def info(self, message: str, **fields: Any) -> None:
        """A `log.info` line naming the target: `session.info("{tab}: {n} items", tab="posts", n=3)`."""
        log.info("{target}: " + message, target=str(self.target), **fields)

    def warn(self, message: str, **fields: Any) -> None:
        """A `log.warn` line naming the target, like `info`."""
        log.warn("{target}: " + message, target=str(self.target), **fields)

    def challenged(self) -> None:
        """Report a challenge (a captcha nobody solved): the source pauses, as after a 429."""
        self._run.backoff.hit("challenged")
        self._run.backoff.wait_turn()

    def finish(self, error: BaseException | None = None) -> int:
        """Close the browser, wait for the downloads (or drop queued ones after `error`), and zip with `--zip`.

        It returns how many files were written, after logging each failed one.
        """
        if self._browser is not None:
            self._run.http.cookies.update(net.cookie_jar(self._browser.cookies.all))
            self._browser.close()
            self._browser = self._page = None
        if self._pool is None:
            return 0
        self._pool.__exit__(type(error) if error else None, error, error.__traceback__ if error else None)
        # The pool keeps failures in its results instead of raising, so each one is named here.
        for result in self._pool.results:
            if not result.ok:
                log.error("{file}: {kind} {error}", file=result.item, kind=type(result.error).__name__,
                          error=result.error)
        if self._settings.options.zip and error is None:
            self._zip()
        return sum(1 for result in self._pool.results if result.value is True)

    def _send(self, method: str, url: str, headers: Mapping[str, str] | None, request: dict, *,
              soft: bool = False) -> httpx.Response:
        if self._browser is not None:
            # Cookies the site set in the browser reach the client too, as a logged-in page would send them.
            self._run.http.cookies.update(net.cookie_jar(self._browser.cookies.all))
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
        self._page = self._browser.open_page()
        for listener in self._listeners:
            listener.attach(self._page)
        return self._page.goto(url, idle=idle, timeout=timeout)

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
        connected = browser.connect(headless=settings.headless, viewport=source.viewport, locale=settings.locale,
                                    timezone=settings.timezone, args=source.browser_args, stealth=source.stealth)
        connected.cookies.add(self._run.jar)
        return connected

    def _target_dir(self) -> Path:
        return self._settings.downloads / self._source.code / fmt.filename(self.target.value)

    def _path(self, folder: str, name: str) -> Path:
        # Every part goes through fmt.filename, so a name from a site can never leave the target's folder.
        parts = [fmt.filename(part) for part in folder.split("/") if part]
        return self._target_dir().joinpath(*parts, fmt.filename(name))

    def _already_saved(self, dest: Path, history_key: str) -> bool:
        if dest.is_file() and dest.stat().st_size > 0:
            return True
        row = self._settings.history.get(history_key)
        return isinstance(row, dict) and Path(row.get("path", "")).is_file()

    def _downloads(self) -> worker.Pool:
        if self._pool is None:
            from shared import worker

            self._pool = worker.pool(concurrency=self._source.concurrency, progress=str(self.target))
        return self._pool

    def _download(self, file: _File) -> bool:
        source = file.source
        if callable(source):
            source = self._sync_fetch(source)
        saved = net.download(self._run.http, source, file.dest, label=file.label, backoff=self._run.backoff,
                             headers=file.headers, wait=type(self._source).wait, size=file.size)
        if not saved:
            return False
        self._settings.history[file.key] = {
            "path": str(file.dest), "size": file.dest.stat().st_size,
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }
        if self._settings.options.unpack and _is_archive(file.dest.name):
            self._unpack(file)
        return True

    def _sync_fetch(self, fetch: net.Fetch) -> net.Fetch:
        def call(dest: Path, progress: Callable[..., None]) -> Any:
            outcome = fetch(dest, progress)
            return self._run.run_async(outcome) if inspect.iscoroutine(outcome) else outcome

        return call

    def _unpack(self, file: _File) -> None:
        from shared import archive

        folder = file.dest.parent / _without_archive_suffixes(file.dest.name)
        try:
            archive.unzip(file.dest, folder, password=file.password, conflict="skip")
        except (OSError, ValueError, RuntimeError) as error:
            log.error("{file}: can't unpack: {error}", file=file.label, error=error)

    def _zip(self) -> None:
        from shared import archive

        folder = self._target_dir()
        if folder.is_dir():
            written = archive.zip(folder, folder.with_name(folder.name + ".zip"))
            log.ok("{target}: zipped to {path}", target=str(self.target), path=written[0])

    def _check_interactive(self) -> None:
        if not self._settings.interactive:
            code = self._source.code
            raise RuntimeError(f"{code}: flnd download never asks questions; run flnd login {code} first")


@dataclass
class _File:
    """One queued download. It prints as `folder/name`, so a failed download names its file."""

    source: net.Download
    dest: Path
    label: str
    key: str
    password: str | None
    size: int | None
    headers: Mapping[str, str] | None

    def __str__(self) -> str:
        return self.label


@dataclass
class _Listener:
    """A handler the session attaches to every tab it opens, until it is stopped."""

    event: str
    match: Match
    callback: Callable[..., Any]
    lines: bool = False
    stops: list[Callable[[], None]] = field(default_factory=list)

    def attach(self, page: Page) -> None:
        if self.event == "response":
            self.stops.append(page.on_response(self.match, self.callback, lines=self.lines))
        else:
            self.stops.append(page.on_request(self.match, self.callback))

    def detach(self) -> None:
        for stop in self.stops:
            stop()
        self.stops.clear()


def check_category(source: Source, category: str) -> None:
    """Raise `ValueError` when a source saves into a category it doesn't declare: a mistake in the source."""
    if category not in source.categories:
        raise ValueError(f"{source.code}: unknown category {category!r}; declare it in categories "
                         f"({', '.join(source.categories)})")


def _show_steps(steps: float) -> str:
    return f"{steps:g} steps"


def _is_archive(name: str) -> bool:
    return _without_archive_suffixes(name) != Path(name).name


def _without_archive_suffixes(name: str) -> str:
    # "photos.7z.001" and "photos.tar.gz" both unpack into "photos".
    stem = Path(name)
    while stem.suffix.lower() in ARCHIVE_SUFFIXES or stem.suffix[1:].isdigit():
        stem = stem.with_suffix("")
    return stem.name or name
