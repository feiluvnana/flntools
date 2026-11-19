# mimihentai (`mht`)

flnd downloads manga from mimihentai.moe: every chapter of a manga as page images, and its
cover. It also downloads one chapter by its link, and finds manga by author, genre or a search
query, downloading each one found. It reads the site's public JSON API over plain HTTP. It needs
no login and no browser. The plugin is `src/plugins/mht.py`, on plugin API 2.

A *target* is what you give `flnd get`; a *category* is one part of a target, named after
its last `/`. See the [glossary](../glossary.md) and the [CLI guide](../cli.md).

## Targets

| You type | Route | What it downloads |
| --- | --- | --- |
| `mht:1234567890` | `manga` | one manga, by category |
| `https://mimihentai.moe/manga/1234567890` | `manga` | the same |
| `mht:chapter/1234567891` | `chapter` | one chapter, whole |
| `https://mimihentai.moe/manga/1234567890/chapter/1234567891` | `chapter` | the same |
| `mht:author/example_creator` | `author` | every manga of an author |
| `https://mimihentai.moe/author/example_creator` | `author` | every manga of an author |
| `https://mimihentai.moe/authors/1234567890` | `author` | the same, by id |
| `mht:genre/example` | `genre` | every manga of a genre |
| `https://mimihentai.moe/genre/example` | `genre` | every manga of a genre |
| `https://mimihentai.moe/genres/1234567890` | `genre` | the same, by id |
| `mht?example words` | `query` | every manga a search finds |

A chapter is `mht:chapter/<id>`, or its link. An author or genre, in a link or after
`mht:author/` or `mht:genre/`, may be a name or a numeric id. A genre name is matched against the
site's whole genre list, and an author name is looked up with the site's author search. Either
match ignores case and accents, and a name with hyphens is also tried with spaces.

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
the plugin's own categories, so both are taken when none are named.

| Category | What it holds |
| --- | --- |
| `chapters` | every chapter's pages |
| `cover` | the manga's cover image |

- `mht:<id>` and manga links: without categories each gets both; `mht:<id>/cover` names
  one.
- `mht?<query>`, author and genre links: the categories a search names
  (`mht?<query>/cover`) apply to each manga found; without them each manga found gets both.
- A chapter link is one post, downloaded whole: it takes no categories.

## Logging in

The plugin has no login (`login = None`). Everything it reads is public, so flnd never asks for a
login, and `flnd auth login mht` is refused with "mht needs no login".

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
2. `chapters` lists every chapter in the order the API gives. A chapter's pages are fetched only
   when the chapter is downloaded, so a chapter already saved costs no request.
3. Pages are numbered by their place in the chapter (`001`, `002`, ...). A page with no image
   keeps its number, so the pages after it keep theirs.
4. Searches page through `ctx.pages`: the first request sends the search's terms and
   `cursor_res=true` alone, and each next one sends the last page's `next_cursor` as `cursor`.
   The paging stops when the cursor is missing, empty or repeated, or a page is empty.
5. The early stops are the engine's: a sync of a manga ends once it meets saved chapters, and
   `--when` drops chapters outside its days. The plugin lists every chapter in the API's order,
   and a search ends only where the site's cursor does.

**Pace and concurrency.** `pace` is 0.5: two page requests of one account are at least half a
second apart. The plugin sets no `limits`: each host's files download 8 at once, adapting up to
32.

## Where files go

The class declares:

- `folder = "chapters/{order} {title}"`: `order` is the chapter's number, zero-padded to 3 digits
  when whole (`007`) and kept as text otherwise (`7.5`); `title` is the chapter's title.
- `filename = "{post}"`, which is not used: the plugin names every page and the cover itself, as
  a stem with no extension, pages `001`, `002` and so on, and the cover `cover`. The engine adds
  the extension from the content, so a page served as WebP is saved as `001.webp`.
- `key = "{post}_{num}"`: the history key, stored as `mht:<chapter id>_<n>`, where `n` is the
  page's place from 0.
- The `cover` category has its own `folder = "cover"` and `key = "{post}_cover"`, stored as
  `mht:<manga id>_cover`.
- `fields` names `title`, `order`, `author`, `genres` and `chapters`. The chapter folder uses
  `order` and `title`; the cover's post sets the manga's author, genres and chapter count. Every
  field is passed raw, and the engine makes each folder and file name safe (`Example Chapter` is
  saved as `Example_Chapter`).

Files go under `.flnd/media/mht/<target>/`, where `<target>` is the manga's id. For a chapter
link, it is the chapter's `chapter/<id>` target, made safe for a folder name. A query, author or
genre target saves nothing under its own name: each manga it finds has its own folder.

```
.flnd/media/mht/1234567890/chapters/001_Example_Chapter/001.webp
.flnd/media/mht/1234567890/cover/cover.webp
```

## Quirks

- **Chapter list size.** The chapters request returns every chapter in one response, whatever
  its `limit` says (seen 2026-11-14).
- **Search results carry the manga.** A search item that has a title and a cover is kept, so the
  manga it names isn't fetched again.
- **Hyphenated names.** Names in links use hyphens for spaces, so a name with hyphens is looked up
  both with spaces and as written.
- **Chapter numbers** may be whole or not, and numbers or text. Only whole ones are zero-padded.
- **Cursor paging.** Searches page by cursor, not by number: flnd always asks for `cursor_res=true`
  and follows `next_cursor`.
- **Dates.** A chapter is dated by its `created_at`, and the cover by the manga's `last_updated`.

## Limits

- No login, so nothing behind one.
- `mht:<number>` is always a manga. A chapter is `mht:chapter/<id>` or its link.
- `chapters:<n>` matches an exact chapter count only. flnd checks it on each result, since the
  site can't filter by chapter count.
- Negation works for genres only.
- The plugin declares no `unsupported` links. Other links on the site are not targets, and are
  refused with the forms above.

## Testing

Unit tests fake the network, the browser and the clock. Live checks are done by hand, on a test
account, and never in unit tests. A change to this plugin must keep the routes and the query
terms above, the chapter and cover folders, and the history keys (`mht:<chapter id>_<n>`,
`mht:<manga id>_cover`), so that files already saved are not saved again.
