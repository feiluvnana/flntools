# links

Phase 7, step 5. A file found in two tabs is saved once, by its key, and skipped in the second, so
it is missing from that tab's folder: a reel is in `videos/` but not `reels/`, a photo in `posts/`
but not `photos/`. The user asked on 2026-11-13 for a symlink in the second folder.

## The rule

- When the job skips a file because its key is saved, and **the file that holds it is in another
  category's folder**, it makes a **relative symlink** at the path the file would have had:
  `reels/2026-11-09_2964864390550364.mp4 → ../videos/2026-11-09_2964864390550364.mp4`. Relative,
  so the target folder can be moved as a whole.
- **Within one category** nothing is linked: a second copy there is a listing bug, fixed at its
  source ([dates](dates.md#1-one-listing-memory-in-the-job)).
- **Where a symlink can't be made** (Windows without Developer Mode or admin rights), a **hard
  link**; where neither can (FAT, exFAT, another volume), the skip stays, with one warning per run.
- A link has **no history row of its own**: it is a view of the saved file. The status line counts
  it as linked, not saved ([output](output.md#status-lines)).

## Kept true by `sync`

- A link that is missing is made again on the next run that lists the file.
- A symlink whose file is gone is removed by `sync`'s repair, after the repair downloads the file
  again (its history row still names it), so the link is then made again.
- A hard link is the file itself, so deleting the original by hand leaves the file in the second
  folder only; `sync` sees the history path missing and moves that copy back to it, as Phase 6's
  classify plan did for its link folders.

## Scope

**Every source** (user, 2026-11-13). The rule is in the job, so it applies wherever categories
overlap: Instagram's tagged posts also in posts, X's media tab and posts, TikTok's reposts,
Facebook's reels and videos, photos and posts.

## Tests

A file listed in two categories is downloaded once and symlinked into the second folder,
relative; the same in one category is skipped, not linked; a symlink that fails falls back to a
hard link, then to a skip with one warning; a dangling link is removed and remade by `sync`.
