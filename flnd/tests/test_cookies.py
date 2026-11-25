"""`flnkit.cookies`: `load` and the `Jar`, over fixture files, browsers, and text.

Profiles are fixture databases under a fake home; no real profile is ever read.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import no_profiles  # noqa: F401  (refuses real browser profiles)

from flnd.errors import DiskError, LoginError, UsageError
from flnkit import cookies as _cookies

REPO = Path(__file__).resolve().parent.parent
EXPIRES = 1_893_456_000
DOMAINS = ["instagram.com", ".instagram.com", "www.instagram.com", "WWW.Instagram.COM",
           "notinstagram.com", "instagram.com.evil.net"]
MATCHING = {"instagram.com", ".instagram.com", "www.instagram.com", "WWW.Instagram.COM"}


class FileTestCase(unittest.TestCase):
    SECRET = "s3cr3t-value"

    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, name, text):
        path = self.folder / name
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        return path


class LoadTest(FileTestCase):
    def test_netscape_with_http_only_a_bom_and_an_empty_expiry(self):
        path = self.write("c.txt", "﻿# Netscape HTTP Cookie File\n\n"
                                   ".x.com\tTRUE\t/\tTRUE\t1700000000\tsid\tabc\n"
                                   "#HttpOnly_x.com\tFALSE\t/a\tFALSE\t\ttoken\tq\tw\n")
        self.assertEqual(_cookies.load(path).list, [
            {"name": "sid", "value": "abc", "domain": ".x.com", "path": "/", "expires": 1700000000,
             "secure": True, "httpOnly": False, "sameSite": "Lax"},
            {"name": "token", "value": "q\tw", "domain": "x.com", "path": "/a", "expires": -1,
             "secure": False, "httpOnly": True, "sameSite": "Lax"},
        ])

    def test_json_list_in_a_txt_file(self):
        entries = [
            {"name": "a", "value": "1", "domain": ".x.com", "path": "/",
             "expirationDate": 1700000000.5, "secure": True, "httpOnly": True,
             "sameSite": "no_restriction"},
            {"name": "b", "value": "2", "domain": "x.com", "session": True, "sameSite": "strict"},
            {"name": "c", "value": "3", "domain": "x.com", "expires": -1, "sameSite": "Lax"},
        ]
        jar = _cookies.load(self.write("c.txt", "  " + json.dumps(entries))).list
        self.assertEqual([(c["expires"], c["sameSite"], c["httpOnly"]) for c in jar],
                         [(1700000000, "None", True), (-1, "Strict", False), (-1, "Lax", False)])
        self.assertEqual(jar[1]["path"], "/")

    def test_raw_cookie_text_and_both_formats_filtered(self):
        self.assertEqual(_cookies.load(".x.com\tTRUE\t/\tFALSE\t0\tsid\tabc\n")["sid"], "abc")
        raw_json = json.dumps([{"name": "tok", "value": "xyz", "domain": ".x.com"}])
        self.assertEqual(_cookies.load(raw_json)["tok"], "xyz")
        netscape = "".join(f"{domain}\tFALSE\t/\tFALSE\t0\tn{index}\tv\n"
                           for index, domain in enumerate(DOMAINS))
        listed = json.dumps([{"name": f"n{index}", "value": "v", "domain": domain}
                             for index, domain in enumerate(DOMAINS)])
        for path in (self.write("n.txt", netscape), self.write("j.json", listed)):
            with self.subTest(path=path.name):
                jar = _cookies.load(path)
                self.assertEqual(len(jar), len(DOMAINS))
                self.assertEqual({c["domain"] for c in jar.filter(domains="Instagram.com")},
                                 MATCHING)

    def test_a_broken_file_names_itself_and_never_a_value(self):
        broken = [
            '[{"name": ', '{"name": "a"}', '[{"name": "a"}]', "x.com\tTRUE\t/\n",
            '[{"name": "a", "value": 1, "domain": "x", "expires": "?"}]',
            f"x.com\tFALSE\t/\tFALSE\t0\tsid\t{self.SECRET}\nx.com\tFALSE\t/\n",
            f"x.com\tFALSE\t/\tFALSE\tsoon\tsid\t{self.SECRET}\n",
            json.dumps([{"name": "sid", "value": self.SECRET, "domain": "x.com"}, {"name": "b"}]),
            json.dumps([{"name": "sid", "value": self.SECRET, "domain": "x", "expires": "?"}]),
            '[{"name": "sid", "value": "' + self.SECRET,
            self.SECRET.encode() + b"\xff\xfe\x00",
        ]
        for index, text in enumerate(broken):
            with self.subTest(text=text):
                with self.assertRaises(LoginError) as caught:
                    _cookies.load(self.write(f"broken{index}.txt", text))
                self.assertIn(f"broken{index}.txt", str(caught.exception))
                self.assertNotIn(self.SECRET, str(caught.exception))
                self.assertNotIsInstance(caught.exception, ValueError)
        with self.assertRaises(FileNotFoundError):
            _cookies.load(self.folder / "none.txt")

    def test_a_file_over_the_limit_is_refused_unread(self):
        line = "x.com\tFALSE\t/\tFALSE\t0\tn\tv\n"
        with mock.patch.object(_cookies, "MAX_SIZE", 64):
            exact = self.write("exact.txt", line + "#" * (64 - len(line) - 1) + "\n")
            self.assertEqual([c["name"] for c in _cookies.load(exact).list], ["n"])
            over = self.write("over.txt", line + "#" * (64 - len(line)) + "\n")
            with mock.patch.object(_cookies, "_parse_netscape",
                                   side_effect=AssertionError("parsed")), \
                    self.assertRaisesRegex(LoginError, "over.txt"):
                _cookies.load(over)
        self.assertEqual(_cookies.MAX_SIZE, 16 * 1024 * 1024)


def cookie(name, value="v", domain=".example.com", path="/", expires=-1):
    return {"name": name, "value": value, "domain": domain, "path": path, "expires": expires,
            "secure": False, "httpOnly": False, "sameSite": "Lax"}


class JarTest(FileTestCase):
    def test_reads_by_name_header_equality_and_repr(self):
        jar = _cookies.Jar([cookie("sid", "1"), cookie("csrf", "2"),
                            cookie("sid", "3", domain="other.example.com")])
        self.assertEqual(jar["sid"], "1")
        self.assertIn("csrf", jar)
        self.assertNotIn("nope", jar)
        self.assertEqual(len(jar), 3)
        self.assertEqual([c["value"] for c in jar], ["1", "2", "3"])
        self.assertEqual(jar.header, "sid=1; csrf=2; sid=3")
        self.assertEqual(_cookies.Jar().header, "")
        self.assertEqual(_cookies.Jar([cookie("a")]), _cookies.Jar([cookie("a")]))
        self.assertNotEqual(_cookies.Jar([cookie("a")]), _cookies.Jar([cookie("a", "other")]))
        self.assertNotEqual(_cookies.Jar(), [])
        secret = _cookies.Jar([cookie("sessionid", self.SECRET)])
        self.assertEqual(repr(secret), "Jar(sessionid)")
        self.assertNotIn(self.SECRET, str(secret))
        with self.assertRaises(KeyError) as caught:
            secret["other"]
        self.assertNotIn(self.SECRET, str(caught.exception))

    def test_immutable(self):
        source = [cookie("sid", "1")]
        jar = _cookies.Jar(source)
        source[0]["value"] = "changed"
        next(iter(jar))["value"] = "changed"
        jar.list[0]["value"] = "changed"
        self.assertEqual(jar["sid"], "1")
        with self.assertRaises(AttributeError):
            jar.extra = 1  # type: ignore[attr-defined]

    def test_needs_a_name_and_a_value_with_the_error_asked_for(self):
        for entry in ({"name": "a"}, {"value": self.SECRET}, "a=1"):
            with self.subTest(entry=entry):
                with self.assertRaises(LoginError) as caught:
                    _cookies.Jar([entry], error=LoginError)  # type: ignore[list-item]
                self.assertNotIn(self.SECRET, str(caught.exception))
        # Importing the cookies once made every Jar raise LoginError; the default is UsageError.
        from flnkit import browser

        with self.assertRaises(UsageError):
            browser.Jar([{"name": "a"}])

    def test_filter_by_domain_and_names(self):
        jar = _cookies.Jar([cookie(f"n{index}", domain=domain)
                            for index, domain in enumerate(DOMAINS)]
                           + [{"name": "bare", "value": "1"}])

        def names(**options):
            return [c["name"] for c in jar.filter(**options).list]

        self.assertEqual({c["domain"] for c in jar.filter(domains=".INSTAGRAM.com")}, MATCHING)
        self.assertEqual(names(domains=["notinstagram.com", "instagram.com.evil.net"]),
                         ["n4", "n5"])
        self.assertEqual(names(domains=[]), [])
        self.assertEqual(names(names=["n0", "n4", "bare"]), ["n0", "n4", "bare"])
        self.assertEqual(names(names="n1"), ["n1"])
        self.assertEqual(names(domains="instagram.com", names=("n0", "n4")), ["n0"])
        self.assertEqual(jar.filter(), jar)
        self.assertIsInstance(jar.filter(names=[]), _cookies.Jar)
        self.assertEqual(len(jar.filter(names=[])), 0)


class SaveTest(FileTestCase):
    jar = _cookies.Jar([
        {"name": "sid", "value": "a b", "domain": ".x.com", "path": "/", "expires": 1700000000,
         "secure": True, "httpOnly": True, "sameSite": "Lax"},
        {"name": "t", "value": "", "domain": "x.com", "path": "/p", "expires": -1,
         "secure": False, "httpOnly": False, "sameSite": "Lax"},
    ])

    def test_round_trip_replaces_and_keeps_0600(self):
        new, old = self.folder / "nested" / "ins.txt", self.write("old.txt", "old")
        os.chmod(old, 0o644)
        for path, given in ((new, new), (old, str(old))):
            with self.subTest(path=path.name):
                self.assertEqual(self.jar.save(given), path)
                self.assertEqual(_cookies.load(path), self.jar)
                if os.name == "posix":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_failure_or_a_line_break_leaves_nothing(self):
        path = self.folder / "ins.txt"
        with mock.patch("os.replace", side_effect=OSError("disk full")), \
                self.assertRaises(DiskError):
            self.jar.save(path)
        for value in ("a\nb", f"{self.SECRET}\n", f"{self.SECRET}\r"):
            with self.subTest(value=value), self.assertRaises(LoginError) as caught:
                _cookies.Jar([cookie("sid", value)], error=LoginError).save(path)
            self.assertNotIn(self.SECRET, str(caught.exception))
        self.assertEqual(list(self.folder.iterdir()), [])


class ImportTest(unittest.TestCase):
    def test_import_loads_neither_cryptography_nor_playwright(self):
        code = ("import sys; from flnkit import cookies; print(sorted({n.split('.')[0] for n in "
                "sys.modules} & {'cryptography', 'playwright'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True,
                                text=True, check=True)
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
