"""The flnd command line: `flnd download`, `flnd sync`, `flnd login`, `flnd logout` and `flnd install-browser`.

Usage:
  flnd download ins:example_user https://x.com/example_handle --from reels,media --since 2026-01-01
  cat list.txt | flnd download
  flnd download ins:example_user --compress-video small --format-video h265
  flnd sync
  flnd login ins --method browser --browser firefox
  flnd logout --all
  flnd install-browser

Only `login` asks questions; `download` and `sync` never do.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path
from typing import Annotated, Optional

from dataclasses import replace

from flnd import __version__, login as logins, sync as syncing
from flnd.engine import Engine
from flnd.session import PRESETS, Options, compress_problem, unknown_categories
from flnd.sources import SOURCES
from flnd.sources.base import Source
from flnd.target import TargetError
from shared import cli, log, prompt, theme

HOME = Path(".flnd")
METHOD_LABELS = {"browser": "From your browser", "file": "From a cookie file", "qr": "With a QR code",
                 "phone": "With your phone number and a code"}


def describe() -> str:
    names = [source.name for source in SOURCES.values()]
    return f"Dead simple media downloader for {', '.join(names[:-1])}, and {names[-1]}."


def epilog() -> str:
    examples = "\n".join(f"  {example}" for source in SOURCES.values() for example in source.examples)
    categories = "\n".join(f"  {code}: {', '.join(_marked(source))}" for code, source in SOURCES.items())
    return f"""\
Target format:
  <source>:<name, id or URL>, or a bare URL
  Sources: {", ".join(SOURCES)}
{examples}

Categories (--from; * is downloaded without --from):
{categories}

Examples:
  flnd download ins:example_user
  flnd download tik:example_creator twt:example_handle --from all
  flnd download fbk:example.page --from profile,stories
  flnd download https://x.com/example_handle/status/1790000000000000000
  flnd download ins:example_user --compress-image balanced --compress-video small
  cat list.txt | flnd download
  flnd sync
  flnd sync --only info
  flnd login ins
