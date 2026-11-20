# Plan: hard targets, per-category folders, one cookbook, rate limits

Written 2026-11-20, after the Instagram and Twitter/X overhaul (251de85, 01bf5be, d8a2c7f) and
`--folder` (61a0fc3). Six asks from the user, turned into five build steps. Each step is one or
more commits, `make check` green at each, docs updated in the same commit.

## Where things stand

- `fbk`: `user` (profiles and pages alike); single `video`, `watch`, `photo` and permalink
  `post`. Comet's "temporarily blocked" page and feed line are held. A 429 from a feed is
  `Blocked` but not caught, so the tab starts again. Tabs are scrolled, never replayed.
- `ins`: `user`, single `post` (p, reel, reels, tv), and `ins:me` (finds the users it follows).
  Done in the overhaul: in-page REST, replayed GraphQL, "please wait" holds, a 2 s pace.
- `twt`: `user`, single `post`, `twt?<query>` (finds tweets). Done in the overhaul: a bucket
  per operation, 429 holds with `reset`, search windows by `max_id`.
- `tik`: `user`, single `video` (video and photo links). A captcha is handed off, then held.
  No 429 handling. The post list is scrolled, never replayed.
- `loi`: single `post` (categories `archive`, `photo`). JSON "slow down" is held, the daily VIP
  quota skipped. A plain 429 is left to the engine.
- `mht` (`chapter`, `manga`, `author`, `genre`, `mht?<query>`), `tlg` (`chat`, flood waits
  held) and `web` (any link): nothing seen blocks them.

The new pieces worth reusing everywhere: `Ctx.request` (an in-page request, signed by the page
and by `Plugin.sign`), browser request capture (replay a query the page made), 429 from a feed
raised as `Blocked` with its wait, and `ctx.hold(..., reset=True)` once progress was made.

A file's history key is already plugin-wide (`<code>:<key>`), so a post saved by its link and
later by its profile is saved once. Only its folder differs. Ask 5 fixes that.

## Step 1: one cookbook (ask 3)

First, so every later step updates one page only.

- `src/docs/` shrinks to `cookbook.md`. The other pages are deleted: `README.md`,
  `architecture.md`, `cli.md`, `errors.md`, `extension.md`, `glossary.md`, `library.md`,
  `performance.md`, `storage.md`, `writing-a-plugin.md` and `plugins/*.md` (about 5,700 lines).
- Target size: at most 1,200 lines. Short sentences, tables over prose, one example per idea.
- Its outline:
  1. Install and first run (5 lines).
  2. Targets: the forms, `--limit`, `--dry`, `--when`, sync and `--again`.
  3. Folders: the default layout, single posts (step 2), `--folder` and its grammar (step 3).
  4. Logins and accounts: `flnd auth`, switching, the extension, the vault (what, not how).
  5. When it goes wrong: exit codes, the error classes in one table, `check`, `repair`, retries.
  6. Plugins: one spec section per plugin, each the same shape:
     targets (accepted forms and refused ones), categories (own or opt-in), folder and file
     names, login, limits and walls, known gaps. Ends with a short "writing a plugin" part:
     the API 2 contract in one table and a 30-line example.
  7. Python: the library calls, one recipe each.
  8. Config: every `config.toml` key in one table.
  9. For developers: the layers in one diagram, the home's files and tables, `make bench`.
- `flnd/README.md`: ten lines, pointing to the cookbook.
- Code that names a page: module docstrings name `cookbook.md#<section>` instead.
  `AGENTS.md` (root and `flnd/`) points to the cookbook sections instead of the deleted pages.
- `tests/test_docs.py` is rewritten for one page: python blocks run, links and `#fragments`
  resolve, every CLI command and flag is named, every built-in plugin has its section and no
  section is left for one that is gone, every module docstring names a real section.
- Delegate the move of text to the low-cost model once the outline above is fixed; write the
  plugin spec sections and the test with the default model.

## Step 2: where a single post goes (ask 5)

Today a link's files land in `media/<code>/<post id>/`, with no category. A search's hits do the
same, one folder per tweet. Proposed: a single post goes where its profile would have put it.

