# Phase 8: draft

**A draft, not a plan** (user, 2026-11-13). Nothing here is decided; an idea becomes a step when
the user picks it, after Phase 7 is built. Each idea says why it is worth building, what to build
on the code Phase 7 leaves, what it costs and risks, and a recommendation. A proposed order closes
the file.

## Where Phase 7 leaves flnd

What Phase 8 builds on, all in [flnd](../phase-7/flnd.md):
- **Three classes per site**: `Site` (access), `Downloader` (a target in), `Finder` (a Lucene
  query in, each site's own parameters, OR as merged searches), and **one job** below both.
- **Stores** in `.flnd/metadata/`: `history.db` (one row per file: path, size, mtime, date, marks),
  `targets.db` (each target's settings, complete listings, cursors), `sites.db` (each site's
  state), `secrets.db`.
- **Commands** `download`, `sync`, `find`, and the `target` and `auth` groups.
- **A lean, readable core**, with its writing rules enforced.

The ideas below are ranked by what they give per line of code.

---

## 1. Watches: a query that keeps downloading

**Why.** Phase 7's `find` answers "what is there?" once. What a reader of a manga site really
wants is "download what's new in the genres I follow, every day", which today needs a cron job
piping `find` into `download`. That pipe loses what was already handled, and can't be listed or
forgotten.

**What to build.**
```
flnd watch add mht 'genre:(vanilla OR romance) chapters:>5'      # prints its id: w1
flnd watch ls                                                    # each watch, its query, last run, found
flnd watch forget w1 | all
flnd sync                                                        # targets, then each watch
```
- A watch is a row in `targets.db` (`watch:<id>`): the site, the query text, the time it last ran,
  and which result ids it has seen.
- `sync` runs each watch's finder (as `find` does), takes the results not seen before, and
  **downloads their targets** through the ordinary download path, with their saved settings
  (`--shrink`). A downloaded target becomes a kept target, so later syncs update its chapters too.
- First run: only remembers what it finds (no download), unless `watch add --now`, so adding a
  broad watch never starts a thousand downloads by surprise.

**Depends on**: step 10's finder and query module; `target` group for the shape of its commands.
**Size**: about 150 lines (engine: 80, cli: 40, tests).
**Risks**: a broad query downloads a lot; the first-run rule and `limit` cap it.
**Recommend**: first. It is the natural end of "flnd does more than download", and small.

## 2. Keep a post's fields, then find what you already have

**Why.** A post's title, author, tags and description are used for its file name and then thrown
away (loi's `info.md` aside). So flnd can't answer "which of my saved manga are tagged X?", and
`target ls` can only count files.

**What to build.**
- **Keep the fields**: each post's `fields` (Phase 7 made their keys the same across sites:
  `title`, `author`, `category`, `tags`, `url`) go into a new `posts` table in `history.db`, one row
  per post: site, post id, target, date, fields as JSON, its files' keys. No sidecar files (Phase 6
  rejected those), no change for downloaders: the job already holds the fields.
- **A local finder**: `flnd find local '<query>'` runs the same Lucene language over that table:
  `site:mht tags:vanilla date:[2026-01-01 TO *]`. It is one more `Finder` whose `find` reads SQLite
  instead of a site, so `query.py`, the table output and `results.json` come for free. Its params:
  `site`, `target`, `title`, `author`, `tags`, `category`, `date`.
- `target ls` gains a count of posts and the newest post's date.

**Depends on**: step 4 (fields with common keys), step 10 (finder).
**Size**: about 120 lines; one table, one finder.
**Risks**: history grows (a row per post, a few hundred bytes); posts saved before this have no
row until `sync` lists them again.
**Recommend**: second. It turns the archive into something you can search, at little cost.

## 3. Finders for every site

**Why.** Finding is only as useful as the sites it covers. Each finder is small once the API
exists (mht's is about 60 lines), and the hard part, how each site searches, is site knowledge
flnd already partly has.

**What to build**, each with its own parameters and fake-JSON tests, and a hand check:

| Site | Finds | Through | Notes |
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
copy its shape. Phase 7's readable rules and the `Site`/`Downloader`/`Finder` split make a small
template possible.

**What to build.**
- `scripts/dev.py site <code> <domain>` writes `flnd/sites/<code>.py` (a `Site`, a `Downloader`
  with one route and one category, a `Finder`) and `tests/test_<code>.py` with fake JSON, all
  passing, so the work starts from green.
- `tests/example_site.py` is the tutorial, as code that runs (Phase 7 moved it there).
- `scripts/dev.py try` covers finders: `dev.py try mht 'genre:example'` runs a live find on the
  saved login and prints the results and the requests, writing nothing.
- A **site checklist** in the template's docstring: the API or the page, how it pages, dates, ids
  for keys, login, pace, CDN, a hand check before the first commit.

**Size**: about 150 lines of scripts and template. **Recommend**: with the next new site, so the
template is shaped by a real one.

## 5. Health of each site

**Why.** Sites change without warning; today a broken site is found by a failed `sync`, with a
user's run as the test. A hand-run check that says *what* broke (an API refused, a field missing, a
page reshaped) turns a vague failure into a precise fix.

**What to build.** `scripts/dev.py health [site…]`: on the test accounts' saved logins, each site's
downloader lists one known target's first page and fetches one file's headers, and each finder runs
one query; a table of pass/fail with the reason (status, missing field, timeout). Nothing written,
no file downloaded whole. Each site declares its probe: a target, a query, the fields it expects.

**Size**: about 120 lines. **Risks**: it runs against real sites, so it stays a hand-run developer
task, never in tests or CI. **Recommend**: after 3, when there are enough finders to break.

## 6. A local viewer

**Why.** Saved manga are folders of images; reading them needs another program, and find results
are a JSON file. A small viewer would make flnd a reader, not only a downloader.

**What to build.** `flnd serve` on `localhost` (stdlib `http.server`, read-only): a page per kept
target (its categories, posts by date, covers), a reader for a chapter's pages, and a page per find
query's `results.json` with covers and a "download" link that runs `flnd download` for it.

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
2. **Watches (1)**: the end of "more than downloading".
3. **Keep fields, find local (2)**.
4. **Finders: twt, tlg (3)**, then a new manga site with the **site template (4)**.
5. **Health (5)**, then more finders.
6. **Viewer (6)** and **outside sites (7)** only on the user's word.

Steps 1–4 are about 600 lines in all, about 4% on top of the lean core.

## Questions for the user

1. The order above, or another?
2. Watches: download on their first run, or remember first (as proposed)?
3. Which manga or catalogue sites come after mht?
4. A viewer: is flnd meant to become a reader too?
