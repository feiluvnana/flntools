import threading
import time
import unittest
from io import StringIO

from shared import log, prompt, theme
from shared._theme import Screen


class PromptCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=StringIO(), inp=StringIO(""), width=80, tty=False, interactive=True)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)
        prompt.keymap = prompt.Keymap()

    def keys(self, *names):
        self.screen.readkey = iter(names).__next__

    def frames(self) -> list[str]:
        """Each drawn frame; on a non-TTY screen every frame ends with a newline."""
        return self.screen.out.getvalue().splitlines()

    def last_line(self) -> str:
        return self.frames()[-1]


class InputTest(PromptCase):
    def test_placeholder_then_typed_value(self):
        self.keys("s", "a", "m", "enter")
        self.assertEqual(prompt.input("Name", placeholder="ada"), "sam")
        self.assertEqual(self.frames()[:2], ["Name", "› ada"])
        self.assertEqual(self.frames()[-3:-1], ["Name", "› sam"])
        self.assertEqual(self.last_line(), "✓ Name: sam")

    def test_placeholder_is_muted(self):
        self.keys("enter")
        with theme.scope(color="16m"):
            prompt.input("Name", placeholder="ada")
        self.assertIn("\x1b[90mada\x1b[0m", self.screen.out.getvalue())

    def test_default_is_the_buffer(self):
        self.keys("backspace", "y", "enter")
        self.assertEqual(prompt.input("Name", default="ada"), "ady")
        self.assertEqual(self.frames()[1], "› ada")

    def test_letters_and_space_are_typed_not_actions(self):
        self.keys("k", "j", "space", "y", "enter")
        self.assertEqual(prompt.input("Name"), "kj y")

    def test_validate(self):
        def nonempty(value):
            return None if value.strip() else "Enter a name."

        self.keys("enter", "a", "enter")
        self.assertEqual(prompt.input("Name", validate=nonempty), "a")
        self.assertEqual(self.frames()[2:5], ["Name", "›", "✗ Enter a name."])

    def test_key_guide(self):
        self.keys("enter")
        prompt.input("Name", default="x", keys=True)
        self.assertEqual(self.frames()[2], "enter accept")

    def test_secret(self):
        self.keys("a", "b", "c", "enter")
        self.assertEqual(prompt.secret("Token"), "abc")
        self.assertIn("› •••", self.frames())
        self.assertEqual(self.last_line(), "✓ Token")
        self.keys("a", "enter")
        prompt.secret("Token", mask="*")
        self.assertIn("› *", self.frames())


class ConfirmTest(PromptCase):
    def test_frame_and_default_no(self):
        self.keys("enter")
        self.assertIs(prompt.confirm("Deploy?"), False)
        self.assertEqual(self.frames(), ["Deploy?", "  ○ Yes", "› ● No", "✓ Deploy? No"])

    def test_yes_and_no_keys(self):
        self.keys("y")
        self.assertIs(prompt.confirm("Deploy?"), True)
        self.keys("n")
        self.assertIs(prompt.confirm("Deploy?", default=True), False)

    def test_up_down_and_labels(self):
        self.keys("k", "enter")
        self.assertIs(prompt.confirm("Overwrite?", default=False, yes="Overwrite", no="Keep"), True)
        self.assertEqual(self.last_line(), "✓ Overwrite? Overwrite")
        self.keys("down", "enter")
        self.assertIs(prompt.confirm("Deploy?", default=True), False)

    def test_key_guide(self):
        self.keys("enter")
        prompt.confirm("Deploy?", keys=True)
        self.assertEqual(self.frames()[3], "up/k up  down/j down  y yes  n no  enter accept")


class ChoiceTest(PromptCase):
    def test_frame_keys_and_done(self):
        self.keys("down", "down", "enter")
        self.assertEqual(prompt.choice("Source", ["tik", "fbk", "ins", "twt"], keys=True), "ins")
        self.assertEqual(self.frames()[:6], [
            "Source", "› ● tik", "  ○ fbk", "  ○ ins", "  ○ twt", "up/k up  down/j down  enter accept",
        ])
        self.assertEqual(self.last_line(), "✓ Source: ins")

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
        self.assertEqual(self.frames()[-3:-1], ["Source", "› ● ins"])

    def test_filter_types_letters_that_are_also_keys(self):
        self.keys("k", "enter")
        self.assertEqual(prompt.choice("Source", ["tik", "kwai"], filter=True), "kwai")

    def test_filter_no_match(self):
        self.keys("z", "enter", "backspace", "t", "w", "enter")
        self.assertEqual(prompt.choice("Source", ["tik", "twt"], filter=True), "twt")
        self.assertIn("✗ no match", self.frames())

    def test_validate(self):
        self.keys("enter", "down", "enter")
        result = prompt.choice("Source", ["a", "b"], validate=lambda value: "not a" if value == "a" else None)
        self.assertEqual(result, "b")
        self.assertIn("✗ not a", self.frames())


class ChoicesTest(PromptCase):
    def test_frame_and_done(self):
        self.keys("space", "down", "down", "space", "enter")
        self.assertEqual(prompt.choices("Sources", ["tik", "fbk", "ins", "twt"]), ["tik", "ins"])
        self.assertEqual(self.frames()[-6:-1], ["Sources", "  ✔ tik", "  · fbk", "› ✔ ins", "  · twt"])
        self.assertEqual(self.last_line(), "✓ Sources: tik, ins")

    def test_default_and_toggle_off(self):
        self.keys("space", "enter")
        self.assertEqual(prompt.choices("Sources", {"TikTok": "tik", "X": "twt"}, default=["tik", "twt"]), ["twt"])

    def test_min_and_max(self):
        self.keys("enter", "space", "down", "space", "enter", "space", "enter")
        self.assertEqual(prompt.choices("Sources", ["a", "b"], min=1, max=1), ["a"])
        self.assertIn("✗ choose at least 1", self.frames())
        self.assertIn("✗ choose at most 1", self.frames())

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

    def test_key_guide(self):
        self.keys("enter")
        prompt.choices("Sources", ["a"], keys=True)
        self.assertEqual(self.frames()[2], "up/k up  down/j down  space toggle  enter accept")


class KeymapTest(PromptCase):
    def test_every_action(self):
        prompt.keymap.up = ("w",)
        prompt.keymap.down = ("s",)
        prompt.keymap.accept = ("tab",)
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
        self.keys("escape")
        with self.assertRaises(KeyboardInterrupt):
            prompt.input("Name")

    def test_freed_key_and_empty_action(self):
        prompt.keymap.up = ("up",)
        prompt.keymap.no = ()
        self.keys("down", "k", "enter")
        self.assertEqual(prompt.choice("Pick", ["a", "b"]), "b")
        self.keys("enter")
        prompt.confirm("Sure?", keys=True)
        self.assertIn("up up  down/j down  y yes  enter accept", self.frames())
        self.keys("left", "right", "h", "l", "enter")
        self.assertEqual(prompt.choice("Pick", ["a", "b"]), "a")

    def test_unknown_action(self):
        with self.assertRaises(AttributeError):
            prompt.keymap.jump = ("g",)

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


class LiveAreaTest(PromptCase):
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
            answers[name] = prompt.input(name.title(), default=name)

        self.screen.readkey = readkey
        threads = [threading.Thread(target=ask, args=(name,), name=name) for name in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(answers, {"first": "first", "second": "second"})
        self.assertEqual(self.frames(), ["First", "› first", "✓ First: first", "Second", "› second",
                                         "✓ Second: second"])


if __name__ == "__main__":
    unittest.main()
