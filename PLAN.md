# PLAN: the work still ahead

Written 2026-11-17 from the state of `main` at 49ff2dd, and trimmed 2026-11-17 to what is left.
The flncore migration it first planned is done: `flncore/` is the standalone kit, `flnd/_net`,
`_browser`, `_files`, `_loop`, `_tools`, `_unpack`, `_ui`, `_prompt` and `_time` are shims over
it, `tests/test_layers.py` has rule 0, and the removals below have landed. What stands is three
pieces of work: the grouped CLI with its library, the Chrome extension, and source API 2. Each
is one or a few commits, keeps `make check` green, and runs `make bench ARGS=--gate` when it
touches a hot path.

## Done since 49ff2dd

- **The kit.** `flncore/` (log, prompt, cli, run, loop, errors, clock, net, browser, media,
  files, dig, config, testing) imports nothing from flnd; `flnd` keeps its import names through
  shims. `flncore.errors` is the one hierarchy; flnd adds `LoginError` and `SourceError`.
- **Removed:** compress (`_shrink`, Pillow, pyoxipng, `--compress`, the compress lane,
  `make media-real`), live stats (`_stats`, psutil), the full screen and the `ui` setting, the
  per-file progress rows, `examples/`.
- **The display** is the summary table (`job.Board` on `ui.Group`), redrawn at
  `flncore.log.REDRAW` (60 a second) by the renderer's throttle and a ticker thread. A file's
  lifecycle is events only (`flnd._track`); the CLI makes no per-file rows.
- **The journal:** every event and log line goes to one JSON-Lines journal; the tee of the
  terminal into `<home>/log/*.log` is gone.
- **Audit fixes:** the prompt no longer sniffs for a test, the vault's legacy key path is gone,
  a 7z password goes over standard input, a resumed download checks `Content-Range`, tlg's app
  version is flnd's, twt no longer copies a set per tweet, `flncore.log.live` is the public
  read, and the engine bugs of the first draft (the lost gap, the head counted early, the lost
  disk error, `check --fix` on an unmounted drive) are fixed; old homes migrate through the
  schema version in `meta`.

## The CLI and the library

> "flnd get [targets] --all · flnd sync · flnd target list/syncon/syncoff/forget ·
> flnd auth login/logout · flnd status · flnd plugin list/add/remove"
> "avoid options on flnd … only -v and --all and -h are enough" (user, 2026-11-17)

The CLI is still 11 flat verbs over argparse. It becomes annotated functions on
`flncore.cli.App` (cyclopts is already a dependency), and the library mirrors the groups.

```text
flnd get <targets…> [--all]          download what the targets have now (targets also from stdin)
flnd sync [targets…]                 what is new for every followed target, or the ones named
flnd target list [targets…]          the targets this home knows: followed or not, files, size, last run
flnd target follow <targets…>        sync keeps these up to date
flnd target unfollow <targets…>      sync leaves these out; files and history stay
flnd target remove <targets…>        flnd forgets them (files on disk stay); a later get starts from the top
flnd auth list                       every source's login, and where it came from
flnd auth login <source> [how]       how: extension (the default), a cookie file, or the source's own (qr, phone)
flnd auth logout <source>
flnd status                          the last run, its failures, the logins, and the disk against the history
flnd plugin list · install <file> · remove <code>
flnd extension install · remove
flnd config init · set <key> <value> · remove <key>

flags: -v (debug lines, and a bug's traceback) · --all (get only) · -h (help, with the version)
```

- **`flnd -h` fits on one screen** (user: "flnd -h is also verbose"): one line per command, the
  sites on one line, and `flnd get -h` adds one line per site with its categories. Each site's
  target forms live in `flnd/docs/sources/<code>.md`. A test pins the help's length.
- **`--only` goes into the target**: `ins:example_user/reels,posts`. A tab's link reads the
  same way. An unknown category is a `UsageError` naming the ones that exist.
- **`--when`, `--limit`, `--dry`** go to the library (`fl.get(..., when=, limit=, dry=)`);
  `make try` becomes a short script on `dry=True`.
- **`--again`** is replaced by `target remove` then `get`: removing drops the row, heads,
  cursors, gaps and failures, and the next `get` lists everything again, recording files found
  on disk from one `stat`, never downloading them.
