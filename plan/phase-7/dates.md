# dates

Phase 7, steps 4 and 5. Every source lists each item once, and every file has a date (user,
2026-11-13: for every source, not Facebook only). Seen first in a live Facebook run on 2026-11-13.

## What happens today

| Source | Where dates come from | Undated items | One item listed dated and undated | Seen-sets |
| --- | --- | --- | --- | --- |
| fbk | a node's own timestamp, or the nearest dated parent's (`media_items`) | yes: the photos grid, some feed items | **yes** | per GraphQL result |
| ins | `taken_at`; a lazy grid post by its `pk` | rare: a listed post with no `pk` | no | per tab and target (`listed`, `taken`) |
| tik | `createTime`, else the id's top bits | practically never | no | `_seen`, and `tab()`'s own |
| twt | `created_at` | no | no (posts and media share key and date: a [links](links.md) case) | per timeline |
| tlg | `message.date` | no | no | none |
| loi | `post.date` | no | no | none |

- **Facebook's seen-set is per result.** The feed sends one photo in two results: inside its post,
  dated, and alone, undated. The job dedupes by key (`fbk:<id>`) and keeps **whichever came
  first**, so a folder mixes `2026-11-11_<id>.jpg` and `<id>.jpg`.
- **Some photos have no date in any listing**, and a Facebook id isn't a timestamp; Facebook strips EXIF.
- **A name built by the source holds the date**, so a date found later can't reach it (fbk
  `post_of`, twt `media_file`, ins `files_of`, tik `post_of` build `<date>_<id>` themselves).

## 1. One listing memory, in the job

Step 4. The job already dedupes by key and owns the history, which a source can't read, so the
memory lives there, for every source:
- **Each key once per target**, across results and categories, as today; and **one key → date
  map**, filled from the listing and from history rows, so a date any category or earlier run
  found reaches every copy.
- **An undated post is held until its category ends**, then queued with the map's date; a dated
  copy that comes meanwhile replaces it. A post is queued at once when its key's date is known.
- **Names follow `Post.date`**: a category keyed by id names its files from the source's
  `filename` format (`{date}_{id}`), not a name the source built, so a date filled in later
  reaches the name ([lean](lean.md#sources) S3). Names stay built where the key is the path
  (ins stories and highlights, every `profile` category, loi, tlg).
- ins's `taken` set and tik's `_seen` go, as the job does their work.

## 2. Every file dated

Step 5, decided by the user on 2026-11-13. For a post still undated when its category ends:
1. **The source's `date` hook**, when it has one: `date(site, post) -> DateLike`, one paced
   request for that item's own info. Facebook's reads `facebook.com/photo/?fbid=<id>` through the
   page's API (`site.fetch(…, in_page=True)`) for its `created_time`: 100 undated photos add
   about 2–3 minutes at its 1.5 s pace. Other sources add theirs when they need one (ins's media
   info API by `pk`, tik's item detail).
2. **Else, or when that fails** (deleted, private, blocked): the date of the nearest dated post in
   listing order, since categories list newest first. Its history row records `estimated: true`.
3. A block while dating stops the dating, not the run: the rest are estimated, and the next run
   asks again.

### Files already saved without a date

**Every `flnd sync`** (user, 2026-11-13) looks in the history for files saved without a date, or
with an estimated one, asks the source's `date` hook for each by its key, and adds the date:
renames the file to its dated name, and updates its row and modified time. A name taken already
(the dated copy saved too, before this fix) keeps the dated file and deletes the undated one when
both have the same bytes. One the site can't date keeps its name, and the next `sync` asks again.

This reverses Phase 6 [simplify §3](../phase-6/simplify.md#3-no-history-migrations)'s "no rename
when the real date is found", by the user's later decision.

## Tests

Fakes only: one item listed dated and undated, in either order, gives one file with the dated
name, in any source; a date found in one category reaches the same key in another; an undated
post whose `date` hook answers is dated by it; one whose hook fails gets its neighbour's date and
the estimated mark; `sync` dates and renames an undated saved file and keeps its row; a block
while dating estimates the rest without failing the run.
