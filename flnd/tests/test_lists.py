"""The lists of `flnd.ui.lists`: every key, the filter, the panel, the pipe's table, Ctrl-C.

A list's `View` is fed keys directly; the three lists of the command line go through Textual's
pilot once each, with their callbacks recorded. Nothing here knows the engine.
"""

from __future__ import annotations

import io
import unittest
from unittest import mock

from test_forms import Terminal, drawn, drive, feed

from flnd.errors import Stopped, UsageError
from flnd.ui import forms, lists
from flnd.ui.forms import PENDING
from flnd.ui.lists import AccountRow, Column, Key, PluginRow, Row, TargetRow, View

H, D = 3600, 86400
TARGETS = [TargetRow("ins:example_user", True, ("posts", "reels"), 1620, 2254857830, 2 * H),
           TargetRow("twt:example_creator", True, ("media",), 49, 188743680, 2 * H),
           TargetRow("tik:example_handle", False, ("videos",), 0, 0, None)]
ACCOUNTS = [AccountRow("ins", "example_user", True, "browser", None, None, 3 * D),
            AccountRow("ins", "example_handle", False, "chrome", 240, None, D),
            AccountRow("twt", "example_creator", True, "file", None, 2 * D, 20 * D)]
PLUGINS = [PluginRow("ins", "Instagram", True, 2, ("instagram.com",), 3),
           PluginRow("exm", "Example", False, 2, ("example.com",), 0)]


class ViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.rows = [Row("a", ("ins:example_user", "1")), Row("b", ("twt:example_creator", "2")),
                     Row("c", ("tik:example_handle", "3"), fixed=True)]

    def view(self) -> View:
        def record(name: str):
            return lambda row: self.calls.append((name, row))

        def refuse(row: object) -> None:
            raise UsageError("example.com refused it")

        keys = (Key("f", "follow", record("f")), Key("x", "fail", refuse),
                Key("a", "add", record("a"), leave=True))
        return View("Things", (Column("name"), Column("n", right=True)), lambda: self.rows,
                    keys, Key("d", "delete", record("d")))

    def test_each_key(self) -> None:
        cases = [
            ("down then f", ["down", "f"], [("f", "b")]),
            ("down stops at the end", ["down", "down", "down", "f"], [("f", "c")]),
            ("up stops at the top", ["up", "f"], [("f", "a")]),
            ("d asks first: no", ["d", "n", "f"], [("f", "a")]),
            ("d asks first: yes", ["d", "y"], [("d", "a")]),
            ("d does nothing on a fixed row", ["down", "down", "d", "y"], []),
            ("the filter keeps what matches", ["/", "t", "w", "enter", "f"], [("f", "b")]),
            ("esc clears the filter", ["/", "t", "w", "escape", "f"], [("f", "a")]),
            ("backspace", ["/", "t", "x", "backspace", "i", "k", "enter", "f"], [("f", "c")]),
            ("the panel takes no keys but esc", ["enter", "f", "escape", "f"], [("f", "a")]),
        ]
        for name, keys, calls in cases:
            with self.subTest(name):
                self.calls.clear()
                self.assertIs(feed(self.view(), *keys), PENDING)
                self.assertEqual(self.calls, calls)

    def test_a_paste_goes_only_into_the_filter(self) -> None:
        view = self.view()
        view.paste("fdq")
        self.assertEqual((self.calls, view.query), ([], ""))
        feed(view, "/")
        view.paste("twt")
        self.assertEqual([row.key for row in view.shown()], ["b"])

    def test_esc_quits_and_a_leaving_key_closes(self) -> None:
        view = self.view()
        self.assertIs(feed(view, "q"), PENDING)
        feed(view, "enter")
        self.assertIs(feed(view, "q"), PENDING)
        self.assertTrue(view.detail)
        self.assertIs(feed(view, "escape"), PENDING)
        self.assertIsNone(feed(self.view(), "escape"))
        key, row = feed(view, "down", "a")
        self.assertEqual((key.char, row), ("a", "b"))

    def test_an_error_shows_and_the_rows_load_again(self) -> None:
        view = self.view()
        self.rows = self.rows[:1]
        feed(view, "x")
        self.assertEqual(len(view.rows), 1)
        self.assertIn("  example.com refused it", drawn(view.view(True)))

    def test_the_titles_head_the_rows_and_the_filter_keeps_them(self) -> None:
        view = self.view()
        before = drawn(view.view(True)).splitlines()
        self.assertEqual([line.split() for line in before[2:4]],
                         [["name", "n"], ["❯", "ins:example_user", "1"]])
        feed(view, "/", "t", "w", "enter")
        after = drawn(view.view(True)).splitlines()
        self.assertEqual([line.split() for line in after[2:4]],
                         [["name", "n"], ["❯", "twt:example_creator", "2"]])
        # The columns don't move with the filter.
        self.assertEqual(before[2], after[2])

    def test_the_panel_and_the_keys_line(self) -> None:
        view = self.view()
        feed(view, "enter")
        self.assertEqual(drawn(view.view(True)).splitlines()[2:7], [
            "  ╭─ ins:example_user ─────╮",
            "  │ name  ins:example_user │",
            "  │ n     1                │",
            "  ╰────────────────────────╯",
            "",
        ])
        feed(view, "escape", "down", "down")
        self.assertTrue(drawn(view.view(False)).endswith(
            "  up/down move - enter details - f follow - x fail - a add - d delete - / filter"
            " - esc quit"))

    def test_a_long_list_scrolls_within_terminal_height(self) -> None:
        rows = [Row(i, (f"ins:user_{i:02d}", str(i))) for i in range(25)]
        view = View("Targets", (Column("name"), Column("n", right=True)), lambda: rows, (), None)
        view.height = 12  # leaves 4 visible rows (12 - 6 chrome - 2 indicators)
        first = drawn(view.view(True))
        self.assertLessEqual(len(first.splitlines()), 12)
        self.assertIn("❯ ins:user_00", first)
        self.assertIn("ins:user_03", first)
        self.assertNotIn("ins:user_04", first)
        self.assertIn("… 21 below", first)
        self.assertNotIn("above", first)

        # Moving past the bottom scrolls the window down and keeps the cursor visible.
        feed(view, "down", "down", "down", "down")
        moved = drawn(view.view(True))
        self.assertLessEqual(len(moved.splitlines()), 12)
        self.assertIn("… 1 above", moved)
        self.assertIn("❯ ins:user_04", moved)
        self.assertNotIn("ins:user_00", moved)
        self.assertIn("… 20 below", moved)

        # Page down, end, home, and page up all keep the cursor in view.
        feed(view, "pagedown")
        self.assertEqual(view.at, 8)
        self.assertIn("❯ ins:user_08", drawn(view.view(True)))
        feed(view, "end")
        bottom = drawn(view.view(True))
        self.assertEqual(view.at, 24)
        self.assertIn("❯ ins:user_24", bottom)
        self.assertIn("… 21 above", bottom)
        self.assertNotIn("below", bottom)
        feed(view, "pageup")
        self.assertEqual(view.at, 20)
        feed(view, "home")
        self.assertEqual(view.at, 0)
        self.assertIn("❯ ins:user_00", drawn(view.view(True)))

    def test_a_long_list_scrolls_through_textual_pilot(self) -> None:
        self.enterContext(mock.patch("sys.stdin", Terminal("")))
        self.enterContext(mock.patch("sys.stdout", Terminal("")))
        rows = [TargetRow(f"ins:user_{i:02d}", True, ("posts",), i, 0, None) for i in range(30)]
        picked: list[str] = []
        with drive(("end", "f", "escape")):
            lists.targets(lambda: rows, follow=lambda row: picked.append(row.name),
                          retry=lambda _: None, add=lambda _: None, delete=lambda _: None)
        self.assertEqual(picked, ["ins:user_29"])


