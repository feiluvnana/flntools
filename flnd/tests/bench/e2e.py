"""Benchmark group `e2e`: a whole flnd run on a faked site, through the real engine and clock.

The site is a source written here, `Bench` (code `bnc`), whose API answers pages of 50 posts;
each post has a photo and a video of 100 KiB. `flnkit.net.fake` answers every request
in-process on the run's loop, after an optional `asyncio.sleep` that stands in for network
latency. Nothing leaves the process, and the pace between API requests is the real one.

The runs:
1. 2,000 posts with no latency and no pace: the engine's own cost, per file;
2. the same target again with nothing new (`stop_at_saved`): the cost of a sync;
3. the same sync once the home's `flnd.db` holds 100,000 files: the 96,000 rows past the 4,000
   downloaded are written straight into the database (`grow`), so the case stays quick. Their
   files aren't on disk, which a sync never looks at: it stops at the first saved posts;
4. 500 posts with 150 ms per API page and 40 ms per file, at the source defaults for pace and
   concurrency: what a real run is bound by;
5. `sync_100_targets`: 100 followed targets of 10 posts each, downloaded once, then synced with
   nothing new at 150 ms per API page and no pace (`Engine.sync`): what a sync of many small
   targets waits for;
6. `db_contended`: 500 files (250 posts, 40 ms per file, no pace) while another process holds
   `flnd.db`'s write lock for 2 s, taken once 50 files were answered; a probe on the run's loop
   wakes every 5 ms, and the number is the longest the loop was held up.

Each run is timed once: they are long enough that noise is small.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import kit

from flnd.engine import home as _home
from flnd.engine import run as _run
from flnd.engine import store as _store
from flnd.engine.core import Engine, _drive
from flnd.engine.options import Options
from flnd.plugin import Ctx, File, Plugin, Post, Request, Target, route
from flnd.ui.log import ui
from flnkit import net as _net

API, CDN, PER_PAGE = "https://api.bench.example", "https://cdn.bench.example", 50
# The files the home of run 3 holds: the size of a large followed account.
LARGE = kit.size(100_000, 1000)
# The posts of the runs without latency (1 to 3), and of the run with it (4).
POSTS, SLOW_POSTS = kit.size(2000, 20), kit.size(500, 10)
# Run 5: the followed targets, and the posts of each.
FOLLOWED, FOLLOWED_POSTS = kit.size(100, 3), 10
# Run 6: its posts, the files answered before the lock is taken, and how long it is held.
HELD_POSTS, HELD_AFTER, HOLD = kit.size(250, 20), kit.size(50, 5), kit.size(2000, 300) / 1e3
PROBE = 0.005
# The other process of run 6: told to, it waits for the run's first commit to make the
# database, takes the write lock, says so, holds it for the seconds it is given, and lets go.
HOLDER = """
import os, sqlite3, sys, time
sys.stdin.readline()
while not os.path.exists(sys.argv[1]):
    time.sleep(0.005)
db = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=30)
db.execute("BEGIN IMMEDIATE")
print("held", flush=True)
time.sleep(float(sys.argv[2]))
db.execute("COMMIT")
"""
BODY = b"\0" * (100 * 1024)
# The first bytes of a JPEG and of an MP4, so the engine's type sniffing sees real media.
HEADS = {".jpg": (b"\xff\xd8\xff\xe0", "image/jpeg"), ".mp4": (b"\0\0\0\x18ftypmp42", "video/mp4")}


class Fake:
    """What the fake site does in the current run: its size and its latency.

    Module state, not arguments, because `answer` is called by the fake with the request only.
    """

    posts = 0
    # Seconds slept before answering an API page, and before answering a file.
    page_latency = 0.0
    file_latency = 0.0
    # Run 6: files answered so far, and what each answered file calls (None for nothing).
    answered = 0
    on_file: Any = None


class Bench(Plugin):
    """A minimal plugin over the fake site: one target, one category, paged by a cursor."""

    code, name, api = "bnc", "Bench", 2
    domains = ("bench.example",)
    folder = "{category}"

    @route("bnc:{user}", newest_first=False)
    async def user(self, ctx: Ctx, user: str) -> dict:
        """The target's profile; the fake site needs nothing but the handle."""
        return {"id": user}

    @user.category(newest_first=True)
    async def posts(self, ctx: Ctx, profile: dict) -> AsyncIterator[Post]:
        """Every post, newest first, page by page until the API sends no cursor."""
        first = Request(f"{API}/posts", params={"user": profile["id"]})
        async for page in ctx.pages(first, cursor="next"):
            for item in page.json["items"]:
                yield Post(item["id"], [File(url) for url in item["urls"]], date=item["taken_at"])


