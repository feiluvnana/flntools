"""
The flnd command line: `flnd download`, `flnd auth set-cookie`, and prompt mode.

Usage:
  flnd download ins:example_user twt:example_handle
  cat list.txt | flnd download
  flnd auth set-cookie --path ~/Downloads/cookies.txt
  flnd                      # prompt mode on a terminal
"""

from pathlib import Path
from typing import Annotated, List, Optional

from flnd import __version__
from flnd.cookies import ask_cookie_path, store_cookies
from flnd.sources import SOURCES, get_source
from flnd.target import parse_target
from shared import cli, log, prompt

DOWNLOAD_DIR = Path(".flnd/downloads")


def describe() -> str:
    names = [source.name for source in SOURCES.values()]
    return f"Dead simple media downloader for {', '.join(names[:-1])}, and {names[-1]}."


def epilog() -> str:
    return f"""\
Target format:
  <source>:<want to download>
  Sources: {", ".join(SOURCES)}

Examples:
  flnd download ins:example_user
  flnd download tik:example_creator twt:example_handle
  cat list.txt | flnd download
  flnd auth set-cookie --path ~/Downloads/cookies.txt
"""


def ask_targets() -> List[str]:
    """Prompt mode: ask for one target at a time, so the user never types `source:value`."""
    sources = {source.name: code for code, source in SOURCES.items()}
    targets = []
    while True:
        source = prompt.choice("Source", sources)
        value = prompt.input("Username or id", validate=nonempty)
        targets.append(f"{source}:{value.strip()}")
        if not prompt.confirm("Add another target?", default=False):
            return targets


def nonempty(text: str) -> Optional[str]:
    return None if text.strip() else "enter a username or id"


app = cli.App("flnd", version=__version__, description=describe(), epilog=epilog(), promptmode=True)


@app.command
def download(targets: Annotated[List[str], cli.arg(nargs="+", ask=ask_targets)]) -> int:
    """Download each target.

    Args:
        targets: source:value pairs; read one per line from stdin when none are given.
    """
    try:
        parsed = [parse_target(raw) for raw in targets]
    except ValueError as err:
        log.error("{err}", err=err)
        return 1

    output = DOWNLOAD_DIR.resolve()
    log.info("Storage: {path}", path=output)
    total = 0
    with log.multi("download") as jobs:
        rows = [(target, jobs.add(str(target))) for target in parsed]
        for target, row in rows:
            try:
                total += get_source(target.source).download(
                    target=target, download_dir=output, limit=None, headless=False
                )
                row.finish()
            except Exception as err:
                log.error("{target}: {err}", target=target, err=err)
                row.fail()
    log.ok("Complete. {n} files", n=total)
    return 0


auth = app.group("auth", description="Cookie files for a source.")


@auth.command("set-cookie")
def set_cookie(
    path: Annotated[Optional[Path], cli.opt(ask=ask_cookie_path)] = None,
    content: Annotated[Optional[str], cli.opt(stdin=True)] = None,
) -> int:
    """Set a cookie file.

    Each cookie is stored under the source its domain belongs to.

    Args:
        path: Netscape cookies.txt or .json file.
        content: the cookie text itself; read from stdin when piped.
    """
    if path and content:
        raise cli.UsageError("give --path or --content, not both")
    if not path and not content:
        raise cli.UsageError("give --path, --content, or pipe the cookie text in")
    try:
        text = path.expanduser().read_text(encoding="utf-8") if path else content
        saved = store_cookies(text)
    except (OSError, UnicodeDecodeError, ValueError) as err:
        log.error("{err}", err=err)
        return 1
    for source, (count, stored) in saved.items():
        log.ok("{source}: {n} cookies saved to {path}", source=source, n=count, path=stored)
    return 0

