import builtins
import hashlib
import io
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

HASHES = {
    "md5": crypto.hash_md5,
    "sha1": crypto.hash_sha1,
    "sha256": crypto.hash_sha256,
    "sha512": crypto.hash_sha512,
    "blake2b": crypto.hash_blake2b,
}


class HashTest(unittest.TestCase):
    def test_every_input_agrees_with_hashlib(self):
        content = b"x" * (crypto.CHUNK_SIZE * 2 + 17)
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "big.bin"
            file.write_bytes(content)
            for algorithm, hash_function in HASHES.items():
                with self.subTest(algorithm):
                    self.assertEqual(hash_function("héllo"), hashlib.new(algorithm, "héllo".encode()).hexdigest())
                    self.assertEqual(hash_function(b"\x00\xff"), hashlib.new(algorithm, b"\x00\xff").hexdigest())
                    expected = hashlib.new(algorithm, content).hexdigest()
                    self.assertEqual(hash_function(file), expected)
                    with file.open("rb") as handle:
                        self.assertEqual(hash_function(handle), expected)

    def test_file_object_is_read_from_its_position(self):
        handle = io.BytesIO(b"skipped|hashed")
        handle.seek(8)
        self.assertEqual(crypto.hash_sha256(handle), hashlib.sha256(b"hashed").hexdigest())

    def test_str_is_text_not_a_file_name(self):
        self.assertEqual(crypto.hash_sha256("photo.jpg"), hashlib.sha256(b"photo.jpg").hexdigest())

    def test_other_types_refused(self):
        with self.assertRaises(TypeError):
            crypto.hash_sha256(123)  # pyright: ignore[reportArgumentType]


class KeyTest(unittest.TestCase):
    def test_random_key(self):
        self.assertEqual(len(crypto.random_key()), 32)
        self.assertEqual(len(crypto.random_key(16)), 16)
        self.assertNotEqual(crypto.random_key(), crypto.random_key())

    def test_derive_scrypt_is_deterministic_per_salt(self):
        salt, other_salt = b"s" * 16, b"t" * 16
        key = crypto.derive_scrypt("password", salt)
        self.assertEqual(len(key), 32)
        self.assertEqual(key, crypto.derive_scrypt(b"password", salt))
        self.assertNotEqual(key, crypto.derive_scrypt("password", other_salt))
        self.assertEqual(len(crypto.derive_scrypt("password", salt, size=16)), 16)

    def test_derive_scrypt_refuses_a_short_salt(self):
        with self.assertRaises(ValueError):
            crypto.derive_scrypt("password", b"s" * 15)


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "cryptography is not installed")
class AesGcmTest(unittest.TestCase):
    def setUp(self):
        self.key = crypto.random_key()

    def test_round_trip(self):
        for key in (self.key, crypto.random_key(16), crypto.random_key(24)):
            self.assertEqual(crypto.decrypt_aes_gcm(crypto.encrypt_aes_gcm("sessionid=abc", key), key),
                             b"sessionid=abc")
        blob = crypto.encrypt_aes_gcm(b"\x00\xff", self.key, context=b"slot")
        self.assertEqual(crypto.decrypt_aes_gcm(blob, self.key, context=b"slot"), b"\x00\xff")

    def test_layout_and_fresh_nonce(self):
        first, second = crypto.encrypt_aes_gcm("same", self.key), crypto.encrypt_aes_gcm("same", self.key)
        self.assertNotEqual(first, second)
        self.assertEqual(first[0], 1)
        self.assertEqual(len(first), 1 + 12 + 4 + 16)

    def test_failures_raise_crypto_error(self):
        blob = crypto.encrypt_aes_gcm("secret", self.key, context=b"slot")
        flipped = blob[:-1] + bytes([blob[-1] ^ 1])
        damaged = {
            "flipped byte": (flipped, self.key, b"slot"),
            "wrong key": (blob, crypto.random_key(), b"slot"),
            "wrong context": (blob, self.key, b"other slot"),
            "truncated": (blob[:20], self.key, b"slot"),
            "unknown version": (b"\x02" + blob[1:], self.key, b"slot"),
        }
        for name, (damaged_blob, key, context) in damaged.items():
            with self.subTest(name), self.assertRaises(crypto.CryptoError):
                crypto.decrypt_aes_gcm(damaged_blob, key, context=context)
        self.assertTrue(issubclass(crypto.CryptoError, ValueError))


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "cryptography is not installed")
class AesCbcTest(unittest.TestCase):
    # NIST SP 800-38A, F.2.1 CBC-AES128.Encrypt, first two blocks.
    KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    PLAINTEXT = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a" "ae2d8a571e03ac9c9eb76fac45af8e51")
    CIPHERTEXT = bytes.fromhex("7649abac8119b246cee98e9b12e9197d" "5086cb9b507219ee95db113a917678b2")

    def test_published_vector(self):
        blob = crypto.encrypt_aes_cbc(self.PLAINTEXT, self.KEY, iv=self.IV)
        # PKCS7 adds a whole block of padding to data that is already whole blocks.
        self.assertEqual(len(blob), 48)
        self.assertEqual(blob[:32], self.CIPHERTEXT)

    def test_round_trip(self):
        key, iv = crypto.random_key(), crypto.random_key(16)
        for text in ("", "short", "é" * 40):
            with self.subTest(text=text):
                blob = crypto.encrypt_aes_cbc(text, key, iv=iv)
                self.assertEqual(crypto.decrypt_aes_cbc(blob, key, iv=iv), text.encode())

    def test_bad_padding_and_partial_blocks_raise_crypto_error(self):
        # The vector without its padding block ends in a byte that is not valid PKCS7 padding.
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_aes_cbc(self.CIPHERTEXT, self.KEY, iv=self.IV)
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_aes_cbc(self.CIPHERTEXT[:20], self.KEY, iv=self.IV)


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "cryptography is not installed")
class KeySizeTest(unittest.TestCase):
    def test_a_20_byte_key_raises_value_error(self):
        key = b"k" * 20
        calls = {
            "encrypt_aes_gcm": lambda: crypto.encrypt_aes_gcm("x", key),
            "decrypt_aes_gcm": lambda: crypto.decrypt_aes_gcm(b"x" * 40, key),
            "encrypt_aes_cbc": lambda: crypto.encrypt_aes_cbc("x", key, iv=b"i" * 16),
            "decrypt_aes_cbc": lambda: crypto.decrypt_aes_cbc(b"x" * 16, key, iv=b"i" * 16),
        }
        for name, call in calls.items():
            with self.subTest(name), self.assertRaises(ValueError) as caught:
                call()
            self.assertNotIsInstance(caught.exception, crypto.CryptoError)


class MissingExtraTest(unittest.TestCase):
    def test_ciphers_name_the_extra_and_hashes_still_work(self):
        real_import = builtins.__import__

        def import_without_cryptography(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("No module named 'cryptography'")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", import_without_cryptography):
            for cipher_call in (lambda: crypto.decrypt_aes_gcm(b"x" * 40, crypto.random_key()),
                                lambda: crypto.encrypt_aes_cbc("x", crypto.random_key(), iv=b"i" * 16)):
                with self.assertRaisesRegex(ImportError, re.escape("pip install 'flntools[browser]'")):
                    cipher_call()
            self.assertEqual(crypto.hash_sha256("a"), hashlib.sha256(b"a").hexdigest())


if __name__ == "__main__":
    unittest.main()
