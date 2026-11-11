# session

Phase 6, step 3. The `Session` protocol has 35 members. Nine have no caller in any source, three only forward to `session.page`, and the scroll defaults are wrong in both directions: `max=200` cuts long feeds short, and the `delay=0.2` tried in the WIP commit scrolls like a bot and declares the end after about 2 s. This step cuts the protocol to 26 members, makes the defaults the ones sources actually want, and moves what isn't the session's job out of `session.py` (874 lines, 603 of code).

## Members

| Group | Phase 6 | Was |
| --- | --- | --- |
| target | `target`, `folder`, `cookies`, `categories`, `since`, `until`, `wants(category=, date=)` | the same |
| requests | `fetch_json(url, *, method="GET", headers=None, soft=False, **kw)`, `fetch_html(url, **kw)` | also `http`, `select` |
| browser | `goto(url, *, idle=False, timeout=30.0)`, `on_response(match, handler, *, lines=False)`, `scroll(*, count=None, more=None, delay=None)`, `handoff(*, until, message)`, `page` | also `open_page`, `on_request`, `wait_for_selector`, `wait_for_url`, `wait_for_load`, `browser`; `scroll` had `by`, `idle`, `max` |
| results | `save(…)`, `downloaded(key)`, `write(name, text)`, `full` | `downloaded` is new |
| SDK | `client` | the same |
| state | `state`, `secrets` | the same |
| people | `interactive`, `ask(message, *, secret=False, description="")`, `info`, `warn`, `throttled(reason, *, retry_after=None)` | also `choose`; `challenged()` becomes `throttled` |

**Removed**, each with no caller in `flnd/sources` (counted 2026-11-12):
- `http`, `select`: a source never needs the raw client, and `selector.select` already covers `select`.
- `open_page`: `goto` opens the browser and a tab on first use, so there's one way to load a page.
- `on_request`: no source watches requests.
- `wait_for_selector`, `wait_for_url`, `wait_for_load`, `browser`: `session.page` is the `browser.Page`, which has them. A source that needs one calls `session.page.wait_for_…` (Rule 2: rarer things go through the wrapper, not a copy of it).
- `choose`: no caller. `ask` stays for tlg's phone and code.

**Changed:**
- **`throttled(reason, *, retry_after=None)`** replaces `challenged()`. It counts one rate limit, waits it out on the source's [backoff](../phase-4/engine.md#downloads) with the countdown, and returns, so the source retries what it was doing. tik calls it for a captcha nobody solved, ins for Instagram's "Please wait a few minutes" ([ins](ins.md)). That is one name for "the site is slowing us down", whether the site said so with a 429, a captcha or a JSON body.
- **`downloaded(key) -> bool`** (from the WIP commit) says whether a file with this `save` `key` was saved by this run or an earlier one, so a source can skip the request that would find it: `if session.downloaded(f"{code}_0"): continue`.
- **`goto` checks the response.** Today `Page.goto` drops Playwright's response, so a 429 page or a login wall counts as loaded. `shared` `Page.goto` now returns the main response's status (`int | None`) instead of a bool; `session.goto`:
  - on 429, calls `throttled("HTTP 429")` with the `Retry-After` and loads the page again, as long as it takes;
  - on another status of 400 or more, warns and returns False;
  - returns True otherwise, after the source's [pace](#pace).

## Defaults

### Scroll

`scroll(*, count=None, more=None, delay=None)`:

| | Was | Phase 6 |
| --- | --- | --- |
| pause between scrolls | `delay=2.0` (0.2 in the WIP commit) | the source's `pace`, 1.0 s by default, ±40%; tik 2.5, ins 3.0 ([source](source.md)) |
| end of the feed | 4 scrolls in a row at the bottom with no progress (8 s at 2.0, 2 s at 0.2) | **10 s** at the bottom with no progress, however many scrolls that is |
| most scrolls | 200, then it stops silently | none: the feed's end, `--limit` or `more()` stop it |
| `count` | required to see progress | defaults to the number of files saved into this folder so far |

- **The end is a time, not a count**, so a faster or slower `delay` doesn't change when a slow feed is given up on. "No progress" is as now: the page height, `count()` and the `on_response` traffic all stayed the same.
- **No `max`.** 200 scrolls is about 2,400 posts on Instagram, so large accounts were cut off without a word. A feed that never ends is caught by the end rule: it keeps growing only while there's something new.
- **`count` defaults to the saves.** tik's `counts`, twt's `found = [0]` and fbk's `feed.found` exist only to feed `count=`, so they go. ins keeps its own count, because it lists posts that are saved later.
- `by=`, `idle=` and `max=` go from the session and from `shared` `Page.scroll`; no source passed them.

### Pace

**`Source.pace`**, 1.0 s by default and ±40%, is the least time between two page loads (`goto`) or two `fetch_json` calls of one source, and the pause between scrolls ([source](source.md)). Today nothing paces page loads at all, so ins opened one post page after another as fast as they loaded ([ins](ins.md)). ins sets 3.0.

### Exit status

`flnd download` and `flnd sync` (today's `sync`) exit with **1 when any target failed**, and 0 when every one ended ok ([fixes](fixes.md#exit-status)). Today they always exit 0.

## What moves out of `session.py`

`session.py` keeps the protocol, `Options`, and `TargetSession`. Moved, unchanged in behaviour:
- **`flnd/compress.py`**: `PRESETS`, `IMAGE_FORMATS`, `VIDEO_*`, `CODECS_IN`, `video_format`, `compress_problem`, `compress`, and the video encode pool. `sync.py` already imports them from session, which isn't where they belong.
- **`flnd/files.py`**: `KINDS` (photo, video and now audio), `media_kind`, and `write`'s atomic file writing, which `vault.py` and `net.py` each do again in their own way.
- **`SourceRun`** goes to `engine.py`, which is the only thing that builds it.
- `_end_handover`'s `dropping` loop and `_hand_over` become one small `_Handover` class with `put` and `close(drop=)`.

`_unpack`, `_zip`, the archive suffixes and `password=` go with `--unpack` and `--zip` ([cli](cli.md#--unpack-and---zip-go)). About 250 code lines leave `session.py`, and about 80 go entirely (the removed members, their docstrings' code, and the per-source counters).

## Tests

- `tests/fakes.py` `FakeSession` implements the new protocol and nothing else; a test checks that its public names equal `Session`'s.
- `tests/typing/usage.py`: `session.open_page` and `session.challenged` don't type-check.
- Scroll: a page that stops growing ends after 10 s on the fake clock, whatever `delay` is; one that grows for 300 scrolls isn't cut off; `count` defaults to the saves.
- `goto`: a 429 with `Retry-After: 30` waits 30 s and loads again; a 404 returns False with a warning.
- Two `goto`s within the source's `pace` sleep the difference, with the jitter seeded.
- `throttled` waits on the backoff, and a second call during the wait doesn't add another.
