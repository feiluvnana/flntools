"""shared.crypto: the five hashes against hashlib, and AES-256-GCM's `encrypt` and `decrypt`.

Nothing is faked but a missing `cryptography`, by patching `__import__`. The ciphers' tests skip
without `cryptography`; the hashes need only the stdlib.
"""

import builtins
import hashlib
import io
import re
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared import crypto

try:
    import cryptography  # noqa: F401
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

HASHES = {
    "md5": crypto.md5,
    "sha1": crypto.sha1,
    "sha256": crypto.sha256,
    "sha512": crypto.sha512,
    "blake2b": crypto.blake2b,
}

# A blob made by `encrypt_aes_gcm` at 62fb584, before step 4 renamed it, with this key and the
# context flnd's vault gives tlg's "session" slot. `decrypt` must open it: `metadata/secrets.db` vaults
# hold such blobs, and a changed format would lock every saved login out.
OLD_KEY = bytes(range(32))
OLD_CONTEXT = b"tlg\x00session"
OLD_BLOB = bytes.fromhex("01e34c119cd7de87f358d80004c1dce93e411b64681cf37637368156b9ac338ba9fb6a214d52dd12cf3d8690765e7563727e")
OLD_EMPTY_BLOB = bytes.fromhex("0168fb78f7564d2a0a984708e0e27bc396591855308acb6b1507b7114a")  # b"", no context


