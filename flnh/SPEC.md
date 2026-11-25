# flnh specification

`flnh` is a cross-platform CLI tool for hiding folders and files at four progressive levels
while remaining cleanly reversible through `flnh show`.

## CLI and exit codes

Only two commands exist: `flnh hide` and `flnh show`.

- `flnh hide <path>`:
  - Default (no mode flag): Level 1 (`flag`, instant vanish).
  - `--lock`: Level 2 (`lock`, vanish + ACL/`chmod` lock).
  - `--vault`: Level 3 (`vault`, vanish into a password-encrypted vault).
  - `--volume`: Level 4 (`volume`, encrypted vault + hide size from drive space).
  - `-y`, `--yes`: confirm `--volume` resize non-interactively.
  - Passwords for `--vault` / `--volume` are read via masked prompt in a terminal, or one line
    from stdin when piped.
- `flnh show [path]`:
  - With `path`: auto-detects level and restores `path`.
  - Without `path`: opens a `flnkit.ui.forms.select` picker in a terminal, or prints the table
    off a terminal.
  - `--list`: prints tracked hidden items and exits `0`.
  - `--backup FILE`: recover a `--volume` from a saved partition backup.

Exit codes: `0` success, `1` operational error (`BusyError`, `CorruptHeaderError`,
`UnsupportedError`), `2` `UsageError`, `3` `AuthError`, `4` `DiskError` / `PermissionError` /
`SafetyError` / unexpected error, `130` `Stopped` (Ctrl-C / Esc).

## Local state

Tracked items are stored in `$FLNH_HOME/state.db` (default `%APPDATA%\flnh\state.db` on Windows,
`$XDG_CONFIG_HOME/flnh/state.db` or `~/.config/flnh/state.db` elsewhere) via `flnkit.db.Db`
with `0700`/`0600` permissions on Unix. Level 4 partition backups go to `<home>/backups/`.

## Levels 1 and 2

- **Level 1 (`flag`)**:
  - Windows: renames folder to `Control Panel.<id12>.{21EC2020-3AEA-1069-A2DD-08002B30309D}`
    and sets `+h +s` (`FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM`). Single files are wrapped
    inside that CLSID folder alongside `.flnh.meta`.
  - macOS: sets `UF_HIDDEN` via `os.chflags`.
  - Linux: renames to leading `.` and adds the name to `<parent>/.hidden`.
- **Level 2 (`lock`)**: applies Level 1 plus access denial:
  - Windows: saves original NTFS DACL SDDL and applies `D:P(D;OICI;0x1000A9;;;WD)(A;;WDACRC;;;OW)`.
  - macOS: saves original mode/flags, applies `chmod 000` and `UF_IMMUTABLE`.
  - Linux: saves original mode and applies `chmod 000`.
  - Optional per-item password hash is verified before unlocking when present.

## Level 3 (`vault`)

Streams a PAX tar archive through `FLNHV1` chunked AES-256-GCM (key derived with scrypt
`N=2^15, r=8, p=1`):
- Header (56 B): `FLNHV1\x00\x01`, `flags` (`1`=single file, `2`=keyfile), `n_log2`, `r`, `p`,
  32-byte `salt`, 8-byte `nonce_prefix`, 4-byte BE `chunk_size`.
- Frames: `ct_len (u32 BE) || last (u8: 0|1) || ciphertext+tag`.
- Nonce: `nonce_prefix || index (u32 BE)`; AAD: `header || index (u32 BE) || last (u8)`.
- Chunk 0 authentication failure raises `AuthError`; later corruption/truncation/trailing bytes
  raise `CorruptHeaderError`.
- Plaintext is deleted only after `pack_to_file` lands atomically and `verify_vault` passes.

## Level 4 (`volume`)

Requires Admin/root, a non-OS/non-boot/non-swap disk, and `ntfs`, `ext4`, or `apfs` (FAT32/exFAT
are refused with `UnsupportedError` suggesting `--vault`).

1. Packs and verifies the encrypted `FLNHV1` vault.
2. Shrinks the filesystem (`FSCTL_SHRINK_VOLUME`, `resize2fs`, or `diskutil apfs resizeContainer`).
3. Writes the vault and `FLNHVOL1` encrypted tail header (`8s32s12sI` + AES-256-GCM JSON) inside
   the freed sectors before any backup GPT header, then read-back verifies every sector.
4. Updates MBR or GPT (recomputing both primary and backup GPT CRC32s) and refreshes the disk.
5. Removes plaintext only after all writes verify.
6. `flnh show` verifies the tail vault before restoring the partition table, expanding the
   filesystem, extracting the vault, and zero-wiping the `FLNHVOL1` header sectors.

## Honest limits

- OS recent-file indexes (Quick Access, Spotlight, shell history) and filesystem journals may
  retain paths accessed while visible.
- Level 4 only supports in-place shrinkable filesystems (`ntfs`, `ext4`, `apfs`).
