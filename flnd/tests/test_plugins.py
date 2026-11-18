"""Plugins (`flnd.engine.plugins`, `flnd.sources.Registry`).

Every test gets a registry that hasn't looked for plugins yet (`PluginCase`), a plugin folder
of its own (`$FLND_SOURCES`), a temporary home, the fake network and the fake clock. A plugin
is written as a file in that folder, or faked as an installed package's entry point.

The gates of plugins, each a test below:
- the example plugin's own tests pass, and the example script downloads through it;
- a broken plugin (a syntax error, an import error, a newer source API, a wrong code) disables
  only itself, with a warning naming its file and line, and a listing that raises fails only
  its own target;
- nothing loads from a home: `<home>/sources/evil.py` never runs, whatever command reads it.

The plugin manager that replaces the registry (`flnd.engine.manager`) has its own tests at the
end, from `ManagerCase` on: install, replace, refuse, check again, ignore, disable, uninstall.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import textwrap
import threading
import unittest
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import no_profiles  # noqa: F401  (also points $FLND_SOURCES away from the real folder)
from fakes import EngineCase, noise

import flnd
from flnd import cli as flnd_cli
from flnd.engine import crawl as job
from flnd.engine import manager as _manager
from flnd.engine import plugins as _plugins
from flnd.engine.resolve import Manifest, resolve
from flnd.errors import SourceError, UsageError
from flnd.sources import SOURCES, _find
from flnkit import clock
from flnkit import net as _net

ROOT = Path(__file__).resolve().parent.parent
JPEG = noise("JPEG")

# A working plugin: `exm:<user>` lists one post with one photo, which `serve` answers.
GOOD = '''
from flnd.source import File, Post, Source, target

__version__ = "2.1"


class {name}(Source):
    code, domains, api = "{code}", ("{code}.example",), {api}

    @target("{code}:{{user}}", "{code}.example/{{user}}")
    def user(self, site, user):
        if user == "broken":
            raise KeyError("id")
        yield Post("p1", [File("https://cdn.example.com/" + user + ".jpg")])
'''


def plugin(code: str, *, name: str = "Example", api: int = 1) -> str:
    """The text of a working plugin file for `code`."""
    return GOOD.format(code=code, name=name, api=api)


class PluginCase(EngineCase):
    """A fresh registry, a plugin folder of its own, and the log in `self.dest`."""

    def serve(self, request):
        if str(request.url).startswith("https://cdn.example.com/"):
            return _net.reply(200, content=JPEG)
        return super().serve(request)

    def setUp(self):
        super().setUp()
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name) / "plugins"
        self.folder.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {_plugins.VARIABLE: str(self.folder)}))
        # The registry looks for plugins once per process; each test starts before that.
        self.enterContext(mock.patch.multiple(
            SOURCES, _find=_find, _found=[], _plugins={}, _codes=list(SOURCES._codes),
            _loaded=dict(SOURCES._loaded)))
        self.enterContext(mock.patch.dict(sys.modules))
        self.fl = self.enterContext(flnd.open(self.home))

    def write(self, name: str, text: str) -> Path:
        """Write a file in the plugin folder, its text dedented."""
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def loaded(self) -> set[str]:
        """The plugin modules imported so far, by code."""
        return {name.removeprefix(_plugins.MODULE_PREFIX) for name in sys.modules
                if name.startswith(_plugins.MODULE_PREFIX) and "." not in name}

    def rows(self) -> dict[str, flnd.Installed]:
        return {row.code: row for row in self.fl.sources}


class DiscoveryTest(PluginCase):
    def test_a_file_plugin_downloads_like_a_built_in(self):
        self.write("exm.py", plugin("exm"))
        report = self.fl.download("exm:example_user").wait()
        self.assertEqual((report.saved, report.failed), (1, ()))
        saved = self.home / "media" / "exm" / "example_user" / "p1_0.jpg"
        self.assertEqual(saved.read_bytes(), JPEG)

    def test_a_code_imports_its_plugin_alone(self):
        self.write("exm.py", plugin("exm"))
        self.write("exo.py", plugin("exo"))
        self.fl._resolve(["exm:example_user", "ins:example_user"])
        self.assertEqual(self.loaded(), {"exm"})

    def test_a_built_in_link_imports_no_plugin(self):
        self.write("exm.py", plugin("exm"))
        self.fl._resolve(["https://www.instagram.com/example_user/"])
        self.assertEqual(self.loaded(), set())

    def test_a_built_in_download_never_looks_for_plugins(self):
        looked = []
        with mock.patch.object(SOURCES, "_find", lambda taken: looked.append(taken) or []):
            self.fl.download("mht:70982").wait()
        self.assertEqual(looked, [])

    def test_a_link_no_built_in_reads_imports_the_plugins(self):
        self.write("exm.py", plugin("exm"))
        [found] = self.fl._resolve(["https://exm.example/example_user"])
        self.assertEqual((found.source, found.kind, found.value), ("exm", "user", "example_user"))

    def test_a_package_plugin_imports_its_own_modules(self):
        self.write("exp/__init__.py", "from .site import Example\n")
        self.write("exp/site.py", plugin("exp"))
        self.assertEqual(self.fl._resolve(["exp:example_user"])[0].kind, "user")
        self.assertEqual(self.rows()["exp"].where, str(self.folder / "exp"))

    def test_an_entry_point_is_a_plugin_named_by_its_name(self):
        module = type(sys)("flnd_example")
        exec(compile(plugin("exe"), "flnd_example.py", "exec"), module.__dict__)
        sys.modules["flnd_example"] = module
        point = metadata.EntryPoint("exe", "flnd_example:Example", _plugins.GROUP)
        with mock.patch.object(metadata, "entry_points", return_value=[point]):
            self.assertEqual(self.fl._resolve(["exe:example_user"])[0].source, "exe")
            row = self.rows()["exe"]
        self.assertEqual((row.origin, row.where, row.api), ("package", "flnd_example:Example", 1))

    def test_a_later_plugin_never_shadows_an_earlier_one(self):
        self.write("ins.py", plugin("ins"))
        module = type(sys)("flnd_example")
        exec(compile(plugin("exm"), "flnd_example.py", "exec"), module.__dict__)
        sys.modules["flnd_example"] = module
        self.write("exm.py", plugin("exm", name="Other"))
        point = metadata.EntryPoint("exm", "flnd_example:Example", _plugins.GROUP)
        with mock.patch.object(metadata, "entry_points", return_value=[point]):
            rows = self.fl.sources
        found = [(row.code, row.origin, bool(row.disabled)) for row in rows]
        self.assertIn(("ins", "built-in", False), found)
        self.assertIn(("ins", "file", True), found)
        self.assertIn(("exm", "package", False), found)
        self.assertIn(("exm", "file", True), found)
        self.assertEqual(SOURCES["exm"].__name__, "Example")
        self.assertIn("ins: disabled", self.dest.getvalue())
        self.assertIn("taken by the built-in Instagram", self.dest.getvalue())

    def test_a_script_adds_after_the_plugins_and_is_refused_a_taken_code(self):
        self.write("exm.py", plugin("exm"))
        Other = type(SOURCES["exm"])("Other", (SOURCES["exm"],), {})
        with self.assertRaises(flnd.UsageError) as caught:
            self.fl.add(Other)
        self.assertIn("exm: the code is taken by", str(caught.exception))
        Own = type(SOURCES["exm"])("Own", (SOURCES["exm"],), {"code": "own"})
        self.fl.add(Own)
        self.assertEqual(self.rows()["own"].origin, "script")

    def test_sources_lists_origin_version_and_login(self):
        self.write("exm.py", plugin("exm"))
        rows = self.rows()
        self.assertEqual((rows["ins"].origin, rows["ins"].version), ("built-in", flnd.__version__))
        exm = rows["exm"]
        self.assertEqual((exm.origin, exm.version, exm.api, exm.login),
                         ("file", "2.1", 1, "needs no login"))
        self.assertEqual(exm.forms, ("exm:<user>", "https://www.exm.example/<user>/"))

    def test_a_folder_that_isnt_there_holds_no_plugin(self):
        os.environ[_plugins.VARIABLE] = str(self.folder / "missing")
        self.assertEqual(list(SOURCES), ["tik", "fbk", "ins", "twt", "loi", "tlg", "mht"])

    def test_the_default_folder_is_the_users_config(self):
        del os.environ[_plugins.VARIABLE]
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(_plugins.folder(), Path.home() / ".config" / "flnd" / "sources")
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"APPDATA": str(self.folder)}):
            self.assertEqual(_plugins.folder(), self.folder / "flnd" / "sources")


class ContainmentTest(PluginCase):
    def disabled(self, code: str) -> str:
        """Why a plugin is disabled, after checking the others still work."""
        rows = self.rows()
        self.assertFalse(rows["exm"].disabled)
        self.assertEqual(self.fl._resolve(["exm:example_user"])[0].source, "exm")
        return rows[code].disabled

    def test_a_syntax_error_disables_that_plugin_alone(self):
        self.write("exm.py", plugin("exm"))
        path = self.write("bad.py", "x = 1\ndef broken(:\n")
        self.assertIn(f"{path}, line 2", self.disabled("bad"))
        self.assertIn("bad: disabled", self.dest.getvalue())

    def test_an_import_error_names_the_file_and_line(self):
        self.write("exm.py", plugin("exm"))
        path = self.write("bad.py", "import os\nimport flnd_no_such_module\n")
        reason = self.disabled("bad")
        self.assertIn(f"{path}, line 2: ModuleNotFoundError", reason)

    def test_a_newer_source_api_is_refused(self):
        self.write("exm.py", plugin("exm"))
        path = self.write("new.py", plugin("new", api=2))
        reason = self.disabled("new")
        self.assertIn(f"{path}, line 7: new needs source API 2; this flnd has 1; update flnd",
                      reason)

    def test_an_older_source_api_points_to_the_changes(self):
        self.write("old.py", plugin("old"))
        [found] = _plugins.discover({})
        # A flnd of source API 2 meets a plugin written for 1.
        with mock.patch("flnd.source.API", 2):
            self.assertIsNone(_plugins.load(found))
        reason = found.error
        self.assertIn("old was written for source API 1; this flnd has 2; see "
                      "the API versions part of the writing-a-source page", reason)

    def test_a_class_with_another_code_is_refused(self):
        self.write("exm.py", plugin("exm"))
        self.write("odd.py", plugin("xyz"))
        self.assertIn("its source's code is 'xyz', not its file's name; name the file xyz.py",
                      self.disabled("odd"))

    def test_a_file_name_that_isnt_a_code_is_refused(self):
        self.write("exm.py", plugin("exm"))
        self.write("Bad-Name.py", plugin("exm"))
        self.assertIn("isn't a source code", self.disabled("Bad-Name"))

    def test_a_disabled_plugin_is_an_unknown_source(self):
        self.write("bad.py", "raise RuntimeError('no')\n")
        with self.assertRaises(flnd.UsageError):
            self.fl._resolve(["bad:example_user"])
        self.assertNotIn("bad", list(SOURCES))

    def test_a_listing_that_raises_fails_its_target_alone(self):
        self.write("exm.py", plugin("exm"))
        run = self.fl.download(["exm:broken", "exm:example_user"])
        failed = [event for event in run if isinstance(event, flnd.Failed)]
        self.assertEqual(run.report.saved, 1)
        [event] = failed
        self.assertEqual(str(event.target), "exm:broken")
        self.assertIsInstance(event.error, flnd.SourceError)
        self.assertEqual(str(event.error), "unexpected KeyError: 'id'")
        self.assertIn(f"a bug in the plugin exm ({self.folder / 'exm.py'})", event.error.hint)

    def test_an_async_category_that_holds_the_loop_warns(self):
        self.write("slow.py", '''
            import time
            from flnd.source import File, Post, Source, target

            class Slow(Source):
                code, domains, api = "slow", ("slow.example",), 1

                @target("slow:{user}")
                async def user(self, site, user):
                    time.sleep(0.05)
                    yield Post("p1", [File("https://cdn.example.com/p1.jpg")])
            ''')
        with mock.patch.object(job, "HELD", 0.01):
            self.assertEqual(self.fl.download("slow:example_user").wait().saved, 1)
        self.assertIn("Slow.user held the loop", self.dest.getvalue())


class NeverFromAHomeTest(PluginCase):
    """A home may be a folder someone else made: code in it must never run."""

    def setUp(self):
        super().setUp()
        self.marker = self.home.parent / "PWNED"
        evil = f"open({str(self.marker)!r}, 'w').close()\n" + plugin("evil")
        for folder in (self.home / "sources", self.home.parent / "sources"):
            folder.mkdir(parents=True)
            (folder / "evil.py").write_text(evil, encoding="utf-8")

    def test_no_read_or_command_loads_it(self):
        self.assertNotIn("evil", self.rows())
        with self.assertRaises(flnd.UsageError):
            self.fl.download("evil:example_user")
        self.fl.logins
        with contextlib.chdir(self.home.parent), contextlib.redirect_stdout(io.StringIO()):
            flnd_cli.main(["--home", str(self.home), "sources"])
            flnd_cli.main(["login"])
        self.assertFalse(self.marker.exists())


class CliTest(PluginCase):
    def run_app(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(self.dest):
            code = flnd_cli.main(["--home", str(self.home), *argv])
        return code, out.getvalue()

    def test_sources_shows_plugins_and_why_one_is_disabled(self):
        self.write("exm.py", plugin("exm"))
        self.write("bad.py", "x = (\n")
        code, out = self.run_app("sources")
        self.assertEqual(code, 0)
        self.assertIn("ins  Instagram  (built-in 0.1.0)", out)
        self.assertIn("exm  Example  (file 2.1)", out)
        self.assertIn(f"  from:       {self.folder / 'exm.py'}", out)
        self.assertIn("  login:      needs no login", out)
        self.assertIn("bad  bad  (file)\n  disabled:   ", out)

    def test_login_lists_plugin_sources(self):
        self.write("exm.py", plugin("exm"))
        code, out = self.run_app("login")
        self.assertEqual(code, 0)
        self.assertIn("exm ", out)


class HelpTest(unittest.TestCase):
    def test_help_imports_no_plugin(self):
        with TemporaryDirectory() as folder:
            marker = Path(folder) / "IMPORTED"
            Path(folder, "exm.py").write_text(f"open({str(marker)!r}, 'w').close()\n",
                                              encoding="utf-8")
            env = {**os.environ, _plugins.VARIABLE: folder}
            subprocess.run([sys.executable, "-m", "flnd", "--help"], cwd=ROOT, env=env,
                           capture_output=True, check=True)
            self.assertFalse(marker.exists())


# ---------------------------------------------------------------------------------------------
# The plugin manager (`flnd.engine.manager`, PLAN.md 6.6), built early: nothing calls it yet.
# ---------------------------------------------------------------------------------------------

# An API 2 plugin; it touches `{marker}` each time it is imported.
API2 = """\
from pathlib import Path
from flnd.plugin import Plugin, route