"""


def _marked(source: type[Source]) -> list[str]:
    return [name + ("*" if name in source.default_categories else "") for name in source.categories]


app = cli.App("flnd", version=__version__, description=describe(), epilog=epilog())
Preset = Annotated[Optional[str], cli.opt(choices=PRESETS)]
# sync takes "off" too, to stop compressing a target.
SyncPreset = Annotated[Optional[str], cli.opt(choices=(*PRESETS, "off"))]


@app.command
def download(
    targets: Annotated[list[str], cli.arg(nargs="+")],
    *,
    categories: Annotated[Optional[list[str]], cli.opt("--from", metavar="CATEGORIES")] = None,
    since: Optional[date] = None,
    until: Optional[date] = None,
    unpack: bool = False,
    zip: bool = False,
    compress_image: Preset = None,
    compress_video: Preset = None,
    format_image: Optional[str] = None,
    format_video: Optional[str] = None,
    locale: Optional[str] = None,
    timezone: Optional[str] = None,
) -> int:
    """Download each target.

    Args:
        targets: source:value pairs or URLs; read one per line from stdin when none are given.
        categories: what to take from each source, such as posts,reels or all; a comma list or repeated.
            Without it, each source's own content; other people's (tagged, reposts) only when named.
        since: only items dated on or after this day (YYYY-MM-DD).
        until: only items dated on or before this day (YYYY-MM-DD).
        unpack: unpack each saved archive into a folder named after it.
        zip: zip each target's folder when it ends.
        compress_image: make each new photo smaller, replacing it; metadata (GPS too) is stripped.
        compress_video: make each new video smaller, replacing it; metadata (GPS too) is stripped.
        format_image: write compressed photos as jpg, webp, avif, png, gif, tif or bmp.
        format_video: write compressed videos in a container (mp4, mkv, webm, …), a codec (h264, h265,
            vp9), or both as container:codec. h265 is smaller, but many browsers can't play it.
        locale: the browser's language, such as fr-FR; match where your connection comes from.
        timezone: the browser's timezone, such as Europe/Paris; match where your connection comes from.
    """
    if since and until and since > until:
        raise cli.UsageError("--since is after --until")
    options = Options(since=since, until=until, unpack=unpack, zip=zip, compress_image=compress_image,
                      compress_video=compress_video, format_image=format_image, format_video=format_video)
    if problem := compress_problem(options):
        raise cli.UsageError(problem)
    engine = Engine(home=HOME, locale=locale, timezone=timezone)
    resolved = _resolve(engine, targets)
    if categories is not None:
        options = replace(options, categories=_categories(categories, resolved))
    syncing.record(HOME, resolved, options)
    log.info("Storage: {path}", path=(HOME / "downloads").resolve())
    total = engine.run(resolved, options)
    log.ok("Complete. {n} files", n=total)
    return 0


@app.command
def sync(
    targets: Annotated[list[str], cli.arg(nargs="*", stdin=False)],
    *,
    only: Annotated[Optional[list[str]], cli.opt(metavar="STEPS")] = None,
    categories: Annotated[Optional[list[str]], cli.opt("--from", metavar="CATEGORIES")] = None,
    unpack: Optional[bool] = None,
    zip: Optional[bool] = None,
    compress_image: SyncPreset = None,
    compress_video: SyncPreset = None,
    format_image: Optional[str] = None,
    format_video: Optional[str] = None,
    locale: Optional[str] = None,
    timezone: Optional[str] = None,
) -> int:
    """Bring every downloaded target up to date: repair, fetch new posts, compress, and sum up.

    Each target runs with the settings it was last downloaded or synced with; options given here
    replace them from now on. Folders downloaded before flnd recorded settings use the defaults.

    Args:
        targets: only these targets, as in flnd download; every one downloaded so far without them.
        only: the steps to run, of repair, fetch, compress and info; info alone changes nothing.
        categories: what to take from each source, as in flnd download.
        unpack: unpack each saved archive into a folder named after it.
        zip: zip each target's folder when it ends.
        compress_image: compress photos not compressed yet, and new ones, replacing them; off stops it.
        compress_video: compress videos not compressed yet, and new ones, replacing them; off stops it.
        format_image: write compressed photos in this format, as in flnd download.
        format_video: write compressed videos in this container, codec or container:codec.
        locale: the browser's language, such as fr-FR.
        timezone: the browser's timezone, such as Europe/Paris.
    """
    steps = tuple(step for step in syncing.STEPS if step in (only or syncing.STEPS))
    if unknown := [step for step in only or () if step not in syncing.STEPS]:
        raise cli.UsageError(f"--only takes {', '.join(syncing.STEPS)}, not {unknown[0]!r}")
    given = {"unpack": unpack, "zip": zip, "compress_image": compress_image, "compress_video": compress_video,
             "format_image": format_image, "format_video": format_video}
    overrides = {name: value for name, value in given.items() if value is not None}
    for kind in ("image", "video"):
        if overrides.get(f"compress_{kind}") == "off":
            overrides.update({f"compress_{kind}": None, f"format_{kind}": None})
    engine = Engine(home=HOME, locale=locale, timezone=timezone)
    if categories is not None:
        overrides["categories"] = _categories(categories, _resolve(engine, targets) if targets else [])
    try:
        chosen = syncing.plan(engine, targets, overrides)
    except TargetError as error:
        raise cli.UsageError(str(error)) from None
    for target, options in chosen:
        if problem := compress_problem(options):
            raise cli.UsageError(f"{target}: {problem}")
    if steps != ("info",):
        for target, options in chosen:
            syncing.record(HOME, [target], options)
    if "fetch" in steps:
        log.info("Storage: {path}", path=(HOME / "downloads").resolve())
    syncing.run(engine, chosen, steps)
    return 0


def _resolve(engine: Engine, targets: list[str]) -> list:
    try:
        return engine.resolve(targets)
    except TargetError as error:
        raise cli.UsageError(str(error)) from None


def _categories(categories: list[str], resolved: list) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(categories))
    sources = list(dict.fromkeys(SOURCES[target.source] for target in resolved)) or list(SOURCES.values())
    if problem := unknown_categories(names, sources):
        raise cli.UsageError(problem)
    return names


@app.command("login")
def login_command(
    source: Annotated[str, cli.arg(choices=SOURCES)],
    *,
    method: Optional[str] = None,
    path: Optional[Path] = None,
    browser: Annotated[Optional[str], cli.opt(choices=logins.BROWSERS)] = None,
    profile: Optional[str] = None,
) -> int:
    """Log in to a source, from your browser or a cookie file, or Telegram's own ways.

    Each method asks only for what it needs and wasn't given. Without a terminal, give them all.

    Args:
        source: the source to log in to.
        method: how to log in: browser or file for cookie sources, browser, qr or phone for tlg;
            asked when there are several.
        path: the cookie file (Netscape cookies.txt or JSON) for --method file; a whole browser export works.
        browser: the installed browser to read cookies from, for --method browser.
        profile: the browser profile's folder name, such as "Profile 1"; the default profile otherwise.
    """
    site = SOURCES[source]()
    method = method or _choose_method(site)
    if method not in site.login_methods:
        raise cli.UsageError(f"{source} has no login method {method!r}: use {', '.join(site.login_methods)}")
    try:
        if type(site).login is not Source.login:
            Engine(home=HOME).login(site, method)
            return 0
        if method == "file":
            path = (path or _ask_path()).expanduser()
            message = logins.store(site, logins.from_file(site, path), home=HOME, origin=path.name)
        else:
            browser = browser or _ask_browser()
            jar = logins.from_browser(site, browser=browser, profile=profile)
            message = logins.store(site, jar, home=HOME, origin=logins.BROWSERS[browser])
    except logins.LoginError as error:
        log.error("{error}", error=error)
        return 1
    log.ok(message)
    return 0


@app.command
def logout(source: Annotated[Optional[str], cli.arg(choices=SOURCES)] = None, *, all: bool = False) -> int:
    """Remove a source's saved login: its cookie file and secrets.

    Args:
        source: the source to log out of.
        all: log out of every source.
    """
    if (source is None) == (not all):
        raise cli.UsageError("give a source or --all, not both" if all else "give a source, or --all")
    for code in SOURCES if all else [source or ""]:
        if logins.logout(HOME, code):
            log.ok("{code}: logged out", code=code)
        elif not all:
            log.info("{code}: was not logged in", code=code)
    return 0


@app.command("install-browser")
def install_browser() -> int:
    """Install Chromium, for the sources that open a browser, into Playwright's folder in your user cache.

    It downloads about 150 MB once, with Playwright's own progress bar. Your installed Chrome is used
    instead when there is one.
    """
    try:
        code = _run_playwright("install", "chromium")
    except Exception as error:
        log.error("Chromium was not installed: {error}", error=error)
        return 1
    if code:
        log.error("Chromium was not installed: Playwright's installer exited with {code}", code=code)
        return 1
    log.ok("Chromium is installed")
    return 0


def _run_playwright(*args: str) -> int:
    # A bundled flnd has no `python -m playwright`, so this runs the driver Playwright ships, as that command does.
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    node, cli_js = compute_driver_executable()
    return subprocess.run([node, cli_js, *args], env=get_driver_env()).returncode


def _choose_method(source: Source) -> str:
    methods = source.login_methods
    if len(methods) == 1:
        return methods[0]
    if not theme.screen.interactive:
        raise cli.UsageError(f"give --method: {', '.join(methods)}")
    return prompt.choice(f"Log in to {source.name}", {METHOD_LABELS.get(name, name): name for name in methods})


def _ask_path() -> Path:
    if not theme.screen.interactive:
        raise cli.UsageError("give --path with the cookie file")
    return Path(prompt.input("Path to cookies.txt or .json", validate=_readable)).expanduser()


def _readable(raw: str) -> Optional[str]:
    path = Path(raw.strip()).expanduser()
    return None if path.is_file() else f"no file at {raw.strip() or '(empty)'}"


def _ask_browser() -> str:
    if not theme.screen.interactive:
        raise cli.UsageError(f"give --browser: {', '.join(logins.BROWSERS)}")
    return prompt.choice("Read cookies from", {label: name for name, label in logins.BROWSERS.items()})
