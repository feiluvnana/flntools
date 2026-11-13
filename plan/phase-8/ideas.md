# Phase 8: draft

**A draft, not a plan** (user, 2026-11-13). Nothing here is decided; an idea becomes a step when
the user picks it, after Phase 7 is built. Each idea says why it is worth building, what to build
on the code Phase 7 leaves, what it costs and risks, and a recommendation. A proposed order closes
the file.

## Where Phase 7 leaves flnd

What Phase 8 builds on, all in [flnd](../phase-7/flnd.md):
- **Two classes per site**: `Site` (access) and `Downloader` (routes: a target in, categories or
  targets out). A query is one route, `<site>?<query>`, in Lucene's syntax, with each site's own
  parameters and OR as merged searches. Both sit on **one job**.
- **Stores** in `.flnd/metadata/`: `history.db` (one row per file: path, size, mtime, date, marks),
  `targets.db` (each target's settings, complete listings, cursors, sync marks, and the results a
  synced query has seen), `sites.db` (each site's state), `secrets.db`.
- **Commands** `download` (with `--limit`, `--dry` and `--sync`), the `sync` and `auth` groups.
- **A lean, readable core**, with its writing rules enforced.

The ideas below are ranked by what they give per line of code.

---

## 1. Watches: covered by synced queries

Phase 7 step 10 builds this: `flnd download --sync 'mht?genre:(vanilla OR romance) chapters:>5'`
keeps a query, and each `flnd sync` downloads up to 25 new results and leaves targets the user
already follows alone. Nothing is left for Phase 8.

## 2. Keep a post's fields, then query what you already have

**Why.** A post's title, author, tags and description are used for its file name and then thrown
away (loi's `info.md` aside). So flnd can't answer "which of my saved manga are tagged X?", and
`sync ls` can only count files.

**What to build.**
- **Keep the fields**: each post's `fields` (Phase 7 made their keys the same across sites:
  `title`, `author`, `category`, `tags`, `url`) go into a new `posts` table in `history.db`, one row
  per post: site, post id, target, date, fields as JSON, its files' keys. No sidecar files (Phase 6
  rejected those), no change for downloaders: the job already holds the fields.
- **A local query target**: `flnd download 'local?site:mht tags:vanilla date:[2026-01-01 TO *]'`.
  It is one more `Downloader` with a query route, `local`, whose route reads SQLite instead of a
  site, so `query.py` and the route's `Query` come for free. Its params: `site`, `target`, `title`,
  `author`, `tags`, `category`, `date`.
- **Listing, not downloading**: what is saved needs no download, so the local route is used with
  `--dry`: `flnd download --dry 'local?…'` lists the matching saved files and downloads none.
- `sync ls` gains a count of posts and the newest post's date.

**Depends on**: step 4 (fields with common keys), step 10 (query routes).
**Size**: about 120 lines; one table, one route.
**Risks**: history grows (a row per post, a few hundred bytes); posts saved before this have no
row until `sync` lists them again.
**Recommend**: second. It turns the archive into something you can search, at little cost.

## 3. Query routes for every site

