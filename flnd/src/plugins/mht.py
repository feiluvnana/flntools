"""mimihentai (mht): manga chapters and covers, one chapter by its link, and the manga an author,
a genre or a search finds, from the site's public JSON API. See SPEC.md#mht-mimihentai.
"""

from __future__ import annotations

import shlex
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import unquote

from flnd.plugin import (
    Browser,
    Ctx,
    File,
    NotFound,
    Plugin,
    Post,
    Request,
    Target,
    UsageError,
    numbered,
    route,
)

API = "https://mimihentai.moe/api/"
# A lookup: the site's `(name, id)` pairs for a name, from one of its search or list endpoints.
Lookup = Callable[[Ctx, str], Awaitable[list[tuple[str, str]]]]


class Mimi(Plugin):
    """mimihentai's manga, chapters and covers, and the manga an author, genre or search finds."""

    code = "mht"
    name = "mimihentai"
    domains = ("mimihentai.moe",)
    api = 2
    # Everything it reads is public JSON: no login, no tab, so no walls.
    browser = Browser.NONE
    # The site publishes no limit, and each chapter costs a request: half a second keeps a long
    # manga gentle on its API.
    pace = 0.5
    folder = "chapters/{order} {title}"
    fields = ("title", "order", "author", "genres", "chapters")
    # Every page and the cover are named here (`File.name`, the extension from the content), so
    # this is never used.
    filename = "{post}"
    # `<chapter id>_<n>`, `n` the page's place from 0: the keys the history has always had.
    key = "{post}_{num}"

    def __init__(self) -> None:
        # Search items carrying a manga's title and cover, by manga id, so a manga a search
        # found is not fetched again: one instance serves a run's discovered targets.
        self._cached: dict[str, dict[str, Any]] = {}

    # The chapter route comes first: the manga link pattern would also read a chapter's link.
    # Its value keeps its `chapter/`, so its key, `mht:chapter/<id>`, reads back as a chapter,
    # never as manga `<id>`; API 1's row of one (`mht:<id>`, of kind chapter) still runs.
    @route((r"mht:chapter/{id:\d+}", "chapter/{id}"),
           (r"mimihentai.moe/manga/*/chapter/{id:\d+}", "chapter/{id}"),
           single=True, newest_first=False)
    async def chapter(self, ctx: Ctx, id: str) -> AsyncIterator[Post]:
        """One chapter: `mht:chapter/1001`, or its link,
        `https://mimihentai.moe/manga/70982/chapter/1001`.

        Raises:
            NotFound: the API has no such chapter.
        """
        id = id.removeprefix("chapter/")
        body = (await ctx.fetch(f"{API}chapters/{id}")).json
        info = body.get("info") if isinstance(body, dict) else None
        if not isinstance(info, dict) or "id" not in info:
            raise NotFound(f"mht: chapter {id} isn't there")
        yield Post(info["id"], _chapter_pages(body), date=info.get("created_at"),
                   order=format_order(info.get("order")), title=info.get("title") or "")

    @route(r"mht:{id:\d+}", r"mimihentai.moe/manga/{id:\d+}", newest_first=False)
    async def manga(self, ctx: Ctx, id: str) -> dict[str, Any]:
        """One manga: `mht:70982`, or `https://mimihentai.moe/manga/70982`.

        Returns:
            The manga's JSON, its `id` set, for the categories.

        Raises:
            NotFound: the API has no such manga, or one without a title.
        """
        cached = self._cached.get(id)
        if cached is not None:
            return {**cached, "id": cached.get("id") or id}
        body = (await ctx.fetch(f"{API}manga/{id}")).json
        if not isinstance(body, dict) or not body.get("title"):
            raise NotFound(f"mht: manga {id} isn't there")
        return {**body, "id": body.get("id") or id}

    @manga.category(own=True)
    async def chapters(self, ctx: Ctx, manga: dict[str, Any]) -> AsyncIterator[Post]:
        """Every chapter, in the API's order, each a lazy post dated by its `created_at`."""
        # The API gives every chapter in one answer whatever `limit` says (seen 2026-11-14).
        asked = Request(f"{API}manga/{manga['id']}/chapters", params={"limit": 100})
        items = (await ctx.fetch(asked)).json
        for item in items if isinstance(items, list) else ():
            if not isinstance(item, dict) or "id" not in item:
                continue
            chapter_id = item["id"]

            async def files(chapter_id: Any = chapter_id) -> list[File]:
                body = (await ctx.fetch(f"{API}chapters/{chapter_id}")).json
                return _chapter_pages(body) if isinstance(body, dict) else []

            yield Post(chapter_id, files, date=item.get("created_at"),
                       order=format_order(item.get("order")), title=item.get("title") or "")

    @manga.category(own=True, folder="cover", key="{post}_cover")
    async def cover(self, ctx: Ctx, manga: dict[str, Any]) -> AsyncIterator[Post]:
        """The manga's cover, keyed `<manga id>_cover`, dated by the manga's last update."""
        url = manga.get("cover_url")
        if url:
            yield Post(manga["id"], [File(url, name="cover")],
                       date=manga.get("last_updated"), **_manga_fields(manga))

    # An author's and a genre's value keep their `author/` or `genre/`, so the key,
    # `mht:<value>`, is never a manga's, and the text reads back through its route.
    @route(("mht:author/{author:[^/]+}", "author/{author}"),
           ("mimihentai.moe/author/{author:[^/]+}", "author/{author}"),
           ("mimihentai.moe/authors/{author:[^/]+}", "author/{author}"),
           discovers=True, newest_first=False)
    async def author(self, ctx: Ctx, author: str) -> AsyncIterator[Target]:
        """Every manga of an author, by name or id: `mht:author/example_creator`, or
        `https://mimihentai.moe/author/7`.

        Raises:
            UsageError: no author has that name.
        """
        author_id = await _resolve_id(ctx, "author", author.removeprefix("author/"), _authors)
        async for item in _search(ctx, "manga/advanced-search", {"author": author_id}):
            yield self._found(item)

    @route(("mht:genre/{genre:[^/]+}", "genre/{genre}"),
           ("mimihentai.moe/genre/{genre:[^/]+}", "genre/{genre}"),
           ("mimihentai.moe/genres/{genre:[^/]+}", "genre/{genre}"),
           discovers=True, newest_first=False)
    async def genre(self, ctx: Ctx, genre: str) -> AsyncIterator[Target]:
        """Every manga of a genre, by name or id: `mht:genre/example`, or
        `https://mimihentai.moe/genres/12`.

        Raises:
            UsageError: no genre has that name.
        """
        genre_id = await _resolve_id(ctx, "genre", genre.removeprefix("genre/"), _genres)
        async for item in _search(ctx, "manga/advanced-search", {"genre": genre_id}):
            yield self._found(item)

    @route("mht?{query}", newest_first=False)
    async def query(self, ctx: Ctx, query: str) -> AsyncIterator[Target]:
        """Every manga a search finds: `mht?example words`, or `mht?genre:example chapters:1`.

        Raises:
            UsageError: the query is empty, has an unknown term, or names nothing the site has.
        """
        words, params, chapter_filters = await _parse_query(ctx, query)
        if words is not None:
            items = _search(ctx, "manga/search", {"q": words})
        else:
            items = _search(ctx, "manga/advanced-search", params)
        async for item in items:
            # The site can't filter by chapter count, so each result is checked here.
            if all(item.get("chapter_count") in wanted for wanted in chapter_filters):
                yield self._found(item)

    def _found(self, item: dict[str, Any]) -> Target:
        """A search item as its manga's target, kept when it holds all `manga` reads."""
        manga_id = str(item["id"])
        if item.get("title") and item.get("cover_url"):
            self._cached[manga_id] = item
        return Target(f"mht:{manga_id}", kind="manga")


