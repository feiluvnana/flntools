"""shared.prompt's questions: `ask`, `confirm` and `choose`, drawn on a fake `theme.screen`.

The screen is a `Screen` over StringIO with its `readkey` replaced by a list of key names, so each
test presses keys and reads back the frames drawn. Nothing else is faked.
"""

import os
import re
import struct
import sys
import threading
import time
import unittest
from io import StringIO
from pathlib import Path

try:
    import fcntl
    import pty
    import select
    import termios

    HAS_PTY = True
except ImportError:
    HAS_PTY = False

from shared import log, prompt, theme
from shared._screen import Paste, paste_text
from shared._theme import Screen

INDIGO_BOLD = "\x1b[1;38;2;117;113;249m"
FUCHSIA = "\x1b[38;2;247;128;226m"
GREEN = "\x1b[38;2;2;191;135m"
TEXT = "\x1b[38;5;252m"
FAINT = "\x1b[38;5;238m"
CELL = "\x1b[38;2;255;253;245;48;2;247;128;226m \x1b[0m"
END = "\x1b[0m"


class PromptCase(unittest.TestCase):
    width = 80
    height = 24

    def setUp(self):
        self.screen = Screen(dest=StringIO(), inp=StringIO(""), width=self.width, height=self.height, tty=False,
                             interactive=True)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)
        prompt.keymap = prompt.Keymap()
        prompt.keys_shown = True

    def keys(self, *names):
        self.screen.readkey = iter(names).__next__

    def frames(self) -> list[str]:
        """Each drawn line; on a non-TTY screen every frame ends with a newline."""
        return self.screen.dest.getvalue().splitlines()

    def last_line(self) -> str:
        return self.frames()[-1]


class AskTest(PromptCase):
    def test_frame_with_description_and_placeholder(self):
        self.keys("s", "a", "m", "enter")
        self.assertEqual(prompt.ask("Name", description="Your account name", placeholder="ada"), "sam")
        self.assertEqual(self.frames()[:4], ["┃ Name", "┃ Your account name", "┃ > _ada", "enter submit"])
        self.assertEqual(self.frames()[-5:-1], ["┃ Name", "┃ Your account name", "┃ > sam_", "enter submit"])
        self.assertEqual(self.last_line(), "✓ Name: sam")

    def test_colors(self):
        self.keys("x", "enter")
        with theme.scope(color="16m"):
            prompt.ask("Name", placeholder="ada")
        output = self.screen.dest.getvalue()
        self.assertIn(f"{FAINT}┃{END} {INDIGO_BOLD}Name{END}\n", output)
        self.assertIn(f"{FAINT}┃{END} {FUCHSIA}>{END} {CELL}{FAINT}ada{END}\n", output)
        self.assertIn(f"{FUCHSIA}>{END} {TEXT}x{END}{CELL}\n", output)
        self.assertIn("\x1b[38;5;241menter\x1b[0m \x1b[38;5;239msubmit\x1b[0m\n", output)

    def test_default_is_the_buffer(self):
        self.keys("backspace", "y", "enter")
        self.assertEqual(prompt.ask("Name", default="ada"), "ady")
        self.assertEqual(self.frames()[1], "┃ > ada_")

    def test_letters_and_space_are_typed_not_actions(self):
        self.keys("k", "j", "space", "y", "enter")
        self.assertEqual(prompt.ask("Name"), "kj y")

    def test_error(self):
        def nonempty(value):
            return None if value.strip() else "Enter a name."

        self.keys("enter", "a", "enter")
        self.assertEqual(prompt.ask("Name", validate=nonempty), "a")
        self.assertEqual(self.frames()[3:7], ["┃ Name *", "┃ > _", "┃ * Enter a name.", "enter submit"])
        with theme.scope(color="16m"):
            self.keys("enter", "a", "enter")
            prompt.ask("Name", validate=nonempty)
        output = self.screen.dest.getvalue()
        self.assertIn(f"{INDIGO_BOLD}Name{END}\x1b[38;2;237;86;122m *{END}", output)
        self.assertIn("\x1b[38;2;237;86;122m* Enter a name.\x1b[0m", output)

    def test_secret(self):
        self.keys("a", "b", "c", "enter")
        self.assertEqual(prompt.ask("Token", secret=True), "abc")
        self.assertIn("┃ > •••_", self.frames())
        self.assertEqual(self.last_line(), "✓ Token")
        self.keys("a", "enter")
        prompt.ask("Token", secret=True, mask="*")
        self.assertIn("┃ > *_", self.frames())

    def test_secret_never_draws_the_answer(self):
        self.keys("p", "w", "enter")
        theme.unicode = False
        self.assertEqual(prompt.ask("Password", secret=True, description="Hidden"), "pw")
        output = self.screen.dest.getvalue()
        self.assertIn("| > **_", self.frames())
        self.assertNotIn("pw", output)
        self.assertEqual(self.last_line(), "+ Password")

    def test_mask_is_ignored_without_secret(self):
        self.keys("a", "enter")
        self.assertEqual(prompt.ask("Name", mask="*"), "a")
        self.assertIn("┃ > a_", self.frames())

    def test_description_wraps_and_every_line_gets_the_bar(self):
        self.screen._width = 20
        self.keys("enter")
        prompt.ask("Name", default="x", description="one two three four five six\nsecond")
        self.assertEqual(self.frames()[:6], [
            "┃ Name", "┃ one two three four", "┃ five six", "┃ second", "┃ > x_", "enter submit",
        ])


