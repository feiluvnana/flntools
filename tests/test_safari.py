"""Safari's cookies (`flnd.kit.cookies.parse_binarycookies`, `load("safari")`), from synthetic files.

Every binarycookies file here is built by `binarycookies` below, in a temporary home folder
(`Path.home` is patched), so Safari's real file is never read; `tests/no_profiles.py` makes a read
of the real home's file fail before it opens anything.
"""

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import no_profiles  # noqa: F401  (refuses real browser profiles)

from flnd import _home
from flnd.errors import LoginError, UsageError
from flnd.kit import cookies as _cookies
from flnd.sources.ins import Instagram

# 2030-01-01 00:00:00 UTC, in Unix seconds, and in seconds from 2001-01-01 as Safari keeps it.
EXPIRES = 1_893_456_000
APPLE = EXPIRES - _cookies.APPLE_EPOCH_OFFSET


def cookie(domain, name, value, path="/", *, flags=0, expires=APPLE):
    """One cookie record in Safari's layout: a 56-byte header, then its four strings."""
    strings = [text.encode() + b"\0" for text in (domain, name, path, value)]
    offsets, at = [], 56
    for text in strings:
        offsets.append(at)
        at += len(text)
    header = struct.pack("<8I", at, 0, flags, 0, *offsets) + struct.pack("<2I", 0, 0)
    return header + struct.pack("<2d", expires, 0.0) + b"".join(strings)


def page(*cookies):
    """One page: its tag, the count, each cookie's offset from the page's start, then them."""
    start = 4 + 4 + 4 * len(cookies) + 4
    offsets = []
    for record in cookies:
        offsets.append(start)
        start += len(record)
    return (_cookies.SAFARI_PAGE + struct.pack(f"<I{len(cookies)}I", len(cookies), *offsets)
            + b"\0\0\0\0" + b"".join(cookies))


def binarycookies(*pages):
    """A whole file: `cook`, the page count and sizes (big-endian), the pages, then a trailer."""
    return (b"cook" + struct.pack(f">I{len(pages)}I", len(pages), *map(len, pages))
            + b"".join(pages) + b"\x00" * 8 + b"bplist00")


SAMPLE = binarycookies(
    page(cookie(".instagram.com", "sessionid", "ins-session", flags=5),
         cookie("www.example.com", "theme", "dark", "/settings", expires=0)),
    page(cookie(".instagram.com", "csrftoken", "ins-csrf", flags=1)),
)


class ParseTest(unittest.TestCase):
    def test_every_cookie_of_every_page(self):
        jar = _cookies.parse_binarycookies(SAMPLE)
        self.assertEqual(jar[0], {
            "name": "sessionid", "value": "ins-session", "domain": ".instagram.com", "path": "/",
            "expires": EXPIRES, "secure": True, "httpOnly": True, "sameSite": "Lax"})
        self.assertEqual((jar[1]["name"], jar[1]["path"], jar[1]["expires"], jar[1]["secure"]),
                         ("theme", "/settings", -1, False))
        self.assertEqual((jar[2]["name"], jar[2]["secure"], jar[2]["httpOnly"]),
                         ("csrftoken", True, False))

    def test_a_damaged_file_names_the_place_never_a_value(self):
        whole = page(cookie(".instagram.com", "sessionid", "ins-session"))
        cases = {
            b"nope": "not a Safari binarycookies file",
            b"cook" + struct.pack(">I", 3): "the page table runs past the end",
            binarycookies(b"\x01\x02\x03\x04" + whole[4:]): "page 1 is damaged",
            # The cookie's strings cut off: its size says more than the page holds.
            binarycookies(whole[:-6]): "cookie 1 of page 1 is damaged",
        }
        for data, message in cases.items():
            with self.subTest(message=message), self.assertRaises(ValueError) as caught:
                _cookies.parse_binarycookies(data)
            self.assertIn(message, str(caught.exception))
            self.assertNotIn("ins-session", str(caught.exception))


class SafariHome(unittest.TestCase):
    """A temporary home folder on a pretended platform, where a test writes Safari's file."""

    platform = "darwin"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.enterContext(mock.patch("pathlib.Path.home", return_value=self.home))
        self.enterContext(mock.patch.object(_cookies, "_PLATFORM", self.platform))

    def write(self, relative, data=SAMPLE):
        path = self.home / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        return path


class LoadTest(SafariHome):
    def test_the_container_file_is_read_first(self):
        self.write(_cookies.SAFARI_FILES[0])
        self.write(_cookies.SAFARI_FILES[1], binarycookies(page(cookie("old.example.com", "a", "b"))))
        self.assertEqual([c["name"] for c in _cookies.load("safari")],
                         ["sessionid", "theme", "csrftoken"])

    def test_the_older_place_when_there_is_no_container(self):
        self.write(_cookies.SAFARI_FILES[1])
        self.assertEqual(len(_cookies.load("Safari")), 3)

    def test_no_file(self):
        with self.assertRaises(FileNotFoundError):
            _cookies.load("safari")

    @unittest.skipIf(sys.platform == "win32" or os.geteuid() == 0, "needs file modes")
    def test_macos_privacy_protection_says_how_to_allow_it(self):
        path = self.write(_cookies.SAFARI_FILES[0])
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        with self.assertRaises(LoginError) as caught:
            _cookies.load("safari")
        self.assertIn("Full Disk Access", caught.exception.hint)
        # The login code keeps that advice.
        with self.assertRaises(LoginError) as caught:
            _home.from_browser(Instagram(), browser="safari")
        self.assertIn("can't read Safari's cookies", str(caught.exception))
        self.assertIn("Full Disk Access", caught.exception.hint)

    def test_a_login_keeps_the_sites_cookies_only(self):
        self.write(_cookies.SAFARI_FILES[0])
        jar = _home.from_browser(Instagram(), browser="safari")
        self.assertEqual(sorted(c["name"] for c in jar), ["csrftoken", "sessionid"])

    def test_a_damaged_file_is_a_login_error(self):
        self.write(_cookies.SAFARI_FILES[0], b"nope")
        with self.assertRaises(LoginError) as caught:
            _cookies.load("safari")
        self.assertIn("Cookies.binarycookies: not a Safari binarycookies file", str(caught.exception))


class OffMacTest(SafariHome):
    platform = "linux"

    def test_safari_is_macos_only(self):
        self.write(_cookies.SAFARI_FILES[0])
        for read in (lambda: _cookies.load("safari"),
                     lambda: _home.from_browser(Instagram(), browser="safari")):
            with self.assertRaises(UsageError) as caught:
                read()
            self.assertIn("macOS only", str(caught.exception))


class GuardTest(unittest.TestCase):
    def test_the_real_safari_file_is_refused_before_it_is_read(self):
        self.assertTrue(os.environ.get(_cookies.NO_PROFILES))
        with mock.patch.object(_cookies, "_PLATFORM", "darwin"), \
                mock.patch("pathlib.Path.read_bytes") as read, \
                self.assertRaises(RuntimeError) as caught:
            _cookies.load("safari")
        read.assert_not_called()
        self.assertIn(_cookies.NO_PROFILES, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
