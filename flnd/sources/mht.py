"""mimihentai (mht): manga chapters and covers, one chapter by its link, and manga found by
author, genre or search, all from the site's public JSON API.

    flnd download mht:1234567890 --only chapters,cover
    flnd download https://mimihentai.moe/manga/1234567890/chapter/1234567891
    flnd download "mht?genre:example chapters:1" --only cover

Who calls it: the engine (`flnd.job`), through the routes and categories below, on the target's
listing thread. What it calls: only its `Site` (`site.fetch` and `site.pages`, over plain HTTP:
no browser, no login) and the stdlib.

How a target is read:
1. A `manga` target (`mht:<id>`, a manga link) fetches the manga (`manga/<id>`), unless a search
   earlier in the run gave it with a title and a cover (`Mimi._cached`), and hands it to each
   wanted category.
2. `chapters` lists every chapter (`manga/<id>/chapters`) as a lazy post: a chapter's pages
   (`chapters/<id>`) are fetched only for a chapter the engine will download.
3. `cover` yields the manga's cover image.
4. A `chapter` target (a chapter link) is one post: the chapter's pages, filed as `chapters`
   files are, so a chapter saved through its link isn't downloaded again through its manga.
5. `author`, `genre` and `query` targets discover: they page through a search
   (`manga/advanced-search` or `manga/search`, `site.pages` following `next_cursor`) and yield
   each manga found as a `mht:<id>` target, which the engine downloads with the run's `--only`.
   A name in a link or a query term is first turned into the site's id (`_resolve_id`).

Rules a change must keep:
- The names and history keys of saved files never change: a page's key is
  `<chapter id>_<n>` and the cover's `<manga id>_cover`. A changed key makes every saved file
  download again.
- The engine owns the early stops (a sync caught up, `--when`); the source lists every chapter in
  the API's order, and a search ends only where the site's cursor does.

Under .flnd/media/mht/<target>/:
  chapters/<order> <title>/001.webp   a chapter's pages, by their place in the chapter
  cover/cover.webp                    the manga's cover
See docs/sources/mht.
"""

from __future__ import annotations

import shlex
import unicodedata
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import unquote, urlsplit

from flnd.source import File, NotFound, Post, Site, Source, Target, UsageError, target

API = "https://mimihentai.moe/api/"
# A lookup: the site's `(name, id)` pairs for a name, from one of its search or list endpoints.
Lookup = Callable[[Site, str], Iterator[tuple[str, str]]]


