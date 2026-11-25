# flnd simplification plan

Written 2026-11-25. Paths are relative to the project folder `flnd/`.

## Why

flnd does its job, but it costs too much to change:

| Measure | Now |
| --- | --- |
| Code (`src/` + `flnkit/`) | 42,300 lines |
| Tests (`tests/`) | 36,400 lines |
| `SPEC.md` | 1,500 lines |
| `fix:` commits in November 2026 | 145 (of about 720) |

Most of the weight is not in the site plugins (they are 6,600 lines together and well isolated).
It is in the machinery around them: three ways to drive a browser and read cookies, a sealed
vault, account switching, a third-party plugin system with a public Python API, interactive
terminal apps, and several config and sync special cases in the core.

The aim is one simple, fast tool: **give flnd a cookie file per site, give it targets, it
downloads fast and keeps them in sync.** Everything that does not serve that goes, without
weakening plugins or degrading performance.

## Ground rules for the work

- Remove, don't rewrite. Each step deletes a feature and the code, tests and docs that exist only
  for it. Kept features keep their behaviour and performance unless this plan says otherwise.
- One step at a time, in order. `make check` (including `test_layers`, `test_docs`, `test_bench`,
  and `test_size`) is green at every commit.
- Existing homes keep working: their files, targets, cursors, heads and failures stay. Only the
  login storage changes (see step 3).
- No new features while this runs, and no performance regressions on kept benchmark gates.
- Mechanical steps (pure deletions, moves, doc edits) go to the low-cost model. Steps 3, 4, 8 and
  9 touch behaviour or hot paths and use the default model, with a review before the commit.
- Each step ends by tightening `CODE_LINES` and `TEST_LINES` in `tests/test_size.py` and recording
  the line counts of `src`, `flnkit` and `tests` in its last commit message body.

## What goes

1. **Chrome extension, `flnd extension`, hidden `flnd host`, `ctx.extension`.**
   - Why: A second browser backend: native messaging, a socket relay, a JS extension, its own
     protocol. 3,300 lines with tests.
   - Instead: flnd's own Playwright browser, which every browser plugin already uses.
2. **Login methods `browser`, `chrome`, `firefox`, `safari`; tlg's `browser` method.**
   - Why: Each reads a different browser store; Safari needs Full Disk Access; tlg's copies a Chrome
     profile.
   - Instead: A cookie file (Netscape `cookies.txt` or a JSON export). tlg keeps `qr` and `phone`.
3. **The vault (`secret.db`, AES key in `~/.config/flnd/key`).**
   - Why: With cookie files the user already holds the secret in plain text; the vault only moved
     it.
   - Instead: The cookie file is copied to `~/.config/flnd/logins/`, mode 0600, never inside a home.
     Cookie write-back stays so rotating site cookies keep logins alive.
4. **Several accounts per plugin, account switching, `switch`.**
   - Why: Touches the scheduler, the crawler, the store and auth. Hard to test, rarely needed.
   - Instead: One login per plugin. A block holds the plugin; a refused login fails its targets with
     a hint.
5. **Third-party plugins: `flnd plugin install/uninstall/list`, `$FLND_PLUGINS`, API version checks,
   plugin class validation, `_manifest.py` and `make manifest`.**
   - Why: Built for outside authors who don't exist. The 777-line generated manifest and its
     generator exist only for third-party plugins and eager CLI help.
   - Instead: A ~20-line static table of the 9 built-ins `(code, module, cls, domains)` in
     `src/plugins/__init__.py`, loading only the needed plugin on first use.
6. **The public Python API: `flnd.open` as a promise, `flnd.testing` as public, `make api`, `make
   docs`, `tests/test_names.py` allowlist, the SPEC Python and "Writing a plugin" sections.**
   - Why: A public surface to freeze and snapshot, for no caller but the CLI.
   - Instead: The CLI is the only interface. `_library.py` stays as the CLI's internal facade.
