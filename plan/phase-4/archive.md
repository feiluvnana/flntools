# archive

Phase 4, step 2. `shared/_archive.py` is rewritten as two functions, `zip` and `unzip`. Plain zip and tar use the stdlib. Password-protected zips and 7z need the `archive` extra (`pyzipper`, `py7zr`). RAR can be read, and only when the `unrar` program is installed.

```python
archive.zip(sources, "out/photos.zip")                                # the format comes from the extension
archive.zip(sources, "out/photos.7z", password="pw")
archive.zip(sources, "out/photos.zip", split=2_000_000_000)           # parts: photos.zip.001, .002, …
archive.unzip("in/a.7z", "out/")                                      # the contents land directly in out/
archive.unzip(["in/a.7z.001", "in/a.7z.002"], "out/", password="pw")
archive.unzip("in/a.zip", "out/", conflict="rename")                  # a.jpg exists, so it becomes a (1).jpg
```

## zip

`zip(sources, dest, *, password=None, split=None) -> list[Path]`

- `sources` is a path or a list of paths. A folder is added with everything inside it. Names in the archive are relative to each source's parent.
- The format comes from `dest`'s extension: `.zip`, `.7z`, `.tar`, `.tar.gz`, `.tar.xz`. Any other extension raises `ValueError`.
- `password` works for `.zip` (AES, through `pyzipper`) and `.7z`. A password on tar raises `ValueError`.
- `split` (in bytes) writes parts in the plain `.001`, `.002` style, which 7-Zip, `unzip`, and this module all read.
- Everything is written to temp names and renamed at the end, so a failed `zip` leaves nothing behind.
- It returns the written files: one archive, or the parts in order.

## unzip

`unzip(parts, dest, *, password=None, conflict="error", limits=Limits()) -> list[Path]`

- `parts` is one archive, or all the parts of one, in any of three styles: `.001`/`.002`; `.z01`/…/`.zip`; `.7z.001`/…. A single path to a first part finds its siblings.
- The format is sniffed from the **magic bytes** of the joined data, never from the name. A mirror often serves a 7z under a `.zip` name.
- Everything inside lands directly in `dest`, which is created if missing. To keep the contents together, create a folder and pass it as `dest`.
- `password` works for zip (ZipCrypto and AES) and 7z. A missing or wrong password raises `PasswordError`.
- RAR needs the `unrar` program. Without it, `RuntimeError` says how to install it.
- It returns the files written.

## Conflicts

`conflict=` decides what happens when a file already exists in `dest`.

| Value | Behavior |
| --- | --- |
| `"error"` (the default) | nothing is written. `ConflictError` lists the clashing names |
| `"skip"` | existing files are kept, and the archive's copies are not written |
| `"overwrite"` | the archive's copies replace the existing files |
| `"rename"` | the archive's copy is written as `name (1).ext`, `name (2).ext`, and so on |

Two entries whose names differ only in case (`A.jpg` and `a.jpg`) count as a conflict too, because many disks can't hold both.

## Safety

Every check below runs on the archive's directory **before anything is written**, so a refused archive leaves `dest` untouched.

| Threat | Rule | Error |
| --- | --- | --- |
| path traversal: `../x`, `/etc/x`, `C:\x`, `\\server\x` | refused | `UnsafeError` |
| a symlink or hard link inside the archive | refused, always. Links are never created, even pointing inside `dest`, because a chain of them can escape. The review reproduced exactly this escape on Python 3.10 and 3.11 | `UnsafeError` |
| device files, pipes, sockets | refused | `UnsafeError` |
| total uncompressed size over `limits.size` (default 20 GB) | refused | `BombError` |
| any entry expanding more than `limits.ratio` times (default 200) | refused | `BombError` |
| more than `limits.files` entries (default 100,000) | refused | `BombError` |
| declared sizes that lie | the size and ratio limits are also counted **while extracting**. An entry that writes more than it declared is stopped, and its partial file is removed | `BombError` |
| nested archives | never extracted automatically | — |

`Limits(size=, ratio=, files=)` adjusts the limits for archives you trust. All four errors (`UnsafeError`, `BombError`, `ConflictError`, `PasswordError`) are `ValueError`s.

## Tests

- **Round trips:** each format, with and without a password where supported; split into 3 parts and joined back.
- **Format sniffing:** a 7z named `.zip`; zip, 7z and tar magic bytes; all three part styles, found from the first part.
- **Conflicts:** each `conflict=` value, plus a case-only clash.
- **Safety:**
  - every row of the safety table, with nothing written;
  - a symlink chain (`a/b -> ..`, `a/b/c -> ..`, `a/b/c/x`) refused on every Python version;
  - a crafted entry whose real size exceeds its declared size is stopped mid-way and cleaned up.
- **Errors:**
  - a wrong password raises `PasswordError`;
  - RAR without `unrar` gives the install message;
  - a failed `zip` leaves no file.
