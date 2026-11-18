"""flnd's own test helpers, over the public test kit `flnd.testing`.

- `FakeSite` and `collect` are `flnd.testing`'s, taking the run's whole `Options` as flnd's own
  tests need (`options=Options(categories=…, stop_at_saved=…)`), where the public kit takes
  `only` and `time`. `Seen` is the kit's own.
- `history` and `targets` read what a run saved in a home's `flnd.db`.
- `fetch` and `download` drive the HTTP library (`flnd.kit.net`) from a test's own thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

import no_profiles  # noqa: F401  (refuses real browser profiles)

from flnd import testing
from flnd.kit import net as _net
from flnd.options import Options
from flnd.source import Range, Source, Target
from flnd.testing import Collected, Seen

__all__ = ["Collected", "FakeSite", "Seen", "collect", "download", "fetch", "history",
           "targets"]

# A fixture: data for a URL, a SiteError to raise, or a function of (url, request) giving either.
Answer = Any


class FakeSite(testing.FakeSite):
    """`flnd.testing.FakeSite`, whose `--when` comes from the run's `Options`.

        site = FakeSite(Target("exm:example_user", kind="user"), options=Options(since=day))

    Args:
        target: the target it acts for.
        options: the run's options; `time` reads `--when` from them.
        **given: the rest of `flnd.testing.FakeSite`'s arguments.
    """

    def __init__(self, target: Target | None = None, *, options: Options = Options(),
                 **given: Any) -> None:
        super().__init__(target, time=Range(options.since, options.until), **given)


def collect(source: type[Source], text: str, *, json: Mapping[str, Answer] | None = None,
            html: Mapping[str, Answer] | None = None, pages: Mapping[str, str] | None = None,
            options: Options = Options(), cookies: Mapping[str, str] | None = None,
            client: Any = None, seen: Seen | None = None,
            state: MutableMapping[str, Any] | None = None, feeds: Any = ()) -> list[Collected]:
    """`flnd.testing.collect` with the run's whole `Options`, and the site's fixtures inline.

        saved = collect(Example, "exm:example_user", json=FIXTURES,
                        options=Options(categories=("posts",)))
        [s.path for s in saved]     # ["posts/2026-01-02_C1abc_0.jpg", …]

    Args:
        source: the source class.
        text: the target as people write it, `code:` or a link.
        json, html, pages, cookies, client, seen, feeds: the `FakeSite`'s, as it takes them.
        state: the site's saved state, shared by runs that pass the same dict.
        options: the run's options: `categories`, `--when`, `stop_at_saved`, ….

    Returns:
        What `flnd.testing.collect` returns.
    """
    site = FakeSite(None, cookies=cookies, json=json, html=html, pages=pages, options=options,
                    client=client, seen=seen, state=state, feeds=feeds)
    return testing._collect(source, text, site, options)


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
    """Every target row in a home's `flnd.db`, by key (`flnd.kit.db.TargetRow`)."""
    from pathlib import Path

    from flnd import _home

    with _home.open(Path(home)) as db:
        return db.targets()
# ---------------------------------------------------------------------------------------------
# The HTTP library, driven from a test's own thread
# ---------------------------------------------------------------------------------------------
# `flnd.kit.net` runs on an asyncio loop. These run one call on a loop of its own (`asyncio.run`,
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
    from flnd.kit import files as _files

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
