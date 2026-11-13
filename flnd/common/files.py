"""flnd's files on disk: the layout of `.flnd/`, where a target's go, what kind each is, and one media check.

`.flnd/` holds three folders (plan/phase-7/layout.md): `media/` with every target's files,
`metadata/` with every store and the vault's key, and `log/` with one log per run. Every module
takes its path from the table below, joined onto flnd's folder, and never names a file itself.

Every temporary file flnd writes comes from `shared.atomic`, which names it `.<name>.<random>.part`
in the folder of the file it becomes and renames it onto that file only once complete. So a reader
never sees half a file, and whatever a killed run leaves behind matches `PART_GLOB`, which
`flnd sync`'s repair removes. `net` streams downloads into such parts; `Session.write` writes small
files whole with `atomic.write`.

`checked` is the one media validation, used by `net` for each download and by `sync` for each
file it repairs, so both sniff the same way and treat the same errors as "not checkable here".

    extension = files.checked(folder / "a.jpg", "image")    # ".webp" when it holds a WebP
    files.target_folder(Path(".flnd"), target)              # .flnd/media/ins/example_user
    Path(".flnd") / files.HISTORY                           # .flnd/metadata/history.db
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
# Each store in `metadata/`: every saved file by key, each target's settings and complete listings,
# each source's own state (a table per source), and the vault with the key beside the store it opens.
HISTORY = METADATA / "history.db"
TARGETS = METADATA / "targets.db"
SOURCE_STATE = METADATA / "sources.db"
SECRETS = METADATA / "secrets.db"
KEY = METADATA / "secret.key"

# What every temporary file flnd writes matches (`shared.atomic`'s part names), and what `flnd sync`'s repair deletes.
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


def part_info(part: Path) -> Path:
    """The `.part.json` sidecar a resumable download's part has beside it, if it has one."""
    return part.with_name(part.name + PART_INFO_SUFFIX)


def resumable(part: Path) -> bool:
    """Whether a leftover part is a download to resume: it has its sidecar, and was written in the last `RESUMABLE_DAYS`."""
    try:
        age = time.time() - part.stat().st_mtime
    except OSError:  # gone already, or unreadable: nothing to resume
        return False
    return part_info(part).is_file() and age < RESUMABLE_DAYS * 24 * 3600


class NotMediaError(ValueError):
    """A web page or JSON where a photo, video or audio file was expected: a login wall or an error page."""


def kind_of(name: str) -> str | None:
    """What a saved file is by its name: `IMAGE`, `VIDEO` or `AUDIO`; None for anything else, such as an archive.

        files.kind_of("posts/a.heic")     # "image"

    Args:
        name: a file name or path; only its extension is read, through `media.kind`, the one table.
    """
    from shared import media

    return media.kind(Path(name).suffix)


def target_folder(home: Path, target: Target) -> Path:
    """A target's folder: `<home>/media/<code>/<value>`, its value made a safe file name.

        files.target_folder(Path(".flnd"), Target("ins", "user", "example_user"))   # .flnd/media/ins/example_user

    Args:
        home: flnd's folder, `.flnd`.
        target: the target, as `Engine.resolve` gives it.
    """
    return home / MEDIA / target.source / fmt.filename(target.value)


def checked(path: Path, kind: str, *, decode: bool = True) -> str | None:
    """The extension a photo, video or audio file's content calls for, once it is known not to be a web page or broken.

        extension = files.checked(Path("posts/a.jpg"), "image")    # ".webp" when it holds a WebP

    Args:
        path: the file to check; it is never changed.
        kind: what the file should be, `IMAGE`, `VIDEO` or `AUDIO`, named in the error for a web page.
        decode: whether to check the whole file with `Media.check` (a photo decoded, a video's packets
            read); False only sniffs its first bytes, for a download whose size was already verified.

    Returns:
        The extension `media.sniff` reads from the first bytes (".jpg", ".mp4"), or None when it knows
        none. A file that can't be checked here counts as fine: see below.

    Raises:
        NotMediaError: the file is a web page or JSON, such as a login wall.
        media.BrokenError: the file is truncated or corrupt.
        media.UnsafeError: media refuses to open it, such as a decompression bomb.

    Without ffmpeg (`RuntimeError`), without the media extra (`ImportError`), or when ffmpeg stalls
    (`TimeoutError`), the file isn't known to be broken, so it is logged at debug level and kept.
    """
    from shared import media

    extension = media.sniff(path)
    if extension in PAGES:
        raise NotMediaError(f"got a web page instead of {NOUNS.get(kind, kind)}")
    if decode:
        try:
            media.open(path).check()
        except (RuntimeError, ImportError, TimeoutError) as error:
            # These say the check can't run here, not that the file is bad; BrokenError and UnsafeError
            # are about the file itself, so they reach the caller, which decides whether to retry or delete.
            log.debug("{name}: not checked: {error}", name=path.name, error=error)
    return extension
