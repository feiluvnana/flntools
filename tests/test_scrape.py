import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import httpx
    import jmespath
    import parsel
    from shared import scrape
    from shared import _scrape
except ImportError:  # the scrape extra is missing
    scrape = None

HTML = """<html><head><title>t</title>
<script id="__NEXT_DATA__" type="application/json">{"props": {"user": "someone"}}</script>
<script>window._sharedData = {"config": {"id": 7}};</script>
<script id="broken">{not json</script>
</head><body>
  <h1>  Example
     <b>User</b>
  </h1>
  <a href="/video/1">one</a><a href="/video/2">two</a><a href="/about">about</a>
</body></html>"""

# The shape of the fixture in tests/test_sources.py, plus the other shapes ins.py reads.
REPOSTS = {"data": {"fetch__XDTUserDict": {"user_reposts_timeline": {"repost_grid_items": [
    {"media": {"code": "TEST_REPOST_CODE", "pk": "123456789", "taken_at": 1700000000,
               "image_versions2": {"candidates": [{"url": "https://example.com/repost_img.jpg"}]}}}]}}}}
POSTS_NEW = {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [{"node": {"code": "a"}}]}}}
POSTS_OLD = {"data": {"user": {"edge_owner_to_timeline_media": {"edges": [{"node": {"shortcode": "b"}}]}}}}
REELS = [
    {"data": {"fetch__XDTUserDict": {"clips_connection": {"edges": [{"node": {"code": "r1"}}]}}}},
    {"data": {"xdt_api__v1__clips__user__connection_v2": {"edges": [{"node": {"code": "r2"}}]}}},
    {"data": {"user": {"edge_felix_video_timeline": {"edges": [{"node": {"code": "r3"}}]}}}},
]
TAGGED = [
    {"data": {"xdt_api__v1__usertags__user_id__feed_connection": {"edges": [{"node": {"code": "t1"}}]}}},
    {"data": {"user": {"edge_user_to_photos_of_you": {"edges": [{"node": {"code": "t2"}}]}}}},
]
HIGHLIGHTS = {"data": {"highlights": {"edges": [{"node": {"id": "h1", "title": "Trip"}}]}}}
STORIES = [
    {"data": {"xdt_api__v1__feed__reels_media__connection": {"edges": [{"node": {"id": "s1"}}]}}},
    {"data": {"reels_media": [{"id": "s2"}]}},
]
EMPTY = {"data": {}}


class FakeResponse:
    def __init__(self, text, content_type):
        self.text = text
        self.headers = {"content-type": content_type}


class FakePage:
    def content(self):
        return "<p>live <i>dom</i></p>"


@unittest.skipIf(scrape is None, "the scrape extra is not installed")
class SelectTest(unittest.TestCase):
    def test_each_source_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "page.html"
            file.write_text(HTML, encoding="utf-8")
            sources = [HTML, HTML.encode() + b"\xff", file, FakeResponse(HTML, "text/html; charset=utf-8"),
                       httpx.Response(200, html=HTML)]
            for source in sources:
                with self.subTest(type(source).__name__):
                    page = scrape.select(source)
                    self.assertIsInstance(page, parsel.Selector)
                    self.assertEqual(page.css("a[href*='/video/']::attr(href)").getall(), ["/video/1", "/video/2"])

    def test_json_response(self):
        for response in (FakeResponse('{"a": {"b": [1, 2]}}', "application/json"),
                         httpx.Response(200, json={"a": {"b": [1, 2]}})):
            self.assertEqual(scrape.select(response).jmespath("a.b[1]").get(), 2)

    def test_page(self):
        self.assertEqual(scrape.select(FakePage()).css("i::text").get(), "dom")

    def test_selector_passes_through_and_bad_type(self):
        page = scrape.select(HTML)
        self.assertIs(scrape.select(page), page)
        with self.assertRaises(TypeError):
            scrape.select(42)


