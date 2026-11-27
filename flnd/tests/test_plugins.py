"""The built-in plugins: their table, their loader and their declarations.

`flnd.plugins.TABLE` is written by hand, so these tests check it against each class, and check
that `--help` and target resolution import no more plugin modules than they need. Each plugin's
own tests are in tests/test_<code>.py.
"""

import ast
import importlib
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

from flnd.engine import plugins
from flnd.engine.plugins import PLUGINS
from flnd.plugin import COOKIE_METHODS, RESERVED, TEMPLATE_FIELDS, Plugin
from flnd.plugins import TABLE
from flnkit import text

PLUGINS_DIR = Path(__file__).resolve().parent.parent / "src" / "plugins"
# The SDKs a built-in plugin may import (inside a function), beside flnd.plugin and the
# standard library: LOIBUS's decryption.
SDKS = ("cryptography",)
CODES = ["tik", "fbk", "ins", "thr", "twt", "loi", "mht", "smp", "pxv", "web"]


def loaded_after(code: str) -> list[str]:
    """The plugin modules a fresh interpreter has imported after running `code`."""
    shown = (f"import sys\n{code}\n"
             "print(sorted(name for name in sys.modules if name.startswith('flnd.plugins.')\n"
             "             and name.count('.') == 2))")
    result = subprocess.run([sys.executable, "-c", shown], capture_output=True, text=True,
                            check=True)
    return eval(result.stdout.strip().splitlines()[-1])  # a list of names, printed by us


def built_in() -> dict[str, type[Plugin]]:
    """Each built-in plugin's class by code, in the table's order."""
    return {entry.code: getattr(importlib.import_module(entry.module), entry.cls)
            for entry in TABLE}


class TableTest(unittest.TestCase):
    def test_the_table_lists_the_built_ins_in_order_with_web_last(self):
        self.assertEqual([entry.code for entry in TABLE], CODES)
        self.assertEqual(list(PLUGINS)[:len(CODES)], CODES)

    def test_each_entry_matches_its_class(self):
        for entry in TABLE:
            with self.subTest(plugin=entry.code):
                cls = built_in()[entry.code]
                self.assertEqual((cls.code, cls.__module__, cls.__name__, cls.domains),
                                 (entry.code, entry.module, entry.cls, entry.domains))

    def test_no_domain_belongs_to_two_plugins(self):
        owners: dict[str, str] = {}
        for entry in TABLE:
            for domain in entry.domains:
                self.assertNotIn(domain, owners, f"{domain} is {owners.get(domain)}'s too")
                owners[domain] = entry.code


class LoaderTest(unittest.TestCase):
    def test_a_plugin_is_imported_when_it_is_looked_up(self):
        base = "from flnd.engine.plugins import PLUGINS\n"
        self.assertEqual(loaded_after(base + "list(PLUGINS); PLUGINS.domains('ins')"), [])
        self.assertEqual(loaded_after(base + "PLUGINS['ins']"), ["flnd.plugins.ins"])

    def test_resolving_a_code_imports_that_plugin_alone(self):
        code = ("from flnd import _library\n"
                "with _library.open('example-home') as fl:\n"
                "    fl._resolve(['ins:example_user'])")
        self.assertEqual(loaded_after(code), ["flnd.plugins.ins"])

    def test_resolving_a_link_imports_the_plugin_of_its_domain_alone(self):
        code = ("from flnd import _library\n"
                "with _library.open('example-home') as fl:\n"
                "    fl._resolve(['https://www.tiktok.com/@example_user'])")
        self.assertEqual(loaded_after(code), ["flnd.plugins.tik"])

    def test_a_link_no_plugin_owns_imports_web_alone(self):
        code = ("from flnd import _library\n"
                "with _library.open('example-home') as fl:\n"
                "    fl._resolve(['https://example.com/page'])")
        self.assertEqual(loaded_after(code), ["flnd.plugins.web"])

    def test_help_imports_no_plugin(self):
        code = "from flnd import cli\ncli.parser().parse_args(['sync'])"
        self.assertEqual(loaded_after(code), [])

    def test_the_forms_of_get_help_import_every_plugin(self):
        code = "from flnd import cli\ncli._forms()"
        self.assertEqual(loaded_after(code), sorted(entry.module for entry in TABLE))

    def test_a_registry_can_be_patched_like_a_dict(self):
        with unittest.mock.patch.dict(PLUGINS, {"exm": object}):
            self.assertIn("exm", PLUGINS)
            self.assertEqual(list(PLUGINS)[-1], "exm")
        self.assertNotIn("exm", PLUGINS)
        self.assertEqual(len(PLUGINS), len(TABLE))

    def test_domains_come_from_the_table_until_a_class_is_set(self):
        self.assertEqual(plugins.domains(PLUGINS, "ins"), ("instagram.com",))
        self.assertEqual(plugins.domains(PLUGINS, "none"), ())