class KeysLineTest(PromptCase):
    def test_on_by_default_and_off_per_question(self):
        self.keys("enter")
        prompt.ask("Name", default="x", keys=False)
        self.assertEqual(self.frames(), ["┃ Name", "┃ > x_", "✓ Name: x"])

    def test_off_everywhere(self):
        prompt.keys_shown = False
        self.keys("enter", "enter")
        prompt.choose("Pick", ["a"])
        prompt.choose("Pick", ["a"], keys=True)
        self.assertEqual(self.frames(), ["┃ Pick", "┃ > a", "✓ Pick: a",
                                         "┃ Pick", "┃ > a", "↑/k up • ↓/j down • enter submit", "✓ Pick: a"])

    def test_typing_questions_list_no_letter_keys(self):
        self.keys("enter", "enter")
        prompt.choose("Pick", ["a"], filter=True)
        prompt.choose("Pick", ["a"], many=True, filter=True)
        self.assertIn("↑ up • ↓ down • enter submit", self.frames())
        self.assertIn("↑ up • ↓ down • space toggle • enter submit", self.frames())

    def test_ascii(self):
        theme.unicode = False
        self.keys("space", "enter", "enter")
        prompt.choose("Sources", ["tik", "fbk"], many=True)
        prompt.confirm("Deploy?")
        output = self.screen.dest.getvalue()
        self.assertEqual(self.frames()[-10:-5], ["| Sources", "| > [x] tik", "|   [ ] fbk",
                                                 "up/k up | down/j down | space toggle | enter submit",
                                                 "+ Sources: tik"])
        self.assertIn("left/h left | right/l right | y yes | n no | enter submit", output)
        self.assertTrue(output.isascii(), output)


class ConfirmTest(PromptCase):
    def test_frame_and_default_no(self):
        self.keys("enter")
        self.assertIs(prompt.confirm("Deploy?"), False)
        self.assertEqual(self.frames(), ["┃ Deploy?", "┃", "┃   Yes    [ No ]",
                                         "←/h left • →/l right • y yes • n no • enter submit", "✓ Deploy? No"])

    def test_buttons_in_color(self):
        self.keys("enter")
        with theme.scope(color="16m"):
            prompt.confirm("Deploy?", keys=False)
        active = "\x1b[38;2;255;253;245;48;2;247;128;226m  No  \x1b[0m"
        inactive = "\x1b[38;5;252;48;5;237m  Yes  \x1b[0m"
        self.assertIn(f"{FAINT}┃{END} {inactive}  {active}\n", self.screen.dest.getvalue())

    def test_yes_and_no_keys_answer_at_once(self):
        self.keys("y")
        self.assertIs(prompt.confirm("Deploy?"), True)
        self.keys("n")
        self.assertIs(prompt.confirm("Deploy?", default=True), False)

    def test_left_and_right_move_and_enter_submits(self):
        self.keys("left", "enter")
        self.assertIs(prompt.confirm("Deploy?"), True)
        self.assertIn("┃ [ Yes ]    No  ", self.frames())
        self.keys("h", "l", "enter")
        self.assertIs(prompt.confirm("Deploy?"), False)
        self.keys("right", "enter")
        self.assertIs(prompt.confirm("Deploy?", default=True), False)

    def test_labels(self):
        self.keys("h", "enter")
        self.assertIs(prompt.confirm("Overwrite?", yes="Overwrite", no="Keep"), True)
        self.assertEqual(self.last_line(), "✓ Overwrite? Overwrite")


