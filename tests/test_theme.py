import os
import unittest
from io import StringIO
from unittest import mock

from shared import style, theme
from shared._screen import key_name
from shared._theme import Bar, HelpModel, LogRecord, PromptView, Screen, Theme, detect_color, fit


class TtyStream(StringIO):
    def isatty(self):
        return True


class GlyphTest(unittest.TestCase):
    def setUp(self):
        self.theme = theme.branch(unicode=True)

    def test_namespaces(self):
        glyphs = self.theme.glyphs
        self.assertEqual((glyphs.bullet, glyphs.tick, glyphs.bang, glyphs.cross, glyphs.dot, glyphs.pointer),
                         ("•", "✓", "!", "✗", "·", "›"))
        self.assertEqual((glyphs.select.yes, glyphs.select.no), ("●", "○"))
        self.assertEqual((glyphs.check.yes, glyphs.check.no), ("✔", "·"))
        self.assertEqual((glyphs.bar.full, glyphs.bar.empty), ("━", "─"))
        self.assertEqual(len(glyphs.spin), 10)

    def test_unknown_names_raise(self):
        with self.assertRaises(AttributeError):
            self.theme.glyphs.star = "*"
        with self.assertRaises(AttributeError):
            self.theme.glyphs.select.maybe = "?"
        with self.assertRaises(AttributeError):
            self.theme.glyphs.select = "?"
        with self.assertRaises(AttributeError):
            self.theme.glyphs.star

    def test_ascii_set(self):
        self.theme.unicode = False
        glyphs = self.theme.glyphs
        self.assertEqual((glyphs.bullet, glyphs.tick, glyphs.bang, glyphs.cross, glyphs.dot, glyphs.pointer),
                         ("*", "+", "!", "x", ".", ">"))
        self.assertEqual((glyphs.select.yes, glyphs.select.no, glyphs.check.yes, glyphs.check.no),
                         ("(*)", "( )", "[x]", "[ ]"))
        self.assertEqual((glyphs.bar.full, glyphs.bar.empty, glyphs.spin), ("=", "-", ("|", "/", "-", "\\")))

    def test_assigned_glyph_wins_over_ascii(self):
        self.theme.glyphs.tick = "√"
        self.theme.glyphs.bar.full = "#"
        self.theme.unicode = False
        self.assertEqual((self.theme.glyphs.tick, self.theme.glyphs.bar.full), ("√", "#"))


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.theme = theme.branch(color="none", unicode=True)

    def test_log_templates(self):
        draw = self.theme._format_log
        self.assertEqual(draw(LogRecord("info", "saved /tmp/a")), "• saved /tmp/a")
        self.assertEqual(draw(LogRecord("ok", "done")), "✓ done")
        self.assertEqual(draw(LogRecord("warn", "retry 2")), "! retry 2")
        self.assertEqual(draw(LogRecord("error", "failed: boom")), "✗ failed: boom")
        self.assertEqual(draw(LogRecord("debug", "raw")), "· raw")
        self.assertEqual(draw(LogRecord("ok", "finished", "  folders=3")), "✓ finished  folders=3")
        self.assertEqual(draw(LogRecord("ok", "")), "✓")

    def test_progress_parts(self):
        draw = self.theme._format_log
        running = Bar("clip.mp4", 3, 10, "part 3", False)
        self.assertEqual(draw(LogRecord("info", "", bar=running)), "• clip.mp4 ━━━━━━────────────── 3/10 part 3")
        finished = Bar("clip.mp4", 10, 10, "", True)
        self.assertEqual(draw(LogRecord("ok", "", bar=finished)), "✓ clip.mp4 ━━━━━━━━━━━━━━━━━━━━ 10/10")
        self.assertTrue(draw(LogRecord("error", "", bar=running)).startswith("✗ clip.mp4"))
        over = Bar("a", 50, 10, "", False)
        self.assertIn("━" * 20, draw(LogRecord("info", "", bar=over)))
        shown = Bar("a", 1024, 2048, "", False, show=lambda n: f"{n // 1024}K")
        self.assertTrue(draw(LogRecord("info", "", bar=shown)).endswith("1K/2K"))

    def test_spinner_parts_collapse(self):
        draw = self.theme._format_log
        spinning = draw(LogRecord("info", "", bar=Bar("scroll posts", 0, None, "", False)))
        self.assertRegex(spinning, r"^• scroll posts [⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]$")
        counted = draw(LogRecord("info", "", bar=Bar("scroll posts", 12, None, "", False)))
        self.assertRegex(counted, r"^• scroll posts . 12$")
        self.assertEqual(draw(LogRecord("ok", "", bar=Bar("scroll posts", 12, None, "", True))), "✓ scroll posts 12")
        self.assertEqual(draw(LogRecord("ok", "", bar=Bar("scroll posts", 0, None, "", True))), "✓ scroll posts")

    def test_assigned_template_and_missing_slot(self):
        self.theme.templates.log.info = "[{g-bar.full}] {p-nothing} {p-message}"
        self.assertEqual(self.theme._format_log(LogRecord("info", "hi")), "[━] hi")
        with self.assertRaises(AttributeError):
            self.theme.templates.log.trace = "x"

    def test_styled_pieces_are_painted_after_filling(self):
        painted = self.theme.branch(color="16m")
        painted.templates.log.info = style.gradient("#000000", "#ffffff")("{p-message}")
        line = painted._format_log(LogRecord("info", "ab"))
        self.assertEqual(line, "\x1b[38;2;0;0;0ma\x1b[38;2;255;255;255mb\x1b[0m")

    def test_prompt_parts(self):
        draw = self.theme._format_prompt
        self.assertEqual(draw(PromptView("input", "Name", placeholder="ada")), "Name\n› ada")
        self.assertEqual(draw(PromptView("input", "Name", "sam")), "Name\n› sam")
        self.assertEqual(draw(PromptView("input", "Name", error="Enter a name.")), "Name\n›\n✗ Enter a name.")
        confirm = PromptView("confirm", "Deploy?", options=("Yes", "No"), index=1)
        self.assertEqual(draw(confirm), "Deploy?\n  ○ Yes\n› ● No")
        many = PromptView("choices", "Sources", options=("tik", "fbk"), index=1, checked=frozenset({0}))
        self.assertEqual(draw(many), "Sources\n  ✔ tik\n› · fbk")
        keys = PromptView("choice", "Source", options=("tik",), keys=(("up", ("up", "k")), ("accept", ("enter",))))
        self.assertEqual(draw(keys), "Source\n› ● tik\nup/k up  enter accept")
        self.assertEqual(draw(PromptView("input", "Name", "sam", done=True)), "✓ Name: sam")
        self.assertEqual(draw(PromptView("secret", "Token", "x", done=True)), "✓ Token")
        self.assertEqual(draw(PromptView("confirm", "Deploy?", "No", done=True)), "✓ Deploy? No")

    def test_help_parts(self):
        model = HelpModel("tool", "Does things.", "tool [options] name",
                          arguments=(("name", "who"),), options=(("--count N", ""), ("-h, --help", "")),
                          commands=(("go", "Go now"),), epilog="See ./docs/a.md")
        self.assertEqual(self.theme._format_help(model), (
            "Does things.\n\nUsage:\n  tool [options] name\n\nArguments:\n  name                who\n\n"
            "Options:\n  --count N\n  -h, --help\n\nCommands:\n  go                  Go now\n\nSee ./docs/a.md\n"
        ))
        error = HelpModel("tool", "", "tool name", error="bad")
        self.assertEqual(self.theme._format_help(error), "✗ bad\n\nUsage:\n  tool name\n")


