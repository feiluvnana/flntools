import builtins
import hashlib
import hmac
import re
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


class DigestTest(unittest.TestCase):
    def test_str_bytes_and_path_agree_with_hashlib(self):
        self.assertEqual(crypto.digest("hello"), hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(crypto.digest(b"hello", algo="blake2b"), hashlib.blake2b(b"hello").hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "big.bin"
            content = b"x" * (crypto.CHUNK_SIZE * 2 + 17)
            file.write_bytes(content)
            self.assertEqual(crypto.digest(file), hashlib.sha256(content).hexdigest())

    def test_other_types_refused(self):
        with self.assertRaises(TypeError):
            crypto.digest(123)


class SignTest(unittest.TestCase):
    def test_agrees_with_hmac(self):
        expected = hmac.new(b"key", b"message", "sha256").hexdigest()
        self.assertEqual(crypto.sign("key", "message"), expected)
        self.assertEqual(crypto.sign(b"key", b"message"), expected)

    def test_verify(self):
        signature = crypto.sign("key", "message")
        self.assertTrue(crypto.verify("key", "message", signature))
        self.assertFalse(crypto.verify("key", "messagE", signature))
        changed = signature[:-1] + ("1" if signature[-1] == "0" else "0")
        self.assertFalse(crypto.verify("key", "message", changed))

    def test_equal(self):
        self.assertTrue(crypto.equal("abc", "abc"))
        self.assertTrue(crypto.equal(b"abc", b"abc"))
        self.assertTrue(crypto.equal("abc", b"abc"))
        self.assertFalse(crypto.equal("abc", "abd"))


class RandomTest(unittest.TestCase):
    def test_token(self):
        value = crypto.token()
        self.assertEqual(len(value), 43)
        self.assertRegex(value, r"^[A-Za-z0-9_-]+$")
        self.assertEqual(len(crypto.token(16)), 22)
        self.assertNotEqual(crypto.token(), crypto.token())

    def test_randbytes_and_keygen(self):
        self.assertEqual(len(crypto.randbytes(16)), 16)
        self.assertEqual(len(crypto.keygen()), 32)


class DeriveTest(unittest.TestCase):
    def test_deterministic_per_salt(self):
        salt, other = b"s" * 16, b"t" * 16
        key = crypto.derive("pw", salt)
        self.assertEqual(len(key), 32)
        self.assertEqual(key, crypto.derive("pw", salt))
        self.assertNotEqual(key, crypto.derive("pw", other))

    def test_short_salt(self):
        with self.assertRaises(ValueError):
            crypto.derive("pw", b"short")


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "cryptography is not installed")
class SealTest(unittest.TestCase):
    def setUp(self):
        self.key = crypto.keygen()

    def test_round_trip(self):
        self.assertEqual(crypto.unseal(crypto.seal("sessionid=abc", self.key), self.key), b"sessionid=abc")
        self.assertEqual(crypto.unseal(crypto.seal(b"\x00\xff", self.key), self.key), b"\x00\xff")

    def test_layout_and_nonce(self):
        first, second = crypto.seal("same", self.key), crypto.seal("same", self.key)
        self.assertNotEqual(first, second)
        self.assertEqual(first[0], 1)
        self.assertEqual(len(first), 1 + 12 + 4 + 16)

    def test_failures_raise_crypto_error(self):
        blob = crypto.seal("secret", self.key)
        flipped = blob[:-1] + bytes([blob[-1] ^ 1])
        damaged = {
            "wrong key": (blob, crypto.keygen()),
            "flipped byte": (flipped, self.key),
            "truncated": (blob[:10], self.key),
            "unknown version": (b"\x02" + blob[1:], self.key),
        }
        for name, (data, key) in damaged.items():
            with self.subTest(name), self.assertRaises(crypto.CryptoError) as caught:
                crypto.unseal(data, key)
            self.assertEqual(str(caught.exception), "cannot unseal: wrong key or damaged data")

    def test_short_key(self):
        with self.assertRaises(ValueError):
            crypto.seal("x", b"k" * 16)
        with self.assertRaises(ValueError):
            crypto.unseal(b"x", b"k" * 16)


class MissingExtraTest(unittest.TestCase):
    def test_seal_names_the_extra_and_hashes_still_work(self):
        real_import = builtins.__import__

        def no_cryptography(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("No module named 'cryptography'")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", no_cryptography):
            with self.assertRaisesRegex(ImportError, re.escape("pip install 'flntools[crypto]'")):
                crypto.seal("x", crypto.keygen())
            self.assertEqual(crypto.digest("a"), hashlib.sha256(b"a").hexdigest())


if __name__ == "__main__":
    unittest.main()
