"""
Unit tests for platform source extraction and parsing.
"""

import tempfile
import unittest
from pathlib import Path
from flnd.sources.ins import InsSource
from flnd.sources.twt import TwtSource


class TestSourceParsing(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
