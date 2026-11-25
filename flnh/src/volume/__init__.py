"""Level 4 (`volume`) engine: shrink + stash in hidden tail / expand + restore."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from flnh import errors, state, vault
from flnh.volume import disk as vol_disk
from flnh.volume import table

VOL_MAGIC = b"FLNHVOL1"
VOL_HDR_FMT = ">8s32s12sI"
VOL_HDR_SIZE = struct.calcsize(VOL_HDR_FMT)


def _pad_sectors(data: bytes, sector_size: int) -> bytes:
    rem = len(data) % sector_size
    return data if rem == 0 else data + b"\x00" * (sector_size - rem)


def _encode_meta(payload: dict[str, Any], secret: bytes, sector_size: int) -> bytes:
    salt = os.urandom(32)
    nonce = os.urandom(12)
    key = vault.derive_key(secret, salt)
    plain = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, plain, VOL_MAGIC + salt)
    hdr = struct.pack(VOL_HDR_FMT, VOL_MAGIC, salt, nonce, len(ct))
    return _pad_sectors(hdr + ct, sector_size)


def _decode_meta(raw: bytes, secret: bytes) -> dict[str, Any]:
    if len(raw) < VOL_HDR_SIZE or raw[:8] != VOL_MAGIC:
        raise errors.CorruptHeaderError("missing or invalid level 4 tail header")
    magic, salt, nonce, ct_len = struct.unpack_from(VOL_HDR_FMT, raw, 0)
    if magic != VOL_MAGIC or ct_len < 16 or VOL_HDR_SIZE + ct_len > len(raw):
        raise errors.CorruptHeaderError("corrupt level 4 tail header geometry")
    ct = raw[VOL_HDR_SIZE : VOL_HDR_SIZE + ct_len]
    key = vault.derive_key(secret, salt)
    try:
        plain = AESGCM(key).decrypt(nonce, ct, VOL_MAGIC + salt)
    except InvalidTag as exc:
        raise errors.AuthError(
            "wrong password",
            hint="check the password and try again",
        ) from exc
    try:
        return dict(json.loads(plain.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.CorruptHeaderError("corrupt level 4 tail metadata payload") from exc


def _target_partition(parts: list[table.Partition], part_index: int) -> table.Partition:
    for part in parts:
        if part.index == part_index:
            return part
    raise errors.CorruptHeaderError(f"partition {part_index} not found on disk")


def _write_table_mbr(d: vol_disk.Disk, mbr: bytes) -> None:
    d.write_sectors(0, mbr)
    if d.read_sectors(0, 1) != mbr:
        raise errors.DiskError("MBR read-back verification failed")


def _write_table_gpt(
    d: vol_disk.Disk,
    info: dict[str, Any],
    pri_hdr: bytes,
    entries: bytes,
    bak_hdr: bytes,
) -> None:
    pri_entry_lba = int(info["part_entry_lba"])
    alt_lba = int(info["alternate_lba"])
    entry_sectors = int(info["entry_sectors"])
    bak_entry_lba = alt_lba - entry_sectors
    d.write_sectors(pri_entry_lba, entries)
    d.write_sectors(1, pri_hdr)
    d.write_sectors(bak_entry_lba, entries)
    d.write_sectors(alt_lba, bak_hdr)
    if d.read_sectors(1, 1) != pri_hdr or d.read_sectors(alt_lba, 1) != bak_hdr:
        raise errors.DiskError("GPT header read-back verification failed")
    table.read_table(d)


def hide(
    path: Path,
    secret: bytes,
    *,
    disk: vol_disk.Disk | None = None,
    part_index: int | None = None,
    is_keyfile: bool = False,
    backup_path: Path | None = None,
    align_bytes: int | None = None,
) -> dict[str, Any]:
    """Pack `path` into an encrypted vault, shrink the volume, and stash in the tail."""
    if not path.exists():
        raise errors.UsageError(f"path not found: {path}")
    if path.parent == path:
        raise errors.SafetyError(f"refusing to hide root path: {path}")

    owns_disk = disk is None
    d = vol_disk.open_disk(path) if disk is None else disk
    try:
        if d.is_system_disk():
            raise errors.SafetyError(
                f"refusing OS/boot/swap disk: {d.system_disk_reason()}",
                hint="use --vault on OS or system drives",
            )
        vol_disk.check_filesystem(d.fs_type)
        scheme, parts, raw_table = table.read_table(d)
        idx = part_index if part_index is not None else getattr(d, "part_index", 1)
        part = _target_partition(parts, idx)
        if scheme == "gpt" and part.type_id.upper() in table.SYSTEM_GPT_GUIDS:
            raise errors.SafetyError(f"refusing protected system partition ({part.type_id})")

        orig_size = vault.tree_size(path)
        kind = "file" if path.is_file() and not path.is_symlink() else "dir"

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_vault = Path(tmp_dir) / "data.flnh"
            vault_size, vault_sha256 = vault.pack_to_file(
                path, tmp_vault, secret, is_keyfile=is_keyfile
            )
            vault_raw = tmp_vault.read_bytes()

        sector_size = d.sector_size
        vault_sectors = (vault_size + sector_size - 1) // sector_size

        prelim_meta = {
            "orig_path": state.norm_path(path),
            "kind": kind,
            "vault_size": vault_size,
            "vault_sha256": vault_sha256,
            "part_index": part.index,
            "scheme": scheme,
            "orig_sector_count": part.sector_count,
            "new_sector_count": 0,
            "tail_lba": 0,
            "meta_lba": 0,
            "meta_sectors": 0,
            "tail_sectors": 0,
            "orig_mbr_b64": base64.b64encode(raw_table["mbr"]).decode("ascii"),
            "orig_gpt_hdr_b64": base64.b64encode(raw_table.get("hdr", b"")).decode("ascii"),
            "orig_gpt_entries_b64": base64.b64encode(raw_table.get("entries", b"")).decode("ascii"),
        }
        # Measure conservative meta size first, then finalize exact coordinates.
        est_meta = _encode_meta(prelim_meta, secret, sector_size)
        meta_sectors = (len(est_meta) // sector_size) + 1
        needed_sectors = vault_sectors + meta_sectors
        if align_bytes is None:
            align_bytes = sector_size if isinstance(d, vol_disk.FileDisk) else 1024 * 1024
        align_sectors = max(1, align_bytes // sector_size)
        shrink_sectors = ((needed_sectors + align_sectors - 1) // align_sectors) * align_sectors

        avail = d.max_shrink_bytes(part.index)
        if shrink_sectors * sector_size > avail:
            raise errors.DiskError(
                f"not enough free shrinkable space ({avail} bytes free, "
                f"{shrink_sectors * sector_size} bytes needed)",
                hint="free space on the drive or use --vault",
            )
        new_sector_count = part.sector_count - shrink_sectors
        if new_sector_count <= 0:
            raise errors.DiskError("cannot shrink partition below 1 sector")

        tail_lba = part.start_lba + new_sector_count
        meta_lba = tail_lba + shrink_sectors - meta_sectors

        prelim_meta["new_sector_count"] = new_sector_count
        prelim_meta["tail_lba"] = tail_lba
        prelim_meta["meta_lba"] = meta_lba
        prelim_meta["meta_sectors"] = meta_sectors
        prelim_meta["tail_sectors"] = shrink_sectors
        meta_blob = _encode_meta(prelim_meta, secret, sector_size)
        if len(meta_blob) > meta_sectors * sector_size:
            raise errors.DiskError("tail metadata exceeded reserved sectors")
        meta_blob = meta_blob.ljust(meta_sectors * sector_size, b"\x00")

        backup_payload = {
            "orig_path": prelim_meta["orig_path"],
            "device_id": d.device_id,
            "scheme": scheme,
            "part_index": part.index,
            "orig_sector_count": part.sector_count,
            "new_sector_count": new_sector_count,
            "tail_lba": tail_lba,
            "meta_lba": meta_lba,
            "meta_sectors": meta_sectors,
            "orig_mbr_b64": prelim_meta["orig_mbr_b64"],
            "orig_gpt_hdr_b64": prelim_meta["orig_gpt_hdr_b64"],
            "orig_gpt_entries_b64": prelim_meta["orig_gpt_entries_b64"],
        }
        if backup_path is not None:
            table.save_backup(backup_path, backup_payload)

        orig_fs_bytes = part.sector_count * sector_size
        new_fs_bytes = new_sector_count * sector_size
        d.shrink_fs(part.index, new_fs_bytes)

        vault_padded = _pad_sectors(vault_raw, sector_size)
        try:
            d.write_sectors(tail_lba, vault_padded)
            d.write_sectors(meta_lba, meta_blob)
            read_vault = d.read_sectors(tail_lba, vault_sectors)[:vault_size]
            read_meta = d.read_sectors(meta_lba, meta_sectors)
            if (
                hashlib.sha256(read_vault).hexdigest() != vault_sha256
                or read_meta != meta_blob
            ):
                raise errors.DiskError("tail sector read-back verification failed")
        except BaseException:
            try:
                d.expand_fs(part.index, orig_fs_bytes)
            except BaseException:
                pass
            raise

        if scheme == "mbr":
            new_mbr = table.update_mbr_partition(raw_table["mbr"], part.index, new_sector_count)
            _write_table_mbr(d, new_mbr)
        else:
            pri_hdr, entries_raw, bak_hdr, _ = table.update_gpt_partition(
                raw_table["hdr"],
                raw_table["entries"],
                part.index,
                new_sector_count,
                sector_size,
            )
            _write_table_gpt(d, raw_table["info"], pri_hdr, entries_raw, bak_hdr)

        d.refresh()
        vault.delete_plaintext(path)

        return {
            "kind": kind,
            "hidden_path": f"{d.device_id}@lba:{tail_lba}",
            "size_bytes": orig_size,
            "meta": {
                "device_id": d.device_id,
                "scheme": scheme,
                "part_index": part.index,
                "tail_lba": tail_lba,
                "tail_sectors": shrink_sectors,
                "meta_lba": meta_lba,
                "meta_sectors": meta_sectors,
                "vault_size": vault_size,
                "vault_sha256": vault_sha256,
                "backup_path": str(backup_path) if backup_path is not None else "",
            },
        }
    finally:
        if owns_disk:
            d.close()


def _scan_tail_meta(d: vol_disk.Disk, part_index: int) -> tuple[int, int]:
    """Scan unallocated sectors after `part_index` for `VOL_MAGIC`."""
    scheme, parts, raw_table = table.read_table(d)
    part = _target_partition(parts, part_index)
    start = part.end_lba + 1
    if scheme == "gpt":
        limit = int(raw_table["info"]["last_usable_lba"])
    else:
        limit = d.total_sectors - 1
    for lba in range(limit, start - 1, -1):
        sec = d.read_sectors(lba, 1)
        if sec[:8] == VOL_MAGIC:
            _magic, _salt, _nonce, ct_len = struct.unpack_from(VOL_HDR_FMT, sec, 0)
            total_bytes = VOL_HDR_SIZE + ct_len
            sectors = (total_bytes + d.sector_size - 1) // d.sector_size
            if lba + sectors - 1 <= limit:
                return lba, sectors
    raise errors.CorruptHeaderError(
        "no level 4 hidden tail found on disk",
        hint="pass --backup <file> if you have a saved partition backup",
    )


def show(
    orig_path: Path,
    secret: bytes,
    *,
    disk: vol_disk.Disk | None = None,
    entry: Any = None,
    backup_path: Path | None = None,
    part_index: int | None = None,
) -> Path:
    """Verify hidden tail, restore partition table + filesystem, and unpack `orig_path`."""
    if orig_path.exists() or orig_path.is_symlink():
        raise errors.UsageError(
            f"destination already exists: {orig_path}",
            hint="move or delete the existing path before running flnh show",
        )

    owns_disk = disk is None
    d = vol_disk.open_disk(orig_path.parent) if disk is None else disk
    try:
        idx = part_index if part_index is not None else getattr(d, "part_index", 1)
        meta_lba: int | None = None
        meta_sectors: int | None = None
        backup_data: dict[str, Any] | None = None

        if backup_path is not None:
            backup_data = table.load_backup(backup_path)
            meta_lba = int(backup_data["meta_lba"])
            meta_sectors = int(backup_data["meta_sectors"])
            idx = int(backup_data["part_index"])
        elif entry is not None and "meta_lba" in entry.meta:
            meta_lba = int(entry.meta["meta_lba"])
            meta_sectors = int(entry.meta["meta_sectors"])
            idx = int(entry.meta.get("part_index", idx))
        else:
            meta_lba, meta_sectors = _scan_tail_meta(d, idx)

        raw_meta = d.read_sectors(meta_lba, meta_sectors)
        meta = _decode_meta(raw_meta, secret)

        vault_size = int(meta["vault_size"])
        vault_sha256 = str(meta["vault_sha256"])
        tail_lba = int(meta["tail_lba"])
        vault_sectors = (vault_size + d.sector_size - 1) // d.sector_size
        vault_bytes = d.read_sectors(tail_lba, vault_sectors)[:vault_size]
        if hashlib.sha256(vault_bytes).hexdigest() != vault_sha256:
            raise errors.CorruptHeaderError("hidden vault checksum mismatch in disk tail")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_vault = Path(tmp_dir) / "restore.flnh"
            tmp_vault.write_bytes(vault_bytes)
            vault.verify_vault(tmp_vault, secret)

            scheme = str(meta["scheme"])
            orig_sectors = int(meta["orig_sector_count"])
            if scheme == "mbr":
                orig_mbr = base64.b64decode(
                    (backup_data or meta)["orig_mbr_b64"]
                )
                _write_table_mbr(d, orig_mbr)
            else:
                _cur_scheme, _parts, cur_raw = table.read_table(d)
                orig_pri = base64.b64decode((backup_data or meta)["orig_gpt_hdr_b64"])
                orig_entries = base64.b64decode((backup_data or meta)["orig_gpt_entries_b64"])
                info = cur_raw["info"]
                alt_lba = int(info["alternate_lba"])
                hdr_size = struct.unpack_from("<I", orig_pri, 12)[0]
                import zlib

                bak = bytearray(orig_pri)
                struct.pack_into("<QQ", bak, 24, alt_lba, 1)
                struct.pack_into("<Q", bak, 72, alt_lba - int(info["entry_sectors"]))
                struct.pack_into("<I", bak, 16, 0)
                struct.pack_into("<I", bak, 16, zlib.crc32(bak[:hdr_size]) & 0xFFFFFFFF)
                _write_table_gpt(d, info, orig_pri, orig_entries, bytes(bak))

            d.refresh()
            d.expand_fs(int(meta["part_index"]), orig_sectors * d.sector_size)
            vault.unpack_from_file(tmp_vault, orig_path, secret)

        # Wipe tail header sectors only after successful restoration.
        d.write_sectors(meta_lba, b"\x00" * (meta_sectors * d.sector_size))
        return orig_path
    finally:
        if owns_disk:
            d.close()
