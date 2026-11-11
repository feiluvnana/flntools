import threading
import time
import unittest
from io import StringIO

from shared import log, prompt, theme
from shared._theme import Screen

INDIGO_BOLD = "\x1b[1;38;2;117;113;249m"
FUCHSIA = "\x1b[38;2;247;128;226m"
GREEN = "\x1b[38;2;2;191;135m"
TEXT = "\x1b[38;5;252m"
FAINT = "\x1b[38;5;238m"
END = "\x1b[0m"


class PromptCase(unittest.TestCase):
    width = 80
    height = 24

    def setUp(self):
        self.screen = Screen(out=StringIO(), inp=StringIO(""), width=self.width, height=self.height, tty=False,
                             interactive=True)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)
        prompt.keymap = prompt.Keymap()
        prompt.keys = True

    def keys(self, *names):
        self.screen.readkey = iter(names).__next__

    def frames(self) -> list[str]:
        """Each drawn line; on a non-TTY screen every frame ends with a newline."""
        return self.screen.out.getvalue().splitlines()

    def last_line(self) -> str:
        return self.frames()[-1]


class InputTest(PromptCase):
    def test_frame_with_description_and_placeholder(self):
        self.keys("s", "a", "m", "enter")
        self.assertEqual(prompt.input("Name", description="Your account name", placeholder="ada"), "sam")
        self.assertEqual(self.frames()[:4], ["┃ Name", "┃ Your account name", "┃ > ada", "enter submit"])
        self.assertEqual(self.frames()[-5:-1], ["┃ Name", "┃ Your account name", "┃ > sam", "enter submit"])
        self.assertEqual(self.last_line(), "✓ Name: sam")

    def test_colors(self):
        self.keys("x", "enter")
        with theme.scope(color="16m"):
            prompt.input("Name", placeholder="ada")
        output = self.screen.out.getvalue()
        self.assertIn(f"{FAINT}┃{END} {INDIGO_BOLD}Name{END}\n", output)
        self.assertIn(f"{FAINT}┃{END} {FUCHSIA}>{END} {FAINT}ada{END}\n", output)
        self.assertIn(f"{FUCHSIA}>{END} {TEXT}x{END}\n", output)
        self.assertIn("\x1b[38;5;241menter\x1b[0m \x1b[38;5;239msubmit\x1b[0m\n", output)

    def test_default_is_the_buffer(self):
        self.keys("backspace", "y", "enter")
        self.assertEqual(prompt.input("Name", default="ada"), "ady")
        self.assertEqual(self.frames()[1], "┃ > ada")

    def test_letters_and_space_are_typed_not_actions(self):
        self.keys("k", "j", "space", "y", "enter")
        self.assertEqual(prompt.input("Name"), "kj y")

    def test_error(self):
        def nonempty(value):
            return None if value.strip() else "Enter a name."

        self.keys("enter", "a", "enter")
        self.assertEqual(prompt.input("Name", validate=nonempty), "a")
        self.assertEqual(self.frames()[3:7], ["┃ Name *", "┃ >", "┃ * Enter a name.", "enter submit"])
        with theme.scope(color="16m"):
            self.keys("enter", "a", "enter")
            prompt.input("Name", validate=nonempty)
        output = self.screen.out.getvalue()
        self.assertIn(f"{INDIGO_BOLD}Name{END}\x1b[38;2;237;86;122m *{END}", output)
        self.assertIn("\x1b[38;2;237;86;122m* Enter a name.\x1b[0m", output)

    def test_secret(self):
        self.keys("a", "b", "c", "enter")
        self.assertEqual(prompt.secret("Token"), "abc")
        self.assertIn("┃ > •••", self.frames())
        self.assertEqual(self.last_line(), "✓ Token")
        self.keys("a", "enter")
        prompt.secret("Token", mask="*")
        self.assertIn("┃ > *", self.frames())

    def test_description_wraps_and_every_line_gets_the_bar(self):
        self.screen._width = 20
        self.keys("enter")
        prompt.input("Name", default="x", description="one two three four five six\nsecond")
        self.assertEqual(self.frames()[:6], [
            "┃ Name", "┃ one two three four", "┃ five six", "┃ second", "┃ > x", "enter submit",
        ])


