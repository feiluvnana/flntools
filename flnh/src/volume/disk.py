"""Disk abstraction (`Disk` protocol, `FileDisk`, OS backends, and boot-disk guards)."""

from __future__ import annotations

import ctypes
import os
import plistlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol

from flnh import errors
from flnh.volume import table

SUPPORTED_FS = frozenset({"ntfs", "ext4", "apfs"})
WIN_SYSTEM_ENTRIES = frozenset({"windows", "pagefile.sys", "hiberfil.sys", "swapfile.sys"})


def check_filesystem(fs_type: str) -> None:
    """Refuse filesystems that cannot be shrunk in-place (such as FAT32/exFAT)."""
    normalized = fs_type.strip().lower()
    if normalized not in SUPPORTED_FS:
        raise errors.UnsupportedError(
            f"level 4 (volume) does not support filesystem {fs_type or 'unknown'!r}",
            hint="use --vault instead (works on FAT32, exFAT, and any filesystem)",
        )


def check_win_system(
    *,
    drive: str,
    system_drive: str,
    root_entries: set[str],
    gpt_type_guids: set[str],
) -> str:
    """Return a refusal reason if a Windows disk hosts OS/boot/swap state, else ''."""
    drv = drive.rstrip("\\").upper()
    sys_drv = system_drive.rstrip("\\").upper()
    if sys_drv and drv == sys_drv:
        return f"disk hosts system drive {sys_drv}"
    lowered = {entry.lower() for entry in root_entries}
    for marker in sorted(WIN_SYSTEM_ENTRIES):
        if marker in lowered:
            return f"disk contains OS/swap artifact {marker!r}"
    for guid in sorted(gpt_type_guids):
        if guid.upper() in table.SYSTEM_GPT_GUIDS:
            return f"disk holds protected system partition ({guid.upper()})"
    return ""


def check_linux_system(
    *,
    mountpoints: set[str],
    swap_devices: set[str],
    disk_devices: set[str],
) -> str:
    """Return a refusal reason if a Linux disk hosts OS/boot/swap mounts, else ''."""
    for mnt in ("/", "/boot", "/boot/efi"):
        if mnt in mountpoints:
            return f"disk hosts OS mount {mnt}"
    overlap = swap_devices & disk_devices
    if overlap:
        return f"disk hosts active swap device {sorted(overlap)[0]}"
    return ""


def check_darwin_system(
    *,
    mountpoints: set[str],
    apfs_roles: set[str],
    is_boot_disk: bool,
) -> str:
    """Return a refusal reason if a macOS disk is the boot/OS/VM disk, else ''."""
    if is_boot_disk:
        return "disk is the macOS boot/internal OS disk"
    for mnt in ("/", "/System/Volumes/Data", "/System/Volumes/VM", "/System/Volumes/Preboot"):
        if mnt in mountpoints:
            return f"disk hosts macOS system volume {mnt}"
    if any(role.lower() in {"system", "preboot", "recovery", "vm"} for role in apfs_roles):
        return "disk contains macOS system/recovery/VM APFS roles"
    return ""


class Disk(Protocol):
    """Block and filesystem interface shared by OS backends and test `FileDisk`."""

    sector_size: int
    total_sectors: int
    device_id: str
    fs_type: str

    def is_system_disk(self) -> bool: ...

    def system_disk_reason(self) -> str: ...

    def read_sectors(self, lba: int, count: int) -> bytes: ...

    def write_sectors(self, lba: int, data: bytes) -> None: ...

    def max_shrink_bytes(self, part_index: int) -> int: ...

    def shrink_fs(self, part_index: int, new_bytes: int) -> None: ...

    def expand_fs(self, part_index: int, new_bytes: int) -> None: ...

    def refresh(self) -> None: ...

    def close(self) -> None: ...


