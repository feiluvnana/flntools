"""`flnkit.cookies`: `load` and the `Jar`, over fixture files, browsers, and text.

Profiles are fixture databases under a fake home, the Keychain and keyring are faked, and any
real program `subprocess` would run fails the test, so no real profile is ever read.
"""

import base64
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

import no_profiles  # noqa: F401  (refuses real browser profiles)

from flnd.errors import DiskError, LoginError, UsageError
from flnkit import cookies as _cookies

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.padding import PKCS7
    HAS_CRYPTOGRAPHY = True
except ImportError:
    Cipher = algorithms = modes = AESGCM = PKCS7 = None
    HAS_CRYPTOGRAPHY = False

REPO = Path(__file__).resolve().parent.parent
# 2030-01-01 00:00:00 UTC, in Unix seconds and in Chromium's microseconds since 1601.
EXPIRES = 1_893_456_000
EXPIRES_UTC = (EXPIRES + 11_644_473_600) * 1_000_000
DOMAINS = ["instagram.com", ".instagram.com", "www.instagram.com", "WWW.Instagram.COM",
           "notinstagram.com", "instagram.com.evil.net"]
MATCHING = {"instagram.com", ".instagram.com", "www.instagram.com", "WWW.Instagram.COM"}


def encrypt_chromium(plain, password, iterations, host=None, prefix=b"v10"):
    assert PKCS7 and Cipher and algorithms and modes
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
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB,"
        " path TEXT, expires_utc INTEGER, is_secure INTEGER, is_httponly INTEGER,"
        " samesite INTEGER)")
    connection.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


FIREFOX_INSERT = ("INSERT INTO moz_cookies (host, name, value, path, expiry, isSecure, isHttpOnly,"
                  " sameSite)")


def make_firefox_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT,"
        " path TEXT, expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER)")
    connection.executemany(FIREFOX_INSERT + " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


def refuse_programs(*args, **kwargs):
    raise AssertionError(f"a real program was run: {args}")


class BrowserTestCase(unittest.TestCase):
    """Points the home folder at a temp dir, and fails if any real program would run."""

    platform = "darwin"

    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch("pathlib.Path.home", return_value=self.home))
        self.enterContext(mock.patch.object(_cookies, "_PLATFORM", self.platform))
        self.enterContext(mock.patch.object(_cookies.subprocess, "run",
                                            side_effect=refuse_programs))
        self.keychain = self.enterContext(
            mock.patch.object(_cookies, "_read_keychain_password", return_value="mac-secret"))
        self.keyring = self.enterContext(
            mock.patch.object(_cookies, "_read_keyring_password", return_value="linux-secret"))