class KeysLineTest(PromptCase):
    def test_on_by_default_and_off_per_question(self):
        self.keys("enter")
        prompt.input("Name", default="x", keys=False)
        self.assertEqual(self.frames(), ["┃ Name", "┃ > x", "✓ Name: x"])

    def test_off_everywhere(self):
        prompt.keys = False
        self.keys("enter", "enter")
        prompt.choice("Pick", ["a"])
        prompt.choice("Pick", ["a"], keys=True)
        self.assertEqual(self.frames(), ["┃ Pick", "┃ > a", "✓ Pick: a",
                                         "┃ Pick", "┃ > a", "↑/k up • ↓/j down • enter submit", "✓ Pick: a"])

    def test_typing_questions_list_no_letter_keys(self):
        self.keys("enter", "enter")
        prompt.choice("Pick", ["a"], filter=True)
        prompt.choices("Pick", ["a"], filter=True)
        self.assertIn("↑ up • ↓ down • enter submit", self.frames())
        self.assertIn("↑ up • ↓ down • space toggle • enter submit", self.frames())

    def test_ascii(self):
        theme.unicode = False
        self.keys("space", "enter", "enter")
        prompt.choices("Sources", ["tik", "fbk"])
        prompt.confirm("Deploy?")
        output = self.screen.out.getvalue()
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
        self.assertIn(f"{FAINT}┃{END} {inactive}  {active}\n", self.screen.out.getvalue())

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


class ChoiceTest(PromptCase):
    def test_frame_and_done(self):
        self.keys("down", "down", "enter")
        self.assertEqual(prompt.choice("Source", ["tik", "fbk", "ins", "twt"]), "ins")
        self.assertEqual(self.frames()[:6], [
            "┃ Source", "┃ > tik", "┃   fbk", "┃   ins", "┃   twt", "↑/k up • ↓/j down • enter submit",
        ])
        self.assertEqual(self.last_line(), "✓ Source: ins")

    def test_colors(self):
        self.keys("enter")
        with theme.scope(color="16m"):
            prompt.choice("Source", ["tik", "fbk"])
        output = self.screen.out.getvalue()
        self.assertIn(f"{FUCHSIA}>{END} {GREEN}tik{END}\n", output)
        self.assertIn(f"{FAINT}┃{END}   {TEXT}fbk{END}\n", output)

    def test_a_wider_pointer_keeps_rows_aligned(self):
        theme.glyphs.pointer = "->"
        try:
            self.keys("enter")
            prompt.choice("Source", ["tik", "fbk"])
        finally:
            theme.glyphs.pointer = ">"
        self.assertEqual(self.frames()[1:3], ["┃ -> tik", "┃    fbk"])

    def test_mapping_and_default(self):
        self.keys("enter")
        self.assertEqual(prompt.choice("Source", {"TikTok": "tik", "Instagram": "ins"}, default="ins"), "ins")
        self.assertEqual(self.last_line(), "✓ Source: Instagram")

    def test_up_stops_at_the_top_and_down_at_the_bottom(self):
        self.keys("up", "j", "j", "j", "enter")
        self.assertEqual(prompt.choice("Source", ["a", "b"]), "b")

    def test_bad_options(self):
        with self.assertRaises(prompt.PromptError):
            prompt.choice("Source", [])
        with self.assertRaises(prompt.PromptError):
            prompt.choice("Source", ["a"], default="z")
        self.assertEqual(self.screen.out.getvalue(), "")

    def test_filter(self):
        self.keys("i", "n", "enter")
        self.assertEqual(prompt.choice("Source", ["tik", "fbk", "ins", "twt"], filter=True), "ins")
        self.assertEqual(self.frames()[-4:-1], ["┃ Source", "┃ > ins", "↑ up • ↓ down • enter submit"])

    def test_filter_types_letters_that_are_also_keys(self):
        self.keys("k", "enter")
        self.assertEqual(prompt.choice("Source", ["tik", "kwai"], filter=True), "kwai")

    def test_filter_no_match(self):
        self.keys("z", "enter", "backspace", "t", "w", "enter")
        self.assertEqual(prompt.choice("Source", ["tik", "twt"], filter=True), "twt")
        self.assertIn("┃ Source *", self.frames())
        self.assertIn("┃ * no match", self.frames())

    def test_validate(self):
        self.keys("enter", "down", "enter")
        result = prompt.choice("Source", ["a", "b"], validate=lambda value: "not a" if value == "a" else None)
        self.assertEqual(result, "b")
        self.assertIn("┃ * not a", self.frames())


