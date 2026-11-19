"""mimihentai (`mht`) on plugin API 2: its routes, chapters, covers, searches and lookups."""

import asyncio
import datetime
import unittest

import no_profiles  # noqa: F401
from fakes import EngineCase

from flnd.engine._engine import Engine
from flnd.plugin import NotFound, Range, Target, UsageError
from flnd.plugins.mht import API, Mimi, format_order
from flnd.testing import FakeCtx, Reply, check, collect
from flnkit import net as _net
from flnkit.files import sniff

CDN = "https://moe-cdn.example.com"

MANGA = {"id": 70982, "title": "Example Manga", "cover_url": f"{CDN}/covers/70982.webp",
         "last_updated": "2026-11-10T12:00:00Z"}
CHAPTERS = [
    {"id": 1001, "title": "First Chapter", "order": 1, "created_at": "2026-11-01T10:00:00Z"},
    {"id": 1002, "title": "Bonus", "order": 1.5, "created_at": "2026-11-02T10:00:00Z"},
]
PAGES = {
    1001: [f"{CDN}/1001/1.webp", f"{CDN}/1001/2.jpg?v=1"],
    1002: [f"{CDN}/1002/1.webp"],
}
LOOKUPS = {
    "genres": [{"id": 1, "name": "Vanilla"}, {"id": 2, "name": "Romance"},
               {"id": 4, "name": "Đô Thị"}, {"id": 9, "name": "Example"}],
    "authors/search": {"items": [{"id": 7, "name": "example creator"}]},
    "parodies/search": {"data": [{"id": 55, "name": "example_parody"}]},
}
EVERY_FILE = [
    ("chapters/001_First_Chapter/001.webp", "1001_0"),
    ("chapters/001_First_Chapter/002.jpg", "1001_1"),
    ("chapters/1.5_Bonus/001.webp", "1002_0"),
    ("cover/cover.webp", "70982_cover"),
]


def answers(**changed):
    """The site's answers by URL: the manga, its chapters, the lookups, and `changed`."""
    found = {f"{API}manga/70982": MANGA, f"{API}manga/70982/chapters": CHAPTERS,
             **{f"{API}chapters/{id}": {"info": next(c for c in CHAPTERS if c["id"] == id),
                                        "pages": [{"image_url": url} for url in urls]}
                for id, urls in PAGES.items()},
             **{f"{API}{path}": body for path, body in LOOKUPS.items()}}
    found.update({f"{API}{path}": body for path, body in changed.items()})
    return {url: body if callable(body) else Reply(body) for url, body in found.items()}


def fake(target="", **changed):
    return FakeCtx(Mimi, target, routes=answers(**changed))


def listed(ctx, kind, value, plugin=None):
    """What a route with no categories yields, on `ctx`."""
    async def run():
        return [item async for item in getattr(plugin or Mimi(), kind)(ctx, value)]
    return asyncio.run(run())


def asked(ctx):
    """The API paths `ctx` was asked for, in order."""
    return [url.removeprefix(API) for _, url, _ in ctx.seen.requests]


