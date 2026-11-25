"""Level 3 (`--volume`) engine: shrink + raw tail stash / expand + restore."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any

from flnh import errors, pack, state
from flnh.volume import disk as vol_disk
from flnh.volume import table

VOL_MAGIC = b"FLNHVOL2"
VOL_HDR_FMT = ">8s32sI"
VOL_HDR_SIZE = struct.calcsize(VOL_HDR_FMT)
STREAM_CHUNK = 1024 * 1024


def _pad_sectors(data: bytes, sector_size: int) -> bytes:
    rem = len(data) % sector_size
    return data if rem == 0 else data + b"\x00" * (sector_size - rem)


def _encode_meta(payload: dict[str, Any], sector_size: int) -> bytes:
    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(json_bytes).digest()
    hdr = struct.pack(VOL_HDR_FMT, VOL_MAGIC, digest, len(json_bytes))
    return _pad_sectors(hdr + json_bytes, sector_size)


def _decode_meta(raw: bytes) -> dict[str, Any]:
    if len(raw) < VOL_HDR_SIZE or raw[:8] != VOL_MAGIC:
        raise errors.CorruptHeaderError("missing or invalid volume tail header")
    magic, digest, json_len = struct.unpack_from(VOL_HDR_FMT, raw, 0)
    if magic != VOL_MAGIC or json_len == 0 or VOL_HDR_SIZE + json_len > len(raw):
        raise errors.CorruptHeaderError("corrupt volume tail header geometry")
    json_bytes = raw[VOL_HDR_SIZE : VOL_HDR_SIZE + json_len]
    if not hmac.compare_digest(hashlib.sha256(json_bytes).digest(), digest):
        raise errors.CorruptHeaderError("volume tail header checksum mismatch")
    try:
        return dict(json.loads(json_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise errors.CorruptHeaderError("corrupt volume tail metadata payload") from exc


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


def _stream_file_to_sectors(d: vol_disk.Disk, src_file: Path, start_lba: int) -> None:
    sector_size = d.sector_size
    chunk_bytes = max(sector_size, (STREAM_CHUNK // sector_size) * sector_size)
    lba = start_lba
    with open(src_file, "rb") as f:
        while True:
            chunk = f.read(chunk_bytes)
            if not chunk:
                break
            padded = _pad_sectors(chunk, sector_size)
            d.write_sectors(lba, padded)
            lba += len(padded) // sector_size


def _hash_sectors(d: vol_disk.Disk, start_lba: int, byte_size: int) -> str:
    sector_size = d.sector_size
    chunk_sectors = max(1, STREAM_CHUNK // sector_size)
    hasher = hashlib.sha256()
    remaining = byte_size
    lba = start_lba
    while remaining > 0:
        need_sectors = (remaining + sector_size - 1) // sector_size
        take_sectors = min(chunk_sectors, need_sectors)
        raw = d.read_sectors(lba, take_sectors)
        take_bytes = min(len(raw), remaining)
        hasher.update(raw[:take_bytes])
        remaining -= take_bytes
        lba += take_sectors
    return hasher.hexdigest()


def _stream_sectors_to_file(
    d: vol_disk.Disk,
    start_lba: int,
    byte_size: int,
    dest_file: Path,
) -> str:
    sector_size = d.sector_size
    chunk_sectors = max(1, STREAM_CHUNK // sector_size)
    hasher = hashlib.sha256()
    remaining = byte_size
    lba = start_lba
    with open(dest_file, "wb") as out:
        while remaining > 0:
            need_sectors = (remaining + sector_size - 1) // sector_size
            take_sectors = min(chunk_sectors, need_sectors)
            raw = d.read_sectors(lba, take_sectors)
            take_bytes = min(len(raw), remaining)
            piece = raw[:take_bytes]
            hasher.update(piece)
            out.write(piece)
            remaining -= take_bytes
            lba += take_sectors
    return hasher.hexdigest()


def hide(
    path: Path,
    *,
    disk: vol_disk.Disk | None = None,
    part_index: int | None = None,
    backup_path: Path | None = None,
    align_bytes: int | None = None,
) -> dict[str, Any]:
    """Pack `path`, shrink the volume, and stash it behind the partition boundary."""
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
                hint="use flnh hide or --lock on OS or system drives",
            )
        vol_disk.check_filesystem(d.fs_type)
        scheme, parts, raw_table = table.read_table(d)
        idx = part_index if part_index is not None else getattr(d, "part_index", 1)
        part = _target_partition(parts, idx)
        if scheme == "gpt" and part.type_id.upper() in table.SYSTEM_GPT_GUIDS:
            raise errors.SafetyError(f"refusing protected system partition ({part.type_id})")

        orig_size = pack.tree_size(path)
        sector_size = d.sector_size

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_tar = Path(tmp_dir) / "data.tar"
            archive_size, archive_sha256, kind = pack.pack_to_file(path, tmp_tar)
            archive_sectors = (archive_size + sector_size - 1) // sector_size

            prelim_meta = {
                "orig_path": state.norm_path(path),
                "kind": kind,
                "archive_size": archive_size,
                "archive_sha256": archive_sha256,
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
                "orig_gpt_entries_b64": base64.b64encode(
                    raw_table.get("entries", b"")
                ).decode("ascii"),
            }
            est_meta = _encode_meta(prelim_meta, sector_size)
            meta_sectors = (len(est_meta) // sector_size) + 1
            needed_sectors = archive_sectors + meta_sectors
            if align_bytes is None:
                align_bytes = sector_size if isinstance(d, vol_disk.FileDisk) else 1024 * 1024
            align_sectors = max(1, align_bytes // sector_size)
            shrink_sectors = (
                (needed_sectors + align_sectors - 1) // align_sectors
            ) * align_sectors

            avail = d.max_shrink_bytes(part.index)
            if shrink_sectors * sector_size > avail:
                raise errors.DiskError(
                    f"not enough free shrinkable space ({avail} bytes free, "
                    f"{shrink_sectors * sector_size} bytes needed)",
                    hint="free space on the drive or use flnh hide / --lock",
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
            meta_blob = _encode_meta(prelim_meta, sector_size)
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

            try:
                _stream_file_to_sectors(d, tmp_tar, tail_lba)
                d.write_sectors(meta_lba, meta_blob)
                if (
                    _hash_sectors(d, tail_lba, archive_size) != archive_sha256
                    or d.read_sectors(meta_lba, meta_sectors) != meta_blob
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
        pack.delete_plaintext(path)

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
                "archive_size": archive_size,
                "archive_sha256": archive_sha256,
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
            _magic, _digest, json_len = struct.unpack_from(VOL_HDR_FMT, sec, 0)
            total_bytes = VOL_HDR_SIZE + json_len
            sectors = (total_bytes + d.sector_size - 1) // d.sector_size
            if lba + sectors - 1 <= limit:
                return lba, sectors
    raise errors.CorruptHeaderError(
        "no hidden volume tail found on disk",
        hint="pass --backup <file> if you have a saved partition backup",
    )


def show(
    orig_path: Path,
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
        meta = _decode_meta(raw_meta)

        archive_size = int(meta["archive_size"])
        archive_sha256 = str(meta["archive_sha256"])
        tail_lba = int(meta["tail_lba"])
        kind = str(meta["kind"])

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_tar = Path(tmp_dir) / "restore.tar"
            got_sha = _stream_sectors_to_file(d, tail_lba, archive_size, tmp_tar)
            if got_sha != archive_sha256:
                raise errors.CorruptHeaderError("hidden archive checksum mismatch in disk tail")

            scheme = str(meta["scheme"])
            orig_sectors = int(meta["orig_sector_count"])
            if scheme == "mbr":
                orig_mbr = base64.b64decode((backup_data or meta)["orig_mbr_b64"])
                _write_table_mbr(d, orig_mbr)
            else:
                _cur_scheme, _parts, cur_raw = table.read_table(d)
                orig_pri = base64.b64decode((backup_data or meta)["orig_gpt_hdr_b64"])
                orig_entries = base64.b64decode((backup_data or meta)["orig_gpt_entries_b64"])
                info = cur_raw["info"]
                alt_lba = int(info["alternate_lba"])
                hdr_size = struct.unpack_from("<I", orig_pri, 12)[0]

                bak = bytearray(orig_pri)
                struct.pack_into("<QQ", bak, 24, alt_lba, 1)
                struct.pack_into("<Q", bak, 72, alt_lba - int(info["entry_sectors"]))
                struct.pack_into("<I", bak, 16, 0)
                struct.pack_into("<I", bak, 16, zlib.crc32(bak[:hdr_size]) & 0xFFFFFFFF)
                _write_table_gpt(d, info, orig_pri, orig_entries, bytes(bak))

            d.refresh()
            d.expand_fs(int(meta["part_index"]), orig_sectors * d.sector_size)
            pack.unpack_from_file(tmp_tar, orig_path, kind=kind)

        d.write_sectors(meta_lba, b"\x00" * (meta_sectors * d.sector_size))
        return orig_path
    finally:
        if owns_disk:
            d.close()
