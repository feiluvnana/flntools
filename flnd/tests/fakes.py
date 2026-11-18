"""flnd's own test helpers for running the engine.

- `history` and `targets` read what a run saved in a home's `flnd.db`.
- `fetch` and `download` drive the HTTP library (`flnkit.net`) from a test's own thread.
- `EngineCase`, `make_plugin`, `login_cookies`, `own_login` and `run_targets` run the engine on
  test plugins (API 2), on `flnkit.net.fake` and a fake clock.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import no_profiles  # noqa: F401  (refuses real browser profiles)
import unawaited  # noqa: F401  (a coroutine never awaited fails its test)

from flnd import plugin
from flnd.engine import home as _home
from flnd.engine import run as _run
from flnd.engine.core import Engine, Report, _drive
from flnd.engine.events import Failed, Phase, State
from flnd.engine.options import Options
from flnd.plugin import Post, Target
from flnd.ui.log import ui
from flnkit import clock as _time
from flnkit import cookies as _cookies
from flnkit import net as _net

__all__ = ["CDN", "EngineCase", "download", "fetch", "history", "login_cookies", "make_plugin",
           "noise", "own_login", "run_targets", "targets"]


def history(home) -> dict[str, dict[str, Any]]:
    """Every saved file's row in a home's `flnd.db`, by key, with its post's file count.

    A test reads what a run saved through this, as a dict per row, the way it reads JSON.
    """
    from pathlib import Path

    import msgspec

    from flnd.engine import home as _home

    with _home.open(Path(home)) as db:
        rows = {row.key: msgspec.structs.asdict(row) for row in db.records()}
        for item, row in rows.items():
            row["files"] = db.files_of_post(item)
    return rows


def targets(home) -> dict[str, Any]:
    """Every target row in a home's `flnd.db`, by key (`flnkit.db.TargetRow`)."""
    from pathlib import Path

    from flnd.engine import home as _home

    with _home.open(Path(home)) as db:
        return db.targets()
# ---------------------------------------------------------------------------------------------
# The HTTP library, driven from a test's own thread
# ---------------------------------------------------------------------------------------------
# `flnkit.net` runs on an asyncio loop. These run one call on a loop of its own (`asyncio.run`,
# which carries the test's context, so a fake clock applies), over a fresh session, inside
# whatever `_net.fake` the test opened. Each is a blocking call, as a script would make.


def fetch(url: str, *, retry: bool = False, stop: Any = None,
          session: _net.Session | None = None, **options: Any) -> _net.Response:
    """`Client.fetch` on a loop of its own: the response, or the error it raises; with
    `retry`, as a job of a scheduler, under the run's retry policy."""
    return asyncio.run(_with_client(lambda client: client.fetch(url, **options), retry=retry,
                                    stop=stop, session=session))


def download(url: Any, dest: Any, *, retry: bool = False, stop: Any = None,
             resume: bool = True, progress: Any = None,
             headers: Mapping[str, str] | None = None) -> Any:
    """`Client.fill` into `dest`'s part on a loop of its own; returns where it landed."""
    from flnkit import files as _files

    async def fill(client: _net.Client) -> Any:
        meter = _net.Meter(progress)
        with _files.part(dest, resume=resume) as held:
            await scheduled(lambda: client.fill(url, held, resume=resume, progress=meter,
                                                headers=headers), retry=retry)
        return held.dest

    return asyncio.run(_with_client(fill, stop=stop))


async def scheduled(work: Callable[[], Any], *, retry: bool) -> Any:
    """`work()`, or with `retry` a `FILE` job of a scheduler of its own, on the fake clock."""
    if not retry:
        return await work()
    from flnd.engine import jobs

    with jobs.Scheduler() as scheduler:
        return await scheduler.run(jobs.Job(kind=jobs.Kind.FILE, work=work, host="example.com"))


async def _with_client(call: Callable[[_net.Client], Any], *, stop: Any, retry: bool = False,
                       session: _net.Session | None = None) -> Any:
    own = session is None
    session = session if session is not None else _net.Session()
    try:
        client = _net.Client(session, stop=stop)
        return await scheduled(lambda: call(client), retry=retry)
    finally:
        if own:
            await session.close()


# ---------------------------------------------------------------------------------------------
# The engine on test sources
# ---------------------------------------------------------------------------------------------

def run_targets(engine: Engine, chosen: Sequence[tuple[Target, Options]],
                stop: _run.Stop) -> Report:
    """Run `chosen` through the engine's run, as a command does, on a loop on this thread."""
    async def command() -> Report:
        async with engine._live(), engine._running(stop) as runner:
            return Report(tuple(await runner.run(chosen)))

    return _drive(command())


CDN = "https://cdn.example"


def noise(format, size=(64, 48)):
    if format.upper() == "PNG":
        return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(32)
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + bytes(32)


