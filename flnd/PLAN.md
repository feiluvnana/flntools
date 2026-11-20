# Plan: hard targets, `me` targets, per-category folders, one cookbook, rate limits

Written 2026-11-20, after the Instagram and Twitter/X overhaul (251de85, 01bf5be, d8a2c7f) and
`--folder` (61a0fc3). Built by worker models, reviewed by Opus (see "How to build").

## How to build (read first)

- **One step at a time**, in the order below. A step is one or more commits. `make check` is
  green at every commit.
- **Workers.** Each step names its model. `haiku` does mechanical work (moves, renames, call
  sites, doc text that follows this plan). `sonnet` does new behaviour, tests and site code.
- **Worker brief.** Give the worker only: this plan's section for its step, the files it
  names, and `flnd/AGENTS.md`. No other exploring. It ends with `make check` green and a
  commit made with `git commit -- <its paths>`, then reports the commit hash, the changed line
  counts (`git diff --stat`) and anything it could not do.
- **Opus review.** Opus reads only `git show <hash>` and the step's section. It checks the
  "Done when" list, the regression guard and the line counts. It sends one list of fixes back
  to the same worker, at most twice; then Opus fixes it itself. No review of the whole tree.
- **Regression guard for `ins` and `twt`.** Before a step touches either plugin, run
  `make test ARGS="-k 'ins or twt'"` and keep the count. After it, the same tests pass, and
  no existing assertion in `tests/test_ins.py` or `tests/test_twt.py` was deleted or weakened
  (Opus checks the diff of those two files line by line). A test may be moved, never dropped.
- **Size.** Every step reports its net code lines and keeps its growth small. No shrink step
  (dropped by the user, 2026-11-20).
- **Live checks** are done by hand on the test accounts after a step lands, never in tests.
  A site detail this plan marks "confirm live" is built against a faked answer first.
- Push only when the user says.

## Where things stand

- `fbk`: `user` (profiles and pages alike); single `video`, `watch`, `photo` and permalink
  `post`. Refuses `/<owner>/posts/<id>`. Comet's block page and feed line are held. A 429 from a
  feed is `Blocked` but not caught, so the tab starts again. Tabs are scrolled, never replayed.
- `ins`: `user`, single `post` (p, reel, reels, tv), `ins:me` (users it follows). In-page REST,
  replayed GraphQL, "please wait" holds, a 2 s pace.
- `twt`: `user`, single `post`, `twt?<query>`. A bucket per operation, 429 holds with `reset`,
  search windows by `max_id`. Refuses `x.com/search?q=` links.
- `tik`: `user`, single `video` (video and photo). Captcha handed off, then held. No 429
  handling. The post list is scrolled, never replayed. No query.
- `loi`: single `post` (`archive`, `photo`). JSON "slow down" held, VIP quota skipped. No query.
- `mht`, `tlg`, `web`: left as they are, apart from the target syntax rule below.

A file's history key is plugin-wide (`<code>:<key>`), so a post saved by its link and later by
its profile is saved once. Only its folder differs; step 3 fixes that.

## The target grammar (every plugin)

A target is written one of three ways, and each plugin accepts every way that fits its kinds:

| form | meaning | example |
| --- | --- | --- |
| a link | an accepted link, with or without `https://` and `www.` | `instagram.com/p/C1abc` |
| `<plugin>:<value>` | a user, `me`, or `<kind>/<id>` | `ins:example_user`, `ins:post/C1abc` |
| `<plugin>?<query>` | a search, on plugins with one | `tik?cats`, `twt?from:example_handle` |

- Then an optional category list after `|`: `<target>|<cat>[,<cat>...]`, or `|all` (settled
  2026-11-20 by the user; `/` is no longer a category separator). Examples:
  `ins:example_user|tagged,reposts`, `instagram.com/example_user|all`. `|` is the shell's
  pipe, so the cookbook quotes every such target: `flnd get "ins:example_user|tagged"`.
