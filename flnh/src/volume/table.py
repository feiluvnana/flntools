"""Pure-Python MBR and GPT parser, writer, CRC32 validator, and backup helpers."""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flnh import errors
from flnkit import files

SECTOR_SIZE = 512
MBR_SIG = b"\x55\xaa"
GPT_SIG = b"EFI PART"
GPT_REV = 0x00010000
GPT_HDR_SIZE = 92
GPT_ENTRY_SIZE = 128

EFI_SYSTEM_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
MSFT_RESERVED_GUID = "E3C9E316-0B5C-4DB8-817D-F92DF00215AE"
MSFT_RECOVERY_GUID = "DE94BBA4-06D1-4D40-A16A-BFD50179D6AC"
BASIC_DATA_GUID = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
LINUX_FS_GUID = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
APFS_GUID = "7C3457EF-0000-11AA-AA11-00306543ECAC"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"

SYSTEM_GPT_GUIDS = frozenset({EFI_SYSTEM_GUID, MSFT_RESERVED_GUID, MSFT_RECOVERY_GUID})
MBR_SYSTEM_TYPES = frozenset({0xEF, 0x27})  # EFI System, Windows Recovery

_GPT_HDR_FMT = "<8sIIIIQQQQ16sQIII"


@dataclass(frozen=True)
class Partition:
    """One MBR or GPT partition entry (1-based index)."""

    scheme: str
    index: int
    start_lba: int
    sector_count: int
    type_id: str
    name: str = ""

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.sector_count - 1


def guid_bytes_to_str(raw: bytes) -> str:
    """Convert 16-byte Microsoft mixed-endian GUID to uppercase hyphenated string."""
    d1, d2, d3 = struct.unpack("<IHH", raw[:8])
    return f"{d1:08X}-{d2:04X}-{d3:04X}-{raw[8:10].hex().upper()}-{raw[10:16].hex().upper()}"


def str_to_guid_bytes(text: str) -> bytes:
    """Convert hyphenated GUID string to 16-byte mixed-endian bytes."""
    parts = text.split("-")
    if len(parts) != 5:
        raise ValueError(f"invalid GUID: {text!r}")
    return (
        struct.pack("<IHH", int(parts[0], 16), int(parts[1], 16), int(parts[2], 16))
        + bytes.fromhex(parts[3] + parts[4])
    )


def parse_mbr(sector0: bytes) -> list[Partition]:
    """Parse the 4 primary partition entries from MBR sector 0."""
    if len(sector0) < SECTOR_SIZE or sector0[510:512] != MBR_SIG:
        raise errors.CorruptHeaderError("invalid MBR boot signature")
    parts: list[Partition] = []
    for idx in range(4):
        off = 446 + idx * 16
        entry = sector0[off : off + 16]
        ptype = entry[4]
        start_lba, count = struct.unpack("<II", entry[8:16])
        if ptype != 0 and count > 0:
            parts.append(
                Partition(
                    scheme="mbr",
                    index=idx + 1,
                    start_lba=start_lba,
                    sector_count=count,
                    type_id=f"0x{ptype:02x}",
                )
            )
    return parts


def is_protective_mbr(sector0: bytes) -> bool:
    """Return True when MBR sector 0 contains a GPT protective (0xEE) partition."""
    if len(sector0) < SECTOR_SIZE or sector0[510:512] != MBR_SIG:
        return False
    return any(sector0[446 + i * 16 + 4] == 0xEE for i in range(4))


def update_mbr_partition(sector0: bytes, index: int, new_sector_count: int) -> bytes:
    """Return updated MBR sector 0 with `index` (1..4) resized to `new_sector_count`."""
    if not 1 <= index <= 4:
        raise errors.UsageError(f"invalid MBR partition index: {index}")
    if len(sector0) < SECTOR_SIZE or sector0[510:512] != MBR_SIG:
        raise errors.CorruptHeaderError("invalid MBR boot signature")
    if new_sector_count <= 0:
        raise errors.UsageError("new partition sector count must be positive")
    buf = bytearray(sector0)
    off = 446 + (index - 1) * 16
    if buf[off + 4] == 0:
        raise errors.CorruptHeaderError(f"MBR partition {index} is empty")
    struct.pack_into("<I", buf, off + 12, new_sector_count)
    return bytes(buf)


def _gpt_header_crc(hdr: bytes) -> int:
    hdr_size = struct.unpack_from("<I", hdr, 12)[0]
    buf = bytearray(hdr[:hdr_size])
    buf[16:20] = b"\x00\x00\x00\x00"
    return zlib.crc32(buf) & 0xFFFFFFFF