class FileDisk:
    """Sparse-file disk image implementing `Disk` for unit tests."""

    def __init__(
        self,
        image_path: Path,
        *,
        sector_size: int = table.SECTOR_SIZE,
        total_sectors: int,
        fs_type: str = "ntfs",
        fs_bytes: int,
        used_bytes: int = 0,
        busy: bool = False,
        system_reason: str = "",
        device_id: str = "filedisk:0",
    ) -> None:
        self.image_path = image_path
        self.sector_size = sector_size
        self.total_sectors = total_sectors
        self.fs_type = fs_type
        self.fs_bytes = fs_bytes
        self.used_bytes = used_bytes
        self.busy = busy
        self.system_reason = system_reason
        self.device_id = device_id
        self.refreshed = False
        self.corrupt_next_write = False
        self._fp = open(image_path, "r+b")

    @classmethod
    def create_mbr(
        cls,
        image_path: Path,
        total_bytes: int,
        *,
        sector_size: int = table.SECTOR_SIZE,
        fs_type: str = "ntfs",
        used_bytes: int = 0,
        part_start_lba: int = 2048,
        busy: bool = False,
        system_reason: str = "",
    ) -> FileDisk:
        """Create a synthetic MBR disk image file."""
        total_sectors = total_bytes // sector_size
        part_sectors = total_sectors - part_start_lba
        with open(image_path, "wb") as f:
            f.truncate(total_sectors * sector_size)
            f.seek(0)
            f.write(table.build_mbr(part_start_lba, part_sectors, sector_size=sector_size))
        return cls(
            image_path,
            sector_size=sector_size,
            total_sectors=total_sectors,
            fs_type=fs_type,
            fs_bytes=part_sectors * sector_size,
            used_bytes=used_bytes,
            busy=busy,
            system_reason=system_reason,
            device_id=f"mbr:{image_path.name}",
        )

    @classmethod
    def create_gpt(
        cls,
        image_path: Path,
        total_bytes: int,
        *,
        sector_size: int = table.SECTOR_SIZE,
        fs_type: str = "ntfs",
        used_bytes: int = 0,
        part_start_lba: int = 2048,
        type_guid: str = table.BASIC_DATA_GUID,
        busy: bool = False,
        system_reason: str = "",
    ) -> FileDisk:
        """Create a synthetic GPT disk image file with primary and backup headers."""
        total_sectors = total_bytes // sector_size
        reserve_end = 34
        part_sectors = total_sectors - part_start_lba - reserve_end
        mbr, pri_hdr, entries, bak_hdr = table.build_gpt(
            total_sectors,
            part_start_lba,
            part_sectors,
            type_guid=type_guid,
            sector_size=sector_size,
        )
        entry_sectors = len(entries) // sector_size
        with open(image_path, "wb") as f:
            f.truncate(total_sectors * sector_size)
            f.seek(0)
            f.write(mbr)
            f.seek(sector_size)
            f.write(pri_hdr)
            f.seek(2 * sector_size)
            f.write(entries)
            f.seek((total_sectors - 1 - entry_sectors) * sector_size)
            f.write(entries)
            f.seek((total_sectors - 1) * sector_size)
            f.write(bak_hdr)
        return cls(
            image_path,
            sector_size=sector_size,
            total_sectors=total_sectors,
            fs_type=fs_type,
            fs_bytes=part_sectors * sector_size,
            used_bytes=used_bytes,
            busy=busy,
            system_reason=system_reason,
            device_id=f"gpt:{image_path.name}",
        )

    def is_system_disk(self) -> bool:
        return bool(self.system_reason)

    def system_disk_reason(self) -> str:
        return self.system_reason

    def read_sectors(self, lba: int, count: int) -> bytes:
        if lba < 0 or count <= 0 or lba + count > self.total_sectors:
            raise errors.DiskError(f"sector read out of bounds: lba={lba} count={count}")
        self._fp.seek(lba * self.sector_size)
        return self._fp.read(count * self.sector_size)

    def write_sectors(self, lba: int, data: bytes) -> None:
        if len(data) % self.sector_size != 0:
            raise errors.DiskError("unaligned sector write")
        count = len(data) // self.sector_size
        if lba < 0 or count <= 0 or lba + count > self.total_sectors:
            raise errors.DiskError(f"sector write out of bounds: lba={lba} count={count}")
        payload = data
        if self.corrupt_next_write:
            self.corrupt_next_write = False
            b = bytearray(data)
            b[0] ^= 0xFF
            payload = bytes(b)
        self._fp.seek(lba * self.sector_size)
        self._fp.write(payload)
        self._fp.flush()

    def max_shrink_bytes(self, part_index: int) -> int:
        return max(0, self.fs_bytes - self.used_bytes)

    def shrink_fs(self, part_index: int, new_bytes: int) -> None:
        if self.busy:
            raise errors.BusyError(
                "volume is in use by another process",
                hint="close open files or Explorer windows on that drive and try again",
            )
        if new_bytes < self.used_bytes or new_bytes > self.fs_bytes:
            raise errors.DiskError("cannot shrink filesystem to requested size")
        self.fs_bytes = new_bytes

    def expand_fs(self, part_index: int, new_bytes: int) -> None:
        if self.busy:
            raise errors.BusyError(
                "volume is in use by another process",
                hint="close open files or Explorer windows on that drive and try again",
            )
        self.fs_bytes = new_bytes

    def refresh(self) -> None:
        self.refreshed = True

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()


