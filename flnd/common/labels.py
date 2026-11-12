"""`--classify`: label each saved photo and video, and group them in `labels/<label>/` link folders, never renaming them.

How it works: `Labeller` is one run's classifying. `labels` asks `Media.labels` what a file shows
and gives the label names, which the job records in the file's history row (`labels`); `link`
puts the file into `<target>/labels/<label>/` once per label, as a second name for it. The file
itself never moves and keeps its name (decided 2026-11-12), so its history row and every other
path stay true. The job labels each new download (flnd/post.py); `Engine.download` calls `apply`
for the files already saved when `--classify` is given, and `Engine.sync` calls `rebuild`.

A link is a **hard link**: the same file under a second name, which takes no space, opens in any
app and needs no rights on Windows. Where one can't be made it is a **symlink**, relative to its
folder, so the target folder can be moved whole. Where neither can (FAT and exFAT have neither),
the views are skipped for the run with one warning, and the labels are still recorded. Two files of
one name from different categories (`posts/a.jpg`, `tagged/a.jpg`) meet in one label folder as
`a.jpg` and `tagged_a.jpg`: the one linked second gets its category in front.

The views are rebuilt, never trusted: `rebuild` makes every link a row's labels ask for that is
missing, and removes every entry of `labels/` that isn't one of them (a broken symlink, a second
name of a file that lost that label). But a hard link is the file itself: when the file's own
name was deleted by hand, its link is its only copy. `restore` moves such a link back to the path
its history row names before anything is removed, and an entry that holds a last copy no row
names is never deleted. So neither `rebuild` nor `remove` (`--classify off`) loses a file.

    labeller = Labeller()
    names = labeller.labels(path)                   # ["food"], or None when classifying stopped
    if names is not None:
        row["labels"] = names
        labeller.link(path, folder, names)
    labels.rebuild(folder, rows, labeller)          # sync: put back what's missing, drop what's stale

What stops classifying for the whole run, with one warning: the `classify` extra missing, or the
model not downloaded (declined, no terminal, failed). A file `media` can't read is only skipped.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any, Iterable, MutableMapping, Sequence

from flnd.common import files
from shared import log

# The folder under a target's own that holds the views, one folder per label.
VIEWS = "labels"
# The history row's field: the label names of the file, best first; ["other"] when none fits.
FIELD = "labels"


class _NoLinks(OSError):
    """This file system makes neither hard links nor symlinks (FAT, exFAT, or a Windows share)."""


class Labeller:
    """One run's classifying: what stopped it, and whether this disk takes links, each warned of once.

        labeller = Labeller()
        labeller.link(path, folder, labeller.labels(path) or [])

    Several download threads use one at once: its flags change under a lock, and `Media.labels`
    shares one model between threads itself.
    """

    def __init__(self) -> None:
        # Why classifying stopped for the run, or None while it goes on.
        self.stopped: str | None = None
        # Set once neither kind of link could be made: the views are skipped for the rest of the run.
        self.unlinked = False
        self._lock = threading.Lock()

    def labels(self, path: Path) -> list[str] | None:
        """The label names of a photo or video, best first, or None when it couldn't be classified.

            labeller.labels(Path("posts/2026-01-02_C1abc.jpg"))      # ["food"]

        Args:
            path: the saved file, after compressing.

        Returns:
            The names `Media.labels` gives, `["other"]` when none fits; None when classifying has
            stopped for the run, or this file can't be read (logged at debug level: the
            download's own check already passed, so it is a file `media` can't label, not a
            broken one).

        It is slow the first time in a process (the model loads, and may be asked about and
        downloaded), then about 20 ms a photo; a video's frames need ffmpeg.
        """
        from shared import media

        if self.stopped is not None:
            return None
        try:
            found = media.open(path).labels()
        # The extra missing, or no model: the same for every file, so the run stops classifying
        # (classify.md › flnd) and says so once, keeping every file saved.
        except (ImportError, RuntimeError) as error:
            self._stop(str(error))
            return None
        # A file this machine can't read: media's own refusals, a timeout or a disk error. Only
        # this file goes unlabelled; the next may be fine.
        except (media.Error, ValueError, OSError) as error:
            log.debug("{name}: not classified: {error}", name=path.name, error=error)
            return None
        return [name for name, _ in found]

    def link(self, path: Path, folder: Path, names: Sequence[str]) -> None:
        """Link the file into `<folder>/labels/<name>/` for each label name, unless this disk takes no links.

            labeller.link(Path("…/example_user/posts/a.jpg"), Path("…/example_user"), ["food", "pet"])

        Args:
            path: the file, inside `folder`.
            folder: the target's folder, which holds `labels/`.
            names: the label names, as `labels` gives them.

        A link already there for this file is kept. A name another file holds gets the file's
        category in front (`tagged_a.jpg`); when that is taken too, the link is left out (logged at
        debug level). A disk with neither kind of link warns once and makes none for the run; any
        other disk error is logged at debug level for that link alone.
        """
        for name in names:
            if self.unlinked:
                return
            try:
                _place(path, folder, name)
            except _NoLinks as error:
                with self._lock:
                    if self.unlinked:
                        return
                    self.unlinked = True
                log.warn("labels/ views are skipped for this run: {folder} takes neither hard links nor "
                         "symlinks (FAT or exFAT?); the labels are still recorded: {error}",
                         folder=str(folder), error=error)
            # Any other failure (a full disk, a name too long) is this link's alone.
            except OSError as error:
                log.debug("{name}: not linked into labels/{label}: {error}", name=path.name, label=name, error=error)

    def _stop(self, reason: str) -> None:
        """Stop classifying for the run, warning once with why."""
        with self._lock:
            if self.stopped is not None:
                return
            self.stopped = reason
        log.warn("classify stops for this run: {reason}", reason=reason)


def apply(history: MutableMapping[str, Any], rows: Iterable[tuple[str, dict]], folder: Path,
          labeller: Labeller, *, label: str) -> int:
    """Label a target's saved photos and videos that have no labels yet, then rebuild its views.

        labels.apply(history, index[folder], folder, Labeller(), label="ins:example_user")

    Args:
        history: the download history, written with each file's `labels`.
        rows: the target's (key, row) history rows, as `repair.index` gives them.
        folder: the target's folder, which holds `labels/`.
        labeller: the classifying for this command.
        label: the target, for the progress row.

    Returns:
        How many files were labelled now.

    Raises:
        OSError: the history can't be written.

    What `--classify` does to the files already saved, when it is given: slow the first time (a
    model run per file), and only links afterwards. Stopped classifying (`Labeller.stopped`)
    leaves the remaining files unlabelled, for the next `download --classify`.
    """
    rows = list(rows)
    todo = [(key, row) for key, row in rows if FIELD not in row and files.kind_of(row["path"]) in (files.IMAGE, files.VIDEO)
            and Path(row["path"]).is_file()]
    done = 0
    if todo:
        with log.progress(f"--classify {label}", total=len(todo)) as bar:
            for key, row in todo:
                bar.advance()
                names = labeller.labels(Path(row["path"]))
                if labeller.stopped is not None:
                    break
                if names is not None:
                    history[key] = {**row, FIELD: names}
                    done += 1
    rebuild(folder, [(key, history.get(key, row)) for key, row in rows], labeller)
    return done


def rebuild(folder: Path, rows: Iterable[tuple[str, Any]], labeller: Labeller) -> None:
    """Make the target's `labels/` hold exactly the links its rows' labels ask for: add the missing, remove the rest.

        labels.rebuild(folder, index[folder], Labeller())

    Args:
        folder: the target's folder.
        rows: its (key, row) history rows; a row's `labels` and an existing `path` ask for links.
        labeller: the run's, so a disk without links warns once over every target.

    First `restore` puts back each file whose only copy is in `labels/`. Then an entry is kept when
    it is a row's file (the same inode, through a hard link or a symlink) under one of its labels,
    or when it holds a last copy no row names; any other is removed: a symlink whose file is gone,
    a second name of a file that lost that label or is linked twice. Empty label folders go, and
    `labels/` itself when it's empty.
    """
    rows = list(rows)
    kept = restore(folder, rows)
    wanted: dict[tuple[int, int], tuple[Path, set[str]]] = {}
    for _, row in rows:
        names = row.get(FIELD) if isinstance(row, dict) else None
        if not isinstance(names, list) or not names or not Path(row["path"]).is_file():
            continue
        path = Path(row["path"])
        found = path.stat()
        wanted[(found.st_dev, found.st_ino)] = (path, set(names))
    views = folder / VIEWS
    linked: set[tuple[tuple[int, int], str]] = set()
    for entry in sorted(views.glob("*/*")) if views.is_dir() else ():
        name = entry.parent.name
        try:
            found = entry.stat()
            inode = (found.st_dev, found.st_ino)
        # A symlink whose file is gone: nothing to keep.
        except OSError:
            inode = None
        if inode in wanted and name in wanted[inode][1] and (inode, name) not in linked:
            linked.add((inode, name))
            continue
        if entry in kept:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    for inode, (path, names) in wanted.items():
        labeller.link(path, folder, [name for name in sorted(names) if (inode, name) not in linked])
    _prune(views)


def remove(folder: Path, rows: Iterable[tuple[str, Any]] = ()) -> bool:
    """Delete a target's `labels/` folder, for `--classify off`, keeping every file; True when there was one.

        labels.remove(Path(".flnd/downloads/ins/example_user"), index[folder])

    Args:
        folder: the target's folder.
        rows: its (key, row) history rows, so a file whose only copy is its link goes back first.

    The links are second names of the files, so the files stay where they are; the history rows
    keep their labels, so turning `--classify` on again only links them. An entry holding a last
    copy that no row names stays, in what is left of `labels/`.
    """
    views = folder / VIEWS
    if not views.exists():
        return False
    kept = restore(folder, rows)
    if not kept:
        shutil.rmtree(views)
        return True
    for entry in views.glob("*/*"):
        if entry not in kept:
            entry.unlink(missing_ok=True)
    _prune(views)
    return True


def restore(folder: Path, rows: Iterable[tuple[str, Any]]) -> set[Path]:
    """Move each file whose only copy is in `labels/` back to its row's path, and give the last copies no row names.

        labels.restore(folder, index[folder])

    Args:
        folder: the target's folder.
        rows: its (key, row) history rows; a row with `labels` whose `path` is gone may be put back.

    Returns:
        The entries of `labels/` that hold the only copy of a file but match no row: never to be
        deleted, so `rebuild` and `remove` leave them.

    A hard link is the file, not a pointer to it, so deleting the file's own name by hand leaves
    its data only in `labels/`. Removing that "stale link" would delete the file for good, so it is
    moved back instead, onto the path its history row names (folders made as needed), and the row
    stays as it is. Every name of such a file being in `labels/` (its link count equals its names
    there) is what tells it apart from a link whose file still exists elsewhere. Symlinks hold no
    data and are never moved.
    """
    views = folder / VIEWS
    if not views.is_dir():
        return set()
    # Each (label, link name) a row's missing file would have, to the path it goes back to.
    missing: dict[tuple[str, str], Path] = {}
    for _, row in rows:
        names = row.get(FIELD) if isinstance(row, dict) else None
        if isinstance(names, list) and names and not os.path.lexists(row["path"]):
            path = Path(row["path"])
            for label in names:
                for name in _names(path, folder):
                    missing.setdefault((label, name), path)
    copies: dict[tuple[int, int], tuple[int, list[Path]]] = {}
    for entry in sorted(views.glob("*/*")):
        if entry.is_symlink() or not entry.is_file():
            continue
        found = entry.stat()
        copies.setdefault((found.st_dev, found.st_ino), (found.st_nlink, []))[1].append(entry)
    kept: set[Path] = set()
    for count, entries in copies.values():
        # A name outside `labels/` still holds the data: these are only links, which rebuild may drop.
        if count > len(entries):
            continue
        home = next(((entry, missing[(entry.parent.name, entry.name)]) for entry in entries
                     if (entry.parent.name, entry.name) in missing
                     and not os.path.lexists(missing[(entry.parent.name, entry.name)])), None)
        if home is None:
            log.debug("{name}: kept in labels/, the only copy of a file no history row names", name=entries[0].name)
            kept.update(entries)
            continue
        entry, path = home
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same target folder, so the same disk: a rename, which can't leave half a file.
        os.replace(entry, path)
        log.info("{name}: put back from labels/{label}, where its only copy was", name=path.name,
                 label=entry.parent.name)
    return kept


def _place(path: Path, folder: Path, label: str) -> Path | None:
    """Link `path` into `<folder>/labels/<label>/` under its name, else its category's, and return the link.

    Returns:
        The link, new or already there; None when both names hold other files.

    Raises:
        _NoLinks: neither a hard link nor a symlink could be made here.
        OSError: the folder can't be made.
    """
    directory = folder / VIEWS / label
    directory.mkdir(parents=True, exist_ok=True)
    for dest in (directory / name for name in _names(path, folder)):
        if _same(dest, path):
            return dest
        if dest.exists() or dest.is_symlink():
            continue
        try:
            _link(path, dest)
        # Another thread took the name between the look and the link: it may even be this file.
        except FileExistsError:
            if _same(dest, path):
                return dest
            continue
        return dest
    log.debug("{name}: labels/{label} has other files under its names", name=path.name, label=label)
    return None


def _link(path: Path, dest: Path) -> None:
    """A hard link at `dest` to `path`, else a symlink relative to `dest`'s folder.

    Raises:
        FileExistsError: `dest` was taken meanwhile.
        _NoLinks: neither works on this disk.
    """
    try:
        os.link(path, dest)
        return
    except FileExistsError:
        raise
    # Another volume (EXDEV), a file system without them (FAT, exFAT: EPERM or ENOTSUP), or too many
    # links to one file (EMLINK): a symlink can still do.
    except OSError:
        pass
    try:
        os.symlink(os.path.relpath(path, dest.parent), dest)
    except FileExistsError:
        raise
    # FAT and exFAT have no symlinks, and Windows makes them only in Developer Mode or as admin.
    except OSError as error:
        raise _NoLinks(error.errno, error.strerror) from None


def _names(path: Path, folder: Path) -> list[str]:
    """The names a file may have in a label folder: its own, then with its category in front (`tagged_a.jpg`)."""
    try:
        parts = path.relative_to(folder).parts
    except ValueError:
        parts = (path.name,)
    # The category is the first folder under the target's; a file in the target's own folder has none.
    return [path.name, f"{parts[0]}_{path.name}"] if len(parts) > 1 else [path.name]


def _same(dest: Path, path: Path) -> bool:
    """Whether `dest` is `path` itself, through a hard link or a symlink; False when it doesn't exist or is broken."""
    try:
        return os.path.samefile(dest, path)
    except OSError:
        return False


def _prune(views: Path) -> None:
    """Remove empty label folders, then `labels/` itself when it's empty."""
    if not views.is_dir():
        return
    for directory in views.iterdir():
        if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()
    if not any(views.iterdir()):
        views.rmdir()
