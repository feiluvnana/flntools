"""flnh error hierarchy over `flnkit.errors` (`SPEC.md#cli-and-exit-codes`)."""

from __future__ import annotations

from flnkit.errors import DiskError, Error, Stopped, UsageError, disk

__all__ = [
    "AuthError",
    "BusyError",
    "CorruptHeaderError",
    "DiskError",
    "Error",
    "PermissionError",
    "SafetyError",
    "Stopped",
    "UnsupportedError",
    "UsageError",
    "disk",
]


class SafetyError(Error):
    """Blocked by a safety guard (OS/boot/swap disk or root directory)."""


class PermissionError(Error):
    """Administrator/root privilege or ACL permission required."""


class BusyError(Error):
    """Volume or target is in use and cannot be locked or unmounted."""


class AuthError(Error):
    """Wrong password or keyfile."""


class CorruptHeaderError(Error):
    """Vault, partition table, or tail header is corrupt or tampered."""


class UnsupportedError(Error):
    """Filesystem or platform feature is not supported for this level."""