class Mimi(Source):
    """mimihentai's manga, chapters and covers, and the manga a search finds."""

    code = "mht"
    name = "mimihentai"
    domains = ("mimihentai.moe",)
    api = 1
    pace = 0.5
    # `chapters/<order> <title>`: the chapter's number and title (the engine makes them safe).
    folder = "chapters/{order} {title}"
    fields = ("title", "order", "author", "genres", "chapters")
    # Every page and the cover are named by the source (`File.name`), so this is never used.
    filename = "{post}"
    # `<chapter id>_<n>`, `n` the page's place from 0: the keys the history has always had.
    key = "{post}_{num}"

    def __init__(self) -> None:
        # Search items that carry a manga's title and cover, by manga id: a manga a search found
        # in this run is not fetched again. One instance serves a run's discovered targets.
        self._cached: dict[str, dict[str, Any]] = {}

    @target(r"mimihentai.moe/manga/*/chapter/{id:\d+}")
    def chapter(self, site: Site, id: str) -> Iterator[Post]:
        """One chapter, by its link: `https://mimihentai.moe/manga/<manga>/chapter/<id>`.

        Args:
            site: the target's site.
            id: the chapter's number.

        Yields:
            The chapter's one post, its pages in order.

        Raises:
            NotFound: the API has no such chapter.
        """
        body = site.fetch(f"{API}chapters/{id}").json
        info = body.get("info") if isinstance(body, dict) else None
        if not isinstance(info, dict) or "id" not in info:
            raise NotFound(f"chapter {id} isn't there")
        yield Post(info["id"], _chapter_pages(body), date=info.get("created_at"),
                   order=format_order(info.get("order")), title=info.get("title") or "")

    @target(r"mht:{id:\d+}", r"mimihentai.moe/manga/{id:\d+}")
    def manga(self, site: Site, id: str) -> dict[str, Any]:
        """One manga, by id or link: `mht:<id>` or `https://mimihentai.moe/manga/<id>`.

        Args:
            site: the target's site.
            id: the manga's number.

        Returns:
            The manga's JSON, its `id` set, for the categories.

        Raises:
            NotFound: the API has no such manga, or one without a title.
        """
        cached = self._cached.get(id)
        if cached is not None:
            return {**cached, "id": cached.get("id") or id}
        body = site.fetch(f"{API}manga/{id}").json
        if not isinstance(body, dict) or not body.get("title"):
            raise NotFound(f"manga {id} isn't there")
        return {**body, "id": body.get("id") or id}

    @manga.category
    def chapters(self, site: Site, manga: dict[str, Any]) -> Iterator[Post]:
        """Every chapter of the manga, each a lazy post dated by its `created_at`.

        Args:
            site: the target's site.
            manga: what the route returned.

        Yields:
            The chapters, in the API's order; a chapter's pages are fetched only when the
            engine asks for them.
        """
        # The API gives every chapter in one answer whatever `limit` says (seen 2026-11-14).
        items = site.fetch(f"{API}manga/{manga['id']}/chapters", params={"limit": 100}).json
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict) or "id" not in item:
                continue
            chapter_id = item["id"]
            yield Post(chapter_id, lambda chapter_id=chapter_id: _chapter_files(site, chapter_id),
                       date=item.get("created_at"), order=format_order(item.get("order")),
                       title=item.get("title") or "")

    @manga.category(folder="cover", key="{post}_cover")
    def cover(self, site: Site, manga: dict[str, Any]) -> Iterator[Post]:
        """The manga's cover image, keyed `<manga id>_cover`, dated by the manga's update.

        Args:
            site: the target's site.
            manga: what the route returned.

        Yields:
            One post, or none for a manga without a cover.
        """
        url = manga.get("cover_url")
        if url:
            yield Post(manga["id"], [File(url, name=cover_name(url))],
                       date=manga.get("last_updated"), **_manga_fields(manga))

    @target(r"mimihentai.moe/author/{author:[^/]+}", r"mimihentai.moe/authors/{author:[^/]+}",
            discovers=True)
    def author(self, site: Site, author: str) -> Iterator[Target]:
        """Every manga of an author, by the author's name or id in a link.

        Args:
            site: the target's site.
            author: the author's id, or name as the link spells it.

        Yields:
            A `mht:<id>` target for each manga.

        Raises:
            UsageError: no author has that name.
        """
        author_id = _resolve_id(site, "author", author, lookup_authors)
        for item in _search(site, f"{API}manga/advanced-search", {"author": author_id}):
            yield self._found(item)

    @target(r"mimihentai.moe/genre/{genre:[^/]+}", r"mimihentai.moe/genres/{genre:[^/]+}",
            discovers=True)
    def genre(self, site: Site, genre: str) -> Iterator[Target]:
        """Every manga of a genre, by the genre's name or id in a link.

        Args:
            site: the target's site.
            genre: the genre's id, or name as the link spells it.

        Yields:
            A `mht:<id>` target for each manga.

        Raises:
            UsageError: no genre has that name.
        """
        genre_id = _resolve_id(site, "genre", genre, lookup_genres)
        for item in _search(site, f"{API}manga/advanced-search", {"genre": genre_id}):
            yield self._found(item)

    @target("mht?{query}")
    def query(self, site: Site, query: str) -> Iterator[Target]:
        """Every manga a search finds: plain words search titles, terms narrow it (`_parse_query`).

        Args:
            site: the target's site.
            query: the text after `mht?`.

        Yields:
            A `mht:<id>` target for each manga found that passes the `chapters:` filters.

        Raises:
            UsageError: the query is empty, has an unknown term, or names nothing the site has.
        """
        words, params, chapter_filters = _parse_query(site, query)
        if words is not None:
            items = _search(site, f"{API}manga/search", {"q": words})
        else:
            items = _search(site, f"{API}manga/advanced-search", params)
        for item in items:
            # The site can't filter by chapter count, so each result is checked here.
            if all(item.get("chapter_count") in wanted for wanted in chapter_filters):
                yield self._found(item)

    def _found(self, item: dict[str, Any]) -> Target:
        """A search item as its manga's target, remembering the item when it has a cover.

        A search item with a title and a cover holds all the `manga` route reads, so the
        manga isn't fetched again when the engine downloads it in this run.

        Args:
            item: one item of a search's page, with an `id`.

        Returns:
            The manga's resolved target.
        """
        manga_id = str(item["id"])
        if item.get("title") and item.get("cover_url"):
            self._cached[manga_id] = item
        return Target(f"mht:{manga_id}", kind="manga")


