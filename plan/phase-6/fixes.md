# fixes

Phase 6, step 1: the bugs the 2026-11-12 audit found (and one from the [gallery-dl comparison](flows.md)), fixed before anything is reshaped, each with a test that fails before its fix. On the audit day all 803 tests passed (5 skipped) and pyright reported 0 errors, so none of these was caught.

## Windows

1. **Every download is corrupted on Windows.** `net._open_part` opens the `.part` file with `os.open(…, O_WRONLY | O_CREAT | O_EXCL)`. Without `O_BINARY`, Windows' C runtime opens it in text mode, and `fdopen(…, "wb")` then writes every `\n` byte as `\r\n`. Fix: add `getattr(os, "O_BINARY", 0)` to the flags. The `mkstemp` paths elsewhere are already binary. Test: a download of bytes containing `\n` comes out byte for byte, with `os.O_BINARY` patched in to prove the flag is passed.
2. **The vault key can't be written on FAT, exFAT or some network drives.** `vault.key` publishes the new key with `os.link`, which those file systems refuse with a plain `OSError`, so `flnd login tlg` fails there. Fix: open the key file with `O_CREAT | O_EXCL` directly and write it, which gives the same "first writer wins" rule without a hard link. `login.LEGACY_DIRS` (`E:/…`, read relative to the working directory on macOS and Linux) goes.
3. **`archive` writes NTFS alternate data streams.** `_safe_name` refuses a drive letter only at the start of an entry name, so `x/a:b` writes the stream `b` of file `a`. It also lets `CON`, `NUL`, `AUX`, `PRN`, `COM1`–`9` and `LPT1`–`9` through, which then fail. flnd no longer unpacks, but `archive` stays in `shared`. Fix: refuse `:` anywhere and those names as any path part, on every platform, as `UnsafeError`.

## Exit status

4. **`flnd download` and `flnd sync` always exit 0**, even when every target failed, so a cron job or a script can't tell. Fix: they exit 1 when any target failed. A rate-limit wait isn't a failure, and a skipped file isn't either.

## Failed groups

5. **A source that can't start shows its targets as ok.** In `Engine.run`, when a whole group fails (`connect` raises, such as tlg's "saved login no longer works", or `logged_in` raises), only `log.error` runs. The group's rows are never failed, and `log.multi` ends unfinished rows as ok. Fix: the group's unfinished rows end as failed, with the error, and count as failed targets for the exit status.

## Twitter headers

6. **twt sends its login to the CDNs, and a stale CSRF token to the API.** `Twitter.headers` puts a static `cookie` header, `authorization` and `x-csrf-token` into the client's defaults, so `auth_token` goes to `pbs.twimg.com` and `video.twimg.com` with every image and video. The fixed `cookie` header also overrides the jar, so a `ct0` that x.com refreshes with `Set-Cookie` is never sent, and the API answers 403 once the old one expires. Fix, in `prepare` once [source](source.md) replaces `headers`: drop the `cookie` header (the jar already sends x.com's cookies), and send the API headers only to the source's `domains` ([api](api.md#source-flndsourcesbasepy)). `x-csrf-token` is read from the jar on each request.

## Media errors

7. **`flnd sync` and `--compress-*` crash on a stalled ffmpeg or a missing extra.** `TimeoutError` (`_ffmpeg` stopping a stalled run, or a slow probe) and `ImportError` are caught neither in `session.compress` nor in `sync._compress`, so a whole run ends in a traceback. `sync._broken` already catches `TimeoutError`; the two places that compress don't. Fix: one helper in `flnd/compress.py` catches what `media` documents (`ValueError`, `RuntimeError`, `TimeoutError`, `ImportError`), records the file as not compressed, and warns once.
8. **One photo's `RuntimeError` turns off video compression for the rest of a sync.** `sync._compress` sets "videos off" for any `RuntimeError`, whatever kind of file raised it. Fix: only a video's missing ffmpeg or encoder turns videos off.
9. **A login wall is downloaded three times.** `net.download` retries `ValueError`, so `NotMediaError` (an HTML page where a photo should be) and `media.UnsafeError` download the same page three times before failing. Fix: those two fail at once; `IncompleteError` and connection errors still retry. A mirror that fails now logs why, not only "mirror 2 failed".
10. **`media` validation is written twice.** `net._as_media` and `sync._repair` / `sync._broken` each sniff and check a file, and catch different exceptions. They become one `files.checked(path, kind)` in `flnd/files.py`, used by both.

## Sync

11. **`flnd sync` resolves every target twice**, in `cli.sync` and again in `sync.plan`, so short links make two HEAD requests each. Fix: `plan` takes the resolved targets.
12. **`sync` scans the whole history three times per target** (repair, compress and the tally each call `_rows`, which reads and resolves every row). With 100 targets and 100,000 rows that is 30 million row reads. Fix: index the history by target folder once per run.
13. **Repair leaves some temporary files behind.** It removes `.*.part` only. `net`'s `.<name>.<hex>.merge.mp4`, `session.write`'s and `vault`'s `mkstemp` files stay. Fix: every temporary file flnd writes is named `.<name>.<random>.part`, which repair already removes.

## Profile copies

14. **A copy of the Chrome profile can be left in the temp folder.** `browser.installed_profile` copies the whole profile, including Telegram Web's login in its local storage, and the copy is deleted only once the browser launches. If Playwright can't be imported, or Ctrl-C comes between the copy and the launch, the copy stays. Fix: `connect` deletes it in a `finally` whenever the launch doesn't happen, and the copy is made in a folder with mode 0700.

## Dates

15. **TikTok dates depend on the machine's timezone.** `tik.py` uses `datetime.fromtimestamp` (local time), so the same video gets a different file name, and falls on a different side of `--since`, on two machines. twt and tlg use UTC. Fix: UTC, as they do. ins's `taken_on` gets the same fix in [ins](ins.md).

## TikTok quality

16. **TikTok videos aren't saved at their best, and some carry a watermark.** `tik.best_video` takes `playAddr` first, then `downloadAddr` (TikTok's watermarked copy), and the highest-bitrate file of `bitrateInfo` only last. Fix: `bitrateInfo` sorted by width × height, then `Bitrate`, skipping DASH entries; then `playAddr`; never `downloadAddr`, which is how gallery-dl picks ([flows](flows.md#what-phase-6-takes)).

## Tests

Each fix above has a test named for it in the module's test file. Fix 1 runs on every platform by patching `os.O_BINARY`. Fix 4's test runs `flnd download` with one target that fails and checks the exit status is 1.