class ListsTest(unittest.TestCase):
    def test_the_three_lists_look_like_the_mockups(self) -> None:
        cases = [
            ("targets", 100, lists.TARGET_COLUMNS, [lists._target(t, True) for t in TARGETS], [
                "  Targets · 3" + " " * 79 + "/ filter",
                "",
                "    target                followed     categories    status         files"
                "       size   last run",
                "  ❯ ins:example_user      ● followed   posts reels   synced   1,620 files"
                "     2.1 GB     2h ago",
                "    twt:example_creator   ● followed   media         synced      49 files"
                "   180.0 MB     2h ago",
                "    tik:example_handle    ○            videos        new          0 files"
                "          –      never",
            ]),
            ("accounts", 90, lists.ACCOUNT_COLUMNS, [lists._account(a, True) for a in ACCOUNTS], [
                "  Accounts · 3" + " " * 68 + "/ filter",
                "",
                "    plugin   name              default     origin    status"
                "                      saved",
                "  ❯ ins      example_user      ★ default   browser   ok                   saved"
                " 3d ago",
                "    ins      example_handle                chrome    held 4 min           saved"
                " 1d ago",
                "    twt      example_creator   ★ default   file      expires in 2 days   saved"
                " 20d ago",
            ]),
        ]
        for name, width, columns, cells, lines in cases:
            with self.subTest(name):
                view = View(name.title(), columns, lambda c=cells: [Row(n, x) for n, x in
                                                                   enumerate(c)], (), None)
                self.assertEqual(drawn(view.view(True), width=width).splitlines()[:6], lines)

    def test_a_pipe_prints_a_plain_table(self) -> None:
        stdout = self.enterContext(mock.patch("sys.stdout", io.StringIO()))
        lists.plugins(lambda: PLUGINS, install=print, uninstall=print)
        self.assertEqual(stdout.getvalue(), "\n".join([
            "code  name       kind       api  domains        targets",
            "ins   Instagram  built-in   2    instagram.com  3",
            "exm   Example    installed  2    example.com    0",
        ]) + "\n")

    def test_on_a_terminal_each_list_runs_its_callbacks(self) -> None:
        self.enterContext(mock.patch("sys.stdin", Terminal("")))
        self.enterContext(mock.patch("sys.stdout", Terminal("")))
        calls: list[tuple[str, object]] = []

        def record(name: str):
            return lambda row: calls.append((name, row))

        asked = mock.patch.object(forms, "text", wraps=forms.text)
        with drive(("down", "f", "r", "a"), ("x", "enter"), ("d", "y", "escape")), asked as text:
            lists.targets(lambda: TARGETS, follow=record("follow"), retry=record("retry"),
                          add=record("add"), delete=record("delete"))
        text.assert_called_once_with("Target to get")
        with drive(("asterisk", "a"), ("escape",)):
            lists.accounts(lambda: ACCOUNTS, default=record("default"), add=record("login"),
                           logout=record("logout"))
        with drive(("d", "y", "down", "d", "y", "a"), ("escape",), ("escape",)):
            lists.plugins(lambda: PLUGINS, install=record("install"),
                          uninstall=record("uninstall"))
        self.assertEqual(calls, [
            ("follow", TARGETS[1]), ("retry", TARGETS[1]), ("add", "x"),
            ("delete", TARGETS[1]),
            ("default", ACCOUNTS[0]), ("login", ACCOUNTS[0]),
            ("uninstall", PLUGINS[1]),
        ])

    def test_ctrl_c_raises_stopped(self) -> None:
        self.enterContext(mock.patch("sys.stdin", Terminal("")))
        self.enterContext(mock.patch("sys.stdout", Terminal("")))
        with drive(("ctrl+c",)), self.assertRaises(Stopped):
            lists.plugins(lambda: PLUGINS, install=print, uninstall=print)

    def test_narrow_terminal_and_wide_chars_fit_within_width(self) -> None:
        from rich.cells import cell_len

        wide_targets = [
            *TARGETS,
            TargetRow("twt:示例频道_示例频道_示例频道", True,
                      ("posts", "media"), 10, 2048, 2 * H),
        ]
        view = View(
            "Targets", lists.TARGET_COLUMNS,
            lambda: [Row(n, lists._target(t, True)) for n, t in enumerate(wide_targets)],
            (), None,
        )
        out = drawn(view.view(True), width=50)
        self.assertTrue(all(cell_len(line) <= 50 for line in out.splitlines()), out)

    def test_long_target_name_is_capped_and_statuses_show(self) -> None:
        from rich.cells import cell_len

        long_name = "web:https://example.com/some/very/long/gallery/slug/with_extra_tail"
        rows = [
            TargetRow(long_name, True, ("posts", "reels"), 12, 4096, 60, status="partial",
                      pending=("reels",)),
            TargetRow("ins:a", True, ("posts", "reels", "stories"), 3, 1024, 60, status="failed",
                      failed=2, pending=("stories",)),
            TargetRow("ins:b", True, ("posts",), 3, 1024, 60, status="retry", failed=2),
            TargetRow("ins:c", False, ("posts",), 0, 0, 60, status="failed"),
            TargetRow("tik?cats", True, (), 0, 0, 60, status="synced", query=True, seen=14),
        ]
        view = View(
            "Targets", lists.TARGET_COLUMNS,
            lambda: [Row(t, lists._target(t, True), details=lists._target_details(t))
                     for t in rows], (), None,
        )
        out = drawn(view.view(True), width=110)
        self.assertTrue(all(cell_len(line) <= 110 for line in out.splitlines()), out)
        # Wrapped onto multiple lines instead of cutting text with ellipsis.
        self.assertNotIn("…", out)
        self.assertIn("❯ web:https://example.com/some", out)
        self.assertIn("/very/long/gallery/slug/with", out)
        self.assertIn("_extra_tail", out)
        for text in ("1/2 done", "2/3 · 2 failed", "2 retry", "failed", "14 found", "synced"):
            self.assertIn(text, out)
        feed(view, "enter")
        panel = drawn(view.view(True), width=110)
        self.assertIn(long_name, panel)
        self.assertIn("done", panel)
        self.assertIn("pending", panel)
        feed(view, "escape", "/", "g", "a", "l", "l", "e", "r", "y", "enter")
        self.assertEqual(len(view.shown()), 1)

    def test_target_categories_are_colored_by_completion(self) -> None:
        from flnd.ui import out, theme

        def styles(row: TargetRow) -> list[tuple[str, str]]:
            text = lists._cell(lists._target(row, True)[2], True)
            return [(text.plain[span.start:span.end], str(span.style))
                    for span in text.spans]

        partial = TargetRow("ins:u", True, ("posts", "reels"), 10, 1024, 60,
                            status="partial", pending=("reels",))
        self.assertEqual(styles(partial), [
            ("posts", theme.style("good")), (" ", theme.style("focus")),
            ("reels", theme.style("warn")),
        ])
        failed = TargetRow("ins:f", False, ("posts",), 0, 0, 60,
                           status="failed", pending=("posts",))
        self.assertEqual(styles(failed), [("posts", theme.style("bad"))])
        new = TargetRow("tik:n", False, ("videos",), 0, 0, None, status="new")
        self.assertEqual(styles(new), [("videos", theme.style("dim"))])
        synced = TargetRow("ins:s", True, ("posts", "reels"), 10, 1024, 60, status="synced")
        self.assertEqual(styles(synced), [
            ("posts", theme.style("good")), (" ", theme.style("focus")),
            ("reels", theme.style("good")),
        ])
        stdout = self.enterContext(mock.patch("sys.stdout", io.StringIO()))
        out.table([lists._target(partial, True)], [c.title for c in lists.TARGET_COLUMNS])
        self.assertIn("posts reels", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
