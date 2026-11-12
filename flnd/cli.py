"""The flnd command line: `flnd download`, `flnd sync`, `flnd login` and `flnd logout`.

Usage:
  flnd download ins:example_user https://x.com/example_handle --from reels,media --time 2026-01-01,
  cat list.txt | flnd download
  flnd download ins:example_user --video h265,low --image webp,mid
  flnd download ins:example_user --classify
  flnd sync
  flnd login
  flnd logout ins tik
  flnd logout all

`login` asks every answer. `download` and `sync` ask nothing, except `shared.browser`'s one
question, to download Chromium, when a source needs a browser and Google Chrome is missing.

It only parses the arguments, asks `login`'s questions and prints: each command is one call of
`flnd.Engine`, given the arguments as typed, whose `ValueError` is shown as a usage error (exit 2)
and whose `Report` gives the exit status, 1 when a target failed. It imports only `flnd` (the
API), `flnd.sources` (for help and login's list) and `shared` (plan/phase-6/redesign.md, B4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

from flnd import Engine, Range, __version__
from flnd.sources import SOURCES
from shared import cli, cookies, fmt, log, prompt, theme

# flnd's data folder, relative to the directory flnd runs in: the logins, history and saved settings live there.
HOME = Path(".flnd")
METHOD_LABELS = {"browser": "From your browser", "file": "From a cookie file", "qr": "With a QR code",
                 "phone": "With your phone number and a code"}
# The browsers a cookie login reads, by `shared.cookies` name, with the name a person knows; the
# same table as the engine's `auth.BROWSERS`, which names them in "logged in from Chrome".
BROWSERS = {"chrome": "Chrome", "firefox": "Firefox", "edge": "Edge", "brave": "Brave", "chromium": "Chromium"}


def describe() -> str:
    """The one-line summary that opens the help, naming every source in the registry."""
    names = [source.name for source in SOURCES.values()]
    return f"Dead simple media downloader for {', '.join(names[:-1])}, and {names[-1]}."


def epilog() -> str:
    """The help's footer: the target forms, each source's categories and the examples, generated from the sources."""
    # Each form a source's routes read, placeholders in angle brackets, so help can't drift from them.
    forms = "\n".join(f"  {form}" for source in SOURCES.values() for form in source.forms)
    categories = "\n".join(f"  {code}: {', '.join(source.categories)}" for code, source in SOURCES.items())
    return f"""\
Target format:
  <source>:<name, id or URL>, or a bare URL
  Sources: {", ".join(SOURCES)}
{forms}

