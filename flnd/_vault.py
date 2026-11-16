"""Secrets sealed with AES-256-GCM, and a vault of them in one SQLite file under a key file.

A library: it knows nothing of flnd's sources or homes. What it offers, for any caller:
- `encrypt` and `decrypt`: authenticated encryption of a value under a 32-byte key, bound to a
  context (associated data), so a sealed value moved elsewhere doesn't open.
- `Vault(path, key_path, slot)`: one slot's secrets as a mapping of text to text, each value
  sealed in its own row of the database at `path`, under the key in `key_path`.
- `key(path)`: the key in a key file, made with mode 0600 on first use.
- `Keychain(account)`: the key in the operating system's keychain instead (macOS's
  `security`, Linux's `secret-tool`), and `to_keychain` and `to_file`, which move a key
  between the two once, removing the old copy only after the new one reads back.
- `digest`: the hex hash of text, bytes, a file or a stream.

    with Vault(Path("secrets.db"), Path("secret.key"), "tlg") as saved:
        saved["session"] = "1BVts…"

Who calls it: in flnd, `flnd/_home.py` opens one `Vault` per source (`_home.secrets`), with the
home's `secrets.db` and `secret.key`, and a source reaches it as `Site.secrets`
(`flnd/site.py`) to keep a login token or session. Its errors are `flnd.errors` classes.

How a value is kept:
1. `Vault[name] = value` reads (or makes) the key file, then seals the value with `encrypt`,
   binding it to `<slot>\\0<name>` as associated data;
2. the blob (version byte, nonce, ciphertext and tag) is written to the `secrets` table, one
   row per `(slot, name)`, in its own transaction (`flnd._db.Db.write`);
3. `Vault[name]` reads the row and opens it with `decrypt`; a wrong key, a moved row or a
   changed byte all fail the same way, as `LoginError` with the vault's `hint`.

Rules a change must keep:
- A secret's value never reaches a message: every way decryption fails says the same thing.
- Opening a vault creates nothing: the database is made on the first write, the key on the
  first value read or written.
- The key file is made with `O_EXCL` and mode 0600, so two processes never make two keys; one
  that finds a short key waits `KEY_WAIT` for the other to finish writing it.
- A key never goes on a command line or into a message: a keychain tool reads it on standard
  input, and prints it only to a pipe this module reads.
- It imports only the stdlib, `cryptography` (on first use), `flnd._db` and `flnd.errors`.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import secrets as _secrets
import subprocess
import sys
import time
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, cast

from flnd import _db
from flnd.errors import DiskError, LoginError, ToolError, UsageError

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["Keychain", "Vault", "decrypt", "digest", "encrypt", "key", "to_file", "to_keychain"]

# ---------------------------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------------------------

Data = str | bytes | bytearray | memoryview
HashInput = str | bytes | bytearray | memoryview | Path | BinaryIO

# The blob is one version byte, a 12-byte random nonce (GCM's standard size), then the
# ciphertext with its 16-byte tag. The format is fixed: vaults hold blobs of version 1.
GCM_VERSION = 1
GCM_NONCE_SIZE = 12
GCM_TAG_SIZE = 16
# AES-256: the only key size, the size `secrets.token_bytes(32)` makes.
KEY_SIZE = 32
# One message for every way decryption fails, so it says nothing about the key or the data.
DECRYPT_FAILED = "cannot decrypt: wrong key or damaged data"
# What a blob that does not open says to do; a `Vault` gives its own advice instead.
SEAL_AGAIN = "seal it again, with the key it is opened with"
MISSING_CRYPTOGRAPHY = "sealing secrets needs cryptography"


def digest(data: HashInput, *, algorithm: str = "sha256") -> str:
    """The hex digest of text, bytes-like data, a `Path`, or a binary stream.

    Args:
        data: what to hash; a `Path` is read as a file, text as UTF-8.
        algorithm: any `hashlib` name.

    Returns:
        The digest, in lowercase hex.

    Raises:
        TypeError: `data` is none of those.

    Example:
        >>> digest("abc")[:16]
        'ba7816bf8f01cfea'
    """
    if isinstance(data, Path):
        with data.open("rb") as handle:
            return digest(handle, algorithm=algorithm)
    if isinstance(data, str):
        return hashlib.new(algorithm, data.encode("utf-8")).hexdigest()
    if isinstance(data, (bytes, bytearray, memoryview)):
        return hashlib.new(algorithm, data).hexdigest()
    if isinstance(data, io.BytesIO):
        view = data.getbuffer()[data.tell():]
        data.seek(0, io.SEEK_END)
        return hashlib.new(algorithm, view).hexdigest()
    if hasattr(data, "read"):
        return hashlib.file_digest(cast(Any, data), algorithm).hexdigest()
    raise TypeError(
        f"cannot hash {type(data).__name__}: pass str, bytes, a Path, or a binary file"
    )


def encrypt(data: Data, key: bytes, *, context: bytes = b"") -> bytes:
    """Seal `data` with AES-256-GCM into a blob only `decrypt`, with the same key, opens.

    Args:
        data: the secret; text is sealed as UTF-8.
        key: 32 bytes.
        context: associated data the blob is bound to: `decrypt` needs the same.

    Returns:
        The version byte, the nonce, then the ciphertext and its tag.

    Raises:
        ValueError: the key isn't 32 bytes.
        ToolError: `cryptography` isn't installed.
    """
    cipher = _aes_gcm(key)
    nonce = _secrets.token_bytes(GCM_NONCE_SIZE)
    return bytes([GCM_VERSION]) + nonce + cipher.encrypt(nonce, _as_bytes(data), context)


def decrypt(blob: bytes, key: bytes, *, context: bytes = b"") -> bytes:
    """Open a blob from `encrypt`, checking that nobody changed it.

    Args:
        blob: what `encrypt` returned.
        key: the key it was sealed with.
        context: the associated data it was sealed with.

    Returns:
        The secret's bytes.

    Raises:
        LoginError: the key, the context or the blob is wrong; the message says no more.
        ToolError: `cryptography` isn't installed.
    """
    cipher = _aes_gcm(key)
    from cryptography.exceptions import InvalidTag

    if len(blob) < 1 + GCM_NONCE_SIZE + GCM_TAG_SIZE or blob[0] != GCM_VERSION:
        raise LoginError(DECRYPT_FAILED, hint=SEAL_AGAIN)
    nonce, ciphertext = blob[1:1 + GCM_NONCE_SIZE], blob[1 + GCM_NONCE_SIZE:]
    try:
        return cipher.decrypt(nonce, ciphertext, context)
    # InvalidTag is the only way GCM rejects; its traceback says nothing more.
    except InvalidTag:
        raise LoginError(DECRYPT_FAILED, hint=SEAL_AGAIN) from None


def _as_bytes(data: Data) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError(f"expected str or bytes, not {type(data).__name__}")


def _aes_gcm(key: bytes) -> AESGCM:
    """`cryptography`'s AESGCM for a 32-byte key, imported here so `digest` never needs it.

    Args:
        key: the AES-256 key; its length is checked, its bytes never shown.

    Returns:
        A cipher object that seals and opens with `key`.

    Raises:
        ValueError: the key isn't 32 bytes.
        ToolError: `cryptography` isn't installed.
    """
    # The message gives the key's length only, never its bytes.
    if len(key) != KEY_SIZE:
        raise ValueError(
            f"an AES-256 key must be {KEY_SIZE} bytes, not {len(key)}: use secrets.token_bytes(32)"
        )
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise ToolError(MISSING_CRYPTOGRAPHY, hint="pip install cryptography") from None
    return AESGCM(key)



# ---------------------------------------------------------------------------------------------
# The vault: sealed values in a database, under a key file
# ---------------------------------------------------------------------------------------------

# The vault's one table: each value sealed under the key, bound to its slot and name. `{}` takes
# a schema name ("" or "<name>." for an attached database).
SCHEMA = ("CREATE TABLE IF NOT EXISTS {}secrets (slot TEXT NOT NULL, name TEXT NOT NULL,"
          " value BLOB NOT NULL, PRIMARY KEY (slot, name)) WITHOUT ROWID")
# How long a key file shorter than KEY_SIZE may be another process's key still being written, in
# seconds, before it counts as damaged; 32 bytes are written in microseconds, so 2 s is ample.
KEY_WAIT = 2.0
# How often a short key file is read again while waiting, in seconds.
KEY_POLL = 0.01
# What a damaged key file says to do, unless the caller gives its own advice.
DAMAGED_KEY = "delete it, then seal the secrets again"


class Vault(MutableMapping[str, str]):
    """One slot's secrets: a mapping of `str` to `str`, each value sealed in its own row.

    Each value is sealed with AES-256-GCM under the vault's key, with `<slot>\\0<name>` as
    associated data, so a value copied to another row doesn't open. Opening a vault creates
    nothing: the database is made on the first write, and the key on the first read or write
    of a value.

    Args:
        path: the database file (`secrets.db`).
        key_path: where the key is: a key file, made with mode 0600 when missing, or a
            `Keychain` item, made when missing.
        slot: whose secrets these are (a source's code); another slot's aren't visible.
        hint: what a value that doesn't open, or a damaged key, says to do.

    Example:
        >>> import tempfile
        >>> folder = Path(tempfile.mkdtemp())
        >>> with Vault(folder / "secrets.db", folder / "secret.key", "tlg") as saved:
        ...     saved["session"] = "s"
        ...     dict(saved)
        {'session': 's'}
    """

    def __init__(self, path: Path, key_path: Path | Keychain, slot: str, *,
                 hint: str = DAMAGED_KEY):
        self._key_path, self._slot, self._hint = key_path, slot, hint
        self._key: bytes | None = None
        self._db = _db.Db(path, schema=SCHEMA.format(""))

    def __getitem__(self, name: str) -> str:
        found = self._db.one("SELECT value FROM secrets WHERE slot = ? AND name = ?",
                             (self._slot, name))
        if found is None:
            raise KeyError(name)
        try:
            return decrypt(found[0], self._secret_key(), context=self._context(name)).decode()
        except LoginError as error:
            raise LoginError(str(error), hint=self._hint) from None

    def __setitem__(self, name: str, value: str) -> None:
        sealed = encrypt(value, self._secret_key(), context=self._context(name))
        self._db.write("INSERT OR REPLACE INTO secrets VALUES (?, ?, ?)",
                       (self._slot, name, sealed))

    def __delitem__(self, name: str) -> None:
        if name not in self:
            raise KeyError(name)
        self._db.write("DELETE FROM secrets WHERE slot = ? AND name = ?", (self._slot, name))

    def __contains__(self, name: object) -> bool:
        return self._db.one("SELECT 1 FROM secrets WHERE slot = ? AND name = ?",
                            (self._slot, name)) is not None

    def __iter__(self) -> Iterator[str]:
        found = self._db.all("SELECT name FROM secrets WHERE slot = ? ORDER BY name", (self._slot,))
        return iter([name for (name,) in found])

    def __len__(self) -> int:
        found = self._db.one("SELECT count(*) FROM secrets WHERE slot = ?", (self._slot,))
        return found[0] if found else 0

    def clear(self) -> None:
        """Remove every secret of the slot; a vault with no database is left without one."""
        if self._db.path.is_file():
            self._db.write("DELETE FROM secrets WHERE slot = ?", (self._slot,))

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database; the same as leaving `with`."""
        self._db.close()

    def _context(self, name: str) -> bytes:
        return f"{self._slot}\0{name}".encode()

    def _secret_key(self) -> bytes:
        if self._key is None:
            where = self._key_path
            self._key = where.key() if isinstance(where, Keychain) else key(where, hint=self._hint)
        return self._key


def key(path: Path, *, hint: str = DAMAGED_KEY) -> bytes:
    """The 32-byte key in a key file, made with mode 0600 when missing.

    Args:
        path: the key file.
        hint: what a damaged key says to do.

    Returns:
        The key's 32 bytes.

    Raises:
        DiskError: the file is damaged: too long, or still short after `KEY_WAIT`.
    """
    # A short key may be another process's that is still being written, so it is read again
    # until `KEY_WAIT` has passed; only then is it damaged (a process that died writing it).
    deadline = time.monotonic() + KEY_WAIT
    while True:
        try:
            secret = path.read_bytes()
        except FileNotFoundError:
            created = _create_key(path)
            if created is not None:
                return created
            # Lost the race: the winner's file exists now, maybe still short, so read it again.
            if time.monotonic() >= deadline:
                raise DiskError(
                    f"{path} is damaged: exists but cannot be read (e.g. broken link)", hint=hint,
                ) from None
            time.sleep(KEY_POLL)
            continue
        if len(secret) == KEY_SIZE:
            return secret
        # A process killed between creating the file and writing it leaves it empty for good.
        if len(secret) > KEY_SIZE or time.monotonic() >= deadline:
            raise DiskError(
                f"{path} is damaged: it should hold {KEY_SIZE} bytes, not {len(secret)}", hint=hint,
            )
        time.sleep(KEY_POLL)


def _create_key(path: Path) -> bytes | None:
    """Make the key file with a new key; None when another process made it first.

    Args:
        path: the key file, which must not exist yet.

    Returns:
        The new key; None when the file was there already.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL makes "the first creator wins" without a hard link.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(path, flags, 0o600)
    except FileExistsError:
        return None
    # Safe because the key is 32 bytes, written in one write, and readers wait up to KEY_WAIT.
    secret = _secrets.token_bytes(KEY_SIZE)
    try:
        try:
            file = os.fdopen(handle, "wb")
        except BaseException:
            os.close(handle)
            raise
        with file:
            file.write(secret)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return secret


# ---------------------------------------------------------------------------------------------
# The key in the OS keychain
# ---------------------------------------------------------------------------------------------

_PLATFORM = sys.platform
# macOS's keychain tool, at the path every Mac has it; a fixed path, so no program named
# `security` earlier on PATH is ever handed a key.
SECURITY = "/usr/bin/security"
# Linux's Secret Service tool (libsecret), found on PATH.
SECRET_TOOL = "secret-tool"
# What `security find-generic-password` exits with when no item matches.
NOT_FOUND = 44
# Seconds a keychain tool may take before it counts as hung: a locked keychain that wants a
# password shows a dialog, which nobody may be there to answer.
TOOL_TIMEOUT = 30.0
# What a missing keychain says, unless the caller gives its own advice.
NO_KEYCHAIN = "keep the key in a file instead"
# The variable flnd's test suite sets (as `flnd._cookies.NO_PROFILES` reads it): while it is
# set, the real keychain tools are refused, so no test can read or change a person's keychain.
NO_REAL_KEYCHAIN = "FLND_TEST_NO_PROFILES"


class Keychain:
    """A 32-byte key kept in the operating system's keychain, instead of a key file.

        chain = Keychain("vault-1f2e", service="example")
        secret = chain.key()          # made and stored on first use
        Vault(Path("secrets.db"), chain, "tlg")

    On macOS the item is a generic password in the login keychain, through `/usr/bin/security`;
    on Linux an item of the Secret Service (GNOME Keyring, KWallet), through `secret-tool`. The
    key is stored as base64 text. It never goes on a command line, where any process could read
    it: macOS's tool reads the command that stores it from its standard input (`security -i`),
    and `secret-tool store` reads the secret from its standard input. Every tool runs from an
    argument list, never through a shell, and its output is never logged.

    Args:
        account: the item's account: which key this is (in flnd, one per home).
        service: the item's service name, which groups a program's items.
        hint: what a missing keychain says to do.

    Raises:
        UsageError: when used, there is no keychain tool here (`available` is False).
    """

    def __init__(self, account: str, *, service: str = "flnd", hint: str = NO_KEYCHAIN):
        # `security -i` splits its commands at spaces, so the names must have none.
        if not account or any(char.isspace() for char in account + service):
            raise ValueError("a keychain account and service are words without spaces")
        self.account, self.service, self._hint = account, service, hint

    @staticmethod
    def tool() -> str | None:
        """The keychain tool here: macOS's `security`, else Linux's `secret-tool`; None for none.

        Returns:
            The tool's path, or None when there is none, or when the test suite's guard
            (`NO_REAL_KEYCHAIN`) refuses the real one.
        """
        import shutil

        if _PLATFORM == "darwin":
            found = SECURITY if os.path.exists(SECURITY) else None
        elif _PLATFORM == "win32":
            # Windows' Credential Manager has no command-line tool that reads a secret back.
            found = None
        else:
            found = shutil.which(SECRET_TOOL)
        if found is not None and os.environ.get(NO_REAL_KEYCHAIN) and not _is_fake(found):
            return None
        return found

    @classmethod
    def available(cls) -> bool:
        """Whether this system has a keychain tool flnd can use."""
        return cls.tool() is not None

    def read(self) -> bytes | None:
        """The key in the keychain; None when there is no item for this account.

        Returns:
            The key's 32 bytes, or None.

        Raises:
            UsageError: there is no keychain here.
            DiskError: the item isn't a key (not base64 of 32 bytes).
            ToolError: the keychain refused (locked, or access denied).
        """
        tool = self.require()
        if _is_security(tool):
            done = _call([tool, "find-generic-password", "-s", self.service, "-a", self.account,
                          "-w"])
            if done.returncode == NOT_FOUND:
                return None
        else:
            done = _call([tool, "lookup", "service", self.service, "account", self.account])
            # `secret-tool lookup` exits 1 with nothing printed for an item that isn't there.
            if done.returncode == 1 and not done.stdout.strip():
                return None
        if done.returncode != 0:
            raise _refused(tool, done)
        try:
            secret = base64.b64decode(done.stdout.strip(), validate=True)
        except ValueError:
            secret = b""
        if len(secret) != KEY_SIZE:
            raise DiskError(f"the keychain item {self.service}/{self.account} is not a key",
                            hint="delete the item, then seal the secrets again")
        return secret

    def write(self, secret: bytes) -> None:
        """Store `secret` as the account's key, replacing any, and check that it reads back.

        Args:
            secret: the key's 32 bytes.

        Raises:
            ValueError: the key isn't 32 bytes.
            UsageError: there is no keychain here.
            ToolError: the keychain refused, or what it holds afterwards isn't `secret`.
        """
        if len(secret) != KEY_SIZE:
            raise ValueError(f"a key is {KEY_SIZE} bytes, not {len(secret)}")
        tool = self.require()
        text = base64.b64encode(secret).decode("ascii")
        if _is_security(tool):
            # `-U` updates an item that is there. The command goes in on standard input, so the
            # key is never in the tool's argument list.
            line = (f"add-generic-password -U -s {self.service} -a {self.account} "
                    f"-w {text}\n")
            done = _call([tool, "-i"], stdin=line)
        else:
            done = _call([tool, "store", f"--label={self.service} key", "service", self.service,
                          "account", self.account], stdin=text)
        if done.returncode != 0:
            raise _refused(tool, done)
        # `security -i` exits 0 even when a command in it failed, so the read-back is the check.
        if self.read() != secret:
            raise ToolError(f"the keychain didn't keep the key {self.service}/{self.account}",
                            hint="unlock the keychain, then try again")

    def delete(self) -> None:
        """Remove the account's key from the keychain; nothing when there is none.

        Raises:
            UsageError: there is no keychain here.
            ToolError: the keychain refused.
        """
        tool = self.require()
        if _is_security(tool):
            done = _call([tool, "delete-generic-password", "-s", self.service, "-a",
                          self.account])
            if done.returncode in (0, NOT_FOUND):
                return
        else:
            done = _call([tool, "clear", "service", self.service, "account", self.account])
            if done.returncode == 0:
                return
        raise _refused(tool, done)

    def key(self) -> bytes:
        """The account's key: the one stored, or a new one stored now.

        Returns:
            The key's 32 bytes.

        Raises:
            UsageError: there is no keychain here.
            ToolError: the keychain refused.
        """
        found = self.read()
        if found is not None:
            return found
        made = _secrets.token_bytes(KEY_SIZE)
        self.write(made)
        return made

    def require(self) -> str:
        """The keychain tool, checked to be there before anything is stored in it.

        Returns:
            The tool's path.

        Raises:
            UsageError: there is no keychain here, with the hint this item was made with.
        """
        tool = self.tool()
        if tool is None:
            raise UsageError("the OS keychain isn't available here: it needs macOS's "
                             f"{SECURITY}, or Linux's {SECRET_TOOL}", hint=self._hint)
        return tool


def to_keychain(path: Path, chain: Keychain) -> bool:
    """Move a key file's key into the keychain, once; the file goes only after it reads back.

    1. No key file: nothing to move (the keychain holds the key, or will make one).
    2. Store the file's key in the keychain (`write` checks that it reads back the same).
    3. Delete the file. A crash before this leaves both, holding the same key, and the next
       call moves it again, harmlessly.

    Args:
        path: the key file.
        chain: the keychain item to move it to; an item there already is replaced, since the
            file's key is the one the vault's values are sealed with.

    Returns:
        Whether a key was moved.

    Raises:
        DiskError: the key file is damaged.
        UsageError: there is no keychain here.
        ToolError: the keychain refused, or didn't keep the key.
    """
    if not path.is_file():
        return False
    chain.write(key(path))
    path.unlink()
    return True


def to_file(chain: Keychain, path: Path) -> bool:
    """Move the keychain's key into a key file, once; the item goes only after the file reads back.

    1. A key file there already, or no keychain item: nothing to move.
    2. Write the key to the file, mode 0600, made with `O_EXCL` so it never replaces another.
    3. Read the file back; only when it holds the key is the keychain item deleted.

    Args:
        chain: the keychain item.
        path: the key file to make.

    Returns:
        Whether a key was moved.

    Raises:
        DiskError: the file can't be made, or doesn't read back the same.
        ToolError: the keychain refused.
    """
    if path.exists():
        return False
    secret = chain.read()
    if secret is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    except OSError as error:
        raise DiskError(f"{path}: {error.strerror or error}") from None
    with os.fdopen(handle, "wb") as file:
        file.write(secret)
    if path.read_bytes() != secret:
        path.unlink(missing_ok=True)
        raise DiskError(f"{path} didn't keep the key; the keychain still holds it")
    chain.delete()
    return True


def _call(argv: list[str], *, stdin: str = "") -> subprocess.CompletedProcess[str]:
    """Run a keychain tool from an argument list, never a shell, and capture what it says.

    Args:
        argv: the tool and its arguments; never a secret, which goes in `stdin`.
        stdin: what the tool reads, such as the secret to store.

    Returns:
        The finished process, with its output as text; the caller never logs it.

    Raises:
        ToolError: the tool can't start, or takes longer than `TOOL_TIMEOUT`.
    """
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False,
                              timeout=TOOL_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ToolError(f"{Path(argv[0]).name} didn't answer in {TOOL_TIMEOUT:.0f} s",
                        hint="unlock the keychain, then try again") from None
    except OSError as error:
        raise ToolError(f"{argv[0]}: {error.strerror or error}") from None


def _refused(tool: str, done: subprocess.CompletedProcess[str]) -> ToolError:
    """The error for a keychain tool that failed: its first line on standard error.

    Its standard output is never shown: `find-generic-password -w` prints the key there.

    Args:
        tool: the tool's path.
        done: the finished process.

    Returns:
        The `ToolError` to raise.
    """
    said = (done.stderr or "").strip().splitlines()
    reason = said[0] if said else f"exit {done.returncode}"
    return ToolError(f"the keychain refused ({Path(tool).name}): {reason}",
                     hint="unlock the keychain, or allow access, then try again")


def _is_security(tool: str) -> bool:
    """Whether a keychain tool is macOS's `security` (or a test's stand-in named so)."""
    return Path(tool).name == "security"


def _is_fake(tool: str) -> bool:
    """Whether a tool lives in a temporary folder: a test's stand-in, never the real one."""
    import tempfile

    return Path(tool).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
