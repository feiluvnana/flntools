# crypto

Phase 3. Depends on nothing. Hashes, HMAC, and tokens use only the stdlib. `seal` and `unseal` need the `crypto` extra (`cryptography`).

[`shared/_crypto.py`](../../shared/_crypto.py). Hashes and random values with sensible defaults, and one way to encrypt a secret at rest.

```python
crypto.digest("hello")                    # sha256 hex
crypto.digest(Path("clip.mp4"))           # reads the file in 1 MB chunks
crypto.digest(b"...", algo="blake2b")
crypto.sign(key, "message")               # HMAC-sha256 hex
crypto.verify(key, "message", signature)  # constant-time check
crypto.equal(sig_a, sig_b)                # constant-time comparison
crypto.token()                            # 32 random bytes, URL-safe base64
crypto.randbytes(16)

key = crypto.keygen()                     # 32 random bytes
blob = crypto.seal("sessionid=abc", key)  # bytes
crypto.unseal(blob, key)                  # b"sessionid=abc"
key = crypto.derive("pw", salt)           # scrypt, 32 bytes
```

## Functions

| Function | Behavior |
| --- | --- |
| `digest(data, *, algo="sha256") -> str` | `data` is `str` (UTF-8 encoded), `bytes`, or a `Path`, which is read in chunks. `algo` is any `hashlib` name. Returns a hex string. |
| `sign(key, data, *, algo="sha256") -> str` | HMAC. `key` and `data` are `str` or `bytes`. Returns a hex string. |
| `verify(key, data, signature, *, algo="sha256") -> bool` | recomputes `sign` and compares in constant time. |
| `equal(a, b) -> bool` | `hmac.compare_digest`. |
| `token(size=32) -> str` | `secrets.token_urlsafe`. |
| `randbytes(n) -> bytes` | `secrets.token_bytes`. |
| `keygen() -> bytes` | 32 random bytes for `seal`. |
| `derive(password, salt, *, n=2**15, r=8, p=1) -> bytes` | `hashlib.scrypt`, 32 bytes. `salt` must be at least 16 bytes. |
| `seal(data, key) -> bytes` | AES-256-GCM. The blob is a version byte (`1`), a random 12-byte nonce, then the ciphertext with its tag. `data` is `str` or `bytes`. |
| `unseal(blob, key) -> bytes` | Reverses `seal`. A wrong key, changed bytes, or an unknown version raises `crypto.CryptoError`, without saying which. |

A key that is not 32 bytes raises `ValueError`. Calling `seal` or `unseal` without `cryptography` installed raises the extra's `ImportError`, but the hash functions still work.

## Tests

- `digest` of a `str`, `bytes`, and a `Path` agree with `hashlib`. A file larger than one chunk.
- `sign` agrees with the stdlib `hmac`. `verify` accepts the right signature and rejects a changed one. `equal` handles both `str` and `bytes`.
- `token` length and alphabet.
- `seal` round trip for `str` and `bytes`. Two seals of the same data differ because of the nonce.
- `CryptoError` for a wrong key, a flipped byte, a truncated blob, and an unknown version.
- `ValueError` for a 16-byte key.
- `derive` is deterministic for the same salt and different for another salt.
