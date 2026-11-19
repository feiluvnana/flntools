"""flnkit/vault.py: cookie jars in the vault, the one-time move of plain cookie files, whether a
source is logged in, logout, and secrets per source and name.

No browser, no network: cookie files are written by the tests, and the vault is a real one
in a temporary `.flnd`.
"""

import io
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import no_profiles  # noqa: F401  (refuses real browser profiles)

from flnd.engine import auth as _auth
from flnd.engine import home as _home
from flnd.engine import store as _store
from flnd.plugins.ins import Instagram
from flnd.ui.log import ui
from flnkit import cookies as _cookies
from flnkit import net as _net
from flnkit import vault as _vault

NETSCAPE = ("# Netscape HTTP Cookie File\n"
            ".instagram.com\tTRUE\t/\tTRUE\t1825861327\tsessionid\tins-session\n")


@contextmanager
def raw(home):
    """The vault's `secrets.db`, opened as any SQLite tool would, to read or tamper with its rows."""
    db = sqlite3.connect(home / _store.SECRETS, isolation_level=None)
    try:
        yield db
    finally:
        db.close()


class AuthCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"
        # The key outside the home, as `~/.config/flnd/key` is.
        self.key = Path(folder.name) / "config" / "key"
        self.enterContext(mock.patch.object(_auth, "KEYFILE", self.key))


