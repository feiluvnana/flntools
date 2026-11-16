"""mimihentai (`mht`): routes, chapters, covers, author/genre links, and query targets."""

import unittest

from fakes import FakeSite, Seen, collect
from test_engine import EngineCase

from flnd import _net
from flnd.engine import Engine, resolve
from flnd.options import Options, categories_by_source, validate
from flnd.source import SiteError, Target, UsageError
from flnd.sources.mht import (
    API, Mimi, _resolve_id, cover_name, format_order, lookup_genres, page_name,
)

CDN = "https://moe-cdn.example"

# Every form mht reads, as people write it, and what it becomes: (kind, value), or a refusal's text.
ROUTES = [
    ("mht:70982", ("manga", "70982")),
    ("mht:https://mimihentai.moe/manga/70982?ref=1", ("manga", "70982")),
    ("https://mimihentai.moe/manga/70982", ("manga", "70982")),
    ("mimihentai.moe/manga/70982", ("manga", "70982")),
    ("https://www.mimihentai.moe/manga/70982/", ("manga", "70982")),
    ("https://mimihentai.moe/manga/70982/chapter/1001", ("chapter", "1001")),
    ("mimihentai.moe/manga/70982/chapter/1001/", ("chapter", "1001")),
    ("https://mimihentai.moe/author/7", ("author", "7")),
    ("https://mimihentai.moe/authors/example_creator", ("author", "example_creator")),
    ("https://mimihentai.moe/genre/12", ("genre", "12")),
    ("https://mimihentai.moe/genres/example", ("genre", "example")),
    ("mht?genre:vanilla", ("query", "genre:vanilla")),
    ("mht:?genre:vanilla", ("query", "genre:vanilla")),
    ('mht?author:"example creator" chapters:1', ("query", 'author:"example creator" chapters:1')),
    ("mht?example words", ("query", "example words")),
    ("mht?", "the query is empty"),
    ("mht:abc", "can't read"),
    ("https://mimihentai.moe/", "can't read"),
]

MANGA = {
    "id": 70982,
    "title": "Example Manga",
    "cover_url": f"{CDN}/covers/70982.webp",
    "last_updated": "2026-11-10T12:00:00Z",
}

CHAPTERS = [
    {"id": 1001, "title": "First Chapter", "order": 1, "created_at": "2026-11-01T10:00:00Z",
     "manga_id": 70982},
    {"id": 1002, "title": "Bonus", "order": 1.5, "created_at": "2026-11-02T10:00:00Z",
     "manga_id": 70982},
]

CHAPTER_1001 = {
    "info": CHAPTERS[0],
    "pages": [{"image_url": f"{CDN}/1001/1.webp"}, {"image_url": f"{CDN}/1001/2.jpg"}],
}

CHAPTER_1002 = {
    "info": CHAPTERS[1],
    "pages": [{"image_url": f"{CDN}/1002/1.webp"}],
}


def fixtures(**changed):
    answers = {
        f"{API}manga/70982": MANGA,
        f"{API}manga/70982/chapters": CHAPTERS,
        f"{API}chapters/1001": CHAPTER_1001,
        f"{API}chapters/1002": CHAPTER_1002,
    }
    answers.update({f"{API}{path}": answer for path, answer in changed.items()})
    return answers


