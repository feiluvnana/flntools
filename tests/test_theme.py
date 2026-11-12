import os
import re
import unittest
from io import StringIO
from unittest import mock

from shared import _theme, theme
from shared._screen import key_name
from shared._theme import PALETTE, Bar, HelpModel, LogRecord, PromptView, Screen, Theme, detect_color, fit


class TtyStream(StringIO):
    def isatty(self):
        return True


class GlyphTest(unittest.TestCase):
    def setUp(self):
        self.theme = Theme(color="none", unicode=True)

    def test_namespaces(self):
        glyphs = self.theme.glyphs
        self.assertEqual((glyphs.bullet, glyphs.tick, glyphs.bang, glyphs.cross, glyphs.dot, glyphs.pointer),
                         ("•", "✓", "!", "✗", "·", ">"))
        self.assertEqual((glyphs.border, glyphs.keysep), ("┃", "•"))
        self.assertEqual((glyphs.select.yes, glyphs.select.no), ("●", "○"))
        self.assertEqual((glyphs.check.yes, glyphs.check.no), ("✓", "•"))
        self.assertEqual((glyphs.keys.up, glyphs.keys.down, glyphs.keys.left, glyphs.keys.right), ("↑", "↓", "←", "→"))
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
        self.assertEqual((glyphs.border, glyphs.keysep), ("|", "|"))
        self.assertEqual((glyphs.select.yes, glyphs.select.no, glyphs.check.yes, glyphs.check.no),
                         ("(*)", "( )", "[x]", "[ ]"))
        self.assertEqual((glyphs.keys.up, glyphs.keys.down, glyphs.keys.left, glyphs.keys.right),
                         ("up", "down", "left", "right"))
        self.assertEqual((glyphs.bar.full, glyphs.bar.empty, glyphs.spin), ("=", "-", ("|", "/", "-", "\\")))

    def test_assigned_glyph_wins_over_ascii(self):
        self.theme.glyphs.tick = "√"
        self.theme.glyphs.bar.full = "#"
        self.theme.unicode = False
        self.assertEqual((self.theme.glyphs.tick, self.theme.glyphs.bar.full), ("√", "#"))


class NoLeftoversTest(unittest.TestCase):
    def test_templates_and_branch_are_gone(self):
        for name in ("templates", "branch"):
            self.assertFalse(hasattr(theme, name), name)
        for name in ("default_templates", "tidy_spaces", "SLOT", "EMPTY_SLOT"):
            self.assertFalse(hasattr(_theme, name), name)


class PaletteTest(unittest.TestCase):
    def setUp(self):
        self.theme = Theme(color="16m", unicode=True, screen=Screen(dest=StringIO(), width=40, height=20))

    def test_defaults(self):
        palette = self.theme.palette
        self.assertEqual((palette.ok, palette.error, palette.cursor, palette.chosen), ("#02BF87", "#ED567A",
                                                                                      "#F780E2", "#02BF87"))
        self.assertEqual((palette.text, palette.muted, palette.faint, palette.key, palette.hint, palette.button),
                         (252, 243, 238, 241, 239, 237))
        self.assertEqual((palette.info, palette.warn, palette.debug, palette.label), ("cyan", "yellow", "dim", "magenta"))
        self.assertEqual((palette.title, palette.cream), ("bold #7571F9", "#FFFDF5"))

    def test_unknown_name_and_bad_value(self):
        with self.assertRaises(AttributeError):
            self.theme.palette.purple = "#ff00ff"
        with self.assertRaises(AttributeError):
            self.theme.palette.purple
        with self.assertRaises(ValueError):
            self.theme.palette.ok = "not-a-style"
        self.theme.palette.ok = "bold red"
        self.theme.palette.ok = "rgb(1, 2, 3)"
        self.theme.palette.ok = 141

    def test_paint(self):
        self.assertEqual(self.theme.paint("ok", "x"), "\x1b[38;2;2;191;135mx\x1b[0m")
        self.assertEqual(self.theme.paint("title", "x"), "\x1b[1;38;2;117;113;249mx\x1b[0m")
        self.assertEqual(self.theme.paint("cream", "x", on="cursor"), "\x1b[38;2;255;253;245;48;2;247;128;226mx\x1b[0m")
        self.assertEqual(self.theme.paint("text", "x", on="button"), "\x1b[38;5;252;48;5;237mx\x1b[0m")
        self.assertEqual(self.theme.paint("debug", ""), "")
        self.theme.color = "none"
        self.assertEqual(self.theme.paint("ok", "x"), "x")

    def test_a_palette_change_restyles_log_help_and_prompt(self):
        self.theme.palette.ok = "#010203"
        self.theme.palette.label = "#040506"
        self.theme.palette.title = "#070809"
        self.assertEqual(self.theme.draw("log", LogRecord("ok", "done")), "\x1b[38;2;1;2;3m✓\x1b[0m done")
        help_text = self.theme.draw("help", HelpModel("tool", "", "tool name"))
        self.assertIn("\x1b[38;2;4;5;6m  tool name\x1b[0m", help_text)
        self.assertIn("\x1b[38;2;7;8;9mUsage:\x1b[0m", help_text)
        frame = self.theme.draw("prompt", PromptView("input", "Name"))
        self.assertIn("\x1b[38;2;7;8;9mName\x1b[0m", frame)

    def test_depth_8_never_paints_black_or_yellow(self):
        # warn is yellow on purpose; cursor and button are the only backgrounds.
        self.theme.color = "8"
        painted = [self.theme.paint(name, "x") for name in PALETTE if name != "warn"]
        painted += [self.theme.paint("cream", "x", on="cursor"), self.theme.paint("text", "x", on="button")]
        for line in painted:
            codes = set(re.findall(r"\x1b\[([0-9;]*)m", line)[0].split(";"))
            with self.subTest(line=line):
                self.assertFalse({"30", "33", "40", "43"} & codes)
        self.assertEqual(self.theme.paint("muted", "x"), "\x1b[90mx\x1b[0m")
        self.assertEqual(self.theme.paint("text", "x", on="button"), "\x1b[37;100mx\x1b[0m")


