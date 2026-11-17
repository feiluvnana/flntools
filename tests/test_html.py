"""`flnd._html`: `parse`, CSS queries, text and attributes, embedded JSON, the size limit, and `dig`."""

import unittest
from unittest import mock

from flnd import _html
from flnd.errors import Changed, SiteError, SourceError

HTML = """<html><head><title>t</title>
<script id="__NEXT_DATA__" type="application/json">{"props": {"user": "example_user"}}</script>
<script type="application/json">{"other": 1}</script>
<script type="application/json">{"__bbox": {"result": 2}}</script>
<script type="application/json">{"__bbox": broken</script>
<script>window._sharedData = {"config": {"id": 7}};</script>
<script id="broken">{not json</script>
<style>p { color: red }</style>
<meta property="og:image" content="https://example.com/og.jpg">
</head><body>
  <h1>  Example
     <b>User</b>
  </h1>
  <ul><li><a href="/video/1">one</a></li><li><a href="/photo/2" hidden>two</a></li><li><a href="/about">about</a></li></ul>
</body></html>"""


class ParseTest(unittest.TestCase):
    def test_text_and_bytes(self):
        for source in (HTML, HTML.encode(), b"\xef\xbb\xbf" + HTML.encode() + b"\xff"):
            with self.subTest(type(source).__name__):
                page = _html.parse(source)
                self.assertIsInstance(page, _html.Document)
                self.assertEqual(page.first("h1").text, "Example User")

    def test_bytes_are_utf8(self):
        self.assertEqual(_html.parse("<p>é</p>".encode()).first("p").text, "é")

    def test_empty_and_broken_markup_parse(self):
        self.assertEqual(_html.parse("").css("p"), [])
        self.assertEqual(_html.parse("<p>a<p>b").css("p")[1].text, "b")

    def test_over_64_mb_is_refused(self):
        self.assertEqual(_html.MAX_SIZE, 64 * 1024 * 1024)
        with mock.patch.object(_html, "MAX_SIZE", 8):
            self.assertEqual(_html.parse("<p>a</p>").first("p").text, "a")
            for source in ("<p>ab</p>", b"<p>ab</p>"):
                # The site changed what it sends: a SiteError, never a ValueError a caller might eat.
                for error in (Changed, SiteError):
                    with self.subTest(source=source, error=error), self.assertRaises(error):
                        _html.parse(source)


class QueryTest(unittest.TestCase):
    def setUp(self):
        self.page = _html.parse(HTML)

    def test_css_gives_every_match_in_page_order(self):
        links = self.page.css('a[href*="/video/"], a[href*="/photo/"]')
        self.assertEqual([link.attr("href") for link in links], ["/video/1", "/photo/2"])
        self.assertEqual([link.tag for link in links], ["a", "a"])

    def test_first_and_nested_queries(self):
        self.assertEqual(self.page.first("ul").first("li a").text, "one")
        self.assertEqual(len(self.page.first("ul").css("li")), 3)
        self.assertIsNone(self.page.first("h2"))
        self.assertEqual(self.page.css("h2"), [])

    def test_attr(self):
        self.assertEqual(self.page.first("meta").attr("content"), "https://example.com/og.jpg")
        self.assertIsNone(self.page.first("a").attr("title"))
        self.assertEqual(self.page.css("a")[1].attr("hidden"), "")

    def test_text_collapses_whitespace(self):
        self.assertEqual(_html.parse("<p>a\n\n  b <i>c</i></p>").first("p").text, "a b c")

    def test_text_keeps_scripts_and_visible_drops_them(self):
        self.assertIn("_sharedData", self.page.text)
        visible = self.page.visible
        self.assertIn("Example User", visible)
        self.assertNotIn("_sharedData", visible)
        self.assertNotIn("color", visible)
        # The document itself keeps its scripts.
        self.assertIn("_sharedData", self.page.text)

    def test_invalid_css_raises_a_source_error(self):
        for query in ("div[", "a[["):
            with self.subTest(query), self.assertRaises(SourceError):
                self.page.css(query)
        with self.assertRaises(SourceError):
            self.page.first("div[")