class HashTest(unittest.TestCase):
    def test_every_input_agrees_with_hashlib(self):
        # Over two chunks and a bit, so the chunked read of a Path or a file is what is tested.
        content = b"x" * (crypto.CHUNK_SIZE * 2 + 17)
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "big.bin"
            file.write_bytes(content)
            for algorithm, hash_function in HASHES.items():
                with self.subTest(algorithm):
                    self.assertEqual(hash_function.__name__, algorithm)
                    self.assertEqual(hash_function("héllo"), hashlib.new(algorithm, "héllo".encode()).hexdigest())
                    self.assertEqual(hash_function(b"\x00\xff"), hashlib.new(algorithm, b"\x00\xff").hexdigest())
                    expected = hashlib.new(algorithm, content).hexdigest()
                    self.assertEqual(hash_function(file), expected)
                    with file.open("rb") as handle:
                        self.assertEqual(hash_function(handle), expected)

    def test_file_object_is_read_from_its_position(self):
        handle = io.BytesIO(b"skipped|hashed")
        handle.seek(8)
        self.assertEqual(crypto.sha256(handle), hashlib.sha256(b"hashed").hexdigest())

    def test_str_is_text_not_a_file_name(self):
        self.assertEqual(crypto.sha256("photo.jpg"), hashlib.sha256(b"photo.jpg").hexdigest())

    def test_other_types_refused(self):
        with self.assertRaises(TypeError):
            crypto.sha256(123)  # pyright: ignore[reportArgumentType]


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "cryptography is not installed")
class CipherTest(unittest.TestCase):
    def setUp(self):
        self.key = secrets.token_bytes(32)

    def test_round_trip(self):
        self.assertEqual(crypto.decrypt(crypto.encrypt("sessionid=abc", self.key), self.key), b"sessionid=abc")
        blob = crypto.encrypt(b"\x00\xff", self.key, context=b"slot")
        self.assertEqual(crypto.decrypt(blob, self.key, context=b"slot"), b"\x00\xff")

    def test_a_blob_from_before_the_rename_still_decrypts(self):
        self.assertEqual(crypto.decrypt(OLD_BLOB, OLD_KEY, context=OLD_CONTEXT), b"session=example-value")
        self.assertEqual(crypto.decrypt(OLD_EMPTY_BLOB, OLD_KEY), b"")

    def test_layout_and_fresh_nonce(self):
        first, second = crypto.encrypt("same", self.key), crypto.encrypt("same", self.key)
        self.assertNotEqual(first, second)
        self.assertEqual(first[0], 1)
        self.assertEqual(len(first), 1 + 12 + 4 + 16)

    def test_failures_raise_crypto_error(self):
        blob = crypto.encrypt("secret", self.key, context=b"slot")
        flipped = blob[:-1] + bytes([blob[-1] ^ 1])
        damaged = {
            "flipped byte": (flipped, self.key, b"slot"),
            "wrong key": (blob, secrets.token_bytes(32), b"slot"),
            "wrong context": (blob, self.key, b"other slot"),
            "truncated": (blob[:20], self.key, b"slot"),
            "unknown version": (b"\x02" + blob[1:], self.key, b"slot"),
        }
        for name, (damaged_blob, key, context) in damaged.items():
            with self.subTest(name), self.assertRaises(crypto.Error):
                crypto.decrypt(damaged_blob, key, context=context)
        # flnd's vault and older code catch a wrong key or damaged data as a ValueError.
        self.assertTrue(issubclass(crypto.Error, ValueError))

    def test_only_a_32_byte_key(self):
        # AES-256 only: a 16- or 24-byte key is a mistake, refused as a ValueError, not a crypto.Error,
        # since it is the caller's bug rather than bad data.
        for size in (16, 20, 24, 33):
            for call in (lambda: crypto.encrypt("x", b"k" * size), lambda: crypto.decrypt(b"x" * 40, b"k" * size)):
                with self.subTest(size=size), self.assertRaises(ValueError) as caught:
                    call()
                self.assertNotIsInstance(caught.exception, crypto.Error)

    def test_no_secret_reaches_a_message(self):
        # C6: an exception's text may be logged or shown; it must never hold a cookie value, key or password.
        cookie, password = "sessionid=8f1c2e5a9b7d4013", "hunter2-example-password"
        key = bytes.fromhex("5a" * 32)
        wrong_key = bytes.fromhex("c3" * 32)
        blob = crypto.encrypt(cookie, key, context=password.encode())
        failures = [
            lambda: crypto.decrypt(blob, wrong_key, context=password.encode()),
            lambda: crypto.decrypt(blob, key, context=b"other"),
            lambda: crypto.decrypt(blob[:-1], key, context=password.encode()),
            lambda: crypto.decrypt(cookie.encode(), key),
            lambda: crypto.encrypt(cookie, key[:20], context=password.encode()),
            lambda: crypto.decrypt(blob, wrong_key[:20], context=password.encode()),
            lambda: crypto.encrypt(123, key),  # pyright: ignore[reportArgumentType]
        ]
        secret_texts = [cookie, password, "8f1c2e5a9b7d4013"]
        for key_bytes in (key, wrong_key, key[:20], wrong_key[:20]):
            secret_texts += [key_bytes.hex(), repr(key_bytes), key_bytes.decode("latin-1")]
        for number, call in enumerate(failures):
            with self.subTest(number), self.assertRaises((ValueError, TypeError)) as caught:
                call()
            error = caught.exception
            texts = [str(error), repr(error), *map(str, error.args)]
            for text in texts:
                for secret in secret_texts:
                    self.assertNotIn(secret, text)
            # A chained cause could print the secret in a traceback even when the message doesn't.
            self.assertIsNone(error.__cause__)


class MissingExtraTest(unittest.TestCase):
    def test_ciphers_name_the_extra_and_hashes_still_work(self):
        real_import = builtins.__import__

        def import_without_cryptography(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("No module named 'cryptography'")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", import_without_cryptography):
            for cipher_call in (lambda: crypto.decrypt(b"x" * 40, secrets.token_bytes(32)),
                                lambda: crypto.encrypt("x", secrets.token_bytes(32))):
                with self.assertRaisesRegex(ImportError, re.escape("pip install 'flntools[browser]'")):
                    cipher_call()
            self.assertEqual(crypto.sha256("a"), hashlib.sha256(b"a").hexdigest())


if __name__ == "__main__":
    unittest.main()
