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

`warm_4` times `Tabs.warm` starting 4 warm tabs beside the target's own (the most PLAN.md
section 6.2 allows), each on a page the server answers 50 ms late, until each had its first
answer; today they open one after the other. Best of 3, each with fresh tabs.

The browser runs on a loop thread of its own (`flnd.kit.loop.Loop`), and every step is handed to it
from this thread with `Loop.run`, as a source's listing thread does through `Site`'s verbs, so
each time includes that hop.

The group is skipped when Chromium can't be launched (run `playwright install chromium`).
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import kit

from flnd.kit import browser as _browser
from flnd.kit import cookies as _cookies
from flnd.kit import html as _html
from flnd.kit import net as _net
from flnd.kit.loop import Loop

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
        tabs = _browser.Tabs(shared, _cookies.Jar([]))
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
            shared = _browser.RunBrowser(loop, headless=True)
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
    report.emit()


if __name__ == "__main__":
    main()