**Why.** A query is only as useful as the sites it covers. Each query route is small once the API
exists (mht's is about 60 lines), and the hard part, how each site searches, is site knowledge
flnd already partly has.

**What to build**, each with its own parameters and fake-JSON tests, and a hand check:

| Site | Yields | Through | Notes |
| --- | --- | --- | --- |
| twt | posts, accounts | `SearchTimeline` (step 3 already pages it) | most value: X's search is rich (`from`, `since`, `min_faves`) and its rate headers are known |
| ins | accounts; hashtag posts | `topsearch`, the tag page's GraphQL | hashtag pages often need a login; tight budgets |
| fbk | people, pages, groups | the search page's Comet GraphQL | needs the feed reader (lean S1) |
| tik | accounts, videos | the browser's search page | browser only, slow |
| tlg | public channels; your chats | Telethon `contacts.Search`, dialogs | cheap, reliable |
| loi | posts | its search API | waits for its page shape (Phase 7) |

**Size**: 40–100 lines each. **Recommend**: twt and tlg first (cheap, reliable), then ins, fbk, tik.

## 4. A cheap way to add a site

**Why.** The user wants more manga sites. Adding one today means reading a large site module to
copy its shape. Phase 7's readable rules and the `Site`/`Downloader` split make a small template
possible.

**What to build.**
- `scripts/dev.py site <code> <domain>` writes `flnd/sites/<code>.py` (a `Site`, and a `Downloader`
  with one target route, one category and one query route) and `tests/test_<code>.py` with fake
  JSON, all passing, so the work starts from green.
- `tests/example_site.py` is the tutorial, as code that runs (Phase 7 moved it there).
- `scripts/dev.py try` covers query routes: `dev.py try 'mht?genre:example'` runs a live query
  target on the saved login and prints the results and the requests, writing nothing.
- A **site checklist** in the template's docstring: the API or the page, how it pages, dates, ids
  for keys, login, pace, CDN, a hand check before the first commit.

**Size**: about 150 lines of scripts and template. **Recommend**: with the next new site, so the
template is shaped by a real one.

## 5. Health of each site

**Why.** Sites change without warning; today a broken site is found by a failed `sync`, with a
user's run as the test. A hand-run check that says *what* broke (an API refused, a field missing, a
page reshaped) turns a vague failure into a precise fix.

**What to build.** `scripts/dev.py health [site…]`: on the test accounts' saved logins, each site's
downloader lists one known target's first page and fetches one file's headers, and each query route
runs one query; a table of pass/fail with the reason (status, missing field, timeout). Nothing
written, no file downloaded whole. Each site declares its probe: a target, a query, the fields it
expects.

**Size**: about 120 lines. **Risks**: it runs against real sites, so it stays a hand-run developer
task, never in tests or CI. **Recommend**: after 3, when there are enough query routes to break.

## 6. A local viewer

**Why.** Saved manga are folders of images; reading them needs another program, and a query's
results are a list only the terminal shows. A small viewer would make flnd a reader, not only a
downloader.

**What to build.** `flnd serve` on `localhost` (stdlib `http.server`, read-only): a page per kept
target (its categories, posts by date, covers), a reader for a chapter's pages, and a page per
query target's results with covers and a "download" link that runs `flnd download` for it.

**Size**: 400+ lines with HTML and tests, the largest idea here. **Risks**: a second interface to
keep working; a server, even on localhost, needs care (bind to 127.0.0.1 only, no write besides
"download"). **Recommend**: only if the user wants flnd to be a reader; otherwise leave it out.

## 7. Sites from outside the repository

**Why.** A site could be added without changing flnd itself.

**What to build.** Site modules loaded from `.flnd/sites/`, listed by a `flnd site ls`, held to the
same import rules.

**Risks**: code from anywhere runs with the user's logins and cookies; a broken outside site looks
like a flnd bug. **Recommend**: not now; idea 4 makes adding a site inside the repository cheap
enough.

## 8. Keep the core lean

**Why.** Phase 7 brings `shared` and `flnd` from about 20,700 lines to about 16,000. Without a
measure, they grow back.

**What to build.** `scripts/dev.py check` prints, after the tests, each module's code lines against
a budget kept in `tests/data/budget.json`, and the code-to-prose ratio; a module past its budget is
a warning, past it by 20% a failure, until the budget is raised in a commit that says why.

**Size**: about 60 lines. **Recommend**: right after Phase 7, before any of the above adds code.

---

## Proposed order

1. **Keep the core lean (8)**: a measure before anything grows.
2. **Keep fields, local query target (2)**.
3. **Query routes: twt, tlg (3)**, then a new manga site with the **site template (4)**.
4. **Health (5)**, then more query routes.
5. **Viewer (6)** and **outside sites (7)** only on the user's word.

Watches (1) are built in Phase 7 step 10, so they are not in the order. Steps 1–3 are about 480
lines in all, about 3% on top of the lean core.

## Questions for the user

1. The order above, or another?
2. Which manga or catalogue sites come after mht?
3. A viewer: is flnd meant to become a reader too?
