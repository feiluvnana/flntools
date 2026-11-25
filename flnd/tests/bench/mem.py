"""Benchmark group `mem`: memory footprint of idle python, browser, downloads and soak.

SPEC.md#for-developers explains flnd's offline benchmark suite and gate metrics.
"""

from __future__ import annotations

import asyncio
import datetime
import gc
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import kit
import psutil
from kit import Loop, serve

from flnd.engine import crawl, jobs, save
from flnd.plugin import File, Post, Target
from flnkit import browser as _browser
from flnkit import cookies as _cookies
from flnkit import net as _net

BROWSER_NAMES = ("chrom", "headless", "firefox", "webkit", "msedge")


def proc_private(proc: psutil.Process) -> int:
    """Private or unique working memory of `proc` in bytes."""
    try:
        if sys.platform == "win32":
            return getattr(proc.memory_info(), "private", 0)
        return getattr(proc.memory_full_info(), "uss", proc.memory_info().rss)
    except (psutil.Error, AttributeError):
        return 0


def idle_private() -> float:
    """Private memory of Python right after importing `flnd.engine._engine`, in MiB."""
    code = (
        "import psutil, sys, flnd.engine._engine\n"
        "p = psutil.Process()\n"
        "mem = getattr(p.memory_info(), 'private', None) if sys.platform == 'win32' "
        "else getattr(p.memory_full_info(), 'uss', p.memory_info().rss)\n"
        "print(mem)\n"
    )
    runs = 1 if kit.SMOKE else 3
    lowest = float("inf")
    for _ in range(runs):
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              check=True)
        lowest = min(lowest, int(done.stdout.strip()) / (1024 * 1024))
    return lowest


def chrome_tree_private() -> int:
    """Summed private memory across all browser processes under this process."""
    root = psutil.Process()
    total = 0
    try:
        children = root.children(recursive=True)
    except psutil.Error:
        children = []
    for proc in children:
        try:
            if any(b in proc.name().lower() for b in BROWSER_NAMES):
                total += proc_private(proc)
        except psutil.Error:
            continue
    return total


def browser_1tab_private(loop: Loop) -> float:
    """Chrome tree private RAM with one tab open, in MiB."""
    shared = _browser.RunBrowser(loop.loop, headless=True)
    try:
        tabs = _browser.Tabs(shared, _cookies.Jar([]))
        try:
            _ = tabs.page
            time.sleep(0.3)
            return chrome_tree_private() / (1024 * 1024)
        finally:
            tabs.close()
    finally:
        shared.close()


class _Kept:
    """A store that keeps nothing for download benchmarks."""

    path = Path("flnd.db")
    due = False

    def twins(self, **asked: Any) -> list[Any]:
        return []

    def commit(self, *args: Any, **options: Any) -> None:
        pass

    def update(self, *args: Any, **fields: Any) -> None:
        pass

    def checkpoint(self, *, force: bool = False) -> None:
        pass


class Capped:
    """Transfers held to a fixed limit per host."""

    def __init__(self, session: _net.Session, at_once: int) -> None:
        self.client = _net.Client(session)
        self.scheduler = jobs.Scheduler(limits={"127.0.0.1": at_once, "localhost": at_once})
        self.saver = save.Saver(_Kept(), self.scheduler,
                                lambda event: None,  # type: ignore[arg-type]
                                dedupe=False)
        self.target = Target("bnc:example_user", kind="user")


async def fill(client: Capped, url: str, dest: Path) -> None:
    """Download `url` into `dest` through the saver."""
    item = save.Item(f"bnc:{dest.name}", client.target, dest.name, "", File(url), dest)
    await client.saver.post([item], client=client.client, plugin="bnc")


async def many(client: Capped, urls: list[str], out: Path) -> None:
    """Download every URL concurrently."""
    await asyncio.gather(*(fill(client, url, out / str(n)) for n, url in enumerate(urls)))


def download_peak_private(loop: Loop) -> float:
    """Peak Python private memory during concurrent downloads, in MiB."""
    count = kit.size(32, 2)
    file_size = kit.size(16 << 20, 1 << 20)
    with tempfile.TemporaryDirectory(prefix="bench-mem-src-") as src_dir, \
            tempfile.TemporaryDirectory(prefix="bench-mem-dst-") as dst_dir:
        src_path = Path(src_dir)
        dst_path = Path(dst_dir)
        (src_path / "chunk.bin").write_bytes(os.urandom(file_size))
        with serve(src_path) as base:
            session = _net.Session()
            capped = Capped(session, at_once=count)
            urls = [f"{base}/chunk.bin?{i}" for i in range(count)]
            peak = [proc_private(psutil.Process())]
            stop = threading.Event()

            def sample() -> None:
                proc = psutil.Process()
                while not stop.is_set():
                    m = proc_private(proc)
                    if m > peak[0]:
                        peak[0] = m
                    stop.wait(0.002)

            thread = threading.Thread(target=sample, daemon=True)
            thread.start()
            try:
                loop.run(many(capped, urls, dst_path))
            finally:
                stop.set()
                thread.join()
                peak[0] = max(peak[0], proc_private(psutil.Process()))
                loop.run(session.close())
            return peak[0] / (1024 * 1024)


def soak_10k_delta() -> float:
    """Private memory growth across simulated posts/files in engine collections, in MiB."""
    n = kit.size(10_000, 100)
    gc.collect()
    proc = psutil.Process()
    before = proc_private(proc)

    claims = crawl.Claims()
    dates: dict[str, datetime.date] = {}
    order: list[tuple[str, datetime.date]] = []
    written: dict[str, Path] = {}
    posts: list[Post] = []

    day = datetime.date(2026, 1, 1)
    for i in range(n):
        dest = Path(f"media/user/{i}.jpg")
        key = f"post_{i}"
        claims.claim(dest, key, f"{i}.jpg")
        dates[key] = day
        order.append((key, day))
        written[key] = dest
        posts.append(Post(key, [File(f"https://example.com/{i}.jpg")], date=1700000000 + i))

    after = proc_private(proc)
    del claims, dates, order, written, posts
    gc.collect()
    return max(0.0, (after - before) / (1024 * 1024))


def main() -> None:
    """Measure memory metrics and emit the report."""
    report = kit.Report("mem")
    report.add("idle_private", idle_private(), "MiB")
    with Loop() as loop:
        try:
            report.add("browser_1tab_private", browser_1tab_private(loop), "MiB")
        except Exception as error:
            report.skip(f"Chromium did not launch ({type(error).__name__})")
        report.add("download_peak_private", download_peak_private(loop), "MiB")
    report.add("soak_10k_delta", soak_10k_delta(), "MiB")
    report.emit()


if __name__ == "__main__":
    main()
