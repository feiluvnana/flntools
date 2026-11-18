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

    def test_q_quits_and_a_leaving_key_closes(self) -> None:
        view = self.view()
        self.assertIsNone(feed(view, "q"))
        key, row = feed(view, "down", "a")
        self.assertEqual((key.char, row), ("a", "b"))

    def test_an_error_shows_and_the_rows_load_again(self) -> None:
        view = self.view()
        self.rows = self.rows[:1]
        feed(view, "x")
        self.assertEqual(len(view.rows), 1)
        self.assertIn("  example.com refused it", drawn(view.view(True)))

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
            " - q quit"))


class ListsTest(unittest.TestCase):
    def test_the_three_lists_look_like_the_mockups(self) -> None:
        cases = [
            ("targets", lists.TARGET_COLUMNS, [lists._target(t, True) for t in TARGETS], [
                "  Targets · 3" + " " * 69 + "/ filter",
                "",
                "  ❯ ins:example_user      ● followed   posts reels   1,620 files     2.1 GB"
                "   2h ago",
                "    twt:example_creator   ● followed   media            49 files   180.0 MB"
                "   2h ago",
                "    tik:example_handle    ○            videos            0 files          –"
                "    never",
            ]),
            ("accounts", lists.ACCOUNT_COLUMNS, [lists._account(a, True) for a in ACCOUNTS], [
                "  Accounts · 3" + " " * 68 + "/ filter",
                "",
                "  ❯ ins   example_user      ★ default   browser   ok                   saved"
                " 3d ago",
                "    ins   example_handle                chrome    held 4 min           saved"
                " 1d ago",
                "    twt   example_creator   ★ default   file      expires in 2 days   saved"
                " 20d ago",
            ]),
        ]
        for name, columns, cells, lines in cases:
            with self.subTest(name):
                view = View(name.title(), columns, lambda c=cells: [Row(n, x) for n, x in
                                                                   enumerate(c)], (), None)
                self.assertEqual(drawn(view.view(True), width=90).splitlines()[:5], lines)

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
        with drive(("down", "f", "r", "a"), ("x", "enter"), ("d", "y", "q")), asked as text:
            lists.targets(lambda: TARGETS, follow=record("follow"), retry=record("retry"),
                          add=record("add"), delete=record("delete"))
        text.assert_called_once_with("Target to get")
        with drive(("asterisk", "a"), ("q",)):
            lists.accounts(lambda: ACCOUNTS, default=record("default"), add=record("login"),
                           logout=record("logout"))
        with drive(("d", "y", "down", "d", "y", "a"), ("escape",), ("q",)):
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


if __name__ == "__main__":
    unittest.main()