def manga(*ids):
    return [Target(f"mht:{id}", kind="manga") for id in ids]


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in [
            ("mht:70982", ("manga", "mht:70982")),
            ("MHT:70982", ("manga", "mht:70982")),
            ("https://www.mimihentai.moe/manga/70982/", ("manga", "mht:70982")),
            ("mimihentai.moe/manga/70982/chapter/1001/", ("chapter", "mht:chapter/1001")),
            ("mht:chapter/1001", ("chapter", "mht:chapter/1001")),
            ("mht:author/example_creator", ("author", "mht:author/example_creator")),
            ("https://mimihentai.moe/author/7", ("author", "mht:author/7")),
            ("https://mimihentai.moe/authors/example-creator", ("author",
                                                                 "mht:author/example-creator")),
            ("mht:genre/example", ("genre", "mht:genre/example")),
            ("https://mimihentai.moe/genres/12", ("genre", "mht:genre/12")),
            ("mht?genre:vanilla", ("query", "mht?genre:vanilla")),
            ('mht?author:"example creator" chapters:1',
             ("query", 'mht?author:"example creator" chapters:1')),
            ("mht:abc", None), ("https://mimihentai.moe/", None),
            ("https://example.com/manga/70982", None),
        ]:
            with self.subTest(text=text):
                found = Mimi.resolve(text)
                self.assertEqual(found and (found.kind, found.text), expected)
        with self.assertRaisesRegex(UsageError, "the query is empty"):
            Mimi.resolve("mht?")

    def test_an_author_or_genre_is_never_keyed_as_a_manga(self):
        """API 1 read `/author/7` as the value `7`: the same key, `mht:7`, as manga 7."""
        for link in ("https://mimihentai.moe/author/7", "https://mimihentai.moe/genre/7"):
            with self.subTest(link=link):
                found = Mimi.resolve(link)
                self.assertNotEqual(found.value, Mimi.resolve("mht:7").value)
                # Its text reads back through the same route.
                self.assertEqual(Mimi.resolve(found.text), found)

    def test_the_declarations(self):
        self.assertEqual(len(check(Mimi)), 8)
        self.assertEqual([route.kind for route in Mimi.routes if route.discovers],
                         ["author", "genre", "query"])
        self.assertTrue(all(category.own for route in Mimi.routes
                            for category in route.categories))


class MangaTest(unittest.TestCase):
    def test_every_file_its_path_and_key(self):
        saved = collect(Mimi, "mht:70982", ctx=fake())
        self.assertEqual([(item.path, item.key) for item in saved], EVERY_FILE)
        self.assertEqual(str(saved[0].date), "2026-11-01")
        self.assertEqual(str(saved[-1].date), "2026-11-10")
        self.assertEqual(saved[-1].post.fields["title"], "Example Manga")

    def test_each_category_asks_only_what_it_needs(self):
        for only, files, paths in [
            (["chapters"], EVERY_FILE[:3],
             ["manga/70982", "manga/70982/chapters", "chapters/1001", "chapters/1002"]),
            (["cover"], EVERY_FILE[3:], ["manga/70982"]),
        ]:
            with self.subTest(only=only):
                ctx = fake()
                saved = collect(Mimi, "mht:70982", ctx=ctx, categories=only)
                self.assertEqual([(item.path, item.key) for item in saved], files)
                self.assertEqual(asked(ctx), paths)

    def test_a_chapter_link_files_as_its_manga_does(self):
        saved = collect(Mimi, "https://mimihentai.moe/manga/70982/chapter/1001", ctx=fake())
        self.assertEqual([(item.path, item.key) for item in saved], EVERY_FILE[:2])

    def test_chapters_are_lazy(self):
        async def posts():
            plugin, ctx = Mimi(), fake()
            return [post async for post in plugin.chapters(ctx, MANGA)], asked(ctx)

        found, paths = asyncio.run(posts())
        self.assertEqual([post.lazy for post in found], [True, True])
        self.assertEqual(paths, ["manga/70982/chapters"])
        # A chapter outside `when` is never asked for its pages.
        ctx = FakeCtx(Mimi, routes=answers(), when=Range(since=datetime.date(2026, 11, 2)))
        saved = collect(Mimi, "mht:70982", ctx=ctx, categories=["chapters"])
        self.assertEqual([item.key for item in saved], ["1002_0"])
        self.assertNotIn("chapters/1001", asked(ctx))

    def test_pages_keep_their_place_and_the_content_names_a_bare_url(self):
        pages = [{"image_url": f"{CDN}/1001/a.webp"}, {"image_url": None}, {"no": "url"},
                 {"image_url": f"{CDN}/1001/d"}]
        ctx = fake(**{"chapters/1001": {"info": CHAPTERS[0], "pages": pages}})
        saved = collect(Mimi, "https://mimihentai.moe/manga/70982/chapter/1001", ctx=ctx)
        folder = "chapters/001_First_Chapter/"
        self.assertEqual([item.path for item in saved], [f"{folder}001.webp", f"{folder}004"])
        # The engine names a file whose URL has no extension from its first bytes.
        for content, name in [(b"RIFF\0\0\0\0WEBPVP8 ", "004.webp"),
                              (b"\xff\xd8\xff\xe0", "004.jpg")]:
            with self.subTest(name=name):
                self.assertEqual(saved[1].path + sniff(content), folder + name)

    def test_the_covers_fields(self):
        for given, fields in [
            ({"authors": [{"name": "example_creator"}, {"name": "example_user"}, {}],
              "genres": [{"name": "Vanilla"}, {"id": 2}, {"name": "Romance"}],
              "chapter_count": 2},
             {"author": "example_creator, example_user", "genres": "Vanilla, Romance",
              "chapters": 2}),
            ({"author": "example_handle", "chapter_count": 0},
             {"author": "example_handle", "genres": "", "chapters": 0}),
            ({}, {"author": "", "genres": "", "chapters": ""}),
        ]:
            with self.subTest(given=given):
                ctx = fake(**{"manga/70982": {**MANGA, **given}})
                saved = collect(Mimi, "mht:70982", ctx=ctx, categories=["cover"])
                self.assertEqual(dict(saved[0].post.fields),
                                 {"title": "Example Manga", **fields})

    def test_a_cover_url_without_an_extension_is_named_from_its_content(self):
        ctx = fake(**{"manga/70982": {**MANGA, "cover_url": f"{CDN}/covers/70982"}})
        [saved] = collect(Mimi, "mht:70982", ctx=ctx, categories=["cover"])
        self.assertEqual((saved.path, saved.key), ("cover/cover", "70982_cover"))
        self.assertEqual(saved.path + sniff(b"RIFF\0\0\0\0WEBPVP8 "), "cover/cover.webp")

    def test_a_missing_manga_or_chapter_is_not_found(self):
        for target, path in [("mht:70982", "manga/70982"),
                             ("https://mimihentai.moe/manga/1/chapter/1001", "chapters/1001")]:
            with self.subTest(target=target), self.assertRaises(NotFound):
                collect(Mimi, target, ctx=fake(**{path: {}}))

    def test_a_manga_a_search_found_is_not_fetched_again(self):
        plugin = Mimi()
        ctx = FakeCtx(plugin, routes=answers(
            **{"manga/search": {"items": [{**MANGA, "chapter_count": 2}]}}))
        self.assertEqual(listed(ctx, "query", "example", plugin), manga(70982))
        saved = collect(Mimi, "mht:70982", ctx=ctx, categories=["cover"])
        self.assertEqual([item.key for item in saved], ["70982_cover"])
        self.assertEqual(asked(ctx), ["manga/search"])

    def test_chapter_numbers(self):
        for value, shown in [(1, "001"), (12, "012"), (1.0, "001"), (1.5, "1.5"), ("3", "003"),
                             ("7.5", "7.5"), (None, ""), ("", ""), (True, "")]:
            with self.subTest(value=value):
                self.assertEqual(format_order(value), shown)