CHROME_MAC = "Library/Application Support/Google/Chrome/Default/Network/Cookies"


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "needs cryptography")
class ChromiumMacTest(BrowserTestCase):
    def chrome_db(self, version, password="mac-secret"):
        hashed = version >= 24
        rows = [(domain, f"c{index}", "",
                 encrypt_chromium(f"value{index}", password, 1003, domain if hashed else None),
                 "/", EXPIRES_UTC, 1, 1, 2)
                for index, domain in enumerate(DOMAINS)]
        rows.append(("instagram.com", "plain", "visible", b"", "/p", 0, 0, 0, -1))
        make_chromium_db(self.home / CHROME_MAC, version, rows)

    def test_decrypts_with_and_without_the_host_hash(self):
        for version in (23, 24):
            with self.subTest(version=version):
                self.chrome_db(version)
                jar = _cookies.load("chrome")
                self.assertEqual({cookie["domain"] for cookie in jar}, {*DOMAINS})
                self.assertEqual(len(jar), len(DOMAINS) + 1)
                matching = jar.filter(domains="INSTAGRAM.com")
                self.assertEqual({cookie["domain"] for cookie in matching}, MATCHING)
                self.assertEqual(sorted(cookie["value"] for cookie in matching),
                                 ["value0", "value1", "value2", "value3", "visible"])
                self.assertEqual(matching.list[0], {
                    "name": "c0", "value": "value0", "domain": "instagram.com", "path": "/",
                    "expires": EXPIRES, "secure": True, "httpOnly": True, "sameSite": "Strict"})
                plain = next(cookie for cookie in jar if cookie["name"] == "plain")
                self.assertEqual(
                    (plain["value"], plain["expires"], plain["sameSite"], plain["secure"]),
                    ("visible", -1, "Lax", False))
                self.keychain.assert_called_once_with("Chrome Safe Storage")
                self.keychain.reset_mock()
                (self.home / CHROME_MAC).unlink()

    def test_wrong_key_raises_a_login_error_without_the_value(self):
        for version in (23, 24):
            with self.subTest(version=version):
                self.chrome_db(version, password="another")
                with self.assertRaises(LoginError) as caught:
                    _cookies.load("chrome")
                self.assertIn("chrome", str(caught.exception))
                self.assertNotIsInstance(caught.exception, ValueError)
                self.assertNotIn("value0", str(caught.exception))
                (self.home / CHROME_MAC).unlink()

    def test_plain_values_need_no_keychain_and_a_missing_database_raises(self):
        make_chromium_db(self.home / CHROME_MAC, 24,
                         [("example.com", "plain", "visible", b"", "/", 0, 0, 0, 1)])
        self.assertEqual(_cookies.load("chrome")["plain"], "visible")
        self.keychain.assert_not_called()
        with self.assertRaises(FileNotFoundError):
            _cookies.load("edge")

    def test_old_cookies_location_and_other_profile(self):
        database = self.home / ("Library/Application Support/BraveSoftware/Brave-Browser/"
                                "Profile 1/Cookies")
        make_chromium_db(database, 20, [("x.com", "auth", "",
                                         encrypt_chromium("token", "mac-secret", 1003), "/",
                                         EXPIRES_UTC, 1, 0, 1)])
        jar = _cookies.load("brave", profile="Profile 1").list
        self.assertEqual((jar[0]["value"], jar[0]["sameSite"]), ("token", "Lax"))
        self.keychain.assert_called_once_with("Brave Safe Storage")


CHROME_LINUX = ".config/google-chrome/Default/Cookies"


def linux_row(host, name, plain, password="peanuts", prefix=b"v10"):
    return (host, name, "", encrypt_chromium(plain, password, 1, host, prefix), "/", 0, 0, 0, 0)


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "needs cryptography")
class ChromiumLinuxTest(BrowserTestCase):
    platform = "linux"

    def test_v10_uses_peanuts_and_v11_the_keyring_or_peanuts_without_one(self):
        make_chromium_db(self.home / ".config/chromium/Default/Cookies", 24, [
            linux_row("a.example", "old", "one"),
            linux_row("a.example", "new", "two", "linux-secret", b"v11")])
        jar = _cookies.load("chromium")
        self.assertEqual([(cookie["value"], cookie["sameSite"]) for cookie in jar],
                         [("one", "None"), ("two", "None")])
        self.keyring.assert_called_once_with("chromium")
        self.keyring.return_value = None
        make_chromium_db(self.home / CHROME_LINUX, 23, [
            ("a.example", "new", "", encrypt_chromium("two", "peanuts", 1, prefix=b"v11"), "/",
             0, 0, 0, 0)])
        self.assertEqual(_cookies.load("chrome")["new"], "two")

    def test_some_undecryptable_values_are_skipped_and_counted(self):
        make_chromium_db(self.home / CHROME_LINUX, 24, [
            linux_row("a.example", "good", "one"),
            ("b.example", "app-bound", "", b"v20" + b"s3cr3t-value", "/", 0, 0, 0, 0),
            linux_row("c.example", "damaged", "two", "another"),
            ("d.example", "plain", "visible", b"", "/", 0, 0, 0, 0)])
        with self.assertLogs("flnkit.cookies", "DEBUG") as said:
            jar = _cookies.load("chrome")
        self.assertEqual([c["name"] for c in jar.list], ["good", "plain"])
        self.assertEqual((jar["good"], jar["plain"]), ("one", "visible"))
        [record] = said.records
        self.assertEqual(record.fields["count"], 2)
        logged = repr((record.msg, record.fields))
        for secret in ("s3cr3t-value", "app-bound", "damaged", "b.example", "c.example"):
            self.assertNotIn(secret, logged)

    def test_every_value_failing_or_an_unknown_prefix_is_a_wrong_key(self):
        make_chromium_db(self.home / CHROME_LINUX, 24, [
            linux_row("a.example", "one", "s3cr3t-value", "another"),
            ("a.example", "two", "", b"v20" + b"\0" * 16, "/", 0, 0, 0, 0),
            ("a.example", "plain", "visible", b"", "/", 0, 0, 0, 0)])
        with self.assertRaises(LoginError) as caught:
            _cookies.load("chrome")
        self.assertIn("can't decrypt any of 2 chrome cookies", str(caught.exception))
        self.assertNotIn("s3cr3t-value", str(caught.exception))
        (self.home / CHROME_LINUX).unlink()
        make_chromium_db(self.home / CHROME_LINUX, 23,
                         [("a.example", "odd", "", b"v99" + b"\0" * 16, "/", 0, 0, 0, 0)])
        with self.assertRaises(LoginError):
            _cookies.load("chrome")


