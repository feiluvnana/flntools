"""Benchmark group `e2e`: a whole flnd run on a faked site, through the real engine and clock.

The site is a source written here, `Bench` (code `bnc`), whose API answers pages of 50 posts;
each post has a photo and a video of 100 KiB. httpx is given a `MockTransport` that answers
every request in-process, after an optional sleep that stands in for network latency. Nothing
leaves the process, and the pace between API requests is the real one.

The runs:
1. 2,000 posts with no latency and no pace: the engine's own cost, per file;
2. the same target again with nothing new (`stop_at_saved`): the cost of a sync;
3. 500 posts with 150 ms per API page and 40 ms per file, at the source defaults for pace and
   concurrency: what a real run is bound by.

Each run is timed once: they are long enough that noise is small.
"""

from __future__ import annotations

import io
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import httpx

import kit
from flnd.engine import Engine
from flnd.options import Options
from flnd.source import File, Post, Site, Source, pages, target
from shared import cli, ui

API, CDN, PER_PAGE = "https://api.bench.example", "https://cdn.bench.example", 50
BODY = b"\0" * (100 * 1024)
# The first bytes of a JPEG and of an MP4, so the engine's type sniffing sees real media.
HEADS = {".jpg": (b"\xff\xd8\xff\xe0", "image/jpeg"), ".mp4": (b"\0\0\0\x18ftypmp42", "video/mp4")}


class Fake:
    """What the fake site does in the current run: its size and its latency.

    Module state, not arguments, because `answer` is called by httpx with the request only.
    """

    posts = 0
    # Seconds slept before answering an API page, and before answering a file.
    page_latency = 0.0
    file_latency = 0.0


class Bench(Source):
    """A minimal source over the fake site: one target, one category, paged by a cursor."""

    code, name = "bnc", "Bench"
    domains = ("bench.example",)
    folder = "{category}"

    @target("{user}")
    def user(self, site: Site, user: str) -> dict:
        """The target's profile; the fake site needs nothing but the handle."""
        return {"id": user}

    @user.category(newest_first=True)
    def posts(self, site: Site, profile: dict) -> Iterator[Post]:
        """Every post, newest first, page by page until the API sends no cursor."""
        def fetch(cursor: str | None) -> dict:
            return site.fetch(f"{API}/posts", params={"cursor": cursor or "0"}).json

        for page in pages(fetch, lambda page: page.get("next")):
            for item in page["items"]:
                yield Post(item["id"], [File(url) for url in item["urls"]], date=item["taken_at"])


def answer(request: httpx.Request) -> httpx.Response:
    """Answer one request of the fake site: an API page, or a media file.

    Called by httpx on whichever thread made the request, so the sleeps block only that thread,
    as real network waits would.
    """
    if request.url.host == "api.bench.example":
        time.sleep(Fake.page_latency)
        start = int(request.url.params.get("cursor", "0"))
        end = min(start + PER_PAGE, Fake.posts)
        items = [{"id": f"p{n}", "taken_at": 1767312000 - n,
                  "urls": [f"{CDN}/{n}.jpg", f"{CDN}/{n}.mp4"]} for n in range(start, end)]
        cursor = str(end) if end < Fake.posts else None
        return httpx.Response(200, json={"items": items, "next": cursor})
    time.sleep(Fake.file_latency)
    head, kind = HEADS[request.url.path[-4:]]
    return httpx.Response(200, content=head + BODY,
                          headers={"content-type": kind, "content-length": str(len(head + BODY))})


def run(home: Path, *, again: bool = False) -> float:
    """Download the target `bnc:example_user` into `home` once, and return the wall time.

    Args:
        home: the flnd home folder; reused by a second run to time a sync.
        again: stop at the first saved post, as a sync does.
    """
    engine = Engine(home=home, sources={"bnc": Bench})
    stop = cli.Stopping(threading.Event(), threading.Event())
    options = Options(categories=("posts",), stop_at_saved=again)
    start = time.perf_counter()
    for _ in engine._run([(t, options) for t in engine.resolve(["bnc:example_user"])], stop=stop):
        pass
    return time.perf_counter() - start


def main() -> None:
    """Run the three scenarios and emit the report."""
    report = kit.Report("e2e")
    default_pace = Bench.pace
    transport = mock.patch.object(httpx.Client, "_init_transport",
                                  lambda *args, **kwargs: httpx.MockTransport(answer))
    with transport, ui.scope(dest=io.StringIO(), color=False), \
            tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        # 1 and 2: the engine alone.
        Fake.posts, Bench.pace = 2000, 0.0
        home = Path(scratch, "fast", ".flnd")
        took = run(home)
        files = sum(1 for path in (home / "media").rglob("*") if path.is_file())
        if files != 2 * Fake.posts:
            raise SystemExit(f"e2e: expected {2 * Fake.posts} files, found {files}")
        report.add("per_file", took / files * 1e3, "ms")
        report.add("sync_nothing_new", run(home, again=True), "s")
        # 3: latency, at the defaults.
        Fake.posts, Bench.pace = 500, default_pace
        Fake.page_latency, Fake.file_latency = 0.150, 0.040
        report.add("latency_500_posts", run(Path(scratch, "slow", ".flnd")), "s")
    report.emit()


if __name__ == "__main__":
    main()
