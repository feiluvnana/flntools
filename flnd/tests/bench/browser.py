"""Benchmark group `browser`: headless Chromium on a synthetic infinite-scroll feed.

The feed is a local page served by `server.py`. Each time the reader nears the bottom it
fetches the next batch of 10 items from `/slow/items<n>.json`, which the server answers after
50 ms; each batch names the next one's cursor, and the 30th says there is no more. So the data
itself takes about 1.5 s to arrive, and the rest of a scroll's time is the scroller's own.

What it times: launching the browser, opening a tab, one `evaluate` round trip, reading the
page as HTML and parsing it, an in-page `fetch` against flnd's HTTP client, reading the whole
feed as a source does, and closing.

The feed is read the way `site.feed(match, scroll=True, cursor=…)` reads one: a `Feed` over a
target's `Tabs` in a `RunBrowser`, with the cursor reader below, once with `scroll = "human"`
(the default, `scroll_default`) and once with `"fast"` (`scroll_fast`). A whole read ends on
the 30th batch's word, with no idle wait.

`warm_4` times `Tabs.warm` starting 4 warm tabs beside the target's own (the most the engine
allows), each on a page the server answers 50 ms late, until each had its first
answer; today they open one after the other. Best of 3, each with fresh tabs.

The browser runs on a loop thread of its own (`kit.Loop`), and every step is handed to it
from this thread with `Loop.run`, as a plain source's thread does through `Site`'s verbs, so
each time includes that hop.

`heavy_*` and `lean_*` measure what the browser costs (SPEC.md#for-developers): a feed of
the same shape whose rows each hold an image (a distinct URL, so none is shared), plus a web
font, read once with the default scroll in a browser of its own, with the images, media and
fonts loaded (`heavy`) and with them blocked (`lean`). `heavy_rss` is the peak of the
summed resident memory of Chrome's processes, sampled every 0.1 s (macOS counts a page that
processes share once for each, so read it only against itself); `heavy_cpu` is their CPU
seconds, the most each process showed. Both are read with `ps` (not on Windows, where the two
are left out). They give the baseline that the choices of what a page loads and which switches
Chrome starts with are measured against.

The group is skipped when Chromium can't be launched (run `playwright install chromium`).
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

import kit
from kit import Loop

from flnd.engine import jobs
from flnkit import browser as _browser
from flnkit import cookies as _cookies
from flnkit import html as _html
from flnkit import net as _net

BATCHES = kit.size(30, 3)
WARM = 4
# The feed: each batch appends 10 tall rows, and nearing the bottom asks for the next batch.
FEED = """<html><body><div id=list></div><script>
let n = 0, busy = false;
async function more() {
  if (busy) return; busy = true;
  const r = await fetch('/slow/items' + n + '.json'); const j = await r.json();
  for (const t of j.items) {
    const d = document.createElement('div'); d.style.height = '120px'; d.textContent = t;
    list.appendChild(d);
  }
  n++; busy = !j.more;
}
addEventListener('scroll', () => {
  if (innerHeight + scrollY >= document.body.scrollHeight - 200) more();
});
more();
</script></body></html>"""
# The heavy feed: each row also holds an image, and the page uses a web font.
HEAVY = FEED.replace("d.textContent = t;", "d.textContent = t; const i = new Image(); "
                     "i.height = 120; i.src = '/pic.png?' + n + '-' + t; d.appendChild(i);")
HEAVY = HEAVY.replace("<html><body>", "<html><head><style>@font-face {font-family: x; "
                      "src: url(/f.woff2)} body {font-family: x}</style></head><body>")
PICTURE = (480, 320)


def png(width: int, height: int) -> bytes:
    """A `width` by `height` picture of a pattern that does not compress away."""
    rows = b"".join(b"\0" + bytes((x * 7 + y * 13 + x * y) % 256 for x in range(width * 3))
                    for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2,
                                                                0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _seconds(text: str) -> float:
    """`ps`'s CPU time, `[[dd-]hh:]mm:ss[.ff]`, in seconds."""
    days, _, clock = text.rpartition("-")
    total = 0.0
    for part in clock.split(":"):
        total = total * 60 + float(part)
    return total + (int(days) * 86400 if days else 0)


