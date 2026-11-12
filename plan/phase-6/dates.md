# dates

Phase 6, step 6, with [scripts](scripts.md). The user asked on 2026-11-12 that every saved photo and video carry its date, and that adding one be easy for a source. Today each source builds `<YYYY-MM-DD>_…` into its own file names, in its own way, and several files get no date at all.

> **Changed by [architecture](architecture.md)** (decided 2026-11-12, flnd adopts gallery-dl's architecture): the engine's dating is the default `name` format (`{date:%Y-%m-%d}_…`) and the `mtime` postprocessor. The rules below are unchanged.


## Today, checked 2026-11-12

| Source | Files saved without a date |
| --- | --- |
| ins | profile pictures (`profile/<id>.jpg`); a post or story whose JSON leaves out `taken_at` |
| fbk | `profile_<id>.jpg`, `cover_<id>.jpg`; a post whose date `posted_on` can't find |
| twt | `avatar_<id>.jpg`, `banner_<id>.jpg` |
| tik | `avatar_<id>.jpg` |
| loi | the preview images `images/01.jpg`, `02.jpg`, … (the post has a date; the names don't use it), and the archives, named as the server names them |
| tlg | none |

And for **every** source, the file's modified time is the moment it was downloaded, so a file manager or photo app sorting by date sorts by download order. `--compress-*` re-encodes and strips metadata, so a photo's own EXIF date doesn't survive either.

## Design: the engine dates files, not the source

1. **`session.save(…, date=)` puts the date in the name.** A source passes the bare name, `ABC123_0.jpg`, and its `date`. The engine saves `2026-01-02_ABC123_0.jpg`. No source builds a date into a name again, and `save_post` ([scripts](scripts.md#save_post)) loses its date logic.
2. **Every saved file's modified time is set to its date** (`os.utime`), at noon UTC, so sorting by date in any file manager sorts by post date. This also covers files kept as the site names them, such as loi's archives.
3. **When the source has no date**, the engine falls back, in this order:
   1. the CDN's `Last-Modified` header on the download, which Instagram's, Twitter's and TikTok's image CDNs send and which is when the file was uploaded;
   2. the day of the download.

   Either way every file gets a date. The history row records which one was used, as `"dated": "source" | "server" | "downloaded"`, so `flnd sync` can replace a fallback once the source finds the real date.
4. **`save(…, dated=False)`** keeps a name exactly as given, for files whose name is the content: loi's `info.md` (which `write` saves, and which is never dated), and loi's archives, whose server names other tools expect. Their modified time is still set.

### Sources, after

- **Profile pictures, covers and banners:** the source passes the date when the site gives one (Twitter's and Facebook's profile JSON has none; Instagram's has none either). Otherwise `Last-Modified` dates them.
- **loi:** passes the post's date for its preview images, which become `2026-01-02_01.jpg`, …. They sort by post when several posts share a folder.
- **ins and fbk:** a missing `taken_at` or post date no longer drops the date from the name. `day` ([scripts](scripts.md#day)) tries every timestamp the JSON has, then the ID's own time (Instagram's `pk`, TikTok's ID), then the fallbacks above.

### Names and keys

- A name the engine dates is `<YYYY-MM-DD>_<name>`, the same as today's names, so files already saved keep their names. [`--classify`](classify.md) never changes it; it only adds links in `labels/`.
- The history key defaults to `folder/name` **without** the date, so a file whose date changes (a fallback replaced by the real date) keeps its row. The one-time history rewrite in [scripts](scripts.md#save_post) also strips the date from old keys.
- `flnd sync`'s repair step sets the modified time of every file it already has to its recorded date, so folders downloaded before Phase 6 sort by post date too.

## Tests

- `session.save(url, "A_0.jpg", date=day)` writes `<day>_A_0.jpg`, and sets the file's modified time to that day at noon UTC.
- Without a date:
  - a download with `Last-Modified` is named and timed by it;
  - one without is named by the download day;
  - the row records `dated` in each case.
- `dated=False` keeps the name as given, and still sets the modified time.
- A file saved with a fallback date and then found with its real date by `flnd sync` is renamed, and keeps its history row.
- loi's preview images are named `<post day>_01.jpg`.
- `flnd sync` on a Phase 5 folder sets every file's modified time from its history row.
