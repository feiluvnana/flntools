# session

Phase 6, step 3. The `Session` protocol has 35 members. Nine have no caller in any source, three only forward to `session.page`, and the scroll defaults are wrong in both directions: `max=200` cuts long feeds short, and the `delay=0.2` tried in the WIP commit scrolls like a bot and declares the end after about 2 s. This step cuts the protocol to 26 members, makes the defaults the ones sources actually want, and moves what isn't the session's job out of `session.py` (874 lines, 603 of code).

## Members

| Group | Phase 6 | Was |
| --- | --- | --- |
| target | `target`, `folder`, `cookies`, `categories`, `since`, `until`, `wants(category=, date=)` | the same |
| requests | `fetch_json(url, *, method="GET", headers=None, soft=False, in_page=False, **kw)`, `fetch_html(url, **kw)` | also `http`, `select`; `in_page=` is new ([ins](ins.md#three-ways-to-reach-an-api)) |
| browser | `goto(url, *, idle=False, timeout=30.0)`, `on_response(match, handler, *, lines=False)`, `scroll(*, count=None, more=None, delay=None)`, `handoff(*, until, message)`, `page` | also `open_page`, `on_request`, `wait_for_selector`, `wait_for_url`, `wait_for_load`, `browser`; `scroll` had `by`, `idle`, `max` |
| results | `save(…)`, `saved(key)`, `write(name, text)`, `full` | `saved` is new |
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
- **`saved(key) -> bool`** (the WIP commit's `downloaded`, renamed to pair with `save`) says whether a file with this `save` `key` was saved by this run or an earlier one, so a source can skip the request that would find it: `if session.saved(f"{code}_0"): continue`.
- **`goto` checks the response.** Today `Page.goto` drops Playwright's response, so a 429 page or a login wall counts as loaded. `shared` `Page.goto` now returns the main response's status (`int | None`) instead of a bool; `session.goto`:
  - on 429, calls `throttled("HTTP 429")` with the `Retry-After` and loads the page again, as long as it takes;
  - on another status of 400 or more, warns and returns False;
  - returns True otherwise, after the source's [pace](#pace).

## Defaults

### Scroll

`scroll(*, count=None, more=None, delay=None)`:

| | Was | Phase 6 |
| --- | --- | --- |
| pause between scrolls | a fixed `delay=2.0` (0.2 in the WIP commit) | **as short as the page allows**: the next scroll comes as soon as the page shows something new, after at least 0.3 s ([below](#scrolling-on-the-pages-own-time)) |
| end of the feed | 4 scrolls in a row at the bottom with no progress (8 s at 2.0, 2 s at 0.2) | **10 s** at the bottom with no progress, however many scrolls that is |
| most scrolls | 200, then it stops silently | none: the feed's end, `--limit` or `more()` stop it |
| `count` | required to see progress | defaults to the number of files saved into this folder so far |

- **The end is a time, not a count**, so a faster or slower `delay` doesn't change when a slow feed is given up on. "No progress" is as now: the page height, `count()` and the `on_response` traffic all stayed the same.
- **No `max`.** 200 scrolls is about 2,400 posts on Instagram, so large accounts were cut off without a word. A feed that never ends is caught by the end rule: it keeps growing only while there's something new.
- **`count` defaults to the saves.** tik's `counts`, twt's `found = [0]` and fbk's `feed.found` exist only to feed `count=`, so they go. ins keeps its own count, because it lists posts that are saved later.
- `by=`, `idle=` and `max=` go from the session; `shared` `Page.scroll` keeps `by` and `limit` ([api](api.md#browser-529-lines)).

#### Scrolling on the page's own time

A fixed pause is always wrong. It is too long when the page has already loaded the next posts, and too short when the site is slow. So a scroll waits for **progress**, not for a clock:
1. Scroll (the wheel, in uneven steps, as now).
2. Wait until one of these happens, checked every 50 ms:
   - the page grew;
   - `count()` rose;
   - a matching `on_response` arrived;
   - **or** `delay` passed (3 s by default).
3. Pause the **floor**, 0.3 s ±40%, so two scrolls never come back to back, then scroll again.

So on a fast connection a feed scrolls about 2–3 times a second, as fast as its pages arrive, and on a slow one it waits each page out without giving up early. While nothing loads (at the bottom), the 10 s end rule above still decides when the feed has ended.

- **The floor is learned**, like a request [pace](pace.md#design). A captcha or a throttle during a scroll doubles it, up to 5 s. 50 scrolls in a row without one shrink it by 20%, never below 0.3 s. It's saved in the source's `state`, so tik, whose captcha is the usual signal, finds its own speed and keeps it. tik's fixed `delay=2.5` goes.
- **The page's own requests aren't flnd's to space out**, and scrolling is the fallback once the [API-first](ins.md#every-source-api-first-the-page-as-fallback) path is in, so scrolling fast costs little.
- `Page.scroll` in `shared` does this wait. Its `delay` becomes the longest wait for progress, and `pause=0.3` is the floor between scrolls. The session passes the learned floor.

### Pace

Every request to a source's own domains (`fetch_json`, in-page calls and `goto`) waits for its turn under the source's [pace](pace.md): the site's rate-limit headers, its known budgets, and a gap learned per source with a floor. Today nothing paces page loads at all, so ins opened one post page after another as fast as they loaded. CDN downloads aren't paced; `concurrency` limits them.

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
- A page that grows 0.1 s after each scroll is scrolled again after 0.3 s, not after `delay`. One that grows only after 2 s is waited for. A captcha doubles the floor, and 50 clean scrolls shrink it back toward 0.3 s.
- `goto`: a 429 with `Retry-After: 30` waits 30 s and loads again; a 404 returns False with a warning.
- `goto` and `fetch_json` call the pace's `wait_turn` before and `record` after; a CDN URL calls neither ([pace](pace.md#tests)).
- `throttled` waits on the backoff, and a second call during the wait doesn't add another.
