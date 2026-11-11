import unittest
from datetime import date, datetime

from shared import fmt


class BytesTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(fmt.bytes(0), "0.0 B")
        self.assertEqual(fmt.bytes(1023), "1023.0 B")
        self.assertEqual(fmt.bytes(1024), "1.0 KB")
        self.assertEqual(fmt.bytes(1536), "1.5 KB")
        self.assertEqual(fmt.bytes(1_048_576), "1.0 MB")
        self.assertEqual(fmt.bytes(1024 ** 3), "1.0 GB")
        self.assertEqual(fmt.bytes(1024 ** 4), "1.0 TB")
        self.assertEqual(fmt.bytes(2048 * 1024 ** 4), "2048.0 TB")

    def test_rounding_up_moves_to_next_unit(self):
        self.assertEqual(fmt.bytes(1024 * 1024 - 1), "1.0 MB")


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
