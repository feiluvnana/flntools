"""Benchmark group `cpu`: the pure-CPU work of a run, each piece timed alone.

What it times, in order:
1. JSON: decoding and encoding a 2 MiB feed page shaped like an Instagram timeline, with the
   stdlib and with msgspec (untyped, and typed to the few fields a source reads);
2. a JSON path: `dig` against plain dict access for the same nodes;
3. HTML: parsing a 2 MiB Meta-style page (40 scripts, 6 of them JSON, and 20,000 posts) with
   `flnd._html` (selectolax), then decoding its JSON script blocks (`html_meta`, the number a
   Facebook or Instagram page costs) or reading its 20,000 `img[src]` (`html_images`);
4. hashing 256 MiB in memory, per MiB;
5. the loop bridge: a thread calling a coroutine on an event loop thread and waiting for it;
6. flnd's per-file work: `job.place` (paths and names), `text.filename` and `day()`;
7. a no-op task handed to the run's loop, and a progress row's `advance`.

Every number is the best of 3 runs (5 for the short per-call ones). The pages are synthetic and
use only example handles and example.com-style hosts.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import msgspec

import kit

# `example_source.py`, the test source, lives in tests/, one folder up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from example_source import CDN, Example  # noqa: E402
from flnd import job  # noqa: E402
from flnd.source import File, Post  # noqa: E402
from flnd._time import day  # noqa: E402
from flnd import _html, _text as text  # noqa: E402
from flnd._loop import Loop  # noqa: E402
from flnd._ui import ui

EDGES = "data.user.edge_owner_to_timeline_media.edges[].node"


# Only the fields a source reads; msgspec skips the rest of the page without building it.
class Node(msgspec.Struct):
    id: str
    shortcode: str
    taken_at_timestamp: int
    display_url: str
    is_video: bool


class Edge(msgspec.Struct):
    node: Node


class Media(msgspec.Struct):
    edges: list[Edge]


class User(msgspec.Struct):
    edge_owner_to_timeline_media: Media


class Data(msgspec.Struct):
    user: User


class Page(msgspec.Struct):
    data: Data


def feed_page() -> bytes:
    """A 2 MiB timeline page of 1,500 posts, as JSON bytes."""
    edges = [{"node": {
        "id": str(10**18 + n), "shortcode": f"C{n:010d}", "taken_at_timestamp": 1700000000 + n,
        "display_url": f"https://cdn.example.com/{n}.jpg?" + "x" * 200, "is_video": n % 3 == 0,
        "edge_media_to_caption": {"edges": [{"node": {"text": "caption " * 20}}]},
        "dimensions": {"height": 1350, "width": 1080}}} for n in range(1500)]
    page = {"data": {"user": {"edge_owner_to_timeline_media": {"edges": edges}}}}
    return json.dumps(page).encode()


def meta_page() -> str:
    """A 2 MiB Meta-style HTML page: 34 plain scripts, 6 JSON ones, and 20,000 posts."""
    blocks = "".join(
        '<script type="application/json" data-sjs>'
        + json.dumps({"require": [["x", {"__bbox": {"result": {"data": {"n": n,
                                                                        "pad": "y" * 30000}}}}]]})
        + "</script>" for n in range(6))
    plain = "".join(f"<script>var a{n} = {n};</script>" for n in range(34))
    body = "".join(f'<div class="c{n % 7}"><a href="/p/{n}">post {n}</a>'
                   f'<img src="https://cdn.example.com/{n}.jpg"></div>' for n in range(20000))
    return f"<html><head>{plain}{blocks}</head><body>{body}</body></html>"


def json_and_paths(report: kit.Report) -> None:
    """Time steps 1 and 2: JSON decode and encode, and the JSON path query."""
    raw = feed_page()
    report.add("json_decode", kit.best(lambda: json.loads(raw)) * 1e3, "ms")
    report.add("msgspec_decode", kit.best(lambda: msgspec.json.decode(raw)) * 1e3, "ms")
    report.add("msgspec_decode_typed",
               kit.best(lambda: msgspec.json.decode(raw, type=Page)) * 1e3, "ms")
    page = json.loads(raw)
    report.add("json_encode", kit.best(lambda: json.dumps(page)) * 1e3, "ms")
    report.add("msgspec_encode", kit.best(lambda: msgspec.json.encode(page)) * 1e3, "ms")
    report.add("dig", kit.best(lambda: _html.dig(page, EDGES)) * 1e6, "us")
    edges = page["data"]["user"]["edge_owner_to_timeline_media"]["edges"]
    report.add("dict_access", kit.best(lambda: [e["node"] for e in edges]) * 1e6, "us")


def html(report: kit.Report) -> None:
    """Time step 3: the Meta-style page, parsed and read as the sources read it."""
    page = meta_page()
    report.add("html_meta", kit.best(lambda: _html.parse(page).blocks("__bbox")) * 1e3, "ms")

    def images() -> list[str | None]:
        return [image.attr("src") for image in _html.parse(page).css("img")]

    report.add("html_images", kit.best(images) * 1e3, "ms")


def hashes(report: kit.Report) -> None:
    """Time step 4: hashing 256 MiB, reported per MiB."""
    blob = os.urandom(256 << 20)
    for name in ("sha256", "blake2b"):
        took = kit.best(lambda name=name: hashlib.new(name, blob).digest())
        report.add(f"hash_{name}", took / 256 * 1e6, "us")
    report.add("hash_crc32", kit.best(lambda: zlib.crc32(blob)) / 256 * 1e6, "us")


def bridge(report: kit.Report) -> None:
    """Time step 5: `run_coroutine_threadsafe(...).result()` from a thread, per call."""
    calls = 20_000
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def noop() -> int:
        return 1

    def work() -> None:
        for _ in range(calls):
            asyncio.run_coroutine_threadsafe(noop(), loop).result()

    report.add("loop_bridge", kit.best(work) / calls * 1e6, "us")
    loop.call_soon_threadsafe(loop.stop)
    thread.join()
    loop.close()


def per_file(report: kit.Report) -> None:
    """Time step 6: `job.place`, `text.filename` and `day()`, per call."""
    files = [File(f"{CDN}/a/{n}.jpg") for n in range(2)]
    posts = [Post(f"C{n:08d}", files, date=1767312000 + n,
                  title=f"a title number {n} with é and / slash").filed("posts")
             for n in range(5000)]
    source = Example()

    def place() -> None:
        for post in posts:
            job.place(source, post, files)

    report.add("place", kit.best(place, runs=5) / (2 * len(posts)) * 1e6, "us")
    names = [f"un nom très long / avec: des <caractères> spéciaux {n}" for n in range(20000)]
    took = kit.best(lambda: [text.filename(name) for name in names], runs=5)
    report.add("filename", took / len(names) * 1e6, "us")

    def days() -> None:
        for n in range(20000):
            day(1767312000 + n)
            day("2026-01-02T03:04:05Z")

    report.add("day", kit.best(days, runs=5) / 40000 * 1e6, "us")


def pool_and_ui(report: kit.Report) -> None:
    """Time step 7: a no-op task handed to the run's loop, and a progress row's `advance`.

    The downloads are tasks on the run's loop, each handed over from the listing thread
    (`Loop.submit`); `loop_task` is that hop's cost, end to end.
    """
    tasks = 20_000

    async def nothing(n: int) -> int:
        return n

    def loop() -> None:
        with Loop() as running:
            for future in [running.submit(nothing(n)) for n in range(tasks)]:
                future.result()

    report.add("loop_task", kit.best(loop) / tasks * 1e6, "us")

    def executor() -> None:
        with ThreadPoolExecutor(8) as running:
            list(running.map(abs, range(tasks)))

    report.add("executor_task", kit.best(executor) / tasks * 1e6, "us")
    advances = 100_000
    for color, name in ((False, "advance_plain"), (True, "advance_live")):
        def advance(color: bool = color) -> None:
            with ui.scope(dest=io.StringIO(), color=color), \
                    ui.progress("file.mp4", total=advances) as row:
                for _ in range(advances):
                    row.advance(1)

        report.add(name, kit.best(advance) / advances * 1e6, "us")


def main() -> None:
    """Run every step and emit the report."""
    report = kit.Report("cpu")
    json_and_paths(report)
    html(report)
    hashes(report)
    bridge(report)
    per_file(report)
    pool_and_ui(report)
    report.emit()


if __name__ == "__main__":
    main()
