"""The options of one run, and the settings every source shares: what `--from`, `--time` and compression ask.

`Options` is what a run downloads and how it compresses it; `Settings` is what the run shares across
its sources (flnd's folder, the options, the history and the compressor). The engine reads both, the
job and the sources' sites through them, and `Engine.download` checks `--from` with the helpers here.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

from flnd.common.compress import Compressor
from flnd.source import Source, Target

# `--from all` takes every category of every source.
ALL = "all"


@dataclass(frozen=True)
class Options:
    """What one run downloads and how it compresses it: `Options(categories=("reels",), shrink=True)`.

    The same for every target of one `Engine._run`; `Engine.download` and `Engine.sync` run targets
    whose saved settings differ separately. It is hashable, so it can key those groups.
    """

    # `--from`: None (or "all") means every category of each source.
    categories: tuple[str, ...] | None = None
    # `--time`'s two days, both included; None is no limit on that side.
    since: datetime.date | None = None
    until: datetime.date | None = None
    # `--shrink`: compress each new photo, video and audio file at media's balanced level, in its own format.
    shrink: bool = False


def selected(options: Options, source: type[Source]) -> tuple[str, ...]:
    """The categories `--from` takes from one source, in the source's order.

        selected(Options(categories=("reels", "media")), Instagram)     # ("reels",)

    Without `--from`, or with `all`, it is every category. A name the source doesn't have is
    ignored; when none of the names is the source's, it takes every category, as without `--from`.
    """
    names = options.categories
    if names is None or ALL in names:
        return source.categories
    return tuple(name for name in source.categories if name in names) or source.categories


def unknown_categories(names: Sequence[str], sources: Sequence[type[Source]]) -> str | None:
    """Why `--from names` can't be used with these sources, listing the names per source; None when it can.

    Args:
        names: the names `--from` gave, after splitting on commas; `all` is always known.
        sources: the sources the targets belong to, whose categories are the known names.

    Returns:
        The message the command shows as a usage error, or None when every name is known.
    """
    known = {category for source in sources for category in source.categories}
    unknown = [name for name in names if name != ALL and name not in known]
    if not unknown:
        return None
    lists = "; ".join(f"{source.code}: {', '.join(source.categories)}" for source in sources)
    return f"no source here has {', '.join(map(repr, unknown))}: use all, or {lists}"


def single_targets(targets: Sequence[Target], sources: Mapping[str, type[Source]]) -> str | None:
    """Why `--from` can't be used with these targets, naming the first single post; None when it can.

        single_targets([Target("ins", "post", "C1abc")], SOURCES)     # "--from applies to profiles and chats; …"

    A post has no categories: tagged or reposts describe a profile's tab, not a post. Filtering
    one by `--from` would only ever drop every file, so the command refuses it before it starts.
    """
    for target in targets:
        if target.kind in sources[target.source].single_kinds:
            return (f"--from applies to profiles and chats; {target} is one post, downloaded whole: "
                    "give it without --from")
    return None


@dataclass
class Settings:
    """What one run shares across every source: flnd's folder, the options, the history and the compressing."""

    # flnd's folder, where the downloads, history, logins and settings are kept.
    home: Path
    # What the run downloads and how it compresses it, shared by every source.
    options: Options
    # The history of downloads, one row per file key, shared by every source of the run.
    history: MutableMapping[str, Any]
    # Whether a person can be asked a question, which a source's own login needs.
    interactive: bool = False
    # One for the whole run, so videos encode one at a time across sources and each error warns once.
    compressor: Compressor = field(default_factory=Compressor)
