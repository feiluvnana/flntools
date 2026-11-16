"""Benchmark group `browser`: headless Chromium on a synthetic infinite-scroll feed.

The feed is a local page served by `server.py`. Each time the reader nears the bottom it
fetches the next batch of 10 items from `/slow/items<n>.json`, which the server answers after
50 ms; after 30 batches the feed says there is no more. So the data itself takes about 1.5 s to
arrive, and the rest of a scroll's time is the scroller's own waiting.

What it times: launching the browser, opening a tab, one `evaluate` round trip, reading the
page as HTML, an in-page `fetch` against flnd's HTTP client, scrolling to the end with today's
default (`idle=10`, how long the scroller waits for silence before deciding the feed ended)
and with `idle=2`, and closing.

The group is skipped when Chromium can't be launched (run `playwright install chromium`).
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import kit
from shared import browser, http

BATCHES = 30
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


def main() -> None:
    """Serve the feed, drive it in headless Chromium, and emit the report."""
    report = kit.Report("browser")
    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        folder = Path(scratch)
        for n in range(BATCHES):
            batch = {"items": [f"item {n}-{k}" for k in range(10)], "more": n < BATCHES - 1}
            (folder / f"items{n}.json").write_text(json.dumps(batch))
        (folder / "feed.html").write_text(FEED)
        with kit.serve(folder) as base:
            try:
                chrome, took = timed(lambda: browser.open(headless=True))
            except Exception as error:  # Playwright's own errors, or shared.MissingError
                report.skip(f"Chromium did not launch ({type(error).__name__}); "
                            "run `playwright install chromium`")
                report.emit()
                return
            report.add("launch", took * 1e3, "ms")
            page, took = timed(lambda: chrome.open(f"{base}/feed.html"))
            report.add("open_tab", took * 1e3, "ms")
            _, took = timed(lambda: [page.raw.evaluate("1") for _ in range(200)])
            report.add("evaluate", took / 200 * 1e3, "ms")
            _, took = timed(lambda: [page.document for _ in range(20)])
            report.add("document", took / 20 * 1e3, "ms")
            _, took = timed(lambda: [page.fetch(f"{base}/items1.json") for _ in range(50)])
            report.add("page_fetch", took / 50 * 1e3, "ms")
            _, took = timed(lambda: [http.fetch(f"{base}/items1.json") for _ in range(50)])
            report.add("http_fetch", took / 50 * 1e3, "ms")
            # `idle` left out is today's default of 10 s.
            for name, tab, options in (("scroll_default", page, {}),
                                       ("scroll_idle_2", chrome.open(f"{base}/feed.html"),
                                        {"idle": 2})):
                start = time.perf_counter()
                tab.scroll(**options)
                took = time.perf_counter() - start
                # A scroll that stopped early would look fast; only a whole feed counts.
                rows = tab.raw.evaluate("list.children.length")
                if rows != BATCHES * 10:
                    raise SystemExit(f"browser: {name} stopped at {rows} of {BATCHES * 10} rows")
                report.add(name, took, "s")
            _, took = timed(chrome.close)
            report.add("close", took * 1e3, "ms")
    report.emit()


if __name__ == "__main__":
    main()
