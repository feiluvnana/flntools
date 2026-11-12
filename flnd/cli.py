"""The flnd command line: `flnd download`, `flnd sync`, `flnd login` and `flnd logout`.

Usage:
  flnd download ins:example_user https://x.com/example_handle --from reels,media --time 2026-01-01,
  cat list.txt | flnd download
  flnd download ins:example_user --video h265,low --image webp,mid
  flnd sync
  flnd login
  flnd logout ins tik
  flnd logout all

`login` asks every answer. `download` and `sync` ask nothing, except `shared.browser`'s one
question, to download Chromium, when a source needs a browser and Google Chrome is missing.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Optional

from flnd import __version__, sync as syncing
from flnd.common import auth
from flnd.common.compress import Choice, parse_choice
from flnd.common.target import TargetError
from flnd.engine import Engine
from flnd.session import Options, single_targets, unknown_categories
from flnd.sources import SOURCES
from flnd.sources.base import Source
from shared import cli, cookies, log, prompt, theme

HOME = Path(".flnd")
METHOD_LABELS = {"browser": "From your browser", "file": "From a cookie file", "qr": "With a QR code",
                 "phone": "With your phone number and a code"}


def describe() -> str:
    names = [source.name for source in SOURCES.values()]
    return f"Dead simple media downloader for {', '.join(names[:-1])}, and {names[-1]}."


def epilog() -> str:
    examples = "\n".join(f"  {example}" for source in SOURCES.values() for example in source.examples)
    categories = "\n".join(f"  {code}: {', '.join(source.categories)}" for code, source in SOURCES.items())
    return f"""\
Target format:
  <source>:<name, id or URL>, or a bare URL
  Sources: {", ".join(SOURCES)}
{examples}

