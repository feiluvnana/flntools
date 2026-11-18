"""The chain's own steps (`flnd._steps`): hashing, dedupe, the sidecar, and `Saved` at the end.

Every run fakes the network (`flnd.kit.net.reply` routes, from `test_engine`) and uses synthetic
photos.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
from unittest import mock

from test_engine import CDN, EngineCase, make_source, noise

JPEG = noise("JPEG")

from flnd._events import Saved
from flnd.engine import Engine
from flnd.kit import net as _net
from flnd.source import File, Post, Source, target


def by_target(site, context):
    """Each target lists one file named after itself (`a.jpg` for `aaa:a`)."""
    name = f"{site.target.value}.jpg"
    return [File(f"{CDN}/{name}", name=name)]


class ChainCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.body = JPEG
        # Every target's file has the same bytes.
        for name in ("a", "b", "c"):
            self.routes[f"{CDN}/{name}.jpg"] = _net.reply(200, content=self.body)
        self.source = make_source("aaa", files=by_target)

    def engine(self, source=None) -> Engine:
        source = source or self.source
        return Engine(home=self.home, sources={source.code: source}, emit=self.events.append)

    def config(self, text: str) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def path(self, name: str, suffix: str = ".jpg") -> Path:
        return self.target_dir("aaa", name) / f"{name}{suffix}"

    def rows(self) -> dict:
        from flnd import _home

        with _home.open(self.home) as db:
            return {row.key: row for row in db.on_disk()}

    def saved(self) -> list[Saved]:
        return [event for event in self.events if isinstance(event, Saved)]


class HashTest(ChainCase):
    def test_a_download_records_the_sha256_of_its_bytes(self):
        self.engine().download(["aaa:a"], categories="all")
        row = self.rows()["aaa:a.jpg"]
        self.assertEqual(row.hash, hashlib.sha256(self.body).digest())


class DedupeTest(ChainCase):
    def test_the_same_bytes_under_another_target_become_a_hard_link(self):
        engine = self.engine()
        engine.download(["aaa:a"], categories="all")
        engine.download(["aaa:b"], categories="all")
        a, b = self.path("a").stat(), self.path("b").stat()
        self.assertTrue(os.path.samestat(a, b))
        self.assertEqual(a.st_nlink, 2)
        rows = self.rows()
        self.assertEqual(rows["aaa:a.jpg"].hash, rows["aaa:b.jpg"].hash)
        self.assertEqual(rows["aaa:b.jpg"].mtime, b.st_mtime_ns)
        self.assertIn("1 file already saved elsewhere, linked", self.dest.getvalue())

    def test_dedupe_off_keeps_both_copies(self):
        self.config('[defaults]\ndedupe = "off"\n')
        engine = self.engine()
        engine.download(["aaa:a"], categories="all")
        engine.download(["aaa:b"], categories="all")
        self.assertFalse(os.path.samestat(self.path("a").stat(), self.path("b").stat()))
        self.assertEqual(self.path("b").read_bytes(), self.body)

    def test_a_file_system_without_hard_links_keeps_both_with_one_warning(self):
        engine = self.engine()
        engine.download(["aaa:a"], categories="all")
        refused = OSError(errno.EXDEV, "Cross-device link")
        with mock.patch("os.link", side_effect=refused):
            engine.download(["aaa:b", "aaa:c"], categories="all")
        for name in ("b", "c"):
            self.assertEqual(self.path(name).read_bytes(), self.body)
            self.assertEqual(self.path(name).stat().st_nlink, 1)
        self.assertEqual(self.dest.getvalue().count("can't hard-link"), 1)

    def test_a_changed_holder_is_not_linked_to(self):
        engine = self.engine()
        engine.download(["aaa:a"], categories="all")
        self.path("a").write_bytes(b"edited by hand")
        engine.download(["aaa:b"], categories="all")
        self.assertEqual(self.path("b").read_bytes(), self.body)

class LinkRowTest(ChainCase):
    """The ledger's link across category folders: a row of its own, and never left dangling."""

    def tabs(self):
        clip = Post("V1", [File(f"{CDN}/a.jpg", name="V1.jpg")])

        class Tabs(Source):
            code, name, domains, key = "lnk", "Links", ("example.com",), "{post}_{num}"
            api = 1
            filename = "{date}_{post}{index}"

            @target("lnk:{user}")
            def user(self, site, user):
                return user

            @user.category
            def videos(self, site, user):
                yield clip

            @user.category
            def reels(self, site, user):
                yield clip

        return Tabs

class SidecarTest(ChainCase):
    def source_with_fields(self):
        class Captioned(Source):
            code, name, domains, key = "cap", "Captions", ("example.com",), "{post}_{num}"
            api = 1
            filename = "{date}_{post}{index}"

            @target("cap:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("P1", [File(f"{CDN}/a.jpg?sig=secret", name="P1.jpg")],
                           date="2026-01-02", caption="a day out", author="example_user",
                           tags=["beach"], url="https://example.com/p/P1")

        return Captioned

    def test_sidecars_are_off_by_default(self):
        self.engine(self.source_with_fields()).download(["cap:example_user"], categories="all")
        folder = self.target_dir("cap", "example_user")
        self.assertEqual([path.name for path in folder.rglob("*.json")], [])

    def test_a_sidecar_holds_the_post_beside_its_file(self):
        self.config("[defaults]\nsidecars = true\n")
        self.engine(self.source_with_fields()).download(["cap:example_user"], categories="all")
        [side] = list(self.target_dir("cap", "example_user").rglob("*.json"))
        data = json.loads(side.read_text(encoding="utf-8"))
        self.assertEqual(side.name, "2026-01-02_P1.jpg.json")
        self.assertEqual(
            {name: data[name] for name in ("source", "target", "post", "date", "caption",
                                           "author", "tags", "url", "file")},
            {"source": "cap", "target": "cap:example_user", "post": "P1", "date": "2026-01-02",
             "caption": "a day out", "author": "example_user", "tags": ["beach"],
             "url": "https://example.com/p/P1", "file": f"{CDN}/a.jpg"})
        self.assertEqual(self.rows()["cap:P1_0"].sidecar, str(side))

    def test_a_broken_config_is_a_usage_error(self):
        from flnd.errors import UsageError

        for text in ("[defaults]\nsidecars = 1\n", '[defaults]\ndedupe = "skip"\n', "[defaults"):
            with self.subTest(text=text):
                self.config(text)
                with self.assertRaises(UsageError):
                    self.engine().download(["aaa:a"], categories="all")


class SavedTest(ChainCase):
    def test_a_file_with_no_slow_step_is_saved_at_once(self):
        self.engine().download(["aaa:a"], categories="all")
        [event] = self.saved()
        self.assertEqual((event.path, event.size), (self.path("a"), len(self.body)))

    def test_a_file_dropped_by_ctrl_c_before_its_steps_is_still_saved(self):
        from flnd import _steps

        stop = mock.Mock(soon=mock.Mock(is_set=lambda: True), now=mock.Mock())
        events = []
        chain = _steps.Chain(None, stop, emit=events.append)
        event = Saved(target=mock.Mock(), category="", post="1", path=Path("a.jpg"), date=None,
                      size=3)
        chain.landed(_steps.Landed("aaa:a.jpg", Path("a.jpg"), event=event))
        chain.close()
        self.assertEqual(events, [event])


if __name__ == "__main__":
    import unittest

    unittest.main()