# ---------------------------------------------------------------------------------------------
# OS Backends
# ---------------------------------------------------------------------------------------------

def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


class Win32Disk:
    """Windows physical drive + NTFS shrink/extend backend via Win32 ioctls."""

    def __init__(self, path: Path) -> None:
        from ctypes import wintypes

        if os.name != "nt":
            raise errors.UnsupportedError("Win32Disk requires Windows")
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        if not shell32.IsUserAnAdmin():
            raise errors.PermissionError(
                "level 4 (volume) requires Administrator privileges",
                hint="re-run from an elevated Administrator terminal",
            )
        drive = os.path.splitdrive(str(path.resolve()))[0]
        if not drive or len(drive) != 2 or drive[1] != ":":
            raise errors.UnsupportedError(f"cannot determine drive letter for {path}")
        self.drive = drive.upper()
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self._k32.GetVolumeInformationW.restype = wintypes.BOOL
        self._k32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self._k32.DeviceIoControl.restype = wintypes.BOOL
        self._k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._k32.CreateFileW.restype = wintypes.HANDLE
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32.CloseHandle.restype = wintypes.BOOL

        fs_buf = ctypes.create_unicode_buffer(64)
        if not self._k32.GetVolumeInformationW(
            self.drive + "\\", None, 0, None, None, None, fs_buf, 64
        ):
            err = ctypes.get_last_error()
            raise errors.disk(OSError(err, ctypes.FormatError(err)), self.drive)
        self.fs_type = fs_buf.value.lower()
        self.disk_number, self.part_index = self._device_number()
        self.device_id = f"\\\\.\\PhysicalDrive{self.disk_number}"
        self.sector_size, self.total_sectors = self._geometry()
        self._raw_handle = self._open_handle(self.device_id, write=True)

    def _open_handle(self, device: str, *, write: bool) -> Any:
        access = 0xC0000000 if write else 0x80000000  # GENERIC_READ | GENERIC_WRITE
        handle = self._k32.CreateFileW(device, access, 3, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            err = ctypes.get_last_error()
            raise errors.disk(OSError(err, ctypes.FormatError(err)), device)
        return handle

    def _device_number(self) -> tuple[int, int]:
        ioctl_storage_get_device_number = 0x2D1080
        h = self._open_handle(f"\\\\.\\{self.drive}", write=False)
        try:
            out = (ctypes.c_ubyte * 12)()
            returned = ctypes.c_ulong()
            if not self._k32.DeviceIoControl(
                h, ioctl_storage_get_device_number, None, 0, out, 12, ctypes.byref(returned), None
            ):
                err = ctypes.get_last_error()
                raise errors.disk(OSError(err, ctypes.FormatError(err)), self.drive)
            _dev_type, dev_num, part_num = struct.unpack("<III", bytes(out))
            return dev_num, max(1, part_num)
        finally:
            self._k32.CloseHandle(h)

    def _geometry(self) -> tuple[int, int]:
        ioctl_disk_get_drive_geometry_ex = 0x700A0
        h = self._open_handle(self.device_id, write=False)
        try:
            out = (ctypes.c_ubyte * 64)()
            returned = ctypes.c_ulong()
            if not self._k32.DeviceIoControl(
                h,
                ioctl_disk_get_drive_geometry_ex,
                None,
                0,
                out,
                64,
                ctypes.byref(returned),
                None,
            ):
                err = ctypes.get_last_error()
                raise errors.disk(OSError(err, ctypes.FormatError(err)), self.device_id)
            raw = bytes(out)
            bytes_per_sector = struct.unpack_from("<I", raw, 20)[0] or 512
            disk_size = struct.unpack_from("<Q", raw, 24)[0]
            return bytes_per_sector, disk_size // bytes_per_sector
        finally:
            self._k32.CloseHandle(h)

    def system_disk_reason(self) -> str:
        sys_drive = os.environ.get("SystemDrive", "C:")
        root = Path(self.drive + "\\")
        try:
            entries = {p.name for p in root.iterdir()}
        except OSError:
            entries = set()
        gpt_guids: set[str] = set()
        try:
            scheme, parts, _ = table.read_table(self)
            if scheme == "gpt":
                gpt_guids = {p.type_id for p in parts}
        except errors.Error:
            pass
        return check_win_system(
            drive=self.drive,
            system_drive=sys_drive,
            root_entries=entries,
            gpt_type_guids=gpt_guids,
        )

    def is_system_disk(self) -> bool:
        return bool(self.system_disk_reason())

    def read_sectors(self, lba: int, count: int) -> bytes:
        buf = (ctypes.c_ubyte * (count * self.sector_size))()
        offset = lba * self.sector_size
        lo = ctypes.c_long(offset & 0xFFFFFFFF)
        hi = ctypes.c_long(offset >> 32)
        self._k32.SetFilePointer(self._raw_handle, lo, ctypes.byref(hi), 0)
        read_n = ctypes.c_ulong()
        if not self._k32.ReadFile(
            self._raw_handle, buf, count * self.sector_size, ctypes.byref(read_n), None
        ):
            err = ctypes.get_last_error()
            raise errors.disk(OSError(err, ctypes.FormatError(err)), self.device_id)
        return bytes(buf)[: read_n.value]

    def write_sectors(self, lba: int, data: bytes) -> None:
        if len(data) % self.sector_size != 0:
            raise errors.DiskError("unaligned sector write")
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        offset = lba * self.sector_size
        lo = ctypes.c_long(offset & 0xFFFFFFFF)
        hi = ctypes.c_long(offset >> 32)
        self._k32.SetFilePointer(self._raw_handle, lo, ctypes.byref(hi), 0)
        written = ctypes.c_ulong()
        if not self._k32.WriteFile(
            self._raw_handle, buf, len(data), ctypes.byref(written), None
        ):
            err = ctypes.get_last_error()
            raise errors.disk(OSError(err, ctypes.FormatError(err)), self.device_id)

    def max_shrink_bytes(self, part_index: int) -> int:
        _total, _used, free = shutil.disk_usage(self.drive + "\\")
        return free

    def shrink_fs(self, part_index: int, new_bytes: int) -> None:
        fsctl_lock_volume = 0x90018
        fsctl_dismount_volume = 0x90020
        fsctl_shrink_volume = 0x9023C
        h = self._open_handle(f"\\\\.\\{self.drive}", write=True)
        try:
            ret = ctypes.c_ulong()
            if not self._k32.DeviceIoControl(
                h, fsctl_lock_volume, None, 0, None, 0, ctypes.byref(ret), None
            ):
                raise errors.BusyError(
                    f"volume {self.drive} has open handles",
                    hint="close files or Explorer windows on the drive and try again",
                )
            # SHRINK_VOLUME_INFORMATION: ShrinkRequestType=1 (Prepare), NewNumberOfSectors, Flags
            sectors = new_bytes // self.sector_size
            for req_type in (1, 2):  # Prepare, then Commit
                inp = struct.pack("<I4xQQ", req_type, sectors, 0)
                buf = ctypes.create_string_buffer(inp)
                if not self._k32.DeviceIoControl(
                    h, fsctl_shrink_volume, buf, len(inp), None, 0, ctypes.byref(ret), None
                ):
                    err = ctypes.get_last_error()
                    raise errors.disk(OSError(err, ctypes.FormatError(err)), self.drive)
            self._k32.DeviceIoControl(
                h, fsctl_dismount_volume, None, 0, None, 0, ctypes.byref(ret), None
            )
        finally:
            self._k32.CloseHandle(h)

    def expand_fs(self, part_index: int, new_bytes: int) -> None:
        fsctl_extend_volume = 0x900F0
        h = self._open_handle(f"\\\\.\\{self.drive}", write=True)
        try:
            sectors = ctypes.c_uint64(new_bytes // self.sector_size)
            ret = ctypes.c_ulong()
            if not self._k32.DeviceIoControl(
                h, fsctl_extend_volume, ctypes.byref(sectors), 8, None, 0, ctypes.byref(ret), None
            ):
                err = ctypes.get_last_error()
                raise errors.disk(OSError(err, ctypes.FormatError(err)), self.drive)
        finally:
            self._k32.CloseHandle(h)

    def refresh(self) -> None:
        ioctl_disk_update_properties = 0x70140
        ret = ctypes.c_ulong()
        self._k32.DeviceIoControl(
            self._raw_handle,
            ioctl_disk_update_properties,
            None,
            0,
            None,
            0,
            ctypes.byref(ret),
            None,
        )

    def close(self) -> None:
        if self._raw_handle:
            self._k32.CloseHandle(self._raw_handle)
            self._raw_handle = None


class LinuxDisk:
    """Linux block device + ext4 resize2fs backend."""

    def __init__(self, path: Path) -> None:
        if os.geteuid() != 0:  # type: ignore[attr-defined]
            raise errors.PermissionError(
                "level 4 (volume) requires root privileges",
                hint="re-run with sudo",
            )
        res = _run(["findmnt", "-no", "SOURCE,FSTYPE,TARGET", "-T", str(path)])
        if res.returncode != 0 or not res.stdout.strip():
            raise errors.DiskError(f"cannot resolve filesystem for {path}")
        parts = res.stdout.strip().split(None, 2)
        self.part_dev = parts[0]
        self.fs_type = parts[1].lower()
        self.mountpoint = parts[2]
        pk = _run(["lsblk", "-no", "PKNAME", self.part_dev])
        pkname = pk.stdout.strip()
        self.device_id = f"/dev/{pkname}" if pkname else self.part_dev
        self.sector_size = 512
        sz = _run(["blockdev", "--getsize64", self.device_id])
        self.total_sectors = int(sz.stdout.strip()) // self.sector_size if sz.stdout.strip() else 0
        self._fp = open(self.device_id, "r+b", buffering=0)

    def system_disk_reason(self) -> str:
        ls = _run(["lsblk", "-no", "PATH,MOUNTPOINT", self.device_id])
        mounts: set[str] = set()
        devs: set[str] = set()
        for line in ls.stdout.splitlines():
            cols = line.split(None, 1)
            if cols:
                devs.add(cols[0])
            if len(cols) > 1 and cols[1]:
                mounts.add(cols[1])
        swaps: set[str] = set()
        try:
            for line in Path("/proc/swaps").read_text(encoding="utf-8").splitlines()[1:]:
                swaps.add(line.split()[0])
        except OSError:
            pass
        return check_linux_system(mountpoints=mounts, swap_devices=swaps, disk_devices=devs)

    def is_system_disk(self) -> bool:
        return bool(self.system_disk_reason())

    def read_sectors(self, lba: int, count: int) -> bytes:
        self._fp.seek(lba * self.sector_size)
        return self._fp.read(count * self.sector_size)

    def write_sectors(self, lba: int, data: bytes) -> None:
        self._fp.seek(lba * self.sector_size)
        self._fp.write(data)

    def max_shrink_bytes(self, part_index: int) -> int:
        _total, _used, free = shutil.disk_usage(self.mountpoint)
        return free

    def shrink_fs(self, part_index: int, new_bytes: int) -> None:
        um = _run(["umount", self.part_dev])
        if um.returncode != 0:
            raise errors.BusyError(
                f"cannot unmount {self.part_dev}: {um.stderr.strip()}",
                hint="close open files on the volume and try again",
            )
        _run(["e2fsck", "-f", "-p", self.part_dev])
        size_k = f"{new_bytes // 1024}K"
        rs = _run(["resize2fs", self.part_dev, size_k])
        if rs.returncode != 0:
            _run(["mount", self.part_dev, self.mountpoint])
            raise errors.DiskError(f"resize2fs shrink failed: {rs.stderr.strip()}")

    def expand_fs(self, part_index: int, new_bytes: int) -> None:
        _run(["umount", self.part_dev])
        _run(["e2fsck", "-f", "-p", self.part_dev])
        rs = _run(["resize2fs", self.part_dev, f"{new_bytes // 1024}K"])
        if rs.returncode != 0:
            raise errors.DiskError(f"resize2fs expand failed: {rs.stderr.strip()}")
        _run(["mount", self.part_dev, self.mountpoint])

    def refresh(self) -> None:
        _run(["partprobe", self.device_id])

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()


class DarwinDisk:
    """macOS raw disk + APFS resizeContainer backend."""

    def __init__(self, path: Path) -> None:
        if os.geteuid() != 0:  # type: ignore[attr-defined]
            raise errors.PermissionError(
                "level 4 (volume) requires root privileges",
                hint="re-run with sudo",
            )
        info = self._diskutil_plist(str(path))
        self.vol_id = str(info.get("DeviceIdentifier", ""))
        self.parent_disk = str(info.get("ParentWholeDisk", self.vol_id))
        self.fs_type = str(info.get("FilesystemType", "")).lower()
        self.device_id = f"/dev/r{self.parent_disk}"
        self.sector_size = int(info.get("DeviceBlockSize", 512))
        disk_info = self._diskutil_plist(self.parent_disk)
        self.total_sectors = int(disk_info.get("TotalSize", 0)) // self.sector_size
        self._fp = open(self.device_id, "r+b", buffering=0)

    def _diskutil_plist(self, target: str) -> dict[str, Any]:
        res = subprocess.run(
            ["diskutil", "info", "-plist", target], capture_output=True, check=False
        )
        if res.returncode != 0:
            raise errors.DiskError(f"diskutil info failed for {target}")
        return plistlib.loads(res.stdout)

    def system_disk_reason(self) -> str:
        info = self._diskutil_plist(self.parent_disk)
        is_boot = bool(info.get("Internal", False)) and bool(info.get("OSInternal", False))
        mounts = {str(info.get("MountPoint", ""))} - {""}
        roles = {str(r) for r in info.get("APFSVolumeRoles", [])}
        return check_darwin_system(mountpoints=mounts, apfs_roles=roles, is_boot_disk=is_boot)

    def is_system_disk(self) -> bool:
        return bool(self.system_disk_reason())

    def read_sectors(self, lba: int, count: int) -> bytes:
        self._fp.seek(lba * self.sector_size)
        return self._fp.read(count * self.sector_size)

    def write_sectors(self, lba: int, data: bytes) -> None:
        self._fp.seek(lba * self.sector_size)
        self._fp.write(data)

    def max_shrink_bytes(self, part_index: int) -> int:
        info = self._diskutil_plist(self.vol_id)
        return int(info.get("FreeSpace", 0))

    def shrink_fs(self, part_index: int, new_bytes: int) -> None:
        rs = _run(["diskutil", "apfs", "resizeContainer", self.vol_id, str(new_bytes)])
        if rs.returncode != 0:
            raise errors.BusyError(
                f"APFS shrink failed: {rs.stderr.strip()}",
                hint="close open files on the volume and try again",
            )
        _run(["diskutil", "unmountDisk", self.parent_disk])

    def expand_fs(self, part_index: int, new_bytes: int) -> None:
        _run(["diskutil", "mountDisk", self.parent_disk])
        rs = _run(["diskutil", "apfs", "resizeContainer", self.vol_id, str(new_bytes)])
        if rs.returncode != 0:
            raise errors.DiskError(f"APFS expand failed: {rs.stderr.strip()}")

    def refresh(self) -> None:
        _run(["diskutil", "mountDisk", self.parent_disk])

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()


def open_disk(path: Path) -> Disk:
    """Open the OS block/filesystem backend for `path`."""
    if sys.platform == "win32":
        return Win32Disk(path)
    if sys.platform == "darwin":
        return DarwinDisk(path)
    return LinuxDisk(path)