# ---------------------------------------------------------------------------------------------
# Names and fields
# ---------------------------------------------------------------------------------------------

def _manga_fields(manga: Mapping[str, Any]) -> dict[str, Any]:
    """A manga's fields for the templates and sidecars, raw: the engine makes them safe.

    Args:
        manga: a manga's JSON, from its own request or a search item.

    Returns:
        `title`, `author` (names joined by commas), `genres` (the same) and `chapters` (the
        chapter count, or "" when unknown).
    """
    authors = ", ".join(
        str(a.get("name", "")) for a in (manga.get("authors") or ())
        if isinstance(a, dict) and a.get("name")
    )
    genres = ", ".join(
        str(g.get("name", "")) for g in (manga.get("genres") or ())
        if isinstance(g, dict) and g.get("name")
    )
    count = manga.get("chapter_count")
    return {
        "title": manga.get("title") or "",
        "author": authors or str(manga.get("author") or ""),
        "genres": genres,
        "chapters": count if count is not None else "",
    }


def format_order(value: Any) -> str:
    """A chapter's number as its folder shows it: zero-padded to 3 digits when whole.

    The API gives it as a whole number, a float or text (seen 2026-11-14); a part chapter
    (`1.5`) keeps its text.

    Args:
        value: the API's `order`.

    Returns:
        `001`, `1.5`, or "" for none.

    Example:
        >>> format_order(7), format_order(7.0), format_order("7.5"), format_order(None)
        ('007', '007', '7.5', '')
    """
    if value is None or value == "" or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return f"{value:03d}"
    if isinstance(value, float):
        return f"{int(value):03d}" if value.is_integer() else str(value)
    text = str(value).strip()
    return f"{int(text):03d}" if text.isdigit() else text


def page_name(number: int, url: str) -> str:
    """A chapter page's name, `001.webp`, keeping the URL's extension when it has one.

    Args:
        number: the page's place in the chapter, from 1.
        url: the page's image URL.

    Returns:
        The name, extension included.

    Example:
        >>> page_name(2, "https://cdn.example.com/2.jpg?v=1")
        '002.jpg'
    """
    suffix = PurePosixPath(urlsplit(url).path).suffix
    return f"{number:03d}{suffix or '.webp'}"


def cover_name(url: str) -> str:
    """The cover's name, `cover.webp`, keeping the URL's extension when it has one.

    Args:
        url: the cover's image URL.

    Returns:
        The name, extension included.

    Example:
        >>> cover_name("https://cdn.example.com/c.png")
        'cover.png'
    """
    suffix = PurePosixPath(urlsplit(url).path).suffix
    return f"cover{suffix or '.webp'}"


def _chapter_files(site: Site, chapter_id: Any) -> list[File]:
    """A chapter's pages, from its own request: a lazy chapter post's files."""
    body = site.fetch(f"{API}chapters/{chapter_id}").json
    return _chapter_pages(body) if isinstance(body, dict) else []


def _chapter_pages(body: Mapping[str, Any]) -> list[File]:
    """The page images of a chapter's JSON, in order, each named by its place."""
    pages = body.get("pages") or []
    return [File(page["image_url"], name=page_name(number, page["image_url"]))
            for number, page in enumerate(pages, start=1)
            if isinstance(page, dict) and page.get("image_url")]


# ---------------------------------------------------------------------------------------------
# Searches and name lookups
# ---------------------------------------------------------------------------------------------

def _search(site: Site, url: str, params: Mapping[str, str]) -> Iterator[dict[str, Any]]:
    """Each manga item of a paged search, the pages chained by `next_cursor`.

    `cursor_res=true` asks the site for cursor paging; without it the site pages by number.
    `site.pages` sends `params` alone first, then each page's `next_cursor` as `cursor`, and
    ends on an empty or repeated cursor.

    Args:
        site: the target's site.
        url: the search endpoint.
        params: the search's query.

    Yields:
        The items that have an id, in the site's order.
    """
    for page in site.pages(url, params={**params, "cursor_res": "true"}, cursor="next_cursor",
                           send="cursor"):
        items = page.get("items") if isinstance(page, dict) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and "id" in item:
                yield item


def _items(body: Any) -> list[dict[str, Any]]:
    """The items of a list answer: a bare list, or one under `items` or `data`.

    Args:
        body: the answer's parsed JSON.

    Returns:
        The items that are objects; none for an answer of another shape.
    """
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = body.get("items") or body.get("data") or []
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def _named(body: Any) -> Iterator[tuple[str, str]]:
    """`(name, id)` of each item of a list answer that has both."""
    for item in _items(body):
        label = str(item.get("name") or "")
        if label and "id" in item:
            yield label, str(item["id"])


