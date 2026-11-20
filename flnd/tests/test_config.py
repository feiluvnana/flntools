"""config.toml (`flnd.engine.config`): its structs, reading, resolution and editing, and where
each setting takes effect: a plugin's run, its templates, its `ctx.settings`, a call's `when`.

Every file is in a temporary folder; the user file is passed in, never the real one.
"""

import datetime
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import msgspec
from example_plugin import Example as CtxExample
from fakes import Engine
from test_ctx import CtxCase
from test_library import LibraryCase, example_source

import flnd
import flnd.plugin
from flnd.engine import config
from flnd.engine.config import Config, Where
from flnd.errors import UsageError
from flnd.plugin import Range, Target
from flnd.testing import fake_clock
from flnkit import clock


class Settings(msgspec.Struct, frozen=True):
    quality: str = "best"
    tries: int = 3


class Browser:
    name = "NONE"


class Example:
    fields = ("title",)
    Settings = Settings
    browser = Browser


PLUGINS = {"exm": Example}


class Case(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "home"
        self.home.mkdir()
        self.user = Path(folder.name) / "user" / "config.toml"
        self.user.parent.mkdir()
        self.warn = self.enterContext(mock.patch.object(config.ui, "warn"))

    def files(self, home="", user=""):
        (self.home / "config.toml").write_text(home, encoding="utf-8")
        self.user.write_text(user, encoding="utf-8")

    def read(self, home="", user="", **given):
        self.files(home, user)
        return config.read(self.home, user=self.user, plugins=PLUGINS, given=given)

    def warned(self):
        return " ".join(str(call.kwargs) + str(call.args) for call in self.warn.call_args_list)


class ReadTest(Case):
    def test_missing_files_are_every_default(self):
        found = config.read(self.home, user=self.user)
        self.assertEqual(found, Config())
        self.assertEqual(found.defaults.exec_timeout, 3600)
        self.assertEqual(found.plugin("ins").stop_after, 5)
        self.assertEqual(found.origin("when"), Where.DEFAULT)

    def test_each_key_takes_its_type(self):
        rows = [
            ("[defaults]\ndedupe = false", "defaults.dedupe", False),
            ("[defaults]\nsidecars = true", "defaults.sidecars", True),
            ('[defaults]\nwhen = "2026-01..2026-03"', "defaults.when", "2026-01..2026-03"),
            ('[defaults]\nrate = "20 MB/s"', "defaults.rate", "20 MB/s"),
            ('[defaults]\nslowest = "500 KB/s"', "defaults.slowest", "500 KB/s"),
            ("[defaults]\nheight = 720", "defaults.height", 720),
            ("[defaults]\nexec_timeout = 0", "defaults.exec_timeout", 0),
            ("[defaults]\nswitch = true", "defaults.switch", True),
            ("[defaults]\nlean = true", "defaults.lean", True),
            ("[defaults]\nwindow = \"hidden\"", "defaults.window", "hidden"),
            ("[plugins.exm]\npace = 2", "plugins.exm.pace", 2.0),
            ('[plugins.exm]\nlimits = { "cdn.example.com" = 4 }', "plugins.exm.limits",
             {"cdn.example.com": 4}),
            ('[plugins.exm]\nproxy = "socks5://127.0.0.1:1080"', "plugins.exm.proxy",
             "socks5://127.0.0.1:1080"),
            ("[plugins.exm]\nstop_after = 9", "plugins.exm.stop_after", 9),
            ("[plugins.exm]\nswitch = true", "plugins.exm.switch", True),
            ('[plugins.exm]\nfolder = "{category}/{title}"', "plugins.exm.folder",
             "{category}/{title}"),
            ('[plugins.exm]\nfilename = "{date}_{post}"', "plugins.exm.filename",
             "{date}_{post}"),
            ('[plugins.exm]\nquality = "720p"', "plugins.exm.quality", "720p"),
            ("[plugins.exm]\ntries = 5", "plugins.exm.tries", 5),
        ]
        for text, key, value in rows:
            with self.subTest(key):
                found = self.read(home=text)
                _, table, *rest = ["", *key.split(".")]
                got = (getattr(found.defaults, rest[0]) if table == "defaults"
                       else self._plugin_value(found.plugin(rest[0]), rest[1]))
                self.assertEqual(got, value)
                self.assertEqual(found.origin(key), Where.HOME)

    @staticmethod
    def _plugin_value(plugin, name):
        if hasattr(plugin, name):
            return getattr(plugin, name)
        return getattr(plugin.settings, name)

    def test_a_wrong_value_names_the_key(self):
        rows = [
            '[defaults]\ndedupe = "yes"', "[defaults]\nsidecars = 1", '[defaults]\nwhen = "soon"',
            '[defaults]\nwhen = "2026-03..2026-01"', '[defaults]\nrate = "fast"',
            "[defaults]\nheight = -1", "[defaults]\nexec_timeout = -1", '[defaults]\nlean = "yes"', '[defaults]\nwindow = "shown"',
            '[defaults]\nexec_timeout = "9"',
            '[defaults]\nheight = "720"', '[plugins.exm]\npace = "2"', "[plugins.exm]\npace = -1",
            '[plugins.exm]\nlimits = { "a" = 0 }', '[plugins.exm]\nproxy = "ftp://h:1"',
            '[plugins.exm]\nproxy = "http://h:99999"', '[plugins.exm]\nproxy = "http://h:1\\n"',
            "[plugins.exm]\nstop_after = 0",
            '[plugins.exm]\nbrowser = "chrome"', '[plugins.exm]\nfolder = "../out"',
            '[plugins.exm]\nfilename = "{nope}"', '[plugins.exm]\nfilename = "{post"',
            '[plugins.exm]\ntries = "x"', "[plugins]\nexm = 1", "defaults = 1",
        ]
        rows = [(text, "home") for text in rows]
        rows += [('[defaults]\nexec = "echo"', "user"), ('[defaults]\nexec = [""]', "user")]
        for text, where in rows:
            with self.subTest(text):
                with self.assertRaises(UsageError) as caught:
                    self.read(**{where: text})
                key = text.splitlines()[-1].split(" ")[0]
                self.assertIn(key, str(caught.exception))
                path = self.home / "config.toml" if where == "home" else self.user
                self.assertIn(str(path), str(caught.exception))

    def test_no_message_repeats_a_value(self):
        mark = "SECRETMARK123"
        url = f"http://u:{mark}@h:1"
        texts = [
            f'plugins = "{url}"', f'defaults = "{url}"', f'sources = "{url}"',
            f'proxy = "{url}"', f'[plugins]\nproxy = "{url}"', f'[plugins]\nexm = "{url}"',
            f'[sources]\nexm = "{url}"', f'[sources]\nproxy = "{url}"',
            f'[defaults]\nproxy = "{url}"', f'[defaults]\nagain = "{mark}"',
            f'[vault]\nkey = "{mark}"', f'[plugins.exm]\nscroll = "{mark}"',
            f'[plugins.zzz]\nodd = "{mark}"', f'[sources.exm]\nproxy = "ftp://u:{mark}@h:1"',
            f'[plugins.exm]\nproxy = "http://u:{mark}@h:1" x',
            f'[plugins.exm]\nproxy = "http://u:\\q{mark}@h:1"',
            f'[plugins.exm]\nproxy = "http://u:{mark}@h:1',
            f'[defaults]\nexec = ["{mark}"]',
        ]
        values = [
            ("plugins.exm.proxy", f"gopher://u:{mark}@h:1"), ("plugins.exm.proxy", url + "\n"),
            ("plugins.exm.proxy", f"u:{mark}@host:1080"),
            ("plugins.exm.proxy", f"http:/u:{mark}@h"),
            ("plugins.exm.proxy", f"http//u:{mark}@h:1"), ("plugins.exm.proxy", url + "99999"),
            ("plugins.exm.proxy", f"http://u:{mark}@[bad"), ("plugins.exm.proxy", [url]),
            ("plugins.exm.proxy", {"u": mark}), ("plugins.exm.pace", mark),
            ("plugins.exm.limits", {f"u:{mark}@h": 0}), ("plugins.exm.limits", mark),
            ("plugins.exm.folder", f"../{mark}"), ("plugins.exm.filename", f"{{{mark}}}"),
            ("plugins.exm.filename", f"{{{mark}"), ("plugins.exm.browser", mark),
            ("plugins.exm.tries", mark), ("plugins.exm.stop_after", [mark]),
            ("defaults.when", mark), ("defaults.rate", mark), ("defaults.slowest", f"1 {mark}/s"),
            ("defaults.dedupe", mark), ("defaults.height", {"a": mark}), ("defaults.exec", mark),
            ("defaults.exec", [""]), ("plugins.exm", url), ("plugins.proxy", url),
        ]
        runs = [(text, lambda text=text: self.read(home=text)) for text in texts]
        for key, value in values:
            runs.append((key, lambda key=key, value=value: config.read(
                self.home, user=self.user, plugins=PLUGINS, given={key: value})))
            runs.append((key, lambda key=key, value=value: config.write(
                key, value, home=self.home, user=self.user, plugins=PLUGINS)))
        for name, run in runs:
            with self.subTest(name):
                self.warn.reset_mock()
                self.files()
                try:
                    run()
                    shown = ""
                except UsageError as error:
                    shown = str(error) + (error.hint or "")
                self.assertNotIn(mark, shown + self.warned())

    def test_unknown_keys_and_gone_keys_warn(self):
        found = self.read(home="[defaults]\nagain = true\n[vault]\nkey = 'file'\n"
                               "[plugins.exm]\nscroll = 'fast'\n[plugins.zzz]\nodd = 1")
        self.assertEqual(found.defaults, config.Defaults())
        for setting in ("[defaults] again", "[vault]", "[plugins.exm] scroll",
                        "[plugins.zzz] odd"):
            self.assertIn(setting, self.warned())

    def test_dedupe_words_are_warned_and_mapped(self):
        for word, flag in (("link", True), ("off", False)):
            with self.subTest(word):
                self.assertIs(self.read(home=f'[defaults]\ndedupe = "{word}"').defaults.dedupe,
                              flag)
                self.assertIn("dedupe", self.warned())

    def test_sources_tables_read_as_plugins_with_a_warning(self):
        found = self.read(home="[sources.exm]\npace = 3\nstop_after = 2\n"
                               "[plugins.exm]\nstop_after = 7")
        self.assertEqual((found.plugin("exm").pace, found.plugin("exm").stop_after), (3.0, 7))
        self.assertIn("rename", self.warned())

    def test_resolution_order_and_where(self):
        found = self.read(home="[defaults]\nheight = 720\nsidecars = true",
                          user="[defaults]\nheight = 480\nwhen = '1y'\nswitch = true",
                          sidecars=False)
        rows = [("sidecars", False, Where.ARGUMENT), ("height", 720, Where.HOME),
                ("when", "1y", Where.USER), ("rate", "0", Where.DEFAULT)]
        for key, value, where in rows:
            with self.subTest(key):
                self.assertEqual(getattr(found.defaults, key), value)
                self.assertEqual(found.origin(key), where)
        self.assertIs(found.plugin("exm").switch, True)
        self.assertEqual(found.origin("plugins.exm.switch"), Where.USER)
        self.assertEqual(found.plugin("exm").settings, Settings())

    def test_exec_is_never_a_call_argument(self):
        for key in ("exec", "defaults.exec"):
            with self.subTest(key), self.assertRaises(UsageError) as caught:
                config.read(self.home, user=self.user, given={key: ["notify"]})
            self.assertIn("user file", str(caught.exception))

    def test_a_non_table_sources_entry_warns(self):
        found = self.read(home="[sources]\nexm = 3")
        self.assertEqual(found.plugin("exm").pace, None)
        self.assertIn("not a table", str(self.warn.call_args_list))

    def test_exec_is_read_only_from_the_user_file(self):
        found = self.read(home='[defaults]\nexec = ["rm", "-rf"]')
        self.assertEqual(found.defaults.exec, ())
        self.assertIn("exec", self.warned())
        found = self.read(user='[defaults]\nexec = ["notify", "--done"]')
        self.assertEqual(found.defaults.exec, ("notify", "--done"))
        self.assertEqual(found.origin("exec"), Where.USER)

    def test_the_real_user_file_is_refused_in_tests(self):
        self.assertEqual(os.environ.get(config.NO_PROFILES), "1")
        if sys.platform == "win32":
            real = Path(os.environ.get("APPDATA") or Path.home()) / "flnd" / "config.toml"
            with self.assertRaises(RuntimeError):
                config.read(self.home, user=real)
            return
        import pwd

        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/flnd/config.toml"
            with self.assertRaises(RuntimeError):
                config.read(self.home, user=real)


class EditTest(Case):
    def test_write_keeps_comments_and_goes_to_the_right_file(self):
        self.files(home="# my settings\n[defaults]\nheight = 720  # tall enough\n")
        rows = [("when", "1y", self.home / "config.toml"),
                ("plugins.exm.pace", 2.5, self.home / "config.toml"),
                ("plugins.exm.tries", 4, self.home / "config.toml"),
                ("defaults.exec", ["notify"], self.user)]
        for key, value, path in rows:
            with self.subTest(key):
                self.assertEqual(config.write(key, value, home=self.home, user=self.user,
                                              plugins=PLUGINS), path)
        text = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("# my settings", text)
        self.assertIn("# tall enough", text)
        found = config.read(self.home, user=self.user, plugins=PLUGINS)
        self.assertEqual((found.defaults.when, found.defaults.exec), ("1y", ("notify",)))
        self.assertEqual((found.plugin("exm").pace, found.plugin("exm").settings.tries), (2.5, 4))

    def test_a_refused_edit_leaves_the_file_as_it_was(self):
        self.files(home="[defaults]\nheight = 720\n")
        before = (self.home / "config.toml").read_text(encoding="utf-8")
        for key, value in (("height", "tall"), ("nope", 1), ("plugins.exm.nope", 1),
                           ("plugins.exm.folder", "/abs"), ("when", object()), ("when", {1})):
            with self.subTest(key), self.assertRaises(UsageError):
                config.write(key, value, home=self.home, user=self.user, plugins=PLUGINS)
        self.assertEqual((self.home / "config.toml").read_text(encoding="utf-8"), before)
        # A file a key repeats in is refused by its line, never its text.
        self.files(home='[defaults]\nwhen = "1y"\nwhen = "2y"\n')
        with self.assertRaisesRegex(UsageError, "not valid TOML"):
            config.write("dedupe", True, home=self.home, user=self.user)

    def test_a_write_is_atomic(self):
        self.files(home="[defaults]\nheight = 720\n")
        with mock.patch.object(config.files, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.write("height", 480, home=self.home, user=self.user)
        self.assertEqual((self.home / "config.toml").read_text(encoding="utf-8"),
                         "[defaults]\nheight = 720\n")
        config.write("height", 480, home=self.home, user=self.user)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.toml"])

    def test_unset_removes_a_plugins_own_key(self):
        config.write("plugins.exm.tries", 7, home=self.home, user=self.user, plugins=PLUGINS)
        config.unset("plugins.exm.tries", home=self.home, user=self.user, plugins=PLUGINS)
        found = config.read(self.home, user=self.user, plugins=PLUGINS)
        self.assertEqual(found.plugin("exm").settings.tries, 3)
        self.assertEqual(found.origin("plugins.exm.tries"), Where.DEFAULT)

    def test_unset_removes_the_key_and_empty_tables(self):
        self.files(home="[defaults]\nheight = 720\n[plugins.exm]\npace = 2\n")
        config.unset("plugins.exm.pace", home=self.home, user=self.user)
        config.unset("when", home=self.home, user=self.user)
        self.assertEqual((self.home / "config.toml").read_text(encoding="utf-8"),
                         "[defaults]\nheight = 720\n")


class NameTest(unittest.TestCase):
    def test_cut_keeps_the_extension_and_whole_characters(self):
        rows = [("a" * 300 + ".mp4", ".mp4"), ("é" * 200 + ".jpg", ".jpg"), ("日本" * 100, ""),
                ("😀" * 100 + ".json", ".json")]
        for name, end in rows:
            with self.subTest(name[:3]):
                found = config.cut(name)
                self.assertTrue(251 < len(found.encode()) <= 255)
                self.assertTrue(found.endswith(end))
                self.assertTrue(name.startswith(found.removesuffix(end)))
        self.assertEqual(config.cut("short.txt"), "short.txt")

    def test_speed(self):
        for value, n in (("0", 0), ("20 MB/s", 20 * 1024 ** 2), ("500 KB/s", 512000),
                         ("1.5kb/s", 1536)):
            with self.subTest(value):
                self.assertEqual(config.speed(value), n)


class WhenTest(unittest.TestCase):
    def test_periods_ranges_and_spans(self):
        day = datetime.date
        for text, since, until in [
            (None, None, None), ("2026", day(2026, 1, 1), day(2026, 12, 31)),
            ("2024-02", day(2024, 2, 1), day(2024, 2, 29)),
            ("2026-12", day(2026, 12, 1), day(2026, 12, 31)),
            ("2026-06-15", day(2026, 6, 15), day(2026, 6, 15)),
            ("2026-01..2026-03", day(2026, 1, 1), day(2026, 3, 31)),
            ("2026-01..", day(2026, 1, 1), None), ("..2026-03", None, day(2026, 3, 31)),
        ]:
            with self.subTest(text=text):
                self.assertEqual(config.when(text), Range(since, until))
        with fake_clock():
            today = datetime.date.fromtimestamp(clock.wall())
            self.assertEqual(config.when("30d").since, today - datetime.timedelta(days=30))
            self.assertEqual(config.when("1y").since, today - datetime.timedelta(days=365))
        for text in ("yesterday", "2026-13", "2026-02-30", "2026-03..2026-01", "30", "d30"):
            with self.subTest(text=text), self.assertRaises(UsageError):
                config.when(text)


class RunTest(CtxCase):
    """A plugin's `[plugins.<code>]` settings reach its run, over what its class says."""

    def configure(self, text):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def test_without_settings_the_plugin_decides(self):
        run = self.open()
        self.assertEqual((run.scheduler.pace["exm"], run.scheduler.limits.limits,
                          run.session.proxy), (CtxExample.pace, dict(CtxExample.limits), None))

    def test_pace_limits_and_proxy_come_from_the_config(self):
        self.configure('[plugins.exm]\npace = 4\nlimits = { "cdn.example.com" = 48 }\n'
                       'proxy = "socks5://127.0.0.1:1080"\n')
        run = self.open()
        self.assertEqual(run.scheduler.pace["exm"], 4.0)
        self.assertEqual(run.scheduler.limits.limits,
                         {**CtxExample.limits, "cdn.example.com": 48})
        # One host's limit above the usual most lets a target keep that many files going.
        self.assertEqual(run.jobs, 48)
        self.assertEqual(run.session.proxy, "socks5://127.0.0.1:1080")
        self.configure("[plugins.exm]\npace = 0\n")
        self.assertEqual(self.open().scheduler.pace["exm"], 0)

    def test_the_proxy_reaches_curl(self):
        self.configure('[plugins.exm]\nproxy = "http://127.0.0.1:8080"\n')
        run = self.open()
        from curl_cffi import CurlOpt

        class Curl:
            def __init__(self):
                self.options = {}

            def setopt(self, option, value):
                self.options[option] = value

            def impersonate(self, target, default_headers):
                return 0

        curl = Curl()
        run.session._session()._setup(curl)
        # Nothing was sent, so there is nothing for the run to close.
        run.session._raw = None
        self.assertEqual(curl.options[CurlOpt.PROXY], "http://127.0.0.1:8080")

    def test_the_browser_says_once_that_it_isnt_proxied(self):
        self.configure('[plugins.exm]\nproxy = "http://127.0.0.1:8080"\n')
        run, ctx = self.ctx()
        ctx._tabs._browser()
        other = type(ctx)(run, Target("exm:example_creator", kind="user"))
        self.addCleanup(other.close)
        other._tabs._browser()
        told = [line for line in self.lines() if "doesn't use the proxy" in line]
        self.assertEqual(len(told), 1)


class TemplatesTest(LibraryCase):
    """`[plugins.<code>] folder` and `filename` replace the plugin's own, checked first."""

    def configure(self, text):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def test_the_configured_templates_name_the_files(self):
        self.configure('[plugins.exs]\nfolder = "kept/{category}"\nfilename = "{post}"\n')
        self.assertEqual(self.fl.get("exs:example_user").wait().saved, 3)
        folder = self.home / "media" / "exs" / "example_user" / "kept"
        self.assertEqual(
            sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*.jpg")),
            ["posts/p1.jpg", "posts/p2.jpg", "reels/r1.jpg"])

    def test_a_template_that_leaves_the_folder_or_names_no_field_is_refused_first(self):
        for text in ('folder = "../out"', 'filename = "{nothing}"'):
            with self.subTest(text):
                self.configure(f"[plugins.exs]\n{text}\n")
                with self.assertRaises(UsageError):
                    self.fl.get("exs:example_user")
        self.assertEqual(self.requests, [])

    def test_a_plugins_own_keys_reach_ctx_settings(self):
        seen = []

        class Tuned(flnd.plugin.Plugin):
            code, name, domains, api = "tun", "Tuned", ("tun.example",), 2

            class Settings(flnd.plugin.Settings):
                """Tuned's settings.

                Attributes:
                    quality: the files' quality.
                """

                quality: str = "best"

            @flnd.plugin.route("tun:{user}", single=True)
            async def user(self, ctx, user):
                seen.append(ctx.settings.quality)
                for post in ():
                    yield post

        engine = Engine(home=self.home, sources={"tun": Tuned})
        engine.download(["tun:example_user"])
        self.configure('[plugins.tun]\nquality = "low"\n')
        engine.download(["tun:example_user"])
        self.assertEqual(seen, ["best", "low"])


class PrecedenceTest(LibraryCase):
    """A value given for one call, then config.toml, then the default."""

    def configure(self, text):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def test_when_from_the_config_bounds_a_download_and_a_given_one_wins(self):
        # Every post of the example source is from 2026-03-01.
        self.configure('[defaults]\nwhen = "2026-04"\n')
        self.assertEqual(self.fl.get("exs:example_user").wait().saved, 0)
        self.assertEqual(self.fl.get("exs:example_user", when="2026-03").wait().saved, 3)
        self.assertEqual(self.fl.get("exs:example_user", when="").wait().saved, 0)


    def test_template_and_check_folder_validation(self):
        bad_templates = (
            "..", ".", "a/../b", "a/./b", "/abs", "a/", "a//b", "\\back", "a\\b",
            "C:drive", "a/C:drive", "a\x00b", "a\nb", "{bad", "bad}", "{}", "{.x}",
            "{nope}", "{target:{nope}}",
        )
        targets = [Target("exm:example_user", kind="user")]
        for value in bad_templates:
            with self.subTest(value=value):
                with self.assertRaises(UsageError):
                    config.template("the call", "folder", value, Example.fields)
                with self.assertRaises(UsageError):
                    config.check_folder(value, PLUGINS, targets)
        for valid in ("example_handle/tagged", "{target}/{date}", "{query}", "{title}"):
            with self.subTest(valid=valid):
                config.check_folder(valid, PLUGINS, targets)

    def test_a_folder_list_is_refused_before_any_request_for_each_wrong_entry(self):
        source = example_source({}, code="exs", fields=("title",))
        targets = [Target("exs:example_user", kind="user")]
        for value, message in (
            ("posts:{target}/a,b", r"folder 'b' isn't <category>:<template>"),
            ("posts:{target}/a,{target}/b", r"folder '\{target\}/b' isn't <category>"),
            ("posts:", r"folder posts: the template is empty"),
            ("", r"folder: the template is empty"),
            ("posts:a,posts:b", r"folder posts: named twice"),
            ("*:a,reels:b,*:c", r"folder \*: named twice"),
            ("nope:a", r"folder nope: no plugin of these targets has this category"),
            ("posts:{nothere}", r"folder\[posts\] is a template of the fields"),
            ("*:{nothere}", r"folder is a template of the fields"),
            ("reels:a,*:{nothere}", r"folder\[\*\] is a template of the fields"),
            ("posts:a,*:../b", r"folder\[\*\] is a template inside the target's folder"),
            ("posts:a,reels:{bad", r"folder\[reels\] is a template whose braces"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(UsageError, message) as found:
                config.check_folder(value, {"exs": source}, targets)
            self.assertTrue(found.exception.hint)
        self.assertEqual(
            config.folders("posts:{target}/feed,reels:{date:%Y,%m},*:{target}/{category}"),
            {"posts": "{target}/feed", "reels": "{date:%Y,%m}", "*": "{target}/{category}"})
        for valid in ("posts:{target}/feed,reels:{title}/r,*:{target}/{category}", "reels:a",
                      "{target}/a,b", "posts:{target}"):
            with self.subTest(valid=valid):
                config.check_folder(valid, {"exs": source}, targets)
        # A list is the same as a mapping, and a comma outside braces can't be in a mapping.
        self.assertEqual(config.folder_spec({"posts": "a", "*": "b/{category}"}),
                         "posts:a,*:b/{category}")
        for mapping in ({}, {"posts": "a,b"}, {"posts": ""}, {"a:b": "c"}):
            with self.subTest(mapping=mapping), self.assertRaises(UsageError):
                config.folder_spec(mapping)

    def test_a_search_needs_a_folder_that_places_every_category(self):
        async def search(self, ctx, words):
            yield Target("exq:example_user")

        source = example_source({}, code="exq", search=flnd.plugin.route("exq?{words}")(search))
        targets = [Target("exq:words", kind="search"), Target("exq:example_user", kind="user")]
        sources = {"exq": source}
        for value, message in (
            (None, r"exq\?words: a search needs --folder$"),
            ("posts:{query}/posts", r"exq\?words: a search needs --folder to place every cat"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(UsageError, message):
                config.check_folder(value, sources, targets)
        for value in ("{query}", "posts:{query}/posts,*:{query}/other"):
            config.check_folder(value, sources, targets)
        config.check_folder(None, sources, targets, stored={"exq:words"})
        config.check_folder(None, sources, targets[1:])


if __name__ == "__main__":
    unittest.main()