| link | category | folder |
| --- | --- | --- |
| `fbk` `/videos/<id>`, `watch?v=`, `fb.watch` to a video | `videos` | `media/fbk/<owner>/videos/` |
| `fbk` `/reel/<id>`, `fb.watch` landing on a reel | `reels` | `media/fbk/<owner>/reels/` |
| `fbk` `photo?fbid=` | `photos` | `media/fbk/<owner>/photos/` |
| `fbk` permalink, `/<owner>/posts/<id>`, `share/p/` | `posts` | `media/fbk/<owner>/posts/` |
| `ins` `/p/<code>` | `posts` | `media/ins/<username>/posts/` |
| `ins` `/reel/`, `/reels/`, `/tv/` | `reels` | `media/ins/<username>/reels/` |
| `twt` a tweet (and each search hit) | `posts` (`replies`: a reply) | `media/twt/<handle>/posts/` |
| `tik` a video (and each search hit) | `videos` | `media/tik/<user>/videos/` |
| `tik` a photo post | `videos` | `media/tik/<user>/images/` (its `part`) |
| `loi` a post (and each search hit) | `archive`, `photo` | `media/loi/<author>/<post> <title>/` |

- So a reel saved by its link sits beside the same user's reels, and a later profile sync finds
  it in place instead of linking it.
- The owner comes from the post's own data (Instagram's `user.username`, X's `screen_name`,
  TikTok's `uniqueId`, Facebook's vanity name else its id). No owner known: the old place,
  `media/<code>/<post id>/`, with a log line.
- Engine: `Post` gains two optional fields, `owner` and `category`, read only on a single
  route (an API 2 addition, no new major). `crawl.place` roots such a file at
  `media/<code>/<owner>/` and uses that category's `folder` template, so `{category}` and the
  per-category `--folder` of step 3 apply.
- `loi` is the odd one: its folder template already starts with `{author}`, so its root drops
  the target value (`media/loi/`), not adds an owner.
- Old files stay where they are. No move, no migration: the key still matches, so nothing is
  downloaded again.

## Step 3: per-category folder templates (ask 2)

The grammar, for `--folder`, `folder=` and `[plugins.<code>] folder`:

```text
--folder "posts:{target}/feed,reels:{target}/reels,*:{target}/other"
--folder "{target}/{category}"        # one template for every category, as today
```

- A value is either one template (today's meaning) or a comma list of `<category>:<template>`.
  `*` is every category not named. Commas inside `{}` don't split.
- A category with no entry and no `*` keeps its default place (`media/<code>/<target>/...`).
- Each template is rooted at `media/<code>/`, as `--folder` is today. Config uses the same
  string, so there is one grammar.
- Library: `folder=` also takes a mapping, `{"posts": "...", "*": "..."}`.

Checks before any request (all `UsageError`, exit 2, with a hint naming what is allowed):

- today's path checks on each template (no `..`, no leading `/`, no `\`, no drive, no control
  characters, braces well formed);
- a list where some entry has no `<category>:` prefix, an empty template, a category named
  twice, more than one `*`;
- a category none of the targets' plugins has (the hint lists the plugins' categories);
- a field the plugin doesn't declare, checked per category against that plugin;
- two categories of one target rendering to the same static folder is allowed (keys differ).

`flnd get --dry` prints each category's rendered folder for the first post it would save, so
a template can be tried without saving anything.

## Step 4: hard targets (ask 1)

The accepted kinds, as the user set them, and nothing else. Every other form of a plugin's own
domains is refused before any request, with a hint naming the right form. Each plugin gets a
table test: accepted text to `(kind, value)`, refused text to its message.

