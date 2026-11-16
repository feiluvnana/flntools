# Review report

This review ran on 2026-11-16 alongside the revamp session (`replace-plan-folder-revamp`). PLAN.md still exists, so the two sessions split files by owner:

- **The revamp session** owns `_fs`, `_sh`, `_worker`, `_http`, `_browser`, `_crypto`, `_prompt`, `_cli`, `_ui`, `_media`, and `flnd/cli.py`, `engine.py` and `__init__.py`. Defects found in those files were sent to that session (see "Handed over").
- **This review** fixed defects in `_archive`, `_store`, `flnd/job.py`, `flnd/targets.py` and the sources.

The baseline was green: 995 tests and pyright 0/0. Every commit below was checked green and pushed signed.

## Fixed
- **`cbbc72b` (ins, twt): a profile whose name starts like a wall path was treated as a login wall.**
  - Instagram (`/challenge_example/`) backed off twice, then raised `LoginError` for a valid user. On X, `x.com/login_example` raised immediately.
  - Guards: `test_a_profile_whose_name_starts_like_a_wall_is_not_one`, `test_a_handle_that_starts_like_the_login_path_is_read`.
- **`8522f68` (tlg): a Telegram resume could write a hole into the file.**
  - The `.json` offsets were saved before the short first chunk after a resume left Python's buffer. A kill right then made the next run skip bytes that were never written.
  - A damaged offsets file (`[]`) crashed the download with `AttributeError` instead of restarting it.
  - Guards: `test_the_offsets_file_never_counts_bytes_still_in_the_buffer`, `test_a_damaged_offsets_file_starts_the_download_over`.
- **`daa2354` (archive): four security and data-safety holes.**
  - 7z and rar archives could hold symlinks, junctions and hard links, which 7zz and unrar extract. Only tar links were refused.
  - A relative path starting with `-` (`-sdel`) reached 7zz or unrar as a switch.
  - Packing a 7z, or a zip with a password, over an existing archive added to it instead of replacing it, and it wasn't atomic.
  - Guards: `test_links_listed_by_7z_are_refused`, `test_links_listed_by_unrar_are_refused`, `test_a_path_like_a_switch_reaches_the_tools_as_a_path`, `test_packing_a_7z_over_an_old_one_replaces_it`.
- **`c11c573` (targets): a failed TikTok download stored the `sessionid` cookie in plain text in `targets.db`.**
  - The cookie came from `Post.headers` and sat outside the encrypted vault.
  - Login headers are now dropped. Old rows are cleaned the next time that target's failed downloads change.
  - Guard: `test_a_failed_download_never_keeps_a_login_header_on_disk`.
- **`deb4898` (store): a busy file broke later transactions.**
  - When `BEGIN` failed (busy timeout), the batch depth stayed at 1, so every later `batch()` ran without a transaction or rollback.
  - A rolled-back batch also left newly created tables marked as made, so the next write failed with "no such table". A failed `COMMIT` left the transaction open.
  - Guards: `test_a_begin_that_fails_leaves_later_batches_whole`, `test_a_table_made_in_a_rolled_back_batch_works_after`.
- **`580380f` (store): a store that failed to open leaked its connection, and a stale row escaped the error tree.**
  - When a PRAGMA failed (damaged file), the SQLite connection was never closed.
  - A row that no longer fits its `Row` type raised `msgspec.ValidationError` instead of `store.Error`.
  - The suite's `ResourceWarning: unclosed database` came from `tests/test_store.py` itself, which is fixed too.
  - Guards: `test_a_file_that_is_no_database_raises_and_closes_its_connection`, `test_a_row_that_no_longer_fits_its_type_is_a_store_error`.
- **`f73e341` (store): two processes opening one old JSON store could both convert it, and one lost the other's data.**
  - Guard: `test_two_openers_convert_a_json_store_once`.
- **`4b4ac90` (fbk): Facebook's wall and block checks could loop forever or fire on a real profile.**
  - A login wall and a block page shown in turn looped forever (the counters reset each other).
  - A profile at `/login.example` was read as the login wall.
  - Guards: `test_a_login_wall_and_a_block_in_turn_still_give_up`, `test_a_profile_named_like_the_login_page_is_no_wall`.
- **`c2f8353` (job): two files of one post with the same name got the same path.**
  - The second was counted as "skipped" and never downloaded.
  - Guard: `test_two_files_of_one_post_with_one_name_get_two_paths`.
- **`fea5f63` (ins): a profile page that brought no user result was reloaded for every category.**
  - That cost up to 10 s each for stories, highlights and profile.
  - Guard: `test_a_profile_page_without_its_user_is_loaded_once`.
- **`889c85c` (tik): a saved photo post of 5 or more images counted as 5 saved posts.**
  - That stopped the scroll early and skipped every older video on sync.
  - An old pinned carousel ended the listing the same way through `--time`.
  - Guard: `test_a_photo_post_counts_once_toward_each_streak`.
