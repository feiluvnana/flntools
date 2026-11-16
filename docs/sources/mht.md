# mimihentai (`mht`)

flnd downloads manga from mimihentai, an adult manga site: every chapter of a manga as page
images, and its cover. It also downloads one chapter by its link, and finds manga by author,
genre or a search query, downloading each one found. It reads the site's public JSON API over
plain HTTP. It needs no login and no browser.

A *target* is what you give `flnd download`; a *category* is one part of a target, chosen with
`--only`. See the [glossary](../glossary.md) and the [CLI guide](../cli.md).

## Targets

| You type | Route | What it downloads |
| --- | --- | --- |
| `mht:1234567890` | `manga` | one manga, by category |
| `https://mimihentai.moe/manga/1234567890` | `manga` | the same |
| `https://mimihentai.moe/manga/1234567890/chapter/1234567891` | `chapter` | one chapter, whole |
| `https://mimihentai.moe/author/example_creator` | `author` | every manga of an author |
| `https://mimihentai.moe/authors/1234567890` | `author` | the same, by id |
| `https://mimihentai.moe/genre/example` | `genre` | every manga of a genre |
| `https://mimihentai.moe/genres/1234567890` | `genre` | the same, by id |
| `mht?example words` | `query` | every manga a search finds |

A chapter has no short form: give its link. An author or genre in a link may be a name or a
numeric id. A name is looked up with the site's search and matched with case and accents
ignored; a name with hyphens is also tried with spaces.

`author`, `genre` and `query` don't download anything themselves. They find manga, and each
manga found is downloaded as its own `mht:<id>` target.

### Queries

A query is words, quoted as in a shell when a value has spaces. Plain words are a title search.
These terms narrow it:

| Term | Meaning |
| --- | --- |
| `title:<text>` | words in the title |
| `author:<name or id>` | by this author |
| `genre:<name or id>` | in this genre; may be given more than once |
| `-genre:<name or id>` | not in this genre |
| `parody:<name or id>` | of this parody |
| `character:<name>` | with this character |
| `chapters:<n>` | only manga with exactly `n` chapters |

For example: `mht?genre:example -genre:1234567890 author:"example creator" chapters:1`. Any
other term, or a `-` before any term but `genre`, is refused. An empty query is refused.

## Categories

These belong to the `manga` route, and through it to `author`, `genre` and `query`.

| Category | What it holds |
| --- | --- |
| `chapters` | every chapter's pages |
| `cover` | the manga's cover image |

- `mht:<id>` and manga links: without `--only` each gets both categories. `--only` names
  one or both.
- `mht?<query>`: `--only` applies to each manga found; without it each gets both categories.
- A chapter link is one post, downloaded whole: `--only` with it is a usage error.
- Author and genre links take no `--only` (the CLI calls them one post), and each manga they
  find is downloaded with both categories.

## Logging in

The source declares no login cookie, so flnd always counts it as logged in and never warns.
`flnd login mht <how>` still takes a browser or a cookie file's path, and a saved cookie jar
is sent with requests, but nothing in the source depends on it.

## How it fetches

Every request goes to `https://mimihentai.moe/api/`:

| Request | Used for |
| --- | --- |
| `manga/<id>` | a manga's title, cover and fields |
| `manga/<id>/chapters?limit=100` | the manga's chapters |
| `chapters/<id>` | a chapter's info and page images |
| `genres` | the genre list, to turn a genre name into its id |
| `authors/search?q=<name>` | an author name to its id |
| `parodies/search?q=<name>` | a parody name to its id |
| `manga/search?q=<words>` | a query of plain words |
| `manga/advanced-search` | author and genre links, and queries with terms |

1. A manga target fetches the manga, unless a search in the same run already returned it with
   a title and cover.
2. `chapters` lists every chapter in the order the API gives. A chapter's pages
   are fetched only when the chapter is downloaded, so a chapter already saved costs no request.
3. Pages are named by their place in the chapter (`001`, `002`, ...).
4. Searches are paged: flnd asks with `cursor_res=true` and follows `next_cursor` until it is
   empty or repeats.

**Pacing:** `pace` is 0.5, so requests to `mimihentai.moe` are at least half a second apart.
It sets no `limits`: each host's files download 8 at once, adapting up to 32.

## Where files go

The class declares:

- `folder = "chapters/{order} {title}"`: `order` is the chapter's number, zero-padded to 3 digits
  when whole (`007`) and kept as text otherwise (`7.5`); `title` is the chapter's title.
- `filename = "{id}"`, which is not used for pages or covers, since each file is named by the
  source: pages as `001.webp` and so on, the cover as `cover.webp`. Each keeps its URL's extension
  when it has one.
- `key = "{id}_{num}"`: the history key, stored as `mht:<chapter id>_<n>`, where `n` is the
  page's place from 0.
- The `cover` category has its own `folder = "cover"` and `key = "{id}_cover"`, stored as
  `mht:<manga id>_cover`.
- `fields` also names `author`, `genres` and `chapters`. The cover's post sets them, but the
  default folders don't use them.

Files go under `.flnd/media/mht/<target>/`, where `<target>` is the manga's id (or the chapter's
id, for a chapter link). A query, author or genre target saves nothing under its own name: each
manga it finds has its own folder.

```
.flnd/media/mht/1234567890/chapters/001 Example Chapter/001.webp
.flnd/media/mht/1234567890/cover/cover.webp
```

## Quirks

- **Chapter list size.** The chapters request returns every chapter in one response, whatever
  its `limit`. Handled since 2026-11-14.
- **Search results carry the manga.** A search item that has a title and a cover is kept, so the
  manga it names isn't fetched again. Handled since 2026-11-14.
- **Hyphenated names.** Names in links use hyphens for spaces, so a name with hyphens is looked up
  both with spaces and as written. Handled since 2026-11-14.
- **Chapter numbers** may be whole or not, numbers or text; only whole ones are zero-padded.
  Handled since 2026-11-14.
- **Dates.** A chapter is dated by its `created_at`, and the cover by the manga's
  `last_updated`. Date not recorded.

## Limits

- No login, so nothing behind one.
- A chapter has no `mht:<id>` form; only its link reads it, and `mht:<number>` is always a manga.
- `chapters:<n>` matches an exact chapter count only, and is checked by flnd on each result, not
  by the site.
- Negation works for genres only.
- The source declares no `unsupported` links; any other link is refused with the forms above.