7. **Interactive lists and Textual forms (`textual`): `target list` keys, `auth list` keys,
   `config` editor.**
   - Why: 1,800 lines of Textual UI and tests to do what plain tables and subcommands do.
   - Instead: Plain tables, plus `target follow/unfollow/retry`, `status --fix`. The questions
     (`ctx.ask` for tlg, `auth logout`, `target clean`) keep their Huh look: `src/ui/forms.py`
     already draws it with Rich and raw keys when Textual can't run, and that becomes the only
     path. Edit `config.toml` by hand.
8. **`--json` output and the `Resources` event.**
   - Why: No consumer besides the Python API.
   - Instead: The text output and the run journal.
9. **The `use` line (runtime `psutil` dependency) on the run screen.**
   - Why: Nice to have, adds a runtime dependency and a meter thread.
   - Instead: Nothing on the run screen (`psutil` moves to `dev` only for `tests/bench/mem.py`,
     or `mem.py` switches to stdlib OS tools).
10. **Config keys `height` and `browser`.**
    - Why: Never read by the engine.
    - Instead: Nothing.
11. **Config keys `rate`, `fresh`, `sidecars`.**
    - Why: Special cases in the transfer or sync path, all off by default.
    - Instead: `timeout` and `slowest` (~30 lines) both stay so stalled and trickling CDN
      connections are cut and retried instead of holding download slots.
12. **`dedupe` (hash every same-size file, hard-link twins) and file hashes.**
    - Why: Keys already stop a post being saved twice; hashing costs CPU and I/O, and the
      `files_hash` and `files_size` indexes slow every DB insert.
    - Instead: `check` compares sizes only; cross-category symlinks (`store.LINK`) stay.
13. **Two runs sharing a home (part locks, `BusyError`, `busy` skips).**
    - Why: Many paths for a rare case; per-file part locks also add syscall overhead per download.
    - Instead: One run per home via `<home>/.lock`, taken by `get`, `sync`, `target clean` and
      `status --fix`; a second one stops at once with `another flnd run is using <home>`.
      `target follow/unfollow/retry` and the read-only commands never take it. The run's
      requeue of a commit that meets a locked `flnd.db` (`store.busy`, `BUSY_PAUSE`) stays.
14. **Sync gaps and `stop_after`.**
    - Why: Two extra rules on top of the head, and the source of several sync bugs.
    - Instead: One head rule, see step 9.
15. **Chromium download prompt.**
    - Why: A progress UI around Playwright's installer.
    - Instead: An error: `Google Chrome is needed`, hint `install Chrome, or run uv run playwright
      install chromium`.
16. **`make dist` (PyInstaller one-file binary), `make ext`, `make cdn-real`.**
    - Why: Release paths nobody uses; `uv tool install -e ./flnd` is the install.
    - Instead: Nothing.
17. **Old-home compatibility: v1 database migration, `[sources.*]` alias, gone-key warnings,
    `dedupe = "link"`.**
    - Why: Paths for homes that no longer exist.
    - Instead: Only the v2-to-current migrations from this plan are kept.

## What stays

So the cuts don't hurt plugins or performance:

- All 9 plugins: `ins thr twt tik fbk tlg loi mht web`. Plugins are isolated and cheap; the
  weight is in the engine.
- The Playwright browser with its anti-ban work (human scrolling, headful user agent, WebGL
  names, holds and walls, low process priority inherited from flnd). This keeps test accounts
  alive and keeps runs from starving the machine.
- Full download performance: `curl_cffi` HTTP/2 on the loop, adaptive per-host concurrency,
  per-device writer threads, **dynamic segmented HTTP downloads (`_Split` / `Segments`, 4.46x on
  per-connection-capped CDNs)**, ranged `fetch` segments (tlg), HLS parallel segments, DASH +
  ffmpeg joins, and `slowest` trickle protection.
- Fast CLI startup: `flnd --help` stays under the 60 ms `startup.help` gate, and target
  resolution imports only the one plugin that owns the target's code or domain.