class CookieFileTest(AuthCase):
    @unittest.skipIf(os.name == "nt", "E: is a drive letter on Windows")
    def test_legacy_dirs_are_gone(self):
        """Fix #2: `E:/…` was read relative to the working directory on macOS and Linux."""
        self.assertFalse(hasattr(_vault, "LEGACY_DIRS"))
        legacy = self.home.parent / "E:" / "flntools" / ".flnd" / "cookies"
        legacy.mkdir(parents=True)
        (legacy / "ins.txt").write_text("# Netscape HTTP Cookie File\n"
                                        ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\tx\n")
        previous = Path.cwd()
        os.chdir(self.home.parent)
        try:
            self.assertEqual(_home.load(self.home, "ins"), _cookies.Jar())
        finally:
            os.chdir(previous)

    def test_logged_in_and_logout_see_cookie_jars_and_sessions(self):
        self.assertFalse(_home.logged_in(self.home, "ins"))
        (self.home / "cookies").mkdir(parents=True)
        (self.home / "cookies" / "ins.json").write_text("[]")
        _home.save(self.home, "twt", _cookies.Jar())
        with _home.secrets(self.home, "tlg", create=True) as secrets:
            secrets["session"] = "s"
        # A cookie login counts as logged in (moved from a plain file or not), and so does tlg's session.
        self.assertEqual([code for code in ("ins", "twt", "tlg", "tik") if _home.logged_in(self.home, code)],
                         ["ins", "twt", "tlg"])
        self.assertEqual([_home.logout(self.home, code) for code in ("ins", "twt", "tlg", "tik")],
                         [True, True, True, False])
        self.assertFalse((self.home / "cookies").exists())
        self.assertFalse(any(_home.logged_in(self.home, code) for code in ("ins", "twt", "tlg")))


class VaultedCookiesTest(AuthCase):
    """B7: every credential is sealed in the vault, and the plain cookie files of before move into it once."""

    def setUp(self):
        super().setUp()
        # A fake stream, so a move that fails warns where the test can read it.
        self.dest = io.StringIO()
        scope = ui.scope(dest=self.dest, width=400, color=False)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def warnings(self):
        return [line for line in self.dest.getvalue().splitlines() if line.startswith("! ")]

    def write(self, name, text=NETSCAPE):
        path = self.home / "cookies" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o600)
        return path

    def test_a_login_stores_its_jar_only_in_the_vault(self):
        export = self.home.parent / "cookies.txt"
        export.write_text(NETSCAPE)
        message = _home.store(Instagram(), _home.from_file(Instagram(), export), home=self.home, origin=export.name)
        self.assertEqual(message, "ins: logged in from cookies.txt (1 cookie)")
        # Read once and never copied: the export is where it was, and `.flnd/` holds only the vault.
        self.assertTrue(export.is_file())
        self.assertEqual(sorted(path.name for path in self.home.rglob("*") if path.is_file()),
                         ["flnd.db", "secret.db"])
        self.assertEqual([(cookie["name"], cookie["domain"]) for cookie in _home.load(self.home, "ins")],
                         [("sessionid", ".instagram.com")])
        # The origin isn't a secret: it is a row of flnd.db's `accounts`.
        with raw(self.home) as db:
            self.assertEqual(db.execute("SELECT account, name FROM secrets").fetchall(), [(1, "cookies")])
            [(sealed,)] = db.execute("SELECT sealed FROM secrets")
            self.assertNotIn(b"ins-session", sealed)
        self.assertEqual(_home.origin(self.home, "ins"), "file")

    def test_a_plain_cookie_file_moves_into_the_vault_on_the_first_run(self):
        self.write("ins.txt")
        self.assertEqual([cookie["value"] for cookie in _home.load(self.home, "ins")], ["ins-session"])
        self.assertFalse((self.home / "cookies").exists())
        self.assertTrue(_home.logged_in(self.home, "ins"))
        self.assertEqual([cookie["value"] for cookie in _home.load(self.home, "ins")], ["ins-session"])

    def test_the_move_keeps_the_file_when_the_vault_write_fails(self):
        path = self.write("ins.txt")
        with mock.patch.object(_auth.Vault, "__setitem__", side_effect=OSError("disk full")):
            self.assertFalse(_home.logged_in(self.home, "ins"))
        self.assertEqual(path.read_text(), NETSCAPE)
        self.assertEqual(self.warnings(), ["! ins: can't move ins.txt into the vault: disk full; kept it"])
        # The next run tries again, and moves it.
        self.assertTrue(_home.logged_in(self.home, "ins"))
        self.assertFalse(path.exists())

    def test_a_txt_wins_over_a_json_and_other_sources_files_stay(self):
        self.write("ins.txt")
        self.write("ins.json", '[{"domain": ".instagram.com", "name": "sessionid", "value": "older"}]')
        other = self.write("twt.txt", "# Netscape HTTP Cookie File\n")
        self.assertEqual([cookie["value"] for cookie in _home.load(self.home, "ins")], ["ins-session"])
        self.assertEqual(sorted(path.name for path in (self.home / "cookies").iterdir()), ["twt.txt"])
        self.assertTrue(other.is_file())
        _home.load(self.home, "twt")
        self.assertFalse((self.home / "cookies").exists())

    def test_a_vaulted_jar_is_newer_than_a_plain_file(self):
        _home.save(self.home, "ins", _cookies.Jar([
            {"name": "sessionid", "value": "new", "domain": ".instagram.com"}]))
        self.write("ins.txt")
        self.assertEqual([cookie["value"] for cookie in _home.load(self.home, "ins")], ["new"])
        self.assertFalse((self.home / "cookies").exists())

    def test_logout_clears_the_jar_with_the_other_secrets(self):
        _home.save(self.home, "ins",
                   _cookies.Jar([{"name": "sessionid", "value": "s", "domain": ".instagram.com"}]))
        with _home.secrets(self.home, "ins", create=True) as secrets:
            secrets["token"] = "t"
        self.assertTrue(_home.logout(self.home, "ins"))
        with _home.secrets(self.home, "ins", create=True) as secrets:
            self.assertEqual(dict(secrets), {})
        self.assertEqual(_home.load(self.home, "ins"), _cookies.Jar())
        self.assertFalse(_home.logout(self.home, "ins"))

    def test_logout_removes_a_plain_file_the_vault_couldnt_take(self):
        path = self.write("ins.txt")
        with mock.patch.object(_auth.Vault, "__setitem__", side_effect=OSError("disk full")):
            self.assertTrue(_home.logout(self.home, "ins"))
        self.assertFalse(path.exists())

    def test_an_api_key_alone_is_not_a_login_and_a_session_is(self):
        with _home.secrets(self.home, "tlg", create=True) as secrets:
            secrets.update(api_id="1", api_hash="h")
        self.assertFalse(_home.logged_in(self.home, "tlg"))
        self.assertTrue(_home.api_key_saved(self.home, "tlg"))
        with _home.secrets(self.home, "tlg", create=True) as secrets:
            secrets["session"] = "s"
        self.assertTrue(_home.logged_in(self.home, "tlg"))
        self.assertFalse(_home.api_key_saved(self.home, "tlg"))

    def test_logout_removes_an_api_key_too(self):
        with _home.secrets(self.home, "tlg", create=True) as secrets:
            secrets.update(api_id="1", api_hash="h")
        self.assertTrue(_home.logout(self.home, "tlg"))
        self.assertFalse(_home.api_key_saved(self.home, "tlg"))
        self.assertFalse(_home.logout(self.home, "tlg"))

    def test_logged_in_creates_no_database(self):
        self.assertFalse(_home.logged_in(self.home, "tlg"))
        self.assertFalse(_home.api_key_saved(self.home, "tlg"))
        self.assertFalse(_home.logout(self.home, "tlg"))
        self.assertFalse(self.home.exists())


