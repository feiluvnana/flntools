"""flnkit.vault: `digest`, `encrypt` and `decrypt`, `seal` and `unseal`, and the key file.

Key files live in temporary folders only; the real `~/.config/flnd` is never touched, and the
key's wait runs on a fake clock. The ciphers' tests skip without `cryptography`.
"""

import builtins
import errno
import hashlib
import io
import os
import secrets
import stat
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from flnd.errors import DiskError, LoginError, ToolError, UsageError
from flnkit import vault as _vault

try:
    import cryptography  # noqa: F401
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

ALGORITHMS = ("md5", "sha1", "sha256", "sha512", "blake2b")

# A blob made by `encrypt_aes_gcm` at 62fb584, before step 4 renamed it, with this key and the
# context flnd's vault gives tlg's "session" slot. `decrypt` must open it: `metadata/secrets.db`
# vaults hold such blobs, and a changed format would lock every saved login out.
OLD_KEY = bytes(range(32))
OLD_CONTEXT = b"tlg\x00session"
OLD_BLOB = bytes.fromhex("01e34c119cd7de87f358d80004c1dce93e411b64681cf37637368156b9ac338b"
                         "a9fb6a214d52dd12cf3d8690765e7563727e")
# b"", with no context.
OLD_EMPTY_BLOB = bytes.fromhex("0168fb78f7564d2a0a984708e0e27bc396591855308acb6b1507b7114a")


class HashTest(unittest.TestCase):
    def test_every_input_agrees_with_hashlib(self):
        content = b"x" * (2 * 1024 * 1024 + 17)
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "big.bin"
            file.write_bytes(content)
            for algorithm in ALGORITHMS:
                with self.subTest(algorithm):
                    self.assertEqual(
                        _vault.digest("héllo", algorithm=algorithm),
                        hashlib.new(algorithm, "héllo".encode()).hexdigest(),
                    )
                    self.assertEqual(
                        _vault.digest(b"\x00\xff", algorithm=algorithm),
                        hashlib.new(algorithm, b"\x00\xff").hexdigest(),
                    )
                    expected = hashlib.new(algorithm, content).hexdigest()
                    self.assertEqual(_vault.digest(file, algorithm=algorithm), expected)
                    with file.open("rb") as handle:
                        self.assertEqual(_vault.digest(handle, algorithm=algorithm), expected)

    def test_default_algorithm_is_sha256(self):
        self.assertEqual(_vault.digest(b"hello"), hashlib.sha256(b"hello").hexdigest())

    def test_digest_accepts_bytearray(self):
        raw = b"hello world"
        expected = hashlib.sha256(raw).hexdigest()
        self.assertEqual(_vault.digest(bytearray(raw)), expected)
        self.assertEqual(_vault.digest(memoryview(raw)), expected)

    def test_file_object_is_read_from_its_position(self):
        handle = io.BytesIO(b"skipped|hashed")
        handle.seek(8)
        self.assertEqual(_vault.digest(handle), hashlib.sha256(b"hashed").hexdigest())

    def test_str_is_text_not_a_file_name(self):
        self.assertEqual(_vault.digest("photo.jpg"), hashlib.sha256(b"photo.jpg").hexdigest())

    def test_unknown_algorithm_is_a_value_error(self):
        with self.assertRaises(ValueError):
            _vault.digest(b"abc", algorithm="no-such-hash")

    def test_other_types_refused(self):
        with self.assertRaises(TypeError):
            _vault.digest(123)


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "cryptography is not installed")
class CipherTest(unittest.TestCase):
    def setUp(self):
        self.key = secrets.token_bytes(32)

    def test_round_trip(self):
        self.assertEqual(_vault.decrypt(_vault.encrypt("sessionid=abc", self.key), self.key),
                         b"sessionid=abc")
        blob = _vault.encrypt(b"\x00\xff", self.key, context=b"slot")
        self.assertEqual(_vault.decrypt(blob, self.key, context=b"slot"), b"\x00\xff")

    def test_a_blob_from_before_the_rename_still_decrypts(self):
        self.assertEqual(_vault.decrypt(OLD_BLOB, OLD_KEY, context=OLD_CONTEXT),
                         b"session=example-value")
        self.assertEqual(_vault.decrypt(OLD_EMPTY_BLOB, OLD_KEY), b"")

    def test_layout_and_fresh_nonce(self):
        first, second = _vault.encrypt("same", self.key), _vault.encrypt("same", self.key)
        self.assertNotEqual(first, second)
        self.assertEqual(first[0], 1)
        self.assertEqual(len(first), 1 + 12 + 4 + 16)

    def test_failures_raise_crypto_error(self):
        blob = _vault.encrypt("secret", self.key, context=b"slot")
        flipped = blob[:-1] + bytes([blob[-1] ^ 1])
        damaged = {
            "flipped byte": (flipped, self.key, b"slot"),
            "wrong key": (blob, secrets.token_bytes(32), b"slot"),
            "wrong context": (blob, self.key, b"other slot"),
            "truncated": (blob[:20], self.key, b"slot"),
            "unknown version": (b"\x02" + blob[1:], self.key, b"slot"),
        }
        for name, (damaged_blob, key, context) in damaged.items():
            with self.subTest(name), self.assertRaises(LoginError) as caught:
                _vault.decrypt(damaged_blob, key, context=context)
            # The library's own advice; flnd's vault replaces it with its own (`LOGIN_HINT`).
            self.assertIn("seal it again", caught.exception.hint)

    def test_only_a_32_byte_key(self):
        for size in (16, 20, 24, 33):
            for call in (lambda size=size: _vault.encrypt("x", b"k" * size),
                         lambda size=size: _vault.decrypt(b"x" * 40, b"k" * size)):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    call()

    def test_no_secret_reaches_a_message(self):
        # C6: an exception's text may be logged or shown; it must never hold a cookie value, key
        # or password.
        cookie, password = "sessionid=8f1c2e5a9b7d4013", "hunter2-example-password"
        key = bytes.fromhex("5a" * 32)
        wrong_key = bytes.fromhex("c3" * 32)
        blob = _vault.encrypt(cookie, key, context=password.encode())
        failures = [
            lambda: _vault.decrypt(blob, wrong_key, context=password.encode()),
            lambda: _vault.decrypt(blob, key, context=b"other"),
            lambda: _vault.decrypt(blob[:-1], key, context=password.encode()),
            lambda: _vault.decrypt(cookie.encode(), key),
            lambda: _vault.encrypt(cookie, key[:20], context=password.encode()),
            lambda: _vault.decrypt(blob, wrong_key[:20], context=password.encode()),
            lambda: _vault.encrypt(123, key),
        ]
        secret_texts = [cookie, password, "8f1c2e5a9b7d4013"]
        for key_bytes in (key, wrong_key, key[:20], wrong_key[:20]):
            secret_texts += [key_bytes.hex(), repr(key_bytes), key_bytes.decode("latin-1")]
        for number, call in enumerate(failures):
            with self.subTest(number), \
                    self.assertRaises((LoginError, ValueError, TypeError)) as caught:
                call()
            error = caught.exception
            texts = [str(error), repr(error), *map(str, error.args)]
            for text in texts:
                for secret in secret_texts:
                    self.assertNotIn(secret, text)
            # A chained cause could print the secret in a traceback even when the message doesn't.
            self.assertIsNone(error.__cause__)


