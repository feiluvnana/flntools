"""
Unit tests for platform source extraction and parsing.
"""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flnd import cli as flnd_cli
from flnd.cookies import store_cookies
from flnd.sources import SOURCES, get_source, source_for_domain
from flnd.sources.base import BaseSource
from flnd.sources.ins import InsSource
from flnd.sources.twt import TwtSource
from flnd.target import parse_target
from shared import theme
from shared._theme import Screen


class AbcSource(BaseSource):
    code = "abc"
    name = "Abc Site"
    domains = ("abc.example",)

    def download(self, target, download_dir, limit=None, headless=True):
        return 0


class TestRegistry(unittest.TestCase):
    def test_every_source_describes_itself(self):
        for code, source in SOURCES.items():
            self.assertEqual(source.code, code)
            self.assertTrue(isinstance(source.name, str) and source.name)
            self.assertTrue(isinstance(source.domains, tuple) and source.domains, code)

    def test_codes_are_unique(self):
        codes = [source.code for source in SOURCES.values()]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(list(SOURCES), ["tik", "fbk", "ins", "twt"])

    def test_no_domain_belongs_to_two_sources(self):
        owners = {}
        for code, source in SOURCES.items():
            for domain in source.domains:
                self.assertNotIn(domain, owners, f"{domain} belongs to {owners.get(domain)} and {code}")
                owners[domain] = code

    def test_source_for_domain(self):
        self.assertEqual(source_for_domain("instagram.com"), "ins")
        self.assertEqual(source_for_domain(".instagram.com"), "ins")
        self.assertEqual(source_for_domain("www.Instagram.COM"), "ins")
        self.assertEqual(source_for_domain(".twitter.com"), "twt")
        self.assertEqual(source_for_domain("x.com"), "twt")
        self.assertEqual(source_for_domain("m.facebook.com"), "fbk")
        self.assertIsNone(source_for_domain("notinstagram.com"))
        self.assertIsNone(source_for_domain("instagram.com.evil.example"))
        self.assertIsNone(source_for_domain(""))

    def test_unknown_code_has_one_message(self):
        message = "unknown source 'abc'. Sources: tik, fbk, ins, twt"
        with self.assertRaisesRegex(ValueError, f"^{message}$"):
            get_source("abc")
        with self.assertRaisesRegex(ValueError, f"^{message}$"):
            parse_target("abc:someone")
        self.assertIsInstance(get_source("INS"), InsSource)

    def test_an_added_source_shows_up_everywhere(self):
        screen = Screen(out=io.StringIO(), inp=io.StringIO(""), width=200, tty=False, interactive=True)
        with mock.patch.dict(SOURCES, {"abc": AbcSource}), tempfile.TemporaryDirectory() as tmp, \
                theme.scope(screen=screen, color="none"):
            self.assertEqual(parse_target("abc:someone").source, "abc")
            self.assertIsInstance(get_source("abc"), AbcSource)
            self.assertIn("Sources: tik, fbk, ins, twt, abc", flnd_cli.epilog())
            self.assertTrue(flnd_cli.describe().endswith("Twitter/X, and Abc Site."))

            # Source: the fifth option; value; Add another? No.
            screen.readkey = iter(["down"] * 4 + ["enter", "x", "enter", "enter"]).__next__
            self.assertEqual(flnd_cli.ask_targets(), ["abc:x"])
            self.assertIn("✓ Source: Abc Site", screen.out.getvalue())

            previous = os.getcwd()
            os.chdir(tmp)
            try:
                saved = store_cookies(".abc.example\tTRUE\t/\tFALSE\t0\tsid\t1\n")
            finally:
                os.chdir(previous)
            self.assertEqual(saved["abc"][0], 1)


class TestSourceParsing(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=io.StringIO(), inp=io.StringIO(""), width=200, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def test_ins_reposts_extraction(self):
        src = InsSource()
        mock_data = {
            "data": {
                "fetch__XDTUserDict": {
                    "user_reposts_timeline": {
                        "repost_grid_items": [
                            {
                                "media": {
                                    "code": "TEST_REPOST_CODE",
                                    "pk": "123456789",
                                    "taken_at": 1700000000,
                                    "image_versions2": {
                                        "candidates": [{"url": "https://example.com/repost_img.jpg"}]
                                    },
                                }
                            }
                        ]
                    }
                }
            }
        }
        extracted = src._extract_media_from_json(mock_data, tab="reposts")
        self.assertEqual(len(extracted), 1)
        self.assertEqual(extracted[0]["id"], "TEST_REPOST_CODE")
        self.assertIn("https://example.com/repost_img.jpg", extracted[0]["media_urls"])

    def test_twt_timeline_parsing(self):
        src = TwtSource()
        mock_data = {
            "data": {
                "user": {
                    "result": {
                        "timeline_v2": {
                            "timeline": {
                                "instructions": [
                                    {
                                        "entries": [
                                            {
                                                "entryId": "tweet-111",
                                                "content": {
                                                    "itemContent": {
                                                        "tweet_results": {
                                                            "result": {
                                                                "rest_id": "111222333",
                                                                "legacy": {
                                                                    "created_at": "Mon Nov 09 10:00:00 +0000 2026",
                                                                    "extended_entities": {
                                                                        "media": [
                                                                            {
                                                                                "type": "photo",
                                                                                "media_url_https": "https://pbs.twimg.com/media/test.jpg",
                                                                            }
                                                                        ]
                                                                    },
                                                                },
                                                            }
                                                        }
                                                    }
                                                },
                                            },
                                            {
                                                "entryId": "cursor-bottom-123",
                                                "content": {
                                                    "cursorType": "Bottom",
                                                    "value": "CURSOR_XYZ",
                                                },
                                            },
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }
        items, cursor = src._parse_timeline_entries(mock_data)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tweet_id"], "111222333")
        self.assertEqual(items[0]["kind"], "image")
        self.assertEqual(cursor, "CURSOR_XYZ")

    def test_twt_save_media_item_dest_and_dedup(self):
        src = TwtSource()
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_dir = Path(tmp_dir) / "twt" / "testuser"
            (target_dir / "posts").mkdir(parents=True)
            (target_dir / "images").mkdir(parents=True)

            # Pre-create a file in posts
            existing_file = target_dir / "posts" / "2026-11-09_999_0.jpg"
            existing_file.write_bytes(b"content")

            # Try saving same tweet_id to media category (images) -> should deduplicate and return False
            item = {
                "category": "media",
                "kind": "image",
                "tweet_id": "999",
                "date": "2026-11-09",
                "idx": 0,
                "url": "http://fake.url/img.jpg",
            }
            saved = src._save_media_item(item, target_dir, client=None)
            self.assertFalse(saved)
            self.assertEqual(self.screen.out.getvalue(), "• skip images/2026-11-09_999_0.jpg\n")


if __name__ == "__main__":
    unittest.main()
