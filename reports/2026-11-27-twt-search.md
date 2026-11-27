# Twitter search stalls past an account's oldest tweet (2026-11-27)

A report on one flnd bug: what it did, why, how it was checked live, the fix, and what is still
open. Accounts are named A and B only.

## Summary

- **Bug:** after a profile's Posts tab, `twt` searches below the oldest tweet it saw (the search
  backfill). Past the account's real first tweet, X answers that search with one page that holds
  no tweets, then sends nothing. flnd took the silence for an interruption: it held 30s, 2m and
  8m, then paused the backfill and saved its cursor. Every later run resumed at that cursor and
  did the same 15 minutes again, and the target never showed complete.
- **No timeline cap was seen.** The logged-in Posts tab read account A to its first tweet
  (3,297 tweets) and account B past 9,000 tweets. The search backfill stays, for accounts where
  X does stop the tab early, but on these two it had nothing to add.
- **Fix:** `61a93c7`. A search window X answered with no tweets is the end of the search. A
  window X never answered, or answered with an error, is still held and tried again.
- **Live result:** account A's real run resumed the stuck cursor and ended the search in 31
  seconds, with no hold; every category finished complete and the cursor was cleared. Account
  B (12,627 files, 4.8 GB) also finished every category complete, with no stall.

## What happened

The first real run of account A listed its posts for about 18 minutes, then printed:

```text
! twt:A: the list got no new answer in 1m; it ends here
! twt:A: search interrupted; waiting 30.0s
! twt:A: the list got no new answer in 1m; it ends here
! twt:A: search interrupted; waiting 2m
! twt:A: the list got no new answer in 1m; it ends here
! twt:A: search interrupted; waiting 8m
! twt:A: posts: paused search backfill after repeated timeline errors; rerun later to resume
```

It saved the cursor `backfill:<the tweet id just below the first tweet>` and left `posts`
incomplete. The messages were wrong too: "it ends here" came just before a retry, there were no
timeline errors, and the same waits read "30.0s", "2m" and "held 350 s".

## Why

1. `posts` reads the Posts tab until X stops sending tweets.
2. If the tab gave 400 or more tweets (`BACKFILL_THRESHOLD`), it searches
   `from:<user> -filter:replies -filter:retweets filter:links max_id:<oldest - 1>`, then the same
   without `filter:links` if that found nothing. This is a count, not a cap check: X gives no
   sign that a timeline was cut short.
3. Below the first tweet, X answers the search with one page: no tweets, a `Bottom` cursor. The
   page shows "No results" and asks for no more, so the browser feed scrolls for 60 seconds and
   ends as stalled (`flnkit/browser.py`, `STALL`).
4. The plugin read every stalled window with no tweets as an interruption, held, and tried
   again; after four, it paused and kept the cursor.

A dry run (`--dry`) did not show the bug: it has no listing, and the stall mark lived on the
listing, so the stall was lost and the search ended as done.

## How it was checked (live, by hand)

A probe wrapped the plugin's page reader and logged each page (count, dates, cursor), without
changing what it did. Runs were read-only (`--dry --again`) unless marked real.

| Check | Result |
| --- | --- |
| A: X's own counts | 11,272 tweets, 1,417 with media |
| A: Posts tab | 161 pages, 3,297 tweets, down to the first tweet; then one empty page |
| A: search below the first tweet | one page, 0 tweets, a cursor; then nothing for 60s |
| A: two full listings compared | the same 1,814 posts; the second also found one card post |
| B: X's own counts | 24,381 tweets, 6,040 with media |
| B: Posts tab | 432 pages, 9,255 tweets, back to 2016-05, no error, still going when stopped |

So the old 3,200-tweet figure does not hold for the logged-in web timeline on these accounts.

## The fix (`61a93c7`)

- `twt._window_stalled`: a window with no new tweet is a stall only if X sent an error, or sent
  nothing at all. If X answered (a page with a cursor, `meta["answered"]`) and the window held
  no tweets, it is the end, and the listing's stall mark is cleared. Both the backfill and
  `twt?<query>` searches use it.
- `_search_reader`: an empty first page ends the feed at once, instead of scrolling 60 seconds.
  Empty pages after tweets still get `SEARCH_EMPTY` more tries.
- The engine's feed records `stalled` itself, so a dry run sees what a real run sees.
- Messages: "no new answer in 1m; stopped scrolling", "search stalled", "paused search backfill
  after it stalled or failed 4 times", and the now line shows holds with `text.duration`.
- Kept as they were: the 400-tweet switch to search, the pauses between search windows, the cap
  of 2 rate-limit resets, the skips on sync, protected accounts and `backfill = false`, and the
  30s/2m/8m holds whenever X really does not answer.
- Tests (network faked): an empty answer ends the search; no answer holds three times then
  fails and keeps `max_id`; a backfill resumed past the first tweet ends with no hold and no
  cursor; a stalled feed says so without a listing.

## Live results after the fix

### Account A (real run)

```text
✓ done in 1h 36m · 1 target · 23 saved (5.9 MB) · 1,383 already saved · 0 failed
```

- `posts` resumed the saved cursor at 10:23:11 and the search was over at 10:23:42: no stall,
  no hold, no warning in the whole run.
- Every category finished complete (posts, media, highlights, replies, profile); the saved
  cursors are empty.
- The long waits left were X's own rate-limit pacing ("rate-limit low (15 left); pausing 776s
  on tab"), which is the anti-ban guard working.

### Account B (real run)

```text
done in 4h 13m · 12,627 files (4.8 GB) · 3 failed
```

- `posts` read the whole account, down to 2013-10 (its oldest media), and the search after it
  ended with no stall and no hold. Every category finished complete; the saved cursors are
  empty.
- No warning besides X's rate-limit pacing ("rate-limit low (15 left); pausing 584-680s"),
  waited out each time, and the per-size fallbacks of three old images.
- The 3 failed files are 2015 images X no longer serves: every size, and the plain image link,
  answer HTTP 404. Nothing flnd can fetch.
- X counts 6,040 tweets with media; flnd saved 5,904 with their own photos or videos (5,590
  posts, 314 replies), plus the 3 posts whose images are gone. The rest, about 2%, is the same
  replies gap as on A (see below).

## Open items

- **One post missed by a full listing.** The first run of A never saw one tweet (a link card
  from 2024-05). A second full listing found it; it was fetched by its link. Normal sync reads
  only down to the newest saved post, so it would not have found it. Cause unknown: most likely
  one timeline page that did not come through.
- **Replies with media.** A has 1,417 tweets with media (X's count) and flnd saved 1,370 with
  their own photos or videos (1,345 posts, 25 replies); B has 6,040 and flnd saved 5,904, plus
  3 whose images are gone. The rest (47 on A, about 130 on B) are most likely replies: the
  replies tab seems to stop early (not proven), and the search backfill leaves replies out
  (`-filter:replies`). This was so before the fix; a replies backfill through search
  (`from:<user> filter:replies filter:media max_id:`) would close it.
- **The Media tab stops early.** On A it listed 825 tweets, back to 2020-08, while posts with
  media go back to 2017. Every tweet it listed was already saved through `posts`.
