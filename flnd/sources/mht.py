"""mimihentai (mht): manga chapters, covers, and search queries from its public JSON API."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import Any, Iterator, Mapping
from urllib.parse import unquote, urlsplit

from flnd.source import FetchError, File, Post, Site, Source, Target, TargetError, pages, target
from shared import text

API = "https://mimihentai.moe/api/"


def lookup_genres(site: Site, name: str) -> Iterator[tuple[str, str]]:
    """Yield `(name, id)` pairs for every genre from mimihentai's genre list."""
    for item in _items_of(site.fetch(f"{API}genres").json):
        label = str(item.get("name") or "")
        if label and "id" in item:
            yield label, str(item["id"])


def lookup_authors(site: Site, name: str) -> Iterator[tuple[str, str]]:
    """Yield `(name, id)` pairs matching `name` from mimihentai's author search."""
    yield from _lookup_search(site, f"{API}authors/search", name)


def lookup_parodies(site: Site, name: str) -> Iterator[tuple[str, str]]:
    """Yield `(name, id)` pairs matching `name` from mimihentai's parody search."""
    yield from _lookup_search(site, f"{API}parodies/search", name)


class Mimi(Source):
    """mimihentai manga, chapters, covers, and search queries over its public JSON API."""

    code = "mht"
    name = "mimihentai"
    domains = ("mimihentai.moe",)
    pace = 0.5
    folder = "chapters/{order} {title}"
    filename = "{id}"
    key = "{id}_{num}"
    fields = ("title", "order", "author", "genres", "chapters")

    def __init__(self) -> None:
        self._cached: dict[str, dict[str, Any]] = {}

    @target(
        r"mimihentai.moe/manga/*/chapter/{id:\d+}",
        from_=(),
        folder="chapters/{order} {title}",
    )
    def chapter(self, site: Site, id: str) -> Iterator[Post]:
        """One chapter, by its URL: `https://mimihentai.moe/manga/70982/chapter/123`."""
        body = site.fetch(f"{API}chapters/{id}").json
        info = body.get("info") if isinstance(body, dict) else None
        if not isinstance(info, dict) or "id" not in info:
            raise FetchError(f"chapter {id} isn't there")
        yield Post(info["id"], _chapter_pages(body), date=info.get("created_at"),
                   order=format_order(info.get("order")), title=info.get("title") or "")

    @target(
        r"{id:\d+}",
        r"mimihentai.moe/manga/{id:\d+}",
        from_=("chapters", "cover"),
        folder="chapters/{order} {title}",
    )
    def manga(self, site: Site, id: str) -> dict[str, Any]:
        """One manga, by id or link: `mht:70982` or `https://mimihentai.moe/manga/70982`."""
        if (cached := self._cached.get(id)) is not None:
            return {**cached, "id": cached.get("id") or id}
        body = site.fetch(f"{API}manga/{id}").json
        if not isinstance(body, dict) or not body.get("title"):
            raise FetchError(f"manga {id} isn't there")
        return {**body, "id": body.get("id") or id}

    @manga.category
    def chapters(self, site: Site, manga: dict[str, Any]) -> Iterator[Post]:
        """Every chapter of `manga` as a lazy post dated by `created_at`."""
        # The API returns every chapter in one response regardless of `limit`.
        items = site.fetch(f"{API}manga/{manga['id']}/chapters", params={"limit": 100}).json
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict) or "id" not in item:
                continue
            chapter_id = item["id"]
            yield Post(chapter_id, lambda cid=chapter_id: _chapter_files(site, cid),
                       date=item.get("created_at"), order=format_order(item.get("order")),
                       title=item.get("title") or "")

    @manga.category(folder="cover", key="{id}_cover")
    def cover(self, site: Site, manga: dict[str, Any]) -> Iterator[Post]:
        """The manga's cover image, saved once."""
        url = manga.get("cover_url")
        if url:
            yield Post(
                manga["id"], [File(url, name=cover_name(url))],
                date=manga.get("last_updated"), **_manga_fields(manga),
            )

    @target(
        r"mimihentai.moe/author/{author:[^/]+}",
        r"mimihentai.moe/authors/{author:[^/]+}",
        from_=(),
        folder="chapters/{order} {title}",
    )
    def author(self, site: Site, author: str) -> Iterator[Target]:
        """Yield `mht:<id>` manga targets for an author page link."""
        author_id = _resolve_id(site, "author", author, lookup_authors)
        for item in _search_pages(site, f"{API}manga/advanced-search", {"author": author_id}):
            self._remember(item)
            yield Target(self.code, "manga", str(item["id"]))

    @target(
        r"mimihentai.moe/genre/{genre:[^/]+}",
        r"mimihentai.moe/genres/{genre:[^/]+}",
        from_=(),
        folder="chapters/{order} {title}",
    )
    def genre(self, site: Site, genre: str) -> Iterator[Target]:
        """Yield `mht:<id>` manga targets for a genre page link."""
        genre_id = _resolve_id(site, "genre", genre, lookup_genres)
        for item in _search_pages(site, f"{API}manga/advanced-search", {"genre": genre_id}):
            self._remember(item)
            yield Target(self.code, "manga", str(item["id"]))

    @target(
        "?{query}",
        discovers=True,
        from_=(),
        folder="chapters/{order} {title}",
    )
    def query(self, site: Site, query: str) -> Iterator[Target]:
        """Yield `mht:<id>` manga targets matching `query`."""
        text_only, params, chapter_filters = _parse_query(site, query)
        if text_only is not None:
            items = _search_pages(site, f"{API}manga/search", {"q": text_only})
        else:
            items = _search_pages(site, f"{API}manga/advanced-search", params)
        for item in items:
            if all(item.get("chapter_count") in wanted for wanted in chapter_filters):
                self._remember(item)
                yield Target(self.code, "manga", str(item["id"]))

    def _remember(self, item: Any) -> None:
        """Cache search items that already carry cover metadata to skip per-manga API calls."""
        if (
            isinstance(item, dict)
            and "id" in item
            and item.get("title")
            and item.get("cover_url")
        ):
            self._cached[str(item["id"])] = item


