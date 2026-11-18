"""Tests for `flnd.ui.log`: Rich-backed logging, progress, groups, and scoping."""

import io
import unittest

from rich.console import Console as RichConsole

from flnd.kit import clock as _time
from flnd.ui.log import Group, Row, ui


class UICase(unittest.TestCase):
    """Base test case capturing `ui` output in a `StringIO` buffer with a fake clock."""

    def setUp(self) -> None:
        self.buf = io.StringIO()
        self.fake_clock = _time.Fake(start=100.0)
        self.enterContext(_time.use(self.fake_clock))
        self.enterContext(ui.scope(color="none", dest=self.buf, width=80, level="info"))

    def output(self) -> str:
        """Return captured output."""
        return self.buf.getvalue()


class LoggingTest(UICase):
    """Tests for log levels, formatting, nested specs, and escape sanitization."""

    def test_raw_console(self) -> None:
        self.assertIsInstance(ui.raw, RichConsole)

    def test_levels_filter(self) -> None:
        ui.debug("hidden")
        ui.info("shown")
        ui.ok("good")
        ui.warn("careful")
        ui.error("bad")
        out = self.output()
        self.assertNotIn("hidden", out)
        self.assertIn("shown", out)
        self.assertIn("good", out)
        self.assertIn("careful", out)
        self.assertIn("bad", out)

    def test_quiet_shows_only_error(self) -> None:
        with ui.scope(level="quiet"):
            ui.debug("d")
            ui.info("i")
            ui.ok("o")
            ui.warn("w")
            ui.error("e")
        out = self.output()
        self.assertNotIn("w", out)
        self.assertIn("e", out)

    def test_invalid_level_raises(self) -> None:
        with self.assertRaises(ValueError):
            ui.level = "verbose"  # type: ignore[assignment]

    def test_unused_field_raises_even_when_filtered(self) -> None:
        with ui.scope(level="error"):
            with self.assertRaises(TypeError):
                ui.debug("hello {name}", typo="x")

    def test_nested_format_specs(self) -> None:
        ui.info("{n:{w}d}", n=5, w=4)
        self.assertIn("   5", self.output())

    def test_sanitizes_csi_osc_and_control_codes(self) -> None:
        evil = "\x1b[31mred\x1b[0m\x1b]8;;https://example.com\x07link\x1b]8;;\x1b\\\x07"
        ui.info("got {val}", val=evil)
        out = self.output()
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("redlink", out)

    def test_markup_in_fields_is_literal(self) -> None:
        ui.info("msg {x}", x="[bold red]literal[/]")
        self.assertIn("[bold red]literal[/]", self.output())


class ProgressTest(UICase):
    """Tests for `progress`, `group`, and `Row`."""

    def test_progress_ok_and_error(self) -> None:
        with ui.progress("photos/a.jpg", total=10) as row:
            self.assertIsInstance(row, Row)
            self.assertIsNotNone(row.raw)
            row.advance(4)
            self.assertEqual(row.count, 4)
            row.finish()
        self.assertIn("photos/a.jpg", self.output())

        bad: Row | None = None
        with self.assertRaises(RuntimeError):
            with ui.progress("photos/b.jpg", total=10) as bad:
                bad.fail()
                raise RuntimeError("boom")
        assert bad is not None
        self.assertIn("photos/b.jpg", self.output())

    def test_part_callback(self) -> None:
        with ui.progress("clip.mp4") as row:
            cb = row.part()
            cb(50, 100)
            self.assertEqual(row.count, 50)
            self.assertEqual(row.total, 100)

    def test_group_rows(self) -> None:
        with ui.group("batch") as grp:
            self.assertIsInstance(grp, Group)
            r1 = grp.add("Metadata", total=3)
            r2 = grp.add("Media", total=10)
            r1.advance(3)
            r1.finish()
            r2.advance(5)
        out = self.output()
        self.assertIn("batch", out)
        self.assertIn("Metadata", out)
        self.assertIn("Media", out)

    def test_concurrent_groups_share_single_live_progress(self) -> None:
        buf = io.StringIO()
        with ui.scope(color=True, dest=buf, width=80, level="info"):
            self.assertTrue(ui._progress_enabled())
            with ui.progress("item1.jpg", total=10) as row1:
                self.assertIsNotNone(ui._live_progress)
                self.assertEqual(ui._live_count, 1)
                live_prog = ui._live_progress
                with ui.progress("item2.jpg", total=20) as row2:
                    self.assertIs(ui._live_progress, live_prog)
                    self.assertEqual(ui._live_count, 2)
                    self.assertIn(row1.raw, ui._live_tasks)
                    self.assertIn(row2.raw, ui._live_tasks)
                    row1.advance(5)
                    row2.advance(10)
                self.assertEqual(ui._live_count, 1)
                self.assertIs(ui._live_progress, live_prog)
                self.assertNotIn(row2.raw, ui._live_tasks)
            self.assertEqual(ui._live_count, 0)
            self.assertIsNone(ui._live_progress)
        out = buf.getvalue()
        self.assertIn("item1.jpg", out)
        self.assertIn("item2.jpg", out)


class ScopeTest(UICase):
    """Tests for `scope`."""

    def test_scope_color_and_restore(self) -> None:
        buf2 = io.StringIO()
        with ui.scope(color="256", dest=buf2, width=100, level="debug"):
            ui.debug("colored")
        self.assertIn("\x1b[", buf2.getvalue())
        self.assertEqual(ui.level, "info")

    def test_scope_dest_flushed_immediately(self) -> None:
        flushed: list[str] = []

        class FlushStream(io.StringIO):
            def flush(self) -> None:
                flushed.append(self.getvalue())
                super().flush()

        stream = FlushStream()
        with ui.scope(dest=stream, level="info"):
            self.assertEqual(flushed, [])
            ui.info("message one")
            self.assertGreaterEqual(len(flushed), 1)
            self.assertIn("message one", flushed[-1])
            count = len(flushed)
            ui.warn("message two")
            self.assertGreater(len(flushed), count)
            self.assertIn("message two", flushed[-1])


if __name__ == "__main__":
    unittest.main()