- **`b14d329` (twt): an error page or an odd date ended or failed a timeline.**
  - A throttle or error body in the middle of a scroll ended the timeline as if it were complete.
  - A `created_at` in another shape failed the whole category with `ValueError`.
  - Guard: `test_an_error_page_or_a_bad_date_does_not_end_the_scroll`.

## Faster
Each number below was measured in this session, on an M-series Mac with Python 3.14.

| What | Before | After | How |
| --- | --- | --- | --- |
| `archive.ls` on a zip with one 256 MiB entry (`c268518`) | 26 ms, +256 MiB RSS | < 1 ms, +0 MiB | `ru_maxrss` and `perf_counter`, a fresh process per run, 3 runs |
| `archive.unpack` on the same zip | 70–78 ms, +256 MiB RSS | 70–78 ms, +2–4 MiB | same |
| `Ledger._target_undated_rows` on 100k history rows over 7 sources (`b3c859d`) | 62 ms per target per sync | 6 ms | best of 5, a store-backed table |

Before `c268518`, a 20 GB entry (inside `Limits.size`) was read whole into memory. Guards: `test_a_big_entry_is_streamed_not_held_in_memory` and `test_listing_reads_the_header_not_the_whole_archive` (tracemalloc peak < 4 MiB for a 16 MiB entry).

## Redundant
- **`flnd/targets.py:SECRET_HEADERS`** duplicates `shared/_http.py:SECRET_HEADERS`. Proposed spelling: `http.secret(name) -> bool`, or export the frozenset as `http.SECRETS`.
- **The 5-in-a-row streak** is defined three times: `job.py` `OLDER_IN_A_ROW` and `SAVED_IN_A_ROW`, and `source.py` `SAVED_STREAK`. `older_streak`, `_hit_saved` and `OLD_STREAK` are rewritten in ins, tik and twt, differing only in how they read a date.
  - Proposed: one `feed.Streak(since, date=…)` with `.saw(post_id, posted) -> bool` (stop).
- **Wall and back-off loops** appear three times: `ins.load`, `fbk.read_page` and twt `_scroll_timeline`, and all three had wall bugs.
  - Proposed: `relay.walled(site, url, walls=(…), attempts=n) -> bool` in `flnd/sources/common`, matching whole path segments.
- **`ALL = "all"`** is defined three times: `engine.py:27`, `job.py:58` and `targets.py:23`. Keep `options.ALL`.
- **Constants and small helpers in the sources:**
  - Chrome UA strings: twt and tik share Chrome/128; ins uses Chrome/131.
  - The Referer-only `CDN_HEADERS` is the same in ins and fbk.
  - `image_extension` is duplicated in fbk and ins.
  - `fbk.page_info` and `PAGE_INFO_DEPTH` are used only by a test.
- **`shared/_http.py`:**
  - `CHUNK_SIZE` is unused.
  - The backoff loop exists in three copies.
  - `_where` re-implements `text.redact`, and `ui._brief` cuts URLs a third way.
  - These were handed over.
- **`MAX_SIZE`** is the same idea with two values: `_cookies.py` (16 MiB) and `_selector.py` (64 MiB).
- **Chrome folders** are listed twice: `_browser.py` `PROFILE_ROOTS`/`CHROME_PATHS` and `_cookies.py` `_CHROMIUM`.
- **`_store.Table.items`/`values`** duplicate `rows.get()`.
- **`dry.py` `tree` and `render_tree`** group the same way. `dry.py:93` hardcodes `.flnd` next to `cli.HOME`.
- **`engine._is_query` and `_is_query_kind`** disagree for a discovering route that isn't a `?` query.
- **`flnd/sources/common/feed.py`** reaches into `Site` through `getattr(site, "_push_tab")`, `_pop_tab`, `_take_warm_responses` and `_warm_tabs`. A small public `Site.tabs` protocol would say it plainly.

## Missing
flnd:
- **`flnd verify` / `fl.files.filter(missing=True).get()`.** Re-check history rows against the disk (size and mtime are stored already), and offer to forget or re-download what's gone.
- **A sync summary that fails loudly.** `flnd sync` should exit non-zero when a login expired (`Report.expired: list[str]`), so cron or launchd notices.
- **`flnd targets --json`.** A script could then read the list without parsing the table.
- **Login headers rebuilt on retry.** A failed download kept for retry no longer carries login headers. Sketch: `Source.headers(site, url) -> Mapping[str, str]`, asked again when a failed file is retried.
- **Parallel targets across sources.** `_run` goes one target at a time even when ins and twt share nothing. Sketch: `fl.sync(jobs=2)`.

shared:
- **`archive` progress through a sink, not `Callable[[int, int], Any]`.** This breaks rule 7, and the archive redesign plans it already.
- **The archive password off argv.** `-p<password>` is visible to other local users in `ps`.
  - A bare `-p` makes 7zz prompt for the password. Whether it reads that from a pipe is unverified.
  - Sketch: unchanged API; `sh.run(..., input=password)` with a new `input` keyword.
- **`store.Db` closing on GC (`weakref.finalize`).** A dropped `Db` would then close its connection instead of warning.
- **A public `http` way to scrub secret headers** (see Redundant). Any script that saves a request needs it.

