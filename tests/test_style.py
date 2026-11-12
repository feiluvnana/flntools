import unittest

from shared import style, theme
from shared import _style
from shared._style import NAMED_COLORS


class StyleTest(unittest.TestCase):
    def paint(self, chain, text="x", depth="16m"):
        with theme.scope(color=depth):
            return chain(text)

    def test_every_named_property(self):
        for name, index in NAMED_COLORS.items():
            fg = 30 + index if index < 8 else 90 + index - 8
            self.assertEqual(self.paint(getattr(style, name)), f"\x1b[{fg}mx\x1b[0m", name)
            self.assertEqual(self.paint(getattr(style, "bg_" + name)), f"\x1b[{fg + 10}mx\x1b[0m", name)
        modifiers = {"bold": "1", "dim": "2", "italic": "3", "underline": "4", "reverse": "7", "strike": "9"}
        for name, code in modifiers.items():
            self.assertEqual(self.paint(getattr(style, name)), f"\x1b[{code}mx\x1b[0m", name)

    def test_unknown_property(self):
        with self.assertRaises(AttributeError):
            style.purple

    def test_removed_attributes(self):
        for name in ("double_underline", "curly_underline", "dotted_underline", "dashed_underline",
                     "underline_color", "overline", "blink", "hidden"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                getattr(style, name)
        for name in ("Styled", "Template", "pieces_of"):
            self.assertFalse(hasattr(_style, name), name)

    def test_chain_order_does_not_matter(self):
        self.assertEqual(style.red.bold, style.bold.red)
        self.assertEqual(self.paint(style.red.bold), self.paint(style.bold.red))

    def test_later_color_wins(self):
        self.assertEqual(style.red.green, style.green)
        self.assertEqual(style.red.color("#00ff00").fg, ("rgb", (0, 255, 0)))

    def test_chain_is_reusable(self):
        danger = style.red.bold
        self.assertEqual(self.paint(danger, "a"), "\x1b[1;31ma\x1b[0m")
        self.assertEqual(self.paint(danger, "b"), "\x1b[1;31mb\x1b[0m")
        self.assertEqual(danger, style.red.bold)

    def test_depth_8_uses_all_16_colors(self):
        self.assertEqual(self.paint(style.color("#ff0000"), depth="8"), "\x1b[91mx\x1b[0m")
        self.assertEqual(self.paint(style.color("#cd0000"), depth="8"), "\x1b[31mx\x1b[0m")
        self.assertEqual(self.paint(style.color("#7571F9"), depth="8"), "\x1b[94mx\x1b[0m")
        self.assertEqual(self.paint(style.color("#F780E2"), depth="8"), "\x1b[95mx\x1b[0m")
        self.assertEqual(self.paint(style.color("#ff0000", background=True), depth="8"), "\x1b[101mx\x1b[0m")

    def test_depth_8_maps_greys_by_lightness(self):
        greys = {232: "30", 237: "90", 238: "90", 241: "90", 243: "90", 252: "37", 255: "97",
                 "#000000": "30", "#808080": "90", "#c8c8c8": "37", "#FFFDF5": "97", 16: "30", 231: "97"}
        for value, code in greys.items():
            with self.subTest(value=value):
                self.assertEqual(self.paint(style.color(value), depth="8"), f"\x1b[{code}mx\x1b[0m")
        self.assertEqual(self.paint(style.color(237, background=True), depth="8"), "\x1b[100mx\x1b[0m")

    def test_color_parsing(self):
        self.assertEqual(style.color("#F25D94").fg, ("rgb", (242, 93, 148)))
        self.assertEqual(style.color("#f0a").fg, ("rgb", (255, 0, 170)))
        self.assertEqual(style.color("#F25D9480").fg, ("rgb", (242, 93, 148)))
        self.assertEqual(style.color("rgb(80, 250, 123)").fg, ("rgb", (80, 250, 123)))
        self.assertEqual(style.color("rgba(80, 250, 123, 0.5)").fg, ("rgb", (80, 250, 123)))
        self.assertEqual(style.color(141).fg, ("index", 141))
        self.assertEqual(style.color("#1a1b26", background=True).bg, ("rgb", (26, 27, 38)))

    def test_bad_colors_raise(self):
        for bad in ("#12", "red", "rgb(1, 2)", "rgb(1, 2, 300)", "rgba(1, 2, 3)", 256, -1, True, "#ggg"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                style.color(bad)

    def test_color_depths(self):
        pink = style.color("#F25D94")
        self.assertEqual(self.paint(pink, depth="none"), "x")
        self.assertEqual(self.paint(pink, depth="8"), "\x1b[35mx\x1b[0m")
        self.assertEqual(self.paint(pink, depth="256"), "\x1b[38;5;204mx\x1b[0m")
        self.assertEqual(self.paint(pink, depth="16m"), "\x1b[38;2;242;93;148mx\x1b[0m")
        self.assertEqual(self.paint(style.color(141), depth="16m"), "\x1b[38;5;141mx\x1b[0m")
        self.assertEqual(self.paint(style.color("#000000", background=True), depth="16m"),
                         "\x1b[48;2;0;0;0mx\x1b[0m")
        self.assertEqual(self.paint(style.red.bold, depth="none"), "x")

    def test_attributes_written_from_depth_8(self):
        self.assertEqual(self.paint(style.underline.italic, depth="8"), "\x1b[3;4mx\x1b[0m")

    def test_gradient_steps(self):
        painted = self.paint(style.gradient("#000000", "#ffffff"), "abc")
        self.assertEqual(painted, "\x1b[38;2;0;0;0ma\x1b[38;2;128;128;128mb\x1b[38;2;255;255;255mc\x1b[0m")

    def test_gradient_three_stops(self):
        painted = self.paint(style.gradient("#ff0000", "#00ff00", "#0000ff"), "abc")
        self.assertIn("38;2;255;0;0ma", painted)
        self.assertIn("38;2;0;255;0mb", painted)
        self.assertIn("38;2;0;0;255mc", painted)

    def test_gradient_skips_newlines(self):
        painted = self.paint(style.gradient("#000000", "#ffffff"), "a\nb")
        self.assertEqual(painted, "\x1b[38;2;0;0;0ma\x1b[0m\n\x1b[38;2;255;255;255mb\x1b[0m")

    def test_gradient_steps_per_grapheme(self):
        painted = self.paint(style.gradient("#000000", "#ffffff"), "éx")
        self.assertIn("\x1b[38;2;0;0;0mé", painted)

    def test_gradient_beats_flat_color_and_keeps_modifiers(self):
        painted = self.paint(style.red.bold.gradient("#000000", "#ffffff"), "ab")
        self.assertNotIn("31", painted.replace("38;2", ""))
        self.assertIn("\x1b[1;38;2;0;0;0ma", painted)

    def test_gradient_needs_two_stops(self):
        with self.assertRaises(ValueError):
            style.gradient("#000000")

    def test_background_gradient_with_foreground(self):
        painted = self.paint(style.gradient("#000000", "#ffffff", background=True).white, "ab")
        self.assertIn("\x1b[37;48;2;0;0;0ma", painted)

    def test_gradient_depth_8(self):
        painted = self.paint(style.gradient("#ff0000", "#0000ff"), "ab", depth="8")
        self.assertEqual(painted, "\x1b[91ma\x1b[34mb\x1b[0m")

    def test_link(self):
        link = style.link("https://example.com")
        self.assertEqual(self.paint(link, "docs"), "\x1b]8;;https://example.com\x1b\\docs\x1b]8;;\x1b\\")
        self.assertEqual(self.paint(link, "docs", depth="none"), "docs")
        colored = self.paint(style.cyan.underline.link("a/b"), "a/b")
        self.assertEqual(colored, "\x1b]8;;a/b\x1b\\\x1b[4;36ma/b\x1b[0m\x1b]8;;\x1b\\")

    def test_strip(self):
        with theme.scope(color="16m"):
            text = style.red("a") + style.link("u")("b")
        self.assertEqual(style.strip(text), "ab")

    def test_painted_text_is_a_plain_str(self):
        with theme.scope(color="16m"):
            danger = style.red("disk full")
        self.assertIs(type(danger), str)
        self.assertEqual(danger, "\x1b[31mdisk full\x1b[0m")

    def test_inner_reset_reopens_outer_style(self):
        with theme.scope(color="16m"):
            inner = style.red("in")
            outer = style.bold(f"a {inner} b")
        self.assertEqual(outer, "\x1b[1ma \x1b[31min\x1b[0m\x1b[1m b\x1b[0m")


if __name__ == "__main__":
    unittest.main()