def query_site(answers):
    """A FakeSite for a query target, and what it saw."""
    seen = Seen()
    return FakeSite(Target("mht?x", kind="query"), json=answers, seen=seen), seen


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in ROUTES:
            with self.subTest(text=text):
                if isinstance(expected, tuple):
                    found = resolve(text, {"mht": Mimi})
                    self.assertEqual((found.kind, found.value), expected)
                    self.assertEqual(found.source, "mht")
                else:
                    with self.assertRaisesRegex(UsageError, f"^mht: {expected}"):
                        resolve(text, {"mht": Mimi})

    def test_a_link_elsewhere_is_not_mht(self):
        with self.assertRaises(UsageError):
            Mimi().resolve("https://example.com/manga/70982")

    def test_author_and_genre_links_take_only(self):
        """They discover manga, so `--only` applies to each manga they find (a fixed bug)."""
        for link in ("https://mimihentai.moe/author/7", "https://mimihentai.moe/genre/12"):
            with self.subTest(link=link):
                found = resolve(link, {"mht": Mimi})
                self.assertEqual(categories_by_source("chapters", [found], {"mht": Mimi}),
                                 {"mht": ("chapters",)})
                validate({"mht": Mimi}, "chapters", [found])

    def test_a_chapter_link_takes_no_only(self):
        found = resolve("https://mimihentai.moe/manga/70982/chapter/1001", {"mht": Mimi})
        with self.assertRaisesRegex(UsageError, "one post"):
            categories_by_source("chapters", [found], {"mht": Mimi})


class HelpersTest(unittest.TestCase):
    def test_format_order(self):
        self.assertEqual(format_order(1), "001")
        self.assertEqual(format_order(12), "012")
        self.assertEqual(format_order(1.0), "001")
        self.assertEqual(format_order(1.5), "1.5")
        self.assertEqual(format_order("3"), "003")
        self.assertEqual(format_order(None), "")
        self.assertEqual(format_order(True), "")

    def test_names(self):
        self.assertEqual(page_name(1, f"{CDN}/1.webp"), "001.webp")
        self.assertEqual(page_name(2, f"{CDN}/2.jpg?v=1"), "002.jpg")
        self.assertEqual(page_name(3, f"{CDN}/noext"), "003.webp")
        self.assertEqual(cover_name(f"{CDN}/cover.png"), "cover.png")
        self.assertEqual(cover_name(f"{CDN}/cover"), "cover.webp")


class MangaAndChapterTest(unittest.TestCase):
    def test_manga_chapters_and_cover_layout_and_keys(self):
        saved = collect(Mimi, "mht:70982", json=fixtures(), options=Options(categories=("all",)))
        self.assertEqual(
            [(item.path, item.key) for item in saved],
            [
                ("chapters/001_First_Chapter/001.webp", "1001_0"),
                ("chapters/001_First_Chapter/002.jpg", "1001_1"),
                ("chapters/1.5_Bonus/001.webp", "1002_0"),
                ("cover/cover.webp", "70982_cover"),
            ],
        )
        self.assertEqual(saved[0].post.date, "2026-11-01T10:00:00Z")
        self.assertEqual(saved[-1].post.date, "2026-11-10T12:00:00Z")
        self.assertEqual(saved[-1].post.fields["title"], "Example Manga")

    def test_only_filters_categories(self):
        chapters_only = collect(Mimi, "mht:70982", json=fixtures(),
                                options=Options(categories=("chapters",)))
        self.assertEqual([item.key for item in chapters_only], ["1001_0", "1001_1", "1002_0"])

        seen = Seen()
        cover_only = collect(Mimi, "mht:70982", json=fixtures(), seen=seen,
                             options=Options(categories=("cover",)))
        self.assertEqual([item.path for item in cover_only], ["cover/cover.webp"])
        # The cover alone asks for no chapter.
        self.assertEqual([url.removeprefix(API) for _, url, _ in seen.requests], ["manga/70982"])

    def test_chapters_are_lazy(self):
        site = FakeSite(Target("mht:70982", kind="manga"), json=fixtures())
        posts = list(Mimi().chapters(site, {**MANGA, "id": 70982}))
        self.assertTrue(all(post.lazy for post in posts))
        self.assertEqual([post.fields["order"] for post in posts], ["001", "1.5"])

    def test_single_chapter_route_uses_same_key_and_folder(self):
        saved = collect(Mimi, "https://mimihentai.moe/manga/70982/chapter/1001", json=fixtures())
        self.assertEqual(
            [(item.path, item.key) for item in saved],
            [("chapters/001_First_Chapter/001.webp", "1001_0"),
             ("chapters/001_First_Chapter/002.jpg", "1001_1")],
        )

    def test_missing_manga_or_chapter_raises_not_found(self):
        site = FakeSite(Target("mht:999", kind="manga"), json={f"{API}manga/999": {}})
        with self.assertRaisesRegex(SiteError, "manga 999"):
            Mimi().manga(site, "999")
        site = FakeSite(Target("mht:999", kind="chapter"), json={f"{API}chapters/999": {}})
        with self.assertRaisesRegex(SiteError, "chapter 999"):
            list(Mimi().chapter(site, "999"))


