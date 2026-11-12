"""flnd/common/auth.py: cookie jars in the vault, the one-time move of plain cookie files, whether a
source is logged in, logout, secrets sealed per source and name, and the key file.

No browser, no network: cookie files are written by the tests, and the vault's key is a real one
in a temporary `.flnd`.
"""

import base64
import io
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from flnd.common import auth
from flnd.sources.ins import Instagram
from shared import cookies, crypto, kv, theme
from shared._theme import Screen

NETSCAPE = ("# Netscape HTTP Cookie File\n"
            ".instagram.com\tTRUE\t/\tTRUE\t1825861327\tsessionid\tins-session\n")


class AuthCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"


class CookieFileTest(AuthCase):
    def test_legacy_dirs_are_gone(self):
        """Fix #2: `E:/…` was read relative to the working directory on macOS and Linux."""
        self.assertFalse(hasattr(auth, "LEGACY_DIRS"))
        legacy = self.home.parent / "E:" / "flntools" / ".flnd" / "cookies"
        legacy.mkdir(parents=True)
        (legacy / "ins.txt").write_text("# Netscape HTTP Cookie File\n"
                                        ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\tx\n")
        previous = Path.cwd()
        os.chdir(self.home.parent)
        try:
            self.assertEqual(auth.load(self.home, "ins"), cookies.Jar())
        finally:
            os.chdir(previous)

    def test_logged_in_and_logout_see_cookie_jars_and_sessions(self):
        self.assertFalse(auth.logged_in(self.home, "ins"))
        (self.home / "cookies").mkdir(parents=True)
        (self.home / "cookies" / "ins.json").write_text("[]")
        auth.save(self.home, "twt", cookies.Jar())
        with auth.secrets(self.home, "tlg") as secrets:
            secrets["session"] = "s"
        # A cookie login counts as logged in (moved from a plain file or not), and so does tlg's session.
        self.assertEqual([code for code in ("ins", "twt", "tlg", "tik") if auth.logged_in(self.home, code)],
                         ["ins", "twt", "tlg"])
        self.assertEqual([auth.logout(self.home, code) for code in ("ins", "twt", "tlg", "tik")],
                         [True, True, True, False])
        self.assertFalse((self.home / "cookies").exists())
        self.assertFalse(any(auth.logged_in(self.home, code) for code in ("ins", "twt", "tlg")))


class VaultedCookiesTest(AuthCase):
    """B7: every credential is sealed in the vault, and the plain cookie files of before move into it once."""

    def setUp(self):
        super().setUp()
        # A fake screen, so a move that fails warns where the test can read it.
        self.screen = Screen(dest=io.StringIO(), inp=io.StringIO(""), width=400, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def warnings(self):
        return [line for line in self.screen.dest.getvalue().splitlines() if line.startswith("! ")]

    def write(self, name, text=NETSCAPE):
        path = self.home / "cookies" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o600)
        return path

    def test_a_login_stores_its_jar_only_in_the_vault(self):
        export = self.home.parent / "cookies.txt"
        export.write_text(NETSCAPE)
        message = auth.store(Instagram(), auth.from_file(Instagram(), export), home=self.home, origin=export.name)
        self.assertEqual(message, "ins: logged in from cookies.txt (1 cookie)")
        # Read once and never copied: the export is where it was, and `.flnd/` holds only the vault.
        self.assertTrue(export.is_file())
        self.assertEqual(sorted(path.name for path in self.home.rglob("*")), ["secret.key", "secrets.db"])
        self.assertEqual([(cookie["name"], cookie["domain"]) for cookie in auth.load(self.home, "ins")],
                         [("sessionid", ".instagram.com")])
        with kv.open(self.home / "secrets.db", table="secrets") as db:
            self.assertEqual(list(db.keys(prefix="ins:")), ["ins:cookies"])
            self.assertNotIn(b"ins-session", base64.b64decode(db["ins:cookies"]))

    def test_a_plain_cookie_file_moves_into_the_vault_on_the_first_run(self):
        self.write("ins.txt")
        self.assertEqual([cookie["value"] for cookie in auth.load(self.home, "ins")], ["ins-session"])
        self.assertFalse((self.home / "cookies").exists())
        self.assertTrue(auth.logged_in(self.home, "ins"))
        self.assertEqual([cookie["value"] for cookie in auth.load(self.home, "ins")], ["ins-session"])

    def test_the_move_keeps_the_file_when_the_vault_write_fails(self):
        path = self.write("ins.txt")
        with mock.patch.object(auth.Vault, "__setitem__", side_effect=OSError("disk full")):
            self.assertFalse(auth.logged_in(self.home, "ins"))
        self.assertEqual(path.read_text(), NETSCAPE)
        self.assertEqual(self.warnings(), ["! ins: can't move ins.txt into the vault: disk full; kept it"])
        # The next run tries again, and moves it.
        self.assertTrue(auth.logged_in(self.home, "ins"))
        self.assertFalse(path.exists())

    def test_the_move_keeps_the_file_when_the_key_is_damaged(self):
        path = self.write("ins.txt")
        (self.home / "secret.key").write_bytes(b"short")
        with mock.patch.object(auth, "KEY_WAIT", 0.01):
            auth.logged_in(self.home, "ins")
        self.assertTrue(path.is_file())
        [warning] = self.warnings()
        self.assertIn("secret.key is damaged", warning)
        self.assertNotIn("ins-session", warning)

    def test_a_txt_wins_over_a_json_and_other_sources_files_stay(self):
        self.write("ins.txt")
        self.write("ins.json", '[{"domain": ".instagram.com", "name": "sessionid", "value": "older"}]')
        other = self.write("twt.txt", "# Netscape HTTP Cookie File\n")
        self.assertEqual([cookie["value"] for cookie in auth.load(self.home, "ins")], ["ins-session"])
        self.assertEqual(sorted(path.name for path in (self.home / "cookies").iterdir()), ["twt.txt"])
        self.assertTrue(other.is_file())
        auth.load(self.home, "twt")
        self.assertFalse((self.home / "cookies").exists())

    def test_a_vaulted_jar_is_newer_than_a_plain_file(self):
        auth.save(self.home, "ins", cookies.Jar([{"name": "sessionid", "value": "new", "domain": ".instagram.com"}]))
        self.write("ins.txt")
        self.assertEqual([cookie["value"] for cookie in auth.load(self.home, "ins")], ["new"])
        self.assertFalse((self.home / "cookies").exists())

    def test_logout_clears_the_jar_with_the_other_secrets(self):
        auth.save(self.home, "ins", cookies.Jar([{"name": "sessionid", "value": "s", "domain": ".instagram.com"}]))
        with auth.secrets(self.home, "ins") as secrets:
            secrets["token"] = "t"
        self.assertTrue(auth.logout(self.home, "ins"))
        with auth.secrets(self.home, "ins") as secrets:
            self.assertEqual(dict(secrets), {})
        self.assertEqual(auth.load(self.home, "ins"), cookies.Jar())
        self.assertFalse(auth.logout(self.home, "ins"))

    def test_logout_removes_a_plain_file_the_vault_couldnt_take(self):
        path = self.write("ins.txt")
        with mock.patch.object(auth.Vault, "__setitem__", side_effect=OSError("disk full")):
            self.assertTrue(auth.logout(self.home, "ins"))
        self.assertFalse(path.exists())

    def test_a_jar_copied_into_another_sources_slot_doesnt_decrypt(self):
        auth.save(self.home, "ins", cookies.Jar([{"name": "sessionid", "value": "s", "domain": ".instagram.com"}]))
        with kv.open(self.home / "secrets.db", table="secrets") as db:
            db["twt:cookies"] = db["ins:cookies"]
        with self.assertRaises(crypto.Error):
            auth.load(self.home, "twt")

    def test_crypto_keeps_no_key_or_derive(self):
        self.assertFalse(hasattr(crypto, "key"))
        self.assertFalse(hasattr(crypto, "derive"))

    def test_an_api_key_alone_is_not_a_login_and_a_session_is(self):
        with auth.secrets(self.home, "tlg") as secrets:
            secrets.update(api_id="1", api_hash="h")
        self.assertFalse(auth.logged_in(self.home, "tlg"))
        self.assertTrue(auth.api_key_saved(self.home, "tlg"))
        with auth.secrets(self.home, "tlg") as secrets:
            secrets["session"] = "s"
        self.assertTrue(auth.logged_in(self.home, "tlg"))
        self.assertFalse(auth.api_key_saved(self.home, "tlg"))

    def test_logout_removes_an_api_key_too(self):
        with auth.secrets(self.home, "tlg") as secrets:
            secrets.update(api_id="1", api_hash="h")
        self.assertTrue(auth.logout(self.home, "tlg"))
        self.assertFalse(auth.api_key_saved(self.home, "tlg"))
        self.assertFalse(auth.logout(self.home, "tlg"))

    def test_logged_in_creates_no_database(self):
        self.assertFalse(auth.logged_in(self.home, "tlg"))
        self.assertFalse(auth.api_key_saved(self.home, "tlg"))
        self.assertFalse(auth.logout(self.home, "tlg"))
        self.assertFalse(self.home.exists())


class WriteBackTest(AuthCase):
    """B5: the cookies the site rotated during a run update the saved jar, for names it already holds."""

    def setUp(self):
        super().setUp()
        self.jar = [{"name": "ct0", "value": "old-ct0", "domain": ".x.example", "path": "/", "expires": 1900000000},
                    {"name": "auth_token", "value": "login-token", "domain": ".x.example", "path": "/"}]
        auth.save(self.home, "twt", cookies.Jar(self.jar))

    def current(self, **values):
        client = httpx.Cookies()
        for name, value in values.items():
            client.set(name, value, domain=".x.example", path="/")
        return client.jar

    def test_a_rotated_ct0_is_saved_and_the_rest_kept(self):
        self.assertEqual(auth.write_back(self.home, "twt", self.current(ct0="new-ct0", auth_token="login-token")),
                         ["ct0"])
        saved = auth.load(self.home, "twt")
        self.assertEqual((saved["ct0"], saved["auth_token"], len(saved)), ("new-ct0", "login-token", 2))
        self.assertEqual([cookie["expires"] for cookie in saved if cookie["name"] == "ct0"], [1900000000])

    def test_a_rotated_cookie_for_a_subdomain_still_updates_the_one_it_replaces(self):
        client = httpx.Cookies()
        client.set("ct0", "new-ct0", domain="api.x.example", path="/")
        self.assertEqual(auth.write_back(self.home, "twt", client.jar), ["ct0"])
        self.assertEqual(auth.load(self.home, "twt")["ct0"], "new-ct0")

    def test_a_new_tracking_cookie_is_never_added(self):
        self.assertEqual(auth.write_back(self.home, "twt", self.current(ct0="old-ct0", _ga="track", guest_id="g")), [])
        self.assertEqual(auth.load(self.home, "twt").names, ["ct0", "auth_token"])
        self.assertEqual(auth.write_back(self.home, "twt", self.current(ct0="new-ct0", _ga="track")), ["ct0"])
        self.assertEqual(auth.load(self.home, "twt").names, ["ct0", "auth_token"])

    def test_nothing_is_written_when_nothing_changed_or_there_is_no_login(self):
        with mock.patch.object(auth, "save") as save:
            self.assertEqual(auth.write_back(self.home, "twt", self.current(ct0="old-ct0", auth_token="login-token")), [])
            # An empty value is a cookie the site is clearing, never a refresh.
            self.assertEqual(auth.write_back(self.home, "twt", self.current(ct0="")), [])
            self.assertEqual(auth.write_back(self.home, "twt", []), [])
            self.assertEqual(auth.write_back(self.home, "tlg", self.current(ct0="x")), [])
            self.assertEqual(auth.write_back(self.home / "never", "twt", self.current(ct0="x")), [])
        save.assert_not_called()
        self.assertFalse((self.home / "never").exists())

    def test_no_cookie_value_reaches_a_log_line(self):
        with mock.patch.object(auth, "log") as log:
            auth.write_back(self.home, "twt", self.current(ct0="new-ct0"))
            with mock.patch.object(auth, "save", side_effect=OSError("disk full: new-ct0")):
                self.assertEqual(auth.write_back(self.home, "twt", self.current(ct0="newer-ct0")), [])
        text = repr(log.mock_calls)
        for value in ("old-ct0", "new-ct0", "newer-ct0", "login-token"):
            self.assertNotIn(value, text)
        self.assertIn("ct0", repr(log.debug.mock_calls))
        log.warn.assert_called_once()

    def test_a_vault_that_cannot_be_written_warns_and_does_not_fail(self):
        with mock.patch.object(auth, "save", side_effect=OSError("read-only")), mock.patch.object(auth, "log") as log:
            self.assertEqual(auth.write_back(self.home, "twt", self.current(ct0="new-ct0")), [])
        log.warn.assert_called_once()
        self.assertEqual(auth.load(self.home, "twt")["ct0"], "old-ct0")


class VaultTest(AuthCase):
    def test_sealed_round_trip(self):
        with auth.secrets(self.home, "tlg") as secrets:
            secrets["session"] = "1BVts-secret"
            secrets["api_hash"] = "abc"
        with auth.secrets(self.home, "tlg") as secrets:
            self.assertEqual(dict(secrets), {"session": "1BVts-secret", "api_hash": "abc"})
        with kv.open(self.home / "secrets.db", table="secrets") as db:
            stored = db["tlg:session"]
        self.assertNotIn(b"1BVts", base64.b64decode(stored))

    def test_sources_are_separate(self):
        with auth.secrets(self.home, "tlg") as tlg, auth.secrets(self.home, "loi") as loi:
            tlg["session"] = "t"
            self.assertNotIn("session", loi)
            self.assertEqual(len(loi), 0)
            loi["session"] = "l"
            del tlg["session"]
            self.assertEqual(dict(loi), {"session": "l"})

    def test_a_value_moved_to_another_slot_fails(self):
        with auth.secrets(self.home, "tlg") as secrets:
            secrets["session"] = "s"
        with kv.open(self.home / "secrets.db", table="secrets") as db:
            db["loi:session"] = db["tlg:session"]
            db["tlg:other"] = db["tlg:session"]
        for code, name in (("loi", "session"), ("tlg", "other")):
            with auth.secrets(self.home, code) as secrets, self.assertRaises(crypto.Error):
                secrets[name]

    def test_no_key_file_until_a_secret_is_used(self):
        with auth.secrets(self.home, "tlg") as secrets:
            self.assertEqual(len(secrets), 0)
        self.assertFalse((self.home / "secret.key").exists())


class KeyTest(AuthCase):
    def test_created_with_mode_0600(self):
        secret = auth.key(self.home)
        path = self.home / "secret.key"
        self.assertEqual(len(secret), 32)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(auth.key(self.home), secret)
        self.assertEqual([entry.name for entry in self.home.iterdir()], ["secret.key"])

    def test_key_is_created_where_hard_links_fail(self):
        # Fix #2: FAT, exFAT and some network drives refuse os.link with a plain OSError.
        with mock.patch.object(auth.os, "link", side_effect=OSError("operation not permitted")):
            secret = auth.key(self.home)
        self.assertEqual(len(secret), 32)
        self.assertEqual(auth.key(self.home), secret)
        self.assertEqual([entry.name for entry in self.home.iterdir()], ["secret.key"])

    def test_two_creators_end_with_one_key(self):
        """Fix #2: the loser of the create race reads the winner's key, even while it is still short."""
        winner = os.urandom(32)
        real_open = os.open
        finish = threading.Timer(0.05, lambda: (self.home / "secret.key").write_bytes(winner))

        def other_run_wins(path, flags, *args, **kwargs):
            if Path(path).name == "secret.key" and flags & os.O_EXCL:
                # The other run has created the file and written only part of its key so far.
                (self.home / "secret.key").write_bytes(winner[:5])
                finish.start()
                raise FileExistsError(path)
            return real_open(path, flags, *args, **kwargs)

        self.home.mkdir(parents=True)
        with mock.patch.object(auth.os, "open", other_run_wins):
            self.assertEqual(auth.key(self.home), winner)
        finish.join()
        self.assertEqual([entry.name for entry in self.home.iterdir()], ["secret.key"])

    def test_a_failed_write_leaves_no_key(self):
        with mock.patch.object(auth.os, "fdopen", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                auth.key(self.home)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_many_threads_agree_on_one_key(self):
        keys = []
        barrier = threading.Barrier(8)

        def read():
            barrier.wait()
            keys.append(auth.key(self.home))

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(keys)), 1)

    def test_a_damaged_key_is_refused(self):
        self.home.mkdir(parents=True)
        for content in (b"", b"x" * 5, b"x" * 33):
            with self.subTest(size=len(content)):
                (self.home / "secret.key").write_bytes(content)
                with mock.patch.object(auth, "KEY_WAIT", 0.05), \
                        self.assertRaisesRegex(ValueError, f"damaged: it should hold 32 bytes, not {len(content)}"):
                    auth.key(self.home)

    def test_a_dangling_symlink_key_is_refused_in_time(self):
        """A dangling link at secret.key can be neither read nor created, so it once spun forever at 100% CPU."""
        self.home.mkdir(parents=True)
        try:
            (self.home / "secret.key").symlink_to(self.home / "missing.key")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks aren't supported here")
        with mock.patch.object(auth, "KEY_WAIT", 0.05), \
                self.assertRaisesRegex(ValueError, "damaged: it exists but can't be read.*delete it"):
            auth.key(self.home)


if __name__ == "__main__":
    unittest.main()
