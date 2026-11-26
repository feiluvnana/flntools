"""Disk abstraction (`Disk` protocol, `FileDisk`, OS backends, and boot-disk guards)."""

from __future__ import annotations

import ctypes
import errno
import os
import plistlib
import re
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

FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020


def check_filesystem(fs_type: str) -> None:
    """Refuse filesystems that cannot be shrunk in-place (such as FAT32/exFAT)."""
    normalized = fs_type.strip().lower()
    if normalized not in SUPPORTED_FS:
        raise errors.UnsupportedError(
            f"level 3 (--volume) does not support filesystem {fs_type or 'unknown'!r}",
            hint="use flnh hide (or --lock) on FAT32/exFAT; --volume needs NTFS, ext4, or APFS",
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
    manages_table: bool

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

    manages_table = False

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


def _pread_all(fd: int, offset: int, size: int) -> bytes:
    """Read exactly `size` bytes at `offset`, or fewer only at the end of the device."""
    chunks: list[bytes] = []
    while size > 0:
        chunk = os.pread(fd, size, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _pwrite_all(fd: int, offset: int, data: bytes, device: str) -> None:
    """Write all of `data` at `offset`, failing on a short write."""
    view = memoryview(data)
    while view:
        n = os.pwrite(fd, view, offset)
        if n <= 0:
            raise errors.DiskError(f"short write to {device} at byte {offset}")
        view = view[n:]
        offset += n


class Win32Disk:
    """Windows physical drive + NTFS online resize backend via Storage APIs."""

    manages_table = True

    def __init__(self, path: Path) -> None:
        from ctypes import wintypes

        if os.name != "nt":
            raise errors.UnsupportedError("Win32Disk requires Windows")
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        if not shell32.IsUserAnAdmin():
            raise errors.PermissionError(
                "--volume requires Administrator privileges",
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
        self._k32.SetFilePointerEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int64,
            ctypes.POINTER(ctypes.c_int64),
            wintypes.DWORD,
        ]
        self._k32.SetFilePointerEx.restype = wintypes.BOOL
        self._k32.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self._k32.ReadFile.restype = wintypes.BOOL
        self._k32.WriteFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self._k32.WriteFile.restype = wintypes.BOOL

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
        self._lock_handle: Any = None
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
        if not self._k32.SetFilePointerEx(self._raw_handle, offset, None, 0):
            err = ctypes.get_last_error()
            raise errors.disk(OSError(err, ctypes.FormatError(err)), self.device_id)
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
        if not self._k32.SetFilePointerEx(self._raw_handle, offset, None, 0):
            err = ctypes.get_last_error()
            raise errors.disk(OSError(err, ctypes.FormatError(err)), self.device_id)
        written = ctypes.c_ulong()
        if not self._k32.WriteFile(
            self._raw_handle, buf, len(data), ctypes.byref(written), None
        ):
            err = ctypes.get_last_error()
            if err == 5 and self._lock_handle is None:
                self._lock_volume()
                if not self._k32.SetFilePointerEx(self._raw_handle, offset, None, 0):
                    seek_err = ctypes.get_last_error()
                    raise errors.disk(
                        OSError(seek_err, ctypes.FormatError(seek_err)), self.device_id
                    )
                if self._k32.WriteFile(
                    self._raw_handle, buf, len(data), ctypes.byref(written), None
                ):
                    return
                err = ctypes.get_last_error()
            raise errors.disk(OSError(err, ctypes.FormatError(err)), self.device_id)

    def _lock_volume(self) -> None:
        if self._lock_handle is not None:
            return
        h = self._open_handle(f"\\\\.\\{self.drive}", write=True)
        ret = ctypes.c_ulong()
        if not self._k32.DeviceIoControl(
            h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None
        ):
            self._k32.CloseHandle(h)
            raise errors.BusyError(
                f"volume {self.drive} has open handles",
                hint=f"cd to another drive and close Explorer windows on {self.drive}, then retry",
            )
        if not self._k32.DeviceIoControl(
            h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(ret), None
        ):
            err = ctypes.get_last_error()
            self._k32.CloseHandle(h)
            raise errors.disk(OSError(err, ctypes.FormatError(err)), self.drive)
        self._lock_handle = h

    def _unlock_volume(self) -> None:
        if self._lock_handle is not None:
            self._k32.CloseHandle(self._lock_handle)
            self._lock_handle = None

    def _close_raw(self) -> None:
        self._unlock_volume()
        if self._raw_handle:
            self._k32.CloseHandle(self._raw_handle)
            self._raw_handle = None

    def _reopen_raw(self) -> None:
        if not self._raw_handle:
            self._raw_handle = self._open_handle(self.device_id, write=True)

    def _drive_letter(self) -> str:
        letter = self.drive.rstrip("\\").rstrip(":").upper()
        if len(letter) != 1 or not ("A" <= letter <= "Z"):
            raise errors.UnsupportedError(f"invalid Windows drive letter: {self.drive}")
        return letter

    def _ps_resize(self, new_bytes: int, *, clamp_max: bool) -> None:
        letter = self._drive_letter()
        target = max(4096, (int(new_bytes) // 4096) * 4096)
        if clamp_max:
            script = (
                f"$m=(Get-PartitionSupportedSize -DriveLetter {letter} -ErrorAction Stop)"
                f".SizeMax; $t=[Math]::Min([UInt64]{target},[UInt64]$m); "
                f"Resize-Partition -DriveLetter {letter} -Size $t -ErrorAction Stop"
            )
        else:
            script = f"Resize-Partition -DriveLetter {letter} -Size {target} -ErrorAction Stop"
        res = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        if res.returncode != 0:
            msg = (res.stderr or res.stdout or f"exit code {res.returncode}").strip()
            raise errors.DiskError(
                f"partition resize failed on {self.drive}: {msg}",
                hint="close open files on that drive or free more contiguous space",
            )

    def max_shrink_bytes(self, part_index: int) -> int:
        letter = self._drive_letter()
        script = (
            f"$p=Get-Partition -DriveLetter {letter} -ErrorAction Stop; "
            f"$s=Get-PartitionSupportedSize -DriveLetter {letter} -ErrorAction Stop; "
            "Write-Output ([Math]::Max(0, [Int64]($p.Size - $s.SizeMin)))"
        )
        res = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        if res.returncode == 0:
            out = res.stdout.strip().splitlines()
            if out and out[-1].strip().isdigit():
                return int(out[-1].strip())
        _total, _used, free = shutil.disk_usage(self.drive + "\\")
        return free

    def shrink_fs(self, part_index: int, new_bytes: int) -> None:
        self._close_raw()
        try:
            self._ps_resize(new_bytes, clamp_max=False)
        finally:
            self._reopen_raw()

    def expand_fs(self, part_index: int, new_bytes: int) -> None:
        self._close_raw()
        try:
            self._ps_resize(new_bytes, clamp_max=True)
        finally:
            self._reopen_raw()

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
        self._unlock_volume()

    def close(self) -> None:
        self._unlock_volume()
        if self._raw_handle:
            self._k32.CloseHandle(self._raw_handle)
            self._raw_handle = None


class LinuxDisk:
    """Linux block device + ext4 resize2fs backend."""

    manages_table = False

    def __init__(self, path: Path) -> None:
        if os.geteuid() != 0:  # type: ignore[attr-defined]
            raise errors.PermissionError(
                "--volume requires root privileges",
                hint="re-run with sudo",
            )
        res = _run(["findmnt", "-no", "SOURCE,FSTYPE,TARGET", "-T", str(path)])
        if res.returncode != 0 or not res.stdout.strip():
            raise errors.DiskError(f"cannot resolve filesystem for {path}")
        parts = res.stdout.strip().split(None, 2)
        self.part_dev = parts[0]
        self.fs_type = parts[1].lower()
        self.mountpoint = parts[2]
        # The kernel's own numbers for the partition, so the table edit lands on it and on
        # nothing else (LVM, RAID, dm-crypt or a whole-disk filesystem have none).
        sys_dir = Path("/sys/class/block") / Path(os.path.realpath(self.part_dev)).name
        self._sys_dir = sys_dir
        pk = _run(["lsblk", "-no", "PKNAME", self.part_dev])
        pkname = pk.stdout.strip()
        if not (sys_dir / "partition").is_file() or not pkname:
            raise errors.UnsupportedError(
                f"--volume needs a filesystem on a plain disk partition: {self.part_dev}",
                hint="LVM, RAID, encrypted and whole-disk filesystems are not supported",
            )
        self.part_index = int((sys_dir / "partition").read_text(encoding="utf-8").strip())
        self.device_id = f"/dev/{pkname}"
        ss = _run(["blockdev", "--getss", self.device_id])
        self.sector_size = int(ss.stdout.strip()) if ss.stdout.strip().isdigit() else 512
        # sysfs counts in 512-byte units whatever the sector size.
        self.part_offset = int((sys_dir / "start").read_text(encoding="utf-8")) * 512
        self.part_bytes = int((sys_dir / "size").read_text(encoding="utf-8")) * 512
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
        return _pread_all(self._fp.fileno(), lba * self.sector_size, count * self.sector_size)

    def write_sectors(self, lba: int, data: bytes) -> None:
        _pwrite_all(self._fp.fileno(), lba * self.sector_size, data, self.device_id)
        os.fsync(self._fp.fileno())

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
        # Mount again either way: the files to hide are read from it next.
        mt = _run(["mount", self.part_dev, self.mountpoint])
        if rs.returncode != 0:
            raise errors.DiskError(f"resize2fs shrink failed: {rs.stderr.strip()}")
        if mt.returncode != 0:
            raise errors.DiskError(
                f"cannot mount {self.part_dev} again after shrinking: {mt.stderr.strip()}",
                hint=f"mount it, then grow it back with resize2fs {self.part_dev}",
            )

    def expand_fs(self, part_index: int, new_bytes: int) -> None:
        # ext4 grows online, so the volume stays mounted for the files restored onto it.
        kernel_bytes = int((self._sys_dir / "size").read_text(encoding="utf-8")) * 512
        if kernel_bytes < new_bytes:
            raise errors.DiskError(
                f"the kernel still sees {self.part_dev} at its shrunk size",
                hint=f"run partx -u {self.device_id}, then resize2fs {self.part_dev}",
            )
        rs = _run(["resize2fs", self.part_dev, f"{new_bytes // 1024}K"])
        if rs.returncode != 0:
            raise errors.DiskError(f"resize2fs expand failed: {rs.stderr.strip()}")

    def refresh(self) -> None:
        # partx updates one partition while it is mounted; partprobe refuses (EBUSY).
        res = _run(["partx", "-u", "--nr", str(self.part_index), self.device_id])
        if res.returncode != 0:
            raise errors.DiskError(
                f"cannot make the kernel re-read {self.part_dev}: {res.stderr.strip()}",
                hint="reboot, or run partx -u on the disk, before using the volume",
            )

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()


class DarwinDisk:
    """macOS raw disk + APFS resizeContainer backend.

    `diskutil apfs resizeContainer` resizes the GPT partition itself, so the table is left to
    it. The raw device is the physical disk that holds the container's one physical store.
    """

    manages_table = True

    def __init__(self, path: Path) -> None:
        if os.geteuid() != 0:  # type: ignore[attr-defined]
            raise errors.PermissionError(
                "--volume requires root privileges",
                hint="re-run with sudo",
            )
        # diskutil takes a device or a mount point, never a folder inside a volume.
        info = self._diskutil_plist(_mount_point(path))
        self.vol_id = str(info.get("DeviceIdentifier", ""))
        self.fs_type = str(info.get("FilesystemType", "")).lower()
        self.container_free = int(info.get("APFSContainerFree") or info.get("FreeSpace") or 0)
        self.store = _physical_store(info)
        store_info = self._diskutil_plist(self.store)
        self.parent_disk = str(store_info.get("ParentWholeDisk", ""))
        match = re.fullmatch(r"disk\d+s(\d+)", self.store)
        if not match or not self.parent_disk:
            raise errors.UnsupportedError(f"cannot find the partition behind {path}")
        self.part_index = int(match.group(1))
        self.part_offset = int(store_info.get("PartitionMapPartitionOffset", -1))
        self.part_bytes = int(store_info.get("TotalSize", 0))
        disk_info = self._diskutil_plist(self.parent_disk)
        self.sector_size = int(disk_info.get("DeviceBlockSize", 512))
        self.total_sectors = int(disk_info.get("TotalSize", 0)) // self.sector_size
        self.device_id = f"/dev/r{self.parent_disk}"
        self._fp: Any = None

    def _raw(self) -> int:
        """The raw disk, opened on first use: only after the safety checks have passed."""
        if self._fp is None:
            try:
                self._fp = open(self.device_id, "r+b", buffering=0)
            except OSError as exc:
                if exc.errno == errno.EBUSY:
                    raise errors.BusyError(
                        f"macOS refuses to write to {self.device_id} while it is in use",
                        hint="--volume cannot hide on this disk while its volumes are mounted",
                    ) from exc
                raise
        return self._fp.fileno()

    def _diskutil_plist(self, target: str) -> dict[str, Any]:
        res = subprocess.run(
            ["diskutil", "info", "-plist", target], capture_output=True, check=False
        )
        if res.returncode != 0:
            raise errors.DiskError(f"diskutil info failed for {target}")
        return plistlib.loads(res.stdout)

    def system_disk_reason(self) -> str:
        boot = self._diskutil_plist("/")
        boot_store = self._diskutil_plist(_physical_store(boot))
        is_boot = str(boot_store.get("ParentWholeDisk", "")) == self.parent_disk
        info = self._diskutil_plist(self.vol_id)
        mounts = {str(info.get("MountPoint", ""))} - {""}
        roles = {str(r) for r in info.get("APFSVolumeRoles", [])}
        return check_darwin_system(mountpoints=mounts, apfs_roles=roles, is_boot_disk=is_boot)

    def is_system_disk(self) -> bool:
        return bool(self.system_disk_reason())

    def read_sectors(self, lba: int, count: int) -> bytes:
        return _pread_all(self._raw(), lba * self.sector_size, count * self.sector_size)

    def write_sectors(self, lba: int, data: bytes) -> None:
        _pwrite_all(self._raw(), lba * self.sector_size, data, self.device_id)
        os.fsync(self._raw())

    def max_shrink_bytes(self, part_index: int) -> int:
        return self.container_free

    def shrink_fs(self, part_index: int, new_bytes: int) -> None:
        rs = _run(["diskutil", "apfs", "resizeContainer", self.store, str(new_bytes)])
        if rs.returncode != 0:
            raise errors.BusyError(
                f"APFS shrink failed: {(rs.stderr or rs.stdout).strip()}",
                hint="close open files on the volume and try again",
            )

    def expand_fs(self, part_index: int, new_bytes: int) -> None:
        rs = _run(["diskutil", "apfs", "resizeContainer", self.store, str(new_bytes)])
        if rs.returncode != 0:
            raise errors.DiskError(f"APFS expand failed: {(rs.stderr or rs.stdout).strip()}")

    def refresh(self) -> None:
        pass

    def close(self) -> None:
        if self._fp is not None and not self._fp.closed:
            self._fp.close()


def _mount_point(path: Path) -> str:
    """The mount point of the volume that holds `path`."""
    here = Path(os.path.abspath(path))
    while not os.path.ismount(here) and here.parent != here:
        here = here.parent
    return str(here)


def _physical_store(info: dict[str, Any]) -> str:
    """The partition (`diskNsM`) behind a volume: its APFS container's single physical store."""
    if str(info.get("FilesystemType", "")).lower() != "apfs":
        return str(info.get("DeviceIdentifier", ""))
    stores = info.get("APFSPhysicalStores") or []
    if len(stores) != 1:
        raise errors.UnsupportedError("--volume needs an APFS container on a single partition")
    return str(stores[0].get("APFSPhysicalStore", ""))


def open_disk(path: Path) -> Disk:
    """Open the OS block/filesystem backend for `path`."""
    if sys.platform == "win32":
        return Win32Disk(path)
    if sys.platform == "darwin":
        return DarwinDisk(path)
    return LinuxDisk(path)