def lookup_genres(site: Site, name: str) -> Iterator[tuple[str, str]]:
    """Every genre's `(name, id)`, from the site's whole genre list (`name` is not sent)."""
    yield from _named(site.fetch(f"{API}genres").json)


def lookup_authors(site: Site, name: str) -> Iterator[tuple[str, str]]:
    """The `(name, id)` of each author the site's author search finds for `name`."""
    yield from _named(site.fetch(f"{API}authors/search", params={"q": name}).json)


def lookup_parodies(site: Site, name: str) -> Iterator[tuple[str, str]]:
    """The `(name, id)` of each parody the site's parody search finds for `name`."""
    yield from _named(site.fetch(f"{API}parodies/search", params={"q": name}).json)


def _fold(text: str) -> str:
    """`text` for comparing names: accents dropped (đ as d), case folded, spaces collapsed."""
    norm = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    stripped = "".join(ch for ch in norm if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def _resolve_id(site: Site, kind: str, raw: str, lookup: Lookup) -> str:
    """The site's id of a genre, author or parody, from its id or its name.

    A name matches with case and accents ignored. Links spell spaces as hyphens, so a name
    with hyphens is looked up with spaces first, then as written (seen 2026-11-14).

    Args:
        site: the target's site.
        kind: what is looked up, for the error: `genre`, `author` or `parody`.
        raw: the id or name, %-encoded as a link may have it.
        lookup: the site's lookup for that kind.

    Returns:
        The id, as text.

    Raises:
        UsageError: no item of that kind has the name.
    """
    value = unquote(raw).strip()
    if value.isdigit():
        return value
    candidates = [value.replace("-", " "), value] if "-" in value else [value]
    for candidate in candidates:
        found = list(lookup(site, candidate))
        # The hyphens-as-spaces search may find the name as written too.
        for wanted in dict.fromkeys((_fold(candidate), _fold(value))):
            matches = [item_id for label, item_id in found if _fold(label) == wanted]
            if matches:
                return matches[0]
    raise UsageError(f"mht: no {kind} named {value}")


def _parse_query(site: Site, raw: str) -> tuple[str | None, dict[str, str], list[set[int]]]:
    """A query's text as a title search, or as an advanced search's params.

    Words are split as a shell splits them, so a quoted value may have spaces. A word without
    `:` is part of the title. `title:`, `author:`, `genre:`, `-genre:`, `parody:`,
    `character:` and `chapters:<n>` are terms; names are turned into the site's ids.

    Args:
        site: the target's site, for the name lookups.
        raw: the query.

    Returns:
        `(words, {}, [])` for a query of plain words, or `(None, params, chapter_filters)`
        for one with terms: each filter is the set of chapter counts a result may have.

    Raises:
        UsageError: the query is empty, a term has no value, or a term is unknown.
    """
    stripped = raw.strip()
    if not stripped:
        raise UsageError("mht: the query is empty")
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        # An unclosed quote: the words as typed.
        tokens = stripped.split()
    titles: list[str] = []
    buckets: dict[str, list[str]] = {
        "author": [], "character": [], "parody": [], "genre": [], "exclude_genre": [],
    }
    chapter_filters: list[set[int]] = []
    has_terms = False
    for token in tokens:
        if ":" not in token:
            titles.append(token)
            continue
        has_terms = True
        negated = token.startswith("-")
        key, _, val = (token[1:] if negated else token).partition(":")
        if not val:
            raise UsageError(f"mht: empty value for {key}")
        if key == "title" and not negated:
            titles.append(val)
        elif key == "chapters" and not negated and val.isdigit():
            chapter_filters.append({int(val)})
        elif key == "genre":
            bucket = "exclude_genre" if negated else "genre"
            buckets[bucket].append(_resolve_id(site, "genre", val, lookup_genres))
        elif key == "author" and not negated:
            buckets["author"].append(_resolve_id(site, "author", val, lookup_authors))
        elif key == "parody" and not negated:
            buckets["parody"].append(_resolve_id(site, "parody", val, lookup_parodies))
        elif key == "character" and not negated:
            buckets["character"].append(val)
        else:
            raise UsageError(f"mht: unsupported query term {token!r}")
    if not has_terms:
        return " ".join(titles), {}, []
    params = {key: ",".join(values) for key, values in buckets.items() if values}
    title = " ".join(titles).strip()
    if title:
        params["title"] = title
    return None, params, chapter_filters