class ScrollTest(PromptCase):
    height = 8

    def test_a_long_list_scrolls_with_the_cursor(self):
        self.keys(*["down"] * 5, "enter")
        options = [f"o{n}" for n in range(10)]
        self.assertEqual(prompt.choice("Pick", options), "o5")
        frames = self.screen.out.getvalue().split("↑/k up • ↓/j down • enter submit\n")[:-1]
        self.assertEqual(frames[0].splitlines(), ["┃ Pick", "┃ > o0", "┃   o1", "┃   o2", "┃   ↓ 7 more"])
        self.assertEqual(frames[-1].splitlines(), ["┃ Pick", "┃   ↑ 4 more", "┃   o4", "┃ > o5", "┃   o6",
                                                   "┃   ↓ 3 more"])
        for frame in frames:
            self.assertLessEqual(len(frame.splitlines()) + 1, self.height - 1)

    def test_redraws_cleanly_on_a_tty(self):
        self.screen._tty = True
        self.keys(*["down"] * 9, "enter")
        prompt.choice("Pick", [f"o{n}" for n in range(10)])
        output = self.screen.out.getvalue()
        frames = output.split("\r")
        # Each redraw moves up over exactly the lines the previous frame drew.
        for previous, current in zip(frames, frames[1:]):
            drawn = previous.split("\x1b[J")[-1].count("\n") + 1
            self.assertTrue(current.startswith(f"\x1b[{drawn - 1}A\x1b[J"), (previous, current))
            self.assertLessEqual(drawn, self.height - 1)
        self.assertTrue(output.endswith("\x1b[J✓ Pick: o9\n"))
        self.assertIn("┃   ↑ 7 more\n", output)


class ChoicesTest(PromptCase):
    def test_frame_and_done(self):
        self.keys("space", "down", "down", "space", "enter")
        self.assertEqual(prompt.choices("Sources", ["tik", "fbk", "ins", "twt"]), ["tik", "ins"])
        self.assertEqual(self.frames()[-7:-1], ["┃ Sources", "┃   ✓ tik", "┃   • fbk", "┃ > ✓ ins", "┃   • twt",
                                                "↑/k up • ↓/j down • space toggle • enter submit"])
        self.assertEqual(self.last_line(), "✓ Sources: tik, ins")

    def test_color_follows_the_checked_state(self):
        self.keys("space", "down", "enter")
        with theme.scope(color="16m"):
            prompt.choices("Sources", ["tik", "fbk"])
        last_frame = self.screen.out.getvalue().splitlines()[-4:-2]
        muted_dot = "\x1b[38;5;243m•\x1b[0m"
        self.assertEqual(last_frame, [f"{FAINT}┃{END}   {GREEN}✓{END} {GREEN}tik{END}",
                                      f"{FAINT}┃{END} {FUCHSIA}>{END} {muted_dot} {TEXT}fbk{END}"])

    def test_default_and_toggle_off(self):
        self.keys("space", "enter")
        self.assertEqual(prompt.choices("Sources", {"TikTok": "tik", "X": "twt"}, default=["tik", "twt"]), ["twt"])

    def test_min_and_max(self):
        self.keys("enter", "space", "down", "space", "enter", "space", "enter")
        self.assertEqual(prompt.choices("Sources", ["a", "b"], min=1, max=1), ["a"])
        self.assertIn("┃ * choose at least 1", self.frames())
        self.assertIn("┃ * choose at most 1", self.frames())

    def test_bad_limits_and_default(self):
        with self.assertRaises(ValueError):
            prompt.choices("Sources", ["a"], min=-1)
        with self.assertRaises(ValueError):
            prompt.choices("Sources", ["a"], min=2, max=1)
        with self.assertRaises(prompt.PromptError):
            prompt.choices("Sources", ["a"], default=["z"])

    def test_filter_keeps_checks(self):
        self.keys("space", "b", "space", "enter")
        self.assertEqual(prompt.choices("Sources", ["a", "b"], filter=True), ["a", "b"])


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
        self.assertEqual(prompt.choice("Pick", ["a", "b"]), "b")
        self.keys("x", "tab")
        self.assertEqual(prompt.choices("Pick", ["a", "b"]), ["a"])
        self.keys("a", "b", "ctrl+h", "tab")
        self.assertEqual(prompt.input("Name"), "a")
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
            prompt.input("Name")

    def test_freed_key_and_empty_action(self):
        prompt.keymap.up = ("up",)
        prompt.keymap.no = ()
        self.keys("down", "k", "enter")
        self.assertEqual(prompt.choice("Pick", ["a", "b"]), "b")
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
            prompt.choice("Pick", ["a"])


