# flnh specification

`flnh` is a cross-platform CLI tool for hiding folders and files at three progressive,
zero-encryption levels while remaining cleanly reversible through `flnh show`.

## CLI and exit codes

Only two commands exist: `flnh hide` and `flnh show`.

- `flnh hide <path>`:
  - Default (no mode flag): Level 1 (`flag`, instant vanish).
  - `--lock`: Level 2 (`lock`, vanish + ACL/`chmod` lock).
  - `--volume`: Level 3 (`volume`, shrink partition and stash behind partition boundary).
  - `-y`, `--yes`: confirm `--volume` resize non-interactively.
- `flnh show [path]`:
  - With `path`: auto-detects level and restores `path`.
  - Without `path`: opens a `flnkit.ui.forms.select` picker in a terminal, or prints the table
    off a terminal.
  - `--list`: prints tracked hidden items and exits `0`.
  - `--backup FILE`: recover a `--volume` from a saved partition backup.

Exit codes: `0` success, `1` operational error (`BusyError`, `CorruptHeaderError`,
`UnsupportedError`), `2` `UsageError`, `4` `DiskError` / `PermissionError` / `SafetyError` /
unexpected error, `130` `Stopped` (Ctrl-C / Esc).

## Local state

Tracked items are stored in `$FLNH_HOME/state.db` (default `%APPDATA%\flnh\state.db` on Windows,
`$XDG_CONFIG_HOME/flnh/state.db` or `~/.config/flnh/state.db` elsewhere) via `flnkit.db.Db`
with `0700`/`0600` permissions on Unix. `--volume` partition backups go to `<home>/backups/`.

## Levels 1 and 2

- **Level 1 (`flag`)**:
  - Windows: renames folder to `Control Panel.<id12>.{21EC2020-3AEA-1069-A2DD-08002B30309D}`
    and sets `+h +s` (`FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM`). Single files are wrapped
    inside that CLSID folder alongside `.flnh.meta`.
  - macOS: sets `UF_HIDDEN` via `os.chflags`.
  - Linux: renames to leading `.` and adds the name to `<parent>/.hidden`.
- **Level 2 (`lock`)**: applies Level 1 plus access denial:
  - Windows: saves original NTFS DACL SDDL and applies `D:P(D;OICI;0x1000A9;;;WD)(A;;WDRC;;;OW)`.
  - macOS: saves original mode/flags, applies `chmod 000` and `UF_IMMUTABLE`.
  - Linux: saves original mode and applies `chmod 000`.

## Level 3 (`--volume`)

Requires Admin/root, a non-OS/non-boot/non-swap disk, and `ntfs`, `ext4`, or `apfs` (FAT32/exFAT
are refused with `UnsupportedError` suggesting `flnh hide` or `--lock`). When run interactively as a
standard user, `--volume` (and `flnh show` on a volume) prompts for OS elevation (UAC on Windows,
`sudo` on macOS/Linux); non-interactive runs without privileges fail with `PermissionError`.

1. Streams `path` into an uncompressed PAX tar archive (`1 MiB` buffer) while computing SHA-256.
2. Shrinks the filesystem (`FSCTL_SHRINK_VOLUME`, `resize2fs`, or `diskutil apfs resizeContainer`).
3. Streams the archive and `FLNHVOL2` checksummed tail header (`8s32sI` + JSON) into the freed
   tail sectors before any backup GPT header, then read-back verifies SHA-256 and header bytes.
4. Updates MBR or GPT (recomputing both primary and backup GPT CRC32s) and refreshes the disk.
5. Removes plaintext only after all writes verify.
6. `flnh show` streams and SHA-256 verifies the tail archive before restoring the partition table,
   expanding the filesystem, extracting the archive, and zero-wiping the `FLNHVOL2` header.

## Honest limits

- `flnh` does not encrypt files or prompt for passwords; anyone with raw disk access or Admin/root
  privileges can inspect locked items or unallocated tail sectors.
- OS recent-file indexes (Quick Access, Spotlight, shell history) and filesystem journals may
  retain paths accessed while visible.
- `--volume` only supports in-place shrinkable filesystems (`ntfs`, `ext4`, `apfs`).
