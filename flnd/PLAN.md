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
- **Shorter code.** Every step reports its net code lines. A step that adds a feature keeps its
  growth small. Step 7 shrinks the rest. The end goal: `CODE_LINES` in `tests/test_size.py`
  at or below 36,000 (now 37,500), `TEST_LINES` at or below 28,500 (now 29,100), with all the
  features below built.
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
  hint showing `--folder "{query}"`. Its hits all go there, not to their owners' folders
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

## Step 6: cookbook pass (haiku)

Bring COOKBOOK.md in line with steps 2 to 5 where a commit missed something, and recheck its
line limit. Opus reads the page once, end to end, against "no gray area".

## Step 7: shrink (sonnet, Opus picks the targets)

- Opus lists the ten largest functions and the code repeated across plugins, from `wc -l`
  and `grep` only, and hands them over in one brief.
- Likely wins: the hold loops now in the engine; per-plugin link-following (`fb.watch`, TikTok
  short links, share links) as one `ctx` call; table tests in place of one-test-per-form;
  docstrings that repeated the deleted pages.
- No behaviour change; every test passes unchanged except moves. Lower `CODE_LINES` and
  `TEST_LINES` to the new counts in the same commit.

## Decisions to settle

1. **Single-post folders (step 3).** Recommended: the owner's folder. The other way: keep
   `media/<code>/<post id>/` and only add the category.
2. **Choosing categories.** Owned by default is settled. Recommended: only the `|<cat>,<cat>`
   list on the target, as now, documented in full. The other way:
   bring `--from <cats>` back as a flag, for one choice
   over many targets.
3. **A category with no entry in a `--folder` list.** Recommended: its default place. The
   other way: refuse the run until every wanted category has an entry or `*` is given.
4. **Single posts** (settled by the user): a link, or `<plugin>:<kind>/<id>`; categories
   after `|`.
5. **Facebook pages.** Recommended: one route for profiles and pages. A separate
   `fbk:page/<name>` is possible, but the links can't tell which is which.
6. **`fbk:me`.** Facebook has friends and follows. Recommended: follows (pages and people),
   which are public-content accounts. The other way: friends, or both.
