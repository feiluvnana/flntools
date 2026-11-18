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
| `mht:author/example_creator` | `author` | every manga of an author |
| `https://mimihentai.moe/author/example_creator` | `author` | every manga of an author |
| `https://mimihentai.moe/authors/1234567890` | `author` | the same, by id |
| `mht:genre/example` | `genre` | every manga of a genre |
| `https://mimihentai.moe/genre/example` | `genre` | every manga of a genre |
| `https://mimihentai.moe/genres/1234567890` | `genre` | the same, by id |
| `mht?example words` | `query` | every manga a search finds |

A chapter has no short form: give its link. An author or genre, in a link or after `mht:author/`
or `mht:genre/`, may be a name or a numeric id. A genre name is matched against the site's whole
genre list, and an author name is looked up with the site's author search. Either match ignores
case and accents, and a name with hyphens is also tried with spaces.

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
other term, or a `-` before any term but `genre`, is refused. An empty query is refused, and so
is a term with no value or a `chapters:` that isn't a number. An unclosed quote is read as the
words typed, split at spaces.

## Categories

These belong to the `manga` route, and through it to `author`, `genre` and `query`. Both are
the plugin's own categories, so both are taken when `--only` isn't given.

| Category | What it holds |
| --- | --- |
| `chapters` | every chapter's pages |
| `cover` | the manga's cover image |

- `mht:<id>` and manga links: without `--only` each gets both categories. `--only` names
  one or both.
- `mht?<query>`, author and genre links: `--only` applies to each manga found; without it
  each gets both categories. (Before 2026-11-16 the author and genre routes weren't marked as
  discovering, so `--only` with them was refused as if they were one post; fixed.)
- A chapter link is one post, downloaded whole: `--only` with it is a usage error.

## Logging in

The plugin has no login (`login = None`): everything it reads is public, so flnd never warns
about a login, and `flnd login mht` is refused as "mht needs no login".

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
3. Pages are numbered by their place in the chapter (`001`, `002`, ...). A page with no image
   keeps its number, so the pages after it keep theirs.
4. Searches page through `ctx.pages`: the first request sends the search's terms and
   `cursor_res=true` alone, and each next one sends the last page's `next_cursor` as `cursor`,
   until the cursor is missing, empty or repeated.
5. The early stops are the engine's: a sync of a manga ends once it meets saved chapters, and
   `--when` drops chapters outside its days. The plugin lists every chapter in the API's
   order; a search ends only where the site's cursor does.

**Pacing:** `pace` is 0.5, so requests to `mimihentai.moe` are at least half a second apart.
It sets no `limits`: each host's files download 8 at once, adapting up to 32.

## Where files go

The class declares:

- `folder = "chapters/{order} {title}"`: `order` is the chapter's number, zero-padded to 3 digits
  when whole (`007`) and kept as text otherwise (`7.5`); `title` is the chapter's title.
- `filename = "{post}"`, which is not used: the plugin names every page and the cover itself, as
  a stem with no extension, pages `001`, `002` and so on, and the cover `cover`. The engine adds
  the extension its answer's `Content-Type` names, else its URL's own when that is a media
  extension, and renames a file whose bytes are another known format. So a page served as WebP
  is saved as `001.webp`.
- `key = "{post}_{num}"`: the history key, stored as `mht:<chapter id>_<n>`, where `n` is the
  page's place from 0.
- The `cover` category has its own `folder = "cover"` and `key = "{post}_cover"`, stored as
  `mht:<manga id>_cover`.
- `fields` also names `author`, `genres` and `chapters`. The cover's post sets them, but the
  default folders don't use them. Every field is passed raw; the engine makes each folder and
  file name safe (`Example Chapter` is saved as `Example_Chapter`).

Files go under `.flnd/media/mht/<target>/`, where `<target>` is the manga's id (or the chapter's
id, for a chapter link). A query, author or genre target saves nothing under its own name: each
manga it finds has its own folder.

```
.flnd/media/mht/1234567890/chapters/001_Example_Chapter/001.webp
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
- **Cursor paging.** Searches page by number unless asked for `cursor_res=true`; flnd always
  asks, and follows `next_cursor`. Date not recorded.
- **Dates.** A chapter is dated by its `created_at`, and the cover by the manga's
  `last_updated`. Date not recorded.

## Limits

- No login, so nothing behind one.
- A chapter has no `mht:<id>` form; only its link reads it, and `mht:<number>` is always a manga.
- `chapters:<n>` matches an exact chapter count only, and is checked by flnd on each result, not
  by the site.
- Negation works for genres only.
- The plugin declares no `unsupported` links; any other link is refused with the forms above.
