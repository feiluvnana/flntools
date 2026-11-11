import tempfile
import unittest
from pathlib import Path

try:
    import jmespath
    import jsonpath
    import parsel
    from shared import selector
except ImportError:  # the selector extra is missing
    selector = None

HTML = """<html><head><title>t</title>
<script id="__NEXT_DATA__" type="application/json">{"props": {"user": "someone"}}</script>
<script>window._sharedData = {"config": {"id": 7}};</script>
<script id="broken">{not json</script>
<meta property="og:image" content="https://example.com/og.jpg">
</head><body>
  <h1>  Example
     <b>User</b>
  </h1>
  <ul><li><a href="/video/1">one</a></li><li><a href="/video/2">two</a></li><li><a href="/about">about</a></li></ul>
</body></html>"""

DATA = {"data": {"user": {"id": "9", "edges": [{"node": {"id": 1}}, {"node": {"id": 2}}], "empty": []}}}


class FakeHttpxResponse:
    """`text` is an attribute, as on an httpx response."""

    def __init__(self, text, content_type):
        self.text = text
        self.headers = {"content-type": content_type}


class FakePlaywrightResponse:
    """`text` is a method, as on a Playwright response."""

    def __init__(self, text, content_type):
        self._body = text
        self.headers = {"content-type": content_type}

    def text(self):
        return self._body


