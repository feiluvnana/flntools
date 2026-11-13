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
        self.assertEqual((palette.info, palette.warn, palette.debug, palette.label),
                         (243, "yellow", 242, "magenta"))
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
        self.assertEqual(self.theme.draw("log", LogRecord("ok", "done")), "\x1b[38;2;1;2;3m✓ done\x1b[0m")
        help_text = self.theme.draw("help", HelpModel("tool", "", "tool name"))
        self.assertIn("\x1b[38;2;4;5;6m  tool name\x1b[0m", help_text)
        self.assertIn("\x1b[38;2;7;8;9mUsage:\x1b[0m", help_text)
        frame = self.theme.draw("prompt", PromptView("input", "Name"))
        self.assertIn("\x1b[38;2;7;8;9mName\x1b[0m", frame)

    def test_depth_8_never_paints_black_or_yellow(self):
        # warn and wait are yellow on purpose; cursor and button are the only backgrounds.
        self.theme.color = "8"
        painted = [self.theme.paint(name, "x") for name in PALETTE if name not in ("warn", "wait")]
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
        # The bar's 20 cells come first, then the label, then the count with its "/" total, then the caption.
        self.assertEqual(draw(running), "• " + "━" * 6 + "─" * 14 + " clip.mp4 3/10 part 3")
        # A line that ended at ok shows the total alone, not n/n.
        self.assertEqual(draw(Bar("clip.mp4", 10, 10, "", True), "ok"), "✓ " + "━" * 20 + " clip.mp4 10")
        # A failed line keeps count/total and its caption, so how far it got stays on screen.
        self.assertEqual(draw(running, "error"), "✗ " + "━" * 6 + "─" * 14 + " clip.mp4 3/10 part 3")
        self.assertIn("━" * 20, draw(Bar("a", 50, 10, "", False)))
        self.assertEqual(draw(Bar("a", 1024, 2048, "", False, show=lambda n: f"{n // 1024}K")),
                         "• " + "━" * 10 + "─" * 10 + " a 1K/2K")

    def test_spinner_rows_collapse(self):
        draw = lambda bar, level="info": self.theme.draw("progress", LogRecord(level, "", bar))  # noqa: E731
        # Nothing counted: one short line, the spinner after the label. Counted: the frame sits in the bar column.
        self.assertRegex(draw(Bar("scroll posts", 0, None, "", False)), r"^• scroll posts [⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]$")
        self.assertRegex(draw(Bar("scroll posts", 12, None, "", False)), r"^• [⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] {19} scroll posts 12$")
        self.assertEqual(draw(Bar("scroll posts", 12, None, "", True), "ok"), "✓ " + "━" * 20 + " scroll posts 12")
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


@unittest.skipIf(os.name == "nt", "reads a POSIX descriptor")
class ReadKeyPosixTest(unittest.TestCase):
    def read(self, data: bytes, count: int = 1) -> list[str]:
        """Feed `data` through a pipe (kept open) and read `count` keys."""
        from shared._screen import _readkey_posix

        r, w = os.pipe()
        try:
            os.write(w, data)
            return [_readkey_posix(r, raw=True) for _ in range(count)]
        finally:
            os.close(r)
            os.close(w)

    def test_ss3_arrows_are_one_key(self):
        self.assertEqual(self.read(b"\x1bOA\x1bOB\x1bOC\x1bOD\x1bOH\x1bOF", 6), ["up", "down", "right", "left", "home", "end"])

    def test_csi_arrow_still_one_key(self):
        self.assertEqual(self.read(b"\x1b[A"), ["up"])

    def test_lone_escape_and_alt_o(self):
        self.assertEqual(self.read(b"\x1b"), ["escape"])
        self.assertEqual(self.read(b"\x1bO"), ["\x1bO"])


class WindowsVtHandleTest(unittest.TestCase):
    def enable(self, dest):
        import ctypes
        import sys

        from shared._screen import _enable_windows_vt

        windll = mock.MagicMock()
        windll.kernel32.GetConsoleMode.return_value = 1
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(ctypes, "windll", windll, create=True):
            _enable_windows_vt(dest)
        return windll.kernel32

    def test_stdout_uses_handle_minus_11(self):
        import sys

        kernel32 = self.enable(sys.stdout)
        kernel32.GetStdHandle.assert_called_once_with(-11)
        kernel32.SetConsoleMode.assert_called_once()

    def test_stderr_uses_handle_minus_12(self):
        import sys

        kernel32 = self.enable(sys.stderr)
        kernel32.GetStdHandle.assert_called_once_with(-12)
        kernel32.SetConsoleMode.assert_called_once()

    def test_other_streams_are_skipped(self):
        kernel32 = self.enable(StringIO())
        kernel32.GetStdHandle.assert_not_called()
        kernel32.SetConsoleMode.assert_not_called()