class ChooseTest(PromptCase):
    def test_frame_and_done(self):
        self.keys("down", "down", "enter")
        self.assertEqual(prompt.choose("Source", ["tik", "fbk", "ins", "twt"]), "ins")
        self.assertEqual(self.frames()[:6], [
            "┃ Source", "┃ > tik", "┃   fbk", "┃   ins", "┃   twt", "↑/k up • ↓/j down • enter submit",
        ])
        self.assertEqual(self.last_line(), "✓ Source: ins")

    def test_colors(self):
        self.keys("enter")
        with theme.scope(color="16m"):
            prompt.choose("Source", ["tik", "fbk"])
        output = self.screen.dest.getvalue()
        self.assertIn(f"{FUCHSIA}>{END} {GREEN}tik{END}\n", output)
        self.assertIn(f"{FAINT}┃{END}   {TEXT}fbk{END}\n", output)

    def test_a_wider_pointer_keeps_rows_aligned(self):
        theme.glyphs.pointer = "->"
        try:
            self.keys("enter")
            prompt.choose("Source", ["tik", "fbk"])
        finally:
            theme.glyphs.pointer = ">"
        self.assertEqual(self.frames()[1:3], ["┃ -> tik", "┃    fbk"])

    def test_mapping_and_default(self):
        self.keys("enter")
        self.assertEqual(prompt.choose("Source", {"TikTok": "tik", "Instagram": "ins"}, default="ins"), "ins")
        self.assertEqual(self.last_line(), "✓ Source: Instagram")

    def test_up_stops_at_the_top_and_down_at_the_bottom(self):
        self.keys("up", "j", "j", "j", "enter")
        self.assertEqual(prompt.choose("Source", ["a", "b"]), "b")

    def test_bad_options(self):
        with self.assertRaises(prompt.Error):
            prompt.choose("Source", [])
        with self.assertRaises(prompt.Error):
            prompt.choose("Source", ["a"], default="z")
        self.assertEqual(self.screen.dest.getvalue(), "")

    def test_filter(self):
        self.keys("i", "n", "enter")
        self.assertEqual(prompt.choose("Source", ["tik", "fbk", "ins", "twt"], filter=True), "ins")
        self.assertEqual(self.frames()[-4:-1], ["┃ Source", "┃ > ins", "↑ up • ↓ down • enter submit"])

    def test_filter_types_letters_that_are_also_keys(self):
        self.keys("k", "enter")
        self.assertEqual(prompt.choose("Source", ["tik", "kwai"], filter=True), "kwai")

    def test_filter_no_match(self):
        self.keys("z", "enter", "backspace", "t", "w", "enter")
        self.assertEqual(prompt.choose("Source", ["tik", "twt"], filter=True), "twt")
        self.assertIn("┃ Source *", self.frames())
        self.assertIn("┃ * no match", self.frames())

    def test_validate(self):
        self.keys("enter", "down", "enter")
        result = prompt.choose("Source", ["a", "b"], validate=lambda value: "not a" if value == "a" else None)
        self.assertEqual(result, "b")
        self.assertIn("┃ * not a", self.frames())


class ScrollTest(PromptCase):
    height = 8

    def test_a_long_list_scrolls_with_the_cursor(self):
        self.keys(*["down"] * 5, "enter")
        options = [f"o{n}" for n in range(10)]
        self.assertEqual(prompt.choose("Pick", options), "o5")
        frames = self.screen.dest.getvalue().split("↑/k up • ↓/j down • enter submit\n")[:-1]
        self.assertEqual(frames[0].splitlines(), ["┃ Pick", "┃ > o0", "┃   o1", "┃   o2", "┃   ↓ 7 more"])
        self.assertEqual(frames[-1].splitlines(), ["┃ Pick", "┃   ↑ 4 more", "┃   o4", "┃ > o5", "┃   o6",
                                                   "┃   ↓ 3 more"])
        for frame in frames:
            self.assertLessEqual(len(frame.splitlines()) + 1, self.height - 1)

    def test_redraws_cleanly_on_a_tty(self):
        self.screen._tty = True
        self.keys(*["down"] * 9, "enter")
        prompt.choose("Pick", [f"o{n}" for n in range(10)])
        output = self.screen.dest.getvalue()
        frames = output.split("\r")
        # Each redraw moves up over exactly the lines the previous frame drew.
        for previous, current in zip(frames, frames[1:]):
            drawn = previous.split("\x1b[J")[-1].count("\n") + 1
            self.assertTrue(current.startswith(f"\x1b[{drawn - 1}A\x1b[J"), (previous, current))
            self.assertLessEqual(drawn, self.height - 1)
        self.assertTrue(output.endswith("\x1b[J✓ Pick: o9\n\x1b[?25h\x1b[?2004l"))
        self.assertIn("┃   ↑ 7 more\n", output)