class WriteBackTest(AuthCase):
    """B5: the cookies the site rotated during a run update the saved jar, for names it already holds."""

    def setUp(self):
        super().setUp()
        self.jar = [{"name": "ct0", "value": "old-ct0", "domain": ".x.example", "path": "/", "expires": 1900000000},
                    {"name": "auth_token", "value": "login-token", "domain": ".x.example", "path": "/"}]
        _home.save(self.home, "twt", _cookies.Jar(self.jar))

    def current(self, **values):
        session = _net.Session(cookies=[{"name": name, "value": value, "domain": ".x.example", "path": "/"}
                                        for name, value in values.items()])
        return session.jar

    def test_a_rotated_ct0_is_saved_and_the_rest_kept(self):
        self.assertEqual(_home.write_back(self.home, "twt", self.current(ct0="new-ct0", auth_token="login-token")),
                         ["ct0"])
        saved = _home.load(self.home, "twt")
        self.assertEqual((saved["ct0"], saved["auth_token"], len(saved)), ("new-ct0", "login-token", 2))
        self.assertEqual([cookie["expires"] for cookie in saved if cookie["name"] == "ct0"], [1900000000])

    def test_a_rotated_cookie_for_a_subdomain_still_updates_the_one_it_replaces(self):
        session = _net.Session(cookies=[{"name": "ct0", "value": "new-ct0", "domain": "api.x.example", "path": "/"}])
        self.assertEqual(_home.write_back(self.home, "twt", session.jar), ["ct0"])
        self.assertEqual(_home.load(self.home, "twt")["ct0"], "new-ct0")

    def test_a_new_tracking_cookie_is_never_added(self):
        self.assertEqual(_home.write_back(self.home, "twt", self.current(ct0="old-ct0", _ga="track", guest_id="g")), [])
        self.assertEqual([c["name"] for c in _home.load(self.home, "twt").list], ["ct0", "auth_token"])
        self.assertEqual(_home.write_back(self.home, "twt", self.current(ct0="new-ct0", _ga="track")), ["ct0"])
        self.assertEqual([c["name"] for c in _home.load(self.home, "twt").list], ["ct0", "auth_token"])

    def test_nothing_is_written_when_nothing_changed_or_there_is_no_login(self):
        with mock.patch.object(_home, "save") as save:
            self.assertEqual(_home.write_back(self.home, "twt", self.current(ct0="old-ct0", auth_token="login-token")), [])
            # An empty value is a cookie the site is clearing, never a refresh.
            self.assertEqual(_home.write_back(self.home, "twt", self.current(ct0="")), [])
            self.assertEqual(_home.write_back(self.home, "twt", []), [])
            self.assertEqual(_home.write_back(self.home, "tlg", self.current(ct0="x")), [])
            self.assertEqual(_home.write_back(self.home / "never", "twt", self.current(ct0="x")), [])
        save.assert_not_called()
        self.assertFalse((self.home / "never").exists())

    def test_no_cookie_value_reaches_a_log_line(self):
        with mock.patch.object(_home, "ui") as log:
            _home.write_back(self.home, "twt", self.current(ct0="new-ct0"))
            with mock.patch.object(_home, "save", side_effect=OSError("disk full: new-ct0")):
                self.assertEqual(_home.write_back(self.home, "twt", self.current(ct0="newer-ct0")), [])
        text = repr(log.mock_calls)
        for value in ("old-ct0", "new-ct0", "newer-ct0", "login-token"):
            self.assertNotIn(value, text)
        self.assertIn("ct0", repr(log.debug.mock_calls))
        log.warn.assert_called_once()

    def test_a_vault_that_cannot_be_written_warns_and_does_not_fail(self):
        with mock.patch.object(_home, "save", side_effect=OSError("read-only")), mock.patch.object(_home, "ui") as log:
            self.assertEqual(_home.write_back(self.home, "twt", self.current(ct0="new-ct0")), [])
        log.warn.assert_called_once()
        self.assertEqual(_home.load(self.home, "twt")["ct0"], "old-ct0")


