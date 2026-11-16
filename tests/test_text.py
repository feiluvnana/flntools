import unittest

from flnd import _text


class ScrubUrlTest(unittest.TestCase):
    def test_strips_userinfo_query_and_fragment(self):
        raw = "https://user:secret@cdn.example.com:8080/a/b.jpg?oe=1&sig=tok#top"
        self.assertEqual(_text.redact(raw), "https://cdn.example.com:8080/a/b.jpg")
        self.assertEqual(_text.redact("http://user@example.com/p"), "http://example.com/p")

    def test_preserves_protocol_relative_urls(self):
        self.assertEqual(
            _text.redact("//user:pw@cdn.example.com/a.jpg?sig=1#x"),
            "//cdn.example.com/a.jpg",
        )
        self.assertEqual(
            _text.redact("//cdn.example.com:8443/path?x=1"),
            "//cdn.example.com:8443/path",
        )

    def test_ipv6_and_relative_paths(self):
        self.assertEqual(
            _text.redact("https://user:pw@[::1]:8080/p?q=1#f"),
            "https://[::1]:8080/p",
        )
        self.assertEqual(_text.redact("/relative/path?x=1#h"), "/relative/path")
        self.assertEqual(
            _text.redact("mailto:user@example.com?subject=hi"),
            "mailto:user@example.com",
        )


class RedactBrokenUrlTest(unittest.TestCase):
    def test_a_url_urlsplit_rejects_is_cut_at_its_query_without_userinfo(self):
        self.assertEqual(_text.redact("http://user:pw@example.com:99999/a?sig=1"),
                         "http://example.com:99999/a")
        self.assertEqual(_text.redact("http://[::1/a?sig=1#f"), "http://[::1/a")
        self.assertEqual(_text.redact("http://user@[::1/a"), "http://[::1/a")


class SizeTest(unittest.TestCase):
    def test_units_and_rounding(self):
        self.assertEqual(_text.size(0), "0.0 B")
        self.assertEqual(_text.size(1023), "1023.0 B")
        self.assertEqual(_text.size(1024), "1.0 KB")
        self.assertEqual(_text.size(1536), "1.5 KB")
        self.assertEqual(_text.size(1_048_576), "1.0 MB")
        self.assertEqual(_text.size(1024**3), "1.0 GB")
        self.assertEqual(_text.size(1024**4), "1.0 TB")
        self.assertEqual(_text.size(2048 * 1024**4), "2048.0 TB")
        self.assertEqual(_text.size(1024 * 1024 - 1), "1.0 MB")


class DurationTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(_text.duration(0), "0ms")
        self.assertEqual(_text.duration(0.25), "250ms")
        self.assertEqual(_text.duration(0.999), "999ms")
        self.assertEqual(_text.duration(1), "1.0s")
        self.assertEqual(_text.duration(12.4), "12.4s")
        self.assertEqual(_text.duration(59.9), "59.9s")
        self.assertEqual(_text.duration(60), "1m")
        self.assertEqual(_text.duration(90), "1m 30s")
        self.assertEqual(_text.duration(3600), "1h")
        self.assertEqual(_text.duration(3661), "1h 1m")
        self.assertEqual(_text.duration(3601), "1h 1s")


class CountTest(unittest.TestCase):
    def test_singular_and_plural(self):
        self.assertEqual(_text.count(1, "file"), "1 file")
        self.assertEqual(_text.count(0, "file"), "0 files")
        self.assertEqual(_text.count(3, "file"), "3 files")
        self.assertEqual(_text.count(-1, "file"), "-1 files")
        self.assertEqual(_text.count(1.5, "second"), "1.5 seconds")
        self.assertEqual(_text.count(1200, "file"), "1,200 files")

    def test_irregular_plural_is_keyword_only(self):
        self.assertEqual(_text.count(1, "story", plural="stories"), "1 story")
        self.assertEqual(_text.count(2, "story", plural="stories"), "2 stories")
        with self.assertRaises(TypeError):
            _text.count(2, "story", "stories")  # pyright: ignore[reportCallIssue]


class FoldTest(unittest.TestCase):
    def test_diacritics_case_and_whitespace(self):
        self.assertEqual(_text.fold("  Café   naïve  "), "cafe naive")
        self.assertEqual(_text.fold("Đồng Nai - Địa chỉ"), "dong nai - dia chi")
        self.assertEqual(_text.fold("Straße"), "strasse")
        self.assertEqual(_text.fold("a\t\nb   c"), "a b c")
        self.assertEqual(_text.fold("   "), "")


class FilenameTest(unittest.TestCase):
    def test_unsafe_chars_and_whitespace(self):
        for char in '\\/*?:"<>|\x00\x1f\x7f\x80\x9f':
            self.assertEqual(_text.filename(f"a{char}b"), "a_b", repr(char))
        self.assertEqual(_text.filename("Summer: 2026 / day 1?"), "Summer_2026_day_1")
        self.assertEqual(_text.filename(" ._name_. "), "name")

    def test_drops_unicode_format_controls(self):
        self.assertEqual(_text.filename("photo\u202egpj.exe"), "photogpj.exe")
        self.assertEqual(_text.filename("a\u200b\u200e\ufeffb"), "ab")
        self.assertEqual(_text.filename("C\u200bON"), "CON_")
        self.assertEqual(_text.filename("\u202e\u200b"), "untitled")

    def test_fallback_and_max_bytes(self):
        self.assertEqual(_text.filename("..."), "untitled")
        self.assertEqual(_text.filename("", fallback="x"), "x")
        name = _text.filename("é" * 10, max_bytes=5)
        self.assertEqual(name, "éé")
        self.assertLessEqual(len(name.encode()), 5)
        self.assertEqual(_text.filename("a" * 300), "a" * 200)

    def test_reserved_windows_names(self):
        for reserved in ("CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "COM1", "LPT9",
                         "COM\u00b9", "COM\u00b2", "COM\u00b3",
                         "LPT\u00b9", "LPT\u00b2", "LPT\u00b3"):
            self.assertEqual(_text.filename(reserved), f"{reserved}_")
            self.assertEqual(
                _text.filename(f"{reserved.lower()}.txt"), f"{reserved.lower()}_.txt"
            )
        self.assertEqual(_text.filename("CONSOLE"), "CONSOLE")

    def test_options_are_keyword_only(self):
        with self.assertRaises(TypeError):
            _text.filename("a", "b")  # pyright: ignore[reportCallIssue]


if __name__ == "__main__":
    unittest.main()