def manga(*ids):
    return [Target(f"mht:{id}", kind="manga") for id in ids]


class AuthorGenreAndQueryTest(unittest.TestCase):
    def test_simple_search_follows_the_cursor(self):
        def search(url, request):
            params = request["params"]
            self.assertEqual((params["q"], params["cursor_res"]), ("example words", "true"))
            if not params.get("cursor"):
                return {"items": [{"id": 10}, {"id": 20}], "next_cursor": "c2", "total": 3}
            self.assertEqual(params["cursor"], "c2")
            return {"items": [{"id": 30}], "next_cursor": None, "total": 3}

        site, seen = query_site({f"{API}manga/search": search})
        self.assertEqual(list(Mimi().query(site, "example words")), manga(10, 20, 30))
        self.assertEqual(len(seen.requests), 2)
        # The first request sends the search's params alone, without a cursor.
        self.assertNotIn("cursor", seen.requests[0][2]["params"])

    def test_advanced_search_lookups_character_and_chapters_filter(self):
        captured = []

        def advanced(url, request):
            captured.append(dict(request["params"]))
            return {"items": [{"id": 100, "chapter_count": 1}, {"id": 101, "chapter_count": 3}],
                    "next_cursor": ""}

        site, _ = query_site({
            f"{API}genres": [{"id": 1, "name": "Vanilla"}, {"id": 2, "name": "Romance"},
                             {"id": 9, "name": "Example"}],
            f"{API}authors/search": lambda url, request: [{"id": 7, "name": "example_creator"}],
            f"{API}parodies/search": lambda url, request: [{"id": 55, "name": "example_parody"}],
            f"{API}manga/advanced-search": advanced,
        })
        found = list(Mimi().query(
            site,
            "genre:vanilla genre:romance -genre:example author:example_creator "
            "character:example_char parody:example_parody title:extra chapters:3",
        ))
        self.assertEqual(found, manga(101))
        self.assertEqual(captured, [{
            "title": "extra", "author": "7", "character": "example_char", "parody": "55",
            "genre": "1,2", "exclude_genre": "9", "cursor_res": "true",
        }])

    def test_bad_queries_are_refused(self):
        site, _ = query_site({})
        for query, message in (("   ", "the query is empty"), ("author:", "empty value"),
                               ("-author:x", "unsupported query term")):
            with self.subTest(query=query), self.assertRaisesRegex(UsageError, message):
                list(Mimi().query(site, query))

    def test_author_and_genre_page_routes(self):
        site, _ = query_site({
            f"{API}genres": [{"id": 3, "name": "Romance"}],
            f"{API}authors/search": lambda url, request: [{"id": 7, "name": "example creator"}],
            f"{API}manga/advanced-search": lambda url, request: {
                "items": [{"id": 70982}] if request["params"].get("author") == "7"
                else [{"id": 70983}],
                "next_cursor": None,
            },
        })
        self.assertEqual(list(Mimi().author(site, "7")), manga(70982))
        self.assertEqual(list(Mimi().author(site, "example-creator")), manga(70982))
        self.assertEqual(list(Mimi().genre(site, "romance")), manga(70983))
        with self.assertRaisesRegex(UsageError, "no genre named missing"):
            list(Mimi().genre(site, "missing"))

    def test_names_match_without_case_or_accents(self):
        site, _ = query_site({f"{API}genres": [{"id": 4, "name": "Đô Thị"}]})
        self.assertEqual(_resolve_id(site, "genre", "do%20thi", lookup_genres), "4")
        self.assertEqual(_resolve_id(site, "genre", "DO-THI", lookup_genres), "4")