def make_plugin(code, route=None, *, files: Any = (), **declared):
    """A test plugin on API 2: `code:<name>` reads a user, `route(ctx)` is its route, and `files` are its `posts`.

        make_plugin("aaa", lambda ctx: seen.append(ctx.target.value), files=[File(url, name="a.jpg")])

    Args:
        code: the plugin's code; its domain is `<code>.example`.
        route: called with the target's `Ctx` when the target is read, and awaited when it gives
            an awaitable; what it returns is the context of the category.
        files: the files `posts` yields, each in a post of its own, kept as the site names them and
            filed in the target's own folder, keyed by their path (`aaa:a.jpg`); or a function of
            `(ctx, context)` giving them (awaited when it gives an awaitable).
        **declared: the class's other fields and methods, such as `login`, `connect` or `sign`;
            `browser=True` is `Browser.ANY`.
    """
    async def listed(ctx, context):
        found: Any = files(ctx, context) if callable(files) else files
        if inspect.isawaitable(found):
            found = await found
        return [Post(file.name or str(number), [file]) for number, file in enumerate(found)]

    @plugin.route(f"{code}:{{user}}", newest_first=False)
    async def user(self, ctx, user):
        found = route(ctx) if route is not None else None
        return await found if inspect.isawaitable(found) else found

    @user.category(newest_first=False)
    async def posts(self, ctx, context):
        for post in await listed(ctx, context):
            yield post

    if declared.get("browser") is True:
        declared["browser"] = plugin.Browser.ANY
    attributes = {"code": code, "name": code.upper(), "domains": (f"{code}.example",), "api": 2,
                  "folder": "", "key": "{path}", "user": user, "posts": posts, **declared}
    return type(f"Plugin_{code}", (plugin.Plugin,), attributes)


def login_cookies(*names: str, required: bool = False) -> plugin.Cookies:
    """A cookie login of a test plugin: `flnd.plugin.cookies` with a login page on example.com."""
    return plugin.cookies(*names, url="https://example.com/login", required=required)


def own_login(start=None, *, methods=("qr",), check=None):
    """A plugin's own `Login`: `start(self, ctx, method)` logs in; `check(self, ctx)` says whether it did.

    Either may be a plain function or an `async def`; what a plain one returns is awaited when it
    is awaitable. Without `check`, it is logged in when its secrets hold `session`.
    """
    async def called(function, *args):
        found = function(*args)
        return await found if inspect.isawaitable(found) else found

    async def started(self, ctx, method):
        if start is not None:
            await called(start, self, ctx, method)

    async def checked(self, ctx):
        if check is None:
            return "session" in ctx.secrets
        return await called(check, self, ctx)

    return type("OwnLogin", (plugin.Login,), {"methods": methods, "start": started,
                                              "check": checked})()


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.dest = io.StringIO()
        scope = ui.scope(dest=self.dest, width=400, color=False)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"
        self.requests = []
        self.routes = {}
        # A lambda, so a test that replaces `self.serve` is answered by its own.
        self.enterContext(_net.fake(lambda request: self.serve(request)))
        # Every wait (pacing, back-offs, retries) passes at once on the fake clock.
        self.fake = self.enterContext(_time.use(_time.Fake()))
        # The events of the runs `report` makes: what the CLI shows, now that the engine logs no rows.
        self.events = []
        # The run's snapshots, kept apart from its events.
        self.states = []

    @property
    def now(self):
        return self.fake.now

    def serve(self, request: _net.Request) -> _net.Reply:
        self.requests.append(request)
        answer = self.routes.get(str(request.url).split("?")[0])
        if callable(answer):
            return answer(request)  # type: ignore[return-value]
        if answer is None:
            return _net.reply(200, content=request.url.path.encode())
        return answer

    def run_engine(self, *sources, targets, options=Options(), **settings):
        """Run the targets, each with `options`, and return how many files were written."""
        return self.report(*sources, targets=targets, options=options, **settings).files

    def report(self, *sources, targets, options=Options(), stop=None, **settings):
        """Run the targets, each with `options`, and return the `Report`; its events go in `events`."""
        settings.setdefault("emit", lambda event: (
            self.states if isinstance(event, State) else self.events).append(event))
        engine = Engine(home=self.home, sources={source.code: source for source in sources}, **settings)
        return run_targets(engine, [(target, options) for target in engine.resolve(targets)],
                           stop or _run.Stop())

    def log_in(self, code, **values):
        jar = [{"name": name, "value": value, "domain": f".{code}.example", "path": "/"} for name, value in values.items()]
        _home.save(self.home, code, _cookies.Jar(jar))

    def lines(self):
        return self.dest.getvalue().splitlines()

    def failures(self):
        """Each `Failed` event of the runs so far, as `<target>: <message>` (a file's: `<name>: ...`)."""
        return [f"{event.path.name if event.path else event.target}: {event.error}"
                for event in self.events if isinstance(event, Failed)]

    def listed(self):
        """Each `(target, category)` of a target that ended done, from the run's last snapshot."""
        state = self.states[-1]
        return {(row.text, name) for row in state.targets if row.phase is Phase.DONE
                for name in row.categories}

    def target_dir(self, code, name):
        return self.home / _home.MEDIA / code / name