class SearchTest(unittest.TestCase):
    def test_a_search_follows_the_cursor(self):
        def search(request):
            more = request.params.get("cursor")
            return Reply({"items": [{"id": 30}], "next_cursor": None} if more == "c2" else
                         {"items": [{"id": 10}, {"id": 20}, {"no": "id"}], "next_cursor": "c2"})

        ctx = fake(**{"manga/search": search})
        self.assertEqual(listed(ctx, "query", "example words"), manga(10, 20, 30))
        params = [request["params"] for _, _, request in ctx.seen.requests]
        self.assertEqual(params, [{"q": "example words", "cursor_res": "true"},
                                  {"q": "example words", "cursor_res": "true", "cursor": "c2"}])

    def test_terms_become_ids_and_chapters_filter(self):
        ctx = fake(**{"manga/advanced-search": {"items": [
            {"id": 100, "chapter_count": 1}, {"id": 101, "chapter_count": 3}]}})
        found = listed(ctx, "query", 'genre:vanilla genre:romance -genre:example author:"example '
                       'creator" character:example_char parody:example_parody title:extra '
                       "chapters:3")
        self.assertEqual(found, manga(101))
        self.assertEqual(ctx.seen.requests[-1][2]["params"], {
            "title": "extra", "author": "7", "character": "example_char", "parody": "55",
            "genre": "1,2", "exclude_genre": "9", "cursor_res": "true"})

    def test_bad_queries_are_refused(self):
        for query, message in [("   ", "the query is empty"), ("author:", "empty value"),
                               ("-author:x", "unsupported query term"),
                               ("chapters:x", "unsupported query term"),
                               ("genre:missing", "no genre named missing")]:
            with self.subTest(query=query), self.assertRaisesRegex(UsageError, message):
                listed(fake(), "query", query)

    def test_authors_and_genres_by_name_or_id(self):
        for kind, value, sent in [
            ("author", "author/7", {"author": "7"}),
            ("author", "author/example-creator", {"author": "7"}),
            ("genre", "genre/romance", {"genre": "2"}),
            ("genre", "genre/do%20thi", {"genre": "4"}),
            ("genre", "genre/DO-THI", {"genre": "4"}),
        ]:
            with self.subTest(value=value):
                ctx = fake(**{"manga/advanced-search": {"items": [{"id": 70982}]}})
                self.assertEqual(listed(ctx, kind, value), manga(70982))
                self.assertEqual(ctx.seen.requests[-1][2]["params"],
                                 {**sent, "cursor_res": "true"})