@unittest.skipIf(scrape is None, "the scrape extra is not installed")
class TextTest(unittest.TestCase):
    def test_nested_tags_and_whitespace(self):
        page = scrape.select(HTML)
        self.assertEqual(scrape.text(page.css("h1")), "Example User")
        self.assertEqual(scrape.text(page.css("h1")[0]), "Example User")
        self.assertEqual(scrape.text(scrape.select("<p>a\n\n  b</p><p>c</p>").css("p")), "a b c")

    def test_empty_selection(self):
        self.assertEqual(scrape.text(scrape.select(HTML).css("h2")), "")


@unittest.skipIf(scrape is None, "the scrape extra is not installed")
class ScriptTest(unittest.TestCase):
    def test_by_id(self):
        self.assertEqual(scrape.script(HTML, id="__NEXT_DATA__"), {"props": {"user": "someone"}})

    def test_by_pattern(self):
        data = scrape.script(HTML, pattern=r"window\._sharedData = (\{.*?\});")
        self.assertEqual(data, {"config": {"id": 7}})
        self.assertEqual(scrape.script(scrape.select(HTML), pattern=r"_sharedData = (\{.*?\});"), data)

    def test_not_found(self):
        self.assertIsNone(scrape.script(HTML, id="missing"))
        self.assertIsNone(scrape.script(HTML, pattern=r"nothing = (\{\})"))

    def test_broken_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            scrape.script(HTML, id="broken")

    def test_neither_or_both(self):
        with self.assertRaises(ValueError):
            scrape.script(HTML)
        with self.assertRaises(ValueError):
            scrape.script(HTML, id="a", pattern="b")


@unittest.skipIf(scrape is None, "the scrape extra is not installed")
class DigTest(unittest.TestCase):
    data = {"data": {"user": {"id": "9", "edges": [{"node": {"id": 1}}, {"node": {"id": 2}}], "empty": []}}}

    def test_paths(self):
        self.assertEqual(scrape.dig(self.data, "data.user.id"), "9")
        self.assertEqual(scrape.dig(self.data, "data.user.edges[].node.id"), [1, 2])
        self.assertEqual(scrape.dig(self.data, "data.user.edges[1].node"), {"id": 2})

    def test_first_of_several_paths(self):
        self.assertEqual(scrape.dig(self.data, "data.user.id", "data.user.edges"), "9")
        self.assertEqual(scrape.dig(self.data, "data.other.id", "data.user.id"), "9")

    def test_empty_list_counts_as_found(self):
        self.assertEqual(scrape.dig(self.data, "data.user.empty", "data.user.id"), [])

    def test_default_and_missing_key_mid_path(self):
        self.assertIsNone(scrape.dig(self.data, "data.nope.deeper[0].x"))
        self.assertEqual(scrape.dig(self.data, "data.user.id.deeper", default="d"), "d")
        self.assertEqual(scrape.dig(None, "a.b", default=[]), [])

    def test_expressions_are_cached(self):
        _scrape._compiled.cache_clear()
        with mock.patch.object(jmespath, "compile", wraps=jmespath.compile) as compile_spy:
            for _ in range(100):
                scrape.dig(self.data, "data.user.id")
        self.assertEqual(compile_spy.call_count, 1)

    def test_bad_expression_raises(self):
        with self.assertRaises(jmespath.exceptions.ParseError):
            scrape.dig(self.data, "data.[")


@unittest.skipIf(scrape is None, "the scrape extra is not installed")
class FindTest(unittest.TestCase):
    def test_depth_first_in_document_order(self):
        data = {"a": {"url": 1, "b": {"url": 2}}, "list": [{"url": 3}, [{"x": {"url": 4}}]], "url": 5}
        self.assertEqual(list(scrape.find(data, "url")), [1, 2, 3, 4, 5])

    def test_value_under_key_is_also_searched(self):
        self.assertEqual(list(scrape.find({"k": {"k": 1}}, "k")), [{"k": 1}, 1])

    def test_no_match(self):
        self.assertEqual(list(scrape.find({"a": [1, "k"]}, "k")), [])