async def answer(request: _net.Request) -> _net.Reply:
    """Answer one request of the fake site: an API page, or a media file.

    Called on the run's loop; the sleeps are the loop's, so they hold up only this request, as
    real network waits would.
    """
    if request.url.host == "api.bench.example":
        await asyncio.sleep(Fake.page_latency)
        start = int(request.url.params.get("cursor", "0"))
        end = min(start + PER_PAGE, Fake.posts)
        # Each target's posts are its own: `example_user`'s are p0, p1…, another's `<user>.p0`.
        user = request.url.params.get("user", "example_user")
        prefix = "" if user == "example_user" else f"{user}."
        items = [{"id": f"{prefix}p{n}", "taken_at": 1767312000 - n,
                  "urls": [f"{CDN}/{prefix}{n}.jpg", f"{CDN}/{prefix}{n}.mp4"]}
                 for n in range(start, end)]
        cursor = str(end) if end < Fake.posts else None
        return _net.reply(200, json={"items": items, "next": cursor})
    await asyncio.sleep(Fake.file_latency)
    Fake.answered += 1
    if Fake.on_file is not None:
        Fake.on_file()
    head, kind = HEADS[request.url.path[-4:]]
    return _net.reply(200, content=head + BODY,
                      headers={"content-type": kind, "content-length": str(len(head + BODY))})


def run(home: Path, *, again: bool = False) -> float:
    """Download the target `bnc:example_user` into `home` once, and return the wall time.

    Args:
        home: the flnd home folder; reused by a second run to time a sync.
        again: stop at the first saved post, as a sync does.
    """
    engine = Engine(home=home, sources={"bnc": Bench})
    stop = _run.Stop()
    options = Options(categories=("posts",), stop_at_saved=again)
    chosen = [(t, options) for t in engine.resolve(["bnc:example_user"])]

    async def command() -> None:
        async with engine._live(), engine._running(stop) as runner:
            await runner.run(chosen)

    start = time.perf_counter()
    _drive(command())
    return time.perf_counter() - start


