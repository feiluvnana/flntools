# flnd on the Phase 3 helpers

Phase 3. Needs [path](path.md), [http](http.md), [browser](browser.md), [scrape](scrape.md), [worker](worker.md), and [store](store.md). Do this after every Phase 2 file.

The four sources move onto the helpers. Their scraping logic does not change: which URLs, which JSON fields, and which tabs stay the same. Only the setup around that logic is replaced.

## What moves

| Today | After |
| --- | --- |
| `sync_playwright()` + `launch` + `new_context` + cookie fix-up loop + webdriver script, in all four sources | `with browser.open(headless=headless, user_agent=..., cookies=cookies) as page:` |
| `def on_response(response): … try: response.json() … except Exception: pass` + `page.on("response", …)` | `browser.capture(page, "graphql", handler)`; parse errors now show at `debug`, and handler bugs at `warn` |
| `scroll_page_to_end(page, …)` in `utils.py` | `browser.scroll(page, …, onstep=…)` inside flnd's `scroll`, which keeps flnd's 2000 px wheel step and the extra 500 px after each wait |
| TikTok's captcha loop (detect, print, poll, print) | `browser.handoff(page, done=no_captcha, message="Solve the TikTok captcha in the browser window")` |
| `httpx.Client(headers=…, timeout=…)` in 8 places | `http.Client(user_agent=…, headers=…, cookies=…)` |
| `resp = client.get(…)`; status check; `resp.json()` | `client.json(…)` |
| `utils.download_file` (60 lines) | `client.download(url, dest)` inside flnd's `save(client, url, dest) -> bool`, which catches `httpx.HTTPStatusError`, logs it, and returns `False` the way callers expect. A network error is raised, so the pool can retry it |
| `data.get("data", {}).get("user", {}).get(…)` chains, 31 of them, many with `or` fallbacks | `scrape.dig(data, "data.user.…", "data.xdt_api__….")` |
| `re.search(r'id="__NEXT_DATA__".*?>(.*?)</script>', …)` + `json.loads` in TikTok | `scrape.script(response, id="__NEXT_DATA__")` |
| `sanitize_name` | `path.safename` |
| `dir.mkdir(parents=True, exist_ok=True)` | `path.mkdir(dir)` |
| the desktop Chrome UA string, written 6 times | `http.agents.chrome` |

After this, [`flnd/utils.py`](../../flnd/utils.py) keeps only what is specific to flnd. It probably goes away entirely. Each source should lose about 30 to 40 lines of setup, roughly 150 lines in total.

## Parallel downloads

Today flnd does everything in sequence:

1. targets, one after another;
2. inside a source, it lists every item first (scrolling, API pages) and only then downloads the items one by one.

[worker](worker.md) changes both levels.

**Targets run in parallel, one source at a time.** Targets are grouped by source, and each group runs in its own worker. Its targets run one after another inside that worker. A run like `ins:a ins:b twt:c tik:d` uses three workers: `[ins:a, ins:b]`, `[twt:c]`, and `[tik:d]`. Two targets on the same site never run together. This avoids the bans and shared-session problems that parallel requests to one site with one cookie set would cause.

```python
groups = group_by_source(parsed)  # {"ins": [...], "twt": [...], ...}
results = worker.map(run_group, groups.values(), workers=len(groups), errors="log")
total = sum(results)
```

Each target still has its row in the `download` multi block. Rows now move at the same time.

**Downloads start while listing is still running.** Each source opens one pool for its files. The listing code submits a download as soon as it finds an item: from a `capture` handler, from an API page loop, or from a scroll step.

```python
with http.Client(...) as client, worker.pool(workers=6, retries=2, errors="log", progress=str(target)) as pool:
    def found(item):
        if limit and pool.submitted >= limit:
            return
        pool.submit(save, client, item.url, item.dest)
    browser.capture(page, "graphql", lambda response, data: [found(i) for i in parse(data)])
    browser.scroll(page, count=lambda: pool.submitted, limit=limit)
return sum(1 for result in pool.results if result.value is True)
```

flnd keeps this in a small `Downloads` class in [`flnd/utils.py`](../../flnd/utils.py), so each source only calls `files.add(client, url, dest, key=media_id)`. It also skips a `dest` that is already queued, so two threads never write the same file.

- `pool.submitted` is one of the pool's [live counters](worker.md#results). The count of new files reads `pool.results`, because `pool.succeeded` also counts files that were skipped or refused, which `save` reports as `False`.
- `limit` counts submitted items, so the limit stops listing as well as downloading.
- `retries=2` replaces today's single-attempt download. It retries network errors, which `save` raises; a status error is final and only logged. The same `http.Client` is shared by the workers, because httpx clients are thread-safe.
- The browser stays in the source's own thread. Only `save` runs in pool threads, and it never touches the page.

**Numbers.** The defaults are 6 downloads per source and one worker per source group. So a run over all four sources has at most 4 browsers and 24 downloads at once. These are constants at the top of [`flnd/cli.py`](../../flnd/cli.py), not options, because `download` takes no options. The expected gain:
- a target whose listing takes as long as its downloads should take about half the time, because they now overlap;
- a run over several sources should take about as long as its slowest source.

**What the screen shows.** One multi row per target, a pool `progress` bar under each active target, and `ok` / `error` lines for each file above them. The log [Threads](../phase-1/log.md#threads) rules keep this readable. If a target needs cookies, its cookie prompt is asked while the other targets keep working. Their output is held until the question is answered.

## Download history

Each saved file is recorded in `.flnd/state.db` (next to the downloads folder), table `downloads`. The key is `<source>:<media id>` and the value is `{"path", "size", "at"}`. The skip rule stays the same as today: a file is skipped when it exists on disk. The history makes these possible later:

- not downloading a file again after the user deleted it on purpose (like yt-dlp's `--download-archive`);
- a `flnd history` command.

Neither of those is part of this phase.

## Open questions

- **Should cookies move into `store.secrets`?** Today they are plain `.flnd/cookies/<source>.txt` files, which [set-cookie](../phase-2/commands.md#set-cookie) just settled on. Moving them would encrypt them at rest, but they could no longer be read or edited by hand. The suggestion is to keep the `.txt` files until a script needs encrypted cookies.
- **`archive` has no flnd caller yet.** One candidate is a `--zip` flag on `download` that packs each target's folder at the end.

## Tests

- The existing source tests in [`tests/test_sources.py`](../../tests/test_sources.py) keep passing.
- `save`: a skip, a success, and an HTTP error that returns `False` and logs `error`.
- The download history: a row is written for each saved file.
- One end-to-end test per source with `httpx.MockTransport`, for its API path (the path that needs no browser). Facebook has no API path, so it has none.
- Targets grouped by source: two targets of the same source never run at once, while different sources do.