def _manga_fields(manga: Mapping[str, Any]) -> dict[str, Any]:
    """Extract folder-template metadata from a manga summary or detail dict."""
    authors = ", ".join(
        str(a.get("name", "")) for a in (manga.get("authors") or ())
        if isinstance(a, dict) and a.get("name")
    )
    genres = ", ".join(
        str(g.get("name", "")) for g in (manga.get("genres") or ())
        if isinstance(g, dict) and g.get("name")
    )
    return {
        "title": manga.get("title") or "",
        "author": authors or str(manga.get("author") or ""),
        "genres": genres,
        "chapters": manga.get("chapter_count") if manga.get("chapter_count") is not None else "",
    }


def format_order(value: Any) -> str:
    """Format a chapter order number as 3-digit zero-padded when whole, else as text."""
    if value is None or value == "" or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return f"{value:03d}"
    if isinstance(value, float):
        return f"{int(value):03d}" if value.is_integer() else str(value)
    text = str(value).strip()
    return f"{int(text):03d}" if text.isdigit() else text


def page_name(number: int, url: str) -> str:
    """A chapter page's filename, `001.webp`, keeping the URL's extension when present."""
    suffix = PurePosixPath(urlsplit(url).path).suffix
    return f"{number:03d}{suffix or '.webp'}"


def cover_name(url: str) -> str:
    """The cover's filename, `cover.webp`, keeping the URL's extension when present."""
    suffix = PurePosixPath(urlsplit(url).path).suffix
    return f"cover{suffix or '.webp'}"


def _chapter_files(site: Site, chapter_id: Any) -> list[File]:
    body = site.fetch(f"{API}chapters/{chapter_id}").json
    return _chapter_pages(body) if isinstance(body, dict) else []


def _chapter_pages(body: Mapping[str, Any]) -> list[File]:
    pages_data = body.get("pages") or []
    return [File(page["image_url"], name=page_name(number, page["image_url"]))
            for number, page in enumerate(pages_data, start=1)
            if isinstance(page, dict) and page.get("image_url")]


def _items_of(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = body.get("items") or body.get("data") or []
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def _lookup_search(site: Site, url: str, name: str) -> Iterator[tuple[str, str]]:
    for item in _items_of(site.fetch(url, params={"q": name}).json):
        label = str(item.get("name") or "")
        if label and "id" in item:
            yield label, str(item["id"])


def _resolve_id(site: Site, kind: str, raw: str, lookup: Any) -> str:
    """Resolve a tag, genre, author, or parody name (or numeric id) to its site id."""
    value = unquote(raw).strip()
    if value.isdigit():
        return value
    candidates = [value.replace("-", " "), value] if "-" in value else [value]
    for candidate in candidates:
        found = list(lookup(site, candidate))
        folded = text.fold(candidate)
        matches = [item_id for label, item_id in found if text.fold(label) == folded]
        if matches:
            return matches[0]
        if candidate != value:
            folded_raw = text.fold(value)
            matches = [item_id for label, item_id in found if text.fold(label) == folded_raw]
            if matches:
                return matches[0]
    raise TargetError(f"mht: no {kind} named {value}")


def _parse_query(
    site: Site, raw: str,
) -> tuple[str | None, dict[str, str], list[set[int]]]:
    """Parse `mht` query text into `(simple_q, advanced_params, chapter_filters)`."""
    stripped = raw.strip()
    if not stripped:
        raise TargetError("mht: the query is empty")
    try:
        tokens = shlex.split(stripped)
    except ValueError:
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
            raise TargetError(f"mht: empty value for {key}")
        if key == "title" and not negated:
            titles.append(val)
        elif key == "chapters" and not negated and val.isdigit():
            chapter_filters.append({int(val)})
        elif key == "genre":
            target_bucket = "exclude_genre" if negated else "genre"
            buckets[target_bucket].append(_resolve_id(site, "genre", val, lookup_genres))
        elif key == "author" and not negated:
            buckets["author"].append(_resolve_id(site, "author", val, lookup_authors))
        elif key == "parody" and not negated:
            buckets["parody"].append(_resolve_id(site, "parody", val, lookup_parodies))
        elif key == "character" and not negated:
            buckets["character"].append(val)
        else:
            raise TargetError(f"mht: unsupported query term {token!r}")
    if not has_terms:
        return " ".join(titles), {}, []
    params = {key: ",".join(vals) for key, vals in buckets.items() if vals}
    if title := " ".join(titles).strip():
        params["title"] = title
    return None, params, chapter_filters


def _search_pages(site: Site, url: str, params: Mapping[str, str]) -> Iterator[dict[str, Any]]:
    """Fetch items across paginated search results."""
    def fetch(cursor: str | None) -> Any:
        request_params = {**params, "cursor_res": "true"}
        if cursor:
            request_params["cursor"] = cursor
        return site.fetch(url, params=request_params).json

    def next_cursor(body: Any) -> str | None:
        return str(body.get("next_cursor") or "") if isinstance(body, dict) else None

    for page in pages(fetch, next_cursor):
        items = page.get("items") if isinstance(page, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and "id" in item:
                yield item