class EmbeddedJsonTest(unittest.TestCase):
    def setUp(self):
        self.page = _html.parse(HTML)

    def test_script_by_id(self):
        self.assertEqual(self.page.script("__NEXT_DATA__"), {"props": {"user": "example_user"}})
        self.assertIsNone(self.page.script("missing"))

    def test_script_that_isnt_json_raises(self):
        with self.assertRaises(Changed):
            self.page.script("broken")

    def test_blocks_skip_the_ones_that_dont_hold_the_text_or_arent_json(self):
        self.assertEqual(self.page.blocks("__bbox"), [{"__bbox": {"result": 2}}])
        self.assertEqual(self.page.blocks(), [{"props": {"user": "example_user"}}, {"other": 1},
                                              {"__bbox": {"result": 2}}])
        self.assertEqual(_html.parse("<p>no scripts</p>").blocks(), [])


class DigTest(unittest.TestCase):
    PAGE = {"data": {"user": {"edges": [
        {"node": {"id": 1, "media": [{"u": "a"}, {"u": "b"}]}},
        {"node": {"id": 2, "media": [{"u": "c"}]}, "other": True},
    ]}}, "next": None, "alt": "z"}

    def test_keys_and_indexes(self):
        self.assertEqual(_html.dig(self.PAGE, "data.user.edges[0].node.id"), 1)
        self.assertEqual(_html.dig(self.PAGE, "data.user.edges[-1].other"), True)
        self.assertIsNone(_html.dig(self.PAGE, "data.user.edges[9]"))
        self.assertIsNone(_html.dig(self.PAGE, "data.nobody.id"))
        self.assertIsNone(_html.dig(["x"], "a.b"))
        self.assertIsNone(_html.dig({"a": 1}, "a.b"))

    def test_flatten(self):
        self.assertEqual(_html.dig(self.PAGE, "data.user.edges[].node.id"), [1, 2])
        self.assertEqual(_html.dig(self.PAGE, "data.user.edges[].node.media[].u"), ["a", "b", "c"])
        self.assertEqual(_html.dig({"a": [[1, 2], [3]]}, "a[]"), [1, 2, 3])
        self.assertEqual(_html.dig(self.PAGE, "data.nobody[].id"), [])
        self.assertEqual(_html.dig({"a": 1}, "a[]"), [])

    def test_first_alternative_that_finds_something(self):
        self.assertEqual(_html.dig(self.PAGE, "next || alt"), "z")
        self.assertEqual(_html.dig(self.PAGE, "data.user.edges[].node.id || alt"), [1, 2])
        self.assertEqual(_html.dig({"a": [], "b": [1]}, "a || b"), [1])
        self.assertIsNone(_html.dig(self.PAGE, "next || nobody"))

    def test_search_at_any_depth(self):
        data = {"__typename": "Tile", "n": 0, "list": [
            {"x": {"__typename": "Tile", "n": 1, "inner": {"__typename": "Tile", "n": 2}}},
            {"__typename": "Other", "n": 3}, [{"__typename": "Tile", "n": 4}]]}
        found = _html.dig(data, "..[__typename=Tile]")
        self.assertEqual([tile["n"] for tile in found], [0, 1, 2, 4])
        self.assertEqual(_html.dig(data, "..[__typename=Tile].n"), [0, 1, 2, 4])
        self.assertEqual(_html.dig(data, "list..[__typename=Other].n"), [3])
        self.assertEqual(_html.dig(data, "..[__typename=Nothing]"), [])
        # The value is text: a number never equals it.
        self.assertEqual(_html.dig({"n": 1}, "..[n=1]"), [])

    def test_search_survives_deep_nesting(self):
        deep: dict = {"t": "x"}
        for _ in range(5000):
            deep = {"child": deep}
        self.assertEqual(_html.dig(deep, "..[t=x]"), [{"t": "x"}])

    def test_a_bad_path_raises_a_source_error(self):
        for path in ("data[x]", "a..b", "a.", "a[1", "..[novalue]"):
            with self.subTest(path), self.assertRaises(Changed):
                _html.dig(self.PAGE, path)

    def test_flnd_source_dig_is_this_one(self):
        from flnd.source import dig

        self.assertIs(dig, _html.dig)


if __name__ == "__main__":
    unittest.main()