def painter(depth: str, *, unicode: bool = True) -> Theme:
    return Theme(color=depth, unicode=unicode, screen=Screen(dest=StringIO()))


def codes(line: str) -> str:
    """The SGR parameters that open a painted line."""
    return line[2:line.index("m")]


class RoleDepthTest(unittest.TestCase):
    """The roles flnd names, pinned at each color depth: plain, flat 16 colors, 256 and 24-bit."""

    ROLES = ("accent", "glow", "target", "category", "size", "path", "skip", "link", "download",
             "shrink", "wait", "debug", "info")

    def paints(self, depth: str) -> dict:
        return {role: painter(depth).paint(role, "x") for role in self.ROLES}

    def test_none_is_plain(self):
        self.assertEqual(set(self.paints("none").values()), {"x"})

    def test_8_is_flat_ansi(self):
        self.assertEqual({role: codes(line) for role, line in self.paints("8").items()},
                         {"accent": "94", "glow": "94", "target": "1;37", "category": "94",
                          "size": "90", "path": "90", "skip": "90", "link": "94", "download": "36",
                          "shrink": "35", "wait": "33", "debug": "90", "info": "90"})

    def test_256(self):
        found = {role: codes(line) for role, line in self.paints("256").items()}
        self.assertEqual((found["accent"], found["glow"]), ("38;5;104", "38;5;147"))
        self.assertEqual((found["target"], found["size"], found["skip"]),
                         ("1;38;5;252", "38;5;245", "38;5;241"))
        self.assertEqual((found["download"], found["shrink"], found["wait"]),
                         ("38;5;74", "38;5;133", "38;5;179"))

    def test_16m(self):
        found = {role: codes(line) for role, line in self.paints("16m").items()}
        self.assertEqual(found["accent"], "38;2;125;132;214")
        self.assertEqual(found["link"], "38;2;111;163;216")
        self.assertEqual(found["wait"], "38;2;215;182;95")


class GradientTest(unittest.TestCase):
    def test_flat_or_plain_without_motion(self):
        self.assertFalse(painter("8").motion)
        self.assertEqual(painter("none").gradient("━━━", phase=4), "━━━")
        self.assertEqual(painter("8").gradient("━━━", phase=4), "\x1b[94m━━━\x1b[0m")

    def test_16m_drifts_between_two_shades(self):
        shades = painter("16m")
        self.assertTrue(shades.motion)
        still = shades.gradient("━" * 4)
        self.assertEqual(still, "\x1b[38;2;125;132;214m━\x1b[38;2;126;133;215m━"
                                "\x1b[38;2;129;136;216m━\x1b[38;2;134;141;219m━\x1b[0m")
        moved = shades.gradient("━" * 4, phase=2)
        self.assertNotEqual(moved, still)
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", moved), "━" * 4)
        # The wave goes from the accent to the glow and back, never past either.
        reds = [int(red) for red in re.findall(r"38;2;(\d+);", shades.gradient("━" * 20))]
        self.assertEqual((min(reds), max(reds)), (125, 169))

    def test_256_is_a_short_ramp_with_runs_merged(self):
        cells = painter("256").gradient("━" * 20)
        self.assertLessEqual(len(set(re.findall(r"38;5;(\d+)", cells))), 4)
        self.assertIn("38;5;104", cells)
        self.assertLess(cells.count("\x1b["), 20)


class MotionDrawTest(unittest.TestCase):
    def draw(self, depth: str, bar: Bar, level: str = "info") -> str:
        return painter(depth).draw("progress", LogRecord(level, "", bar))

    def test_a_running_bar_moves_with_its_tick(self):
        bar = Bar("posts/a.jpg", 10, 20, "", False, tick=0)
        moved = Bar("posts/a.jpg", 10, 20, "", False, tick=_theme.DRIFT)
        self.assertNotEqual(self.draw("16m", bar), self.draw("16m", moved))
        self.assertEqual(self.draw("8", bar), self.draw("8", moved))

    def test_ended_and_waiting_bars_are_one_flat_color(self):
        ended = Bar("posts/a.jpg", 20, 20, "", True, tick=0)
        later = Bar("posts/a.jpg", 20, 20, "", True, tick=7)
        self.assertEqual(self.draw("16m", ended), self.draw("16m", later))
        self.assertIn("\x1b[38;2;2;191;135m" + "━" * 20, self.draw("16m", ended, "ok"))
        failed = Bar("posts/a.jpg", 5, 20, "timed out", False, tick=0)
        self.assertIn("\x1b[38;2;237;86;122m" + "━" * 5, self.draw("16m", failed, "error"))
        waiting = Bar("ins: waiting", 5, 20, "", False, tick=0, wait=True)
        self.assertIn("\x1b[38;2;215;182;95m" + "━" * 5, self.draw("16m", waiting))
        still = Bar("ins: waiting", 5, 20, "", False, tick=9, wait=True)
        self.assertEqual(self.draw("16m", waiting), self.draw("16m", still))

    def test_a_path_label_has_a_dim_folder_and_a_normal_name(self):
        line = self.draw("256", Bar("posts/a.jpg", 1, 2, "", False, tick=0))
        folder, name = "\x1b[38;5;245mposts/\x1b[0m", "\x1b[38;5;252ma.jpg\x1b[0m"
        self.assertIn(f"{folder}{name} \x1b[38;5;245m1/2\x1b[0m", line)

    def test_the_spinner_steps_through_shades(self):
        frames = {self.draw("16m", Bar("scroll", 0, None, "", False, tick=tick))
                  for tick in range(10)}
        self.assertEqual(len(frames), 10)
        self.assertEqual(self.draw("8", Bar("scroll", 0, None, "", False, tick=3)),
                         "\x1b[94m•\x1b[0m \x1b[37mscroll\x1b[0m \x1b[94m⠸\x1b[0m")


