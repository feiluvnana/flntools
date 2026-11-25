"""Windows Levels 1 & 2: CLSID rename + `+h +s`, NTFS SDDL lock (`SPEC.md#levels-1-and-2`)."""

from __future__ import annotations

import builtins
import ctypes
import json
import os
import shutil
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

from flnh import errors, vault
from flnkit import files

CLSID = "{21EC2020-3AEA-1069-A2DD-08002B30309D}"
META_NAME = ".flnh.meta"

FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SDDL_REVISION_1 = 1

# Deny Everyone list/read/traverse/attributes while keeping Owner WRITE_DAC and READ_CONTROL.
LOCK_SDDL = "D:P(D;OICI;0x1000A9;;;WD)(A;;WDRC;;;OW)"
UNLOCK_TEMP_SDDL = "D:P(A;OICI;FA;;;OW)"


def _kernel32() -> Any:
    if os.name != "nt":
        raise errors.UnsupportedError("Win32 API is only available on Windows")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    k32.GetFileAttributesW.restype = wintypes.DWORD
    k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    k32.SetFileAttributesW.restype = wintypes.BOOL
    k32.LocalFree.argtypes = [wintypes.HLOCAL]
    k32.LocalFree.restype = wintypes.HLOCAL
    return k32


def _advapi32() -> Any:
    if os.name != "nt":
        raise errors.UnsupportedError("Win32 API is only available on Windows")
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    adv.SetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    ]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    adv.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    adv.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    return adv


def get_attrs(path: Path) -> int:
    """Return Win32 file attributes for `path`."""
    attrs = _kernel32().GetFileAttributesW(str(path))
    if attrs == INVALID_FILE_ATTRIBUTES:
        err = ctypes.get_last_error()
        raise errors.disk(OSError(err, ctypes.FormatError(err)), path)
    return int(attrs)


def set_attrs(path: Path, attrs: int) -> None:
    """Set Win32 file attributes on `path`."""
    if not _kernel32().SetFileAttributesW(str(path), attrs):
        err = ctypes.get_last_error()
        raise errors.disk(OSError(err, ctypes.FormatError(err)), path)


def add_hidden_system(path: Path) -> None:
    """Set `FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM` on `path`."""
    attrs = get_attrs(path)
    set_attrs(path, attrs | FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)


def clear_hidden_system(path: Path) -> None:
    """Clear `HIDDEN`, `SYSTEM`, and `READONLY` attributes on `path`."""
    attrs = get_attrs(path)
    cleaned = attrs & ~(FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_READONLY)
    set_attrs(path, cleaned if cleaned else FILE_ATTRIBUTE_NORMAL)


def get_sddl(path: Path) -> str:
    """Read the DACL of `path` as an SDDL string."""
    adv = _advapi32()
    k32 = _kernel32()
    p_sd = ctypes.c_void_p()
    p_dacl = ctypes.c_void_p()
    rc = adv.GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(p_dacl),
        None,
        ctypes.byref(p_sd),
    )
    if rc != 0:
        raise errors.disk(OSError(rc, ctypes.FormatError(rc)), path)
    str_ptr = wintypes.LPWSTR()
    try:
        ok = adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            p_sd,
            SDDL_REVISION_1,
            DACL_SECURITY_INFORMATION,
            ctypes.byref(str_ptr),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            raise errors.disk(OSError(err, ctypes.FormatError(err)), path)
        return str(str_ptr.value)
    finally:
        if str_ptr.value is not None:
            k32.LocalFree(str_ptr)
        if p_sd.value:
            k32.LocalFree(p_sd)


def set_sddl(path: Path, sddl: str) -> None:
    """Apply a DACL SDDL string to `path`."""
    adv = _advapi32()
    k32 = _kernel32()
    p_sd = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, SDDL_REVISION_1, ctypes.byref(p_sd), None
    ):
        err = ctypes.get_last_error()
        raise errors.disk(OSError(err, ctypes.FormatError(err)), path)
    try:
        present = wintypes.BOOL()
        p_dacl = ctypes.c_void_p()
        defaulted = wintypes.BOOL()
        if not adv.GetSecurityDescriptorDacl(
            p_sd, ctypes.byref(present), ctypes.byref(p_dacl), ctypes.byref(defaulted)
        ):
            err = ctypes.get_last_error()
            raise errors.disk(OSError(err, ctypes.FormatError(err)), path)
        sec_info = DACL_SECURITY_INFORMATION
        if sddl.startswith("D:P"):
            sec_info |= PROTECTED_DACL_SECURITY_INFORMATION
        else:
            sec_info |= UNPROTECTED_DACL_SECURITY_INFORMATION
        rc = adv.SetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            sec_info,
            None,
            None,
            p_dacl if present.value else None,
            None,
        )
        if rc != 0:
            raise errors.disk(OSError(rc, ctypes.FormatError(rc)), path)
    finally:
        if p_sd.value:
            k32.LocalFree(p_sd)


def clsid_path(orig_path: Path) -> Path:
    """Return the deterministic vanished Control Panel CLSID path for `orig_path`."""
    return orig_path.parent / f"Control Panel.{vault.item_id(orig_path.name)}.{CLSID}"


def _write_meta(folder: Path, data: dict[str, Any]) -> None:
    meta_file = folder / META_NAME
    files.write(meta_file, json.dumps(data, separators=(",", ":")))
    add_hidden_system(meta_file)


def _read_meta(folder: Path) -> dict[str, Any] | None:
    meta_file = folder / META_NAME
    if not meta_file.is_file():
        return None
    return json.loads(meta_file.read_text(encoding="utf-8"))