def chrome_processes() -> dict[int, tuple[int, float]]:
    """Chrome's processes under this one: pid -> (resident KiB, CPU seconds), read with `ps`."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss=,time=,command="],
                         capture_output=True, text=True, check=True).stdout
    rows: dict[int, tuple[int, int, float, str]] = {}
    for line in out.splitlines():
        pid, ppid, rss, cpu, command = line.split(None, 4)
        rows[int(pid)] = (int(ppid), int(rss), _seconds(cpu), command)
    found = {}
    for pid, (_, rss, cpu, command) in rows.items():
        up, hops = pid, 0
        while up in rows and up != os.getpid() and hops < 50:
            up, hops = rows[up][0], hops + 1
        if up == os.getpid() and "hrom" in command:
            found[pid] = (rss, cpu)
    return found


class Meter:
    """Samples `chrome_processes` every 0.1 s on a thread: the peak memory and the CPU seconds."""

    def __init__(self) -> None:
        self.peak_kib = 0
        self._cpu: dict[int, float] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while True:
            seen = chrome_processes()
            self.peak_kib = max(self.peak_kib, sum(rss for rss, _ in seen.values()))
            for pid, (_, cpu) in seen.items():
                self._cpu[pid] = max(self._cpu.get(pid, 0.0), cpu)
            if self._stop.wait(0.1):
                return

    @property
    def cpu(self) -> float:
        """CPU seconds, summed over every process seen."""
        return sum(self._cpu.values())

    def __enter__(self) -> Meter:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join()


def heavy(shared: _browser.RunBrowser, base: str, report: kit.Report, name: str, *,
          assets: bool) -> None:
    """Read the heavy feed once, with the default scroll, and report the browser's cost."""
    tabs = _browser.Tabs(shared, _cookies.Jar([]), scroll=_browser.HUMAN, assets=assets)
    try:
        _ = tabs.page
        with Meter() as meter:
            batches = read_feed(tabs, f"{base}/heavy.html")
            time.sleep(0.5)
        loaded = tabs.run(tabs.page.evaluate("document.images.length"))
    finally:
        tabs.close()
    if batches != BATCHES:
        raise SystemExit(f"browser: the heavy feed stopped at {batches} of {BATCHES} batches")
    print(f"  heavy page: {loaded} images", file=sys.stderr, flush=True)
    report.add(f"{name}_rss", meter.peak_kib / 1024, "MiB")
    report.add(f"{name}_cpu", meter.cpu, "s")


def timed(work):
    """Run `work` once and return its result and its wall time in seconds."""
    start = time.perf_counter()
    result = work()
    return result, time.perf_counter() - start


def cursor(batch):
    """The feed's cursor reader: whether there is another batch, and its cursor."""
    return batch["more"], batch.get("next")


def read_feed(tabs: _browser.Tabs, url: str) -> int:
    """Read the whole feed at `url` as a source does, and return how many batches came."""
    with _browser.Feed(tabs, "/slow/items", scroll=True, cursor=cursor) as batches:
        tabs.run(tabs.page.goto(url))
        return sum(1 for _ in batches)


def warm_4(shared: _browser.RunBrowser, base: str) -> float:
    """The best of 3 times `Tabs.warm` took to start `WARM` warm tabs, in seconds."""
    fastest = float("inf")
    for attempt in range(1 if kit.SMOKE else 3):
        scheduler = jobs.Scheduler()
        scheduler.tabs = jobs.Pool(WARM + jobs.WARM_FREE + 1)
        tabs = _browser.Tabs(shared, _cookies.Jar([]), budget=jobs.Budget(scheduler, "bench"))
        try:
            # The target's own tab is opened before the clock starts.
            _ = tabs.page
            urls = [f"{base}/slow/warm{n}.html?{attempt}" for n in range(WARM)]
            start = time.perf_counter()
            tabs.warm(urls)
            took = time.perf_counter() - start
            # A tab that failed to start would look fast; only all of them count.
            if len(tabs._warmed) != WARM:
                raise SystemExit(f"browser: warm_4 started {len(tabs._warmed)} of {WARM} tabs")
        finally:
            tabs.close()
        fastest = min(fastest, took)
    return fastest


