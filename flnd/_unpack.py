"""Unpack archives beside the archive after safety checks."""

from __future__ import annotations

from flncore.files import (  # noqa: F401
    CHUNK,
    DAMAGED,
    DRIVE,
    ENCRYPTED,
    STAGE,
    SUFFIXES,
    WIN_RESERVED,
    Entry,
    Kind,
    Limits,
    extract,
    folder,
    is_archive,
)

__all__ = ["Limits", "extract", "folder", "is_archive"]
