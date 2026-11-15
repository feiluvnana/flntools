"""flnd: a dead simple media downloader, usable as a Python library or CLI."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

__version__ = "0.1.0"

from flnd.engine import AuthInfo, Engine, Error, Report, TargetInfo, TargetReport
from flnd.options import RunShared
from flnd.site import TargetSite, source_run
from flnd.source import LoginError, Range, Source, Target, TargetError
from flnd.sources import SOURCES
from shared import cli as _cli

__all__ = [
    "AuthInfo",
    "Engine",
    "Error",
    "LoginError",
    "Range",
    "Report",
    "SOURCES",
    "Source",
    "Target",
    "TargetError",
    "TargetInfo",
    "TargetReport",
    "__version__",
    "download",
    "open",
    "resolve",
    "source",
]


def source(code: str) -> Source:
    """Instantiate a registered source by its short code (e.g. `'twt'`, `'ins'`)."""
    cls = SOURCES.get(code)
    if cls is None:
        raise TargetError(f"unknown source {code!r}; known: {', '.join(SOURCES)}")
    return cls()


def resolve(text: str) -> Target:
    """Resolve one target spec or URL into a `Target`."""
    return Engine().resolve([text])[0]


@contextmanager
def open(
    code: str,
    *,
    home: Path = Path(".flnd"),
    time: Range | str = Range(),
) -> Iterator[TargetSite]:
    """Open a `Site` for `code` with saved cookies, HTTP client, and state."""
    src = source(code)
    span = Range.parse(time) if isinstance(time, str) else time
    with _cli.stopping() as stop:
        settings = RunShared(home=home, history={}, stop=stop)
        with source_run(settings, src) as run:
            site = TargetSite(run, Target(src.code, "account", ""), time=span)
            try:
                yield site
            finally:
                site.close()


def download(
    targets: Target | str | Sequence[Target | str],
    *,
    categories: str | Sequence[str] | None = None,
    time: Range | str = Range(),
    limit: int | None = None,
    dry: bool = False,
    sync: bool = False,
    again: bool = False,
    home: Path = Path(".flnd"),
) -> Report:
    """Download one or more targets sequentially and return a `Report`."""
    items = [targets] if isinstance(targets, (str, Target)) else list(targets)
    return Engine(home=home).download(
        items,
        categories=categories,
        time=time,
        limit=limit,
        dry=dry,
        sync=sync,
        again=again,
    )