Categories (--from; without it, every one; not for a single post's link):
{categories}

Examples:
  flnd download ins:example_user
  flnd download tik:example_creator twt:example_handle --from all
  flnd download fbk:example.page --from profile,stories
  flnd download https://x.com/example_handle/status/1790000000000000000
  flnd download ins:example_user --image webp,mid --video h265,low
  flnd download ins:example_user --classify
  flnd download twt:example_handle --time 2026-01-01,2026-06-30
  cat list.txt | flnd download
  flnd sync
  flnd login
  flnd logout all
"""


app = cli.App("flnd", version=__version__, description=describe(), epilog=epilog())
# --image, --video and --audio: a format, a level, or both as <format>,<level> in either order, or
# off. Passed to the engine as text, which `Choice.parse` reads, so "off" can be told from "not
# given" and an error can name what each part takes.
MediaChoice = Annotated[Optional[str], cli.opt(metavar="FORMAT,LEVEL")]
# --classify alone is on, and --classify off turns it off: the value is optional, and passed on as text.
OnOff = Annotated[Optional[str], cli.opt(metavar="off", choices=("on", "off"), const="on")]


@app.command
def download(
    targets: Annotated[list[str], cli.arg(nargs="+")],
    *,
    categories: Annotated[Optional[list[str]], cli.opt("--from", metavar="CATEGORIES")] = None,
    time: Annotated[Optional[str], cli.opt(metavar="SINCE,UNTIL")] = None,
    image: MediaChoice = None,
    video: MediaChoice = None,
    audio: MediaChoice = None,
    classify: OnOff = None,
) -> int:
    """Download each target.

    --image, --video, --audio and --classify are saved for each target and used by every later
    download and sync of it; giving one again changes it, for the files already saved too, and off
    turns it off.

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
        classify: group each photo and video by what it shows (food, pet, selfie, …) as links in
            labels/<label>/, never renaming a file; off removes labels/. The model (about 150 MB)
            is downloaded on first use, after asking.
    """
    try:
        # Every value is checked before anything starts, so a usage error never follows any work.
        # Only ValueError is a usage error: the engine raises it for a bad option or target, with the
        # text to show. Anything else is a bug or a failed run, and propagates to report it.
        report = Engine(home=HOME).download(targets, categories=categories, time=Range() if time is None else time,
                                            image=image, video=video, audio=audio, classify=classify)
    except ValueError as error:
        raise cli.UsageError(str(error)) from None
    log.ok("Complete. {files}", files=fmt.count(report.files, "file"))
    # 1 when a target failed, so a script can tell. A rate-limit wait, a skipped file, or one file
    # of a target that failed while the target went on, isn't a failed target.
    return 0 if report.ok else 1


@app.command
def sync(targets: Annotated[list[str], cli.arg(nargs="*", stdin=False)]) -> int:
    """Bring downloaded targets up to date: repair broken or missing files, fetch new posts, and sum up.

    Each target runs with its saved --image, --video, --audio and --classify and each source's
    default categories; sync changes no setting. To change one, run flnd download with it. With
    --classify, the labels/ links are rebuilt: missing ones made, ones whose file is gone removed.

    Args:
        targets: only these targets, as in flnd download; every one downloaded so far without them.
    """
    try:
        # The engine logs each target's line and the totals, which only it can count (the folder's files and size).
        # As for download, only ValueError is a usage error; the rest propagates.
        report = Engine(home=HOME).sync(targets)
    except ValueError as error:
        raise cli.UsageError(str(error)) from None
    return 0 if report.ok else 1


@app.command("login")
def login_command() -> int:
    """Log in to a source: from your browser, a cookie file, or Telegram's own ways.

    Every answer is asked, in a terminal: the source, then the method when there are several,
    then what the method needs.
    """
    # Logging in is a one-time step done by a person, so there is no flag form for scripts. With no
    # terminal there is nobody to answer the questions, so the command refuses before asking any.
    if not theme.screen.interactive:
        raise cli.UsageError("flnd login asks its questions in a terminal")
    engine = Engine(home=HOME)
    code = _ask_source(engine)
    source = SOURCES[code]
    methods = source.login_methods
    method = methods[0] if len(methods) == 1 else prompt.choose(
        f"Log in to {source.name}", {METHOD_LABELS.get(name, name): name for name in methods})
    answers: dict[str, Optional[str]] = {}
    # A source with its own login (tlg) asks its own questions, through the engine.
    if code in engine.cookie_logins:
        if method == "file":
            answers["path"] = prompt.ask("Path to cookies.txt or .json", validate=_readable).strip()
        else:
            browser = prompt.choose("Read cookies from", {label: name for name, label in BROWSERS.items()})
            answers.update(browser=browser, profile=_ask_profile(browser))
    try:
        message = engine.login(code, method, **answers)
    # A login that didn't work is an expected result, shown as the error with what to do instead; any
    # other exception is a bug and propagates.
    except Engine.LoginError as error:
        log.error("{error}", error=error)
        return 1
    if message:
        log.ok(message)
    return 0


@app.command
def logout(sources: Annotated[list[str], cli.arg(nargs="*", choices=(*SOURCES, "all"), stdin=False)]) -> int:
    """Remove saved logins: each source's cookies and secrets, an API key included.

    Args:
        sources: the sources to log out of, such as ins tik, or all for every one.
    """
    engine = Engine(home=HOME)
    if not sources:
        logged_in = [code for code, state in engine.logins.items() if state == "logged in"]
        raise cli.UsageError("give the sources to log out of, or all; "
                             + (f"logged in: {', '.join(logged_in)}" if logged_in else "no source is logged in"))
    everything = "all" in sources
    removed = engine.logout(sources)
    for code in SOURCES if everything else dict.fromkeys(sources):
        if code in removed:
            log.ok("{code}: logged out", code=code)
        # `all` names every source, so only the ones asked for by name say they had no login.
        elif not everything:
            log.info("{code}: not logged in", code=code)
    return 0


def _ask_source(engine: Engine) -> str:
    """Ask which source to log in to, marking the ones logged in or holding only an API key, and return its code."""
    # tlg keeps its API key before its login; the list tells the two apart, by the mark after its name.
    logins = engine.logins
    options = {f"{source.name} ({code})" + (f" · {logins[code]}" if code in logins else ""): code
               for code, source in SOURCES.items()}
    return prompt.choose("Log in to", options)


def _ask_profile(browser: str) -> Optional[str]:
    """Ask which of the browser's profiles to read, with the default one preselected.

    Only asked when there are several. With none found, None lets the engine's login say why.
    """
    profiles = cookies.profiles(browser)
    if len(profiles) <= 1:
        return profiles[0] if profiles else None
    return prompt.choose("Profile", profiles, default=profiles[0])


def _readable(raw: str) -> Optional[str]:
    """The validator for the cookie file's path: None when it names a file, else the message to ask again with."""
    path = Path(raw.strip()).expanduser()
    return None if path.is_file() else f"no file at {raw.strip() or '(empty)'}"
