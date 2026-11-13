"""A dry run of one target: the real listing, and what it would save, with nothing downloaded or recorded.

This is gallery-dl's `--simulate` and `--get-urls` (plan/phase-6/redesign.md, A11, B5). It is the
developer task `scripts/dev.py try`, not a `flnd` option: the source runs for real (network,
browser, login) with the saved login of the `.flnd` it is given, and the post tree comes out as
text. No file is written, no history row or setting is recorded, no cookie is written back, and a
URL is printed without its query, so no signed token reaches a terminal or a log.

    python -m flnd.dry ins:example_user --from reels --time 2026-01-01,
    for line in tree(plan(Path(".flnd"), "ins:example_user")): print(line)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

from flnd import job
from flnd.engine import ACCOUNT, Engine, _connected, _offer_browser, needs_browser
from flnd.session import Options, Settings, single_targets, unknown_categories
from flnd.site import TargetSite, source_run
from flnd.source import File, Range, Source, Target
from flnd.sources import SOURCES
from shared import fmt

__all__ = ["main", "plan", "tree"]


def plan(home: Path, text: str, *, categories: str | Sequence[str] | None = None,
         time: Range | str = Range(), sources: Mapping[str, type[Source]] = SOURCES) -> Iterator[job.Placed]:
    """Every file a download of one target would save, listed for real and not downloaded.

        for placed in plan(Path(".flnd"), "ins:example_user", categories="reels"):
            print(placed.path)

    Args:
        home: flnd's folder; its saved login is used, and nothing is written in it.
        text: the target, as `flnd download` takes it.
        categories: `--from`'s names, a comma list or a sequence; None for every category.
        time: `--time`, as text or a `Range`.
        sources: the sources to read the target against; every source flnd has by default.

    Raises:
        ValueError: the target or an option can't be read, or the source isn't logged in.
        Whatever the source's listing raises, such as `FetchError`.
    """
    engine = Engine(home=home, sources=sources)
    target = engine.resolve([text])[0]
    items = [categories] if isinstance(categories, str) else list(categories or ())
    names = tuple(dict.fromkeys(name.strip() for item in items for name in item.split(",") if name.strip())) or None
    source = engine.sources[target.source]
    if names and (problem := unknown_categories(names, [source]) or single_targets([target], engine.sources)):
        raise ValueError(problem)
    span = Range.parse(time) if isinstance(time, str) else time
    options = Options(categories=names, since=span.since, until=span.until)
    if needs_browser(source, target, options):
        _offer_browser()
    # Never the real history: a file already saved still shows, and nothing is recorded.
    settings = Settings(home=home, options=options, history={})
    with source_run(settings, source()) as run:
        account = TargetSite(run, Target(source.code, ACCOUNT, ""))
        if not run.source.logged_in(account) and run.source.requires_login:
            raise ValueError(f"{source.code}: not logged in; run flnd login")
        with _connected(run, account):
            yield from job.plan(run, target)


def tree(placed: Iterable[job.Placed]) -> Iterator[str]:
    """The files as a tree, one line each as they come: the category, then each post, then its files.

        ins:example_user
          reels
            C1abc  2026-01-02
              reels/2026-01-02_C1abc_0.jpg  https://cdn.example.com/a.jpg

    Every URL is shown by `fmt.url`, without its query, and a source's own fetch function or a text file is named as such.
    """
    category = post = None
    for item in placed:
        if item.post.category != category:
            category, post = item.post.category, None
            if category:
                yield f"  {category}"
        indent = "    " if category else "  "
        if item.post is not post:
            post = item.post
            yield f"{indent}{post.id}  {item.values['date'] or 'undated'}"
        yield f"{indent}  {item.path}  {_where(item.file)}"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the post tree of one target, and return the exit status: 0, or 1 when it fails.

        main(["ins:example_user", "--from", "reels", "--time", "2026-01-01,"])
    """
    parser = argparse.ArgumentParser(prog="dev.py try", description="List what a download would save, without downloading.")
    parser.add_argument("target")
    parser.add_argument("--from", dest="categories", default=None, metavar="CATEGORIES")
    parser.add_argument("--time", default="", metavar="SINCE,UNTIL")
    args = parser.parse_args(argv)
    print(args.target)
    try:
        for line in tree(plan(Path(".flnd"), args.target, categories=args.categories,
                              time=Range.parse(args.time) if args.time else Range())):
            print(line, flush=True)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # a source's own failure (FetchError, LoginError): shown, not a traceback
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


def _where(file: File) -> str:
    """Where a file comes from, for the tree: its URL without the query, its mirrors counted, or what else it is."""
    if file.content is not None:
        return f"(text, {len(file.content)} characters)"
    if isinstance(file.url, str):
        found = fmt.url(file.url)
    elif isinstance(file.url, tuple):
        found = fmt.url(file.url[0]) + (f" (+{len(file.url) - 1} mirrors)" if len(file.url) > 1 else "")
    else:
        found = "(fetched by the source)"
    return f"{found} + audio {fmt.url(file.audio)}" if file.audio else found


if __name__ == "__main__":
    sys.exit(main())
