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
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import textwrap
import unittest
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import no_profiles  # noqa: F401  (also points $FLND_SOURCES away from the real folder)
from test_engine import EngineCase, noise

import flnd
from flnd import cli as flnd_cli
from flnd.engine import job
from flnd.engine import plugins as _plugins
from flnd.kit import net as _net
from flnd.sources import SOURCES, _find

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
                      "flnd/docs/writing-a-source.md#source-api-versions", reason)

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


if __name__ == "__main__":
    unittest.main()