def parse_gpt(
    read_sectors: Callable[[int, int], bytes],
    sector_size: int,
    total_sectors: int,
) -> tuple[dict[str, Any], list[Partition], bytes, bytes]:
    """Parse and CRC32-validate GPT header and partition entry array."""
    hdr_sec = read_sectors(1, 1)
    if len(hdr_sec) < GPT_HDR_SIZE or hdr_sec[:8] != GPT_SIG:
        raise errors.CorruptHeaderError("invalid GPT header signature")
    (
        sig,
        rev,
        hdr_size,
        hdr_crc,
        _reserved,
        my_lba,
        alt_lba,
        first_usable,
        last_usable,
        disk_guid,
        part_entry_lba,
        num_entries,
        entry_size,
        entries_crc,
    ) = struct.unpack_from(_GPT_HDR_FMT, hdr_sec, 0)
    if sig != GPT_SIG or hdr_size < GPT_HDR_SIZE or hdr_size > sector_size:
        raise errors.CorruptHeaderError("invalid GPT header geometry")
    if _gpt_header_crc(hdr_sec) != hdr_crc:
        raise errors.CorruptHeaderError("GPT primary header CRC32 mismatch")
    if my_lba != 1 or alt_lba >= total_sectors or first_usable > last_usable:
        raise errors.CorruptHeaderError("GPT header LBA bounds are invalid")
    if entry_size != GPT_ENTRY_SIZE or num_entries == 0 or num_entries > 4096:
        raise errors.CorruptHeaderError("unsupported GPT entry layout")

    entry_bytes_len = num_entries * entry_size
    entry_sectors = (entry_bytes_len + sector_size - 1) // sector_size
    entries_raw = read_sectors(part_entry_lba, entry_sectors)
    entries_bytes = entries_raw[:entry_bytes_len]
    if (zlib.crc32(entries_bytes) & 0xFFFFFFFF) != entries_crc:
        raise errors.CorruptHeaderError("GPT partition entries CRC32 mismatch")

    parts: list[Partition] = []
    for slot in range(num_entries):
        rec = entries_bytes[slot * entry_size : (slot + 1) * entry_size]
        type_guid = guid_bytes_to_str(rec[:16])
        if type_guid == ZERO_GUID:
            continue
        start_lba, end_lba, _attrs = struct.unpack_from("<QQQ", rec, 32)
        name = rec[56:128].decode("utf-16-le", errors="replace").rstrip("\x00")
        if end_lba >= start_lba:
            parts.append(
                Partition(
                    scheme="gpt",
                    index=slot + 1,
                    start_lba=start_lba,
                    sector_count=end_lba - start_lba + 1,
                    type_id=type_guid,
                    name=name,
                )
            )

    info = {
        "revision": rev,
        "my_lba": my_lba,
        "alternate_lba": alt_lba,
        "first_usable_lba": first_usable,
        "last_usable_lba": last_usable,
        "disk_guid": guid_bytes_to_str(disk_guid),
        "part_entry_lba": part_entry_lba,
        "num_entries": num_entries,
        "entry_size": entry_size,
        "entry_sectors": entry_sectors,
    }
    return info, parts, hdr_sec, entries_raw