class MissingExtraTest(unittest.TestCase):
    def test_ciphers_name_the_extra_and_digest_still_works(self):
        real_import = builtins.__import__

        def import_without_cryptography(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("No module named 'cryptography'")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", import_without_cryptography):
            for cipher_call in (lambda: _vault.decrypt(b"x" * 40, secrets.token_bytes(32)),
                                lambda: _vault.encrypt("x", secrets.token_bytes(32))):
                with self.assertRaises(ToolError) as caught:
                    cipher_call()
                self.assertEqual(caught.exception.hint, "pip install cryptography")
            self.assertEqual(_vault.digest("a"), hashlib.sha256(b"a").hexdigest())


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "cryptography is not installed")
class SealTest(unittest.TestCase):
    VALUES = ("", "1BVts-example-session", "héllo ✓", "sessionid=" + "ab" * 500)

    def setUp(self):
        self.key = secrets.token_bytes(32)

    def test_round_trip(self):
        for value in self.VALUES:
            with self.subTest(size=len(value)):
                sealed = _vault.seal(self.key, 7, "session", value)
                self.assertEqual(_vault.unseal(self.key, 7, "session", sealed), value)

    def test_layout_version_byte_and_fresh_nonce(self):
        first, second = (_vault.seal(self.key, 7, "session", "same") for _ in range(2))
        self.assertNotEqual(first, second)
        self.assertEqual(first[0], 1)
        self.assertEqual(len(first), 1 + 12 + len("same") + 16)

    def test_no_plaintext_in_sealed_bytes(self):
        for value in self.VALUES[1:]:
            with self.subTest(size=len(value)):
                sealed = _vault.seal(self.key, 7, "session", value)
                raw = value.encode()
                windows = {raw[i:i + 6] for i in range(len(raw) - 5)}
                self.assertFalse([part for part in windows if part in sealed])
                self.assertNotIn(b"session", sealed)

    def test_every_failure_says_the_key_is_gone(self):
        sealed = _vault.seal(self.key, 7, "session", "1BVts-example-session")
        # Without the separator both slots would be "12x".
        colliding = _vault.seal(self.key, 1, "2x", "1BVts-example-session")
        # A value that opens but isn't UTF-8: a UnicodeDecodeError would quote its bytes.
        binary = _vault.encrypt(b"\xff\xfeexample-session", self.key, context=b"7\0session")
        cases = {
            "wrong key": (secrets.token_bytes(32), 7, "session", sealed),
            "moved to another account": (self.key, 8, "session", sealed),
            "moved to another name": (self.key, 7, "cookies", sealed),
            "moved across the separator": (self.key, 12, "x", colliding),
            "flipped byte": (self.key, 7, "session", sealed[:-1] + bytes([sealed[-1] ^ 1])),
            "truncated": (self.key, 7, "session", sealed[:20]),
            "version 2": (self.key, 7, "session", b"\x02" + sealed[1:]),
            "version 0": (self.key, 7, "session", b"\x00" + sealed[1:]),
            "not UTF-8": (self.key, 7, "session", binary),
            "a text row": (self.key, 7, "session", "example-session"),
            "a null row": (self.key, 7, "session", None),
        }
        for case, args in cases.items():
            with self.subTest(case), self.assertRaises(LoginError) as caught:
                _vault.unseal(*args)
            error = caught.exception
            self.assertEqual((str(error), error.hint), ("the vault's key is gone", ""))
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            self.assertNotIn("example-session", repr(error))

    def test_only_an_int_account_and_text(self):
        cases = {
            "bytes value": lambda: _vault.seal(self.key, 7, "session", b"example-session"),
            "text account": lambda: _vault.seal(self.key, "example-session", "x", "v"),
            "float account": lambda: _vault.seal(self.key, 7.0, "session", "example-session"),
            "bool account": lambda: _vault.seal(self.key, True, "session", "example-session"),
            "unseal, bool account": lambda: _vault.unseal(self.key, True, "session", b""),
            "bytes name": lambda: _vault.seal(self.key, 7, b"session", "example-session"),
            "unseal, text account": lambda: _vault.unseal(self.key, "example-session", "x", b""),
        }
        for case, call in cases.items():
            with self.subTest(case), self.assertRaises(TypeError) as caught:
                call()
            self.assertNotIn("example-session", str(caught.exception))


