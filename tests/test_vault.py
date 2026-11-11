"""flnd/vault.py: secrets sealed per source and name, and the key file."""

import base64
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from flnd import vault
from shared import crypto, kv


class VaultCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"


class VaultTest(VaultCase):
    def test_sealed_round_trip(self):
        with vault.open(self.home, "tlg") as secrets:
            secrets["session"] = "1BVts-secret"
            secrets["api_hash"] = "abc"
        with vault.open(self.home, "tlg") as secrets:
            self.assertEqual(dict(secrets), {"session": "1BVts-secret", "api_hash": "abc"})
        with kv.open(self.home / "secrets.db", table="secrets") as db:
            stored = db["tlg:session"]
        self.assertNotIn(b"1BVts", base64.b64decode(stored))

    def test_sources_are_separate(self):
        with vault.open(self.home, "tlg") as tlg, vault.open(self.home, "loi") as loi:
            tlg["session"] = "t"
            self.assertNotIn("session", loi)
            self.assertEqual(len(loi), 0)
            loi["session"] = "l"
            del tlg["session"]
            self.assertEqual(dict(loi), {"session": "l"})

    def test_a_value_moved_to_another_slot_fails(self):
        with vault.open(self.home, "tlg") as secrets:
            secrets["session"] = "s"
        with kv.open(self.home / "secrets.db", table="secrets") as db:
            db["loi:session"] = db["tlg:session"]
            db["tlg:other"] = db["tlg:session"]
        for code, name in (("loi", "session"), ("tlg", "other")):
            with vault.open(self.home, code) as secrets, self.assertRaises(crypto.CryptoError):
                secrets[name]

    def test_no_key_file_until_a_secret_is_used(self):
        with vault.open(self.home, "tlg") as secrets:
            self.assertEqual(len(secrets), 0)
        self.assertFalse((self.home / "secret.key").exists())


class KeyTest(VaultCase):
    def test_created_with_mode_0600(self):
        secret = vault.key(self.home)
        path = self.home / "secret.key"
        self.assertEqual(len(secret), 32)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(vault.key(self.home), secret)
        self.assertEqual([entry.name for entry in self.home.iterdir()], ["secret.key"])

    def test_losing_the_race_reads_the_winner(self):
        winner = os.urandom(32)
        real_link = os.link

        def other_run_wins(source, target):
            Path(target).write_bytes(winner)
            raise FileExistsError(target)

        with mock.patch.object(vault.os, "link", other_run_wins):
            self.assertEqual(vault.key(self.home), winner)
        self.assertEqual(sorted(entry.name for entry in self.home.iterdir()), ["secret.key"])
        self.assertIs(os.link, real_link)

    def test_many_threads_agree_on_one_key(self):
        keys = []
        barrier = threading.Barrier(8)

        def read():
            barrier.wait()
            keys.append(vault.key(self.home))

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(keys)), 1)

    def test_a_damaged_key_is_refused(self):
        self.home.mkdir(parents=True)
        (self.home / "secret.key").write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "damaged: it should hold 32 bytes, not 0"):
            vault.key(self.home)


if __name__ == "__main__":
    unittest.main()