class EpilogPathTest(unittest.TestCase):
    def test_paths_are_links_and_other_words_are_dim(self):
        painted = theme.branch(color="16m")
        line = painted._epilog_line("  use ~/Downloads/cookies.txt and/or ./a/b, not list.txt or .env or Twitter/X")
        link = "\x1b]8;;~/Downloads/cookies.txt\x1b\\\x1b[4;36m~/Downloads/cookies.txt\x1b[0m\x1b]8;;\x1b\\"
        self.assertIn(link, line)
        self.assertIn("\x1b]8;;./a/b,", line)
        self.assertIn("\x1b[2mand/or\x1b[0m", line)
        self.assertIn("\x1b[2mnot list.txt or .env or Twitter/X\x1b[0m", line)
        self.assertNotIn("]8;;and/or", line)
        self.assertNotIn("]8;;Twitter/X", line)
        self.assertNotIn("]8;;list.txt", line)
        self.assertNotIn("]8;;.env", line)
        self.assertTrue(line.startswith("  \x1b[2muse\x1b[0m "))


class ColorTest(unittest.TestCase):
    def test_validation(self):
        other = theme.branch()
        for depth in ("none", "8", "256", "16m"):
            other.color = depth
        with self.assertRaises(ValueError):
            other.color = "24bit"

    def test_detection(self):
        with mock.patch.dict(os.environ, {"COLORTERM": "truecolor", "TERM": "xterm"}, clear=True):
            self.assertEqual(detect_color(TtyStream()), "16m")
            self.assertEqual(detect_color(StringIO()), "none")
        with mock.patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            self.assertEqual(detect_color(TtyStream()), "256")
        with mock.patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": "1"}, clear=True):
            self.assertEqual(detect_color(TtyStream()), "none")
        with mock.patch.dict(os.environ, {"TERM": "dumb"}, clear=True):
            self.assertEqual(detect_color(TtyStream()), "none")