## Decisions taken
Each one is reversible.

- **Commit attribution.** No co-author trailer, per AGENTS.md and REVIEW.md, overriding the harness's default trailer.
- **Splitting files with the revamp session.**
  - I followed its ownership list instead of the strict "last hour" rule; it explicitly cleared `_archive`, `_store`, `job.py` and the sources for me.
  - I worked in a separate worktree and fast-forwarded `main`, so its uncommitted edits never ran in my checks.
- **Old cookie rows in `targets.db`.** They are cleaned the next time that target's failed list changes, not by a migration over the whole file.
- **A retried TikTok download now goes without the session cookie.** I think its signed CDN URL has likely expired by then anyway, but I didn't verify that. A source hook to rebuild login headers on retry is listed under Missing.
- **How link entries are refused.**
  - 7z: a Windows `L` (reparse point) flag, a Unix `l` mode, or a `Symbolic Link`/`Hard Link` field.
  - unrar: a `Type` naming a link, junction or reference.
  - Both refuse the archive, as tar links already were. They don't skip the entry.
- **The `unpack` conflict check.** It still checks for an existing file and then stages; it doesn't hard-link like `fs.write(replace=False)`. The upfront check in `conflict="error"` mode already covers the normal case.
- **Telegram offsets on each chunk.** The offsets file is still written per chunk, now after a flush. Batching it would save renames, but resume would lose its exactness, and the cost wasn't measured as a problem.
- **Ending a Twitter timeline.** A page without a `Bottom` cursor and without new tweets is now skipped, not treated as the end. The scroll still ends when no response arrives.

## Not done
Handed over to the revamp session, all verified by reading the code. The http items were read before the http redesign (`cc8ccbe`) and still need checking against it:

- **`http`:**
  - URL userinfo leaks into error messages.
  - A complete `.part` file gets a 416 forever.
  - 429 handling has no attempt cap when `Retry-After` is sent.
  - POST requests are retried.
  - A `.part` file carries over between mirrors.
- **`cookies`:**
  - `Jar.save` creates its stage before setting mode 0600.
  - Missing files raise bare builtins.
  - `security` and `secret-tool` run with no timeout.
- **`fs` and `text`:**
  - `fs.lock` turns the caller's own `OSError`s into `fs.Error`.
  - `fs.write(replace=False)` raced; already fixed in `771e6f5`.
  - `text.filename` allows 255 bytes, which stage and lock siblings overflow; fixed in `32cc030`.
- **`worker`:**
  - `Loop.close` leaves tasks pending, which is the suite's "Task was destroyed but it is pending" warning.
  - `Pool.__exit__` on Ctrl-C runs the whole queue first.
  - Both are folded into the worker redesign.
- **`site` and `engine`:**
  - `site.close` leaks the browser when Chrome has crashed.
  - `target del` doesn't stop syncing.
  - `TargetError` gives a traceback in `sync` and the `target` commands.
  - The `.flnd` lock waits forever with no message.
  - `_folder_stats` rglobs the tree and double-counts links.
  - Every fetch copies every source's browser cookies.
- **`flnd --help` startup.** It measured 180 ms wall and 140 ms of imports, against a 150 ms target.
  - `flnd/__init__` imports `engine`, which pulls in questionary/prompt_toolkit (28 ms), httpx (20 ms), every source (16 ms) and `rich.markdown` (13 ms).
  - Fix: import the engine inside the commands, and load sources lazily.

Found and left for later:
- **The tik watchers leak when a category isn't run.** With `--from videos`, the stories watcher is never closed.
  - It needs `Profile.close()` in a `finally`. `tik.py` was being edited by the revamp session.
- **fbk albums build a new `seen` set for each result**, so repeated photos are yielded again. Only extra work, because the engine dedupes by key.
- **A synced query loses items past `MAX_SYNC_NEW`** (`engine.py:654` with `job.py:1425`). Children 26–40 of a burst of 40 are never fetched.
  - This needs a "pending" list in the target row, which is a design decision for the owner of `engine.py`.
- **loi's daily quota uses the local date**, while the site's day is likely UTC+8. Near midnight the count resets at the wrong time. Which day the site uses has to be checked by hand.
- **The tik `cdn_headers` cookie** is sent to every mirror host the JSON names. It should go only to TikTok's hosts.
- **`test_browser.ScrollTest.test_seeded_run_is_repeatable` is flaky under load.** It failed once in a full check and passed 8 times out of 8 alone, because the scroll depends on wall-clock waits of 20 ms. It should use `clock.Fake`.

To verify by hand on the authorized test accounts:
- **Instagram:** a profile whose name starts with `challenge`, and a profile that sends no user result.
- **X:** a handle starting with `login`, and a long timeline under rate limiting.
- **TikTok:** a profile whose newest saved post is a photo carousel, synced after new videos.
- **Facebook:** an account that hits the block page, and a profile path starting with `login`.
- **Telegram:** kill a large document download mid-way, rerun, and compare checksums.
- **TikTok, after a failed download:** `.flnd/metadata/targets.db` holds no `sessionid`, and the retry still downloads.
