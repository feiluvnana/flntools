"""The home's layout: a target's folder under `media/`, and the samples and crash files under
`log/`, with their sweep. src/docs/storage explains it.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from flnd.engine.store import MEDIA
from flnd.ui.log import ui
from flnkit import text

if TYPE_CHECKING:
    from flnd.plugin import Target

__all__ = ["sample", "sweep", "target_folder"]

# ---------------------------------------------------------------------------------------------
# The home's layout
# ---------------------------------------------------------------------------------------------

# Under the home: media/ for downloads, metadata/ for stores (flnd.db and secret.db), and
# log/ for run logs and crash reports.
LOGS = Path("log")

# Logs and samples older than this are swept at the start of each run that downloads.
LOG_DAYS = 30
# A response whose URL looks like this is a login's, and is never kept as a sample: its body may
# hold the session it just made.
LOGIN_URL = re.compile(r"login|logon|signin|sign_in|oauth|auth|session|token|password|account",
                       re.IGNORECASE)


def target_folder(home: Path, target: Target) -> Path:
    """A target's folder: `<home>/media/<code>/<value>`, with the value made a safe name.

    Args:
        home: the home folder.
        target: the target, resolved.

    Returns:
        The folder; nothing is made on disk.

    Example:
        >>> from flnd.plugin import Target
        >>> target_folder(Path("home"), Target("ins:example_user", kind="user")).as_posix()
        'home/media/ins/example_user'
    """
    return home / MEDIA / target.plugin / text.filename(target.value)


# ---------------------------------------------------------------------------------------------
# Logs and samples
# ---------------------------------------------------------------------------------------------

def sample(home: Path, run: int | None, error: BaseException) -> Path | None:
    """Keep the answer a `Changed` error was raised on, scrubbed, in `logs/<run>/samples/`.

    A parse failure is a site that changed or a source's bug, and the answer is what shows
    which. It is kept only when it is safe to:
    - values under keys that look like secrets (`token`, `csrf`, `session`, `cookie`, `auth`,
      `password`, `key`, …) become "…" (`flnkit.text.scrub`);
    - a login's answer is never kept: one whose URL looks like a login (`LOGIN_URL`), or that
      sets a cookie, may hold the session it made;
    - the file is named by a count, never by anything the site sent.

    Args:
        home: the home folder.
        run: the run's id; None (outside a recorded run) keeps nothing.
        error: what failed; anything but a `Changed` with a response keeps nothing.

    Returns:
        The sample's path; None when nothing was kept.
    """
    from flnd.errors import Changed

    response = getattr(error, "response", None)
    if run is None or not isinstance(error, Changed) or response is None:
        return None
    url = str(getattr(response, "url", "") or "")
    headers = getattr(response, "headers", None) or {}
    if LOGIN_URL.search(text.redact(url)) or "set-cookie" in {str(k).lower() for k in headers}:
        return None
    content = getattr(response, "content", b"")
    body = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    folder = home / LOGS / str(run) / "samples"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        count = len(list(folder.iterdir()))
        while True:
            count += 1
            path = folder / f"{count}.txt"
            try:
                # An exclusive create, so two samples kept at once never take the same name.
                with path.open("x", encoding="utf-8") as out:
                    out.write(f"{text.redact(url)}\n\n{text.scrub(body)}")
                break
            except FileExistsError:
                continue
    except OSError as failed:
        # A sample is a help for a bug report, never worth failing the run over.
        ui.debug("sample not kept: {error}", error=failed)
        return None
    return path


def sweep(home: Path, *, days: int = LOG_DAYS) -> int:
    """Remove the logs older than `days`: each run's folder (its samples) and crash files.

    Called at the start of each run that downloads, so a home that runs every day keeps a
    month of them. An entry that can't be removed is left for the next sweep.

    Args:
        home: the home folder.
        days: how old, by its modified time, an entry may get.

    Returns:
        How many entries were removed.

    Example:
        >>> import tempfile
        >>> sweep(Path(tempfile.mkdtemp()))
        0
    """
    logs = home / LOGS
    if not logs.is_dir():
        return 0
    oldest = time.time() - days * 86_400
    removed = 0
    for entry in logs.iterdir():
        try:
            if entry.lstat().st_mtime >= oldest:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as error:
            ui.debug("{name}: not swept: {error}", name=entry.name, error=error)
    return removed
