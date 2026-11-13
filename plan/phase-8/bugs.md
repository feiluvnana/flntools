# bugs: what is broken in `shared` and flnd today

Part of [Phase 8](README.md). An audit of `shared` and flnd's engine side on 2026-11-13, at
`37147c1` (Phase 7 step 8 built, step 10 begun). Line numbers are at that commit, except in the
files Phase 7 step 9 changed (`_log.py`, `_theme.py`, `_style.py`, `_http.py`, `_browser.py`, and
flnd's `job.py` and `engine.py`): those were re-checked at `af6eef3` on 2026-11-13. Every bug in
them still stands, and step 9's new code added L16–L19.

**How each bug was shown.** Every bug here has a failure scenario that a script reproduced on
fakes (`httpx.MockTransport`, `clock.Fake`, the fake browser, generated media, example names), or,
where it can't run on macOS (Windows only, no `unrar`), a line-by-line trace, marked *traced*.
The high and medium ones were run a second time by hand before this file was written. Suspected
bugs that could be neither run nor traced were dropped, and nothing that Phase 7's plan already
fixes is listed. The scripts stay outside the repository; each bug's test is written first, fails,
then passes with the fix (as Phase 7's fixes did).

Severity: **high** loses data, hangs, or spins a core; **medium** fails a file or a run that
should work, or leaks something; **low** is wrong but rare or cosmetic.

## Summary

| Severity | shared | flnd | All |
| --- | --- | --- | --- |
| high | 5 | 1 | 6 |
| medium | 22 | 7 | 29 |
| low | 29 | 3 | 32 |
| all | 56 | 11 | 67 |

Fix order: the six high ones first, one commit each with its test, then medium by module, then
low folded into the module's API commit ([api](api.md)) where they touch the same lines.

## http, download, pace

| # | Sev | Where | What fails, and why | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| H1 | high | `_pace.py:130-140`; `_http.py:624-626, 645-648` | A 429 with `Retry-After: 0` (or a date in the past) makes `Client.fetch`, `download` and `call` resend at once, forever, with a warning each time: 10,000 requests and 0 s slept in the repro. `throttled(0)` holds the host until *now*, and a floor of 0 (any `Pace(label=)`) adds nothing. | Hold at least the back-off's step (30 s doubling) whatever the header says. | `test_a_429_with_retry_after_zero_waits` |
| H2 | medium | `_http.py:689, 867`; `_pace.py:115-122` | `Retry-After: 1e400` raises a builtin `OverflowError` out of `Client.fetch`; a huge finite value (or `x-rate-limit-reset`) holds the host for ever. | Refuse a value that isn't finite; one clamp (900 s) for every path, so `Client.download`'s own `MAX_RETRY_AFTER` goes. | `test_retry_after_infinite_or_huge_is_clamped` |
| H3 | medium | `_http.py:366, 675`; `_download.py:136, 443` | A URL httpx can't parse (a scraped link ending in `\n`) leaks `httpx.InvalidURL` from `fetch`, `Client.fetch` and `download`, past every `http.Error` handler; in `download([bad, good])` the good mirror is never tried. | Wrap `InvalidURL` as `http.Error`; build each mirror's transfer inside its `try`. | `test_invalid_url_raises_http_error`, `test_download_skips_an_invalid_mirror` |
| H4 | medium | `_http.py:385-388, 558-561` | `Client(stop=…).fetch` goes on retrying a 5xx after `stop` is set: three more requests and 1 + 2 + 4 s of sleep that `stop` can't end. | One retry layer: `Client.fetch` sends once and retries through `Client.call`, whose waits honour `stop`. | `test_client_fetch_stop_ends_5xx_retries` |
| H5 | medium | `_pace.py:143-150`; `_http.py:649` | Under concurrency a host's back-off never doubles: a 200 for a request sent before the 429 calls `clean()` and resets the strikes (30, 33, 29, 27 s instead of 34, 66, 116, 217 s). | `turn()` notes when a request went out; `clean()` ignores answers sent before the last throttle. | `test_backoff_doubles_despite_in_flight_successes` |
| H6 | medium | `_download.py:763-768` | Resuming a part with another width (a one-connection part resumed with `parts="auto"`, as the next mirror does) fetches the rest twice: 63 MB served for 38 MB missing, progress at 65 of 40 MB. `_resumed` opens its first stream before `_recut` splits that segment. | `_recut` before choosing the first stream. | `test_resume_with_more_parts_fetches_each_byte_once` |
| H7 | medium | `_download.py:908, 921, 1008-1013` | A server that caps each range response (a valid, shorter 206) fails the file: each short piece counts as a failure, and failures are never reset between attempts. | A body that ends at its own `Content-Range` end is progress: requeue the rest; reset failures each attempt. | `test_download_from_a_server_capping_ranges_completes` |
| H8 | low | `_download.py:464-466, 551-569` | A body that drops before its first byte, three times, raises `ChangedError` ("the file changed during a resume") instead of `http.Error`: the retry has no `Range`, and its 200 is read as a changed file. | A 200 means "restart" only when a `Range` was sent. | `test_drop_before_first_byte_raises_error_not_changed` |
| H9 | low | `_download.py:479-480` | *Traced, Windows.* The final rename is a raw `os.replace` outside `atomic`'s retry, so a scanner holding the file gives a builtin `PermissionError`. | One rename through `atomic`'s retrying replace (which also drops a double rename). | `test_final_rename_retries_permission_error` |
| H10 | low | `_http.py:744-751` | `Transport` resends a POST over HTTP/1.1 after HTTP/2 dropped, though the server may have handled it. | Resend only GET, HEAD, OPTIONS; other methods raise. | `test_transport_does_not_resend_post` |
| H11 | low | `_http.py:366` | `fetch(data=b"…")`, documented as raw bytes, gives httpx's deprecation warning; httpx 1.0 removes it. | Send bytes and text as httpx's `content=`. | `test_fetch_raw_body_no_deprecation` |

## kv, crypto

| # | Sev | Where | What fails, and why | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| K1 | medium | `_kv.py:224-230, 300-309, 369-374` | A lone surrogate (an `os.fsdecode` name from a non-UTF-8 Linux file) in a value or key raises a builtin `UnicodeEncodeError`; `keys(prefix="퟿")` builds an invalid bound. | Values: fall back to `ensure_ascii=True` JSON; a surrogate key is `UnstorableError`; the prefix bound skips U+D800–DFFF. | `test_store_value_with_a_surrogate`, `test_keys_prefix_before_surrogates` |
| K2 | low | `_kv.py:181-183` | `for k, v in store.items()` raises `KeyError` when a `ttl` key expires, or another process deletes one, mid-loop. | Own `items` and `values`, one `SELECT` each (also [speed](speed.md#kv)). | `test_items_skips_a_key_expiring_mid_loop` |
| C1 | low | `_crypto.py:208` | The hashes refuse `bytearray` and `memoryview`, which `encrypt` takes. | Accept what `encrypt` accepts. | `test_digest_accepts_bytearray` |

## log, theme, prompt, cli, fmt

| # | Sev | Where | What fails, and why | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| L1 | high | `_log.py:824-837, 862-880` | While a question is open, once any row changes, the drawing thread spins a whole core: 1.8 million ticks and 0.91 CPU-s in one second. `tick` returns early without clearing `dirty` and `wake`. flnd asks questions mid-run (a login). | Clear both before each early return. | `test_live_area_sleeps_while_a_question_is_open` |
| L2 | medium | `_theme.py:834-879`; `_log.py:500, 586` | A row whose text holds `\n` or `\t` (a multi-line Playwright error as a caption) leaves stale lines in the scrollback: the area counts one line per row. Zero-width characters count one cell. A status line splits on `\n`, but a tab or a zero-width character in it miscounts the same way. | Turn control characters (but colour and link escapes) into spaces before fitting; zero-width characters are 0 cells. | `test_row_caption_with_newline_or_tab_keeps_one_line` |
| L3 | medium | `_log.py:314-333, 1048-1063` | Remote text in a log field reaches the terminal raw when colour is on: OSC 52 writes the clipboard, `ESC]0` sets the title, `ESC[2J` clears the screen. | Clean each field value: drop escapes other than colour, and control characters but `\n`. | `test_log_field_escapes_other_than_colour_are_dropped` |
| L4 | medium | `_log.py:296-312, 343-351` | A line logged from another thread while `log.record` exits raises `ValueError: I/O operation on closed file` in that thread (a download ending as the run does). | Close the file under the sink's lock, mark it closed; a write after it is dropped. | `test_record_line_racing_block_exit_is_dropped` |
| L5 | medium | `_log.py:67`; `_fmt.py:44-45` | A password in a URL (`https://example_user:secret@example.com/`) reaches the run log, the screen and error messages. Two scrubbers, both keeping the user part. | One rule in `fmt.url`: drop user info, query and fragment; `log.record` uses it. | `test_url_drops_userinfo`, `test_record_drops_userinfo` |
| L6 | medium | `_params.py:530-533` | An option read from stdin keeps its newline (`'abc123\n'`); a bad choice or non-UTF-8 bytes there end in a traceback, not exit 2. | Strip one line ending; turn conversion errors into `UsageError`, as positionals do. | `test_stdin_option_drops_trailing_newline`, `test_stdin_option_bad_value_is_usage_error` |
| L7 | low | `_theme.py:623-634`; `_prompt.py:496` | Typed input longer than the screen hides the caret and the end. | Scroll a window around the caret. | `test_long_input_keeps_caret_on_screen` |
| L8 | low | `_theme.py:604-607` | Wide characters in a question's description are cut (38 of 60 shown). | Wrap by display width. | `test_prompt_description_wraps_wide_characters` |
| L9 | low | `_params.py:491-511` | Command help includes the docstring's `Returns:` and `Raises:`; an `Args:` continuation such as `example: https://…` starts a false argument. | Any `Word:` header ends the description; entries count at the first entry's indent only. | `test_help_ignores_returns_and_raises`, `test_args_continuation_with_colon` |
| L10 | low | `_log.py:1090-1096` | `log.info("{name:>{width}}", name="a", width=6)` raises `TypeError`. | Walk fields nested in a format spec. | `test_log_nested_format_field` |
| L11 | low | `_cli.py:431-433` | A handler returning `True` exits 1. | A bool is no exit code: refuse it, or True is 0. | `test_handler_returning_bool_exit_code` |
| L12 | low | `_log.py:309` | Record lines are stamped with `datetime.now()`, not `shared.clock`, so a fake clock can't pin them. | `clock.wall()`. | `test_record_stamps_with_shared_clock` |
| L13 | low | `_fmt.py:45` | `fmt.url("//cdn.example.com/a.jpg?sig=x")` gives `://cdn.example.com/a.jpg`. | No scheme keeps `//host/path`. | `test_url_protocol_relative` |
| L14 | low | `_fmt.py:22, 25` | `fmt.filename` keeps bidi controls (`photo‮gpj.exe` shows as `photoexe.jpg`) and allows `COM¹`–`³`, `LPT¹`–`³`, `CONIN$`, `CONOUT$`. | Drop Unicode format controls; complete Windows' reserved names. | `test_filename_drops_bidi_controls`, `test_filename_superscript_com_reserved` |
| L15 | low | `_prompt.py:496` | *Traced, Windows' old console.* A question's lines are fit to the full width, so a console that wraps at the last column miscounts and leaves a line on erase (Phase 7 fixed this for rows only). | Fit to `width - 1`. | `test_prompt_line_is_cut_one_short_of_the_width` |
| L16 | low | `_log.py:568-574, 650-667` | A `StatusRow` ended on one thread while its group closes on another writes its final line twice, on the screen and in the record file. `_end` sets the outcome, then `_retire` waits for the screen lock; `close` writes the row in between (it is still in the group), then `_retire` writes it again. Shown with the two steps interleaved by hand. | Set the outcome and retire the row under the screen lock, in one step. | `test_status_row_ended_while_its_group_closes_is_written_once` |
| L17 | low | `_log.py:898-931` | Rows nested under a `StatusRow` (`log.under(row)`) vanish from the live area when that row ends while they still run. A wait row (`example.com: waiting 0/30`) disappears, so the run looks hung. The ended row leaves the group, and nested groups are drawn only under their parent's line. | Draw a nested group whose parent row has left the area as a top-level group. | `test_rows_under_an_ended_status_row_stay_drawn` |
| L18 | low | `_theme.py:328-345` | `theme.gradient` over text that holds an escape (a painted word, a link) paints each character of the escape apart, so the terminal shows `[38;2;2;191;135m` as text. It walks characters, not `fit`'s units. | Walk escape-or-character units, and pass escapes through unpainted. | `test_gradient_keeps_escapes_in_its_text_whole` |
| L19 | low | `_theme.py:547-553`; `_log.py:576-580, 650-652, 763-779` | A `theme.Count` whose `role` is no palette key fails only once its number is above 0. Then the drawing thread stops for the rest of the run (a debug line only), and `finish()` raises `AttributeError` and leaves the row in its group. The block's exit raises again inside `LiveArea.close`, so the final frame is never drawn and the cursor stays hidden after the program ends. | Check each count's role and glyph when a `Status` is set (`add_status`, the `status` setter), raising `theme.Error`; `close` shows the cursor again in a `finally`. | `test_status_with_an_unknown_role_is_refused_when_set` |

## media

| # | Sev | Where | What fails, and why | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| M1 | high | `_pillow.py:117-118` | A JPEG with an MPF block (Ultra HDR, iPhone gain maps) opens as a 2-frame animation: `compress` raises a bare `KeyError: '.jpg'` (past flnd's handler) and `resize`/`crop` refuse "an animation". | MPO is a still: 1 frame, not animated. | `test_ultra_hdr_jpeg_compresses_as_a_still` |
| M2 | high | `_ffmpeg.py:352-357` | Cover art makes an MP3 or M4A a "video" with codec `mjpeg`; `compress` then fails with `RuntimeError`, which flnd reads as "no ffmpeg" and stops all audio compression for the run. | Skip `(attached pic)` streams when choosing the kind and the video stream. | `test_mp3_with_cover_art_is_audio` |
| M3 | high | `_ffmpeg.py:168-170, 194-195, 248-257` | An encode can hang without limit: the feeder thread swallows a frame error and never closes ffmpeg's stdin, and the stall watchdog is kicked by every progress line ffmpeg prints while it waits. A slideshow with a cut-off photo was still running after 40 s with a 3 s stall limit. | The feeder keeps its error, kills ffmpeg and re-raises it as `BrokenError` naming the photo; the watchdog counts only progress that moved. | `test_merge_with_a_broken_photo_raises_broken_at_once`, `test_encode_with_a_stuck_feed_stalls` |
| M4 | medium | `_pillow.py:156-159` | A blank or odd `OffsetTimeOriginal` (`"      "`) makes `media.open` raise a builtin `ValueError`; flnd's check fails a good photo as a download. | Parse the zone strictly; leave the time naive otherwise. | `test_open_reads_a_photo_whose_zone_is_blank` |
| M5 | medium | `_media.py:885` | ADTS AAC (`FF F1`) is sniffed as MP3; `compress().save()` re-encodes AAC to MP3 and renames `a.aac` to `a.mp3`. | Test the ADTS sync before MP3's. | `test_sniff_tells_adts_aac_from_mp3` |
| M6 | medium | `_ffmpeg.py:279-280`; `_media_video.py:256-261` | ffmpeg's failures are builtin `RuntimeError`, no `media.Error`; a common DASH pair (VP9 `.webm` + AAC `.m4a`) fails `merge(audio=)` because the container can't hold the track. | A `media.Error` subclass for ffmpeg failures; `merge` falls back to `.mkv` (no `format` given) or encodes the audio. | `test_merge_webm_video_with_aac_audio`, `test_ffmpeg_failures_are_media_errors` |
| M7 | medium | `_pillow.py:244-252` | A photo with XMP orientation is rotated and still tagged rotated, so `resize`, `crop` and `compress(strip=False)` come out sideways. | Take EXIF and XMP from the turned image, which drops the orientation. | `test_resize_of_a_photo_with_xmp_orientation_stays_upright` |
| M8 | medium | `_pillow.py:353-359` | A 16-bit grey PNG comes out 99.6% white (clipped at 255); a CMYK JPEG becomes RGB but keeps its CMYK profile. | Scale 16-bit to 8-bit first; keep CMYK for JPEG, or convert to sRGB and drop the profile. | `test_compress_keeps_a_16_bit_png_visible`, `test_cmyk_jpeg_keeps_a_matching_profile` |
| M9 | medium | `_media.py:1028-1031`; `_ffmpeg.py:370-371` | Anamorphic video (SAR 4:3) is compressed to square pixels without rescaling: 16:9 becomes 4:3. | Scale by the SAR before the cap; read the display size. | `test_compress_keeps_the_display_aspect` |
| M10 | low | `_media_video.py:92, 106, 190-193` | `frames(at=[1, 1])` raises `FileExistsError` after moving one frame in, though nothing should be written. | Dedupe times; check every name before moving any. | `test_frames_with_repeated_times_writes_each_once` |
| M11 | low | `_media_video.py:252, 293, 309` | Slideshow, join and concat errors name the temporary part, not the file. | Name the origin. | (in M3's test) |
| M12 | low | `_media.py:136, 160, 453, 949` | The docs say the cap is on the longer side; the code and its test cap the shorter, so a 12000×1000 panorama is never capped. | The docs say "shorter side" (question 7). | the existing `test_media.py` cap test |

## worker, archive, bundler

| # | Sev | Where | What fails, and why | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| W1 | medium | `_worker.py:647-653` | With `timeout=`, a task that raises `TimeoutError` itself (a socket timeout, `media.StalledError`) is reported at once as the pool's own "took longer than 10s", its error dropped. | Wait on the future with the deadline; a finished future's own error stands. | `test_a_task_raising_timeout_error_keeps_its_error` |
| W2 | medium | `_worker.py:466` | A task that submits to its own pool deadlocks once every runner waits for room (a crawler finding work while working). | No room wait when the caller is one of the pool's own runners. | `test_a_task_can_submit_to_its_own_pool` |
| W3 | low | `_worker.py:637-638, 685-690` | An `async def` task with `processes=True` always fails (`cannot pickle 'coroutine'`). | Run it with `asyncio.run` in the child. | `test_async_task_in_processes` |
| W4 | low | `_worker.py:351-355` | A pool never submitted to warns at exit "never joined; 0 tasks". | Warn only for unfinished tasks. | `test_unused_pool_is_silent` |
| A1 | medium | `_archive.py:727-750, 781-786` | An archive with a file `a` and `a/c.txt` fails half-way with a builtin `FileExistsError`, leaving files in `dest` that should be untouched. | Refuse a file that is also a folder of another entry, before writing, as `UnsafeError`. | `test_unzip_refuses_a_file_that_is_also_a_folder` |
| A2 | low | `_archive.py:738-749` | `Photo.jpg` and `photo.jpg` into an empty folder say "already in dest", which is false. | Its own message for names that clash inside the archive. | `test_case_twins_in_one_archive_say_so` |
| A3 | low | `_archive.py:627-635` | *Traced, no `unrar` here.* A RAR password goes on `unrar`'s command line, readable by any local user in `ps`. | Pass it on stdin. | `test_rar_password_not_on_the_command_line` |
| B1 | low | `_bundler.py:79, 96` | A `str` for `hidden=` or `data=` is taken a character at a time. | Type them as sequences and refuse a bare `str`. | `test_build_takes_one_hidden_module_as_text` |

## browser, cookies, selector

| # | Sev | Where | What fails, and why | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| R1 | low | `_browser.py:551` (and flnd `site.py:286`) | A failed `goto` warns with the URL's query (tokens, signatures). | `fmt.url` in both. | `test_goto_messages_drop_the_query` |
| R2 | low | `_browser.py:386-388, 413-414, 467` | A `Browser` keeps every tab it opened (1,000 wrappers after 1,000 closed tabs) and closes each again at the end. | Forget a tab when it closes. | `test_closed_tabs_are_forgotten` |
| R3 | medium | `_cookies.py:267-272, 576-578`; `_selector.py:271, 289-296` | A cookies file saved with a UTF-8 BOM (Windows editors) is refused whole; an empty expiry (a session cookie, which the stdlib reads) is refused; `selector.parse` reads BOM'd JSON as HTML and every query returns `[]`. | Decode with `utf-8-sig`; an empty expiry is a session cookie. | `test_load_reads_a_bom_file_and_an_empty_expiry`, `test_parse_reads_bom_json` |

## flnd

| # | Sev | Where | What fails, and why | Fix | Test |
| --- | --- | --- | --- | --- | --- |
| F1 | high | `job.py:1229-1231`, `_read` `1545-1552` | **New posts are never fetched** after an early stop by `--time`: the stop on page 2 saves that page's cursor as if the run were interrupted, the next run starts there and stops at once, and the "from the newest" pass only runs after a listing runs out. The cursor is saved again, so it repeats every run. | Only Ctrl-C or an error keeps a cursor: an early stop (`Ended.CUT`) clears it. | `test_an_early_stop_by_time_keeps_no_cursor` |
| F2 | medium | same lines | `sync`'s stop after 5 saved posts in a row saves a cursor when the run crosses a page; the next sync then lists the whole feed, then the newest. Every site pages through `pages()`. | F1's fix. | `test_a_saved_run_stop_keeps_no_cursor` |
| F3 | medium | `site.py:115-127`; `net.py:392-396` | A second Ctrl-C during a Telegram download hangs the run: the `KeyboardInterrupt` raised in the coroutine kills the site's loop thread before its future resolves, so `run_async` waits for ever and a third Ctrl-C is needed. | Progress inside a coroutine raises an `Exception`, never a `BaseException`; the loop thread survives a task's error ([flnd › machinery](flnd.md#2-generic-machinery-into-shared): `worker.Loop`). | `test_second_ctrl_c_in_an_async_fetch_returns_and_keeps_the_loop` |
| F4 | medium | `job.py:919-923, 1060-1063` | With `--shrink`, a category that raises drops the queued video encodes: the videos are on disk and linked but have **no history entry**, no date and no shrink, and later runs skip them because the file exists. | End the encode pool normally whenever downloads ended; keep the error for downloads only. | `test_a_failed_category_still_finishes_downloaded_videos_rows` |
| F5 | medium | `job.py:1220-1222`; `site.py:300-308` | A refresh during the final wait (a 403 on a lazy post) reopens a browser after the site closed, and nothing closes it: a Chrome and a Playwright driver per target. | Close the site after the downloads finish; a closed site refuses a page. | `test_a_refresh_after_the_listing_closes_its_browser` |
| F6 | medium | `site.py:247-249, 357-362` | A crashed browser is never closed: reading its cookies raises, `close()` stops there, and that error hides the listing's own; in `fetch` it escapes as a raw Playwright error. | Copy cookies in `try`, close in `finally`; map the error in `fetch`. | `test_close_with_a_crashed_browser_still_closes_it` |
| F7 | medium | `net.py:213-259` | A DASH merge retry downloads both finished streams again (Phase 7's "kept across a merge retry" isn't met), and a refused stream waits for its sibling to finish, then deletes it. | Delete streams only after the merge or the last failure; a failed stream stops its sibling, which keeps its part. | `test_dash_merge_retry_keeps_finished_streams`, `test_a_refused_stream_stops_its_sibling` |
| F8 | low | `net.py:367-412` | A DASH row shows wrong numbers (1100/1100 where 1050 were fetched): both streams share one base and overwrite the total. | Per-stream counts, summed ([machinery](flnd.md#2-generic-machinery-into-shared): `Row.part()`). | `test_dash_row_sums_both_streams` |
| F9 | medium | `repair.py:126-145` | `flnd sync` deletes parts another flnd run is still writing (a scheduled sync during a download): that writer's file never appears, without an error. | A lock per target folder; repair skips a locked folder and keeps parts younger than an hour (`atomic.sweep`, `atomic.lock`). | `test_repair_keeps_parts_of_a_locked_folder` |
| F10 | low | `engine.py:782-791`; `repair.py:110-114` | The first Ctrl-C doesn't stop a repair that is checking files; it takes three. | Check the stop between files; rows not checked stay unstamped. | `test_first_ctrl_c_stops_repair_between_checks` |
| F11 | low | `job.py:943-948` | Two download threads can each make the target's encode pool; one is never ended. | Make it once, up front. | `test_two_videos_at_once_share_one_encode_pool` |

## To check while building

Seen but not shown, so not listed above; whoever builds the module checks each and adds it as a
bug only with a failing test:
- after an interrupted tik run, the "from the newest" pass calls the category on a context whose
  feed is spent, so videos posted since may wait one more run (`job.py:1551-1559`, `tik.py`);
- `TargetSite.cookies` is the login's jar while `prepare` gets live cookies, so ins's CSRF header
  and tik's CDN headers may read a stale value after the site rotates one (`site.py:207-210`);
- `query.py` counts its 8-search limit before removing duplicate searches.