Path({marker!r}).touch()


class Example(Plugin):
    code, name, api, domains = "{code}", "{name}", {api}, ("{code}.example",)
    unsupported = {{"{code}.example/explore": "explore pages aren't targets"}}

    @route("{code}:{{user}}", "{code}.example/{{user}}")
    async def user(self, ctx, user):
        return {{}}

    @user.category
    async def posts(self, ctx, profile):
        yield None

    @user.category(own=False)
    async def tagged(self, ctx, profile):
        yield None
"""


class ManagerCase(unittest.TestCase):
    """A plugin folder and the files to install, in a temporary folder; a fake clock."""

    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name).resolve()
        self.folder, self.marker = self.root / "plugins", self.root / "IMPORTED"
        self.enterContext(clock.use(clock.Fake()))
        self.enterContext(mock.patch.dict(sys.modules))
        self.manager = self.fresh()

    def write(self, where: str, text: str = "", **fields) -> Path:
        """A file under the temporary folder: `text`, or an API 2 plugin made from `fields`."""
        path = self.root / where
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or self.plugin(**fields), encoding="utf-8")
        return path

    def plugin(self, **fields) -> str:
        return API2.format(marker=str(self.marker), **{"code": "exm", "name": "Example",
                                                       "api": 2, **fields})

    def fresh(self) -> _manager.Manager:
        """A manager reading the folder anew, as the next command does."""
        return _manager.Manager(self.folder, built_in=("ins", "tik"))

    def imported(self) -> bool:
        """Whether a plugin was imported since the last call."""
        found = self.marker.exists()
        self.marker.unlink(missing_ok=True)
        return found


class InstallTest(ManagerCase):
    def test_install_a_file_and_a_package(self):
        entry = self.manager.install(self.write("src/my_plugin.py"))
        self.assertEqual(entry["file"], "exm.py")
        self.assertEqual(entry["sha256"], hashlib.sha256(
            (self.folder / "exm.py").read_bytes()).hexdigest())
        self.assertEqual(entry["installed"], "2026-01-01T00:00:00Z")
        self.assertEqual(entry["routes"], [{
            "name": "user", "single": False, "discovers": False, "shown": None,
            "patterns": ["exm:{user}", "exm.example/{user}"],
            "categories": [{"name": "posts", "own": True}, {"name": "tagged", "own": False}]}])
        self.assertEqual(json.loads((self.folder / "plugins.json").read_text())["plugins"],
                         {"exm": entry})
        self.write("src/exp/__init__.py", "from .site import Example\n")
        self.write("src/exp/site.py", code="exp")
        self.assertEqual(self.manager.install(self.root / "src" / "exp")["file"], "exp")
        self.assertTrue((self.folder / "exp" / "site.py").is_file())
        manager = self.fresh()
        self.assertEqual((manager.load("exm").code, manager.load("exp").code), ("exm", "exp"))

    def test_install_replaces_one(self):
        self.manager.install(self.write("src/exm.py"))
        self.assertEqual(self.manager.load("exm").name, "Example")
        self.manager.install(self.write("src/exm/__init__.py", name="Example 2").parent)
        self.assertEqual(self.manager.load("exm").name, "Example 2")
        self.assertFalse((self.folder / "exm.py").exists())
        self.assertEqual(self.fresh().manifest["plugins"]["exm"]["file"], "exm")

    def test_refusals(self):
        def unreadable(name: str) -> Path:
            # Mode 0: reading it would raise PermissionError, not the refusal.
            path = self.write(name, "never read")
            path.chmod(0)
            return path

        def link(name: str, to: Path) -> Path:
            (self.root / name).symlink_to(to)
            return self.root / name

        def hard(name: str) -> Path:
            os.link(self.write(f"{name}.real"), self.root / name)
            return self.root / name

        def fifo(name: str) -> Path:
            os.mkfifo(self.root / name)
            return (self.root / name).parent

        def recoded(name: str, code: str) -> Path:
            # The code changed once the class is made, which only a check after the run sees.
            return self.write(name, self.plugin() + f"\nExample.code = {code!r}\n")

        def lying(name: str, shown: str) -> Path:
            # A str subclass that reads "exm" but formats as `shown`, in a path's f-string.
            return self.write(name, self.plugin() + "\n\nclass Lie(str):\n    def __format__("
                              f"self, spec):\n        return {shown!r}\n\n\n"
                              'Example.code = Lie("exm")\n')

        # Where `.{name}.old` would point if a lying code were formatted into it.
        kept = self.write("src/victim.old/keep.txt", "kept")

        # Two folders down, so `../../evil` would land in the temporary folder, not above it.
        self.folder = self.root / "deep" / "plugins"
        self.manager = self.fresh()
        victim = self.write("src/victim/thesis.txt", "years of work")
        two = self.plugin() + '\n\nclass Other(Example):\n    code = "exo"\n'
        home = "is a home or cookie file"
        changed = "its code is .* once it has run"
        cases = {
            "a code that climbs, after the class": (lambda: recoded("src/c1.py", "../../evil"),
                                                    SourceError, changed),
            "a code in capitals, after the class": (lambda: recoded("src/c2.py", "EXM"),
                                                    SourceError, changed),
            "an absolute code, after the class": (
                lambda: recoded("src/pv/__init__.py", str(victim.parent)).parent, SourceError,
                changed),
            "a lying code": (lambda: lying("src/c3.py", "../../evil"), SourceError, changed),
            "a lying code, in a package": (
                lambda: lying("src/pz/__init__.py", "/../../src/victim").parent, SourceError,
                changed),
            "a package holding a pipe": (lambda: fifo("src/ps/pipe"), UsageError,
                                         "holds .*ps/pipe, a special file"),
            "a built-in's code": (lambda: self.write("src/ins.py", code="ins"), UsageError,
                                  "ins is a built-in"),
            "in a home": (lambda: self.write("src/.flnd/exm.py"), UsageError, home),
            # The disk may ignore case, so names are compared casefolded.
            "in a home, in capitals": (lambda: self.root / "src/.FLND/exm.py", UsageError, home),
            "a cookie file": (lambda: unreadable("src/x_cookies.txt"), UsageError, home),
            "a package holding cookies": (lambda: unreadable("src/pc/a_cookies.txt").parent,
                                          UsageError, "holds .*a_cookies.txt"),
            "a package holding cookies, in capitals": (
                lambda: unreadable("src/pu/A_COOKIES.TXT").parent, UsageError,
                "holds .*A_COOKIES.TXT"),
            "a hard link": (lambda: hard("src/hl.py"), UsageError, "other hard links"),
            "a package holding a home": (lambda: unreadable("src/ph/.flnd/flnd.db").parent.parent,
                                         UsageError, r"holds .*\.flnd"),
            "a link": (lambda: link("src/l.py", self.write("src/real.py")), UsageError,
                       "is a link"),
            "a package holding a link": (lambda: link("src/pl/up", self.root).parent, UsageError,
                                         "holds .*pl/up"),
            "neither a file nor a package": (lambda: self.write("src/notes.txt", "x"),
                                             UsageError, "neither"),
            "a code that climbs": (lambda: self.write("src/up.py", code="../../up"),
                                   SourceError, r"src/up\.py, line 7: Example\.code"),
            "two plugins": (lambda: self.write("src/two.py", two), SourceError,
                            "defines Example, Other"),
        }
        for path in ("src/pc", "src/ph", "src/pl", "src/pu", "src/ps"):
            self.write(f"{path}/__init__.py")
        for label, (make, error, said) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(error, said):
                self.manager.install(make())
        self.assertFalse(self.folder.exists())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["IMPORTED", "src"])
        self.assertEqual((victim.read_text(), kept.read_text()), ("years of work", "kept"))

    def test_an_api_it_cant_read_is_refused_unimported(self):
        decoy = "class Decoy:\n    api = 2\n\n\n"
        cases = {
            "a sum": self.plugin(api="1 + 2"),
            "a float": self.plugin(api="2.0"),
            "a variable": "API = 3\n" + self.plugin(api="API"),
            "an if": self.plugin().replace("    unsupported", "    if True:\n        api = 3\n"
                                           "    unsupported"),
            "a decoy": self.plugin(api="int('3')").replace("\n\nclass Example", decoy + "class "
                                                           "Example"),
            "an annotation": self.plugin().replace("    unsupported",
                                                   "    api: int = 3\n    unsupported"),
            "none": self.plugin().replace(", api, ", ", ").replace(", 2, (", ", ("),
        }
        for label, text in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(SourceError, "(?i)api"):
                    self.manager.install(self.write("src/exm.py", text))
                self.assertFalse(self.imported())
        # Set after the class is made: it runs, but its entry never says api 3.
        with self.assertRaisesRegex(SourceError, "(?i)api"):
            self.manager.install(self.write("src/exm.py", self.plugin() + "\nExample.api = 3\n"))
        self.assertFalse(self.folder.exists())

    def test_a_file_swapped_after_its_check_is_never_read(self):
        cookies = self.write("x_cookies.txt", "never read")
        cookies.chmod(0)
        fifo, writers = self.root / "src/exm.py", []

        def to_link():
            fifo.unlink()
            fifo.symlink_to(cookies)

        def to_fifo():
            fifo.unlink()
            os.mkfifo(fifo)
            # A writer, so the unfixed code's read ends rather than hangs.
            writers.append(threading.Thread(target=write))
            writers[-1].start()

        def write():
            with contextlib.suppress(OSError):
                fifo.write_text("x = 1\n")

        def to_other():
            # A regular file with one link, renamed in: only its inode tells it apart.
            os.replace(self.write("src/other.py"), fifo)

        def swapping(name, after, swap):
            """`name`, swapping the file once it has been called `after` times."""
            real, calls = getattr(_manager, name), []

            def swapped(*args, **kwargs):
                found = real(*args, **kwargs)
                calls.append(args)
                if len(calls) == after:
                    swap()
                return found
            return swapped

        # After the check of the source; then after the copy has listed it, before its read.
        swaps = [("_refused", 1, "a link", to_link), ("_refused", 1, "a fifo", to_fifo),
                 ("_tree", 2, "a link", to_link), ("_tree", 2, "a fifo", to_fifo),
                 ("_tree", 2, "another file", to_other)]
        for name, after, label, swap in swaps:
            self.write("src/exm.py")
            with self.subTest(label, after=name), mock.patch.object(
                    _manager, name, swapping(name, after, swap)):
                with self.assertRaises(UsageError) as caught:
                    self.manager.install(fifo)
                self.assertIn(str(fifo), str(caught.exception))
                self.assertNotIn("flnd-install-", str(caught.exception))
            for writer in writers:
                fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
                writer.join()
                os.close(fd)
            writers.clear()
            fifo.unlink()
        self.assertFalse(self.folder.exists())

        # A pipe nobody writes to: the read must not wait for a writer. Bounded, so a read
        # that waits fails here instead of hanging the suite.
        self.write("src/exm.py")
        raised = []

        def install():
            try:
                self.manager.install(fifo)
            except Exception as error:
                raised.append(error)

        with mock.patch.object(_manager, "_tree", swapping("_tree", 2, lambda: (
                fifo.unlink(), os.mkfifo(fifo)))):
            worker = threading.Thread(target=install, daemon=True)
            worker.start()
            worker.join(10)
            waited = worker.is_alive()
            if waited:
                os.close(os.open(fifo, os.O_WRONLY))  # lets the waiting read go
                worker.join()
        self.assertFalse(waited, "the read waited for a writer")
        self.assertIsInstance(raised[0], UsageError)

    def test_the_copy_is_checked_again(self):
        real = _manager._copy

        def sneaky(source, dest, *args):
            real(source, dest, *args)
            (dest / "up").symlink_to(self.root)

        self.write("src/pkg/__init__.py")
        with mock.patch.object(_manager, "_copy", sneaky), self.assertRaisesRegex(
                UsageError, "holds .*up"):
            self.manager.install(self.root / "src/pkg")
        # Refused before it is imported, not only before it is kept.
        self.assertFalse(self.imported())
        self.assertFalse(self.folder.exists())


class LoadTest(ManagerCase):
    def test_an_edited_file_is_checked_again(self):
        self.manager.install(self.write("src/exm.py"))
        before = self.fresh().manifest["plugins"]["exm"]
        self.write("plugins/exm.py", name="Edited")
        self.assertEqual(self.fresh().load("exm").name, "Edited")
        after = self.fresh().manifest["plugins"]["exm"]
        self.assertNotEqual(after["sha256"], before["sha256"])
        self.assertEqual((after["name"], after["installed"]), ("Edited", before["installed"]))

    def test_a_file_without_an_entry_is_ignored(self):
        self.manager.install(self.write("src/exm.py"))
        stray = self.write("plugins/stray.py", code="stray")
        manager = self.fresh()
        self.assertEqual(list(manager.manifest["plugins"]), ["exm"])
        self.assertEqual(manager.warnings,
                         [f"{stray} isn't installed: flnd plugin install {stray}"])
        self.imported()
        with self.assertRaises(UsageError):
            manager.load("stray")
        self.assertFalse(self.imported())
        # What the warning says to do: install it where it is.
        manager.install(stray)
        self.assertEqual(self.fresh().load("stray").code, "stray")

    def test_another_api_is_disabled_with_its_hint(self):
        self.manager.install(self.write("src/exm.py"))
        old = self.plugin(api=1).replace(
            "    unsupported", '    needs = "sessionid"\n    single_kinds = ("post",)\n'
            "    unsupported")
        cases = {3: (self.plugin(api=3), "it needs a newer flnd"),
                 1: (old, 'needs becomes login = cookies("<name>", url=...); single_kinds '
                          "becomes @route(..., single=True) on each one-post route")}
        for api, (text, hint) in cases.items():
            with self.subTest(api=api):
                self.write("plugins/exm.py", text)
                self.imported()
                manager = self.fresh()
                with self.assertRaises(SourceError) as caught:
                    manager.load("exm")
                self.assertIn(f"written for plugin API {api}", str(caught.exception))
                self.assertEqual(caught.exception.hint, hint)
                self.assertEqual(manager.disabled["exm"], f"{caught.exception.__cause__}; {hint}")
                with self.assertRaises(SourceError):
                    self.manager.install(self.write("src/exm.py", text))
                self.assertFalse(self.imported())

    def test_a_broken_plugin_names_its_own_file_and_line(self):
        # S7: a frame in `exm.py` is not the package `ex`'s, whatever their names share. Each
        # plugin declares its API plainly, so it is imported and fails there.
        exm = self.folder / "exm.py"
        cases = {
            "exm.py": ("x = 1\nraise KeyError('id')\n", f"{exm}, line 2: KeyError: 'id'"),
            "ex": (f"exec(compile('raise KeyError(1)', {str(exm)!r}, 'exec'))\n",
                   f"{self.folder / 'ex' / '__init__.py'}, line 1: KeyError: 1"),
        }
        for name, (text, said) in cases.items():
            with self.subTest(name):
                code = name.removesuffix(".py")
                where = f"plugins/{name}" + ("/__init__.py" if code == name else "")
                self.write(where, text + self.plugin(code=code))
                self.write("plugins/plugins.json", json.dumps(
                    {"version": 1, "plugins": {code: {"file": name, "api": 2}}}))
                manager = self.fresh()
                with self.assertRaises(SourceError):
                    manager.load(code)
                self.assertEqual(manager.disabled[code], said)

    def test_resolution_imports_no_plugin(self):
        self.manager.install(self.write("src/exm.py"))
        self.assertTrue(self.imported())
        prefix = _manager.MODULE_PREFIX
        self.assertFalse([name for name in sys.modules if name.startswith(prefix)])
        manifest = Manifest({}, self.fresh().manifest)
        for text in ("exm:example_user", "https://www.exm.example/example_user/"):
            with self.subTest(text):
                found = resolve(text, manifest=manifest, known={})
                self.assertEqual((found.code, found.kind, found.value, found.categories),
                                 ("exm", "user", "example_user", ("posts",)))
        with self.assertRaisesRegex(UsageError, "explore pages"):
            resolve("https://exm.example/explore", manifest=manifest, known={})
        self.assertFalse(self.imported())

    def test_an_edit_that_fails_keeps_the_entry(self):
        self.manager.install(self.write("src/exm.py"))
        before = self.fresh().manifest["plugins"]["exm"]
        self.write("plugins/exm.py", code="zzz")
        with self.assertRaisesRegex(SourceError, "'zzz'"):
            self.fresh().load("exm")
        self.assertEqual(self.fresh().manifest["plugins"]["exm"], before)

    def test_what_load_refuses(self):
        outside = self.write("x.py", f"open({str(self.marker)!r}, 'w').close()\n")
        cookies = self.write("z_cookies.txt", "never read")
        self.manager.install(self.write("src/exp/__init__.py", code="exp").parent)
        (self.folder / "exp" / "leak.txt").symlink_to(cookies)
        (self.folder / "exm.py").symlink_to(outside)
        entries = {"climbs": "../x.py", "absolute": str(outside), "linked": "exm.py",
                   "a link inside": "exp"}
        for label, file in entries.items():
            code = "exp" if file == "exp" else "exm"
            entry = {**self.fresh().manifest["plugins"].get(code, {}), "file": file, "api": 2}
            self.write("plugins/plugins.json", json.dumps(
                {"version": 1, "plugins": {code: entry}}))
            self.imported()
            with self.subTest(label), self.assertRaisesRegex(SourceError, "link|not exm"):
                self.fresh().load(code)
            self.assertFalse(self.imported(), label)

    def test_a_built_in_code_in_the_index_is_ignored(self):
        self.write("plugins/ins.py", code="ins")
        self.write("plugins/plugins.json", json.dumps(
            {"version": 1, "plugins": {"ins": {"file": "ins.py", "api": 2, "sha256": ""}}}))
        manager = self.fresh()
        self.assertEqual(manager.manifest["plugins"], {})
        self.assertEqual(manager.warnings, [
            f"{self.folder / 'plugins.json'} lists ins, a built-in plugin's code; "
            "that entry is ignored",
            f"{self.folder / 'ins.py'} has a built-in plugin's code; it is ignored"])
        for act in (manager.load, manager.uninstall):
            with self.subTest(act.__name__), self.assertRaisesRegex(UsageError, "built in"):
                act("ins")
        self.assertTrue((self.folder / "ins.py").exists())
        self.assertFalse(self.imported())


class UninstallTest(ManagerCase):
    def test_uninstall(self):
        self.manager.install(self.write("src/exm.py"))
        self.assertEqual(self.manager.uninstall("exm")["file"], "exm.py")
        self.assertEqual(list(self.folder.iterdir()), [self.folder / "plugins.json"])
        self.assertEqual(self.fresh().manifest["plugins"], {})
        self.assertEqual(self.manager.warnings,
                         ["exm is uninstalled; its accounts and targets stay"])
        for code in ("ins", "exm", "../plugins.json"):
            with self.subTest(code), self.assertRaises(UsageError):
                self.manager.uninstall(code)
        self.assertTrue((self.folder / "plugins.json").exists())

    def test_the_folder(self):
        with mock.patch.dict(os.environ, {_manager.VARIABLE: str(self.folder)}):
            self.assertEqual(_manager.folder(), self.folder)
        with mock.patch.dict(os.environ, {_manager.VARIABLE: ""}), \
                self.assertRaises(RuntimeError):
            _manager.folder()


if __name__ == "__main__":
    unittest.main()