- On a query, the text after the last `|` is a category list only when every name in it is a
  category of that plugin or `all`; otherwise the `|` is part of the query. No other case.
- With no list, only the owned categories (`own=True`) are taken, as the code does now
  (settled by the user). Other people's are opt-in: `fbk` `tagged`; `ins` `tagged`,
  `reposts`; `twt` `reposts`; `tik` `reposts`, `liked`, `favorites`. A target found by `me` or
  a query takes its own owned set. The cookbook lists each plugin's owned and opt-in ones.
  The `|` list is the one way to choose categories (decision 2).
- A single post is `<plugin>:<kind>/<id>`, where `<kind>` is the name of a single route, as
  `mht:chapter/<id>` is today (settled by the user). The forms:

  | plugin | forms |
  | --- | --- |
  | `fbk` | `fbk:video/<id>` (and reels), `fbk:photo/<id>`, `fbk:post/<owner id>_<story id>` |
  | `ins` | `ins:post/<code>` (posts, reels, tv) |
  | `twt` | `twt:post/<id>` |
  | `tik` | `tik:post/<id>` (videos and photo posts; the route `video` is renamed `post`) |
  | `loi` | `loi:post/<id>` |
  | `thr` | `thr:post/<code>` |
  | `mht` | `mht:chapter/<id>`, as now |

  A user name never holds `/`, so `ins:post` alone is still the user `post`. `fb.watch` and
  share links stay links only: their id is known only after following them. The old
  `tik:<id>` and `loi:<id>` stay readable, since stored targets use them, but the cookbook
  shows only the new forms. No other words (`reel/`, `video/` on `ins`): one form per kind.
- `me` is reserved after `<plugin>:`. A user really named `me` is written as their link.
- Any other text of a plugin's own domains is refused before any request (`UsageError`, exit 2)
  with a hint naming the right form.
- Stored targets (`targets` table) must keep resolving. If a value changes form, step 4's
  worker adds a store migration and a test that an old row still syncs.
- **Key clash (found 2026-11-20 by probing the resolver).** A single post's key is
  `<code>:<id>`, the same shape as a user's: `instagram.com/p/C1abc` is `ins:C1abc`, the user
  `C1abc` too; `facebook.com/reel/123` and `profile.php?id=123` are both `fbk:123`; a tweet
  `twt:123` and a numeric handle too. They share one `targets` row. Fix in step 4 first: a
  single post's key is its written form, `ins:post/C1abc`, so the key is what you type. A
  store migration rewrites old rows by their `kind` column; a test per plugin.
