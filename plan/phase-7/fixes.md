# fixes

Phase 7, step 3. A read-only bug hunt of `flnd` and `shared` on 2026-11-13, each finding traced
through the code at 699116a and checked again by hand; none is covered by a test today. Each fix
comes with a test that fails first, as Phase 6's [fixes](../phase-6/fixes.md) did.

## 1. High: `sync` can't restore a lost file once a feed is caught up, and deletes a broken one for good

`Engine.sync` (`engine.py` 258–265) runs `repair.repair`, which deletes a broken file with its
history row and notes a missing one, then fetches with the saved-run stop (`sync=True`): a
`newest_first` category recorded complete ends after 5 saved posts in a row. A lost file deeper
in the feed is never reached, so it stays lost, and `repair.count` blames the site ("its post may
be gone").

**Fix:** a target whose tally has a broken or missing file lists its categories to the end in that
sync (no saved-run stop), as Phase 6's redesign promised ("missing, empty or broken files
downloaded again"). **Test:** a complete feed with a broken file 10 posts deep is listed past the
stop and the file is saved again.

## 2. Medium: a lazy post whose first file is saved is skipped forever, even if a later file failed

`Job._saved_first` (`job.py`) skips a lazy post when its first key is saved. An Instagram carousel
whose file 0 saved and file 1 failed (`IncompleteError`, `NotMediaError`, a 403 after a fresh
link) is skipped by every later run, and file 1 has no history row for repair to notice. Under
`sync` it also counts as caught up, feeding #1.

**Fix:** the first file's history row records the post's file count (`files: 3`); `_saved_first`
skips only when every `<id>_<n>` key is saved. **Test:** a carousel with file 1 failing is listed
again by the next run, and only file 1 is downloaded.

## 3. Medium: X marks a category complete after its listing failed part way

`twt` `api()` (`twt.py` 368–378) warns and returns normally after a connection failure (a
`FetchError` with no status) or an answer that isn't JSON; the category then ends as if listed to
its end, and `targets.db` records it complete. Every later `sync` stops after 5 saved posts, so the
pages after the failure are never fetched. tik's `open()`/`_load()` returning False may end `videos`
the same way; to trace while fixing.

**Fix:** a listing cut short raises (a `FetchError`, after the warning), so the category isn't
recorded; the engine's rule "complete only when the generator ends" then holds. The same audit
for every source's early returns. **Test:** a failure on page 3 leaves the category unrecorded.

## 4. Low: a file queued earlier in the same run counts as saved for the saved-run stop

`Job.queue` counts a file found in `_queued` as `found`, so a post whose files another category
queued this run counts as caught up. Under `sync`, X's `posts` queues new tweets and `media` then
stops after 5 of them, missing media further down; the queued files may still fail.

**Fix:** only a file on disk or in history counts toward `caught_up`; a queued one is neutral
(None). **Test:** two overlapping `newest_first` categories with 6 new posts list the second one past them.

## 5. Low: a resume can adopt a sibling file's part

`shared.http._resumable` matches sidecars by `startswith(".<name>.")` and `endswith(".part.json")`,
so `a.rar` adopts `.a.rar.001.<hex>.part`; its `If-Range` gets a 200 and the sibling's part is
deleted, even while that download runs. loi's split archives make this real.

**Fix:** the rest of the name must be exactly 8 hex digits and `.part.json`. **Test:** `a.rar`
and `a.rar.001` downloading in one folder keep their own parts.

## 6. Minor: a URL with its query in one warning

`on_response`'s handler warning (`shared/_browser.py`) logs `response.url` with its query; every
other message uses the URL without it. **Fix:** the [lean](lean.md) E6 URL scrubber.

## Gaps found by comparing with gallery-dl

Not crashes, but files flnd never gets; each with a fake-network test, and a live check by hand
on the test accounts.

### 7. X: older tweets past the timeline's cap are never listed

X's `UserTweets` and `UserMedia` stop after a few thousand tweets, so a prolific account's older
ones are missed, and with #3 fixed the category is still recorded complete. gallery-dl goes on
with `SearchTimeline` and `from:<user> max_id:<last id>`, `filter:links` first, moving `max_id`
down a page at a time, and tolerates 3 empty pages (with a smaller `count` each) before stopping.
**Fix:** the same in `twt`, inside the `posts` and `media` categories, under X's own rate headers.

### 8. One CDN address per file, where the site gives several

`tik` keeps only the first of `PlayAddr.UrlList` (several CDN hosts) and of an image's `urlList`;
TikTok answers 403 to many posts on one host and not another. X images have `orig`, `4096x4096`,
`large` and `medium`; Facebook videos `hd` then `sd`. **Fix:** every alternate URL goes into
`File.url` as a mirror, best first, which `http.download` already tries in order.

### 9. A login that is about to expire isn't said

A scheduled `flnd sync` meets the login wall with no warning. **Fix:** at a run's start, a login
cookie that has expired, or will within 24 hours, is a warning naming the source and the cookie's
name (never its value), as gallery-dl does.

### 10. An interrupted listing starts over

A 5,000-post Instagram account costs about 400 GraphQL calls to list again after a Ctrl-C or a
block, about 15 minutes of its budget. **Fix** (user, 2026-11-13; the lighter form of what
Phase 6's redesign D7 deferred): a category that stops before its end saves its last cursor in
`targets.db`, and the next run of that target starts from it, then lists the newest posts down to
where the first run began; a cursor the site refuses is dropped and the listing starts over.

How (user, 2026-11-13): `pages()` tells the job each page's cursor, and a category cut short
saves the cursor of the page it was reading in `targets.db` (`cursors`). The next run lists from
that cursor to the end first, then the newest posts down to 5 saved in a row (the saved-run stop,
forced on even under `download`); a `newest_first` category only, since only its newest posts are
together. Only a category's first `pages()` call resumes. A cursor the site refuses (a 4xx) is
dropped with one info line, and the listing starts over. Async sites (tlg) aren't covered.

### 11. Content gallery-dl gets that flnd leaves out

Decided by the user on 2026-11-13, with no new option (redesign §1): **an Instagram story's
music** is saved beside it as its audio file; **an X post's quoted tweet** brings its media, filed
with the quoting post; **an X card's image** is saved with its post. Each is a file of the post it
belongs to, keyed so a second listing doesn't save it twice.

## Checked and found sound

Split and resumed ranges, `If-Range`, the 206 checks, preallocated parts, host slots; `kv` across
threads and the live area's drawing thread; the job's pending counts and the pool's stop; both
Ctrl-C presses; path safety in names (`..` becomes `untitled`) and UTC in `day()`; secrets in
messages and cookie write-back.
