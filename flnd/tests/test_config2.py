"""config.toml's structs, reading, resolution and editing (`flnd.engine.config`).

Every file is in a temporary folder; the user file is passed in, never the real one.
"""

import os
import pwd
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import msgspec

from flnd.engine import config
from flnd.engine.config import Config, Where
from flnd.errors import UsageError


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
            ("[defaults]\nswitch = true", "defaults.switch", True),
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
            "[defaults]\nheight = -1",
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
                           ("plugins.exm.folder", "/abs")):
            with self.subTest(key), self.assertRaises(UsageError):
                config.write(key, value, home=self.home, user=self.user, plugins=PLUGINS)
        self.assertEqual((self.home / "config.toml").read_text(encoding="utf-8"), before)

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


if __name__ == "__main__":
    unittest.main()