class BrowserChoiceTest(BrowserTestCase):
    def test_an_unknown_browser_or_no_cryptography_raises(self):
        with self.assertRaises(UsageError):
            _cookies.load("opera", profile="Default")
        make_chromium_db(self.home / CHROME_MAC, 24,
                         [("x.com", "auth", "", b"v10" + b"\0" * 16, "/", 0, 0, 0, 0)])
        blocked = {name: None for name in sys.modules if name.split(".")[0] == "cryptography"}
        with mock.patch.dict(sys.modules, {**blocked, "cryptography": None}), \
                self.assertRaisesRegex(ImportError, "pip install cryptography"):
            _cookies.load("chrome")

    @unittest.skipUnless(HAS_CRYPTOGRAPHY, "needs cryptography")
    def test_windows_decrypts_with_dpapi_and_aesgcm_and_needs_local_state(self):
        local_app = self.home / "AppData/Local"
        user_data = local_app / "Microsoft/Edge/User Data"
        user_data.mkdir(parents=True)
        master_key, nonce = os.urandom(32), os.urandom(12)
        sealed = base64.b64encode(b"DPAPI" + b"sealed-key").decode()
        (user_data / "Local State").write_text(
            json.dumps({"os_crypt": {"encrypted_key": sealed}}), encoding="utf-8")
        payload = hashlib.sha256(b"example.com").digest() + b"win-secret"
        assert AESGCM is not None
        value = b"v10" + nonce + AESGCM(master_key).encrypt(nonce, payload, None)
        row = ("example.com", "sid", "", value, "/", EXPIRES_UTC, 1, 1, 1)
        make_chromium_db(user_data / "Default/Network/Cookies", 24, [row])
        make_chromium_db(local_app / "Google/Chrome/User Data/Default/Network/Cookies", 24, [row])
        with mock.patch.object(_cookies, "_PLATFORM", "win32"), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app)}), \
                mock.patch.object(_cookies, "_dpapi_unprotect", return_value=master_key) as dpapi:
            self.assertEqual(_cookies.load("edge")["sid"], "win-secret")
            dpapi.assert_called_once_with(b"sealed-key")
            with self.assertRaises(LoginError):
                _cookies.load("chrome")  # no Local State beside it


