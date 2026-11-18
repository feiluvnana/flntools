"""The sources together: the registry, the guard on what they import, and Source's defaults.

Each source's own tests are in tests/test_<code>.py.
"""

import ast
import unittest
from pathlib import Path

from fakes import FakeSite

from flnd.engine.options import Options, selected
from flnd.source import Source, Target, target
from flnd.sources import SOURCES
from flnd.sources.fbk import Facebook
from flnd.sources.ins import CHROME as INS_CHROME
from flnd.sources.ins import Instagram
from flnd.sources.loi import Loibus
from flnd.sources.mht import Mimi
from flnd.sources.tik import TIKTOK_UA, TikTok
from flnd.sources.tlg import Telegram
from flnd.sources.twt import CHROME, Twitter

SOURCES_DIR = Path(__file__).resolve().parent.parent / "flnd" / "sources"
# A source may not reach the loop (threads) or ask the user (prompts) itself: the engine does that.
FORBIDDEN = ("httpx", "curl_cffi", "playwright", "flnd.kit.loop", "flnd.ui.forms")
FORBIDDEN_NAMES = {"_loop", "_prompt"}


class RegistryTest(unittest.TestCase):
    def test_every_source_describes_itself(self):
        for code, source in SOURCES.items():
            self.assertEqual(source.code, code)
            self.assertTrue(isinstance(source.name, str) and source.name)
            self.assertTrue(isinstance(source.domains, tuple) and source.domains, code)
            self.assertTrue(source.forms, code)

    def test_codes_and_order(self):
        self.assertEqual(list(SOURCES), ["tik", "fbk", "ins", "twt", "loi", "tlg", "mht"])

    def test_no_domain_belongs_to_two_sources(self):
        owners = {}
        for code, source in SOURCES.items():
            for domain in source.domains:
                self.assertNotIn(domain, owners, f"{domain} belongs to {owners.get(domain)} and {code}")
                owners[domain] = code

    def test_declarations(self):
        self.assertEqual(Instagram.login.names, ("sessionid",))
        self.assertEqual(Twitter.login.names, ("auth_token",))
        self.assertEqual(Loibus.login.names, ("access-token",))
        self.assertEqual(TikTok.login.names, ("sessionid", "sid_tt"))
        self.assertIsNone(Mimi.login)
        self.assertEqual((Twitter.user_agent, Twitter.stealth), (CHROME, False))
        self.assertEqual(TikTok.user_agent, TIKTOK_UA)
        self.assertEqual(Instagram.user_agent, INS_CHROME)
        for source in SOURCES.values():
            if source.login is None:
                continue
            telegram = source is Telegram
            methods = ("browser", "qr", "phone") if telegram else ("browser", "file")
            self.assertEqual((source.api, source.limits, source.login.methods),
                             (1, {"t.me": 3} if telegram else {}, methods))

    def test_listing_floors(self):
        floors = {"ins": 0.2, "twt": 0.2, "fbk": 0.2, "tik": 0.2, "loi": 0.5, "mht": 0.5}
        for code, floor in floors.items():
            with self.subTest(source=code):
                self.assertEqual(SOURCES[code].pace, floor)

    def test_categories_and_single_post_kinds(self):
        # Without --from a target gets every category; a single post's kind has none.
        table = {
            Instagram: ("posts reels tagged reposts stories highlights profile", "post"),
            Facebook: ("posts photos tagged albums reels videos stories profile", "video watch photo post"),
            TikTok: ("videos stories playlists reposts liked favorites profile", "video"),
            Twitter: ("posts media highlights reposts replies profile", "post"),
            Loibus: ("photo archive", ""),
            Telegram: ("photo video file", ""),
            Mimi: ("chapters cover", "chapter"),
        }
        for source, (categories, single) in table.items():
            with self.subTest(source=source.code):
                self.assertEqual((source.categories, source.single_kinds),
                                 (tuple(categories.split()), tuple(single.split())))

    def test_without_from_every_category_is_taken(self):
        for source in SOURCES.values():
            with self.subTest(source=source.code):
                self.assertEqual(selected(Options(), source), source.categories)
                self.assertEqual(selected(Options(categories=("all",)), source), source.categories)
        self.assertEqual(selected(Options(categories=("reels", "media")), Instagram), ("reels",))
        # None of the names is this source's: nothing is selected.
        self.assertEqual(selected(Options(categories=("media",)), Instagram), ())


class GuardTest(unittest.TestCase):
    """A source only talks to the session: it never imports the libraries the engine wraps."""

    def test_sources_import_nothing_the_engine_owns(self):
        for path in sorted(SOURCES_DIR.rglob("*.py")):
            for imported in runtime_imports(ast.parse(path.read_text(encoding="utf-8"))):
                with self.subTest(module=path.name, imports=imported):
                    self.assertFalse(any(imported == name or imported.startswith(name + ".") for name in FORBIDDEN))


def runtime_imports(tree):
    """Every module a file imports when it runs; `if TYPE_CHECKING:` blocks are only read by checkers."""
    checking_only = {id(node) for block in ast.walk(tree) if isinstance(block, ast.If)
                     and ast.unparse(block.test) == "TYPE_CHECKING" for node in ast.walk(block)}
    for node in ast.walk(tree):
        if id(node) in checking_only:
            continue
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "flnd":
                yield from (f"flnd.{alias.name}" for alias in node.names if alias.name in FORBIDDEN_NAMES)
            yield node.module


class SourceDefaultsTest(unittest.TestCase):
    def test_logged_in_needs_any_one_cookie(self):
        self.assertFalse(TikTok.login.check(FakeSite(Target("tik:", kind="account"))))
        self.assertTrue(TikTok.login.check(FakeSite(Target("tik:", kind="account"), cookies={"sid_tt": "1"})))

    def test_a_source_without_a_login_has_none_and_connects_to_nothing(self):
        class Open(Source):
            code, name, domains, api = "opn", "Open", ("open.example",), 1

            @target("opn:{user}")
            def user(self, site, user): ...

        account = FakeSite(Target("opn:", kind="account"))
        self.assertIsNone(Open.login)
        with Open().connect(account) as client:
            self.assertIsNone(client)


if __name__ == "__main__":
    unittest.main()