class MimiEngineTest(EngineCase):
    def test_query_and_lazy_chapter_skip_through_engine(self):
        genre_calls = 0

        def genres_route(request):
            nonlocal genre_calls
            genre_calls += 1
            return _net.reply(200, json=[{"id": 1, "name": "Vanilla"}])

        self.routes[f"{API}genres"] = genres_route
        self.routes[f"{API}manga/advanced-search"] = _net.reply(
            200, json={"items": [{"id": 70982}], "next_cursor": None})
        self.routes[f"{API}manga/70982"] = _net.reply(200, json=MANGA)
        self.routes[f"{API}manga/70982/chapters"] = _net.reply(200, json=[CHAPTERS[0]])
        self.routes[f"{API}chapters/1001"] = _net.reply(200, json={
            "info": CHAPTERS[0], "pages": [{"image_url": f"{CDN}/1001/1.webp"}],
        })
        self.routes[f"{CDN}/1001/1.webp"] = _net.reply(200, content=b"WEBP1")
        self.routes[f"{CDN}/covers/70982.webp"] = _net.reply(200, content=b"COVER1")

        engine = Engine(home=self.home, sources={"mht": Mimi})
        report = engine.download(["mht?genre:vanilla"], categories="all", limit=1)
        self.assertTrue(report.ok)
        self.assertEqual(genre_calls, 1)
        folder = self.target_dir("mht", "70982")
        self.assertTrue((folder / "chapters" / "001_First_Chapter" / "001.webp").exists())
        self.assertTrue((folder / "cover" / "cover.webp").exists())

        # A second run: the saved chapter's pages are not asked for again.
        del self.routes[f"{API}chapters/1001"]
        self.requests.clear()
        report = engine.download(["mht?genre:vanilla"], categories="all", limit=1)
        self.assertTrue(report.ok)
        self.assertEqual(genre_calls, 2)
        self.assertNotIn(f"{API}chapters/1001",
                         [str(request.url).split("?")[0] for request in self.requests])

    def test_cover_query_reuses_search_metadata_without_manga_api(self):
        self.routes[f"{API}genres"] = _net.reply(200, json=[{"id": 1, "name": "Vanilla"}])
        self.routes[f"{API}manga/advanced-search"] = _net.reply(
            200, json={"items": [MANGA], "next_cursor": None})
        self.routes[f"{CDN}/covers/70982.webp"] = _net.reply(200, content=b"COVER1")

        engine = Engine(home=self.home, sources={"mht": Mimi})
        report = engine.download(["mht?genre:vanilla"], categories="cover", limit=1)
        self.assertTrue(report.ok)
        self.assertTrue((self.target_dir("mht", "70982") / "cover" / "cover.webp").exists())
        self.assertNotIn(f"{API}manga/70982",
                         [str(request.url).split("?")[0] for request in self.requests])

    def test_an_author_link_downloads_only_the_named_category(self):
        """`--only chapters` on an author link reaches each manga it finds (a fixed bug)."""
        self.routes[f"{API}manga/advanced-search"] = _net.reply(
            200, json={"items": [MANGA], "next_cursor": None})
        self.routes[f"{API}manga/70982/chapters"] = _net.reply(200, json=[CHAPTERS[0]])
        self.routes[f"{API}chapters/1001"] = _net.reply(200, json={
            "info": CHAPTERS[0], "pages": [{"image_url": f"{CDN}/1001/1.webp"}],
        })
        self.routes[f"{CDN}/1001/1.webp"] = _net.reply(200, content=b"WEBP1")

        engine = Engine(home=self.home, sources={"mht": Mimi})
        report = engine.download(["https://mimihentai.moe/author/7"], categories="chapters")
        self.assertTrue(report.ok, report)
        folder = self.target_dir("mht", "70982")
        self.assertTrue((folder / "chapters" / "001_First_Chapter" / "001.webp").exists())
        self.assertFalse((folder / "cover").exists())


if __name__ == "__main__":
    unittest.main()