class FirefoxTest(BrowserTestCase):
    platform = "linux"

    def test_default_profile_and_domain_filter(self):
        root = self.home / ".mozilla/firefox"
        root.mkdir(parents=True)
        (root / "profiles.ini").write_text(
            "[Profile1]\nName=other\nIsRelative=1\nPath=other.x\n\n"
            "[Profile0]\nName=main\nIsRelative=1\nPath=main.default\nDefault=1\n")
        make_firefox_db(root / "main.default/cookies.sqlite",
                        [(domain, f"f{index}", f"v{index}", "/", EXPIRES, 1, 0, 1)
                         for index, domain in enumerate(DOMAINS)])
        make_firefox_db(root / "other.x/cookies.sqlite",
                        [("instagram.com", "wrong", "no", "/", 0, 0, 0, 0)])
        jar = _cookies.load("firefox").filter(domains="instagram.com")
        self.assertEqual({cookie["domain"] for cookie in jar}, MATCHING)
        self.assertNotIn("wrong", jar)
        self.assertEqual(jar.list[0], {"name": "f0", "value": "v0", "domain": "instagram.com",
                                       "path": "/", "expires": EXPIRES, "secure": True,
                                       "httpOnly": False, "sameSite": "Lax"})

    def test_install_section_wins_and_milliseconds(self):
        root = self.home / "Library/Application Support/Firefox"
        root.mkdir(parents=True)
        (root / "profiles.ini").write_text(
            "[Profile0]\nPath=Profiles/old\nDefault=1\n\n"
            "[Install4F96D1932A9F858E]\nDefault=Profiles/new\nLocked=1\n")
        make_firefox_db(root / "Profiles/new/cookies.sqlite",
                        [("a.example", "n", "v", "/", EXPIRES * 1000, 0, 1, 0)])
        with mock.patch.object(_cookies, "_PLATFORM", "darwin"):
            jar = _cookies.load("firefox").list
        self.assertEqual((jar[0]["expires"], jar[0]["httpOnly"], jar[0]["sameSite"]),
                         (EXPIRES, True, "None"))

    def test_named_profile_on_windows_and_a_missing_profiles_ini(self):
        with self.assertRaises(FileNotFoundError):
            _cookies.load("firefox")
        root = self.home / "AppData/Roaming/Mozilla/Firefox"
        make_firefox_db(root / "Profiles/work/cookies.sqlite",
                        [("a.example", "n", "v", "/", 0, 0, 0, 2)])
        with mock.patch.object(_cookies, "_PLATFORM", "win32"), \
                mock.patch.dict(os.environ, {"APPDATA": str(root.parents[1])}):
            jar = _cookies.load("firefox", profile="work").list
        self.assertEqual((jar[0]["expires"], jar[0]["sameSite"]), (-1, "Strict"))


class ProgramsTest(unittest.TestCase):
    def test_the_keychain_and_the_keyring(self):
        def answer(code, out):
            return mock.patch.object(_cookies.subprocess, "run",
                                     return_value=subprocess.CompletedProcess([], code, out, ""))

        with answer(44, ""), self.assertRaises(LoginError):
            _cookies._read_keychain_password("Chrome Safe Storage")
        with answer(0, "pw==\n") as run:
            self.assertEqual(_cookies._read_keychain_password("Chrome Safe Storage"), "pw==")
        self.assertEqual(run.call_args.args[0][:2], ["security", "find-generic-password"])
        with mock.patch.object(_cookies.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(_cookies._read_keyring_password("chrome"))
        with answer(1, ""):
            self.assertIsNone(_cookies._read_keyring_password("chrome"))


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

    def test_sqlite_backup_reads_open_wal_db(self):
        db = self.folder / "cookies.sqlite"
        make_firefox_db(db, [("a.example", "old", "1", "/", 0, 0, 0, 0)])
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(FIREFOX_INSERT + " VALUES ('a.example', 'live', '2', '/', 0, 0, 0, 0)")
        conn.commit()
        with _cookies._open_copy(db) as copy:
            names = [r[0] for r in copy.execute("SELECT name FROM moz_cookies").fetchall()]
        self.assertEqual(names, ["old", "live"])


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
        with mock.patch.object(_cookies.os, "replace", side_effect=OSError("disk full")), \
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