class EngineTest(EngineCase):
    """Searches and author links through the real engine, on `flnkit.net.fake`."""

    def setUp(self):
        super().setUp()
        self.genre_calls = 0

        def genres(request):
            self.genre_calls += 1
            return _net.reply(200, json=[{"id": 1, "name": "Vanilla"}])

        page = f"{CDN}/1001/1.webp"
        self.routes.update({
            f"{API}genres": genres,
            f"{API}manga/advanced-search": _net.reply(200, json={"items": [{"id": 70982}],
                                                                 "next_cursor": None}),
            f"{API}manga/70982": _net.reply(200, json=MANGA),
            f"{API}manga/70982/chapters": _net.reply(200, json=CHAPTERS[:1]),
            f"{API}chapters/1001": _net.reply(200, json={"info": CHAPTERS[0],
                                                         "pages": [{"image_url": page}]}),
            page: _net.reply(200, content=b"WEBP1"),
            MANGA["cover_url"]: _net.reply(200, content=b"COVER1")})
        self.engine = Engine(home=self.home, sources={"mht": Mimi})
        self.folder = self.target_dir("mht", "70982")

    def asked(self):
        return [str(request.url).split("?")[0] for request in self.requests]

    def test_a_query_downloads_its_manga_and_a_second_run_skips_a_saved_chapter(self):
        report = self.engine.download(["mht?genre:vanilla/all"], limit=1)
        self.assertTrue(report.ok, report)
        self.assertEqual(self.genre_calls, 1)
        self.assertTrue((self.folder / "chapters" / "001_First_Chapter" / "001.webp").exists())
        self.assertTrue((self.folder / "cover" / "cover.webp").exists())
        # The saved chapter's pages are not asked for again.
        del self.routes[f"{API}chapters/1001"]
        self.requests.clear()
        report = self.engine.download(["mht?genre:vanilla/all"], limit=1)
        self.assertTrue(report.ok, report)
        self.assertEqual(self.genre_calls, 2)
        self.assertNotIn(f"{API}chapters/1001", self.asked())

    def test_a_cover_query_reuses_the_searchs_manga_without_asking_for_it(self):
        self.routes[f"{API}manga/advanced-search"] = _net.reply(
            200, json={"items": [MANGA], "next_cursor": None})
        report = self.engine.download(["mht?genre:vanilla/cover"], limit=1)
        self.assertTrue(report.ok, report)
        self.assertTrue((self.folder / "cover" / "cover.webp").exists())
        self.assertNotIn(f"{API}manga/70982", self.asked())

    def test_an_author_target_downloads_only_the_named_category(self):
        self.routes[f"{API}manga/advanced-search"] = _net.reply(
            200, json={"items": [MANGA], "next_cursor": None})
        report = self.engine.download(["mht:author/7/chapters"])
        self.assertTrue(report.ok, report)
        self.assertTrue((self.folder / "chapters" / "001_First_Chapter" / "001.webp").exists())
        self.assertFalse((self.folder / "cover").exists())

if __name__ == "__main__":
    unittest.main()