- Cookie refresh write-back (`logins/<code>.txt`), so rotating site cookies extend login life.
- Targets, searches, `--when`, `--limit`, `--dry`, `--folder`, `--again`, categories, folder and
  file name templates, cross-category symlinks (`store.LINK`).
- Following and `sync`, resume cursors for a cut first download, failed-file retries.
- Archive unpacking (loi needs it).
- `proxy`, `pace`, `limits`, `timeout`, `slowest`, `when`, `folder`, `filename`.
- `status` and `check` (missing, wrong size, unknown), `target clean [--media]`.
- The live run screen, the end summary, exit codes, the error hierarchy and hints.
- The run journal, crash files and scrubbed samples.
- The `flnkit/` and `src/` split and the import rules (updated to the modules that remain).
- `make bench` with its gates, `make live`, `make try`, `make test-browser`.

## The new login model

One login per site. No database rows, no keys, no encryption.

```sh
flnd auth login ins ~/Downloads/instagram_cookies.txt   # copies it into the logins folder
flnd auth login tlg                                     # qr on a terminal; `phone` also works
flnd auth list                                          # plugin, status, expiry, file
flnd auth logout ins                                    # deletes the copy, after a question
```

- Cookie logins live at `~/.config/flnd/logins/<code>.txt` (`%APPDATA%\flnd\logins\` on Windows),
  mode 0600, stored as Netscape `cookies.txt` (or normalized JSON).
- Telegram's `session`, `api_id` and `api_hash` live at `logins/tlg.json`, mode 0600, behind a
  ~20-line JSON-backed `ctx.secrets` mapping so `tlg.py` keeps its existing login and connect code
  unchanged (no conflict with Telethon's SQLite `.session` files).
- Logins are per user, shared by every home. A copied home carries no login, as today.
- `login` keeps today's checks: only the plugin's domains are kept, and one of the plugin's login
  cookies must be there. A cookie value is never printed, logged or put in an error.
- `login` also refuses a file whose login cookies have all expired: `<code>: the login in <file>
  has expired`, hint `export a fresh cookie file`. Today such a file is saved and fails on the
  next run. The `logins/` folder is made with mode 0700.
- A run warns when a login cookie has expired or expires within 3 days. When a site refreshes an
  existing cookie during a run, `write_back` (~35 lines) atomically updates `logins/<code>.txt`
  (mode 0600) so rotating sessions stay alive.
- A refused login (401 or 403 on the plugin's domains, a declared wall after its holds) fails that
  plugin's targets with the hint `export a fresh cookie file, then flnd auth login <code> <file>`.
- A block holds the plugin (30 s, 2 min, 8 min, then the target fails), as it holds an account
  today.

## Steps

Each step is one or a few commits. The commit messages are the ones to use.

### Step 0. Baseline (default model, no code change)

Record: line counts, `make check` time, `make bench` numbers, `flnd --help` time,
`flnd get --help` time. Keep them in this file under "Numbers".

Done on main: 6b79ff2.

### Step 1. Remove the Chrome extension (low-cost model)

Done on main: db3343e.

`refactor:remove the chrome extension and its host`

- Delete `src/extension/`, `src/engine/extension.py`, `src/engine/chrome.py`, the `extension` and
  `host` commands, `Ctx.extension`, the `ExtensionTabs`/`ExtensionFeed` branches in
  `src/engine/ctx.py`, the `flnd.extension` package data in `pyproject.toml`, `make ext`.
- Delete `tests/test_extension.py`, `tests/test_chrome.py`, `tests/ext.py`.
- Remove the `EXTENSION` (`src/extension/manifest.json`) check from `tests/plugin_manifest.py` and
  `tests/test_manifest.py` so `make check` stays green.
- SPEC: remove "The Chrome extension" and the `chrome` login method; update any module docstrings
  that pointed to that section.

### Step 2. Cookie files are the only cookie login (low-cost model)

Done on main: 081741c.

`refactor:log in with a cookie file only`

- `src/plugin.py`: `COOKIE_METHODS = ("file",)`; `flnd auth login <code> <path>` needs no method
  word. Telegram's methods become `("qr", "phone")`.
- `flnkit/cookies.py`: keep `_parse_netscape`, `_parse_json` and serializer for `write_back`;
  delete the Firefox and Safari readers, `parse_binarycookies`, `refuse_profile`.
- `flnkit/browser.py`: delete `_profile_root`, `_copy_profile`, `local_storage`, `refuse_profile`,
  the `profile=` option of `launch`.
- `src/engine/auth.py`: delete `_window` (the `browser` method) and the browser branches of
  `_read_jar`.
- Keep `FLND_TEST_NO_PROFILES` and `tests/no_profiles.py`! They still guard `~/.config/flnd/`
  (`config.py`, `auth.KEYFILE` until step 3, `$FLND_PLUGINS` until step 4, and `logins/` from
  step 3 onward). Only remove the browser-profile guards and `tests/test_safari.py`, plus the
  removed method tests in `tests/test_auth.py`, `tests/test_browser.py` and `tests/test_cookies.py`.

### Step 3. One login per plugin, no vault (default model, reviewed)

Done on main: 755d2c0.

`feat:keep one cookie file per plugin in the logins folder`, then
`refactor:remove the vault and account switching`

- New: `logins/<code>.txt` and `logins/tlg.json` (mode 0600) as above, with `write_back` updating
  `logins/<code>.txt` atomically on cookie refresh. `auth list` reads the folder.
- Guard `logins/` in `src/engine/auth.py` (like `config._guard`) and redirect `auth.LOGINS_DIR` to
  a temp folder in `tests/no_profiles.py` so tests can never touch the user's real logins.
- Delete `flnkit/vault.py`, `tests/test_vault.py`, `Vault`/`secrets`/`seal_plaintext`/`_key` in
  `src/engine/auth.py`, `Switcher` and `settle`, the switch paths in `src/engine/crawl.py`
  (`_switch_work`, `_try_switch`, `_with_switch`), tried-account tracking in `src/engine/jobs.py`,
  the `switch` config key.
- Holds move from per account to per plugin (the scheduler's hold key changes; the hold rounds do
  not).
- Database version 3: copy `metadata/flnd.v2.db`, drop the `accounts` table and `targets.account`
  column (`DROP TABLE IF EXISTS accounts; ALTER TABLE targets DROP COLUMN account`).
- An old home's `metadata/secret.db` is left in place and ignored; the first run on such a home
  prints once: `logins are cookie files now: flnd auth login <code> <file>`. A Telegram login has
  to be made again (`flnd auth login tlg`).

### Step 4. Built-in plugins only, zero startup regression (default model, reviewed)

Done on main: 56e2993.

`refactor:load only the built-in plugins`

- `src/plugins/__init__.py`: replace `_manifest.py` with a ~20-line static registry table of the 9
  built-ins: `(code, module, cls, domains)` (with `web` last as the catch-all). `flnd get <code>:…`
  and `flnd get https://<domain>/…` look up the single matching plugin and import only that one.
- `src/cli.py`: make `sub.tail` lazy (a callable evaluated only inside `format_help()`), so
  `flnd --help` and normal commands never import plugins; only `flnd get --help` imports them to
  print the plugin forms table.
- `src/engine/plugins.py` (809 lines) collapses into a small loader over `flnd.plugins.PLUGINS`.
- Delete `src/plugins/_manifest.py`, `tests/plugin_manifest.py`, `tests/test_manifest.py`,
  `make manifest`, `flnd plugin`, `$FLND_PLUGINS`, `install`, `uninstall`, `disabled`, and the
  `FLND_PLUGINS` line in `tests/no_profiles.py`.
- `src/plugin.py`: delete the API version check and the `_check_*` class validation. One test
  checks the 9 built-ins' declarations instead.
- Update `tests/test_layers.py` (allow `cli.py` and `engine/plugins.py` to import `flnd.plugins`
  instead of `_manifest`), `tests/live.py`, and tests that imported `_manifest`.
- Gate: `make bench ARGS="startup --gate"` — `startup.help` stays under 60 ms (no regression from
  the ~39 ms baseline).

### Step 5. No public Python API (low-cost model)

Done on main: ffb8ee7.

`refactor:make the cli the only interface`

- `src/__init__.py` keeps `__version__` and nothing else public; `src/testing.py` moves to
  `tests/kit.py`.
- Update internal callers of `flnd.open` / `flnd.Error` to import `flnd._library` and
  `flnd.errors` directly: `src/cli.py`, `tests/live.py`, and unit tests.
- Update `Makefile` `try` recipe to invoke the CLI (`$(RUN) flnd --home .try get --dry $(ARGS)`).
- Update `tests/test_layers.py` Rule 6 so `src/cli.py` may import `flnd._library` and
  `flnd.errors` (and drop the `flnd.testing` rule).
- Delete `tests/api_snapshot.py`, `tests/data/api.txt`, `tests/test_api.py`,
  `tests/test_names.py`, `make api`, `make docs`, `pdoc`.
- Update `tests/test_docs.py` (remove `api_snapshot` import and public-surface checks, drop
  Python-API blocks from `PageTest`, fix or remove `>>>` doctests that imported `flnd.open`, and
  update `src/__init__.py` / `src/plugin.py` docstrings to point to remaining `SPEC.md` sections).
- Update `tests/test_packaging.py` (drop lazy public re-export checks).
- SPEC: remove the Python and "Writing a plugin" sections; keep a short "Adding a built-in plugin"
  note under "For developers".
- AGENTS.md: drop the rules about the public surface, `test_names` and `make api`.

### Step 6. Plain output, Huh-look prompts without Textual (sonnet)

Done on main: 52f805b, d78aba2.

`refactor:replace the interactive lists with plain tables and subcommands`, then
`refactor:remove json output and the use line`

- Delete `src/ui/lists.py`, `src/engine/meter.py`, `tests/test_lists.py`, `tests/test_forms.py`.
- Keep `src/ui/forms.py` and its look (Charm's Huh: the title, the `>` pick, the hint row).
  Its plain stand-in (`_plain`, `_frame`, `_keys`: Rich frames, raw keys) already draws the same
  owners without Textual off the main thread. Make it the only path: delete `_app`, `_show`
  and the Textual widgets, keep `select`, `text` (with `secret`), `confirm`, `ask`, `choose`.
- `_plain` uses `termios`, which Windows lacks. Add a Windows branch: keys from
  `msvcrt.getwch()` mapped to the same names (arrows arrive as `\xe0` + a code). Off a terminal
  the questions stay refused as today (`_needed`).
- Rewrite `tests/test_forms.py` to drive `_plain` through fake keys (no Textual pilot).
- Drop `textual` and `tomlkit` from `pyproject.toml` (reading uses stdlib `tomllib`; delete
  `config.write`/`config.unset` and `fl.config.set`/`unset`).
- Move `psutil` from runtime dependencies to `[dependency-groups] dev` (used only by
  `tests/bench/mem.py`). `cli._lower_priority` already lowers flnd's priority before Chromium
  starts, and children should inherit it, so `flnkit/browser.py`'s `_lower_priority()` and its
  `psutil` test in `tests/test_browser.py` can go. Confirm on Windows first (Playwright starts
  Node, then Chromium, which may set its own priority back to normal). If the browser
  processes end up at normal priority there, keep a small `ctypes` version for Windows instead.
- New subcommands, each a few lines over existing library calls: `flnd target follow <target>`,
  `flnd target unfollow <target>`, `flnd target retry <target>`, `flnd status --fix`.
  `flnd config` prints each key, value, default and the file to edit.
- Delete `--json`, the `Resources` event and the JSON error lines.
- Update `tests/bench/ui.py` and `tests/bench/gates.json` to drop `ui.open` (which benchmarked
  `lists.py`) so `tests/test_bench.py` stays green.

### Step 7. Config and file cuts (low-cost model)

Done on main: ec8864a, 3daadba.

`refactor:remove unused and rarely used settings`, then `refactor:drop dedupe and file hashes`

- Delete the keys `height`, `browser`, `rate`, `fresh`, `sidecars`, `dedupe`, and the code each
  one guards (the rate bucket, `_prune_fresh`, the sidecar writer, the hash and hard-link dedupe
  step in `src/engine/save.py`, `Kind.HASH`, `files.digest` and `files.link`). Keep `slowest`
  (`Speed` in `jobs.py`, ~30 lines) and keep cross-category symlinks (`store.LINK` in `crawl.py`).
- An old `config.toml` that still has a removed key gets a one-line warning, `<key> is gone`, from
  a plain list of gone names (no per-key upgrade rules).
- Delete the v1 database migration, the `[sources.*]` alias and the `dedupe = "link"` mapping.
- Delete leftovers of the removed `exec` setting: `Kind.EXEC`, its pool and its special cases in
  `src/engine/jobs.py`, and `_without_exec` in `src/engine/config.py`.
- Database version 4 (incremental migration from v3, and v2->v4 in one pass): first
  `DROP INDEX IF EXISTS files_hash; DROP INDEX IF EXISTS files_size;` (required by SQLite before
  dropping an indexed column, and also speeds up `INSERT INTO files`), then
  `ALTER TABLE files DROP COLUMN hash; ALTER TABLE files DROP COLUMN sidecar;`.
- Update `tests/bench/net.py` (`big_4_at_once` no longer calls `saver._hash`; remove `dedupe=`/
  `sidecars=` kwargs from `net.py` and `mem.py`), `tests/bench/db.py` (drop `hash` column), and
  `tests/bench/gates.json` so `make check` and `make bench` stay green.

### Step 8. One writer per home (default model, reviewed, bench-gated)

Done on main: d3f3642.

`refactor:allow one writer per home and drop part locks`

- `get`, `sync`, `target clean` (with or without `--media`) and `status --fix` take
  `<home>/.lock`. A second one stops at once: `another flnd run is using <home>`. `clean` and
  `--fix` rewrite rows a run may be landing, so they must not overlap one.
- `target follow/unfollow/retry` never take it: each is one tiny update that waits on SQLite's
  `busy_timeout` (30 s on non-loop connections), so you can still follow a target while a sync
  runs. Read-only commands (`status`, `target list`, `auth list`, `config`) never take it.
- Keep the run's side of that: the loop's connection has `busy_timeout = 0` so a commit never
  blocks the loop, and a commit that meets a lock is queued again (`store.busy`, `jobs.Busy`,
  `BUSY_PAUSE`). A loop `busy_timeout` instead would block the loop, against AGENTS.md.
- Delete per-file part locks (`fcntl`/`msvcrt` in `flnkit/files.py`), `BusyError` and the
  `busy` skip. Removing per-file locks should make `disk.part_write` faster.
- Test: a `target follow` during a run's commit lands, and the run goes on without an error.
- Keep HTTP dynamic segmented downloads (`_Split` and `Segments`) and `File(ranged=True)` (tlg)
  so throttled CDNs keep their 4.46x `net.per_conn_limit` throughput.
- Keep `e2e.db_contended` in `tests/bench/`: a short write holding `flnd.db` during a run is
  still allowed, so its requeue path stays measured.
- Gate: `make bench ARGS=--gate`. No kept gate may regress over 15%.

### Step 9. One sync rule (default model, reviewed)

Done on main: e93b5a8.

`refactor:stop a sync at the head id or an older timestamp`

Before, a newest-first listing stopped at its head, after 5 saved posts in a row (`stop_after`),
or at a gap left by a cut-short sync. As built, one rule:

- Ins and thr reposts are read to the end (`newest_first=False`, like twt's): a repost carries
  the original post's old time, not the repost time.
- A newest-first category keeps a head, `{"id": post_id, "ts": time_or_day}`: the newest post of
  the completed pass by time (ties: the first seen; a post marked `pinned` is left out; when no
  post has a time, the first post's id).
- A post is old when its id is the head's, or its time is strictly older than the head's (never
  equal, so posts of the head's second or day are never skipped). A post with no time is never
  old by time.
- A sync stops after 4 old posts in a row. A new post resets the count, and a pinned post is
  skipped. The count of 4 is above every site's pin limit, so pins that are not marked never
  stop a sync early.
- A head with no time (an older home kept the bare id) takes the time of the head post when the
  pass meets it.
- Newest-first discovering routes (`twt?query`, `loi:author/{id}`) yield `Target`s with no dates:
  they keep ID-based head matching (`child_key == head_id`) and keep `ctx.listing.head`.
- The head moves only when a pass reaches the old head or the end. A cut-short sync leaves the
  head where it was, so the next sync reads down to it again; saved files are skipped without a
  request.
- A full first download still keeps its cursor and resumes there.
- Delete `gaps`, `stop_after`, `targets.gaps` (schema version 5: `ALTER TABLE targets DROP COLUMN
  gaps`), and their tests.
- In the same version 5, drop `targets.seen`: `target_seen` replaced it, and the store only still
  writes the old JSON copy. Remove that write; the v2 migration's `_backfill_seen` runs first.
- Rewrite `tests/test_smart_sync.py` around the rule: new posts, same-day posts, a cut sync, a
  deleted head, a pinned post, discovering query heads, `--when`, `--again`.

### Step 10. Small cuts (low-cost model)

Done on main: 9dcffb8, 0e13c7b.

`refactor:remove the chromium download and the one-file build`

- Delete `_install_chromium` and the download question; a missing Chrome is a `ToolError` with
  the hint in "What goes" item 15.
- With the question gone, `browser.Screen`, `_Bar` and `ctx._Screen` have no use left (check
  that nothing else calls `screen.confirm` or its bar first); delete them and `launch`'s
  `screen=` option.
- Delete `make dist`, `tests/dist.py`, the `dist` dependency group, `make cdn-real`.

### Step 11. Tidy pass (default model reads, low-cost model edits)

One commit per module: `refactor:tidy <module> after the cuts`

- Read each remaining module for code that only the removed features used: options with one
  value left, branches that can't run, helpers with one caller. `ruff` finds unused imports;
  the rest is reading.
- Merge leftovers where a module became a few dozen lines.
- Rename `tests/no_profiles.py` to `tests/guard.py` (with `FLND_TEST_NO_USER`) if clearer once
  browser profiles are gone.
- Update `AGENTS.md` (layout tree, import rules, test helpers), `SPEC.md`, `COOKBOOK.md`,
  `README.md`, and run `make bench ARGS=--record` to record the post-simplification baselines.
- Under "Numbers", list each gate still short of its target (today: `net.small_8_at_once`,
  `ui.overhead`, `e2e.sync_100_targets`, `browser.warm_4`, and others). Record them; don't chase
  them here. They go to the follow-up below.

### Step 12. Hand live check (default model)

Per the privacy rules: on the test accounts, by hand, never in unit tests. For each cookie plugin,
`flnd auth login <code> <file>`, then `flnd get <target> --dry --limit 5`; tlg by `qr`. Also
verify cookie `write_back` on a site that rotates cookies. Record pass or fail per plugin in this
file.

## Follow-up, after this plan

Not part of this plan, because each one changes how flnd behaves or how fast it is, rather than
deleting something. Each gets its own plan once step 12 passes:

- **Small-file speed.** `net.small_8_at_once` is 595 ms against a 300 ms target. Profile the
  per-file work in `src/engine/save.py` and `flnkit/files.py` after step 8 drops the part locks.
  This is the biggest speed win left for image-heavy sites (ins, twt, mht).
- **One browser recovery path.** fbk, twt, ins, tik and thr each have their own reload and
  stall loops, and most of November's browser fixes were there. Moving them into one verb in
  `flnkit/browser.py` and `src/engine/ctx.py` would cut code and bugs, but it is anti-ban
  behaviour: it needs the live check on every browser plugin.

Splitting the big modules (`browser.py`, `crawl.py`, ...) into smaller files is left out on
purpose. It moves lines without removing any; step 11 merges or splits only where a cut leaves an
obvious seam.

## After

Commands (all accept `--home DIR` and `-v`/`--verbose`):

```text
flnd get [target...] [--when W] [--limit N] [--dry] [--folder F] [--again]
flnd sync [pattern...] [--when W] [--limit N] [--dry] [--again]
flnd target list | follow T | unfollow T | retry T | clean T... [--media]
flnd auth list | login <code> <file> | login tlg [qr|phone] | logout <code>
flnd status [--fix]
flnd config
```

Config keys: `[defaults] when timeout slowest`; `[plugins.<code>] pace limits proxy folder
filename` and the plugin's own settings.

Dependencies: `curl_cffi playwright selectolax cryptography telethon cryptg qrcode rich msgspec`
(gone from runtime: `textual psutil tomlkit`; `dev` keeps `ruff pyte` and optionally `psutil` for
`tests/bench/mem.py`, drops `pdoc`; the `dist` group goes). Note: `cryptography` stays because
`loi.py` decrypts media blobs with AES-CBC.

Expected performance gains (on top of keeping every speed feature):

- Faster DB inserts: dropping `files_hash` and `files_size` removes two index updates per file.
- Faster disk writes: dropping per-file `flock`/`msvcrt` part locks cuts syscalls per file.
- Zero hashing CPU/disk I/O on same-size files.
- `flnd --help` stays under 60 ms (~39 ms) without the 777-line generated manifest.

Rough size, an estimate to check against at the end:

| | Now | After |
| --- | --- | --- |
| Code | 42,300 | about 30,500 |
| Tests | 36,400 | about 28,000 |
| `SPEC.md` | 1,500 | about 900 |

## Risks

- **One account per plugin means a blocked account stops that site.** Holds still apply; switching
  is the one feature here worth reviving if needed, and it would come back as its own step.
- **Telegram has to log in again after step 3.** Said once on the first run; QR takes a minute.
- **A cut-short sync re-reads down to the head on the next run (no gaps).** Saved files are
  skipped from the DB without HTTP requests, and normal syncs are short.
- **Removing the vault keeps cookies in plain text.** The logins folder is 0600 and outside every
  home, the same protection the vault's key file had.

## Decisions (settled 2026-11-25)

1. **One login per plugin** (item 4). A second account is one `flnd auth login <code> <file>`
   away.
2. **No vault** (item 3). Cookie files sit in plain text, mode 0600, in a 0700 folder outside
   every home.
3. **No `--json` and no Python API** (items 6 and 8). flnd runs from the terminal, shell scripts
   and cron, which read exit codes.
4. **No `rate` or `fresh`** (item 11). `slowest` stays to cut trickling connections. `rate` is
   about 30 lines to bring back if a shared connection ever needs a cap.

## Numbers

Step 0 (1923c7a, 2026-11-25, machine not quiet):

| Measure | Value |
| --- | --- |
| Code / tests / SPEC lines | 42,317 / 36,412 / 1,504 |
| `make check` | 67 s, 1,486 tests (20 skipped) |
| `flnd --help` (warm, through `uv run`) | 0.04 s |
| Gates not met | `db.point_read` 2.39 us (2.2), `e2e.sync_100_targets` 3.93 s (3.84), |
| | `browser.scroll_default` 21.4 s (16), `browser.warm_4` 0.115 s (0.091) |

The other gated numbers met their targets. The full run is kept outside the repo; later steps
compare against a fresh run made back to back, not against these.