class WithoutKeysTest(PromptCase):
    def test_defaults_block(self):
        self.screen.readkey = lambda: self.fail("read a key")
        with prompt.defaults():
            self.assertIs(prompt.confirm("Deploy?", default=True), True)
            self.assertEqual(prompt.input("Name", default="ada"), "ada")
            self.assertEqual(prompt.choice("Source", ["a", "b"], default="b"), "b")
            self.assertEqual(prompt.choices("Sources", ["a", "b"], default=[]), [])
            with self.assertRaises(prompt.PromptError):
                prompt.input("Name")
            with self.assertRaises(prompt.PromptError):
                prompt.choice("Source", ["a"])
        self.assertEqual(self.screen.out.getvalue(), "")

    def test_non_interactive_screen(self):
        self.screen._interactive = False
        self.assertEqual(prompt.secret("Token", default="t"), "t")
        self.assertIs(prompt.confirm("Deploy?"), False)
        with self.assertRaises(prompt.PromptError):
            prompt.choices("Sources", ["a"])

    def test_no_per_call_theme(self):
        with self.assertRaises(TypeError):
            prompt.input("Name", default="a", theme=theme)  # type: ignore[call-arg]


class LiveAreaTest(PromptCase):
    def test_done_line_replaces_the_frame_and_help_line(self):
        self.screen._tty = True
        self.keys("enter")
        prompt.input("Name", default="a")
        self.assertEqual(self.screen.out.getvalue(), "┃ Name\n┃ > a\nenter submit\r\x1b[2A\x1b[J✓ Name: a\n")

    def test_prompt_while_bar_is_open_on_tty(self):
        self.screen._tty = True
        self.keys("enter")
        with log.progress("run", total=2) as bar:
            bar.advance()
            self.assertIs(prompt.confirm("Go?", default=True), True)
        output = self.screen.out.getvalue()
        cleared = output.index("\x1b[1A\x1b[J")
        self.assertLess(output.index("• run"), cleared)
        self.assertLess(cleared, output.index("Go?"))
        self.assertIn("✓ Go? Yes\n• run", output)
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
            answers[name] = prompt.input(name.title(), default=name, keys=False)

        self.screen.readkey = readkey
        threads = [threading.Thread(target=ask, args=(name,), name=name) for name in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(answers, {"first": "first", "second": "second"})
        self.assertEqual(self.frames(), ["┃ First", "┃ > first", "✓ First: first", "┃ Second", "┃ > second",
                                         "✓ Second: second"])


if __name__ == "__main__":
    unittest.main()