- **fbk: user, page, single post.**
  - Pages and profiles keep one route (their links can't be told apart before the page loads).
    After loading, the route records which it is, and the categories a page lacks (`tagged`)
    log "no such tab" instead of failing.
  - Add single forms: `/<owner>/posts/<id or pfbid…>` (refused today), `/share/p/<code>`,
    `/share/r/<code>`, `/share/v/<code>` (followed like `fb.watch`), `/<owner>/videos/<slug>/<id>`,
    and the `m.`, `web.` and `mbasic.` hosts.
  - Refuse groups, events, stories, marketplace, hashtags, `/watch/` with no `v=`, and the
    site's own pages, as today, each with its reason.
- **ins: user, single post.**
  - A user name is `[A-Za-z0-9._]{1,30}`; anything else is refused.
  - Refuse `/explore/`, `/explore/tags/`, `/accounts/`, `/direct/`, `/stories/…` and
    `/stories/highlights/…` links (hint: use the user target with `stories` or `highlights`).
  - `ins:me` is a decision, below.
- **twt: user, single post, query.**
  - A handle is `[A-Za-z0-9_]{1,15}`.
  - Add `x.com/search?q=…` (and `twitter.com`) as a query target: today `search` is refused.
  - Add tweet forms `/status/<id>/photo/<n>`, `/status/<id>/video/<n>`, `/i/web/status/<id>`,
    `mobile.` hosts.
  - Refuse lists, communities, hashtags, spaces, `/i/…` pages.
- **tik: user, single post, query (new).**
  - Add `tik?<query>` and `tiktok.com/search?q=` / `/search/video?q=`. It discovers single
    post targets, as `twt?` does, with `--limit` as the number of posts. Read from the search
    page's `api/search/item/full/` answers (to confirm live).
  - Add short links `vm.tiktok.com/<code>`, `vt.tiktok.com/<code>`, `tiktok.com/t/<code>`,
    followed to their video.
  - Add an `unsupported` table: `/tag/`, `/music/`, `/discover/`, `/live`, `/explore`.
- **loi: single post, query (new).**
  - Add `loi?<query>` and the site's search link. It discovers post targets, paged by the API.
    The search endpoint and its signing are read from the web client's JavaScript (to confirm
    live; the signing is the one `sign` already does).
  - Refuse every other `loix.cc` and `moegoat.com` page.
- `mht`, `tlg` and `web` are left as they are.

## Step 5: rate limits on the other plugins (ask 4)

First, lift the pattern `twt` repeats into the engine, so a plugin gets it by a flag:

- `ctx.feed(..., resume=True)`: a 429 or a declared block met while scrolling holds (the site's
  wait, else the engine's), with `reset=True` when items came since the last hold, then reopens
  the page at the last cursor instead of the top. `twt` moves onto it and loses its own loops.

Then each plugin:

- **fbk** (the most to win).
  - Tabs: capture the tab's GraphQL query (`doc_id`, variables, cursor) on the first scroll,
    then page it with `Ctx.request` in-page, as `ins` replays its post queries. Scrolling stays
    as the fallback when no query is captured.
  - Holds: Comet's block line and page, Comet's rate-limit error codes (to confirm live), and
    429, through `resume=True`. A held tab carries on from its cursor.
  - Pace: from 0.2 s to a measured value from a live run; scrolling gets the human-like pauses
    of the scroll request.
- **tik.**
  - Post list: capture `api/post/item_list/` and page it in-page by its cursor (the page's own
    script signs in-page requests; to confirm live). Scrolling stays as the fallback.
  - Holds: captcha as today, plus 429 and empty pages that still say `hasMore`, through
    `resume=True`. The search of step 4 uses the same path.
- **loi.** A plain 429 uses `Retry-After`. The daily quota is read once, before the first
  archive, and the run's summary says how many archives were left for tomorrow.
- **mht, tlg, web.** No change: nothing seen blocks them.

Each limit added is held by a unit test on a faked answer. The live checks are done by hand on
the test accounts, never in a test.

## Order and size

1. Step 1 (docs). 2. Step 2 (single posts). 3. Step 3 (folders). 4. Step 4, one commit per
plugin. 5. Step 5: engine `resume`, then `twt` onto it, then `fbk`, `tik`, `loi`.

Steps 2 to 5 raise `CODE_LINES` and `TEST_LINES` in `tests/test_size.py`, each in its commit,
saying why. Step 1 should lower them. Steps 5 `fbk` and `tik` run `make bench ARGS=--gate`.

## Decisions to settle

1. **`ins:me`.** The ask lists only user and post for `ins`. Recommended: keep it, since it
   only discovers user targets and the cookbook's "sync everyone you follow" depends on it.
2. **Single-post folders (step 2).** Recommended: the owner's folder, as above. The other way
   is to keep `media/<code>/<post id>/` and only add the category.
3. **A category with no entry in a per-category `--folder`.** Recommended: its default place.
   The other way is to refuse the run until every wanted category has an entry or `*` is given.
4. **One cookbook.** Recommended: developer notes (architecture, plugin API, storage tables)
   go in the cookbook's last two parts, and module docstrings keep the details. The other way is
   to drop them and keep only user docs.
5. **Facebook pages.** Recommended: one route for profiles and pages, told apart after load. A
   separate `fbk:page/<name>` form is possible, but the links can't tell which is which.