class ChooseManyTest(PromptCase):
    def test_frame_and_done(self):
        self.keys("space", "down", "down", "space", "enter")
        self.assertEqual(prompt.choose("Sources", ["tik", "fbk", "ins", "twt"], many=True), ["tik", "ins"])
        self.assertEqual(self.frames()[-7:-1], ["┃ Sources", "┃   ✓ tik", "┃   • fbk", "┃ > ✓ ins", "┃   • twt",
                                                "↑/k up • ↓/j down • space toggle • enter submit"])
        self.assertEqual(self.last_line(), "✓ Sources: tik, ins")

    def test_color_follows_the_checked_state(self):
        self.keys("space", "down", "enter")
        with theme.scope(color="16m"):
            prompt.choose("Sources", ["tik", "fbk"], many=True)
        last_frame = self.screen.dest.getvalue().splitlines()[-4:-2]
        muted_dot = "\x1b[38;5;243m•\x1b[0m"
        self.assertEqual(last_frame, [f"{FAINT}┃{END}   {GREEN}✓{END} {GREEN}tik{END}",
                                      f"{FAINT}┃{END} {FUCHSIA}>{END} {muted_dot} {TEXT}fbk{END}"])

    def test_default_and_toggle_off(self):
        self.keys("space", "enter")
        self.assertEqual(prompt.choose("Sources", {"TikTok": "tik", "X": "twt"}, many=True, default=["tik", "twt"]), ["twt"])

    def test_min_and_max(self):
        self.keys("enter", "space", "down", "space", "enter", "space", "enter")
        self.assertEqual(prompt.choose("Sources", ["a", "b"], many=True, min=1, max=1), ["a"])
        self.assertIn("┃ * choose at least 1", self.frames())
        self.assertIn("┃ * choose at most 1", self.frames())

    def test_max_alone(self):
        # Checking all three is refused; unchecking the last one, under the cursor, is accepted.
        self.keys("space", "down", "space", "down", "space", "enter", "space", "enter")
        self.assertEqual(prompt.choose("Tabs", ["posts", "reels", "tagged"], many=True, max=2), ["posts", "reels"])
        self.assertIn("┃ * choose at most 2", self.frames())

    def test_min_and_max_need_many(self):
        # Without many there is one answer, so a limit on how many is a mistake, not ignored.
        with self.assertRaises(TypeError):
            prompt.choose("Sources", ["a"], min=1)  # type: ignore[call-overload]
        with self.assertRaises(TypeError):
            prompt.choose("Sources", ["a"], max=1)  # type: ignore[call-overload]
        self.assertEqual(self.screen.dest.getvalue(), "")

    def test_bad_limits_and_default(self):
        with self.assertRaises(ValueError):
            prompt.choose("Sources", ["a"], many=True, min=-1)
        with self.assertRaises(ValueError):
            prompt.choose("Sources", ["a"], many=True, min=2, max=1)
        with self.assertRaises(prompt.Error):
            prompt.choose("Sources", ["a"], many=True, default=["z"])

    def test_filter_keeps_checks(self):
        self.keys("space", "b", "space", "enter")
        self.assertEqual(prompt.choose("Sources", ["a", "b"], many=True, filter=True), ["a", "b"])


