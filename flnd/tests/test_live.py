"""Offline unit tests for `tests/live.py` (`make live`): fake plugin, temp config, dry runs."""

from __future__ import annotations

import datetime
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import no_profiles  # noqa: F401 - keeps tests away from real profiles and ~/.config/flnd

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import live  # noqa: E402

from flnd.plugin import File, Plugin, Post, route  # noqa: E402


class _DemoPlugin(Plugin):
    """Fake multi-route, multi-category plugin for testing `tests/live.py` offline."""

    code = "dmv"
    name = "Demo"
    api = 2
    fields = ("part",)

    @route("dmv:{user}", folder="{category}/{part}")
    async def user(self, ctx: Any, user: str):
        return user

    @user.category(own=True)
    async def posts(self, ctx: Any, user: str):
        yield Post("p1", [File("https://example.com/1.jpg")],
                   date=datetime.date(2026, 2, 3), part="feed")

    @user.category(own=True)
    async def reels(self, ctx: Any, user: str):
        yield Post("r1", [File("https://example.com/2.mp4")],
                   date=datetime.date(2026, 2, 4), part="")

    @user.category(own=True, folder="profile", key="{path}")
    async def profile(self, ctx: Any, user: str):
        yield Post(user, [File("https://example.com/avatar.jpg", name="avatar.jpg")])

    @route("dmv:post/{id}", single=True)
    async def post(self, ctx: Any, id: str):
        yield Post(id, [File("https://example.com/p.jpg")],
                   date=datetime.date(2026, 2, 5))


class _BrokenDatePlugin(Plugin):
    """Fake plugin yielding a post whose date fails to parse."""

    code = "bdt"
    name = "BadDate"
    api = 2

    @route("bdt:{user}")
    async def user(self, ctx: Any, user: str):
        yield Post("p1", [File("https://example.com/1.jpg")], date="not-a-date")


class _MissingDatePlugin(Plugin):
    """Fake plugin yielding a post with `date=None` on a dated template."""

    code = "mdt"
    name = "MissingDate"
    api = 2

    @route("mdt:{user}")
    async def user(self, ctx: Any, user: str):
        yield Post("p1", [File("https://example.com/1.jpg")], date=None)


class _EmptyCategoryPlugin(Plugin):
    """Fake plugin whose category yields no posts."""

    code = "emp"
    name = "Empty"
    api = 2

    @route("emp:{user}")
    async def user(self, ctx: Any, user: str):
        return user

    @user.category(own=True)
    async def posts(self, ctx: Any, user: str):
        if False:
            yield


class _EmptyFieldPlugin(Plugin):
    """Fake plugin whose template field (`author`) is left empty."""

    code = "efl"
    name = "EmptyField"
    api = 2
    folder = "{author}/{post}"
    fields = ("author",)

    @route("efl:{user}")
    async def user(self, ctx: Any, user: str):
        yield Post("p1", [File("https://example.com/1.jpg")],
                   date=datetime.date(2026, 2, 3), author="")


class LiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.scratch / "home"

    def test_passing_routes_and_categories_download_nothing(self) -> None:
        config = {
            "home": str(self.home),
            "limit": 3,
            "dmv": {
                "user": "dmv:example_user",
                "post": "dmv:post/9001",
            },
        }
        results, notes = live.run_checks(config, plugins=[_DemoPlugin])
        self.assertEqual(notes, [])
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result.ok for result in results), results)
        self.assertEqual(
            [(r.route, r.category, r.planned) for r in results],
            [("user", "posts", 1), ("user", "reels", 1), ("user", "profile", 1), ("post", "", 1)],
        )
        # Dry run writes no downloaded media files.
        self.assertFalse((self.home / "media").exists())

    def test_empty_category_unparsed_date_missing_date_and_empty_field_fail(self) -> None:
        cases: list[tuple[type[Plugin], dict[str, Any], str]] = [
            (_EmptyCategoryPlugin, {"emp": {"user": "emp:example_user"}}, "no files found"),
            (_BrokenDatePlugin, {"bdt": {"user": "bdt:example_user"}}, "unparsed date"),
            (_MissingDatePlugin, {"mdt": {"user": "mdt:example_user"}}, "missing date"),
            (_EmptyFieldPlugin, {"efl": {"user": "efl:example_user"}}, "empty template field"),
        ]
        for plugin, cfg, expected in cases:
            with self.subTest(plugin=plugin.code):
                results, _ = live.run_checks(
                    {"home": str(self.home), **cfg}, plugins=[plugin],
                )
                self.assertEqual(len(results), 1)
                self.assertFalse(results[0].ok)
                self.assertIn(expected, "; ".join(results[0].errors))

    def test_undated_option_allows_missing_date(self) -> None:
        results, _ = live.run_checks(
            {"home": str(self.home), "mdt": {"user": "mdt:example_user", "undated": True}},
            plugins=[_MissingDatePlugin],
        )
        self.assertTrue(results[0].ok, results)

    def test_missing_config_and_unknown_plugin_exit_cleanly(self) -> None:
        missing = self.scratch / "missing.toml"
        err = io.StringIO()
        with redirect_stderr(err):
            code = live.main(["--config", str(missing), "ins"])
        self.assertEqual(code, 2)
        self.assertIn("not found", err.getvalue())

        cfg_path = self.scratch / "live.toml"
        cfg_path.write_text('[zzz]\nuser = "zzz:example_user"\n', encoding="utf-8")
        err = io.StringIO()
        with redirect_stderr(err):
            code = live.main(["--config", str(cfg_path), "zzz"])
        self.assertEqual(code, 2)
        self.assertIn("unknown plugin", err.getvalue())

    def test_skipped_route_prints_note(self) -> None:
        results, notes = live.run_checks(
            {"home": str(self.home), "dmv": {"post": "dmv:post/42"}},
            plugins=[_DemoPlugin],
        )
        self.assertEqual(notes, ["dmv.user: skipped (not in live.toml)"])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)

    def test_cli_prints_pass_and_fail_summary(self) -> None:
        cfg_path = self.scratch / "live.toml"
        cfg_path.write_text('[ins]\nnope = "ins:example_user"\n', encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = live.main(["--config", str(cfg_path), "ins"])
        self.assertEqual(code, 2)
        self.assertIn("unknown route", err.getvalue())

        cfg_path.write_text(
            f'home = "{self.home}"\n[dmv]\npost = "dmv:post/7"\n', encoding="utf-8",
        )
        out = io.StringIO()
        with mock.patch.object(
            live, "run_checks",
            return_value=(
                [live.CheckResult("dmv", "post", "", "dmv:post/7", 1, ())],
                ["dmv.user: skipped (not in live.toml)"],
            ),
        ), redirect_stdout(out):
            code = live.main(["--config", str(cfg_path), "dmv"])
        self.assertEqual(code, 0)
        self.assertIn("SKIP  dmv.user: skipped (not in live.toml)", out.getvalue())
        self.assertIn("PASS  dmv.post: 1 planned", out.getvalue())

        out = io.StringIO()
        with mock.patch.object(
            live, "run_checks",
            return_value=(
                [live.CheckResult("dmv", "user", "posts", "dmv:example_user", 0, ("no files",))],
                [],
            ),
        ), redirect_stdout(out):
            code = live.main(["--config", str(cfg_path), "dmv"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL  dmv.user[posts]: no files", out.getvalue())


if __name__ == "__main__":
    unittest.main()