class VaultTest(AuthCase):
    def test_sealed_round_trip(self):
        with _home.secrets(self.home, "tlg", create=True) as secrets:
            secrets["session"] = "1BVts-secret"
            secrets["api_hash"] = "abc"
        with _home.secrets(self.home, "tlg") as secrets:
            self.assertEqual(dict(secrets), {"session": "1BVts-secret", "api_hash": "abc"})
        with raw(self.home) as db:
            [(stored,)] = db.execute("SELECT sealed FROM secrets WHERE name = 'session'")
        self.assertEqual(stored[0], 1)
        self.assertNotIn(b"1BVts-secret", stored)
        self.assertEqual(len(self.key.read_bytes()), 32)

    def test_a_missing_or_wrong_key_keeps_the_rows(self):
        with _home.secrets(self.home, "ins", create=True) as secrets:
            secrets["cookies"] = "[]"
        kept = self.key.read_bytes()
        for name, change in (("missing", self.key.unlink), ("wrong", lambda: self.key.write_bytes(bytes(32)))):
            with self.subTest(name), _home.secrets(self.home, "ins") as secrets:
                change()
                with self.assertRaises(_auth.LoginError) as raised:
                    secrets["cookies"]
                self.assertEqual((str(raised.exception), raised.exception.hint),
                                 ("the vault's key is gone", "flnd auth login ins"))
                self.assertIn("cookies", secrets)
                self.key.unlink(missing_ok=True)
                self.key.write_bytes(kept)
                self.assertEqual(secrets["cookies"], "[]")

    def test_plaintext_is_sealed_on_first_open(self):
        path = self.home / _store.SECRETS
        path.parent.mkdir(parents=True)
        with raw(self.home) as db:
            db.execute("CREATE TABLE secrets (slot TEXT NOT NULL, name TEXT NOT NULL,"
                       " value TEXT NOT NULL, PRIMARY KEY (slot, name)) WITHOUT ROWID")
            db.executemany("INSERT INTO secrets VALUES (?, ?, ?)",
                           [("tlg", "session", "plain-session"), ("ins", "cookies", "[]")])
        with _home.secrets(self.home, "tlg") as secrets:
            self.assertEqual(dict(secrets), {"session": "plain-session"})
        with _store.open(self.home) as db:
            self.assertEqual([(row.plugin, row.name, row.default) for row in db.accounts()],
                             [("ins", "default", True), ("tlg", "default", True)])
        stored = b"".join(found.read_bytes() for found in path.parent.glob("secret.db*"))
        self.assertNotIn(b"plain-session", stored)

    def test_sources_are_separate(self):
        with _home.secrets(self.home, "tlg", create=True) as tlg, _home.secrets(self.home, "loi") as loi:
            tlg["session"] = "t"
            self.assertNotIn("session", loi)
            self.assertEqual(len(loi), 0)
            loi["session"] = "l"
            del tlg["session"]
            self.assertEqual(dict(loi), {"session": "l"})

    def test_no_key_file_until_a_secret_is_written(self):
        with _home.secrets(self.home, "tlg", create=True) as secrets:
            self.assertEqual(len(secrets), 0)
        self.assertFalse(self.key.exists())

    def test_the_secret_store_is_the_owners_alone(self):
        with _home.secrets(self.home, "tlg", create=True) as vault:
            vault["session"] = "x"
        db = self.home / _store.SECRETS
        self.assertTrue(db.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(db.parent.stat().st_mode), 0o700)


class SealedTest(AuthCase):
    def test_s16_secret_db_holds_no_plaintext_after_a_login(self):
        import flnd

        export = self.home.parent / "cookies.txt"
        export.write_text(NETSCAPE)
        with flnd.open(self.home) as fl:
            fl.login("ins", str(export))
        stored = b"".join(path.read_bytes() for path in self.home.rglob("secret.db*"))
        self.assertTrue(stored)
        self.assertNotIn(b"ins-session", stored)


class OriginAndExpiryTest(AuthCase):
    def test_origin_and_expiring_cookies_never_expose_values(self):
        import time

        now = time.time()
        jar = _cookies.Jar([
            {"name": "sessionid", "value": "top-secret-1", "domain": ".instagram.com", "expires": now + 3600},
            {"name": "other", "value": "top-secret-2", "domain": ".instagram.com", "expires": now - 10},
        ])
        _home.store(Instagram(), jar, home=self.home, origin="Chrome")
        # Schema 2 names an origin by its browser or method (`accounts.origin`).
        self.assertEqual(_home.origin(self.home, "ins"), "chrome")
        expiring = _home.expiring(self.home, Instagram())
        self.assertEqual(len(expiring), 1)
        self.assertEqual(expiring[0][0], "sessionid")
        self.assertGreater(expiring[0][1], 0)
        self.assertNotIn("top-secret", repr(expiring))
        _home.logout(self.home, "ins")
        self.assertEqual(_home.origin(self.home, "ins"), "")


if __name__ == "__main__":
    unittest.main()
