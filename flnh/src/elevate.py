"""OS admin/root elevation helpers for `--volume` (`SPEC.md#level-3-volume`)."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any

from flnh import errors, state
from flnkit.ui import forms, out

ELEVATED_ENV = "FLNH_ELEVATED"
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0
ERROR_CANCELLED = 1223
INFINITE = 0xFFFFFFFF


def is_admin() -> bool:
    """Return True when the current process has Administrator/root privileges."""
    if sys.platform == "win32":
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        return bool(shell32.IsUserAnAdmin())
    geteuid = getattr(os, "geteuid", None)
    return geteuid() == 0 if geteuid is not None else False


def can_elevate() -> bool:
    """Return True when non-admin interactive execution can request OS elevation."""
    return not is_admin() and os.environ.get(ELEVATED_ENV) != "1" and forms.interactive()


def permission_error(action: str = "--volume") -> errors.PermissionError:
    """Return a `PermissionError` explaining how to run elevated."""
    hint = "re-run as Administrator" if sys.platform == "win32" else "re-run with sudo"
    return errors.PermissionError(f"{action} requires Administrator/root privileges", hint=hint)


def run_elevated(argv: Sequence[str]) -> int:
    """Re-launch `flnh` with Administrator/root privileges and return its exit code."""
    if os.environ.get(ELEVATED_ENV) == "1":
        raise permission_error()
    if sys.platform == "win32":
        return _run_win32_uac(argv)
    cmd = [
        "sudo",
        "env",
        f"{ELEVATED_ENV}=1",
        f"FLNH_HOME={state.home()}",
        sys.executable,
        "-m",
        "flnh",
        *argv,
    ]
    res = subprocess.run(cmd, check=False)
    return int(res.returncode)


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def _run_win32_uac(argv: Sequence[str]) -> int:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        req_file = tmp / "req.json"
        out_file = tmp / "stdout.txt"
        err_file = tmp / "stderr.txt"
        spec: dict[str, Any] = {
            "home": str(state.home()),
            "out": str(out_file),
            "err": str(err_file),
        }
        req_file.write_text(json.dumps(spec), encoding="utf-8")

        params = subprocess.list2cmdline(["-m", "flnh", "--_elevated", str(req_file), *argv])
        sei = _SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb = "runas"
        sei.lpFile = sys.executable
        sei.lpParameters = params
        sei.lpDirectory = os.getcwd()
        sei.nShow = SW_HIDE

        if not shell32.ShellExecuteExW(ctypes.byref(sei)):
            err = ctypes.get_last_error()
            if err == ERROR_CANCELLED:
                raise errors.PermissionError(
                    "administrator elevation was cancelled",
                    hint="accept the UAC prompt or run from an elevated terminal",
                )
            raise errors.disk(OSError(err, ctypes.FormatError(err)))

        code = wintypes.DWORD(1)
        try:
            if sei.hProcess:
                kernel32.WaitForSingleObject(sei.hProcess, INFINITE)
                kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
        finally:
            if sei.hProcess:
                kernel32.CloseHandle(sei.hProcess)

        if out_file.is_file():
            text = out_file.read_text(encoding="utf-8", errors="replace").rstrip("\r\n")
            if text:
                out.plain(text)
        if err_file.is_file():
            text = err_file.read_text(encoding="utf-8", errors="replace").rstrip("\r\n")
            if text:
                out.plain(text, err=True)
        return int(code.value)