# ---------------------------------------------------------------------------------------------
# Names and fields
# ---------------------------------------------------------------------------------------------

def _manga_fields(manga: Mapping[str, Any]) -> dict[str, Any]:
    """A manga's `title`, `author` and `genres` (names joined by commas) and `chapters` (its
    chapter count, "" when unknown), raw: the engine makes them safe."""
    authors = ", ".join(str(a["name"]) for a in manga.get("authors") or ()
                        if isinstance(a, dict) and a.get("name"))
    genres = ", ".join(str(g["name"]) for g in manga.get("genres") or ()
                       if isinstance(g, dict) and g.get("name"))
    count = manga.get("chapter_count")
    return {
        "title": manga.get("title") or "",
        "author": authors or str(manga.get("author") or ""),
        "genres": genres,
        "chapters": count if count is not None else "",
    }


def format_order(value: Any) -> str:
    """A chapter's number as its folder shows it: `007` when whole, `7.5` as text, "" for none.

    The API gives it as a whole number, a float or text (seen 2026-11-14).
    """
    if value is None or value == "" or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return numbered(value, width=3)
    if isinstance(value, float):
        return numbered(int(value), width=3) if value.is_integer() else str(value)
    text = str(value).strip()
    return numbered(int(text), width=3) if text.isdigit() else text


