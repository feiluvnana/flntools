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

The partition is the one the OS reports for `path` (Linux sysfs, `diskutil`, Windows device
numbers), and its table entry must match the OS's start and size. Symlinks are refused as the
target; symlinks inside a folder come back as they were, wherever they point.

1. Shrinks the filesystem and its partition (`Resize-Partition` on Windows and
   `diskutil apfs resizeContainer` on macOS change the table themselves; on Linux `resize2fs`
   shrinks ext4 and `flnh` updates the MBR or GPT, recomputing both GPT CRC32s, then
   `partx -u` tells the kernel while the volume stays mounted).
2. Streams `path` as an uncompressed PAX tar (`1 MiB` buffer) into the freed tail sectors while
   computing SHA-256, then writes the `FLNHVOL2` checksummed tail header (`8s32sI` + JSON).
3. Read-back verifies the archive's SHA-256 and the header bytes; any failure grows the volume
   back.
4. Records the item, then removes plaintext. Files it cannot remove are reported, and the item
   still restores.
5. `flnh show` verifies the tail archive, then:
   - with room on the shrunk volume, extracts it, zero-wipes the header and grows the volume;
   - without, copies it to a temp folder, checks every entry, zero-wipes the header, grows the
     volume (ext4 online, still mounted) and extracts. From the wipe on, any failure or Ctrl-C
     keeps the copy and reports its path; if the volume does not grow, nothing is extracted.
   Restored files get their original owners back when run as root. When growing the volume fails
   after the header is wiped, the item is forgotten and the error says to grow it by hand.

After a run as root through `sudo`, the state folder is handed back to the `sudo` user, but only
when it is a real folder (not a symlink) inside that user's home.

## Honest limits

- `flnh` does not encrypt files or prompt for passwords; anyone with raw disk access or Admin/root
  privileges can inspect locked items or unallocated tail sectors.
- OS recent-file indexes (Quick Access, Spotlight, shell history) and filesystem journals may
  retain paths accessed while visible.
- `--volume` only supports in-place shrinkable filesystems (`ntfs`, `ext4`, `apfs`).
