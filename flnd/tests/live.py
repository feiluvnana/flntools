"""`make live ARGS=<code>`: the hand check on the test accounts (`~/.config/flnd/live.toml`).

Runs each configured route of a plugin with `dry=True` (downloading nothing) and checks that
every category yields at least one file, every template field was filled, and dates parsed.
Never part of `make check`, and no test handle or cookie ever lives in the repository.

Example `~/.config/flnd/live.toml` (outside the repo):

    home = ".try"
    limit = 5

    [ins]
    user = "ins:example_user"
    post = "https://www.example.com/p/C1abc/"
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import flnd
from flnd.engine import crawl
from flnd.engine.events import Failed, Planned, Saved
from flnd.plugin import Plugin
from flnd.plugins._manifest import PLUGINS
from flnkit import text

DEFAULT_CONFIG = Path.home() / ".config" / "flnd" / "live.toml"
CONFIG_VARIABLE = "FLND_LIVE_CONFIG"
DEFAULT_HOME = ".try"
DEFAULT_LIMIT = 5
OPTION_KEYS = frozenset({"home", "limit", "only", "skip", "undated"})
UNDATED_CATEGORIES = frozenset({"profile", "cover"})
OPTIONAL_FIELDS = frozenset({
    "album", "category", "extension", "idx", "index", "part", "path", "query", "title",
})
UNFILLED = re.compile(r"\{[^{}]*\}")


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one route (or one category of a route) in a live check."""

    code: str
    route: str
    category: str
    target: str
    planned: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether at least one file was planned with no failures or template/date errors."""
        return self.planned > 0 and not self.errors


@dataclass(frozen=True)
class _Recorded:
    """One `Placed` item observed during a dry run, with the active templates."""

    placed: crawl.Placed
    folder: str
    filename: str
    key: str
    category: str
    # What the source's `date` hook said for an undated post (the engine asks it when it saves,
    # never in a dry run): "" not asked, "dated", "none", or "failed: <why>".
    asked: str = ""


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse `make live` arguments."""
    parser = argparse.ArgumentParser(prog="make live", description=__doc__.splitlines()[0])
    parser.add_argument("codes", nargs="*", help="plugin codes to check (default: all in config)")
    parser.add_argument(
        "--config", type=Path, help=f"path to live.toml (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument("--home", help=f"flnd home folder (default: config, else {DEFAULT_HOME})")
    parser.add_argument("--limit", type=int, help=f"posts per check (default: {DEFAULT_LIMIT})")
    parser.add_argument("--route", help="only run this route name")
    parser.add_argument("--only", help="comma-separated categories to check")
    return parser.parse_args(argv)


def config_path(explicit: Path | None = None) -> Path:
    """Resolve the live config file path without touching `~/.config/flnd` when overridden."""
    if explicit is not None:
        return explicit.expanduser()
    env = os.environ.get(CONFIG_VARIABLE)
    return Path(env).expanduser() if env else DEFAULT_CONFIG


def load_config(path: Path) -> dict[str, Any]:
    """Read `live.toml`; raise `ValueError` with an example if missing or invalid."""
    if not path.is_file():
        raise ValueError(
            f"{path} not found; create it outside the repo with test handles, e.g.:\n"
            '[ins]\nuser = "ins:example_user"'
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a TOML table")
    return data


def _string_list(value: Any, name: str) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item.strip() for item in value if item.strip()]
    raise ValueError(f"{name} must be a string or list of strings")


def _route_specs(code: str, plugins: Sequence[type[Any]] = ()) -> dict[str, list[str]]:
    """Return `{route_name: [category_name, ...]}` for `code` from manifest or added plugins."""
    for cls in plugins:
        if getattr(cls, "code", None) == code:
            return {
                route.kind: [cat.name for cat in route.categories]
                for route in cls.routes
            }
    entry = PLUGINS.get(code)
    if entry is None:
        raise ValueError(f"unknown plugin {code!r}")
    routes: Sequence[Mapping[str, Any]] = entry["routes"]  # type: ignore[assignment]
    return {
        str(route["name"]): [str(cat["name"]) for cat in route.get("categories", ())]
        for route in routes
    }


@contextlib.contextmanager
def _capture_placed(*, ask: bool = True) -> Iterator[list[_Recorded]]:
    """Observe `crawl.place` calls during a `dry=True` run without altering outputs.

    With `ask`, an undated post of a source with a `date` hook is also asked that hook, as the
    engine does when it saves, so a live check tests the hook too.
    """
    recorded: list[_Recorded] = []
    original = crawl.place
    original_files = crawl._files
    asked: dict[str, str] = {}

    async def files(run: Any, site: Any, post: Any) -> Any:
        listed = await original_files(run, site, post)
        hooked = type(run.source).date is not Plugin.date
        if ask and hooked and post.date is None and post.category not in UNDATED_CATEGORIES:
            try:
                async with site.aside():
                    found = await run.source.date(site, post)
                asked[post.id] = "dated" if found is not None else "none"
            except Exception as error:
                asked[post.id] = f"failed: {error}"
        return listed

    def wrapped(
        source: Any, post: Any, files: Any, *, category: Any = None, route: Any = None,
        target: Any = None, options: Any = None,
    ) -> list[crawl.Placed]:
        result = original(
            source, post, files, category=category, route=route, target=target, options=options,
        )
        folder = crawl._folder_format(source, route, category)
        filename = crawl._format(source, category, "filename")
        key = crawl._format(source, category, "key")
        cat_name = category.name if category is not None else post.category
        for item in result:
            recorded.append(_Recorded(item, folder, filename, key, cat_name,
                                      asked.get(post.id, "")))
        return result

    crawl.place = wrapped  # type: ignore[assignment]
    crawl._files = files  # type: ignore[assignment]
    try:
        yield recorded
    finally:
        crawl.place = original  # type: ignore[assignment]
        crawl._files = original_files  # type: ignore[assignment]


def _validate_recorded(code: str, item: _Recorded, *, undated: bool) -> list[str]:
    """Check template fields and date parsing for one `Placed` item."""
    errors: list[str] = []
    placed = item.placed
    post_id = placed.post.id
    if UNFILLED.search(placed.path) or UNFILLED.search(placed.key):
        errors.append(f"post {post_id}: unfilled placeholder in {placed.path!r}")
    if not placed.path or not Path(placed.path).stem:
        errors.append(f"post {post_id}: empty planned path")
    if placed.post.date is not None and placed.values.get("date") is None:
        errors.append(f"post {post_id}: unparsed date {placed.post.date!r}")
    elif item.asked.startswith("failed"):
        errors.append(f"post {post_id}: date hook {item.asked}")
    elif item.asked == "none":
        errors.append(f"post {post_id}: date hook found no day")
    elif (
        placed.values.get("date") is None
        and item.asked != "dated"
        and not undated
        and item.category not in UNDATED_CATEGORIES
        and code != "web"
        and placed.file.content is None
    ):
        errors.append(f"post {post_id}: missing date")
    templates = [item.folder, item.key]
    if placed.file.name is None:
        templates.append(item.filename)
    used: set[str] = set()
    for tmpl in templates:
        if tmpl:
            used.update(text.fields(tmpl))
    for field in sorted(used - OPTIONAL_FIELDS - {"date"}):
        value = placed.values.get(field)
        if value in (None, ""):
            errors.append(f"post {post_id}: empty template field {{{field}}}")
    return errors


def _run_one(
    fl: flnd.Flnd, code: str, route: str, category: str, target: str, *,
    limit: int, undated: bool,
) -> CheckResult:
    """Execute one `dry=True` run for `(route, category)` and validate every planned file."""
    named = f"{target}|{category}" if category else target
    planned: list[Planned] = []
    errors: list[str] = []
    with _capture_placed(ask=not undated) as recorded:
        run = fl.get(named, limit=limit, dry=True)
        for event in run:
            if isinstance(event, Planned):
                planned.append(event)
                if event.path is not None and UNFILLED.search(str(event.path)):
                    errors.append(f"post {event.post}: unfilled placeholder in {event.path}")
            elif isinstance(event, Failed):
                errors.append(str(event.error))
            elif isinstance(event, Saved):
                errors.append(f"dry run unexpectedly saved {event.path}")
    for item in recorded:
        errors.extend(_validate_recorded(code, item, undated=undated))
    if not planned and not errors:
        errors.append("no files found")
    # Deduplicate while keeping order so repeated posts don't flood output.
    unique = tuple(dict.fromkeys(errors))
    return CheckResult(code, route, category, target, len(planned), unique)


def run_checks(
    config: Mapping[str, Any], *, codes: Sequence[str] = (), home: str | None = None,
    limit: int | None = None, only_route: str | None = None,
    only_categories: Sequence[str] = (), plugins: Sequence[type[Any]] = (),
) -> tuple[list[CheckResult], list[str]]:
    """Run configured live checks with `dry=True` and return `(results, notes)`."""
    selected = list(codes) if codes else [
        key for key, value in config.items() if isinstance(value, Mapping)
    ]
    if not selected:
        raise ValueError("no plugins selected and none configured in live.toml")
    default_home = home or config.get("home") or os.environ.get("FLND_HOME") or DEFAULT_HOME
    default_limit = limit if limit is not None else int(config.get("limit", DEFAULT_LIMIT))
    if default_limit < 1:
        raise ValueError("limit must be at least 1")

    results: list[CheckResult] = []
    notes: list[str] = []
    for code in selected:
        routes = _route_specs(code, plugins)
        section = config.get(code)
        if not isinstance(section, Mapping):
            raise ValueError(f"missing [{code}] section in live.toml")
        unknown = [key for key in section if key not in OPTION_KEYS and key not in routes]
        if unknown:
            raise ValueError(f"[{code}]: unknown route(s) {', '.join(sorted(unknown))}")

        plugin_home = str(section.get("home") or default_home)
        plugin_limit = int(section.get("limit", default_limit))
        undated = bool(section.get("undated", False))
        cfg_only = _string_list(section["only"], f"[{code}].only") if "only" in section else []
        cfg_skip = (
            set(_string_list(section["skip"], f"[{code}].skip")) if "skip" in section else set()
        )
        configured_routes = [r for r in routes if r in section]
        if only_route:
            if only_route not in routes:
                raise ValueError(f"[{code}]: unknown route {only_route!r}")
            if only_route not in section:
                raise ValueError(f"[{code}]: route {only_route!r} has no target in live.toml")
            configured_routes = [only_route]
        if not configured_routes:
            raise ValueError(f"[{code}]: no route targets configured in live.toml")
        for missing_route in routes:
            if missing_route not in section and not only_route:
                notes.append(f"{code}.{missing_route}: skipped (not in live.toml)")

        with flnd.open(plugin_home) as fl:
            for cls in plugins:
                fl.add(cls)
            for route in configured_routes:
                targets = _string_list(section[route], f"[{code}].{route}")
                cats = routes[route]
                if cats:
                    wanted = list(only_categories) or cfg_only or cats
                    wanted = [cat for cat in wanted if cat not in cfg_skip]
                    invalid = [cat for cat in wanted if cat not in cats]
                    if invalid:
                        raise ValueError(
                            f"[{code}].{route}: unknown category {', '.join(invalid)}"
                        )
                    check_cats = wanted
                else:
                    check_cats = [""]
                for target in targets:
                    for category in check_cats:
                        results.append(_run_one(
                            fl, code, route, category, target,
                            limit=plugin_limit, undated=undated,
                        ))
    return results, notes


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for `make live`."""
    args = arguments(argv)
    only_cats = (
        [part.strip() for part in args.only.split(",") if part.strip()] if args.only else []
    )
    try:
        config = load_config(config_path(args.config))
        results, notes = run_checks(
            config, codes=args.codes, home=args.home, limit=args.limit,
            only_route=args.route, only_categories=only_cats,
        )
    except ValueError as error:
        print(f"live: {error}", file=sys.stderr)
        return 2
    for note in notes:
        print(f"SKIP  {note}")
    failed = 0
    for result in results:
        label = f"{result.code}.{result.route}" + (
            f"[{result.category}]" if result.category else ""
        )
        if result.ok:
            print(f"PASS  {label}: {result.planned} planned")
        else:
            failed += 1
            print(f"FAIL  {label}: {'; '.join(result.errors)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