class KeymapTest(PromptCase):
    def test_every_action(self):
        prompt.keymap.up = ("w",)
        prompt.keymap.down = ("s",)
        prompt.keymap.left = ("a",)
        prompt.keymap.right = ("d",)
        prompt.keymap.submit = ("tab",)
        prompt.keymap.toggle = ("x",)
        prompt.keymap.backspace = ("ctrl+h",)
        prompt.keymap.yes = ("o",)
        prompt.keymap.no = ("q",)
        prompt.keymap.interrupt = ("escape",)
        self.keys("s", "w", "s", "tab")
        self.assertEqual(prompt.choose("Pick", ["a", "b"]), "b")
        self.keys("x", "tab")
        self.assertEqual(prompt.choose("Pick", ["a", "b"], many=True), ["a"])
        self.keys("a", "b", "ctrl+h", "tab")
        self.assertEqual(prompt.ask("Name"), "a")
        self.keys("o")
        self.assertIs(prompt.confirm("Sure?"), True)
        self.keys("q")
        self.assertIs(prompt.confirm("Sure?", default=True), False)
        self.keys("a", "tab")
        self.assertIs(prompt.confirm("Sure?"), True)
        self.keys("d", "tab")
        self.assertIs(prompt.confirm("Sure?", default=True), False)
        self.keys("escape")
        with self.assertRaises(KeyboardInterrupt):
            prompt.ask("Name")

    def test_freed_key_and_empty_action(self):
        prompt.keymap.up = ("up",)
        prompt.keymap.no = ()
        self.keys("down", "k", "enter")
        self.assertEqual(prompt.choose("Pick", ["a", "b"]), "b")
        self.keys("enter")
        prompt.confirm("Sure?")
        self.assertIn("←/h left • →/l right • y yes • enter submit", self.frames())

    def test_accept_was_renamed_submit(self):
        with self.assertRaisesRegex(AttributeError, r"^prompt\.keymap\.accept was renamed to submit$"):
            prompt.keymap.accept
        with self.assertRaisesRegex(AttributeError, r"^prompt\.keymap\.accept was renamed to submit$"):
            prompt.keymap.accept = ("tab",)

    def test_unknown_action(self):
        with self.assertRaises(AttributeError):
            prompt.keymap.jump = ("g",)
        with self.assertRaises(AttributeError):
            prompt.keymap.jump

    def test_ctrl_c(self):
        self.keys("ctrl+c")
        with self.assertRaises(KeyboardInterrupt):
            prompt.choose("Pick", ["a"])


class CursorTest(PromptCase):
    HIDE, SHOW = "\x1b[?25l", "\x1b[?25h"
    PASTE_ON, PASTE_OFF = "\x1b[?2004h", "\x1b[?2004l"

    def test_hidden_while_a_frame_is_drawn_and_shown_at_its_end(self):
        self.screen._tty = True
        self.keys("a", "enter")
        prompt.ask("Name")
        output = self.screen.dest.getvalue()
        self.assertTrue(output.startswith(self.PASTE_ON + self.HIDE))
        self.assertTrue(output.endswith("✓ Name: a\n" + self.SHOW + self.PASTE_OFF))
        self.assertEqual((output.count(self.HIDE), output.count(self.SHOW)), (1, 1))

    def test_shown_after_ctrl_c(self):
        self.screen._tty = True
        self.keys("a", "ctrl+c")
        with self.assertRaises(KeyboardInterrupt):
            prompt.ask("Name")
        output = self.screen.dest.getvalue()
        self.assertIn(self.HIDE, output)
        self.assertTrue(output.endswith(self.SHOW + self.PASTE_OFF))

    def test_shown_after_an_error(self):
        self.screen._tty = True

        def broken():
            raise OSError("terminal gone")

        self.screen.readkey = broken
        with self.assertRaises(OSError):
            prompt.ask("Name")
        self.assertTrue(self.screen.dest.getvalue().endswith(self.SHOW + self.PASTE_OFF))

    def test_the_exit_hook_shows_a_cursor_left_hidden(self):
        from shared import _screen

        self.screen._tty = True
        _screen.hide_cursor(self.screen)
        _screen._show_all_cursors()
        self.assertEqual(self.screen.dest.getvalue(), self.HIDE + self.SHOW)
        _screen._show_all_cursors()
        self.assertEqual(self.screen.dest.getvalue(), self.HIDE + self.SHOW)

    def test_a_pipe_gets_no_escape_codes(self):
        self.keys("a", "enter")
        prompt.ask("Name")
        self.assertNotIn("\x1b", self.screen.dest.getvalue())

    def test_the_cursor_sits_after_the_typed_text(self):
        self.keys("a", "b", "enter")
        prompt.ask("Name")
        self.assertIn("┃ > ab_", self.frames())

    def test_the_cursor_sits_after_the_mask(self):
        self.keys("a", "b", "enter")
        prompt.ask("Token", secret=True)
        self.assertIn("┃ > ••_", self.frames())

    def test_the_cursor_is_a_reverse_video_cell_in_color(self):
        self.keys("a", "enter")
        with theme.scope(color="16m"):
            prompt.ask("Name")
        cell = "\x1b[38;2;255;253;245;48;2;247;128;226m \x1b[0m"
        self.assertIn(f"{TEXT}a{END}{cell}\n", self.screen.dest.getvalue())

    def test_arrows_move_the_cursor_and_typing_inserts_there(self):
        self.keys("a", "c", "left", "b", "enter")
        self.assertEqual(prompt.ask("Name"), "abc")
        self.assertIn("┃ > a_c", self.frames())
        self.assertIn("┃ > ab_c", self.frames())

    def test_backspace_deletes_before_the_cursor_and_arrows_stop_at_the_ends(self):
        self.keys("a", "b", "left", "left", "left", "backspace", "right", "right", "right", "x", "enter")
        self.assertEqual(prompt.ask("Name"), "abx")
        self.keys("a", "b", "left", "backspace", "enter")
        self.assertEqual(prompt.ask("Name"), "b")

    def test_h_and_l_are_text_in_an_input(self):
        self.keys("h", "l", "enter")
        self.assertEqual(prompt.ask("Name"), "hl")