def update_gpt_partition(
    primary_hdr_sec: bytes,
    entries_raw: bytes,
    index: int,
    new_sector_count: int,
    sector_size: int,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Update GPT partition `index` and recompute primary & backup CRC32s.

    Returns `(new_primary_hdr, new_entries_raw, new_backup_hdr, new_entries_raw)`.
    """
    (
        _sig,
        _rev,
        hdr_size,
        _hdr_crc,
        _res,
        my_lba,
        alt_lba,
        _first_u,
        last_u,
        _guid,
        _entry_lba,
        num_entries,
        entry_size,
        _entries_crc,
    ) = struct.unpack_from(_GPT_HDR_FMT, primary_hdr_sec, 0)
    if not 1 <= index <= num_entries:
        raise errors.UsageError(f"invalid GPT partition index: {index}")
    if new_sector_count <= 0:
        raise errors.UsageError("new partition sector count must be positive")

    entry_buf = bytearray(entries_raw)
    off = (index - 1) * entry_size
    if guid_bytes_to_str(entry_buf[off : off + 16]) == ZERO_GUID:
        raise errors.CorruptHeaderError(f"GPT partition {index} is empty")
    start_lba = struct.unpack_from("<Q", entry_buf, off + 32)[0]
    new_end = start_lba + new_sector_count - 1
    if new_end > last_u:
        raise errors.SafetyError("partition end LBA would exceed GPT last usable LBA")
    struct.pack_into("<Q", entry_buf, off + 40, new_end)

    entry_bytes_len = num_entries * entry_size
    entries_crc = zlib.crc32(entry_buf[:entry_bytes_len]) & 0xFFFFFFFF

    pri = bytearray(primary_hdr_sec)
    struct.pack_into("<I", pri, 88, entries_crc)
    struct.pack_into("<I", pri, 16, 0)
    struct.pack_into("<I", pri, 16, zlib.crc32(pri[:hdr_size]) & 0xFFFFFFFF)

    entry_sectors = (entry_bytes_len + sector_size - 1) // sector_size
    bak = bytearray(pri)
    struct.pack_into("<QQ", bak, 24, alt_lba, my_lba)
    struct.pack_into("<Q", bak, 72, alt_lba - entry_sectors)
    struct.pack_into("<I", bak, 16, 0)
    struct.pack_into("<I", bak, 16, zlib.crc32(bak[:hdr_size]) & 0xFFFFFFFF)

    return bytes(pri), bytes(entry_buf), bytes(bak), bytes(entry_buf)


def read_table(disk: Any) -> tuple[str, list[Partition], dict[str, Any]]:
    """Read and validate the partition table (`'gpt'` or `'mbr'`) of `disk`."""
    s0 = disk.read_sectors(0, 1)
    if is_protective_mbr(s0) or disk.read_sectors(1, 1)[:8] == GPT_SIG:
        info, parts, hdr, entries = parse_gpt(
            disk.read_sectors, disk.sector_size, disk.total_sectors
        )
        return "gpt", parts, {"mbr": s0, "hdr": hdr, "entries": entries, "info": info}
    return "mbr", parse_mbr(s0), {"mbr": s0}


def build_protective_mbr(total_sectors: int, sector_size: int = SECTOR_SIZE) -> bytes:
    """Build a standard GPT protective MBR sector."""
    buf = bytearray(sector_size)
    size_lba = min(total_sectors - 1, 0xFFFFFFFF)
    buf[446] = 0x00
    buf[447:450] = b"\x00\x02\x00"
    buf[450] = 0xEE
    buf[451:454] = b"\xff\xff\xff"
    struct.pack_into("<II", buf, 454, 1, size_lba)
    buf[510:512] = MBR_SIG
    return bytes(buf)


def build_mbr(
    start_lba: int,
    sector_count: int,
    *,
    ptype: int = 0x07,
    sector_size: int = SECTOR_SIZE,
) -> bytes:
    """Build a valid single-partition MBR sector."""
    buf = bytearray(sector_size)
    buf[446 + 4] = ptype
    struct.pack_into("<II", buf, 446 + 8, start_lba, sector_count)
    buf[510:512] = MBR_SIG
    return bytes(buf)


def build_gpt(
    total_sectors: int,
    part_start_lba: int,
    part_sector_count: int,
    *,
    type_guid: str = BASIC_DATA_GUID,
    disk_guid: str = "11111111-2222-3333-4444-555555555555",
    part_guid: str = "66666666-7777-8888-9999-AAAAAAAAAAAA",
    name: str = "Data",
    sector_size: int = SECTOR_SIZE,
    num_entries: int = 128,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Build `(protective_mbr, primary_hdr, entries_raw, backup_hdr)` for a GPT disk."""
    entry_bytes_len = num_entries * GPT_ENTRY_SIZE
    entry_sectors = (entry_bytes_len + sector_size - 1) // sector_size
    first_usable = 2 + entry_sectors
    last_usable = total_sectors - 2 - entry_sectors
    if part_start_lba < first_usable or part_start_lba + part_sector_count - 1 > last_usable:
        raise ValueError("GPT partition exceeds usable LBA range")

    entries = bytearray(entry_sectors * sector_size)
    entries[0:16] = str_to_guid_bytes(type_guid)
    entries[16:32] = str_to_guid_bytes(part_guid)
    struct.pack_into(
        "<QQQ", entries, 32, part_start_lba, part_start_lba + part_sector_count - 1, 0
    )
    encoded_name = name.encode("utf-16-le")[:72]
    entries[56 : 56 + len(encoded_name)] = encoded_name
    entries_crc = zlib.crc32(entries[:entry_bytes_len]) & 0xFFFFFFFF

    pri = bytearray(sector_size)
    struct.pack_into(
        _GPT_HDR_FMT,
        pri,
        0,
        GPT_SIG,
        GPT_REV,
        GPT_HDR_SIZE,
        0,
        0,
        1,
        total_sectors - 1,
        first_usable,
        last_usable,
        str_to_guid_bytes(disk_guid),
        2,
        num_entries,
        GPT_ENTRY_SIZE,
        entries_crc,
    )
    struct.pack_into("<I", pri, 16, zlib.crc32(pri[:GPT_HDR_SIZE]) & 0xFFFFFFFF)

    bak = bytearray(pri)
    struct.pack_into("<QQ", bak, 24, total_sectors - 1, 1)
    struct.pack_into("<Q", bak, 72, total_sectors - 1 - entry_sectors)
    struct.pack_into("<I", bak, 16, 0)
    struct.pack_into("<I", bak, 16, zlib.crc32(bak[:GPT_HDR_SIZE]) & 0xFFFFFFFF)

    return build_protective_mbr(total_sectors, sector_size), bytes(pri), bytes(entries), bytes(bak)


def save_backup(path: Path, payload: dict[str, Any]) -> Path:
    """Write a checksummed Level 4 partition backup file atomically."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    wrapper = {"version": 1, "sha256": digest, "payload": payload}
    return files.write(path, json.dumps(wrapper, indent=2))


def load_backup(path: Path) -> dict[str, Any]:
    """Load and verify a Level 4 partition backup file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        payload = raw["payload"]
        digest = raw["sha256"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise errors.CorruptHeaderError(f"cannot read partition backup {path}: {exc}") from exc
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != digest:
        raise errors.CorruptHeaderError(f"partition backup checksum mismatch: {path}")
    return dict(payload)
