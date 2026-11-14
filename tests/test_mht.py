"""mimihentai (`mht`): routes, chapters, covers, author/genre links, and query targets."""

import unittest

import httpx
from fakes import FakeSite, Seen, collect
from test_engine import EngineCase

from flnd import Engine
from flnd.options import Options
from flnd.query import plan
from flnd.source import FetchError, Target, TargetError
from flnd.sources.mht import API, Mimi, cover_name, format_order, page_name

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
    ("mht:?genre:vanilla", ("query", "genre:vanilla")),
    ('mht:?author:"example creator" chapters:1', ("query", 'author:"example creator" chapters:1')),
    ("mht:?example words", ("query", "example words")),
    ("mht:?", "the query is empty"),
    ("mht:?unknown:x", "'unknown:x': no parameter unknown"),
    ("mht:?-author:x", "'author:x': this site can't exclude by author"),
    ("mht:?chapters:>5", "'chapters:>5': this site can't compare chapters"),
    ("mht:abc", "can't read"),
    ("https://mimihentai.moe/", "can't read"),
    ("https://example.com/manga/70982", "can't read"),
]

MANGA = {
    "id": 70982,
    "title": "Example Manga",
    "cover_url": f"{CDN}/covers/70982.webp",
    "last_updated": "2026-11-10T12:00:00Z",
}