class DrawerTest(unittest.TestCase):
    def setUp(self):
        self.theme = Theme(color="none", unicode=True)

    def test_log_lines(self):
        draw = lambda record: self.theme.draw("log", record)  # noqa: E731
        self.assertEqual(draw(LogRecord("info", "saved /tmp/a")), "• saved /tmp/a")
        self.assertEqual(draw(LogRecord("ok", "done")), "✓ done")
        self.assertEqual(draw(LogRecord("warn", "retry 2")), "! retry 2")
        self.assertEqual(draw(LogRecord("error", "failed: boom")), "✗ failed: boom")
        self.assertEqual(draw(LogRecord("debug", "raw")), "· raw")
        self.assertEqual(draw(LogRecord("ok", "")), "✓")

    def test_progress_rows(self):
        draw = lambda bar, level="info": self.theme.draw("progress", LogRecord(level, "", bar))  # noqa: E731
        running = Bar("clip.mp4", 3, 10, "part 3", False)
        self.assertEqual(draw(running), "• clip.mp4 ━━━━━━────────────── 3/10 part 3")
        self.assertEqual(draw(Bar("clip.mp4", 10, 10, "", True), "ok"), "✓ clip.mp4 ━━━━━━━━━━━━━━━━━━━━ 10/10")
        self.assertTrue(draw(running, "error").startswith("✗ clip.mp4"))
        self.assertIn("━" * 20, draw(Bar("a", 50, 10, "", False)))
        self.assertTrue(draw(Bar("a", 1024, 2048, "", False, show=lambda n: f"{n // 1024}K")).endswith("1K/2K"))

    def test_spinner_rows_collapse(self):
        draw = lambda bar, level="info": self.theme.draw("progress", LogRecord(level, "", bar))  # noqa: E731
        self.assertRegex(draw(Bar("scroll posts", 0, None, "", False)), r"^• scroll posts [⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]$")
        self.assertRegex(draw(Bar("scroll posts", 12, None, "", False)), r"^• scroll posts . 12$")
        self.assertEqual(draw(Bar("scroll posts", 12, None, "", True), "ok"), "✓ scroll posts 12")
        self.assertEqual(draw(Bar("scroll posts", 0, None, "", True), "ok"), "✓ scroll posts")

    def test_help(self):
        model = HelpModel("tool", "Does things.", "tool [options] name",
                          arguments=(("name", "who"),), options=(("--count N", ""), ("-h, --help", "")),
                          commands=(("go", "Go now"),), epilog="See ./docs/a.md")
        self.assertEqual(self.theme.draw("help", model), (
            "Does things.\n\nUsage:\n  tool [options] name\n\nArguments:\n  name                who\n\n"
            "Options:\n  --count N\n  -h, --help\n\nCommands:\n  go                  Go now\n\nSee ./docs/a.md\n"
        ))
        error = HelpModel("tool", "", "tool name", error="bad")
        self.assertEqual(self.theme.draw("help", error), "✗ bad\n\nUsage:\n  tool name\n")

    def test_help_colors_and_no_path_detection(self):
        self.theme.color = "16m"
        text = self.theme.draw("help", HelpModel("tool", "", "tool", options=(("--a A", "an a"),),
                                                 epilog="use ~/x/cookies.txt\n\nok"))
        self.assertIn("\x1b[35m  --a A             \x1b[0m  \x1b[38;5;243man a\x1b[0m", text)
        self.assertIn("\x1b[38;5;243muse ~/x/cookies.txt\x1b[0m\n\n\x1b[38;5;243mok\x1b[0m\n", text)
        self.assertNotIn("]8;;", text)

    def test_a_drawer_is_a_plain_function(self):
        self.theme.drawers.log = lambda current, record: f"[{record.level}] {record.message}"
        self.assertEqual(self.theme.draw("log", LogRecord("warn", "x")), "[warn] x")
        self.assertIsNot(theme.drawers.log, self.theme.drawers.log)
        with self.assertRaises(AttributeError):
            self.theme.drawers.table = print


