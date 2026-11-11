"""
Unit tests for cookie loading, parsing, and writing using standard unittest.
"""

import json
import tempfile
import unittest
from pathlib import Path
from flnd.cookies import (
    cookies_to_dict, parse_json_cookies, parse_json_text, parse_netscape_cookies, parse_netscape_text,
    readable_cookie_file, sort_cookies, write_netscape,
)


class TestCookies(unittest.TestCase):
    def test_parse_netscape_cookies(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cookie_file = Path(tmp_dir) / "test_cookies.txt"
            content = (
                "# Netscape HTTP Cookie File\n"
                "# https://curl.haxx.se/rfc/cookie_spec.html\n"
                ".example.com\tTRUE\t/\tTRUE\t1825861327\tauth_token\tsecret_token_123\n"
                ".example.com\tTRUE\t/\tTRUE\t1828885327\tct0\tcsrf_token_abc\n"
            )
            cookie_file.write_text(content, encoding="utf-8")

            cookies = parse_netscape_cookies(cookie_file)
            self.assertEqual(len(cookies), 2)

            cookie_map = cookies_to_dict(cookies)
            self.assertEqual(cookie_map["auth_token"], "secret_token_123")
            self.assertEqual(cookie_map["ct0"], "csrf_token_abc")

    def test_parse_json_cookies(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cookie_file = Path(tmp_dir) / "cookies.json"
            cookie_file.write_text(json.dumps([{"name": "a", "value": "1", "domain": ".x.com"}]), encoding="utf-8")
            self.assertEqual(cookies_to_dict(parse_json_cookies(cookie_file)), {"a": "1"})
        self.assertEqual(parse_json_text('{"cookies": []}'), [])
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            parse_json_text("[nope")

    def test_write_netscape_round_trip(self):
        cookies = [
            {"domain": ".x.com", "name": "auth_token", "value": "t", "path": "/", "secure": True, "expires": 99},
            {"domain": "x.com", "name": "ct0", "value": "c", "expirationDate": 12.7},
        ]
        text = write_netscape(cookies)
        self.assertEqual(text.splitlines()[1:], [
            ".x.com\tTRUE\t/\tTRUE\t99\tauth_token\tt",
            "x.com\tFALSE\t/\tFALSE\t12\tct0\tc",
        ])
        parsed = parse_netscape_text(text)
        self.assertEqual([(c["domain"], c["name"], c["expires"], c["secure"]) for c in parsed],
                         [(".x.com", "auth_token", 99, True), ("x.com", "ct0", 12, False)])

    def test_sort_cookies(self):
        text = json.dumps([
            {"domain": ".instagram.com", "name": "sessionid", "value": "1"},
            {"domain": "google.com", "name": "NID", "value": "2"},
        ])
        by_source, skipped = sort_cookies("  \n" + text)
        self.assertEqual(list(by_source), ["ins"])
        self.assertEqual(skipped, 1)
        with self.assertRaisesRegex(ValueError, "^no cookies found$"):
            sort_cookies("# only a comment\n")

    def test_readable_cookie_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            good = Path(tmp_dir) / "good.txt"
            good.write_text(".x.com\tTRUE\t/\tTRUE\t0\tauth_token\tt\n", encoding="utf-8")
            self.assertIsNone(readable_cookie_file(str(good)))
            self.assertEqual(readable_cookie_file(str(Path(tmp_dir) / "missing.txt")),
                             f"no file at {Path(tmp_dir) / 'missing.txt'}")
            self.assertEqual(readable_cookie_file(tmp_dir), f"no file at {tmp_dir}")


if __name__ == "__main__":
    unittest.main()
