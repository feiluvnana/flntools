# kv

Phase 4, step 2. `shared/_kv.py` replaces `shared/_store.py`. It is a simple key-value store in one sqlite file, used like a dict.

```python
db = kv.open(".flnd/state.db")                     # also: with kv.open(...) as db
db = kv.open(".flnd/state.db", table="downloads")  # separate tables in one file
db["ins:1"] = {"path": "…"}; db["ins:1"]; "ins:1" in db; del db["ins:1"]; len(db)
db.get("ins:1", default=None)
db.set("loi:quota", 3, ttl=86400)                  # gone after a day
db.get_keys(prefix="tlg:123:")                     # for resuming
db.raw                                             # the sqlite3.Connection
db.close()
```

## API

`kv.open(path, *, table="default") -> KV`. The folder and the file are created if missing. A `KV` works with or without `with`, like `sqlite3.connect`.

| `KV` member | Does |
| --- | --- |
| `kv[key]`, `kv[key] = value`, `del kv[key]`, `key in kv`, `len(kv)`, `iter(kv)` | a `MutableMapping[str, Any]` |
| `get(key, default=None)` | the value, or `default` when missing or expired |
| `set(key, value, *, ttl=None)` | like `kv[key] = value`, with an optional expiry in seconds |
| `get_keys(prefix="") -> list[str]` | a snapshot of the keys, filtered by prefix with an indexed range query. The mapping's own `keys()` keeps its standard meaning |
| `raw` | the `sqlite3.Connection`, for anything else |
| `close()` | closes the connection; later use raises `RuntimeError` |

- Keys are `str`.
- Values are anything `json.dumps` accepts; anything else raises `TypeError` when set.
- An expired key reads as missing, and is deleted on that read. Opening a table also sweeps out expired rows.
- **Threads.** Writes from many threads are safe: one connection behind a lock, in WAL mode. There is no batch API. The review found that the old `store.batch()` deadlocked when other threads wrote, and the engine's writes are single rows anyway.
- **Secrets.** `kv` stores plain JSON. Encrypted values are the [engine's](engine.md#secrets) job, using `crypto` on top of a `kv` table.

The SQL layout is one table, `kv(tbl, key, value, expires)`, with the primary key `(tbl, key)`.

## Tests

- **Mapping:** the full mapping protocol; `collections.abc.MutableMapping` checks pass.
- **Values:** a JSON round trip; `TypeError` for a value that isn't JSON.
- **Expiry:** `ttl=1` expires with a fake clock; the sweep on open; `get` returns `default` once expired.
- **Prefix:** results are correct, and the query plan uses the index.
- **Tables:** two tables in one file don't see each other's keys.
- **Threads:** 8 threads × 500 writes, with no lost row and no `database is locked`.
- **Lifecycle:** reopening keeps the data; use after `close()` raises.
