# Phase 8: draft

**A draft of ideas, not a plan** (user, 2026-11-13: "a general draft idea for now"). Nothing here
is decided; each idea becomes a step only when the user picks it, after Phase 7 is done. They grow
from what the user asked for in Phase 7: flnd doing more than downloading, finding things on any
site with one query language, more sites (manga readers first), commands in groups, and a lean,
readable core.

## Ideas

### 1. Finders everywhere

Phase 7 gives the find API and two finders (loi, mht). Phase 8 gives every site that has a search
worth having its own, each declaring its own parameters:

| Site | What it could find | Its parameters, roughly |
| --- | --- | --- |
| fbk | people, pages, groups by name | `kind`, `city` |
| ins | accounts by name; posts by hashtag | `kind`, `tag` |
| twt | posts and accounts | `from`, `kind`, `date`, `lang`, `likes` |
| tik | accounts, videos by words or hashtag | `kind`, `tag` |
| tlg | your chats; public channels by name | `kind` |
| loi | posts by words, author, category | `author`, `category` |

### 2. Watches: a query that keeps downloading

A saved query that `sync` runs like a target: whatever it finds that's new is downloaded.

```
flnd watch add mht 'genre:(vanilla OR romance) chapters:>5'
flnd watch ls
flnd watch forget <id> | all
flnd sync                    # targets, then watches: new results downloaded
```

It joins `find` and `download` without a pipe, and fits the `target` and `auth` groups. Each
watch remembers what it has seen, so a result is downloaded once.

### 3. More sites, and a way to add one

Manga and catalogue sites like mht, chosen by the user, each a module with a `Site`, a
`Downloader`, a `Finder`, or any of them. To make a new site cheap:
- `scripts/dev.py site <code>` writes a skeleton module and its test with fake JSON;
- a short "writing a site" page made from `tests/example_site.py`, which runs;
- `scripts/dev.py try` extended to finders, for the hand check on a live site.

### 4. Find what you already have

The same query language over what flnd has saved, with no request to any site:

```
flnd find local 'site:mht genre:vanilla date:[2026-01-01 TO *]'
```

It reads history, `results.json` and the fields kept with each post. "Local" is one more finder,
whose items are files and posts on disk.

### 5. Keep a post's fields

Today a post's title, author, tags and description are used for its name and then dropped (loi's
`info.md` aside). Keeping them in history, not in sidecar files, would make idea 4 possible and
let `target ls` show more. Phase 6 rejected metadata sidecars, which this avoids.

### 6. Read and browse

A small local viewer, `flnd serve`, for what is saved: a manga's chapters page by page, a
profile's posts by date, and find results with their covers. Read-only, on localhost. It is a
bigger step than the others, and the user's call.

### 7. Sites from outside the repository

Site modules loaded from a folder of the user's (`.flnd/sites/`), so a site can be added without
a release. They follow the same import rules and are listed by `flnd site ls`. It is risky (code
from anywhere runs with the user's logins), so it waits for the user's word.

### 8. A lean core, kept lean

Phase 7's readability rules and refactors land; Phase 8 holds the line:
- a size budget per module, measured by `scripts/dev.py check` and shown in its summary;
- the code-to-prose ratio reported, so a file sliding back into prose is seen;
- a yearly pass removing what no site and no command uses, inside flnd (`shared` keeps whatever
  is useful).

### 9. Health of each site

Sites change without warning. A hand-run `scripts/dev.py health` runs each site's downloader and
finder once on the test accounts and says what broke (an API refused, a field gone, a page
changed), so a fix starts from a report, not from a failed sync.

## Questions for the user

1. Which of these matter most, and in what order?
2. Which manga or catalogue sites come next after mht?
3. Watches (2): run by `sync`, or a command of their own?
4. A viewer (6) and outside sites (7): wanted at all?