class EventLineTest(unittest.TestCase):
    def test_an_event_takes_its_glyph_and_color(self):
        shades = painter("256")
        self.assertEqual(shades.draw("log", LogRecord("debug", "a → b", kind="skip")),
                         "\x1b[38;5;241m↷ a → b\x1b[0m")
        self.assertEqual(shades.draw("log", LogRecord("debug", "a → b", kind="link")),
                         "\x1b[38;5;74m⤷ a → b\x1b[0m")
        shades.unicode = False
        self.assertEqual(shades.draw("log", LogRecord("debug", "a", kind="shrink")),
                         "\x1b[38;5;133m~ a\x1b[0m")


class StatusDrawTest(unittest.TestCase):
    COUNTS = (theme.Count(140, "found", "text", "found"), theme.Count(9, "saved", "ok", "tick"),
              theme.Count(0, "failed", "error", "cross"))
    RUNNING = (theme.Count(4, "downloading", "download", "download"),
               theme.Count(0, "shrinking", "shrink", "shrink"))

    def draw(self, status, depth: str = "none", unicode: bool = True) -> str:
        return painter(depth, unicode=unicode).draw("status", status)

    def status(self, **changes):
        fields = dict(subject_width=18, label_width=7, elapsed=72, tick=2)
        fields.update(changes)
        return theme.Status("fbk:example.page", "photos", self.COUNTS, self.RUNNING, ("3.4 MB",),
                            **fields)

    def test_running_has_a_second_line_without_zeros(self):
        self.assertEqual(self.draw(self.status()).split("\n"), [
            "⠹ fbk:example.page   photos   140 found    9 saved    0 failed 3.4 MB 1m 12s",
            "                             4 downloading"])

    def test_ended_keeps_its_first_line_and_a_still_glyph(self):
        self.assertEqual(self.draw(self.status(outcome="ok")), "✓ fbk:example.page   photos "
                         "  140 found    9 saved    0 failed 3.4 MB 1m 12s")
        self.assertTrue(self.draw(self.status(outcome="error")).startswith("✗ "))

    def test_a_wait_shows_on_the_second_line(self):
        line = self.draw(self.status(note="waiting 4m 10s: blocked")).split("\n")[1]
        self.assertEqual(line.strip(), "4 downloading  waiting 4m 10s: blocked")

    def test_narrow_drops_the_words(self):
        self.assertEqual(self.draw(self.status(width=60)).split("\n"), [
            "⠹ fbk:example.page   photos  ◦140 ✓9 ✗0 3.4 MB 1m 12s",
            "                             ⇣4"])
        self.assertEqual(self.draw(self.status(width=60), unicode=False).split("\n")[0],
                         "- fbk:example.page   photos  #140 +9 x0 3.4 MB 1m 12s")

    def test_counts_are_colored_only_above_zero(self):
        line = self.draw(self.status(outcome="ok"), depth="256")
        self.assertIn("\x1b[38;5;36m   9\x1b[0m \x1b[38;5;243msaved", line)
        self.assertIn("\x1b[38;5;243m   0\x1b[0m \x1b[38;5;243mfailed", line)
        self.assertIn("\x1b[1;38;5;252mfbk:example.page  \x1b[0m", line)

    def test_seconds_then_minutes(self):
        self.assertTrue(self.draw(self.status(elapsed=12.9, outcome="ok")).endswith("3.4 MB 12s"))
        self.assertTrue(self.draw(self.status(elapsed=None, outcome="ok")).endswith("3.4 MB"))
