"""`flnkit.html`: `parse`, CSS queries, text and attributes, embedded JSON, size limit, `dig`."""

import unittest
from unittest import mock

from flnd.errors import Changed, SiteError
from flnkit import html as _html

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
  <ul><li><a href="/video/1">one</a></li><li><a href="/photo/2" hidden>two</a></li>
  <li><a href="/about">about</a></li></ul>
</body></html>"""


class ParseTest(unittest.TestCase):
    def test_text_bytes_and_broken_markup(self):
        for source in (HTML, HTML.encode(), b"\xef\xbb\xbf" + HTML.encode() + b"\xff"):
            with self.subTest(type(source).__name__):
                page = _html.parse(source)
                self.assertIsInstance(page, _html.Document)
                self.assertEqual(page.first("h1").text, "Example User")
        self.assertEqual(_html.parse("<p>é</p>".encode()).first("p").text, "é")
        self.assertEqual(_html.parse("").css("p"), [])
        self.assertEqual(_html.parse("<p>a<p>b").css("p")[1].text, "b")

    def test_over_64_mb_is_refused_as_changed(self):
        self.assertEqual(_html.MAX_SIZE, 64 * 1024 * 1024)
        with mock.patch.object(_html, "MAX_SIZE", 8):
            self.assertEqual(_html.parse("<p>a</p>").first("p").text, "a")
            for source in ("<p>ab</p>", b"<p>ab</p>"):
                # A SiteError, never a ValueError a caller might eat.
                with self.subTest(source=source), self.assertRaises(Changed) as caught:
                    _html.parse(source)
                self.assertIsInstance(caught.exception, SiteError)


class QueryTest(unittest.TestCase):
    def setUp(self):
        self.page = _html.parse(HTML)

    def test_css_first_and_attr(self):
        links = self.page.css('a[href*="/video/"], a[href*="/photo/"]')
        self.assertEqual([(link.tag, link.attr("href")) for link in links],
                         [("a", "/video/1"), ("a", "/photo/2")])
        self.assertEqual(self.page.first("ul").first("li a").text, "one")
        self.assertEqual(len(self.page.first("ul").css("li")), 3)
        self.assertIsNone(self.page.first("h2"))
        self.assertEqual(self.page.css("h2"), [])
        self.assertEqual(self.page.first("meta").attr("content"), "https://example.com/og.jpg")
        self.assertIsNone(self.page.first("a").attr("title"))
        self.assertEqual(self.page.css("a")[1].attr("hidden"), "")

    def test_text_collapses_and_visible_drops_scripts_and_styles(self):
        self.assertEqual(_html.parse("<p>a\n\n  b <i>c</i></p>").first("p").text, "a b c")
        visible = self.page.visible
        self.assertIn("Example User", visible)
        self.assertNotIn("_sharedData", visible)
        self.assertNotIn("color", visible)
        self.assertIn("_sharedData", self.page.text)  # the document keeps its scripts

    def test_invalid_css_raises_a_value_error(self):
        for query in (lambda: self.page.css("div["), lambda: self.page.css("a[["),
                      lambda: self.page.first("div[")):
            with self.assertRaisesRegex(ValueError, "invalid CSS selector"):
                query()

    def test_embedded_json(self):
        self.assertEqual(self.page.script("__NEXT_DATA__"), {"props": {"user": "example_user"}})
        self.assertIsNone(self.page.script("missing"))
        with self.assertRaises(Changed):
            self.page.script("broken")
        # Blocks without the text, or that aren't JSON, are skipped.
        self.assertEqual(self.page.blocks("__bbox"), [{"__bbox": {"result": 2}}])
        self.assertEqual(self.page.blocks(), [{"props": {"user": "example_user"}}, {"other": 1},
                                              {"__bbox": {"result": 2}}])
        self.assertEqual(_html.parse("<p>no scripts</p>").blocks(), [])


class DigTest(unittest.TestCase):
    PAGE = {"data": {"user": {"edges": [
        {"node": {"id": 1, "media": [{"u": "a"}, {"u": "b"}]}},
        {"node": {"id": 2, "media": [{"u": "c"}]}, "other": True},
    ]}}, "next": None, "alt": "z"}
    TILES = {"__typename": "Tile", "n": 0, "list": [
        {"x": {"__typename": "Tile", "n": 1, "inner": {"__typename": "Tile", "n": 2}}},
        {"__typename": "Other", "n": 3}, [{"__typename": "Tile", "n": 4}]]}

    def test_paths(self):
        page = self.PAGE
        cases = [
            # keys and indexes
            (page, "data.user.edges[0].node.id", 1),
            (page, "data.user.edges[-1].other", True),
            (page, "data.user.edges[9]", None), (page, "data.nobody.id", None),
            (["x"], "a.b", None), ({"a": 1}, "a.b", None),
            # flattening
            (page, "data.user.edges[].node.id", [1, 2]),
            (page, "data.user.edges[].node.media[].u", ["a", "b", "c"]),
            ({"a": [[1, 2], [3]]}, "a[]", [1, 2, 3]), (page, "data.nobody[].id", []),
            ({"a": 1}, "a[]", []),
            # the first alternative that finds something
            (page, "next || alt", "z"), (page, "data.user.edges[].node.id || alt", [1, 2]),
            ({"a": [], "b": [1]}, "a || b", [1]), (page, "next || nobody", None),
            # a search at any depth; its value is text, which a number never equals
            (self.TILES, "..[__typename=Tile].n", [0, 1, 2, 4]),
            (self.TILES, "list..[__typename=Other].n", [3]),
            (self.TILES, "..[__typename=Nothing]", []), ({"n": 1}, "..[n=1]", []),
        ]
        for data, path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(_html.dig(data, path), expected)
        found = _html.dig(self.TILES, "..[__typename=Tile]")
        self.assertEqual([tile["n"] for tile in found], [0, 1, 2, 4])

    def test_search_survives_deep_nesting(self):
        deep: dict = {"t": "x"}
        for _ in range(5000):
            deep = {"child": deep}
        self.assertEqual(_html.dig(deep, "..[t=x]"), [{"t": "x"}])

    def test_a_bad_path_raises_a_value_error(self):
        # A path is written by its caller, so a broken one is a ValueError, never `Changed`.
        for path in ("data[x]", "a..b", "a.", "a[1", "..[novalue]", "a["):
            with self.subTest(path), self.assertRaises(ValueError):
                _html.dig(self.PAGE, path)

    def test_flnd_plugin_dig_is_this_one(self):
        from flnd.plugin import dig

        self.assertIs(dig, _html.dig)


if __name__ == "__main__":
    unittest.main()