def grow(home: Path, files: int) -> None:
    """Add file rows to a home's database until it holds `files`, as downloads would have.

    Run 1 saved posts `p0` to `p1999`, two files each; the rows added go on from `p2000`, with
    the keys, target and paths a download would give them.

    Args:
        home: the home of run 1.
        files: how many file rows the home holds after.
    """
    folder = _home.target_folder(home, Target("bnc:example_user", kind="user")) / "posts"
    with _store.open(home) as db:
        held = db.count("bnc:example_user")[0]
        rows = [
            _store.FileRow(f"bnc:p{n}_{num}", "bnc:example_user",
                          str(folder / f"2026-01-01_p{n}_{num}{('.jpg', '.mp4')[num]}"),
                          len(BODY) + 4, post_id=f"p{n}", category="posts", date="2026-01-01",
                          kind=("image", "video")[num], files=2 if num == 0 else None)
            for n in range(held // 2, files // 2) for num in (0, 1)
        ]
        # A transaction per 1,000 rows: the same rows a long run commits post by post.
        for start in range(0, len(rows), 1000):
            db.save(rows[start:start + 1000])
        # A sync over fewer rows would time the wrong case, so say so instead of reporting it.
        held = db.count("bnc:example_user")[0]
        if held != files:
            raise SystemExit(f"e2e: the grown home holds {held} files, not {files}")


def many_targets(home: Path) -> float:
    """Follow `FOLLOWED` targets, then time a sync of them with nothing new, in seconds."""
    engine = Engine(home=home, sources={"bnc": Bench})
    texts = [f"bnc:example_user_{n}" for n in range(FOLLOWED)]
    engine.download([f"{text}/posts" for text in texts], sync=True)
    files = sum(1 for path in (home / "media").rglob("*") if path.is_file())
    if files != 2 * FOLLOWED * FOLLOWED_POSTS:
        raise SystemExit(f"e2e: expected {2 * FOLLOWED * FOLLOWED_POSTS} files, found {files}")
    Fake.page_latency = 0.150
    start = time.perf_counter()
    engine.sync()
    return time.perf_counter() - start


def contended(home: Path) -> float:
    """Run `HELD_POSTS` posts while another process holds `flnd.db`'s write lock for `HOLD`.

    Returns:
        The longest the run's loop was held up, in seconds.
    """
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(home / _store.DB), str(HOLD)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    lags: list[float] = []
    # The probe's task, kept so the loop can't drop it half way.
    probes: list[asyncio.Future] = []

    async def probe() -> None:
        loop = asyncio.get_running_loop()
        try:
            while holder.poll() is None:
                at = loop.time()
                await asyncio.sleep(PROBE)
                lags.append(loop.time() - at - PROBE)
        except asyncio.CancelledError:
            pass  # the run ended and its loop closed: the probe has what it needs

    def on_file() -> None:
        # Called on the run's loop: the first file starts the probe, the HELD_AFTER-th the hold.
        if Fake.answered == 1:
            probes.append(asyncio.ensure_future(probe()))
        if Fake.answered == HELD_AFTER:
            # Its answer is read after the run: waiting for it here would stall the loop.
            assert holder.stdin is not None
            holder.stdin.write("go\n")
            holder.stdin.flush()

    Fake.answered, Fake.on_file = 0, on_file
    try:
        run(home)
    finally:
        Fake.on_file = None
        said, _ = holder.communicate()
    if said.strip() != "held":
        raise SystemExit("e2e: the other process didn't take flnd.db's write lock")
    files = sum(1 for path in (home / "media").rglob("*") if path.is_file())
    if files != 2 * HELD_POSTS:
        raise SystemExit(f"e2e: expected {2 * HELD_POSTS} files, found {files}")
    return max(lags)


def main() -> None:
    """Run the six scenarios and emit the report."""
    report = kit.Report("e2e")
    default_pace = Bench.pace
    with _net.fake(answer), ui.scope(dest=io.StringIO(), color=False), \
            tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        # 1 and 2: the engine alone.
        Fake.posts, Bench.pace = POSTS, 0.0
        home = Path(scratch, "fast", ".flnd")
        took = run(home)
        files = sum(1 for path in (home / "media").rglob("*") if path.is_file())
        if files != 2 * Fake.posts:
            raise SystemExit(f"e2e: expected {2 * Fake.posts} files, found {files}")
        report.add("per_file", took / files * 1e3, "ms")
        report.add("sync_nothing_new", run(home, again=True), "s")
        # 3: the same sync over a home of 100k files.
        grow(home, LARGE)
        report.add("sync_nothing_new_100k", run(home, again=True), "s")
        # 4: latency, at the defaults.
        Fake.posts, Bench.pace = SLOW_POSTS, default_pace
        Fake.page_latency, Fake.file_latency = 0.150, 0.040
        report.add("latency_500_posts", run(Path(scratch, "slow", ".flnd")), "s")
        # 5: many small followed targets, nothing new.
        Fake.posts, Bench.pace = FOLLOWED_POSTS, 0.0
        Fake.page_latency, Fake.file_latency = 0.0, 0.0
        report.add("sync_100_targets", many_targets(Path(scratch, "many", ".flnd")), "s")
        # 6: a held database.
        Fake.posts, Fake.page_latency, Fake.file_latency = HELD_POSTS, 0.0, 0.040
        report.add("db_contended", contended(Path(scratch, "held", ".flnd")) * 1e3, "ms")
    report.emit()


if __name__ == "__main__":
    main()
