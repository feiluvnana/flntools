"""Tests for `shared._ui`: Rich-backed logging, progress, status rows, recording and scoping."""

import io
import tempfile
import threading
import unittest
from pathlib import Path

from rich.console import Console as RichConsole
from rich.panel import Panel
from rich.rule import Rule
from rich.style import Style
from rich.table import Table
from rich.tree import Tree

import shared
from shared import _clock as clock
from shared._ui import Count, Error, Group, Row, Status, StatusRow, ui


class UICase(unittest.TestCase):
    """Base test case capturing `ui` output in a `StringIO` buffer with a fake clock."""

    def setUp(self) -> None:
        self.buf = io.StringIO()
        self.fake_clock = clock.Fake(start=100.0)
        self.enterContext(clock.use(self.fake_clock))
        self.enterContext(ui.scope(color="none", dest=self.buf, width=80, level="info"))

    def output(self) -> str:
        """Return captured output."""
        return self.buf.getvalue()


class LoggingTest(UICase):
    """Tests for log levels, formatting, nested specs and escape sanitization."""

    def test_error_hierarchy(self) -> None:
        self.assertIs(ui.Error, Error)
        self.assertTrue(issubclass(Error, shared.Error))

    def test_console_raw(self) -> None:
        self.assertIsInstance(ui.console.raw, RichConsole)

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

    def test_event_kinds(self) -> None:
        with ui.scope(level="debug"):
            ui.event("skip", "{a} -> {b}", a="a.jpg", b="b.jpg")
            ui.event("link", "linked {name}", name="c.jpg")
            ui.event("shrink", "shrank {name}", name="d.jpg")
        out = self.output()
        self.assertIn("a.jpg -> b.jpg", out)
        self.assertIn("linked c.jpg", out)
        self.assertIn("shrank d.jpg", out)
        with self.assertRaises(ValueError):
            ui.event("unknown", "nope")


class ComponentsTest(UICase):
    """Tests for `table`, `tree`, `panel`, `rule`, `style` and `strip`."""

    def test_table_tree_panel_rule(self) -> None:
        tbl = ui.table(["Col1", "Col2"], rows=[("a", "1")])
        self.assertIsInstance(tbl, Table)
        tr = ui.tree("root")
        tr.add("leaf")
        self.assertIsInstance(tr, Tree)
        ui.console.print(tr)
        pnl = ui.panel("body", title="Box")
        self.assertIsInstance(pnl, Panel)
        rl = ui.rule("End")
        self.assertIsInstance(rl, Rule)
        out = self.output()
        self.assertIn("Col1", out)
        self.assertIn("leaf", out)
        self.assertIn("Box", out)
        self.assertIn("End", out)

    def test_style_and_strip(self) -> None:
        st = ui.style("bold #02BF87")
        self.assertIsInstance(st, Style)
        self.assertTrue(st.bold)
        with self.assertRaises(Error):
            ui.style("not_a_real_style_name_xyz")
        self.assertEqual(ui.strip("\x1b[32mok\x1b[0m\x07!"), "ok!")


class ProgressTest(UICase):
    """Tests for `progress`, `group`, `Row`, `StatusRow` and `pause_live`."""

    def test_progress_ok_and_error(self) -> None:
        with ui.progress("photos/a.jpg", total=10) as row:
            self.assertIsInstance(row, Row)
            row.caption = "downloading"
            row.advance(4)
            self.assertEqual(row.count, 4)
        self.assertEqual(row.outcome, "ok")
        self.assertEqual(row.caption, "")
        self.assertIn("photos/a.jpg", self.output())

        bad: Row | None = None
        with self.assertRaises(RuntimeError):
            with ui.progress("photos/b.jpg", total=10) as bad:
                bad.caption = "404"
                raise RuntimeError("boom")
        assert bad is not None
        self.assertEqual(bad.outcome, "error")
        self.assertEqual(bad.caption, "404")

    def test_part_callback(self) -> None:
        with ui.progress("clip.mp4") as row:
            cb = row.part()
            cb(50, 100)
            self.assertEqual(row.count, 50)
            self.assertEqual(row.total, 100)

    def test_group_and_status_row(self) -> None:
        with ui.group("batch") as grp:
            self.assertIsInstance(grp, Group)
            srow = grp.add_status(
                Status("example.com", "posts", counts=(Count(3, "saved", "ok", "tick"),)),
            )
            self.assertIsInstance(srow, StatusRow)
            self.fake_clock.advance(2.0)
            srow.finish()
            self.assertTrue(srow.finished)
            self.assertEqual(srow.outcome, "ok")
        out = self.output()
        self.assertIn("batch", out)
        self.assertIn("example.com", out)
        self.assertIn("2s", out)

    def test_pause_live_holds_and_flushes(self) -> None:
        with ui.progress("work", total=2):
            with ui.pause_live():
                ui.info("during pause")
                self.assertNotIn("during pause", self.output())
            self.assertIn("during pause", self.output())


class ScopeAndRecordTest(UICase):
    """Tests for `scope`, `record` timestamps, URL scrubbing and thread safety."""

    def test_scope_color_and_restore(self) -> None:
        buf2 = io.StringIO()
        with ui.scope(color="256", dest=buf2, width=100, level="debug"):
            ui.debug("colored")
        self.assertIn("\x1b[", buf2.getvalue())
        self.assertEqual(ui.level, "info")

    def test_record_timestamps_and_url_scrubbing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "sub" / "run.log"
            with ui.record(log_path):
                ui.debug("GET https://user:secret@cdn.example.com/a.jpg?sig=tok#frag")
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("https://cdn.example.com/a.jpg", content)
            self.assertNotIn("secret", content)
            self.assertNotIn("sig=tok", content)
            self.assertNotIn("frag", content)
            self.assertRegex(content, r"^\d{2}:\d{2}:\d{2}\.\d{3} ")

    def test_record_thread_safe_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "concurrent.log"
            errors: list[Exception] = []
            stop = threading.Event()

            def writer() -> None:
                try:
                    while not stop.is_set():
                        ui.info("tick")
                except Exception as exc:
                    errors.append(exc)

            t = threading.Thread(target=writer)
            t.start()
            with ui.record(log_path):
                for _ in range(20):
                    ui.info("main")
            stop.set()
            t.join(timeout=2.0)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