class Clock:
    """A fake `time` for the key's wait: `sleep` moves `monotonic` on, and runs `then` once."""

    def __init__(self, then=None):
        self.now, self.then = 0.0, then

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        then, self.then = self.then, None
        if then is not None:
            then()


class KeyFileTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.path = self.root / "config" / "flnd" / "key"
        self.enterContext(mock.patch.object(_vault, "time", Clock()))

    def key(self, path=None, **options):
        return _vault.key(path or self.path, home=self.home, **options)

    def files(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                      if not p.is_dir())

    def test_made_once_with_mode_0600_then_read(self):
        secret = self.key(create=True)
        self.assertEqual(len(secret), 32)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(self.key(create=True), secret)
        self.assertEqual(self.key(), secret)
        self.assertEqual(self.files(), ["config/flnd/key"])

    def test_a_missing_key_is_gone_and_nothing_is_made(self):
        with self.assertRaises(LoginError) as caught:
            self.key()
        self.assertEqual((str(caught.exception), caught.exception.hint),
                         ("the vault's key is gone", ""))
        self.assertFalse((self.root / "config").exists())

    def test_the_default_place(self):
        cases = {"darwin": ".config/flnd/key", "linux": ".config/flnd/key",
                 "win32": "AppData/Roaming/flnd/key"}
        for platform, expected in cases.items():
            with self.subTest(platform), mock.patch.object(_vault, "_PLATFORM", platform), \
                    mock.patch.dict(os.environ, {"HOME": str(self.root)}), \
                    mock.patch.object(Path, "home", return_value=self.root):
                os.environ.pop("APPDATA", None)
                self.assertEqual(_vault.keyfile(), self.root / expected)

    def test_a_key_inside_the_home_is_refused(self):
        inner = self.root / "inner"
        (self.home / "metadata").mkdir()
        try:
            inner.symlink_to(self.home / "metadata", target_is_directory=True)
        except (OSError, NotImplementedError):
            inner = None
        later, cafe = self.root / "later", self.root / "caf\u00e9"
        # Each row: the home, then the key path. Case and Unicode form count even where the
        # file system keeps them apart, and before the home exists.
        cases = {"in the home": (self.home, self.home / "key"),
                 "deep in it": (self.home, self.home / "metadata" / "key"),
                 "through a link": (self.home, inner and inner / "key"),
                 "in other case": (self.home, self.root / "HOME" / "key"),
                 "in other case, no home yet": (later, self.root / "LATER" / "key"),
                 "in another Unicode form": (cafe, self.root / "cafe\u0301" / "key")}
        # macOS shows the data volume at a second path, which `resolve` leaves alone.
        mount = Path("/System/Volumes/Data") / self.home.relative_to(self.home.anchor)
        if mount.exists() and os.path.samefile(mount, self.home):
            cases["through another mount"] = (self.home, mount / "key")
        for case, (home, path) in cases.items():
            with self.subTest(case):
                if path is None:
                    self.skipTest("symlinks aren't supported here")
                with self.assertRaisesRegex(UsageError, "is inside"):
                    _vault.key(path, home=home, create=True)
        self.assertEqual(self.files(), [])

    def test_a_second_spelling_of_a_missing_folder_is_refused(self):
        """macOS's data volume spells every folder a second way, which `resolve` leaves alone."""
        from flnkit import cookies

        data = Path("/System/Volumes/Data")
        if not (data / self.root.relative_to(self.root.anchor)).exists():
            self.skipTest("no second path to the data volume here")

        def spelled(path):
            return data / path.relative_to(path.anchor)

        # The stand-in account has no .config, then a .config with no flnd in it.
        for made in ("", ".config"):
            account = self.root / f"account-{made or 'bare'}"
            (account / made).mkdir(parents=True)
            folder = account / ".config" / "flnd"
            rows = {"the data volume's spelling": (spelled(folder / "key"), True),
                    "a sibling": (spelled(account / "other" / "key"), False),
                    "outside": (self.root / f"elsewhere-{made or 'bare'}" / "key", False)}
            # Each check: what the guard takes for the real home, the home, the error.
            checks = {"guard": (account, self.home, RuntimeError),
                      "home": (self.root / "nobody", folder, UsageError)}
            for case, (path, refused) in rows.items():
                for check, (real, home, error) in checks.items():
                    with self.subTest(case, made=made, check=check), \
                            mock.patch.object(cookies, "home", return_value=real):
                        if refused:
                            with self.assertRaises(error):
                                _vault.key(path, home=home, create=True)
                        else:
                            self.assertEqual(len(_vault.key(path, home=home, create=True)), 32)
            self.assertFalse(folder.exists())

    def test_the_real_folder_is_refused_under_the_guard(self):
        """A real `~/.config/flnd` is refused before any read or write, which fail loudly here."""
        from flnkit import cookies

        real = cookies.home()
        fake = self.root / "account"
        roaming = self.root / "roaming"
        # Each row: the account's home, the platform, then the key path. The stand-in's
        # folders don't exist, so only the paths themselves can match.
        cases = {"the real home": (real, "darwin", real / ".config" / "flnd" / "key"),
                 "a stand-in for it": (fake, "linux", fake / ".config" / "flnd" / "key"),
                 "in other case": (fake, "linux", fake / ".CONFIG" / "Flnd" / "key"),
                 "windows": (fake, "win32", fake / "AppData" / "Roaming" / "flnd" / "key"),
                 "windows' APPDATA": (fake, "win32", roaming / "flnd" / "key")}
        touch = mock.Mock(side_effect=AssertionError("touched a key file"))
        self.assertTrue(os.environ.get(_vault.NO_PROFILES))
        for case, (account, platform, path) in cases.items():
            with self.subTest(case), ExitStack() as stack:
                stack.enter_context(mock.patch.object(cookies, "home", return_value=account))
                stack.enter_context(mock.patch.object(_vault, "_PLATFORM", platform))
                stack.enter_context(mock.patch.dict(os.environ, {"APPDATA": str(roaming)}))
                for method in ("read_bytes", "mkdir"):
                    stack.enter_context(mock.patch.object(Path, method, touch))
                with self.assertRaisesRegex(RuntimeError, "may not use the real key file"):
                    self.key(path, create=True)
        self.assertFalse(fake.exists())
        # The guard, not the file system, refused it: without it the stand-in is made.
        with mock.patch.object(cookies, "home", return_value=fake), \
                mock.patch.dict(os.environ, {_vault.NO_PROFILES: ""}):
            self.assertEqual(len(self.key(fake / ".config" / "flnd" / "key", create=True)), 32)

    def test_the_key_is_whole_before_it_is_in_place(self):
        real_link = os.link
        seen = []

        def link(draft, path):
            seen.append((Path(path).exists(), len(Path(draft).read_bytes()),
                         stat.S_IMODE(Path(draft).stat().st_mode)))
            real_link(draft, path)

        with mock.patch.object(_vault.os, "link", link):
            self.key(create=True)
        self.assertEqual(seen, [(False, 32, 0o600 if os.name != "nt" else seen[0][2])])
        self.assertEqual(self.files(), ["config/flnd/key"])

    def test_a_failed_write_leaves_no_key(self):
        full = OSError(errno.ENOSPC, "full")
        cases = {"fdopen": {"fdopen": full}, "fsync": {"fsync": full},
                 # No hard links, and then the write in place fails too.
                 "in place": {"link": OSError(errno.EPERM, "no"), "fsync": [None, full]}}
        for case, failures in cases.items():
            with self.subTest(case), ExitStack() as stack:
                for name, effect in failures.items():
                    stack.enter_context(mock.patch.object(_vault.os, name, side_effect=effect))
                with self.assertRaises(OSError):
                    self.key(create=True)
                self.assertEqual(self.files(), [])

    def test_two_creators_end_with_one_key(self):
        winner = os.urandom(32)
        real_open = os.open

        def links_first(draft, path):
            Path(path).write_bytes(winner)
            raise FileExistsError(path)

        def still_writing(path, flags, *args):
            # Without hard links, the other run has created the key and written part of it.
            if Path(path) == self.path and flags & os.O_EXCL:
                self.path.write_bytes(winner[:5])
                _vault.time.then = lambda: self.path.write_bytes(winner)
                raise FileExistsError(path)
            return real_open(path, flags, *args)

        cases = {"it links first": [mock.patch.object(_vault.os, "link", links_first)],
                 "no hard links, still writing": [
                     mock.patch.object(_vault.os, "link", side_effect=OSError(errno.EPERM, "no")),
                     mock.patch.object(_vault.os, "open", still_writing)]}
        for case, patches in cases.items():
            with self.subTest(case), ExitStack() as stack:
                self.path.unlink(missing_ok=True)
                for patch in patches:
                    stack.enter_context(patch)
                self.assertEqual(self.key(create=True), winner)
                self.assertEqual(self.files(), ["config/flnd/key"])

    def test_a_drive_without_hard_links_still_gets_a_key(self):
        with mock.patch.object(_vault.os, "link", side_effect=OSError(errno.EPERM, "no")):
            secret = self.key(create=True)
        self.assertEqual(self.key(), secret)
        self.assertEqual(self.files(), ["config/flnd/key"])

    def test_many_threads_agree_on_one_key(self):
        keys, barrier = [], threading.Barrier(8)

        def read():
            barrier.wait()
            keys.append(self.key(create=True))

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((len(keys), len(set(keys))), (8, 1))
        self.assertEqual(self.files(), ["config/flnd/key"])

    def test_a_damaged_key_is_refused_and_kept(self):
        self.path.parent.mkdir(parents=True)
        for content in (b"", b"x" * 5, b"x" * 33):
            for create in (False, True):
                with self.subTest(size=len(content), create=create):
                    self.path.write_bytes(content)
                    with self.assertRaisesRegex(DiskError, f"hold 32 bytes, not {len(content)}") \
                            as caught:
                        self.key(create=create)
                    self.assertIn("delete it", caught.exception.hint)
                    self.assertEqual(self.path.read_bytes(), content)

    def test_a_folder_at_the_key_path_is_damaged(self):
        self.path.mkdir(parents=True)
        for create in (False, True):
            with self.subTest(create=create), \
                    self.assertRaisesRegex(DiskError, "is a folder, not a key file"):
                self.key(create=create)

    def test_a_dangling_link_is_refused_in_time(self):
        self.path.parent.mkdir(parents=True)
        try:
            self.path.symlink_to(self.root / "missing")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks aren't supported here")
        with self.assertRaisesRegex(DiskError, "exists but cannot be read"):
            self.key(create=True)


if __name__ == "__main__":
    unittest.main()
