"""
Saving files for flnd: `save`, one download pool per target, the download history, and scrolling.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, MutableMapping, Optional, Set

import httpx

from shared import browser, log, store, worker

# Each saved file is recorded here, next to the downloads folder: `.flnd/state.db`.
HISTORY_FILE = "state.db"
HISTORY_TABLE = "downloads"
# flnd has always scrolled a feed with a 2000 px mouse wheel step, then 500 px more after each wait.
WHEEL_STEP = 2000
SCROLL_AFTER_WAIT = "() => { window.scrollBy(0, 500); }"


def save(
    client: httpx.Client,
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

    def __init__(self, client: httpx.Client, url: str, dest: Path, key: str, headers: Optional[Dict[str, str]]):
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
            self.history = exits.enter_context(store.open(self.history_file)).table(HISTORY_TABLE)
            self.pool = exits.enter_context(
                worker.pool(workers=DOWNLOAD_WORKERS, retries=2, errors="log", progress=self.label)
            )
            self._exits = exits.pop_all()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._exits.__exit__(*exc_info)

    @property
    def full(self) -> bool:
        return bool(self.limit) and self.pool.submitted >= self.limit

    @property
    def saved(self) -> int:
        """How many files were written. Read it after the block, when every download has finished."""
        return sum(1 for result in self.pool.results if result.value is True)

    def add(self, client: httpx.Client, url: str, dest: Path, *, key: str,
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


def scroll(
    page: Any,
    label: str,
    *,
    count: Callable[[], int],
    more: Callable[[], bool],
    delay: float = 2.0,
    step: Optional[Callable[[Any], None]] = None,
) -> None:
    """`browser.scroll` the way flnd has always scrolled, under a `scroll <label>` spinner.

    A failing scroll step is ignored, as before.
    """

    def scroll_step(p: Any) -> None:
        try:
            step(p) if step else p.mouse.wheel(0, WHEEL_STEP)
        except Exception:
            pass

    with log.progress(f"scroll {label}", show=show_steps) as bar:

        def after_wait() -> None:
            # Before, this ran in the same script that read the page height, so the height is unchanged.
            page.evaluate(SCROLL_AFTER_WAIT)
            bar.advance()

        browser.scroll(page, count=count, more=more, idle=4, delay=delay, step=scroll_step, onstep=after_wait)


def show_steps(steps: float) -> str:
    """How a scroll spinner shows its count: `12 steps`."""
    return f"{steps:g} steps"
