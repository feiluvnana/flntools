# crypto

Phase 4, step 2. `shared/_crypto.py` is rewritten around names that say exactly what each function does: `hash_<algo>`, `encrypt_<cipher>` and `decrypt_<cipher>`. Hashing uses the stdlib. The ciphers need `cryptography`, which comes with the `browser` extra (its cookie reader depends on it) or can be installed alone.

```python
crypto.hash_sha256("text"); crypto.hash_sha256(b"bytes"); crypto.hash_sha256(Path("f.jpg")); crypto.hash_sha256(open("f", "rb"))
crypto.hash_md5(...); crypto.hash_sha1(...); crypto.hash_sha512(...); crypto.hash_blake2b(...)

key = crypto.random_key()                                   # 32 random bytes
key = crypto.derive_scrypt("password", salt)                # a 32-byte key from a password

blob = crypto.encrypt_aes_gcm(data, key)                    # authenticated; the nonce is stored in the blob
data = crypto.decrypt_aes_gcm(blob, key)
blob = crypto.encrypt_aes_cbc(data, key, iv=iv)             # for formats you don't control, such as loi's fields
data = crypto.decrypt_aes_cbc(blob, key, iv=iv)
```

## Hashing

`hash_<algo>(data) -> str` returns a hex string. `data` can be:

| Input | What is hashed |
| --- | --- |
| `str` | its UTF-8 text |
| `bytes` | the bytes |
| a `Path` | the file's contents, read in 1 MB chunks |
| a binary file object | its contents from the current position, read in chunks |

`hash_sha256("photo.jpg")` hashes the *text* "photo.jpg". Only a `Path` means "the file". The docstring says so.

## Keys

- `random_key(size=32) -> bytes` uses `secrets.token_bytes`.
- `derive_scrypt(password, salt, *, size=32) -> bytes` uses `hashlib.scrypt` with n=2¹⁵, r=8, p=1, and sets `maxmem` correctly. `salt` must be at least 16 bytes.

## Ciphers

| Function | Use |
| --- | --- |
| `encrypt_aes_gcm(data, key, *, context=b"")` / `decrypt_aes_gcm(blob, key, *, context=b"")` | your own data. The blob is a version byte, a random 12-byte nonce, then the ciphertext and tag. `context` is authenticated but not stored, so a value sealed for one slot can't be moved to another |
| `encrypt_aes_cbc(data, key, *, iv)` / `decrypt_aes_cbc(blob, key, *, iv)` | someone else's format. PKCS7 padding is added and removed |

- A key must be 16, 24 or 32 bytes; anything else raises `ValueError`.
- A wrong key, changed data, bad padding or an unknown version raises `CryptoError(ValueError)`, without saying which.
- `data` may be `str` (UTF-8) or `bytes`. Decryption returns `bytes`.

**HMAC is not included**, because nothing needs it. loi signs requests with plain SHA-256 over a salted string. If something ever does, it is `crypto.hmac_sha256(key, data)`, following the same pattern.

## Tests

- Every hash function agrees with `hashlib` for `str`, `bytes`, a `Path`, and a file object; a file larger than one chunk.
- `derive_scrypt` is deterministic for one salt and differs for another; a 15-byte salt raises `ValueError`.
- **GCM:**
  - round trip;
  - two encryptions of the same data differ;
  - a flipped byte, a wrong key, a wrong `context` and a truncated blob all raise `CryptoError`.
- **CBC:**
  - a published AES-128-CBC test vector;
  - round trip;
  - bad padding raises `CryptoError`.
- A 20-byte key raises `ValueError`.
