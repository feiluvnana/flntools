# Output

Phase 2. Needs [log](../phase-1/log.md) and [fmt](../phase-1/fmt.md).

| Today | After |
| --- | --- |
| `[info]`, `[browser]`, `[tab:x]`, `[stories]`, `[highlight…]`, `Starting download`, `Destination`, `Browser mode` | `log.info`, without the bracket prefix or leading spaces |
| `[warn]` | `log.warn` |
| `[error]`, `[fail]`, `[!] Error` | `log.error` |
| `Finished. Total…`, `Captcha solved!` | `log.ok` |
| captcha detected | `log.warn` |
| `[skip] Already downloaded` | `log.info("skip {name}", name=...)` |
| `[saved] … (1.5 MB)` and the byte loop in `download_file` | `log.progress(name, total=content_length, show=fmt.bytes)` around the chunk loop; its `ok` line replaces `[saved]` |
| `format_bytes` | `fmt.bytes` |
| `scroll_page_to_end` with no output | an `on_step` callback, called by a `log.progress("scroll …")` spinner |
| `====` banners | `log.info("Storage: {path}", ...)` and `log.ok("Complete. {n} files", ...)` |
| targets one after another | `log.multi("download")` with one row per target |

In `download_file`, a non-200 status raises inside the progress block, so the bar ends at `error`. The function catches the exception outside the block and returns `False` as it does today.
