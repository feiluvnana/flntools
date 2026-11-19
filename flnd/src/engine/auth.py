"""Accounts, switching, the sealed vault and the login methods.

src/docs/storage explains it.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Iterator, MutableMapping
from pathlib import Path

from flnd.engine import store
from flnd.errors import DiskError, LoginError
from flnkit import db, vault

__all__ = ["Vault", "seal_plaintext", "secrets"]

# The key file; None is the user's (`flnkit.vault.keyfile`). The tests set one of their own.
KEYFILE: Path | None = None
TABLE = ("CREATE TABLE IF NOT EXISTS secrets (account INTEGER NOT NULL, name TEXT NOT NULL,"
         " sealed BLOB NOT NULL, PRIMARY KEY (account, name)) WITHOUT ROWID")
# The name a version-1 home's one account per plugin takes.
DEFAULT = "default"


# ---------------------------------------------------------------------------------------------
# The sealed vault
# ---------------------------------------------------------------------------------------------

class Vault(MutableMapping[str, str]):
    """One account's secrets in `secret.db`, each sealed under the user's key (6.7).

    Opening makes neither `secret.db` nor the key file. A read with the key missing or wrong
    raises `LoginError` and keeps the rows; the first write makes a missing key file.

    Args:
        home: the home folder.
        account: the account's id.
        plugin: its plugin's code, for the hint of a key that is gone.
    """

    def __init__(self, home: Path, account: int, *, plugin: str = "") -> None:
        self._home, self._account, self._plugin = home, account, plugin
        self._db = db.Db(home / store.SECRETS, schema=TABLE, secure=True)

    def _key(self, *, create: bool) -> bytes:
        try:
            return vault.key(KEYFILE or vault.keyfile(), home=self._home, create=create)
        except LoginError as error:
            raise LoginError(str(error), hint=f"flnd auth login {self._plugin}"
                             if self._plugin else "") from None

    def __getitem__(self, name: str) -> str:
        found = self._db.one("SELECT sealed FROM secrets WHERE account = ? AND name = ?",
                             (self._account, name))
        if found is None:
            raise KeyError(name)
        try:
            return vault.unseal(self._key(create=False), self._account, name, found[0])
        except LoginError as error:
            raise LoginError(str(error), hint=error.hint or (
                f"flnd auth login {self._plugin}" if self._plugin else "")) from None

    def __setitem__(self, name: str, value: str) -> None:
        sealed = vault.seal(self._key(create=True), self._account, name, str(value))
        self._db.write("INSERT OR REPLACE INTO secrets VALUES (?, ?, ?)",
                       (self._account, name, sealed))

    def __delitem__(self, name: str) -> None:
        if name not in self:
            raise KeyError(name)
        self._db.write("DELETE FROM secrets WHERE account = ? AND name = ?",
                       (self._account, name))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self._db.one(
            "SELECT 1 FROM secrets WHERE account = ? AND name = ?",
            (self._account, name)) is not None

    def __iter__(self) -> Iterator[str]:
        found = self._db.all("SELECT name FROM secrets WHERE account = ? ORDER BY name",
                             (self._account,))
        return iter([name for (name,) in found])

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def clear(self) -> None:
        """Remove every secret of the account; a home without `secret.db` stays without."""
        if self._db.path.is_file():
            self._db.write("DELETE FROM secrets WHERE account = ?", (self._account,))

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database; the same as leaving `with`."""
        self._db.close()


def secrets(home: Path, account: int, *, plugin: str = "") -> Vault:
    """The sealed secrets of one account, for a `with` block.

    A version-1 `secret.db`, whose values are plaintext, is sealed first (`seal_plaintext`).
    """
    seal_plaintext(home)
    return Vault(home, account, plugin=plugin)


def seal_plaintext(home: Path) -> None:
    """Seal a version-1 `secret.db` in place, once: each plugin's plaintext values go under
    its default account, made when `flnd.db` has none, in one transaction (6.7).

    Raises:
        DiskError: `secret.db` can't be read or written.
    """
    path = home / store.SECRETS
    if not path.is_file():
        return
    sql = db.connect(path)
    try:
        if "slot" not in {row[1] for row in sql.execute("PRAGMA table_info(secrets)")}:
            return
        rows = sql.execute("SELECT slot, name, value FROM secrets").fetchall()
        with store.open(home) as home_db:
            ids = {row.plugin: row.id for row in reversed(home_db.accounts()) if row.default}
            for slot in sorted({slot for slot, _, _ in rows} - set(ids)):
                ids[slot] = home_db.login(slot, DEFAULT, origin="").id
        key = vault.key(KEYFILE or vault.keyfile(), home=home, create=True) if rows else b""
        sql.execute("BEGIN IMMEDIATE")
        try:
            sql.execute("ALTER TABLE secrets RENAME TO v1_secrets")
            sql.execute(TABLE)
            sql.executemany("INSERT INTO secrets VALUES (?, ?, ?)", [
                (ids[slot], name, vault.seal(key, ids[slot], name, _text(value)))
                for slot, name, value in rows])
            sql.execute("DROP TABLE v1_secrets")
            sql.execute("COMMIT")
        except BaseException:
            if sql.in_transaction:
                sql.execute("ROLLBACK")
            raise
        # No page or WAL frame keeps a plaintext value.
        sql.execute("VACUUM")
        sql.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as error:
        raise DiskError(f"{path}: {error}") from error
    finally:
        sql.close()
    if os.name != "nt":
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def _text(value: object) -> str:
    return bytes(value).decode("utf-8", "replace") if isinstance(
        value, (bytes, memoryview)) else str(value)
