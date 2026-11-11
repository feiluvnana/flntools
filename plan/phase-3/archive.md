# archive

Phase 3. Depends on [path](path.md). Stdlib only (`zipfile`, `tarfile`).

[`shared/_archive.py`](../../shared/_archive.py). Pack and unpack with one call. The format comes from the file name.

```python
archive.pack(".flnd/downloads/ins/example_user", "example_user.zip")
archive.pack(["a.txt", "photos/"], "bundle.tar.gz")
archive.unpack("bundle.tar.gz", "out/")
archive.names("bundle.zip")  # ["a.txt", "photos/1.jpg", ...]
```

| Suffix | Format |
| --- | --- |
| `.zip` | zip, deflated |
| `.tar` | tar |
| `.tar.gz`, `.tgz` | tar + gzip |
| `.tar.bz2` | tar + bzip2 |
| `.tar.xz` | tar + xz |

Any other suffix raises `ValueError`.

## Functions

`pack(sources, dest, *, root=None) -> Path` takes one path or a list of paths.

- A directory is added recursively.
- Names inside the archive are relative to `root`. When `root` is not given, it defaults to the parent of a single source, or to the current directory for a list.
- The archive is written with `path.atomic`, so a failed pack leaves no half-written file.

`unpack(src, dest_dir) -> list[Path]` extracts every entry and returns the extracted paths. It refuses any entry that would land outside `dest_dir`: absolute paths, `..`, and links that point outside. It raises `archive.UnsafeError` before writing anything. On Python 3.12+ tar files use `filter="data"`. On 3.10 and 3.11 the same check is done by hand.

`names(src) -> list[str]` lists the entries without extracting them.

flnd does not use `archive` yet.

## Tests

- A round trip for each format: `pack` a directory, then `unpack` it and compare the files.
- `root` handling for a single source and for a list.
- An unknown suffix raises `ValueError`.
- `UnsafeError` for `../evil`, `/abs`, and a symlink pointing outside, in both zip and tar, with nothing written.