class PasteTest(PromptCase):
    HASH = "0123456789abcdef0123456789abcdef"

    def test_a_paste_is_inserted_whole_at_the_cursor(self):
        self.keys("a", "d", "left", Paste("bc"), "enter")
        self.assertEqual(prompt.ask("Name"), "abcd")
        self.assertIn("┃ > abc_d", self.frames())

    def test_a_secret_paste_is_inserted_whole_and_masked(self):
        self.keys(Paste(self.HASH), "enter")
        self.assertEqual(prompt.ask("api_hash", secret=True), self.HASH)
        self.assertIn("┃ > " + "•" * 32 + "_", self.frames())
        self.assertNotIn(self.HASH, self.screen.dest.getvalue())

    def test_newlines_in_a_paste_do_not_submit_or_stay(self):
        self.keys(paste_text("one\r\ntwo\nthree\x1b"), "enter")
        self.assertEqual(prompt.ask("Name"), "onetwothree")

    def test_a_burst_of_plain_characters(self):
        self.keys(*self.HASH, "enter")
        self.assertEqual(prompt.ask("api_hash"), self.HASH)

    def test_bracketed_paste_is_on_while_reading_and_off_after_each_exit(self):
        self.screen._tty = True
        self.keys(Paste("a"), "enter")
        prompt.ask("Name")
        output = self.screen.dest.getvalue()
        self.assertEqual((output.count("\x1b[?2004h"), output.count("\x1b[?2004l")), (1, 1))
        self.assertLess(output.index("\x1b[?2004h"), output.index("┃ Name"))
        self.screen.dest.truncate(0)
        self.keys("ctrl+c")
        with self.assertRaises(KeyboardInterrupt):
            prompt.ask("Name")
        self.assertIn("\x1b[?2004l", self.screen.dest.getvalue())


class WithoutKeysTest(PromptCase):
    def test_defaults_block(self):
        self.screen.readkey = lambda: self.fail("read a key")
        with prompt.defaults():
            self.assertIs(prompt.confirm("Deploy?", default=True), True)
            self.assertEqual(prompt.ask("Name", default="ada"), "ada")
            self.assertEqual(prompt.choose("Source", ["a", "b"], default="b"), "b")
            self.assertEqual(prompt.choose("Sources", ["a", "b"], many=True, default=[]), [])
            with self.assertRaises(prompt.Error):
                prompt.ask("Name")
            with self.assertRaises(prompt.Error):
                prompt.choose("Source", ["a"])
        self.assertEqual(self.screen.dest.getvalue(), "")

    def test_non_interactive_screen(self):
        self.screen._interactive = False
        self.assertEqual(prompt.ask("Token", secret=True, default="t"), "t")
        self.assertIs(prompt.confirm("Deploy?"), False)
        with self.assertRaises(prompt.Error):
            prompt.choose("Sources", ["a"], many=True)

    def test_no_per_call_theme(self):
        with self.assertRaises(TypeError):
            prompt.ask("Name", default="a", theme=theme)  # type: ignore[call-arg]