class BuiltInTest(unittest.TestCase):
    def test_each_describes_itself(self):
        for code, plugin in built_in().items():
            with self.subTest(plugin=code):
                self.assertTrue(plugin.name.strip() and plugin.forms)
                # `web` reads any link, so it has no domains of its own.
                self.assertEqual(bool(plugin.domains), code != "web")

    def test_routes_have_unique_names_and_the_templates_name_known_fields(self):
        for code, plugin in built_in().items():
            with self.subTest(plugin=code):
                kinds = [route.kind for route in plugin.routes]
                self.assertTrue(kinds)
                self.assertEqual(len(kinds), len(set(kinds)))
                known = {*TEMPLATE_FIELDS, *plugin.fields}
                self.assertFalse(set(plugin.fields) & RESERVED)
                templates = [plugin.folder, plugin.filename, plugin.key]
                for route in plugin.routes:
                    templates.append(route.folder)
                    for category in route.categories:
                        templates += [category.folder, category.key]
                for template in templates:
                    if template:
                        self.assertLessEqual(text.fields(template), known, template)

    def test_declarations(self):
        rows = {  # code: (login cookies, pace, stealth, own User-Agent, host limits)
            "tik": (("sessionid", "sid_tt"), 2.0, True, False, {}),
            "fbk": (("c_user", "xs"), 2.0, True, False, {}),
            "ins": (("sessionid",), 2.0, True, False, {}),
            "thr": (("sessionid",), 2.0, True, False, {}),
            "twt": (("auth_token",), 2.0, True, False,
                    {"pbs.twimg.com": 4, "video.twimg.com": 4}),
            "loi": (("access-token",), 1.5, True, False, {"api.moegoat.com": 2}),
            "mht": ((), 0.5, True, False, {}),
            "smp": ((), 5.0, True, False, {}),
            "pxv": (("PHPSESSID",), 2.0, True, False, {"i.pximg.net": 4}),
            "web": ((), 0.5, True, False, {}),
        }
        logins = {"mht": None, "web": None}
        for code, plugin in built_in().items():
            with self.subTest(plugin=code):
                self.assertEqual((tuple(getattr(plugin.login, "names", ())), plugin.pace,
                                  plugin.stealth,
                                  bool(plugin.user_agent), dict(plugin.limits)), rows[code])
                methods = plugin.login.methods if plugin.login is not None else None
                self.assertEqual(methods, logins.get(code, COOKIE_METHODS))

    def test_categories_and_single_post_kinds(self):
        # Without a category named a target gets its own; a single post's route is its kind.
        rows = {
            "tik": ("videos stories playlists reposts liked favorites profile", "post short"),
            "fbk": ("posts highlights photos tagged albums reels videos stories profile",
                    "video watch share photo post"),
            "ins": ("posts reels tagged reposts stories highlights profile", "post"),
            "thr": ("posts replies reposts profile", "post"),
            "twt": ("posts media highlights reposts replies profile", "post"),
            "loi": ("archive photo", "post"),
            "mht": ("chapters cover", "chapter"),
            "smp": ("images videos", ""),
            "pxv": ("illusts manga ugoira", "artwork"),
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
