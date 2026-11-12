"""`shared.cookies`: `load`, `from_browser`, `profiles` and the `Jar`, over fixture files only.

Browser profiles are fixture databases under a temporary home folder (`Path.home` is patched),
the Keychain and keyring passwords are faked, and any real program `subprocess` would run fails
the test, so no real browser profile is ever read.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shared
from shared import cookies

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

REPO = Path(__file__).resolve().parent.parent
# 2030-01-01 00:00:00 UTC, in Unix seconds and in Chromium's microseconds since 1601.
EXPIRES = 1_893_456_000
EXPIRES_UTC = (EXPIRES + 11_644_473_600) * 1_000_000
DOMAINS = ["instagram.com", ".instagram.com", "www.instagram.com", "WWW.Instagram.COM",
           "notinstagram.com", "instagram.com.evil.net"]
MATCHING = {"instagram.com", ".instagram.com", "www.instagram.com", "WWW.Instagram.COM"}


def encrypt_chromium(plain, password, iterations, host=None, prefix=b"v10"):
    key = hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", iterations, 16)
    message = (hashlib.sha256(host.encode()).digest() if host else b"") + plain.encode()
    padder = PKCS7(128).padder()
    padded = padder.update(message) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return prefix + encryptor.update(padded) + encryptor.finalize()


def make_chromium_db(path, version, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
    connection.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, path TEXT,"
        " expires_utc INTEGER, is_secure INTEGER, is_httponly INTEGER, samesite INTEGER)"
    )
    connection.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


def make_firefox_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT, path TEXT,"
        " expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER)"
    )
    connection.executemany(
        "INSERT INTO moz_cookies (host, name, value, path, expiry, isSecure, isHttpOnly, sameSite)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    connection.commit()
    connection.close()


def refuse_programs(*args, **kwargs):
    raise AssertionError(f"a real program was run: {args}")


class BrowserTestCase(unittest.TestCase):
    """Points the home folder at a temp dir, and fails if any real program would run."""

    platform = "darwin"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        for patcher in (
            mock.patch("pathlib.Path.home", return_value=self.home),
            mock.patch.object(cookies, "_PLATFORM", self.platform),
            mock.patch.object(cookies.subprocess, "run", side_effect=refuse_programs),
            mock.patch.object(cookies, "_read_keychain_password", return_value="mac-secret"),
            mock.patch.object(cookies, "_read_keyring_password", return_value="linux-secret"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "needs cryptography")
class ChromiumMacTest(BrowserTestCase):
    def chrome_db(self, version, password="mac-secret"):
        database = self.home / "Library/Application Support/Google/Chrome/Default/Network/Cookies"
        hashed = version >= 24
        rows = [
            (domain, f"c{index}", "", encrypt_chromium(f"value{index}", password, 1003, domain if hashed else None),
             "/", EXPIRES_UTC, 1, 1, 2)
            for index, domain in enumerate(DOMAINS)
        ]
        rows.append(("instagram.com", "plain", "visible", b"", "/p", 0, 0, 0, -1))
        make_chromium_db(database, version, rows)

    def test_decrypts_with_host_hash(self):
        self.chrome_db(24)
        jar = cookies.from_browser().filter(domain="instagram.com")
        self.assertEqual({cookie["domain"] for cookie in jar}, MATCHING)
        first = jar.raw[0]
        self.assertEqual(first, {"name": "c0", "value": "value0", "domain": "instagram.com", "path": "/",
                                 "expires": EXPIRES, "secure": True, "httpOnly": True, "sameSite": "Strict"})
        cookies._read_keychain_password.assert_called_once_with("Chrome Safe Storage")

    def test_decrypts_without_host_hash(self):
        self.chrome_db(23)
        jar = cookies.from_browser("chrome").filter(domain="INSTAGRAM.com")
        self.assertEqual(sorted(cookie["value"] for cookie in jar),
                         ["value0", "value1", "value2", "value3", "visible"])

    def test_plain_session_cookie(self):
        self.chrome_db(24)
        plain = next(cookie for cookie in cookies.from_browser() if cookie["name"] == "plain")
        self.assertEqual((plain["value"], plain["expires"], plain["sameSite"], plain["secure"]),
                         ("visible", -1, "Lax", False))

    def test_every_cookie_of_the_profile(self):
        self.chrome_db(24)
        jar = cookies.from_browser()
        self.assertEqual(len(jar), len(DOMAINS) + 1)
        self.assertEqual({cookie["domain"] for cookie in jar}, {*DOMAINS})

    def test_wrong_key_raises_cookies_error_without_the_value(self):
        for version in (23, 24):
            with self.subTest(version=version):
                self.chrome_db(version, password="another")
                with self.assertRaises(cookies.Error) as caught:
                    cookies.from_browser()
                self.assertIn("chrome", str(caught.exception))
                self.assertIsInstance(caught.exception, ValueError)
                self.assertNotIn("value0", str(caught.exception))
                (self.home / "Library/Application Support/Google/Chrome/Default/Network/Cookies").unlink()

    def test_plain_values_need_no_keychain(self):
        database = self.home / "Library/Application Support/Google/Chrome/Default/Network/Cookies"
        make_chromium_db(database, 24, [("example.com", "plain", "visible", b"", "/", 0, 0, 0, 1)])
        self.assertEqual(cookies.from_browser()["plain"], "visible")
        cookies._read_keychain_password.assert_not_called()

    def test_old_cookies_location_and_other_profile(self):
        database = self.home / "Library/Application Support/BraveSoftware/Brave-Browser/Profile 1/Cookies"
        make_chromium_db(database, 20, [
            ("x.com", "auth", "", encrypt_chromium("token", "mac-secret", 1003), "/", EXPIRES_UTC, 1, 0, 1)
        ])
        jar = cookies.from_browser("brave", profile="Profile 1").raw
        self.assertEqual((jar[0]["value"], jar[0]["sameSite"]), ("token", "Lax"))
        cookies._read_keychain_password.assert_called_once_with("Brave Safe Storage")

    def test_missing_database(self):
        with self.assertRaises(FileNotFoundError):
            cookies.from_browser("edge")


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "needs cryptography")
class ChromiumLinuxTest(BrowserTestCase):
    platform = "linux"

    def test_v10_uses_peanuts_and_v11_the_keyring(self):
        database = self.home / ".config/chromium/Default/Cookies"
        make_chromium_db(database, 24, [
            ("a.example", "old", "", encrypt_chromium("one", "peanuts", 1, "a.example"), "/", 0, 0, 0, 0),
            ("a.example", "new", "", encrypt_chromium("two", "linux-secret", 1, "a.example", b"v11"), "/", 0, 0, 0, 0),
        ])
        jar = cookies.from_browser("chromium")
        self.assertEqual([(cookie["value"], cookie["sameSite"]) for cookie in jar], [("one", "None"), ("two", "None")])
        cookies._read_keyring_password.assert_called_once_with("chromium")

    def test_v11_falls_back_to_peanuts_without_keyring(self):
        cookies._read_keyring_password.return_value = None
        make_chromium_db(self.home / ".config/google-chrome/Default/Cookies", 23, [
            ("a.example", "new", "", encrypt_chromium("two", "peanuts", 1, prefix=b"v11"), "/", 0, 0, 0, 0),
        ])
        self.assertEqual(cookies.from_browser()["new"], "two")

    def test_some_undecryptable_values_are_skipped_and_counted(self):
        database = self.home / ".config/google-chrome/Default/Cookies"
        make_chromium_db(database, 24, [
            ("a.example", "good", "", encrypt_chromium("one", "peanuts", 1, "a.example"), "/", 0, 0, 0, 0),
            ("b.example", "app-bound", "", b"v20" + b"s3cr3t-value", "/", 0, 0, 0, 0),
            ("c.example", "damaged", "", encrypt_chromium("two", "another", 1, "c.example"), "/", 0, 0, 0, 0),
            ("d.example", "plain", "visible", b"", "/", 0, 0, 0, 0),
        ])
        with mock.patch.object(cookies.log, "debug") as debug:
            jar = cookies.from_browser()
        self.assertEqual(jar.names, ["good", "plain"])
        self.assertEqual((jar["good"], jar["plain"]), ("one", "visible"))
        debug.assert_called_once()
        self.assertEqual(debug.call_args.kwargs["count"], 2)
        logged = repr(debug.call_args)
        for secret in ("s3cr3t-value", "app-bound", "damaged", "b.example", "c.example"):
            self.assertNotIn(secret, logged)

    def test_every_value_failing_is_a_wrong_key(self):
        make_chromium_db(self.home / ".config/google-chrome/Default/Cookies", 24, [
            ("a.example", "one", "", encrypt_chromium("s3cr3t-value", "another", 1, "a.example"), "/", 0, 0, 0, 0),
            ("a.example", "two", "", b"v20" + b"\0" * 16, "/", 0, 0, 0, 0),
            ("a.example", "plain", "visible", b"", "/", 0, 0, 0, 0),
        ])
        with self.assertRaises(cookies.Error) as caught:
            cookies.from_browser()
        self.assertIn("can't decrypt any of 2 chrome cookies", str(caught.exception))
        self.assertNotIn("s3cr3t-value", str(caught.exception))

    def test_unknown_prefix_raises_cookies_error(self):
        make_chromium_db(self.home / ".config/google-chrome/Default/Cookies", 23, [
            ("a.example", "odd", "", b"v99" + b"\0" * 16, "/", 0, 0, 0, 0),
        ])
        with self.assertRaises(cookies.Error):
            cookies.from_browser()


class BrowserChoiceTest(BrowserTestCase):
    def test_unknown_browser(self):
        with self.assertRaises(ValueError):
            cookies.from_browser("safari")

    def test_windows_chromium_points_to_cdp(self):
        with mock.patch.object(cookies, "_PLATFORM", "win32"):
            with self.assertRaises(RuntimeError) as caught:
                cookies.from_browser("edge")
        self.assertIn("browser.open(cdp=", str(caught.exception))
        self.assertIn("then b.cookies", str(caught.exception))

    def test_missing_cryptography(self):
        make_chromium_db(self.home / "Library/Application Support/Google/Chrome/Default/Network/Cookies", 24, [
            ("x.com", "auth", "", b"v10" + b"\0" * 16, "/", 0, 0, 0, 0),
        ])
        blocked = {name: None for name in sys.modules if name.split(".")[0] == "cryptography"}
        with mock.patch.dict(sys.modules, {**blocked, "cryptography": None}):
            with self.assertRaises(ImportError) as caught:
                cookies.from_browser()
        self.assertIn("flntools[browser]", str(caught.exception))


class FirefoxTest(BrowserTestCase):
    platform = "linux"

    def write_profiles(self, root, text):
        root.mkdir(parents=True, exist_ok=True)
        (root / "profiles.ini").write_text(text, encoding="utf-8")

    def firefox_rows(self):
        return [(domain, f"f{index}", f"v{index}", "/", EXPIRES, 1, 0, 1) for index, domain in enumerate(DOMAINS)]

    def test_default_profile_and_domain_filter(self):
        root = self.home / ".mozilla/firefox"
        self.write_profiles(root, "[Profile1]\nName=other\nIsRelative=1\nPath=other.x\n\n"
                                  "[Profile0]\nName=main\nIsRelative=1\nPath=main.default\nDefault=1\n")
        make_firefox_db(root / "main.default/cookies.sqlite", self.firefox_rows())
        make_firefox_db(root / "other.x/cookies.sqlite", [("instagram.com", "wrong", "no", "/", 0, 0, 0, 0)])
        jar = cookies.from_browser("firefox").filter(domain="instagram.com")
        self.assertEqual({cookie["domain"] for cookie in jar}, MATCHING)
        self.assertNotIn("wrong", jar)
        self.assertEqual(jar.raw[0], {"name": "f0", "value": "v0", "domain": "instagram.com", "path": "/",
                                  "expires": EXPIRES, "secure": True, "httpOnly": False, "sameSite": "Lax"})

    def test_install_section_wins_and_milliseconds(self):
        root = self.home / "Library/Application Support/Firefox"
        self.write_profiles(root, "[Profile0]\nPath=Profiles/old\nDefault=1\n\n"
                                  "[Install4F96D1932A9F858E]\nDefault=Profiles/new\nLocked=1\n")
        make_firefox_db(root / "Profiles/new/cookies.sqlite", [("a.example", "n", "v", "/", EXPIRES * 1000, 0, 1, 0)])
        with mock.patch.object(cookies, "_PLATFORM", "darwin"):
            jar = cookies.from_browser("firefox").raw
        self.assertEqual((jar[0]["expires"], jar[0]["httpOnly"], jar[0]["sameSite"]), (EXPIRES, True, "None"))

    def test_named_profile_on_windows(self):
        root = self.home / "AppData/Roaming/Mozilla/Firefox"
        make_firefox_db(root / "Profiles/work/cookies.sqlite", [("a.example", "n", "v", "/", 0, 0, 0, 2)])
        with mock.patch.object(cookies, "_PLATFORM", "win32"), mock.patch.dict(os.environ, {"APPDATA": str(root.parents[1])}):
            jar = cookies.from_browser("firefox", profile="work").raw
        self.assertEqual((jar[0]["expires"], jar[0]["sameSite"]), (-1, "Strict"))

    def test_missing_profiles_ini(self):
        with self.assertRaises(FileNotFoundError):
            cookies.from_browser("firefox")


class ProfilesTest(BrowserTestCase):
    def test_chromium_profiles_with_a_cookie_database_default_first(self):
        root = self.home / "Library/Application Support/Google/Chrome"
        for name, database in (("Profile 2", "Network/Cookies"), ("Default", "Network/Cookies"),
                               ("Profile 1", "Cookies"), ("System Profile", None)):
            (root / name).mkdir(parents=True)
            if database:
                (root / name / database).parent.mkdir(parents=True, exist_ok=True)
                (root / name / database).write_bytes(b"")
        self.assertEqual(cookies.profiles("chrome"), ["Default", "Profile 1", "Profile 2"])
        self.assertEqual(cookies.profiles("brave"), [])

    def test_firefox_profiles_the_opened_one_first(self):
        root = self.home / "Library/Application Support/Firefox"
        self.assertEqual(cookies.profiles("firefox"), [])
        root.mkdir(parents=True)
        (root / "profiles.ini").write_text("[Profile0]\nPath=Profiles/old\nDefault=1\n\n[Profile1]\nPath=Profiles/new\n\n"
                                           "[Install4F96D1932A9F858E]\nDefault=Profiles/new\n", encoding="utf-8")
        self.assertEqual(cookies.profiles("firefox"), ["Profiles/new", "Profiles/old"])

    def test_unknown_browser(self):
        with self.assertRaises(ValueError):
            cookies.profiles("safari")


class ProgramsTest(unittest.TestCase):
    def test_keychain_failure_raises_cookies_error(self):
        failed = subprocess.CompletedProcess([], 44, stdout="", stderr="not found")
        with mock.patch.object(cookies.subprocess, "run", return_value=failed) as run:
            with self.assertRaises(cookies.Error):
                cookies._read_keychain_password("Chrome Safe Storage")
        self.assertEqual(run.call_args.args[0][:2], ["security", "find-generic-password"])

    def test_keychain_password(self):
        found = subprocess.CompletedProcess([], 0, stdout="pw==\n", stderr="")
        with mock.patch.object(cookies.subprocess, "run", return_value=found):
            self.assertEqual(cookies._read_keychain_password("Chrome Safe Storage"), "pw==")

    def test_keyring_missing_or_empty(self):
        with mock.patch.object(cookies.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(cookies._read_keyring_password("chrome"))
        empty = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with mock.patch.object(cookies.subprocess, "run", return_value=empty):
            self.assertIsNone(cookies._read_keyring_password("chrome"))


class FileTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def write(self, name, text):
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return path


class LoadTest(FileTestCase):
    def test_netscape_with_http_only(self):
        path = self.write("c.txt", "# Netscape HTTP Cookie File\n\n"
                                   ".x.com\tTRUE\t/\tTRUE\t1700000000\tsid\tabc\n"
                                   "#HttpOnly_x.com\tFALSE\t/a\tFALSE\t0\ttoken\tq\tw\n")
        self.assertEqual(cookies.load(path).raw, [
            {"name": "sid", "value": "abc", "domain": ".x.com", "path": "/", "expires": 1700000000,
             "secure": True, "httpOnly": False, "sameSite": "Lax"},
            {"name": "token", "value": "q\tw", "domain": "x.com", "path": "/a", "expires": -1,
             "secure": False, "httpOnly": True, "sameSite": "Lax"},
        ])

    def test_json_list_in_a_txt_file(self):
        entries = [
            {"name": "a", "value": "1", "domain": ".x.com", "path": "/", "expirationDate": 1700000000.5,
             "secure": True, "httpOnly": True, "sameSite": "no_restriction"},
            {"name": "b", "value": "2", "domain": "x.com", "session": True, "sameSite": "strict"},
            {"name": "c", "value": "3", "domain": "x.com", "expires": -1, "sameSite": "Lax"},
        ]
        jar = cookies.load(self.write("c.txt", "  " + json.dumps(entries))).raw
        self.assertEqual([(c["expires"], c["sameSite"], c["httpOnly"]) for c in jar],
                         [(1700000000, "None", True), (-1, "Strict", False), (-1, "Lax", False)])
        self.assertEqual(jar[1]["path"], "/")

    def test_broken_files_name_themselves(self):
        for text in ("[{\"name\": ", "{\"name\": \"a\"}", "[{\"name\": \"a\"}]", "x.com\tTRUE\t/\n",
                     "x.com\tTRUE\t/\tTRUE\tsoon\ta\tb\n", "[{\"name\": \"a\", \"value\": 1, \"domain\": \"x\", \"expires\": \"?\"}]"):
            with self.subTest(text=text):
                path = self.write("broken.json", text)
                with self.assertRaises(cookies.Error) as caught:
                    cookies.load(path)
                self.assertIn("broken.json", str(caught.exception))
                self.assertIsInstance(caught.exception, ValueError)

    def test_not_utf8_names_itself(self):
        path = self.folder / "binary.txt"
        path.write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(cookies.Error) as caught:
            cookies.load(path)
        self.assertIn("binary.txt", str(caught.exception))

    def test_over_16_mb_is_refused_unread(self):
        # A sparse file: 16 MiB and one byte on disk's books, nothing written.
        path = self.folder / "huge.txt"
        with open(path, "wb") as file:
            file.truncate(16 * 1024 * 1024 + 1)
        with mock.patch.object(cookies, "_parse_netscape", side_effect=AssertionError("parsed")):
            with self.assertRaises(cookies.UnsafeError) as caught:
                cookies.load(path)
        self.assertIn("huge.txt", str(caught.exception))
        self.assertIsInstance(caught.exception, shared.UnsafeError)
        self.assertIsInstance(caught.exception, cookies.Error)

    def test_exactly_16_mb_is_read(self):
        path = self.folder / "big.txt"
        line = "x.com\tFALSE\t/\tFALSE\t0\tn\tv\n"
        path.write_text(line + "#" * (16 * 1024 * 1024 - len(line) - 1) + "\n", encoding="utf-8")
        self.assertEqual(path.stat().st_size, 16 * 1024 * 1024)
        self.assertEqual(cookies.load(path).names, ["n"])

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            cookies.load(self.folder / "none.txt")

    def test_domain_filter_on_both_formats(self):
        netscape = "".join(f"{domain}\tFALSE\t/\tFALSE\t0\tn{index}\tv\n" for index, domain in enumerate(DOMAINS))
        listed = json.dumps([{"name": f"n{index}", "value": "v", "domain": domain} for index, domain in enumerate(DOMAINS)])
        for path in (self.write("n.txt", netscape), self.write("j.json", listed)):
            with self.subTest(path=path.name):
                jar = cookies.load(path).filter(domain="Instagram.com")
                self.assertEqual({cookie["domain"] for cookie in jar}, MATCHING)
                self.assertEqual(len(cookies.load(path)), len(DOMAINS))


def cookie(name, value="v", domain=".example.com", path="/", expires=-1):
    return {"name": name, "value": value, "domain": domain, "path": path, "expires": expires,
            "secure": False, "httpOnly": False, "sameSite": "Lax"}


class JarTest(unittest.TestCase):
    def test_reads_by_name(self):
        jar = cookies.Jar([cookie("sid", "1"), cookie("csrf", "2"), cookie("sid", "3", domain="other.example.com")])
        self.assertEqual(jar["sid"], "1")
        self.assertIn("csrf", jar)
        self.assertNotIn("nope", jar)
        self.assertEqual(jar.names, ["sid", "csrf"])
        self.assertEqual(len(jar), 3)
        self.assertEqual([c["value"] for c in jar], ["1", "2", "3"])
        with self.assertRaises(KeyError):
            jar["nope"]

    def test_immutable(self):
        source = [cookie("sid", "1")]
        jar = cookies.Jar(source)
        source[0]["value"] = "changed"
        next(iter(jar))["value"] = "changed"
        jar.raw[0]["value"] = "changed"
        self.assertEqual(jar["sid"], "1")
        with self.assertRaises(AttributeError):
            jar.extra = 1  # type: ignore[attr-defined]

    def test_needs_a_name_and_a_value(self):
        for entry in ({"name": "a"}, {"value": "secret-1"}, "a=1"):
            with self.subTest(entry=entry):
                with self.assertRaises(cookies.Error) as caught:
                    cookies.Jar([entry])  # type: ignore[list-item]
                self.assertNotIn("secret-1", str(caught.exception))

    def test_filter_by_domain_and_names(self):
        jar = cookies.Jar([cookie(f"n{index}", domain=domain) for index, domain in enumerate(DOMAINS)]
                          + [{"name": "bare", "value": "1"}])
        self.assertEqual({c["domain"] for c in jar.filter(domain=".INSTAGRAM.com")}, MATCHING)
        self.assertEqual(jar.filter(names=["n0", "n4", "bare"]).names, ["n0", "n4", "bare"])
        self.assertEqual(jar.filter(names="n1").names, ["n1"])
        self.assertEqual(jar.filter(domain="instagram.com", names=("n0", "n4")).names, ["n0"])
        self.assertEqual(jar.filter(), jar)
        self.assertIsInstance(jar.filter(names=[]), cookies.Jar)
        self.assertEqual(len(jar.filter(names=[])), 0)

    def test_merge_newer_wins_in_place(self):
        saved = cookies.Jar([cookie("sid", "old"), cookie("ct0", "old", expires=2_000_000_000), cookie("lang", "en")])
        # ct0 was rotated with a shorter expiry: the argument still wins, since it is the newer jar.
        refreshed = cookies.Jar([cookie("ct0", "new", domain="EXAMPLE.com", expires=1_900_000_000),
                                 cookie("guest", "g")])
        merged = saved.merge(refreshed)
        self.assertEqual([(c["name"], c["value"]) for c in merged],
                         [("sid", "old"), ("ct0", "new"), ("lang", "en"), ("guest", "g")])
        self.assertEqual(merged.raw[1]["expires"], 1_900_000_000)
        self.assertEqual(saved["ct0"], "old")

    def test_merge_keeps_other_domains_and_paths_apart(self):
        saved = cookies.Jar([cookie("sid", "a"), cookie("sid", "b", path="/app")])
        merged = saved.merge([cookie("sid", "c", domain="other.example"), cookie("sid", "d", path="/app")])
        self.assertEqual([c["value"] for c in merged], ["a", "d", "c"])

    def test_merge_a_missing_path_is_the_root(self):
        merged = cookies.Jar([{"name": "sid", "value": "a", "domain": "example.com"}]).merge(
            [cookie("sid", "b", domain="example.com")])
        self.assertEqual([c["value"] for c in merged], ["b"])

    def test_header_in_order(self):
        jar = cookies.Jar([{"name": "b", "value": "2"}, {"name": "a", "value": "1"}])
        self.assertEqual(jar.header, "b=2; a=1")
        self.assertEqual(cookies.Jar().header, "")

    def test_equality(self):
        self.assertEqual(cookies.Jar([cookie("a")]), cookies.Jar([cookie("a")]))
        self.assertNotEqual(cookies.Jar([cookie("a")]), cookies.Jar([cookie("a", "other")]))
        self.assertNotEqual(cookies.Jar(), [])


class SaveTest(FileTestCase):
    jar = cookies.Jar([
        {"name": "sid", "value": "a b", "domain": ".x.com", "path": "/", "expires": 1700000000,
         "secure": True, "httpOnly": True, "sameSite": "Lax"},
        {"name": "t", "value": "", "domain": "x.com", "path": "/p", "expires": -1,
         "secure": False, "httpOnly": False, "sameSite": "Lax"},
    ])

    def test_round_trip_and_mode(self):
        path = self.folder / "nested" / "ins.txt"
        self.assertEqual(self.jar.save(path), path)
        self.assertEqual(cookies.load(path), self.jar)
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_replaces_and_keeps_0600(self):
        path = self.folder / "ins.txt"
        path.write_text("old", encoding="utf-8")
        os.chmod(path, 0o644)
        self.jar.save(str(path))
        self.assertEqual(cookies.load(path), self.jar)
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_no_temp_file_after_failure(self):
        path = self.folder / "ins.txt"
        with mock.patch.object(cookies.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.jar.save(path)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_line_break_refused(self):
        with self.assertRaises(cookies.Error):
            cookies.Jar([{**self.jar.raw[0], "value": "a\nb"}]).save(self.folder / "ins.txt")
        self.assertEqual(list(self.folder.iterdir()), [])


class SecretsTest(FileTestCase):
    """C6's secrets row: no error message, log line or repr holds a cookie's value."""

    SECRET = "s3cr3t-value"

    def test_repr_shows_names_only(self):
        jar = cookies.Jar([cookie("sessionid", self.SECRET)])
        self.assertEqual(repr(jar), "Jar(sessionid)")
        self.assertNotIn(self.SECRET, str(jar))

    def test_errors_never_hold_a_value(self):
        broken = [
            f"x.com\tFALSE\t/\tFALSE\t0\tsid\t{self.SECRET}\nx.com\tFALSE\t/\n",
            f"x.com\tFALSE\t/\tFALSE\tsoon\tsid\t{self.SECRET}\n",
            json.dumps([{"name": "sid", "value": self.SECRET, "domain": "x.com"}, {"name": "b"}]),
            json.dumps([{"name": "sid", "value": self.SECRET, "domain": "x.com", "expires": "?"}]),
            '[{"name": "sid", "value": "' + self.SECRET,
        ]
        messages = []
        for index, text in enumerate(broken):
            with self.assertRaises(cookies.Error) as caught:
                cookies.load(self.write(f"broken{index}.txt", text))
            messages.append(str(caught.exception))
        path = self.folder / "binary.txt"
        path.write_bytes(self.SECRET.encode() + b"\xff")
        with self.assertRaises(cookies.Error) as caught:
            cookies.load(path)
        messages.append(str(caught.exception))
        for value in (f"{self.SECRET}\n", f"{self.SECRET}\r"):
            with self.assertRaises(cookies.Error) as caught:
                cookies.Jar([cookie("sid", value)]).save(self.folder / "out.txt")
            messages.append(str(caught.exception))
        with self.assertRaises(KeyError) as caught:
            cookies.Jar([cookie("sid", self.SECRET)])["other"]
        messages.append(str(caught.exception))
        for message in messages:
            with self.subTest(message=message):
                self.assertNotIn(self.SECRET, message)


class ImportTest(unittest.TestCase):
    def test_import_loads_neither_cryptography_nor_playwright(self):
        code = ("import sys; from shared import _cookies; "
                "print(sorted({n.split('.')[0] for n in sys.modules} & {'cryptography', 'playwright'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