class ColorTest(unittest.TestCase):
    def test_validation(self):
        other = Theme()
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


class AmbiguousWidthTest(unittest.TestCase):
    class Utf8(StringIO):
        encoding = "utf-8"

    def test_cjk_locales_get_ascii_glyphs(self):
        for variable, value in (("LANG", "ja_JP.UTF-8"), ("LANG", "ko_KR.UTF-8"), ("LC_ALL", "zh_CN.UTF-8")):
            with self.subTest(value), mock.patch.dict(os.environ, {variable: value}, clear=True):
                self.assertFalse(_theme.detect_unicode(self.Utf8()))
                picked = Theme(color="none")
                self.assertEqual(
                    [picked.glyphs.bullet, picked.glyphs.select.yes, picked.glyphs.select.no, picked.glyphs.tick,
                     picked.glyphs.cross, picked.glyphs.border],
                    ["*", "(*)", "( )", "+", "x", "|"])

    def test_lc_all_wins_over_lang(self):
        with mock.patch.dict(os.environ, {"LANG": "ja_JP.UTF-8", "LC_ALL": "en_US.UTF-8"}, clear=True):
            self.assertTrue(_theme.detect_unicode(self.Utf8()))

    def test_other_locales_keep_unicode(self):
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True):
            self.assertTrue(_theme.detect_unicode(self.Utf8()))

    def test_windows_legacy_console_gets_ascii(self):
        with mock.patch("os.name", "nt"):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertFalse(_theme.detect_unicode(self.Utf8()))
            with mock.patch.dict(os.environ, {"WT_SESSION": "1"}, clear=True):
                self.assertTrue(_theme.detect_unicode(self.Utf8()))


class ScopeTest(unittest.TestCase):
    def test_scope_restores(self):
        screen = Screen(dest=StringIO(), width=40, tty=False, interactive=False)
        before = (theme.color, theme.unicode, theme.screen)
        with theme.scope(color="8", unicode=False, screen=screen):
            self.assertEqual((theme.color, theme.unicode, theme.screen), ("8", False, screen))
        self.assertEqual((theme.color, theme.unicode, theme.screen), before)
        with self.assertRaises(RuntimeError), theme.scope(color="16m"):
            raise RuntimeError
        self.assertEqual(theme.color, before[0])
        with self.assertRaises(TypeError), theme.scope(glyphs=None):
            pass


class ScreenTest(unittest.TestCase):
    def test_fake_screen(self):
        screen = Screen(dest=StringIO(), inp=StringIO("a\r"), width=30, tty=False, interactive=True)
        self.assertEqual((screen.width, screen.tty, screen.interactive), (30, False, True))
        self.assertEqual((screen.readkey(), screen.readkey()), ("a", "enter"))
        with self.assertRaises(EOFError):
            screen.readkey()

    def test_write_strips_when_color_is_none(self):
        screen = Screen(dest=StringIO(), tty=False)
        with theme.scope(color="none"):
            screen.write("\x1b[31mred\x1b[0m")
        with theme.scope(color="16m"):
            screen.write("\x1b[31m!\x1b[0m")
        self.assertEqual(screen.dest.getvalue(), "red\x1b[31m!\x1b[0m")

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
