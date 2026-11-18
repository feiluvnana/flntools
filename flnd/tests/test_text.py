"""`flnkit.text`: `redact`, `size`, `duration`, `count`, `filename` and `fill`."""

import datetime
import unittest

from flnkit import text as _text


class TextTest(unittest.TestCase):
    def table(self, function, cases, **options):
        for given, expected in cases:
            with self.subTest(given=given):
                self.assertEqual(function(given, **options), expected)

    def test_redact_keeps_scheme_host_port_and_path(self):
        self.table(_text.redact, [
            ("https://user:secret@cdn.example.com:8080/a/b.jpg?oe=1&sig=tok#top",
             "https://cdn.example.com:8080/a/b.jpg"),
            ("http://user@example.com/p", "http://example.com/p"),
            ("//user:pw@cdn.example.com/a.jpg?sig=1#x", "//cdn.example.com/a.jpg"),
            ("//cdn.example.com:8443/path?x=1", "//cdn.example.com:8443/path"),
            ("https://user:pw@[::1]:8080/p?q=1#f", "https://[::1]:8080/p"),
            ("/relative/path?x=1#h", "/relative/path"),
            ("mailto:user@example.com?subject=hi", "mailto:user@example.com"),
            # urlsplit refuses these: they are cut at the query, without user info.
            ("http://user:pw@example.com:99999/a?sig=1", "http://example.com:99999/a"),
            ("http://[::1/a?sig=1#f", "http://[::1/a"),
            ("http://user@[::1/a", "http://[::1/a"),
        ])

    def test_size(self):
        self.table(_text.size, [
            (0, "0.0 B"), (1023, "1023.0 B"), (1024, "1.0 KB"), (1536, "1.5 KB"),
            (1_048_576, "1.0 MB"), (1024**3, "1.0 GB"), (1024**4, "1.0 TB"),
            (2048 * 1024**4, "2048.0 TB"), (1024 * 1024 - 1, "1.0 MB"),
        ])

    def test_duration(self):
        self.table(_text.duration, [
            (0, "0ms"), (0.25, "250ms"), (0.999, "999ms"), (1, "1.0s"), (12.4, "12.4s"),
            (59.9, "59.9s"), (60, "1m"), (90, "1m 30s"), (3600, "1h"), (3661, "1h 1m"),
            (3601, "1h 1s"),
        ])

    def test_count(self):
        for n, expected in ((1, "1 file"), (0, "0 files"), (3, "3 files"), (-1, "-1 files"),
                            (1200, "1,200 files")):
            self.assertEqual(_text.count(n, "file"), expected)
        self.assertEqual(_text.count(1.5, "second"), "1.5 seconds")
        self.assertEqual(_text.count(1, "story", plural="stories"), "1 story")
        self.assertEqual(_text.count(2, "story", plural="stories"), "2 stories")
        with self.assertRaises(TypeError):
            _text.count(2, "story", "stories")

    def test_filename(self):
        self.table(_text.filename, [
            *((f"a{char}b", "a_b") for char in '\\/*?:"<>|\x00\x1f\x7f\x80\x9f'),
            ("Summer: 2026 / day 1?", "Summer_2026_day_1"), (" ._name_. ", "name"),
            # Unicode format controls go.
            ("photo\u202egpj.exe", "photogpj.exe"), ("a\u200b\u200e\ufeffb", "ab"),
            ("C\u200bON", "CON_"), ("\u202e\u200b", "untitled"), ("...", "untitled"),
            ("a" * 300, "a" * 200), ("CONSOLE", "CONSOLE"),
        ])
        reserved = ("CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "COM1", "LPT9",
                    "COM\u00b9", "COM\u00b2", "COM\u00b3", "LPT\u00b9", "LPT\u00b2", "LPT\u00b3")
        self.table(_text.filename, [(name, f"{name}_") for name in reserved]
                   + [(f"{name.lower()}.txt", f"{name.lower()}_.txt") for name in reserved])
        self.assertEqual(_text.filename("", fallback="x"), "x")
        self.assertEqual(_text.filename("é" * 10, max_bytes=5), "éé")
        with self.assertRaises(TypeError):
            _text.filename("a", "b")


class FormatsTest(unittest.TestCase):
    """The templates rule: a missing or empty field is "", and the separator it leaves is dropped."""

    def test_an_empty_field_and_its_separator_are_dropped(self):
        rows = [
            ("{category}/{album}", {"category": "posts"}, "posts"),
            ("{category}/{album}", {"category": "highlights", "album": "Trip"}, "highlights/Trip"),
            ("{category}/{album}", {}, ""),
            ("{date}_{post}{index}", {"post": "C1abc", "date": None, "index": ""}, "C1abc"),
            ("{date}_{post}{index}", {"post": "C1abc", "date": datetime.date(2026, 1, 2), "index": "_1"},
             "2026-01-02_C1abc_1"),
            ("{post}_{idx}", {"post": "71"}, "71"),
            ("{a} - {b}", {"a": "x"}, "x"),
            ("{num:02d}", {"num": 0}, "00"),
            ("{date:%Y}/{post}", {"post": "9", "date": datetime.date(2026, 1, 2)}, "2026/9"),
        ]
        for template, fields, expected in rows:
            with self.subTest(template=template, fields=fields):
                self.assertEqual(_text.fill(template, fields), expected)

    def test_a_values_own_separators_are_kept(self):
        self.assertEqual(_text.fill("{title}{index}", {"title": "a_", "index": ""}, safe=False), "a_")

    def test_every_part_goes_through_a_file_name(self):
        self.assertEqual(_text.fill("{author}/{post} {title}", {"author": "../a/b", "post": 7, "title": "x:y"}),
                         "a_b/7_x_y")
        self.assertEqual(_text.fill("{chat}:{post}", {"chat": "-100", "post": "5"}, safe=False), "-100:5")
        self.assertEqual(_text.fill("{chat}:{post}", {"post": "5"}, safe=False), "5")


if __name__ == "__main__":
    unittest.main()
