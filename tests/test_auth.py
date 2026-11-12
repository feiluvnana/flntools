"""flnd/common/auth.py: cookie files, whether a source is logged in, logout, secrets sealed per source and name, and the key file.

No browser, no network.
"""

import base64
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from flnd.common import auth
from shared import cookies, crypto, kv


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

    def test_logged_in_and_logout_see_cookie_files_and_secrets(self):
        self.assertFalse(auth.logged_in(self.home, "ins"))
        auth.cookie_file(self.home, "ins").parent.mkdir(parents=True)
        (self.home / "cookies" / "ins.json").write_text("[]")
        auth.cookie_file(self.home, "twt").write_text("# Netscape HTTP Cookie File\n")
        with auth.secrets(self.home, "tlg") as secrets:
            secrets["session"] = "s"
        self.assertEqual([code for code in ("ins", "twt", "tlg", "tik") if auth.logged_in(self.home, code)],
                         ["ins", "twt", "tlg"])
        self.assertEqual([auth.logout(self.home, code) for code in ("ins", "twt", "tlg", "tik")],
                         [True, True, True, False])
        self.assertEqual(list((self.home / "cookies").iterdir()), [])
        self.assertFalse(any(auth.logged_in(self.home, code) for code in ("ins", "twt", "tlg")))

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