- **A query needs a folder** (settled 2026-11-20 by the user). A query target, written as
  `<plugin>?<query>` or as a search link, on every plugin with one (`twt`, `tik`, `loi`,
  `mht`), is refused before any request unless `--folder` (or `folder=`) is given and places
  every category: one template, or a list with `*`. `[plugins.<code>] folder` in
  `config.toml` doesn't count. The error: `<code>?<query>: a search needs --folder`, with a
  hint showing `--folder "{query}"`. Examples: `--folder "{query}"` and
  `--folder "posts:{query}/posts,*:{query}/other"` pass; `--folder "posts:{query}/posts"` is
  refused (`replies` would fall back to their owners' folders), as is no `--folder`. The
  `<cat>:<path>[,...]` list stays allowed here (settled by the user).
  Its hits all go there, not to their owners' folders
  (step 3 is for links only). A followed query stores its folder in its `targets` row (a
  store migration), so a plain `flnd get` sync reuses it, and a different `--folder` on a
  later run is refused unless `--again` is given.
- Already working, per the probe, so step 4 only adds tests for them: tweet links with
  `/photo/<n>`, `/i/web/status/`, `mobile.` hosts; `m.facebook.com`; Instagram `/reel/`.

Accepted kinds per plugin:

| plugin | user | `me` | single post | query |
| --- | --- | --- | --- | --- |
| `fbk` | profile or page | follows (new) | yes | no |
| `ins` | yes | follows (kept) | yes | no |
| `twt` | yes | follows (new) | yes | yes |
| `tik` | yes | follows (new) | yes | yes (new) |
| `loi` | no | no | yes | yes (new) |
| `thr` (new, step 5b) | yes | no | yes | no |

`me` needs a login. It discovers user targets, one per followed account, as `ins:me` does now.
`--limit N` takes the first N.

## Step 1: one cookbook (sonnet writes, haiku moves text)

- `src/docs/` is deleted. Two pages remain, both short: `flnd/README.md` (at most 40 lines:
  what flnd is, install, three example commands, a link to the cookbook) and
  `flnd/COOKBOOK.md` (at most 1,500 lines).
- COOKBOOK.md says everything about flnd, with no gray area. Every grammar is given in full,
  with its accepted forms, its refused forms and the exact error a refusal prints:
  1. **Install and first run.**
  2. **Targets:** the grammar above; every plugin's accepted forms and refused forms in one
     table each; category lists; `me`; queries and `--limit`.
  3. **Dates (`--when`):** year, month, day, `a..b` ranges, open ranges, `30d`, `""`; how a date
     is compared (which post date, which time zone); what a target without dates does.
  4. **Categories:** each plugin's categories, own or other people's, and how to pick them.
  5. **Folders:** the default layout, single posts (step 3), `--folder` and its whole grammar
     (step 2): fields, empty fields, safety rules, the per-category list, `config.toml`.
  6. **Sync:** what is skipped, early stops, `--again`, `--dry`, `--exec`.
  7. **Logins and accounts:** `flnd auth`, methods, switching, the extension, the vault.
  8. **When it goes wrong:** exit codes, every error class in one table, `check`, `repair`.
  9. **Every command and flag**, one table each, with its default.
  10. **Config:** every `config.toml` key, its type and its default.
  11. **Plugins:** one spec section each, the same shape: targets, categories, folder and file
      names, login, limits and walls, known gaps.
  12. **Python:** every library call, one recipe each.
  13. **Writing a plugin:** the API 2 contract in one table, a 30-line example, the test kit.
  14. **For developers:** the layers, the home's files and tables, `make` targets, benches.
- Sections 2, 3 and 5 are written by sonnet from the code (`engine/resolve.py`,
  `engine/config.py`, `engine/crawl.py`), not from the old pages. The rest is haiku condensing
  the old pages.
- Module docstrings name `COOKBOOK.md#<section>`. Both `AGENTS.md` files point there.
- `tests/test_docs.py` checks the one page: python blocks run, links and `#fragments` resolve,
  every CLI command and flag is named, every built-in plugin has its section, every module
  docstring names a real section. It shrinks with the docs.
- **Done when:** `src/docs/` is gone; the two pages are within their limits; `make check` green.
- Later steps update COOKBOOK.md in the same commit as the behaviour.

## Step 1b: `--exec` without holding output in memory (sonnet)

Asked by the user 2026-11-20. Built after step 4's first (resolver) commit, before step 2.
Files: `src/engine/save.py` (`_exec`), `src/cli.py` (`_Exec`), `src/engine/config.py`, tests.

- Today `subprocess.run(..., capture_output=True)` keeps all the program's stdout and stderr in
  memory until it exits: a command writing media to stdout (`ffmpeg -i {path} -f mp4 -`) puts
  the whole file in flnd's memory. Instead: stdout to `DEVNULL`; stderr read as it comes,
  keeping only its last 4 KiB, for the warning line.
- A timeout: `[defaults] exec_timeout` in `config.toml`, seconds, default 3600, `0` for none.
  Past it, the program's whole process group is killed and a warning says so.
- Ctrl-C kills the running program's process group too, not only flnd's wait.
- One exec at a time stays (`Kind.EXEC: 1`), so two encodes never run together.
- `src/cli.py` `_Exec` and `save.py` `_exec` both fill `{path}`, `{target}`, `{post}`: keep one
  and delete the other.
- Tests, faked with a tiny Python program as the command: a program writing 50 MB to stdout
  leaves flnd's memory flat (stdout not kept); a long stderr keeps its last line; a timeout
  kills it; the config key is read. COOKBOOK.md's `--exec` and config parts updated.
- **Done when:** the tests pass; code lines go down or stay the same.

## Step 2: per-category folder templates (sonnet)

Files: `engine/config.py`, `engine/crawl.py`, `_library.py`, `cli.py`, their tests.

```text
--folder "{target}/{category}"                            # one template for all, as today
--folder "posts:{target}/feed,reels:{target}/reels,*:{target}/other"
```

- A value is one template, or a comma list of `<category>:<template>`. `*` is every category
  not named. Commas inside `{}` don't split.
- A category with no entry and no `*` keeps its default place (decision 3).
- Every template is rooted at `media/<code>/`, as today. `[plugins.<code>] folder` in
  `config.toml` takes the same string. `folder=` in the library also takes a mapping.
- Refused before any request (`UsageError`, exit 2, hint lists what is allowed): today's path
  checks on each template; an entry without `<category>:` in a list; an empty template; a
  category named twice; two `*`; a category none of the targets' plugins has; a field a
  category's plugin doesn't declare.
- `flnd get --dry` prints each category's rendered folder for its first post.
- **Done when:** a table test covers every refusal and the mixed case; old one-template tests
  pass unchanged.

## Step 3: where a single post goes (sonnet)

Files: `plugin.py` (`Post`), `engine/crawl.py`, the five plugins' single routes, tests.

| link | category | folder |
| --- | --- | --- |
| `fbk` video, `watch?v=`, `fb.watch` to a video | `videos` | `media/fbk/<owner>/videos/` |
| `fbk` reel, `fb.watch` to a reel | `reels` | `media/fbk/<owner>/reels/` |
| `fbk` photo | `photos` | `media/fbk/<owner>/photos/` |
| `fbk` permalink, `/<owner>/posts/<id>`, `share/p/` | `posts` | `media/fbk/<owner>/posts/` |
| `ins` `/p/<code>` | `posts` | `media/ins/<username>/posts/` |
| `ins` `/reel/`, `/reels/`, `/tv/` | `reels` | `media/ins/<username>/reels/` |
| `twt` a tweet | `posts` (`replies`: a reply) | `media/twt/<handle>/posts/` |
| `tik` a video | `videos` | `media/tik/<user>/videos/` |
| `tik` a photo post | `videos` | `media/tik/<user>/images/` |
| `loi` a post | `archive`, `photo` | `media/loi/<author>/<post> <title>/` |

- `Post` gains optional `owner` and `category`, read only on a single route (API 2 addition).
  `crawl.place` roots such a file at `media/<code>/<owner>/` with that category's `folder`
  template, so `{category}` and step 2's lists apply.
- Owner: Instagram `user.username`, X `screen_name`, TikTok `uniqueId`, Facebook vanity name
  else id. Unknown owner: the old place `media/<code>/<post id>/`, with a log line.
- `loi`: its template starts with `{author}`, so its root is `media/loi/`.
- Old files are not moved; the keys match, so nothing downloads again.
- **Done when:** a test per row of the table; the `ins`/`twt` guard holds.

## Step 4: hard targets and `me` (sonnet, one commit per plugin)

First commit, in the resolver (`engine/resolve.py`, sonnet): read `|` as the category
separator and `<plugin>:<kind>/<id>` as a single post; refuse the old `<target>/<cat>` written
form with the hint `write the categories after |: "ins:example_user|tagged"`; key single posts
by their written form (the key clash above), with the store migration; rename `tik`'s route
`video` to `post`. Stored rows keep resolving (a test).

Each plugin gets one table test: accepted text to `(kind, value)`, refused text to its message.
Order: `tik`, `loi`, `fbk`, then `twt` and `ins` last, under the guard.

- **fbk.** Profiles and pages share one route, told apart after load (decision 5); a tab a page
  lacks logs "no such tab". Add `/<owner>/posts/<id or pfbid…>`,
  `/share/p|r|v/<code>` (followed like `fb.watch`), `/<owner>/videos/<slug>/<id>`, and the `m.`,
  `web.`, `mbasic.` hosts. Refuse groups, events, stories, marketplace, hashtags, `/watch/`
  without `v=`. Add `fbk:me`: the accounts the login follows, from `/me/following` (confirm
  live), each as `fbk:<name or id>`.
- **ins.** User names `[A-Za-z0-9._]{1,30}`. Refuse `/explore/`,
  `/accounts/`, `/direct/`, `/stories/…` (hint: the user target with `stories`/`highlights`).
  `ins:me` stays as it is.
- **twt.** Handles `[A-Za-z0-9_]{1,15}`. Add `x.com/search?q=…` as a query,
  `/status/<id>/photo|video/<n>`, `mobile.` hosts. Refuse lists, communities, hashtags,
  spaces, other `/i/…` pages. Add `twt:me`: the `Following` GraphQL operation (its query id
  read from the main script, as the others are), paged by its cursor, held like the timelines.
- **tik.** Add `tik?<query>` and `/search?q=`, `/search/video?q=` links,
  discovering post targets from `api/search/item/full/` (confirm live); short links
  `vm.tiktok.com/…`, `vt.tiktok.com/…`, `tiktok.com/t/…`, followed to the video. Refuse `/tag/`,
  `/music/`, `/discover/`, `/live`, `/explore`. Add `tik:me`: the following list,
  `api/user/list/` (confirm live), from the logged-in page.
- **loi.** Add `loi?<query>` and the site's search link, paged by the API
  (endpoint read from the web client's script, confirm live; `sign` already signs). Refuse
  every other page of its domains.
- **Done when:** the five table tests pass; `me` tests use faked lists; the guard holds.

## Step 5: rate limits (sonnet)

1. **Engine first:** `ctx.feed(..., resume=True)`. A 429 or a declared block met while
   scrolling or paging holds (the site's wait, else the engine's), with `reset=True` when items
   came since the last hold, then carries on from the last cursor instead of the top.
2. **twt onto it,** deleting its own hold loops. Guard applies; this is the riskiest commit.
3. **fbk:** capture the tab's GraphQL query (`doc_id`, variables, cursor) on its first scroll,
   then page it in-page with `Ctx.request`, as `ins` replays its queries; scrolling is the
   fallback. Comet's block line and page, its rate-limit error codes (confirm live) and 429 go
   through `resume=True`. Pace from a live run, not a guess.
4. **tik:** capture `api/post/item_list/` and page it in-page by its cursor (confirm the page
   signs it); scrolling is the fallback. Captcha as today, plus 429 and empty pages that still
   say `hasMore`. The search and `tik:me` use the same path.
5. **loi:** a plain 429 uses `Retry-After`; the quota is read once before the first archive;
   the summary says how many archives were left for tomorrow.

- Each new limit is tested on a faked answer. `fbk` and `tik` commits run
  `make bench ARGS=--gate`.
- **Done when:** the listed tests pass; `twt` lost more lines than the engine gained.

## Step 5b: Threads, a new plugin `thr` (sonnet)

Asked by the user 2026-11-20. Built after step 5, so it starts on `resume=True`, the step 2
and 3 folders and the step 4 grammar. Modelled on `ins` (same company, same web stack): read
`src/plugins/ins.py` and its tests first, and copy their shape, not their code.

- **Targets:** a user and a single post only. No `me`, no query (a `thr?…` or search link is
  refused: `thr has no search`).
  - User: `thr:<name>`, `threads.com/@<name>`, `threads.net/@<name>`; names as Instagram's,
    `[A-Za-z0-9._]{1,30}`.
  - Post: `threads.com/@<name>/post/<code>` (and `threads.net`), `thr:post/<code>`.
  - Refused, each with a hint: `/search`, `/activity`, `/settings` and the site's other
    pages; tab links such as `threads.com/@<name>/replies` (hint: `"thr:<name>|replies"`).
- **Categories:** `posts` (owned), `replies` (owned: the user's own replies' media), `reposts`
  (opt-in), `profile` (owned: the avatar, `folder="profile"`, `key="{path}"`).
- **Reading:** a logged-in browser, the profile page's embedded data then its GraphQL feed
  (or in-page requests replayed like `ins`; confirm live which). A post's files: photos at
  their best size, carousels, videos (best MP4). Owner for step 3: the post's `username`.
- **Login:** Threads uses the Instagram login. `login = cookies("sessionid", url=...)` on
  `threads.com` (confirm live which cookies); without one, what the public sees.
- **Limits:** treat as `ins`: its "please wait" answers held through `blocked`, 429 through
  `resume=True`, pace measured on a live run (start at 2 s as `ins`).
- **Templates:** `folder = "{category}"`, `filename = "{date}_{post}{index}"`, key by post
  and index, as `ins`.
- **Files:** `src/plugins/thr.py`, `src/plugins/_manifest.py` (the built-in list), its test
  file with a table test of accepted and refused targets and faked pages, a COOKBOOK.md
  plugin section, `CODE_LINES`/`TEST_LINES` raised in the same commit with the reason.
- **Done when:** the table test and faked-feed tests pass; `make check` green; a hand live
  check on a test account follows.

## Step 5c: a lighter browser, hidden where it can be (sonnet, Opus reviews closely)

Asked by the user 2026-11-20. Found: the engine always opens a visible window (only
`flnkit.browser.launch(headless=...)` can hide it, and nothing passes it); pages load every
image, video and font, though plugins read JSON (`ASSET_TYPES` only filters what is captured);
videos autoplay (TikTok, Facebook reels); `LAUNCH_ARGS` turn off background throttling on
purpose (background tabs must keep running).

1. **Measure first.** A bench in `tests/bench/` for one scrolled faked page: the browser's peak
   memory (all its processes) and CPU seconds. Record before every change below.
2. **Block what no plugin reads.** A context route that aborts `image`, `media` and `font`
   requests in every tab. A plugin that needs them declares `Plugin.assets = True` (an API 2
   addition). The captured responses are unchanged. Confirm live per plugin that scrolling
   still loads more (a feed sized by images may need a viewport height instead).
3. **Cheaper switches.** Add `--mute-audio`, `--autoplay-policy=user-gesture-required`,
   `--disable-features=Translate,MediaRouter,OptimizationHints`,
   `--disable-component-update`, `--no-first-run`, a small fixed window. Keep the anti-throttling
   switches. Each switch measured; one that saves nothing is left out.
4. **Hidden by default, visible when needed.** `[defaults] window` in `config.toml`, `"hidden"`
   (default) or `"visible"`. A handoff (captcha) in a hidden browser closes it, reopens the
   same profile visible, and carries on. The `browser` login method is always visible.
5. **Hide the hidden state.** Use Chrome's new headless (the full browser, not the headless
   shell). Already done: `navigator.webdriver`, the `HeadlessChrome` user agent. Add: the
   client-hint brands (`Sec-CH-UA`, `navigator.userAgentData`) set through CDP
   `Emulation.setUserAgentOverride` with metadata, so no `HeadlessChrome` brand is sent; a real
   screen and window size (`screen`, `outerWidth`); `navigator.plugins` and `mimeTypes` as a
   desktop Chrome has them; WebGL vendor and renderer not `SwiftShader`; `Notification`
   permission consistent with `permissions.query`. A test page in the suite reads each of these
   in hidden and visible mode and asserts they match.
6. **Per plugin**, after a hand live check on the test accounts: `twt`, `ins`, `fbk`, `loi`
   (none), `thr` hidden; `tik` hidden too, relying on rule 4 for its captcha. A plugin that is
   caught hidden goes back to visible by declaring it, with the reason.

- **Done when:** the bench shows the memory and CPU saved (numbers in the commit message);
  the fingerprint test passes; COOKBOOK.md says what is blocked, the `window` key, and the
  reopen rule.

## Step 5d: resources on the run screen (sonnet)

Asked by the user 2026-11-20. The `get` (and sync) screen gains one line: flnd's processes
(itself, the browser's, a running `--exec` program) with their count, total memory (RSS) and
CPU %, and the same for the browser alone. Read every 2 s off the loop (a `CALL` thread), drawn
by `src/ui` through the theme. `--json` gets it as a periodic event; a plain non-terminal log
does not.

- Reading: `psutil` (one new dependency, with its reason in `pyproject.toml`, and added to
  AGENTS.md's planned set), since the standard library can't read another process's memory on
  all three systems. The process tree is flnd's own pid and its children, so the browser and
  exec programs are counted without asking Playwright.
- The cost stays small: one read per 2 s, measured by `make bench` (`ui.overhead` must not get
  worse by over 15%).
- Tests fake the reading; one test that the line renders, one for the JSON event.
- **Done when:** the line shows on a live run; COOKBOOK.md's run-screen part says what each
  number counts.
- Built after step 5c, whose bench reads the same numbers.

## Step 6: SPEC.md and a short COOKBOOK.md (sonnet splits, haiku moves text)

Asked by the user 2026-11-20. Built right after step 4's `fbk` commit, before `twt` and `ins`,
so every later step updates the right page.

- `flnd/SPEC.md` holds everything, with no gray area: today's COOKBOOK.md content (every
  grammar with its accepted forms, refused forms and exact errors; every command, flag, config
  key, error class; every plugin's spec; the library; writing a plugin; developers). At most
  1,500 lines.
- `flnd/COOKBOOK.md` keeps only simple, common uses, each a one-line goal and the command or
  three-line script that does it: download a profile, pick categories, a single post, a search
  with a folder, sync what you follow, a date window, a dry run, log in, switch accounts,
  `--exec` with ffmpeg, fix a failed run. Each recipe links to its SPEC.md section. At most 250
  lines.
- `flnd/README.md` stays at most 40 lines, linking both.
- Module docstrings and `AGENTS.md` name `SPEC.md#<section>`. `tests/test_docs.py` checks both
  pages: python blocks run and `flnd` lines parse in both; links and fragments resolve; SPEC.md
  names every command, flag and plugin; each page within its limit.
- From here on, a behaviour change updates SPEC.md, and COOKBOOK.md only when a common use
  changes.
- Opus reads both pages once, end to end: SPEC.md against "no gray area", COOKBOOK.md against
  "simple and common".

## Step 7: last cookbook pass, then done (haiku, Opus reads)

Replaces the shrink step (dropped by the user, 2026-11-20). After 5c and 5d: bring
COOKBOOK.md (at most 250 lines) and SPEC.md (at most 1,500) in line with everything built,
fix what the live checks found, and stop. Opus reads COOKBOOK.md end to end once.

## Decisions (all settled by the user, 2026-11-20)

1. **Single-post links** go to the owner's folder (step 3); queries go to their `--folder`.
2. **Categories:** owned by default; chosen only by the `|<cat>,<cat>` list or `|all`. No
   `--from` flag.
3. **A category with no entry in a `--folder` list** keeps its default place. For a query the
   list must cover every category (one template, or `*`).
4. **Single posts:** a link, or `<plugin>:<kind>/<id>`.
5. **Facebook pages:** one route for profiles and pages, told apart after load.
6. **`fbk:me`:** the accounts the login follows (pages and people), not friends.
7. **A followed query's folder** is stored; a different `--folder` later is refused unless
   `--again` is given.