- **`--json`** goes: machines read the journal or call the library.
- **Old → new:** `download`→`get`; `follow`→`get` then `target follow`; `unfollow`→
  `target unfollow`; `ls`→`target list`; `retry`→removed (every `get` and `sync` retries
  failed files first; `status` names the ones that ran out of tries); `check`→`status`
  (always checks the disk; repair is `fl.check(fix=True)`); `login`/`logout`→`auth …`;
  `sources`→`plugin list`.
- **`--all`:** without it, `get` takes a target's *own* categories (`own=True` on the route's
  categories); `--all` adds the rest (tagged, reposts, liked). Categories named in the target
  override both. A followed target keeps the categories it was followed with.
- **`remove` is the one verb for "flnd stops keeping this"** (`target remove`, `plugin remove`,
  `extension remove`), with `install` as its pair. It deletes only what flnd itself made.
- **`flnd config init | set | remove`** (tomlkit, already a dependency): `init` writes
  config.toml with every key commented, generated from the settings' structs; `set` writes a
  key in place, checked by the same struct; `remove` re-comments it. Comments survive; the file
  is written atomically. A source declares its own keys (source API 2), and `config` shows them.

The library mirrors the groups (`flnd/_library.py`):

```python
import flnd

with flnd.open() as fl:                          # $FLND_HOME, else ./.flnd
    for event in fl.get("ins:example_user/reels"):
        if isinstance(event, flnd.Saved):
            print(event.path)

    fl.targets.follow("ins:example_user")        # also unfollow, remove; iterating lists it
    fl.auth.login("ins")                         # also logout
    fl.plugins.install("~/exm.py")               # also remove
    fl.extension.install()                       # also remove; reads connected, version, folder
    fl.config.set("dedupe", "off")               # also init, remove

    fl.status                                    # a read is a property
    fl.files(target="ins:example_user", kind="video")
    fl.check(fix=True)
    fl.add(MySource)                             # a source for this process only
```

`get` has 4 keyword options (`all`, `when`, `limit`, `dry`); `sync` has none. Every action
returns something useful (`get`/`sync` a `Run`; `follow`, `unfollow`, `remove` the targets they
changed; `auth.login` the `Login`; `plugins.install` the `Installed`).

## The extension

> "Have a chrome extension named flnd. This extension will become one way to get the cookies
> and do advanced things to the browser that current flnd can't do." (user, 2026-11-17)

**Why:** cookies read the way the browser means them to be read (`chrome.cookies`, HttpOnly
included, no keychain decryption, no Full Disk Access, still working under Chrome's app-bound
encryption on Windows), a real browser with the user's own fingerprint instead of Playwright's,
and a toolbar button that shows the target of the page you are on, with Get and Follow.

- **Two pieces.** `extension/` at the repo root (Manifest V3, plain JavaScript, no build step:
  `manifest.json`, `background.js`, `page.js`, `popup.html`/`popup.js`), and
  `flnd/_extension.py`: the native-messaging host (`flnd host`) and the protocol.
