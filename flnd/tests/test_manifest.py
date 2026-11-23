"""The plugin manifest (`src/plugins/_manifest.py`), the lazy registry built on it, and the
built-in plugins together.

The manifest is generated (`make manifest`, tests/plugin_manifest.py); these tests fail when it
no longer matches the plugins, and check that `--help` and target resolution import no more
plugins than they need. Each plugin's own tests are in tests/test_<code>.py.
"""

import ast
import importlib
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

import plugin_manifest

from flnd import plugins
from flnd.plugin import COOKIE_METHODS
from flnd.plugins import _manifest

PLUGINS_DIR = Path(__file__).resolve().parent.parent / "src" / "plugins"
# The SDKs a built-in plugin may import (inside a function), beside flnd.plugin and the
# standard library: Telegram's client and QR codes, and LOIBUS's decryption.
SDKS = ("telethon", "qrcode", "cryptography")


def loaded_after(code: str) -> list[str]:
    """The plugins a fresh interpreter has imported after running `code`."""
    shown = (f"import sys\n{code}\n"
             "print(sorted(name for name in sys.modules if name.startswith('flnd.plugins.')\n"
             "             and name.count('.') == 2\n"
             "             and not name.endswith(('_manifest', '.common'))))")
    result = subprocess.run([sys.executable, "-c", shown], capture_output=True, text=True,
                            check=True)
    return eval(result.stdout.strip().splitlines()[-1])  # a list of names, printed by us


def built_in() -> dict:
    """Each built-in plugin's class by code, in the registry's order, without discovery."""
    classes = [getattr(importlib.import_module(module), name) for module, name in plugins.BUILT_IN]
    return {cls.code: cls for cls in classes}


class ManifestTest(unittest.TestCase):
    def test_the_manifest_matches_the_plugins(self):
        text = plugin_manifest.MANIFEST.read_text(encoding="utf-8")
        self.assertEqual(text, plugin_manifest.render(),
                         "src/plugins/_manifest.py is stale: run make manifest")

    def test_the_extension_may_read_every_built_in_domain(self):
        text = plugin_manifest.EXTENSION.read_text(encoding="utf-8")
        self.assertEqual(text, plugin_manifest.extension(),
                         "src/extension/manifest.json is stale: run make manifest")

    def test_the_registry_lists_the_built_in_plugins_in_order(self):
        codes = ["tik", "fbk", "ins", "thr", "twt", "loi", "tlg", "mht", "web"]
        self.assertEqual(list(built_in()), codes)
        self.assertEqual(list(_manifest.PLUGINS), codes)
        self.assertEqual(list(plugins.PLUGINS)[:len(codes)], codes)

    def test_a_plugin_is_imported_when_it_is_looked_up(self):
        self.assertEqual(loaded_after("from flnd.plugins import PLUGINS\nlist(PLUGINS)"), [])
        self.assertEqual(loaded_after("from flnd.plugins import PLUGINS\nPLUGINS['ins']"),
                         ["flnd.plugins.ins"])

    def test_resolving_a_built_in_target_imports_no_plugin(self):
        code = ("import flnd\n"
                "with flnd.open('example-home') as fl:\n"
                "    fl._resolve(['ins:example_user', 'https://www.tiktok.com/@example_user'])")
        self.assertEqual(loaded_after(code), [])

    def test_a_registry_can_be_patched_like_a_dict(self):
        with unittest.mock.patch.dict(plugins.PLUGINS, {"exm": object}):
            self.assertIn("exm", plugins.PLUGINS)
            self.assertEqual(list(plugins.PLUGINS)[-1], "exm")
        self.assertNotIn("exm", plugins.PLUGINS)
        self.assertEqual(len(plugins.PLUGINS), len(_manifest.PLUGINS))


class BuiltInTest(unittest.TestCase):
    def test_each_describes_itself_and_no_domain_belongs_to_two(self):
        owners = {}
        for code, plugin in built_in().items():
            with self.subTest(plugin=code):
                self.assertEqual((plugin.api, bool(plugin.name), bool(plugin.forms)),
                                 (2, True, True))
                # `web` reads any link, so it has no domains of its own.
                self.assertEqual(bool(plugin.domains), code != "web")
                for domain in plugin.domains:
                    self.assertNotIn(domain, owners, f"{domain} is {owners.get(domain)}'s too")
                    owners[domain] = code

    def test_declarations(self):
        rows = {  # code: (login cookies, pace, stealth, own User-Agent, host limits)
            "tik": (("sessionid", "sid_tt"), 2.0, True, False, {}),
            "fbk": (("c_user", "xs"), 2.0, True, False, {}),
            "ins": (("sessionid",), 2.0, True, False, {}),
            "thr": (("sessionid",), 2.0, True, False, {}),
            "twt": (("auth_token",), 2.0, True, False,
                    {"pbs.twimg.com": 4, "video.twimg.com": 4}),
            "loi": (("access-token",), 0.5, True, False, {}),
            "tlg": ((), 0.5, True, False, {"t.me": 3}),
            "mht": ((), 0.5, True, False, {}),
            "web": ((), 0.5, True, False, {}),
        }
        logins = {"tlg": ("browser", "qr", "phone"), "mht": None, "web": None}
        for code, plugin in built_in().items():
            with self.subTest(plugin=code):
                self.assertEqual((tuple(getattr(plugin.login, "names", ())), plugin.pace,
                                  plugin.stealth,
                                  bool(plugin.user_agent), dict(plugin.limits)), rows[code])
                methods = plugin.login.methods if plugin.login is not None else None
                self.assertEqual(methods, logins.get(code, COOKIE_METHODS))

    def test_categories_and_single_post_kinds(self):
        # Without --only a target gets every category; a single post's route is its kind.
        rows = {
            "tik": ("videos stories playlists reposts liked favorites profile", "post short"),
            "fbk": ("posts photos tagged albums reels videos stories profile",
                    "video watch share photo post"),
            "ins": ("posts reels tagged reposts stories highlights profile", "post"),
            "thr": ("posts replies reposts profile", "post"),
            "twt": ("posts media highlights reposts replies profile", "post"),
            "loi": ("archive photo", "post"),
            "tlg": ("photo video file", ""),
            "mht": ("chapters cover", "chapter"),
            "web": ("", "page"),
        }
        for code, plugin in built_in().items():
            with self.subTest(plugin=code):
                single = tuple(route.kind for route in plugin.routes if route.single)
                categories, kinds = rows[code]
                self.assertEqual((plugin.categories, single),
                                 (tuple(categories.split()), tuple(kinds.split())))

    def test_a_plugin_imports_only_flnd_plugin_the_standard_library_and_its_sdk(self):
        # tests/test_layers.py checks the import time; this checks every import, anywhere.
        for path in sorted(PLUGINS_DIR.glob("[!_]*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    with self.subTest(plugin=path.stem, imports=name):
                        top = name.split(".")[0]
                        self.assertTrue(name == "flnd.plugin" or top in SDKS
                                        or top in sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
