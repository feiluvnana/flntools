"""
Unit tests for cookie loading and Netscape format parsing using standard unittest.
"""

import tempfile
import unittest
from pathlib import Path
from flnd.cookies import parse_netscape_cookies, cookies_to_dict


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


if __name__ == "__main__":
    unittest.main()