def main() -> None:
    """Serve the feed, drive it in headless Chromium, and emit the report."""
    report = kit.Report("browser")
    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch, Loop() as loop:
        folder = Path(scratch)
        for n in range(BATCHES):
            batch = {"items": [f"item {n}-{k}" for k in range(10)], "more": n < BATCHES - 1,
                     "next": f"c{n + 1}" if n < BATCHES - 1 else None}
            (folder / f"items{n}.json").write_text(json.dumps(batch))
        (folder / "feed.html").write_text(FEED)
        (folder / "heavy.html").write_text(HEAVY)
        (folder / "pic.png").write_bytes(png(*PICTURE))
        (folder / "f.woff2").write_bytes(b"not a font")
        for n in range(WARM):
            (folder / f"warm{n}.html").write_text(f"<html><body>page {n}</body></html>")
        with kit.serve(folder) as base:
            try:
                chrome, took = timed(lambda: loop.run(_browser.launch(headless=True)))
            except Exception as error:  # Playwright's own errors, or flnd.ToolError (no browser)
                report.skip(f"Chromium did not launch ({type(error).__name__}); "
                            "run `playwright install chromium`")
                report.emit()
                return
            report.add("launch", took * 1e3, "ms")

            async def tab_at(url):
                page = await chrome.tab()
                await page.goto(url)
                return page

            page, took = timed(lambda: loop.run(tab_at(f"{base}/feed.html")))
            report.add("open_tab", took * 1e3, "ms")
            _, took = timed(lambda: [loop.run(page.evaluate("1")) for _ in range(200)])
            report.add("evaluate", took / 200 * 1e3, "ms")
            _, took = timed(lambda: [_html.parse(loop.run(page.content())) for _ in range(20)])
            report.add("document", took / 20 * 1e3, "ms")
            _, took = timed(lambda: [loop.run(page.fetch(f"{base}/items1.json"))
                                     for _ in range(50)])
            report.add("page_fetch", took / 50 * 1e3, "ms")
            client = _net.Client(_net.Session())
            _, took = timed(lambda: [loop.run(client.fetch(f"{base}/items1.json"))
                                     for _ in range(50)])
            loop.run(client.session.close())
            report.add("http_fetch", took / 50 * 1e3, "ms")
            _, took = timed(lambda: loop.run(chrome.close()))
            report.add("close", took * 1e3, "ms")

            # A target's tabs in a run's browser, as a source's feed gets them.
            shared = _browser.RunBrowser(loop.loop, headless=True)
            try:
                for name, style in (("scroll_default", _browser.HUMAN),
                                    ("scroll_fast", _browser.FAST)):
                    tabs = _browser.Tabs(shared, _cookies.Jar([]), scroll=style)
                    try:
                        # The tab is opened (and the browser launched) before the clock starts.
                        _ = tabs.page
                        start = time.perf_counter()
                        batches = read_feed(tabs, f"{base}/feed.html")
                        took = time.perf_counter() - start
                        # A read that stopped early would look fast; only a whole feed counts.
                        rows = tabs.run(tabs.page.evaluate("list.children.length"))
                    finally:
                        tabs.close()
                    if (batches, rows) != (BATCHES, BATCHES * 10):
                        raise SystemExit(f"browser: {name} stopped at {batches} batches, "
                                         f"{rows} of {BATCHES * 10} rows")
                    report.add(name, took, "s")
                report.add("warm_4", warm_4(shared, base), "s")
            finally:
                shared.close()
            if sys.platform != "win32":
                for name, assets in (("heavy", True), ("lean", False)):
                    alone = _browser.RunBrowser(loop.loop, headless=True)
                    try:
                        heavy(alone, base, report, name, assets=assets)
                    finally:
                        alone.close()
    report.emit()


if __name__ == "__main__":
    main()