def _remove_meta(folder: Path) -> None:
    meta_file = folder / META_NAME
    if meta_file.exists():
        clear_hidden_system(meta_file)
        meta_file.unlink()


def detect(orig_path: Path, entry: Any = None) -> dict[str, Any] | None:
    """Detect a hidden Level 1 or Level 2 item for `orig_path`."""
    target = Path(entry.hidden_path) if entry is not None else clsid_path(orig_path)
    if target.exists():
        return {"hidden_path": target, "entry": entry}
    return None


def hide(
    path: Path,
    *,
    level: str,
    password: str | None = None,
    track: bool = True,
) -> dict[str, Any]:
    """Hide `path` on Windows using CLSID + `+h +s`, plus NTFS SDDL for `lock`."""
    dest = clsid_path(path)
    if dest.exists():
        raise errors.UsageError(
            f"already hidden: {path}",
            hint=f"run flnh show {path} to restore it",
        )
    is_file = path.is_file() and not path.is_symlink()
    kind = "file" if is_file else "dir"
    size_bytes = vault.tree_size(path)
    orig_sddl = get_sddl(path) if level == "lock" else ""

    meta: dict[str, Any] = {
        "kind": kind,
        "orig_name": path.name,
        "level": level,
    }
    if level == "lock":
        meta["sddl"] = orig_sddl
        if password is not None:
            salt_hex, hash_hex = vault.hash_password(password)
            meta["pass_salt"] = salt_hex
            meta["pass_hash"] = hash_hex

    if is_file:
        dest.mkdir(parents=True, exist_ok=False)
        try:
            moved = dest / path.name
            os.replace(path, moved)
            add_hidden_system(moved)
            _write_meta(dest, meta)
        except BaseException:
            if (dest / path.name).exists() and not path.exists():
                os.replace(dest / path.name, path)
            shutil.rmtree(dest, ignore_errors=True)
            raise
    else:
        os.replace(path, dest)
        if level == "lock":
            _write_meta(dest, meta)

    add_hidden_system(dest)
    if level == "lock":
        try:
            set_sddl(dest, LOCK_SDDL)
        except BaseException:
            clear_hidden_system(dest)
            if is_file:
                _remove_meta(dest)
                clear_hidden_system(dest / path.name)
                os.replace(dest / path.name, path)
                shutil.rmtree(dest, ignore_errors=True)
            else:
                _remove_meta(dest)
                os.replace(dest, path)
            raise

    return {
        "kind": kind,
        "hidden_path": str(dest),
        "size_bytes": size_bytes,
        "meta": meta,
    }


def show(
    orig_path: Path,
    *,
    entry: Any = None,
    get_password: Callable[[], str] | None = None,
) -> tuple[Path, str]:
    """Unhide a Windows Level 1 or Level 2 item back to `orig_path`."""
    if orig_path.exists() or orig_path.is_symlink():
        raise errors.UsageError(f"destination already exists: {orig_path}")
    target = Path(entry.hidden_path) if entry is not None else clsid_path(orig_path)
    if not target.exists():
        raise errors.UsageError(
            f"no hidden folder or file found for: {orig_path}",
            hint="run flnh show --list to see tracked items",
        )

    cur_sddl = get_sddl(target)
    cur_lower = cur_sddl.lower()
    is_locked = (entry is not None and entry.level == "lock") or (
        "(d;" in cur_lower and ";;;wd)" in cur_lower
    )

    # If locked, unlock DACL temporarily only if needed to read metadata, re-locking on AuthError.
    meta: dict[str, Any] = dict(entry.meta) if entry is not None else {}
    if is_locked and not meta:
        set_sddl(target, UNLOCK_TEMP_SDDL)
        try:
            meta = _read_meta(target) or {}
        except BaseException:
            set_sddl(target, LOCK_SDDL)
            raise
    elif not is_locked and not meta:
        meta = _read_meta(target) or {}

    if meta.get("pass_hash") and meta.get("pass_salt"):
        if get_password is None:
            if is_locked:
                set_sddl(target, LOCK_SDDL)
            raise errors.AuthError("password required to unlock this item")
        try:
            pw = get_password()
        except BaseException:
            if is_locked:
                set_sddl(target, LOCK_SDDL)
            raise
        if not vault.verify_password(pw, meta["pass_salt"], meta["pass_hash"]):
            if is_locked:
                set_sddl(target, LOCK_SDDL)
            raise errors.AuthError(
                "wrong password",
                hint="check the password and try again",
            )

    level = str(meta.get("level") or ("lock" if is_locked else "flag"))
    if is_locked:
        orig_sddl = meta.get("sddl")
        set_sddl(target, orig_sddl or UNLOCK_TEMP_SDDL)

    try:
        meta_now = _read_meta(target)
    except builtins.PermissionError as exc:
        raise errors.PermissionError(
            f"permission denied reading {target}",
            hint="run from an elevated terminal if ACLs were altered",
        ) from exc
    if meta_now and not meta:
        meta = meta_now

    is_file = meta.get("kind") == "file"
    clear_hidden_system(target)
    _remove_meta(target)

    if is_file:
        orig_name = str(meta.get("orig_name") or orig_path.name)
        wrapped = target / orig_name
        if not wrapped.exists():
            raise errors.CorruptHeaderError(f"wrapped file missing inside {target}")
        clear_hidden_system(wrapped)
        os.replace(wrapped, orig_path)
        shutil.rmtree(target, ignore_errors=True)
    else:
        os.replace(target, orig_path)
    return orig_path, level