class ScopeBranchTest(unittest.TestCase):
    def test_scope_restores(self):
        screen = Screen(out=StringIO(), width=40, tty=False, interactive=False)
        before = (theme.color, theme.unicode, theme.screen)
        with theme.scope(color="8", unicode=False, screen=screen):
            self.assertEqual((theme.color, theme.unicode, theme.screen), ("8", False, screen))
        self.assertEqual((theme.color, theme.unicode, theme.screen), before)
        with self.assertRaises(RuntimeError), theme.scope(color="16m"):
            raise RuntimeError
        self.assertEqual(theme.color, before[0])
        with self.assertRaises(TypeError), theme.scope(glyphs=None):
            pass

    def test_branch_leaves_process_theme_alone(self):
        before = (theme.color, theme.glyphs.tick, theme.templates.log.ok)
        other = theme.branch(color="16m")
        other.glyphs.tick = "Y"
        other.templates.log.ok = "{p-message}"
        self.assertIsInstance(other, Theme)
        self.assertEqual((theme.color, theme.glyphs.tick, theme.templates.log.ok), before)
        self.assertEqual(other.glyphs.tick, "Y")
        self.assertIs(other.screen, theme.screen)


class ScreenTest(unittest.TestCase):
    def test_fake_screen(self):
        screen = Screen(out=StringIO(), inp=StringIO("a\r"), width=30, tty=False, interactive=True)
        self.assertEqual((screen.width, screen.tty, screen.interactive), (30, False, True))
        self.assertEqual((screen.readkey(), screen.readkey()), ("a", "enter"))
        with self.assertRaises(EOFError):
            screen.readkey()

    def test_write_strips_when_color_is_none(self):
        screen = Screen(out=StringIO(), tty=False)
        with theme.scope(color="none"):
            screen.write("\x1b[31mred\x1b[0m")
        with theme.scope(color="16m"):
            screen.write("\x1b[31m!\x1b[0m")
        self.assertEqual(screen.out.getvalue(), "red\x1b[31m!\x1b[0m")

    def test_key_names(self):
        self.assertEqual([key_name(k) for k in ("\x1b[A", "\x1b[B", "\r", "\x7f", " ", "\x03", "é")],
                         ["up", "down", "enter", "backspace", "space", "ctrl+c", "é"])

    def test_fit(self):
        self.assertEqual(fit("abcdef", 3), "abc")
        self.assertEqual(fit("ab", 3), "ab")
        self.assertEqual(fit("\x1b[31mabcdef\x1b[0m", 2), "\x1b[31mab\x1b[0m\x1b]8;;\x1b\\")
        self.assertEqual(fit("日本語", 4), "日本")


if __name__ == "__main__":
    unittest.main()