@unittest.skipIf(scrape is None, "the scrape extra is not installed")
class InstagramChainTest(unittest.TestCase):
    """Each `.get` chain in flnd/sources/ins.py, next to the same lookup written with `dig`."""

    def test_posts(self):
        def chain(data):
            timeline = (data.get("data", {}).get("xdt_api__v1__feed__user_timeline_graphql_connection", {})
                        or data.get("data", {}).get("user", {}).get("edge_owner_to_timeline_media", {}))
            return timeline.get("edges", [])

        for data in (POSTS_NEW, POSTS_OLD, EMPTY):
            self.assertEqual(scrape.dig(data, "data.xdt_api__v1__feed__user_timeline_graphql_connection.edges",
                                        "data.user.edge_owner_to_timeline_media.edges", default=[]), chain(data))

    def test_reels(self):
        def chain(data):
            clips = (data.get("data", {}).get("fetch__XDTUserDict", {}).get("clips_connection", {})
                     or data.get("data", {}).get("xdt_api__v1__clips__user__connection_v2", {})
                     or data.get("data", {}).get("user", {}).get("edge_felix_video_timeline", {}))
            return clips.get("edges", [])

        for data in (*REELS, EMPTY):
            self.assertEqual(scrape.dig(data, "data.fetch__XDTUserDict.clips_connection.edges",
                                        "data.xdt_api__v1__clips__user__connection_v2.edges",
                                        "data.user.edge_felix_video_timeline.edges", default=[]), chain(data))

    def test_tagged(self):
        def chain(data):
            tagged = (data.get("data", {}).get("xdt_api__v1__usertags__user_id__feed_connection", {})
                      or data.get("data", {}).get("user", {}).get("edge_user_to_photos_of_you", {}))
            return tagged.get("edges", [])

        for data in (*TAGGED, EMPTY):
            self.assertEqual(scrape.dig(data, "data.xdt_api__v1__usertags__user_id__feed_connection.edges",
                                        "data.user.edge_user_to_photos_of_you.edges", default=[]), chain(data))

    def test_reposts(self):
        def chain(data):
            xdt_dict = data.get("data", {}).get("fetch__XDTUserDict", {})
            reposts = (xdt_dict.get("user_reposts_timeline", {})
                       or data.get("data", {}).get("xdt_api__v1__feed__user_reposts_graphql_connection", {})
                       or data.get("data", {}).get("user_reposts_timeline", {}))
            return [item.get("media", item) for item in reposts.get("repost_grid_items", [])]

        for data in (REPOSTS, EMPTY):
            nodes = scrape.dig(data, "data.fetch__XDTUserDict.user_reposts_timeline.repost_grid_items[].media",
                               "data.xdt_api__v1__feed__user_reposts_graphql_connection.repost_grid_items[].media",
                               "data.user_reposts_timeline.repost_grid_items[].media", default=[])
            self.assertEqual(nodes, chain(data))
        self.assertEqual(scrape.dig(REPOSTS, "data.*.*.repost_grid_items[][].media.code | [0]"), "TEST_REPOST_CODE")
        self.assertEqual(list(scrape.find(REPOSTS, "candidates"))[0][0]["url"], "https://example.com/repost_img.jpg")

    def test_highlights(self):
        def chain(data):
            return data.get("data", {}).get("highlights", {}).get("edges", [])

        for data in (HIGHLIGHTS, EMPTY):
            self.assertEqual(scrape.dig(data, "data.highlights.edges", default=[]), chain(data))

    def test_stories(self):
        def chain(data):
            return (data.get("data", {}).get("xdt_api__v1__feed__reels_media__connection", {}).get("edges", [])
                    or data.get("data", {}).get("reels_media", []))

        for data in (*STORIES, EMPTY):
            self.assertEqual(scrape.dig(data, "data.xdt_api__v1__feed__reels_media__connection.edges",
                                        "data.reels_media", default=[]), chain(data))

    def test_image_candidates(self):
        node = REPOSTS["data"]["fetch__XDTUserDict"]["user_reposts_timeline"]["repost_grid_items"][0]["media"]
        chained = node.get("image_versions2", {}).get("candidates", [])
        self.assertEqual(scrape.dig(node, "image_versions2.candidates", default=[]), chained)
        self.assertEqual(scrape.dig({}, "image_versions2.candidates", default=[]), {}.get("image_versions2", {}).get(
            "candidates", []))



if __name__ == "__main__":
    unittest.main()
