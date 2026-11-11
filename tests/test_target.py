"""
Unit tests for flnd target parsing using standard library unittest.
"""

import unittest
from flnd.target import parse_target, Target


class TestTargetParsing(unittest.TestCase):
    def test_valid_targets(self):
        t1 = parse_target("ins:example_user")
        self.assertEqual(t1.source, "ins")
        self.assertEqual(t1.value, "example_user")

        t2 = parse_target("tik:example_creator")
        self.assertEqual(t2.source, "tik")
        self.assertEqual(t2.value, "example_creator")

        t3 = parse_target("twt:example_handle")
        self.assertEqual(t3.source, "twt")
        self.assertEqual(t3.value, "example_handle")

        t4 = parse_target("fbk:example.page")
        self.assertEqual(t4.source, "fbk")
        self.assertEqual(t4.value, "example.page")

    def test_leading_at_stripped(self):
        t = parse_target("tik:@example_creator")
        self.assertEqual(t.source, "tik")
        self.assertEqual(t.value, "example_creator")

    def test_invalid_source(self):
        with self.assertRaisesRegex(ValueError, "^unknown source 'ytb'. Sources: tik, fbk, ins, twt$"):
            parse_target("ytb:nasa")

    def test_source_is_case_insensitive(self):
        self.assertEqual(parse_target(" INS:someone ").source, "ins")

    def test_missing_colon(self):
        with self.assertRaises(ValueError):
            parse_target("invalid_target")

    def test_empty_value(self):
        with self.assertRaises(ValueError):
            parse_target("ins:")


if __name__ == "__main__":
    unittest.main()