- **`flnd extension install`** copies the files to `~/.config/flnd/extension/`
  (`%APPDATA%\flnd\extension\` on Windows), writes the native host's manifest (`com.flnd.host`)
  for every Chromium browser it finds, then prints the three numbered lines for the one step a
  person must do (Developer mode, Load unpacked, paste the path), and waits with one live line
  until the extension connects. `flnd extension remove` undoes it.
- **The protocol** is JSON messages `{id, op, args}` in, `{id, ok, value | error}` out; the ops
  are one `Site` browser verb each (`cookies`, `open`, `wait`, `scroll`, `feed`, `document`,
  `click`, `close`). The host relays between the extension and running flnd processes over
  `<home>/host.sock` (a named pipe on Windows), which only the user can open.
- **Which browser a run uses:** the extension's, when it is connected; otherwise flnd's own
  Chromium through Playwright, as today. A source whose site walls automation says
  `Browser(real=True)`, and without the extension it fails with the hint
  `flnd extension install`.
- **Security:** Chrome lets only the extension talk to the host; `host_permissions` are the
  built-ins' domains, written from the manifest by `make manifest`; a plugin's domains are
  `optional_host_permissions`, approved in Chrome's dialog; cookie values cross the pipe once
  and go straight into the vault, never into a log. `Site.evaluate` leaves API 2, since MV3
  can't run code from a string.
- **Tests:** the protocol against a fake extension in `make check`; the extension itself in
  `make ext`, loaded unpacked into Playwright's Chromium against `tests/bench/server.py`,
  never in `make check`.

When the extension lands, `flnd auth login ins` goes through it by default, and the
`chrome`, `firefox`, `edge`, `brave`, `chromium` and `safari` hows go away with the Safari
Full Disk Access advice. Until then they stay.

## Source API 2

> "Condense the flnd api to still keep current performance, but increase stability, and
> easier to write. Make it bullet-proof."

`api = 2`. An `api = 1` plugin is refused, with a message that names what to rename.

- **A block is waited out in place, never raised through the listing.** A source declares
  `def blocked(self, answer) -> float | None` (the seconds to wait); `fetch`, `open` and `feed`
  call it inside the client's retry, so the request is waited out where it stands. It absorbs
  ins's `THROTTLED`, loi's refusal, fbk's `BLOCKED` and `COMET_ERROR`, tik's captcha. The round
  waits (30 / 120 / 480 s) and the fourth-round raise move into the net policy. This replaces
  the sources' `site.back_off` calls.
- **Declared login walls:** `walls = ("/accounts/login", "/challenge")`; `open` raises
  `LoginError` itself when it lands on one. Replaces fbk's `_on_login`, ins's `is_wall` and
  twt's `WALLS`.
- **Pre-loading is declared:** `@route.category(url="{profile}/reels")` pre-loads the next
  wanted category's page. Today `site.warm` pre-loads every later tab whatever the categories
  asked. Per-item pre-loads (ins's highlights) are dropped. Measured live before and after.
- **`Site` has 16 members**: HTTP `fetch`, `pages`; browser `open` (returns the URL it landed
  on), `feed`, `document`, `click`, `handoff`; run `target`, `time`, `settings`, `cookies`
  (of the source's own domains only), `state`, `secrets`, `client`; talking
  `log(level, message, **fields)` and `ask`. Gone: `back_off`, `warm`, `pause`, `url`, `html`,
  and `wait`, `scroll`, `evaluate`, `tab`, which no built-in calls.
- **Shared helpers instead of copies:** `page.meta()` (the `__bbox` results fbk and ins copy),
  `site.load(url, match, scroll=)` (about ten copies), `page_info(path)` (a cursor pair),
  `Post(date=, listed=)` (separates the listing date from the naming date, deleting ins's own
  file naming), a file stem whose extension comes from the content, and a resumable part for a
  `Fetch` function (deleting tlg's own part code).
- **A source declares its own settings**, so `flnd config` shows and checks them like flnd's:

  ```python
  class Instagram(Source):
      class Settings(source.Settings):
          """Instagram's settings.

          Attributes:
              covers: also save each highlight's cover image.
          """
          covers: bool = False
  ```

  `source.Settings` is a frozen msgspec struct that already holds `proxy`. A field without a
  default or an `Attributes:` line is a `SourceError` when the class is made. The values reach
  the source typed, as `site.settings.covers`.
- **Declarations:** `stealth` applied per tab by an init script, so twt's `stealth = False`
  finally works; `user_agent` goes (it contradicts curl_cffi's fingerprint); `scroll` goes (no
  built-in uses it); categories get `own=True|False` and `url=`.
- **Mistakes fail when the class is made**, as today; the checks that need posts at run time go
  into `flnd.testing.check` and a run warning (a category listed by date without
  `newest_first`, a template field the posts never fill).
- **Errors in single-item routes are no longer swallowed**: tik's embed and twt's tweet routes
  turn a 404 into a warning today, so nothing is recorded gone or failed; their tests pin it
  and change with the fix.

## Open, small

Found in the same audit, still true, each small enough to land alone:

- `flnd/cli.py` duck-types its library: about 15 `getattr`/`hasattr` calls on types it knows.
  Each becomes a plain attribute access (most go when the CLI is rewritten).
- `flnd/engine.py` still calls the private `ui._progress_enabled()`; it reads `log.live`.
- About 11 broad `except Exception` blocks are left in `flnd/`: each either names the errors it
  expects, or logs at debug with a comment saying why it is safe.
- The first draft's remaining bug list was verified against today's tree; every entry it named
  is fixed or decided otherwise (old homes migrate, `retry` and `check` stay commands,
  `--exec`, link-following and the third Ctrl-C are kept). Re-check any new finding the same
  way: reproduce first, then fix with its test.