CHAPTERS = [
    {"id": 1001, "title": "First Chapter", "order": 1, "created_at": "2026-11-01T10:00:00Z", "manga_id": 70982},
    {"id": 1002, "title": "Bonus", "order": 1.5, "created_at": "2026-11-02T10:00:00Z", "manga_id": 70982},
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


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in ROUTES:
            with self.subTest(text=text):
                value = text.removeprefix("mht:")
                if isinstance(expected, tuple):
                    self.assertEqual(Mimi().resolve(value), Target("mht", *expected))
                else:
                    with self.assertRaisesRegex(TargetError, f"^mht: {expected}"):
                        Mimi().resolve(value)


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
        saved = collect(Mimi, "mht:70982", json=fixtures())
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

    def test_from_filters_categories(self):
        chapters_only = collect(Mimi, "mht:70982", json=fixtures(), options=Options(categories=("chapters",)))
        self.assertEqual([item.key for item in chapters_only], ["1001_0", "1001_1", "1002_0"])

        cover_only = collect(Mimi, "mht:70982", json=fixtures(), options=Options(categories=("cover",)))
        self.assertEqual([item.path for item in cover_only], ["cover/cover.webp"])

    def test_single_chapter_route_uses_same_key_and_folder(self):
        saved = collect(Mimi, "https://mimihentai.moe/manga/70982/chapter/1001", json=fixtures())
        self.assertEqual(
            [(item.path, item.key) for item in saved],
            [("chapters/001_First_Chapter/001.webp", "1001_0"), ("chapters/001_First_Chapter/002.jpg", "1001_1")],
        )

    def test_missing_manga_or_chapter_raises_fetch_error(self):
        site = FakeSite(Target("mht", "manga", "999"), json={f"{API}manga/999": {}})
        with self.assertRaisesRegex(FetchError, "manga 999"):
            Mimi().manga(site, "999")
        site_ch = FakeSite(Target("mht", "chapter", "999"), json={f"{API}chapters/999": {}})
        with self.assertRaisesRegex(FetchError, "chapter 999"):
            list(Mimi().chapter(site_ch, "999"))


class AuthorGenreAndQueryTest(unittest.TestCase):
    def _site(self, answers):
        seen = Seen()
        return FakeSite(Target("mht", "query", ""), json=answers, seen=seen), seen

    def test_simple_search_paginates_by_cursor(self):
        def search(url, req):
            cursor = req["params"].get("cursor")
            self.assertEqual(req["params"]["q"], "example words")
            self.assertEqual(req["params"]["cursor_res"], "true")
            if not cursor:
                return {"items": [{"id": 10}, {"id": 20}], "next_cursor": "c2", "total": 3}
            return {"items": [{"id": 30}], "next_cursor": None, "total": 3}

        site, seen = self._site({f"{API}manga/search": search})
        route = next(r for r in Mimi.routes if r.is_query)
        [q] = plan("example words", route.params, site=site)
        self.assertEqual(list(Mimi().query(site, q)),
                         [Target("mht", "manga", "10"), Target("mht", "manga", "20"), Target("mht", "manga", "30")])
        self.assertEqual(len(seen.requests), 2)

    def test_advanced_search_lookups_character_and_chapters_filter(self):
        captured = []

        def adv(url, req):
            captured.append(dict(req["params"]))
            return {
                "items": [
                    {"id": 100, "chapter_count": 1},
                    {"id": 101, "chapter_count": 3},
                ],
                "next_cursor": "",
            }

        site, _ = self._site({
            f"{API}genres": [{"id": 1, "name": "Vanilla"}, {"id": 2, "name": "Romance"}, {"id": 9, "name": "Example"}],
            f"{API}authors/search": lambda u, r: [{"id": 7, "name": "example_creator"}],
            f"{API}parodies/search": lambda u, r: [{"id": 55, "name": "example_parody"}],
            f"{API}manga/advanced-search": adv,
        })
        route = next(r for r in Mimi.routes if r.is_query)
        [q] = plan('genre:(vanilla AND romance) -genre:example author:example_creator '
                   'character:example_char parody:example_parody title:extra chapters:3',
                   route.params, site=site)
        found = list(Mimi().query(site, q))
        self.assertEqual(found, [Target("mht", "manga", "101")])
        self.assertEqual(captured, [{
            "title": "extra",
            "author": "7",
            "character": "example_char",
            "parody": "55",
            "genre": "1,2",
            "exclude_genre": "9",
            "cursor_res": "true",
        }])

    def test_genre_or_splits_into_two_searches(self):
        site, _ = self._site({
            f"{API}genres": [{"id": 1, "name": "Vanilla"}, {"id": 2, "name": "Romance"}],
            f"{API}manga/advanced-search": lambda u, r: {
                "items": [{"id": 10 if r["params"]["genre"] == "1" else 20, "chapter_count": 1}],
                "next_cursor": None,
            },
        })
        route = next(r for r in Mimi.routes if r.is_query)
        queries = plan("genre:(vanilla OR romance)", route.params, site=site)
        self.assertEqual(len(queries), 2)
        results = [target for q in queries for target in Mimi().query(site, q)]
        self.assertEqual(results, [Target("mht", "manga", "10"), Target("mht", "manga", "20")])

    def test_author_and_genre_page_routes(self):
        site, _ = self._site({
            f"{API}genres": [{"id": 3, "name": "Romance"}],
            f"{API}authors/search": lambda u, r: [{"id": 7, "name": "example creator"}],
            f"{API}manga/advanced-search": lambda u, r: {
                "items": [{"id": 70982}] if r["params"].get("author") == "7" else [{"id": 70983}],
                "next_cursor": None,
            },
        })
        self.assertEqual(list(Mimi().author(site, "7")), [Target("mht", "manga", "70982")])
        self.assertEqual(list(Mimi().author(site, "example-creator")), [Target("mht", "manga", "70982")])
        self.assertEqual(list(Mimi().genre(site, "romance")), [Target("mht", "manga", "70983")])
        with self.assertRaisesRegex(TargetError, "no genre named missing"):
            list(Mimi().genre(site, "missing"))


class MimiEngineTest(EngineCase):
    def test_query_and_lazy_chapter_skip_through_engine(self):
        genre_calls = 0

        def genres_route(request):
            nonlocal genre_calls
            genre_calls += 1
            return httpx.Response(200, json=[{"id": 1, "name": "Vanilla"}])

        self.routes[f"{API}genres"] = genres_route
        self.routes[f"{API}manga/advanced-search"] = httpx.Response(200, json={"items": [{"id": 70982}], "next_cursor": None})
        self.routes[f"{API}manga/70982"] = httpx.Response(200, json=MANGA)
        self.routes[f"{API}manga/70982/chapters"] = httpx.Response(200, json=[CHAPTERS[0]])
        self.routes[f"{API}chapters/1001"] = httpx.Response(200, json={
            "info": CHAPTERS[0],
            "pages": [{"image_url": f"{CDN}/1001/1.webp"}],
        })
        self.routes[f"{CDN}/1001/1.webp"] = httpx.Response(200, content=b"WEBP1")
        self.routes[f"{CDN}/covers/70982.webp"] = httpx.Response(200, content=b"COVER1")

        engine = Engine(home=self.home, sources={"mht": Mimi})
        report = engine.download(["mht?genre:vanilla"], categories="all", limit=1)
        self.assertTrue(report.ok)
        self.assertEqual(genre_calls, 1)
        folder = self.target_dir("mht", "70982")
        self.assertTrue((folder / "chapters" / "001_First_Chapter" / "001.webp").exists())
        self.assertTrue((folder / "cover" / "cover.webp").exists())

        # Second run: genre lookup is cached for 24h and saved chapter is not re-fetched.
        del self.routes[f"{API}chapters/1001"]
        report2 = engine.download(["mht?genre:vanilla"], categories="all", limit=1)
        self.assertTrue(report2.ok)
        self.assertEqual(genre_calls, 1)

    def test_cover_query_reuses_search_metadata_without_manga_api(self):
        self.routes[f"{API}genres"] = httpx.Response(200, json=[{"id": 1, "name": "Vanilla"}])
        self.routes[f"{API}manga/advanced-search"] = httpx.Response(200, json={
            "items": [MANGA], "next_cursor": None,
        })
        self.routes[f"{CDN}/covers/70982.webp"] = httpx.Response(200, content=b"COVER1")

        engine = Engine(home=self.home, sources={"mht": Mimi})
        report = engine.download(["mht?genre:vanilla"], categories="cover", limit=1)
        self.assertTrue(report.ok)
        self.assertTrue((self.target_dir("mht", "70982") / "cover" / "cover.webp").exists())


if __name__ == "__main__":
    unittest.main()
