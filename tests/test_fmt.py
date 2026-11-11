import unittest
from datetime import date, datetime

from shared import fmt


class SizeTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(fmt.size(0), "0.0 B")
        self.assertEqual(fmt.size(1023), "1023.0 B")
        self.assertEqual(fmt.size(1024), "1.0 KB")
        self.assertEqual(fmt.size(1536), "1.5 KB")
        self.assertEqual(fmt.size(1_048_576), "1.0 MB")
        self.assertEqual(fmt.size(1024 ** 3), "1.0 GB")
        self.assertEqual(fmt.size(1024 ** 4), "1.0 TB")
        self.assertEqual(fmt.size(2048 * 1024 ** 4), "2048.0 TB")

    def test_rounding_up_moves_to_next_unit(self):
        self.assertEqual(fmt.size(1024 * 1024 - 1), "1.0 MB")

    def test_old_name_is_gone(self):
        self.assertFalse(hasattr(fmt, "bytes"))


class DurationTest(unittest.TestCase):
    def test_milliseconds(self):
        self.assertEqual(fmt.duration(0), "0ms")
        self.assertEqual(fmt.duration(0.25), "250ms")
        self.assertEqual(fmt.duration(0.999), "999ms")

    def test_seconds(self):
        self.assertEqual(fmt.duration(1), "1.0s")
        self.assertEqual(fmt.duration(12.4), "12.4s")
        self.assertEqual(fmt.duration(59.9), "59.9s")

    def test_two_largest_units(self):
        self.assertEqual(fmt.duration(60), "1m")
        self.assertEqual(fmt.duration(90), "1m 30s")
        self.assertEqual(fmt.duration(3600), "1h")
        self.assertEqual(fmt.duration(3661), "1h 1m")
        self.assertEqual(fmt.duration(3601), "1h 1s")



class FilenameTest(unittest.TestCase):
    def test_each_replaced_character(self):
        for char in '\\/*?:"<>|\x00\x1f\x7f':
            self.assertEqual(fmt.filename(f"a{char}b"), "a_b", repr(char))

    def test_runs_collapse(self):
        self.assertEqual(fmt.filename("a  _ \t_b"), "a_b")
        self.assertEqual(fmt.filename("Summer: 2026 / day 1?"), "Summer_2026_day_1")

    def test_ends_are_stripped(self):
        self.assertEqual(fmt.filename(" ._name_. "), "name")

    def test_fallback(self):
        self.assertEqual(fmt.filename("..."), "untitled")
        self.assertEqual(fmt.filename("", fallback="x"), "x")

    def test_cut_never_splits_a_character(self):
        name = fmt.filename("é" * 10, maxlen=5)
        self.assertEqual(name, "éé")
        self.assertLessEqual(len(name.encode()), 5)
        self.assertEqual(fmt.filename("a" * 300), "a" * 200)

    def test_reserved_windows_names(self):
        self.assertEqual(fmt.filename("CON"), "CON_")
        self.assertEqual(fmt.filename("nul.txt"), "nul_.txt")
        self.assertEqual(fmt.filename("com1"), "com1_")
        self.assertEqual(fmt.filename("CONSOLE"), "CONSOLE")

    def test_old_sanitize_name_cases(self):
        self.assertEqual(fmt.filename("My:Album*Name? / 2026"), "My_Album_Name_2026")
        self.assertEqual(fmt.filename("   hello   world   "), "hello_world")


class DateTest(unittest.TestCase):
    def test_inputs(self):
        self.assertEqual(fmt.date(date(2026, 11, 11)), "2026-11-11")
        self.assertEqual(fmt.date(datetime(2026, 11, 11, 12, 8)), "2026-11-11 12:08")
        stamp = datetime(2026, 11, 11, 12, 8).timestamp()
        self.assertEqual(fmt.date(stamp), "2026-11-11 12:08")
        self.assertEqual(fmt.date(int(stamp)), "2026-11-11 12:08")

    def test_pattern(self):
        self.assertEqual(fmt.date(datetime(2026, 11, 11, 12, 8), "%Y-%m-%d"), "2026-11-11")
        self.assertEqual(fmt.date(date(2026, 11, 11), "%d/%m"), "11/11")

    def test_date_rejects_bool(self):
        with self.assertRaises(TypeError):
            fmt.date(True)  # pyright: ignore[reportArgumentType]


if __name__ == "__main__":
    unittest.main()