def _chapter_pages(body: Mapping[str, Any]) -> list[File]:
    """The page images of a chapter's JSON, in order, each named by its place from 1 (`001`);
    a page without an image keeps its number, so the next pages keep theirs."""
    return [File(page["image_url"], name=numbered(number, width=3))
            for number, page in enumerate(body.get("pages") or (), 1)
            if isinstance(page, dict) and page.get("image_url")]


# ---------------------------------------------------------------------------------------------
# Searches and name lookups
# ---------------------------------------------------------------------------------------------

async def _search(ctx: Ctx, path: str, params: Mapping[str, str]) -> AsyncIterator[dict[str, Any]]:
    """Each manga item of a search, its pages chained by `next_cursor`."""
    first = Request(f"{API}{path}", params={**params, "cursor_res": "true"})
    async for page in ctx.pages(first, cursor="next_cursor", send="cursor"):
        body = page.json
        items = body.get("items") if isinstance(body, dict) else None
        for item in items if isinstance(items, list) else ():
            if isinstance(item, dict) and "id" in item:
                yield item


def _pairs(body: Any) -> list[tuple[str, str]]:
    """`(name, id)` of each item of a list answer (bare, or under `items` or `data`)."""
    if isinstance(body, dict):
        body = body.get("items") or body.get("data") or []
    return [(str(item["name"]), str(item["id"])) for item in body if isinstance(item, dict)
            and item.get("name") and "id" in item] if isinstance(body, list) else []


async def _genres(ctx: Ctx, name: str) -> list[tuple[str, str]]:
    """Every genre's `(name, id)`, from the site's whole genre list (`name` is not sent)."""
    return _pairs((await ctx.fetch(f"{API}genres")).json)


async def _authors(ctx: Ctx, name: str) -> list[tuple[str, str]]:
    return _pairs((await ctx.fetch(Request(f"{API}authors/search", params={"q": name}))).json)


async def _parodies(ctx: Ctx, name: str) -> list[tuple[str, str]]:
    return _pairs((await ctx.fetch(Request(f"{API}parodies/search", params={"q": name}))).json)


def _fold(text: str) -> str:
    """`text` for comparing names: accents dropped (đ as d), case folded, spaces collapsed."""
    norm = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    stripped = "".join(ch for ch in norm if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


async def _resolve_id(ctx: Ctx, kind: str, raw: str, lookup: Lookup) -> str:
    """The site's id of a genre, author or parody, from its id or its %-encoded name."""
    value = unquote(raw).strip()
    if value.isdigit():
        return value
    candidates = [value.replace("-", " "), value] if "-" in value else [value]
    for candidate in candidates:
        found = await lookup(ctx, candidate)
        # The hyphens-as-spaces search may find the name as written too.
        for wanted in dict.fromkeys((_fold(candidate), _fold(value))):
            matches = [item_id for label, item_id in found if _fold(label) == wanted]
            if matches:
                return matches[0]
    raise UsageError(f"mht: no {kind} named {value}")


# A query term's bucket in the advanced search, and the lookup turning its names into ids.
TERMS: dict[str, tuple[str, Lookup | None]] = {
    "author": ("author", _authors), "character": ("character", None),
    "parody": ("parody", _parodies), "genre": ("genre", _genres),
    "-genre": ("exclude_genre", _genres),
}


async def _parse_query(ctx: Ctx, raw: str) -> tuple[str | None, dict[str, str], list[set[int]]]:
    """A query as a title search, or as an advanced search's params; see the docs' Queries."""
    stripped = raw.strip()
    if not stripped:
        raise UsageError("mht: the query is empty")
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        # An unclosed quote: the words as typed.
        tokens = stripped.split()
    titles: list[str] = []
    buckets: dict[str, list[str]] = {bucket: [] for bucket, _ in TERMS.values()}
    chapter_filters: list[set[int]] = []
    has_terms = False
    for token in tokens:
        if ":" not in token:
            titles.append(token)
            continue
        has_terms = True
        term, _, val = token.partition(":")
        if not val:
            raise UsageError(f"mht: empty value for {term.removeprefix('-')}")
        if term == "title":
            titles.append(val)
        elif term == "chapters" and val.isdigit():
            chapter_filters.append({int(val)})
        elif term in TERMS:
            bucket, lookup = TERMS[term]
            kind = term.removeprefix("-")
            buckets[bucket].append(await _resolve_id(ctx, kind, val, lookup) if lookup else val)
        else:
            raise UsageError(f"mht: unsupported query term {token!r}")
    if not has_terms:
        return " ".join(titles), {}, []
    params = {bucket: ",".join(values) for bucket, values in buckets.items() if values}
    title = " ".join(titles).strip()
    if title:
        params["title"] = title
    return None, params, chapter_filters
