"""
Saving files for flnd: `save`, one download pool per target, the download history, and scrolling.
"""

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, MutableMapping, Optional, Sequence, Set

import httpx

from shared import browser, http, kv, log, worker

# Each saved file is recorded here, next to the downloads folder: `.flnd/history.db`. The old
# `.flnd/state.db` has another table layout, so the history starts a new file instead of reading it.
HISTORY_FILE = "history.db"
HISTORY_TABLE = "downloads"


def save(
    client: http.Client,
    url: str,
    dest: Path,
    *,
    headers: Optional[Dict[str, str]] = None,
    key: Optional[str] = None,
    history: Optional[MutableMapping[str, Any]] = None,
) -> bool:
    """Download url into dest. True when a file was written; False when dest exists or the server refused.

    A status error is logged and gives False. A network error is raised, so the pool can retry it.
    """
    name = f"{dest.parent.name}/{dest.name}"
    try:
        if not client.download(url, dest, label=name, headers=headers):
            return False
    except httpx.HTTPStatusError as err:
        log.error("{name}: {err}", name=name, err=err)
        return False
    if history is not None and key:
        history[key] = {
            "path": str(dest),
            "size": dest.stat().st_size,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    return True


class File:
    """One queued download. It prints as `folder/name`, so a failed download names its file."""

    def __init__(self, client: http.Client, url: str, dest: Path, key: str, headers: Optional[Dict[str, str]]):
        self.client, self.url, self.dest, self.key, self.headers = client, url, dest, key, headers

    def __str__(self) -> str:
        return f"{self.dest.parent.name}/{self.dest.name}"


class Downloads:
    """One target's downloads: files are saved in a worker pool while the source keeps listing.

    `limit` counts queued files. The browser stays in the source's thread; pool threads only run `save`.
    """

    def __init__(self, source: str, target: Any, download_dir: Path, limit: Optional[int] = None):
        self.source = source
        self.label = str(target)
        self.limit = limit
        self.history_file = Path(download_dir).parent / HISTORY_FILE
        # Every dest queued so far, so one file is never written by two threads at once.
        self.claimed: Set[Path] = set()
        self._exits = ExitStack()

    def __enter__(self) -> "Downloads":
        # flnd.cli imports every source, and every source imports this module.
        from flnd.cli import DOWNLOAD_WORKERS

        with ExitStack() as exits:
            self.history = exits.enter_context(kv.open(self.history_file, table=HISTORY_TABLE))
            self.pool = exits.enter_context(worker.pool(concurrency=DOWNLOAD_WORKERS, retries=2, progress=self.label))
            self._exits = exits.pop_all()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._exits.__exit__(*exc_info)
        # The pool keeps failures in its results instead of raising, so each one is named here.
        for result in self.pool.results:
            if not result.ok:
                log.error("{file}: {kind} {err}", file=result.item, kind=type(result.error).__name__, err=result.error)

    @property
    def full(self) -> bool:
        return bool(self.limit) and self.pool.submitted >= self.limit

    @property
    def saved(self) -> int:
        """How many files were written. Read it after the block, when every download has finished."""
        return sum(1 for result in self.pool.results if result.value is True)

    def add(self, client: http.Client, url: str, dest: Path, *, key: str,
            headers: Optional[Dict[str, str]] = None) -> bool:
        """Queue a download. False when the limit is reached or dest is already queued."""
        if self.full:
            return False
        if dest in self.claimed:
            log.info("skip {name}", name=dest.name)
            return False
        self.claimed.add(dest)
        self.pool.submit(self._save, File(client, url, dest, key, headers))
        if self.full:
            log.info("Reached limit of {limit} downloads.", limit=self.limit)
        return True

    def _save(self, file: File) -> bool:
        return save(file.client, file.url, file.dest, headers=file.headers,
                    key=f"{self.source}:{file.key}", history=self.history)


@contextmanager
def open_page(*, headless: bool, cookies: List[Dict[str, Any]], args: Sequence[str] = (),
              stealth: bool = True) -> Iterator[Any]:
    """A page in a fresh incognito browser, logged in with a source's cookies; closed with the block."""
    with browser.connect(headless=headless, args=args, stealth=stealth) as connected:
        connected.cookies.add([for_browser(cookie) for cookie in cookies])
        yield connected.open_page()


def for_browser(cookie: Dict[str, Any]) -> Dict[str, Any]:
    """A cookie from a cookie file as the browser should get it: a leading-dot domain, and path `/`."""
    # Exported files often hold host-only domains; the dot lets every subdomain see the cookie, as before.
    domain = cookie.get("domain", "")
    return {"name": cookie["name"], "value": cookie["value"], "path": cookie.get("path") or "/",
            "domain": domain if domain.startswith(".") else "." + domain}


def scroll(
    page: Any,
    label: str,
    *,
    count: Callable[[], int],
    more: Callable[[], bool],
    delay: float = 2.0,
    step: Optional[Callable[[Any], None]] = None,
) -> int:
    """`page.scroll` through a feed under a `scroll <label>` spinner, and return the number of scrolls.

    A failing custom `step` is ignored, as flnd always has: one bad scroll shouldn't end a feed.
    """

    by = _ignoring_errors(step) if step is not None else None
    with log.progress(f"scroll {label}", show=show_steps) as bar:
        return page.scroll(count=count, more=more, idle=4, delay=delay, by=by, onscroll=bar.advance)


def _ignoring_errors(step: Callable[[Any], None]) -> Callable[[Any], None]:
    def guarded(raw_page: Any) -> None:
        try:
            step(raw_page)
        except Exception:
            pass

    return guarded


def show_steps(steps: float) -> str:
    """How a scroll spinner shows its count: `12 steps`."""
    return f"{steps:g} steps"
