"""flnd: a dead simple media downloader, and its engine API (plan/phase-6/redesign.md, B1).

These seven names are the whole API: what flnd/cli.py calls, and what tests and scripts import.
`Engine` has one method per command; `download` and `sync` return a `Report`; options take the
command line's text or a value, `Range` for `--time` and `Choice` for `--image`, `--video` and
`--audio`; `resolve` raises `TargetError` for a target it can't read.

    from flnd import Engine

    engine = Engine(home=Path(".flnd"))
    report = engine.download(["ins:example_user"], categories="reels", time="2026-01-01,", image="webp,mid")
    exit_status = 0 if report.ok else 1
"""

# First, before the engine is imported: the sources and the CLI read it while `flnd` is still loading.
__version__ = "0.1.0"

from flnd.common.compress import Choice  # noqa: E402
from flnd.engine import Engine, Report, TargetReport  # noqa: E402
from flnd.source import Range, Target, TargetError  # noqa: E402

__all__ = ["Choice", "Engine", "Range", "Report", "Target", "TargetError", "TargetReport"]