@unittest.skipIf(selector is None, "the selector extra is not installed")
class SelectTest(unittest.TestCase):
    def test_each_html_source(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "page.html"
            file.write_text(HTML, encoding="utf-8")
            sources = [HTML, HTML.encode() + b"\xff", file, FakeHttpxResponse(HTML, "text/html; charset=utf-8"),
                       FakePlaywrightResponse(HTML, "text/html")]
            for source in sources:
                with self.subTest(type(source).__name__):
                    page = selector.select(source)
                    self.assertIsInstance(page.raw, parsel.Selector)
                    self.assertEqual(page.css("a[href*='/video/']::attr(href)").all, ["/video/1", "/video/2"])

    def test_each_json_source(self):
        text = '{"a": {"b": [1, 2]}}'
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "data.json"
            file.write_text(text, encoding="utf-8")
            sources = [text, text.encode(), file, {"a": {"b": [1, 2]}}, FakeHttpxResponse(text, "application/json"),
                       FakePlaywrightResponse(text, "application/json; charset=utf-8"),
                       FakeHttpxResponse(text, "text/plain")]
            for source in sources:
                with self.subTest(type(source).__name__):
                    self.assertEqual(selector.select(source).jmespath("a.b[1]").first, 2)

    def test_json_content_type_with_broken_json_raises(self):
        with self.assertRaises(ValueError):
            selector.select(FakePlaywrightResponse("<html>", "application/json"))

    def test_bad_type(self):
        with self.assertRaises(TypeError):
            selector.select(42)

    def test_query_shortcut(self):
        self.assertEqual(selector.select(HTML, "h1 b::text").first, "User")
        self.assertEqual(selector.select(DATA, "data.user.id").first, "9")


@unittest.skipIf(selector is None, "the selector extra is not installed")
class HtmlTest(unittest.TestCase):
    def setUp(self):
        self.page = selector.select(HTML)

    def test_css_and_xpath(self):
        self.assertEqual(self.page.css("li a::text").all, ["one", "two", "about"])
        self.assertEqual(self.page.xpath("//meta[@property='og:image']/@content").first, "https://example.com/og.jpg")
        self.assertEqual(self.page.css("ul").css("li").xpath("./a/@href").first, "/video/1")

    def test_first_and_all_are_text(self):
        self.assertEqual(self.page.css("li a").first, '<a href="/video/1">one</a>')
        self.assertIsInstance(self.page.css("li").all[0], str)

    def test_text_collapses_whitespace(self):
        self.assertEqual(self.page.css("h1").text, "Example User")
        self.assertEqual(selector.select("<p>a\n\n  b</p><p>c</p>").css("p").text, "a b c")

    def test_empty_selection(self):
        missing = self.page.css("h2")
        self.assertEqual(missing.text, "")
        self.assertIsNone(missing.first)
        self.assertEqual(missing.all, [])
        self.assertFalse(missing)
        self.assertEqual(len(missing), 0)

    def test_len_bool_and_iteration(self):
        links = self.page.css("li a")
        self.assertTrue(links)
        self.assertEqual(len(links), 3)
        self.assertEqual([link.css("::attr(href)").first for link in links], ["/video/1", "/video/2", "/about"])
        self.assertIsInstance(links.raw, parsel.SelectorList)

    def test_jsonpath_on_html_raises(self):
        with self.assertRaises(ValueError):
            self.page.jsonpath("$..a")


@unittest.skipIf(selector is None, "the selector extra is not installed")
class JmespathTest(unittest.TestCase):
    def setUp(self):
        self.data = selector.select(DATA)

    def test_path_projection_and_index(self):
        self.assertEqual(self.data.jmespath("data.user.id").first, "9")
        self.assertEqual(self.data.jmespath("data.user.edges[].node.id").all, [1, 2])
        self.assertEqual(self.data.jmespath("data.user.edges[1].node").first, {"id": 2})

    def test_list_result_is_split_into_items(self):
        edges = self.data.jmespath("data.user.edges")
        self.assertEqual(len(edges), 2)
        self.assertEqual(edges.first, {"node": {"id": 1}})
        self.assertEqual([edge.jmespath("node.id").first for edge in edges], [1, 2])

    def test_scalar_or_object_result_is_one_item(self):
        self.assertEqual(self.data.jmespath("data.user.id").all, ["9"])
        self.assertEqual(len(self.data.jmespath("data.user")), 1)

    def test_missing_path_is_empty(self):
        for query in ("data.nope.deeper[0].x", "data.user.id.deeper", "data.user.empty"):
            with self.subTest(query):
                missing = self.data.jmespath(query)
                self.assertIsNone(missing.first)
                self.assertEqual(missing.all, [])
                self.assertFalse(missing)
        self.assertEqual(self.data.jmespath("data.nope").first or "", "")

    def test_first_of_several_paths(self):
        self.assertEqual(self.data.jmespath("data.other.id || data.user.id").first, "9")

    def test_invalid_expression_raises(self):
        with self.assertRaises(jmespath.exceptions.ParseError):
            self.data.jmespath("data.[")


@unittest.skipIf(selector is None, "the selector extra is not installed")
class JsonpathTest(unittest.TestCase):
    def test_key_at_any_depth_in_document_order(self):
        data = {"a": {"url": 1, "b": {"url": 2}}, "list": [{"url": 3}, [{"x": {"url": 4}}]]}
        self.assertEqual(selector.select(data).jsonpath("$..url").all, [1, 2, 3, 4])

    def test_matches_are_json_values(self):
        found = selector.select(DATA).jsonpath("$..node")
        self.assertEqual(found.all, [{"id": 1}, {"id": 2}])
        self.assertEqual(found.jmespath("id").all, [1, 2])

    def test_keyword_like_key(self):
        self.assertEqual(selector.select({"a": {"missing": 1}}).jsonpath("$..missing").all, [1])

    def test_no_match(self):
        self.assertEqual(selector.select(DATA).jsonpath("$..absent").all, [])
        self.assertIsNone(selector.select(DATA).jsonpath("$..absent").first)

    def test_invalid_expression_raises(self):
        with self.assertRaises(jsonpath.JSONPathSyntaxError):
            selector.select(DATA).jsonpath("$..[")


@unittest.skipIf(selector is None, "the selector extra is not installed")
class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.page = selector.select(HTML)

    def test_by_id(self):
        data = self.page.script("__NEXT_DATA__")
        self.assertEqual(data.first, {"props": {"user": "someone"}})
        self.assertEqual(data.jmespath("props.user").first, "someone")

    def test_by_pattern(self):
        data = self.page.script(pattern=r"window\._sharedData = (\{.*?\});")
        self.assertEqual(data.jmespath("config.id").first, 7)

    def test_not_found(self):
        self.assertFalse(self.page.script("missing"))
        self.assertIsNone(self.page.script(pattern=r"nothing = (\{\})").first)

    def test_broken_json_raises(self):
        with self.assertRaises(ValueError):
            self.page.script("broken")

    def test_neither_or_both_raise(self):
        with self.assertRaises(ValueError):
            self.page.script()
        with self.assertRaises(ValueError):
            self.page.script("a", pattern="b")


if __name__ == "__main__":
    unittest.main()
