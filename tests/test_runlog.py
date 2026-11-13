"""The `.flnd/` layout and each run's log (plan/phase-7/layout.md), end to end through `Engine`.

Every store opens under `metadata/` and every file lands under `media/`. A `download` or `sync`
writes `.flnd/log/<date>_<time>_<command>.log` at debug while the screen stays at info, marks its
phases, writes a failure's causes and its exit status, never a URL's query string or a cookie's
value, and deletes the logs older than 30 days. The network is `httpx.MockTransport`, as in
tests/test_engine.py, whose `EngineCase` this reuses.
"""

import os
import time
import unittest

import httpx

from test_engine import CDN, EngineCase, make_source

from flnd import Engine
from flnd.common import files
from flnd.source import File
from shared import log


class RunLogCase(EngineCase):
    def engine(self, *sources):
        return Engine(home=self.home, sources={source.code: source for source in sources})

    def logs(self):
        return sorted((self.home / files.LOGS).glob("*.log"))

    def logged(self):
        """The one run log's lines, without their times."""
        [path] = self.logs()
        return [line[13:] for line in path.read_text(encoding="utf-8").splitlines()]


class LayoutTest(RunLogCase):
    def test_files_land_under_media_and_stores_under_metadata(self):
        source = make_source("aaa", files=[File(f"{CDN}/a.jpg", name="a.jpg")])
        self.engine(source).download(["aaa:example_user"])
        self.assertTrue((self.home / "media" / "aaa" / "example_user" / "a.jpg").is_file())
        self.assertTrue((self.home / "metadata" / "history.db").is_file())
        self.assertTrue((self.home / "metadata" / "targets.db").is_file())
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), ["log", "media", "metadata"])
        self.assertEqual(self.lines()[0], f"• Storage: {(self.home / 'media').resolve()}")

    def test_an_old_layout_is_neither_read_nor_moved(self):
        old = self.home / "downloads" / "aaa" / "example_user"
        old.mkdir(parents=True)
        (old / "a.jpg").write_bytes(b"old")
        source = make_source("aaa", files=[File(f"{CDN}/a.jpg", name="a.jpg")])
        report = self.engine(source).download(["aaa:example_user"])
        self.assertEqual(report.files, 1)
        self.assertEqual((old / "a.jpg").read_bytes(), b"old")


class RunLogTest(RunLogCase):
    def test_a_download_logs_its_phases_at_debug_while_the_screen_shows_info(self):
        source = make_source("aaa", files=[File(f"{CDN}/a.jpg", name="a.jpg")])
        self.engine(source).download(["aaa:example_user"])
        self.engine(source).download(["aaa:example_user"])
        # Two runs in one second: the second has a log of its own, numbered.
        first, second = sorted(self.logs(), key=lambda path: "-2" in path.name)
        self.assertRegex(first.name, r"^\d{4}-\d\d-\d\d_\d{6}_download\.log$")
        if "-2" in second.name:
            self.assertEqual(second.name, first.name.replace(".log", "-2.log"))
        lines = [line[13:] for line in second.read_text(encoding="utf-8").splitlines()]
        self.assertRegex(lines[0], r"^· flnd \S+ download: targets=\['aaa:example_user'\]$")
        self.assertIn("· resolve aaa:example_user: aaa:example_user (user)", lines)
        self.assertIn("· aaa:example_user: list posts", lines)
        self.assertIn("· skip a.jpg: already saved", lines)
        self.assertIn("· aaa:example_user: 0 written, 1 skipped; complete: posts", lines)
        self.assertEqual(lines[-1], "· totals: 0 written, 1 skipped, 0 of 1 failed; exit 0")
        self.assertFalse(any(line.startswith("·") for line in self.lines()))

    def test_a_failed_target_logs_its_causes_and_exit_1(self):
        def broken(*_):
            try:
                raise KeyError("cursor")
            except KeyError as cause:
                raise RuntimeError("feed changed") from cause

        self.engine(make_source("aaa", files=broken)).download(["aaa:example_user"])
        lines = self.logged()
        self.assertIn("· aaa:example_user: RuntimeError: feed changed ← KeyError: 'cursor'", lines)
        self.assertEqual(lines[-1], "· totals: 0 written, 0 skipped, 1 of 1 failed; exit 1")

    def test_a_usage_error_logs_exit_2(self):
        with self.assertRaises(ValueError):
            self.engine(make_source("aaa")).download(["aaa:example_user"], time="not a date")
        self.assertRegex(self.logged()[-1], r"^· exit 2: ")

    def test_a_sync_logs_its_repair(self):
        source = make_source("aaa", files=[File(f"{CDN}/a.jpg", name="a.jpg")])
        self.engine(source).download(["aaa:example_user"])
        for path in self.logs():
            path.unlink()
        self.engine(source).sync()
        self.assertTrue(self.logs()[0].name.endswith("_sync.log"))
        lines = self.logged()
        self.assertIn("· aaa:example_user: repair", lines)
        self.assertEqual(lines[-1], "· totals: 0 written, 1 skipped, 0 of 1 failed; exit 0")

    def test_no_query_string_or_cookie_value_reaches_the_log(self):
        source = make_source("aaa", files=lambda *_: [File(f"{CDN}/a.jpg?sig=signed-token", name="a.jpg")])
        self.log_in("aaa", sessionid="cookie-secret-value")
        self.routes[f"{CDN}/a.jpg"] = httpx.Response(403)
        with log.record(self.home / "extra.log"):
            log.debug("fetch {url}", url=f"{CDN}/a.jpg?sig=signed-token")
        self.engine(source).download(["aaa:example_user"])
        text = "\n".join(self.logged()) + (self.home / "extra.log").read_text(encoding="utf-8")
        self.assertIn(f"{CDN}/a.jpg", text)
        self.assertNotIn("signed-token", text)
        self.assertNotIn("cookie-secret-value", text)

    def test_logs_older_than_30_days_are_deleted_as_a_run_starts(self):
        folder = self.home / files.LOGS
        folder.mkdir(parents=True)
        old, recent = folder / "2026-01-01_000000_download.log", folder / "2026-11-01_000000_sync.log"
        for path in (old, recent):
            path.write_text("x")
        month = time.time() - 31 * 24 * 3600
        os.utime(old, (month, month))
        self.engine(make_source("aaa")).download(["aaa:example_user"])
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(len(self.logs()), 2)

    def test_login_and_logout_write_no_log(self):
        self.log_in("aaa", sessionid="x")
        self.engine(make_source("aaa")).logout(["aaa"])
        self.assertFalse((self.home / files.LOGS).exists())


if __name__ == "__main__":
    unittest.main()