class LiveAreaTest(PromptCase):
    def test_done_line_replaces_the_frame_and_help_line(self):
        self.screen._tty = True
        self.keys("enter")
        prompt.ask("Name", default="a")
        self.assertEqual(self.screen.dest.getvalue(), "\x1b[?2004h\x1b[?25l┃ Name\n┃ > a_\nenter submit\r\x1b[2A\x1b[J✓ Name: a\n\x1b[?25h\x1b[?2004l")

    def test_prompt_while_bar_is_open_on_tty(self):
        self.screen._tty = True
        self.keys("enter")
        with log.progress("run", total=2) as bar:
            bar.advance()
            self.assertIs(prompt.confirm("Go?", default=True), True)
        output = self.screen.dest.getvalue()
        cleared = output.index("\x1b[1A\x1b[J")
        self.assertLess(output.index("• run"), cleared)
        self.assertLess(cleared, output.index("Go?"))
        self.assertIn("✓ Go? Yes\n\x1b[?25h\x1b[?2004l• run", output)
        self.assertTrue(output.endswith("✓ run ━━━━━━━━━━────────── 1/2\n"))

    def test_two_threads_ask_one_at_a_time(self):
        first_asking, second_started = threading.Event(), threading.Event()
        answers = {}

        def readkey():
            if threading.current_thread().name == "first":
                first_asking.set()
                second_started.wait(2)
                time.sleep(0.05)
            return "enter"

        def ask(name):
            if name == "second":
                first_asking.wait(2)
                second_started.set()
            answers[name] = prompt.ask(name.title(), default=name, keys=False)

        self.screen.readkey = readkey
        threads = [threading.Thread(target=ask, args=(name,), name=name) for name in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(answers, {"first": "first", "second": "second"})
        self.assertEqual(self.frames(), ["┃ First", "┃ > first_", "✓ First: first", "┃ Second", "┃ > second_",
                                         "✓ Second: second"])


CHILD = """
import sys
sys.path.insert(0, {root!r})
from shared import prompt
choice = prompt.choose("Log in to", ["TikTok (tik)", "Facebook (fbk)", "Telegram (tlg)"], keys=False)
secret = prompt.ask("Token", secret=True, keys=False)
sys.stdout.write("RESULT %s %d\\n" % (choice, len(secret)))
sys.stdout.flush()
"""


@unittest.skipUnless(HAS_PTY, "needs a POSIX pty")
class RealTerminalCase(unittest.TestCase):
    """Run the prompts on a real pty and check the raw bytes the terminal receives.

    The fake-screen tests above can't catch this: a `Screen` over StringIO never has a terminal
    line discipline, so raw mode clearing OPOST (which turns the terminal's newline into a bare
    line feed and staircases every line) goes unnoticed there.
    """

    def test_every_line_break_is_crlf_and_paste_survives(self):
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(TERM="xterm-256color", COLUMNS="100", LINES="30")
            os.execv(sys.executable, [sys.executable, "-c", CHILD.format(root=str(Path(__file__).parent.parent))])
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        out = b""

        def pump(until: bytes, timeout=10.0) -> None:
            nonlocal out
            end = time.monotonic() + timeout
            while until not in out and time.monotonic() < end:
                if select.select([fd], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    out += chunk

        try:
            pump(b"Telegram")
            os.write(fd, b"\x1b[B")  # down
            pump(b"Telegram")
            time.sleep(0.2)
            os.write(fd, b"\r")
            pump(b"Token")
            time.sleep(0.2)
            os.write(fd, b"\x1b[200~" + b"x" * 32 + b"\x1b[201~")
            time.sleep(0.2)
            os.write(fd, b"\r")
            pump(b"RESULT")
            pump(b"\n", 2)
        finally:
            os.close(fd)
            os.waitpid(pid, 0)
        self.assertIn(b"Log in to", out)
        self.assertRegex(out, rb"RESULT .+ 32")
        self.assertIn(b"\n", out)
        self.assertIsNone(re.search(rb"(?<!\r)\n", out), out)


if __name__ == "__main__":
    unittest.main()
