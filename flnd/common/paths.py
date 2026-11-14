"""flnd's files on disk: layout of `.flnd/`, target directories, and media validation.

`.flnd/` holds three folders: `media/` with every target's files, `metadata/` with stores
and secrets, and `log/` with one log per run. Every module takes its path from the table
below, joined onto flnd's home folder, and never hardcodes paths.

Every temporary file matches `PART_GLOB`, which `flnd sync`'s repair removes.
`net` streams downloads into temporary parts; `Session.write` writes small files atomically.

`checked` provides media validation used by `net` and `sync`.

    extension = paths.checked(folder / "a.jpg", "image")
    paths.target_folder(Path(".flnd"), target)
    Path(".flnd") / paths.HISTORY
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from shared import fmt, log

if TYPE_CHECKING:
    from flnd.source import Target

# `.flnd/`'s three folders: the media, the stores, and a log of each run.
MEDIA, METADATA, LOGS = Path("media"), Path("metadata"), Path("log")
# Each store in `metadata/`: saved files by key, target settings, complete listings,
# source state (table per source), and the vault with the key beside the store it opens.
HISTORY = METADATA / "history.db"
TARGETS = METADATA / "targets.db"
SOURCE_STATE = METADATA / "sources.db"
SECRETS = METADATA / "secrets.db"
KEY = METADATA / "secret.key"

# Temporary files match (`shared.atomic`'s part names) and `flnd sync`'s repair deletes them.
PART_GLOB = ".*.part"
# What a resumable download's part has beside it (`shared.http.download`'s sidecar: its URL,
# validator, size and ranges), named after the part; and what finds every such sidecar.
PART_INFO_SUFFIX = ".json"
PART_INFO_GLOB = ".*.part.json"
# How long `flnd sync`'s repair keeps a resumable part, in days since it was last written: long
# enough to resume after a week away, short enough that a file never asked for again goes.
RESUMABLE_DAYS = 7
# What `media.sniff` names a server's web page or JSON answer: a login wall or an error page.
PAGES = (".html", ".json")
# The kinds of saved file, in `media.kind`'s words; each gets its extension from its content.
IMAGE, VIDEO, AUDIO = "image", "video", "audio"
# The kind `net.download` takes for a file whose content decides what it is: a post's file named
# without an extension, because neither its name nor its URL said.
SNIFF = "sniff"
# How an error names each kind.
NOUNS = {IMAGE: "a photo", VIDEO: "a video", AUDIO: "an audio file"}
# The history fields of a finished file's size and modified time (ns), which `flnd sync` trusts
# while the file still has them.
SIZE, MTIME = "size", "mtime"


def part_info(part: Path) -> Path:
    """The `.part.json` sidecar a resumable download's part has beside it, if it has one."""
    return part.with_name(part.name + PART_INFO_SUFFIX)


def resumable(part: Path) -> bool:
    """Whether a leftover part is a recent download with a sidecar to resume."""
    try:
        age = time.time() - part.stat().st_mtime
    except OSError:  # gone already, or unreadable: nothing to resume
        return False
    return part_info(part).is_file() and age < RESUMABLE_DAYS * 24 * 3600


def stamp(path: Path) -> dict[str, int]:
    """A finished file's size and modified time (ns), as its history row keeps them."""
    found = path.stat()
    return {SIZE: found.st_size, MTIME: found.st_mtime_ns}


def unchanged(path: Path, row: dict) -> bool:
    """Whether a file still has the size and modified time its history row keeps."""
    found = path.stat()
    return row.get(SIZE) == found.st_size and row.get(MTIME) == found.st_mtime_ns


class NotMediaError(ValueError):
    """A web page or JSON where media was expected (e.g. login wall or error page)."""


def kind_of(name: str) -> str | None:
    """What a saved file is by its name: `IMAGE`, `VIDEO` or `AUDIO`, or None.

        paths.kind_of("posts/a.heic")     # "image"
    """
    from shared import media

    return media.kind(Path(name).suffix)


def target_folder(home: Path, target: Target) -> Path:
    """A target's folder: `<home>/media/<code>/<value>`, with value made safe.

        paths.target_folder(Path(".flnd"), Target("ins", "user", "example_user"))
    """
    return home / MEDIA / target.source / fmt.filename(target.value)


def checked(path: Path, kind: str, *, decode: bool = True) -> str | None:
    """The extension a media file calls for, once validated not a web page or corrupt."""
    from shared import media

    extension = media.sniff(path)
    if extension in PAGES:
        raise NotMediaError(f"got a web page instead of {NOUNS.get(kind, kind)}")
    if decode:
        try:
            media.open(path).check()
        except (RuntimeError, ImportError, TimeoutError) as error:
            # These say check cannot run here; BrokenError and UnsafeError reach caller to
            # decide whether to retry or delete.
            log.debug("{name}: not checked: {error}", name=path.name, error=error)
    return extension