Categories (--from; without it, every one; not for a single post's link):
{categories}

Examples:
  flnd download ins:example_user
  flnd download tik:example_creator twt:example_handle --from all
  flnd download fbk:example.page --from profile,stories
  flnd download https://x.com/example_handle/status/1790000000000000000
  flnd download ins:example_user --image webp,mid --video h265,low
  flnd download twt:example_handle --time 2026-01-01,2026-06-30
  cat list.txt | flnd download
  flnd sync
  flnd login
  flnd logout all
"""


app = cli.App("flnd", version=__version__, description=describe(), epilog=epilog())
# --image, --video and --audio: a format, a level, or both as <format>,<level> in either order, or
# off. Read as text and parsed by `compress.parse_choice` in the command, so "off" can be told from
# "not given" and an error can name what each part takes.
MediaChoice = Annotated[Optional[str], cli.opt(metavar="FORMAT,LEVEL")]
TIME_USAGE = ("--time takes <since>,<until>, such as 2026-01-01,2026-06-30; use * or leave a side empty "
              "for no limit")


@app.command
def download(
    targets: Annotated[list[str], cli.arg(nargs="+")],
    *,
    categories: Annotated[Optional[list[str]], cli.opt("--from", metavar="CATEGORIES")] = None,
    time: Annotated[Optional[str], cli.opt(metavar="SINCE,UNTIL")] = None,
    image: MediaChoice = None,
    video: MediaChoice = None,
    audio: MediaChoice = None,
) -> int:
    """Download each target.

    --image, --video and --audio are saved for each target and used by every later download and
    sync of it; giving one again changes it, for the files already saved too, and off turns it off.

    Args:
        targets: source:value pairs or URLs; read one per line from stdin when none are given.
        categories: what to take from each source, such as posts,reels; a comma list or repeated.
            Without it, every category. Not for a single post's link, which is downloaded whole.
        time: only items dated from one day to another, both included, as 2026-01-01,2026-06-30;
            leave a side empty, or give '*' (quoted), for no limit: 2026-01-01, or ,2026-06-30.
        image: compress each photo, replacing it when smaller, metadata (GPS too) stripped: a format
            (jpg, webp, avif, png, gif, tif, bmp), a level (best, high, mid, low), or both; off stops it.
        video: compress each video the same way: a container (mp4, mkv, webm, …), a codec (h264,
            h265, vp9) or container:codec, and a level. h265 is smaller, but many browsers can't play it.
        audio: compress each audio file the same way: mp3, m4a, aac, opus, ogg or flac, and a level.
    """
    since, until = _time(time)
    given = _given(image=image, video=video, audio=audio)
    engine = Engine(home=HOME)
    resolved = _resolve(engine, targets)
    options = Options(categories=_categories(categories, resolved) if categories is not None else None,
                      since=since, until=until)
    log.info("Storage: {path}", path=(HOME / "downloads").resolve())
    total = syncing.download(engine, resolved, options, given)
    log.ok("Complete. {n} files", n=total)
    return _status(engine)


@app.command
def sync(targets: Annotated[list[str], cli.arg(nargs="*", stdin=False)]) -> int:
    """Bring downloaded targets up to date: repair broken or missing files, fetch new posts, and sum up.

    Each target runs with its saved --image, --video and --audio and each source's default
    categories; sync changes no setting. To change one, run flnd download with it.

    Args:
        targets: only these targets, as in flnd download; every one downloaded so far without them.
    """
    engine = Engine(home=HOME)
    # Resolved here once, for the plan: a short link costs a HEAD request each time (fixes #11).
    resolved = _resolve(engine, targets) if targets else []
    syncing.run(engine, syncing.plan(engine, resolved))
    return _status(engine)


def _status(engine: Engine) -> int:
    """The exit status of `download` and `sync`: 1 when a target failed, so a script can tell; 0 otherwise.

        return _status(engine)

    A rate-limit wait, a skipped file, or one file of a target that failed while the target went
    on, isn't a failed target.
    """
    return 1 if engine.failed else 0


def _time(text: str | None) -> tuple[date | None, date | None]:
    """`--time`'s two days, each None for no limit; a value that isn't `<since>,<until>` is a usage error.

        _time("2026-01-01,")      # (date(2026, 1, 1), None)
    """
    if text is None:
        return None, None
    since, comma, until = text.partition(",")
    if not comma or "," in until:
        raise cli.UsageError(TIME_USAGE)
    try:
        days = [_day(side) for side in (since, until)]
    except ValueError:
        raise cli.UsageError(TIME_USAGE) from None
    if days[0] and days[1] and days[0] > days[1]:
        raise cli.UsageError(TIME_USAGE)
    return days[0], days[1]


def _day(text: str) -> date | None:
    """One side of `--time`: a YYYY-MM-DD day, or None for `*` or nothing; `ValueError` otherwise."""
    text = text.strip()
    if text in ("", "*"):
        return None
    return datetime.strptime(text, "%Y-%m-%d").date()


def _given(**values: str | None) -> dict[str, Choice | None]:
    """The `--image`, `--video` and `--audio` given, parsed, by name: a `Choice`, or None for off.

    A name not given is left out, so the target keeps its saved setting.
    """
    given: dict[str, Choice | None] = {}
    for kind, text in values.items():
        if text is None:
            continue
        try:
            given[kind] = parse_choice(kind, text)
        except ValueError as error:
            raise cli.UsageError(str(error)) from None
    return given


def _resolve(engine: Engine, targets: list[str]) -> list:
    try:
        return engine.resolve(targets)
    except TargetError as error:
        raise cli.UsageError(str(error)) from None


def _categories(categories: list[str], resolved: list) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(categories))
    sources = list(dict.fromkeys(SOURCES[target.source] for target in resolved)) or list(SOURCES.values())
    if problem := unknown_categories(names, sources) or single_targets(resolved, SOURCES):
        raise cli.UsageError(problem)
    return names


@app.command("login")
def login_command() -> int:
    """Log in to a source: from your browser, a cookie file, or Telegram's own ways.

    Every answer is asked, in a terminal: the source, then the method when there are several,
    then what the method needs.
    """
    # Logging in is a one-time step done by a person, so there is no flag form for scripts.
    if not theme.screen.interactive:
        raise cli.UsageError("flnd login asks its questions in a terminal")
    site = SOURCES[_ask_source()]()
    methods = site.login_methods
    method = methods[0] if len(methods) == 1 else prompt.choice(
        f"Log in to {site.name}", {METHOD_LABELS.get(name, name): name for name in methods})
    try:
        # A source with its own login (tlg) asks its own questions, through the engine's session.
        if type(site).login is not Source.login:
            Engine(home=HOME).login(site, method)
            return 0
        if method == "file":
            path = Path(prompt.input("Path to cookies.txt or .json", validate=_readable).strip()).expanduser()
            message = auth.store(site, auth.from_file(site, path), home=HOME, origin=path.name)
        else:
            browser = prompt.choice("Read cookies from", {label: name for name, label in auth.BROWSERS.items()})
            jar = auth.from_browser(site, browser=browser, profile=_ask_profile(browser))
            message = auth.store(site, jar, home=HOME, origin=auth.BROWSERS[browser])
    except auth.LoginError as error:
        log.error("{error}", error=error)
        return 1
    log.ok(message)
    return 0


@app.command
def logout(sources: Annotated[list[str], cli.arg(nargs="*", choices=(*SOURCES, "all"), stdin=False)]) -> int:
    """Remove saved logins: each source's cookie file and secrets.

    Args:
        sources: the sources to log out of, such as ins tik, or all for every one.
    """
    if not sources:
        logged_in = [code for code in SOURCES if auth.logged_in(HOME, code)]
        raise cli.UsageError("give the sources to log out of, or all; "
                             + (f"logged in: {', '.join(logged_in)}" if logged_in else "no source is logged in"))
    everything = "all" in sources
    for code in SOURCES if everything else dict.fromkeys(sources):
        if auth.logout(HOME, code):
            log.ok("{code}: logged out", code=code)
        # `all` names every source, so only the ones asked for by name say they had no login.
        elif not everything:
            log.info("{code}: not logged in", code=code)
    return 0


def _ask_source() -> str:
    """Ask which source to log in to, marking the ones already logged in, and return its code."""
    options = {}
    for code, source in SOURCES.items():
        mark = " · logged in" if auth.logged_in(HOME, code) else ""
        options[f"{source.name} ({code}){mark}"] = code
    return prompt.choice("Log in to", options)


def _ask_profile(browser: str) -> Optional[str]:
    """Ask which of the browser's profiles to read, with the default one preselected.

    Only asked when there are several. With none found, None lets `auth.from_browser` say why.
    """
    profiles = cookies.profiles(browser)
    if len(profiles) <= 1:
        return profiles[0] if profiles else None
    return prompt.choice("Profile", profiles, default=profiles[0])


def _readable(raw: str) -> Optional[str]:
    path = Path(raw.strip()).expanduser()
    return None if path.is_file() else f"no file at {raw.strip() or '(empty)'}"
