"""Dry run of one target: list what it would save without downloading or recording."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

from rich.tree import Tree

from flnd import job
from flnd.engine import ACCOUNT, Engine, _connected
from flnd.options import Options, RunShared, unknown
from flnd.site import TargetSite, shared_browser, source_run
from flnd.source import File, Range, Source, Target
from flnd.sources import SOURCES
from flnd import _cli as cli, _text as text
from flnd._ui import ui

__all__ = ["main", "plan", "tree"]


def plan(
    home: Path,
    target_text: str,
    *,
    categories: str | Sequence[str] | None = None,
    time: Range | str = Range(),
    sources: Mapping[str, type[Source]] = SOURCES,
) -> Iterator[job.Placed]:
    """Yield every file a download of one target would save, without writing anything."""
    engine = Engine(home=home, sources=sources)
    target = engine.resolve([target_text])[0]
    names = _parse_names(categories)
    source = engine.sources[target.source]
    _validate_dry(names, target, source)
    span = Range.parse(time) if isinstance(time, str) else time
    options = Options(categories=names, since=span.since, until=span.until)
    with cli.stopping() as stop:
        settings = RunShared(home=home, history={}, stop=stop, dry=True)
        with shared_browser(settings), source_run(settings, source()) as run:
            account = TargetSite(run, Target(source.code, ACCOUNT, ""))
            if not run.source.logged_in(account) and run.source.requires_login:
                raise cli.UsageError(f"{source.code}: not logged in; run flnd auth login")
            with _connected(run, account):
                yield from job.plan(run, target, options)


def tree(placed: Iterable[job.Placed]) -> Iterator[str]:
    """Format planned files as indented text lines grouped by category and post."""
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


def render_tree(target_text: str, placed: Iterable[job.Placed]) -> Tree:
    """Build a Rich `Tree` of planned files grouped by category and post."""
    root = Tree(target_text)
    cat_node: Tree | None = None
    post_node: Tree | None = None
    category = post = None
    for item in placed:
        if item.post.category != category:
            category, post = item.post.category, None
            cat_node = root.add(category) if category else root
        if item.post is not post:
            post = item.post
            parent = cat_node if cat_node is not None else root
            post_node = parent.add(f"{post.id}  {item.values['date'] or 'undated'}")
        target_node = post_node if post_node is not None else root
        target_node.add(f"{item.path}  [dim]{_where(item.file)}[/dim]")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    """Print the post tree of one target and return 0, or 1 on failure."""
    parser = argparse.ArgumentParser(
        prog="make try", description="List what a download would save, without downloading."
    )
    parser.add_argument("target")
    parser.add_argument("--from", dest="categories", default=None, metavar="CATEGORIES")
    parser.add_argument("--time", default="", metavar="SINCE,UNTIL")
    args = parser.parse_args(argv)
    try:
        span = Range.parse(args.time) if args.time else Range()
        placed = plan(Path(".flnd"), args.target, categories=args.categories, time=span)
        ui.raw.print(render_tree(args.target, placed))
    except ValueError as error:
        ui.error("{error}", error=error)
        return 1
    except Exception as error:
        ui.error("{kind}: {error}", kind=type(error).__name__, error=error)
        return 1
    return 0


def _parse_names(categories: str | Sequence[str] | None) -> tuple[str, ...] | None:
    items = [categories] if isinstance(categories, str) else list(categories or ())
    names = tuple(dict.fromkeys(n.strip() for item in items for n in item.split(",") if n.strip()))
    return names or None


def _validate_dry(
    names: tuple[str, ...] | None, target: Target, source: type[Source],
) -> None:
    if not names:
        return
    if target.kind in source.single_kinds:
        raise cli.UsageError(
            f"--from applies to profiles and chats; {target} is one post, downloaded whole"
        )
    if problem := unknown(names, [source]):
        raise cli.UsageError(problem)


def _where(file: File) -> str:
    """Describe where a file comes from without exposing query parameters."""
    if file.content is not None:
        return f"(text, {len(file.content)} characters)"
    if isinstance(file.url, str):
        found = text.redact(file.url)
    elif isinstance(file.url, tuple) and len(file.url) > 0:
        found = text.redact(file.url[0])
        if len(file.url) > 1:
            found += f" (+{len(file.url) - 1} mirrors)"
    else:
        found = "(fetched by the source)"
    return f"{found} + audio {text.redact(file.audio)}" if file.audio else found


if __name__ == "__main__":
    raise SystemExit(main())
