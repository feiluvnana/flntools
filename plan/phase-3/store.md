# store

Phase 3. Depends on [crypto](crypto.md) for `secrets`. `sqlite3` is in the stdlib. `secrets` needs the `crypto` extra.

[`shared/_store.py`](../../shared/_store.py). A key-value store in one sqlite file, plus an encrypted secret store on top of it.

```python
db = store.open(".flnd/state.db")  # creates the file and its folder

seen = db.table("downloads")       # a named key-value table, dict-like
seen["ins:3210"] = {"path": "ins/a.jpg", "size": 12345}
"ins:3210" in seen
seen.get("ins:3210", {})
del seen["ins:3210"]
len(seen)
for key, value in seen.items(prefix="ins:"):
    ...

with db.batch():                   # one transaction for many writes
    for item in items:
        seen[item.key] = item.meta

secrets = db.secrets()             # values encrypted with crypto.seal
secrets["ins:sessionid"] = "abc"
secrets["ins:sessionid"]           # "abc"

db.sql                             # the real sqlite3.Connection
db.close()                         # or: with store.open(...) as db:
```

## Tables

`db.table(name)` returns a `Table`, a `MutableMapping[str, Any]`.

- Keys are strings.
- Values are anything `json.dumps` accepts. A value that is not JSON raises `TypeError` when it is set.
- `items(prefix="")`, `keys(prefix="")`, and `values(prefix="")` filter by key prefix with an indexed range query, not by scanning every row.
- `table.mtime(key) -> datetime | None` returns when the key was last written.

Every table lives in one SQL table:

```sql
CREATE TABLE IF NOT EXISTS kv (
  ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated REAL NOT NULL,
  PRIMARY KEY (ns, key)
) WITHOUT ROWID;
```

The database opens in WAL mode. Each write commits on its own unless it is inside `db.batch()`, which commits once at the end, or rolls back if the block raises. One connection is shared behind a lock, so threads (such as a Playwright response handler) can write safely.

Anything the key-value API doesn't cover goes through `db.sql`. Your own tables live in the same file next to `kv`.

## Secrets

`db.secrets(key=None, *, password=None)` returns a `Table` whose values are `str`.

- Each value is encrypted with `crypto.seal` and stored base64-encoded in namespace `secret:`.
- Secrets never appear in plain text in the database file.
- A value that cannot be decrypted raises `crypto.CryptoError` on read.

Where the key comes from:

- **`key=None`:** the key is read from `<db>.key`, for example `.flnd/state.db.key`. It is created on first use with `crypto.keygen()` and file mode `0600`. This keeps secrets safe from someone who copies only the database. It does **not** protect against someone who can read the whole folder. That is the honest limit of a key file, and it is written in the module's docstring.
- **`password="..."`:** a password-derived key, made with `crypto.derive` and a random 16-byte salt. The salt is stored in `kv` under namespace `meta`, key `secrets.salt`, and created on first use. The caller passes the password rather than a derived key, because the salt lives in the database and is not known before it is opened.
- **`key=...`:** any 32-byte key the caller already has. Passing both `key` and `password` raises `ValueError`.

## Tests

- Table:
  - set, get, `in`, delete, `len`, and iteration;
  - prefix queries;
  - JSON round trip;
  - `TypeError` for a value that is not JSON;
  - `mtime`.
- Two tables in one file do not see each other's keys.
- `batch` commits once, and rolls back when the block raises.
- Writes from several threads.
- Reopening the file keeps the data.
- `db.sql` works for a custom table.
- Secrets:
  - round trip;
  - the raw `kv` row does not contain the plain text;
  - a key file is created with mode `0600`;
  - a wrong key raises `CryptoError`;
  - a password-derived key and its stored salt.
