"""The chain's own steps (`flnd.engine.steps`): the links between category folders, and
`Saved` at the end.

Every run fakes the network (`flnkit.net.reply` routes, from `test_engine`) and uses synthetic
photos.
"""

from __future__ import annotations

from pathlib import Path

from fakes import CDN, Engine, EngineCase, make_plugin, noise

JPEG = noise("JPEG")

from flnd.engine import store as _store
from flnd.engine.events import Saved
from flnd.plugin import File, Plugin, Post, route
from flnkit import net as _net


def by_target(ctx, context):
    """Each target lists one file named after itself (`a.jpg` for `aaa:a`)."""
    name = f"{ctx.target.value}.jpg"
    return [File(f"{CDN}/{name}", name=name)]


class ChainCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.body = JPEG
        # Every target's file has the same bytes.
        for name in ("a", "b", "c"):
            self.routes[f"{CDN}/{name}.jpg"] = _net.reply(200, content=self.body)
        self.source = make_plugin("aaa", files=by_target)

    def engine(self, source=None) -> Engine:
        source = source or self.source
        return Engine(home=self.home, sources={source.code: source}, emit=self.events.append)

    def config(self, text: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def path(self, name: str, suffix: str = ".jpg") -> Path:
        return self.target_dir("aaa", name) / f"{name}{suffix}"

    def rows(self) -> dict:

        with _store.open(self.home) as db:
            return {row.key: row for row in db.on_disk()}

    def saved(self) -> list[Saved]:
        return [event for event in self.events if isinstance(event, Saved)]


class LinkRowTest(ChainCase):
    """The ledger's link across category folders: a row of its own, and never left dangling."""

    def tabs(self):
        clip = Post("V1", [File(f"{CDN}/a.jpg", name="V1.jpg")])

        class Tabs(Plugin):
            code, name, domains, key = "lnk", "Links", ("example.com",), "{post}_{num}"
            api = 2
            filename = "{date}_{post}{index}"

            @route("lnk:{user}", newest_first=False)
            async def user(self, ctx, user):
                return user

            @user.category
            async def videos(self, ctx, user):
                yield clip

            @user.category
            async def reels(self, ctx, user):
                yield clip

        return Tabs

class ConfigTest(ChainCase):
    def source_with_fields(self):
        class Captioned(Plugin):
            code, name, domains, key = "cap", "Captions", ("example.com",), "{post}_{num}"
            api = 2
            filename = "{date}_{post}{index}"

            @route("cap:{user}", newest_first=False)
            async def user(self, ctx, user):
                return user

            @user.category
            async def posts(self, ctx, user):
                yield Post("P1", [File(f"{CDN}/a.jpg?sig=secret")],
                           date="2026-01-02", caption="a day out", author="example_user",
                           tags=["beach"], url="https://example.com/p/P1")

        return Captioned

    def test_no_file_gets_a_json_sidecar(self):
        self.engine(self.source_with_fields()).download(["cap:example_user|all"])
        folder = self.target_dir("cap", "example_user")
        self.assertEqual([path.name for path in folder.rglob("*.json")], [])

    def test_a_broken_config_is_a_usage_error(self):
        from flnd.errors import UsageError

        for text in ("[defaults]\nwhen = 1\n", '[defaults]\ntimeout = "soon"\n', "[defaults"):
            with self.subTest(text=text):
                self.config(text)
                with self.assertRaises(UsageError):
                    self.engine().download(["aaa:a|all"])


class SavedTest(ChainCase):
    def test_a_file_with_no_slow_step_is_saved_at_once(self):
        self.engine().download(["aaa:a|all"])
        [event] = self.saved()
        self.assertEqual((event.path, event.size), (self.path("a"), len(self.body)))


if __name__ == "__main__":
    import unittest

    unittest.main()
