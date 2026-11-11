"""
Unit tests for platform source extraction and parsing.
"""

import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import httpx

from flnd import cli as flnd_cli
from flnd.cookies import store_cookies
from flnd.sources import SOURCES, get_source, source_for_domain
from flnd.sources.base import BaseSource
from flnd.sources.ins import INSTAGRAM_MOBILE_UA, InsSource
from flnd.sources.tik import TikSource
from flnd.sources.twt import TwtSource
from flnd.target import parse_target
from shared import _http, http, store, theme
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


def day(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def tweet(tweet_id, media):
    return {"tweet_results": {"result": {"rest_id": tweet_id, "legacy": {
        "created_at": "Mon Nov 09 10:00:00 +0000 2026", "extended_entities": {"media": media}}}}}


def timeline(entries):
    return {"data": {"user": {"result": {"timeline_v2": {"timeline": {"instructions": [{"entries": entries}]}}}}}}


class ApiPathTest(unittest.TestCase):
    """Each source's API path, end to end, against httpx.MockTransport. No browser, no network."""

    def setUp(self):
        self.screen = Screen(out=io.StringIO(), inp=io.StringIO(""), width=200, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.downloads = self.root / "downloads"
        self.requests = []
        real_client = _http.Client

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(self.route), **kwargs)

        patcher = mock.patch.object(_http, "Client", client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def route(self, request):
        self.requests.append(request)
        return self.respond(request)

    def cookies(self, module, **values):
        cookies = [{"name": name, "value": value, "domain": ".site.example", "path": "/"}
                   for name, value in values.items()]
        patcher = mock.patch(f"flnd.sources.{module}.load_cookies", return_value=cookies)
        patcher.start()
        self.addCleanup(patcher.stop)

    def history(self):
        with store.open(self.root / "state.db") as db:
            return sorted(db.table("downloads"))

    def media_requests(self, host):
        return [request for request in self.requests if request.url.host == host]

    def test_ins_stories_from_the_mobile_api(self):
        self.cookies("ins", sessionid="s", ds_user_id="42")
        reels = {"reels_media": [{"items": [
            {"id": "301_42", "taken_at": 1700000000, "media_type": 2,
             "video_versions": [{"url": "https://cdn.example/s1.mp4"}]},
            {"pk": "302", "taken_at": 1700000000, "media_type": 1,
             "image_versions2": {"candidates": [{"url": "https://cdn.example/s2.jpg"}]}},
        ]}]}

        def respond(request):
            if request.url.host == "i.instagram.com":
                return httpx.Response(200, json=reels)
            return httpx.Response(200, content=request.url.path.encode())

        self.respond = respond
        with mock.patch.object(InsSource, "_download_via_browser") as browse:
            count = InsSource().download(parse_target("ins:someone"), self.downloads, headless=True)
        self.assertEqual(count, 2)
        browse.assert_called_once()
        stories = self.downloads / "ins" / "someone" / "stories"
        self.assertEqual((stories / f"{day(1700000000)}_301.mp4").read_bytes(), b"/s1.mp4")
        self.assertEqual((stories / f"{day(1700000000)}_302.jpg").read_bytes(), b"/s2.jpg")
        api = self.requests[0]
        self.assertEqual(api.url.path, "/api/v1/feed/reels_media/")
        self.assertEqual(api.url.params["user_ids"], '["42"]')
        self.assertEqual(api.headers["user-agent"], INSTAGRAM_MOBILE_UA)
        self.assertEqual(api.headers["cookie"], "sessionid=s; ds_user_id=42")
        self.assertEqual(api.headers["x-ig-app-id"], "936619743392459")
        self.assertEqual({r.headers["user-agent"] for r in self.media_requests("cdn.example")}, {http.agents.chrome})
        self.assertEqual(self.history(), ["ins:301", "ins:302"])

    def test_tik_single_video_from_the_embed_page(self):
        self.cookies("tik", sessionid="s", tt_chain_token="t")
        video_id = "7234567890123456789"
        next_data = {"props": {"pageProps": {"itemStruct": {
            "id": video_id, "createTime": 1700000000, "video": {"playAddr": "https://v.example/clip.mp4"}}}}}
        page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'

        def respond(request):
            if request.url.host == "www.tiktok.com":
                return httpx.Response(200, html=page)
            return httpx.Response(200, content=b"clip")

        self.respond = respond
        count = TikSource().download(parse_target(f"tik:{video_id}"), self.downloads, headless=True)
        self.assertEqual(count, 1)
        clip = self.downloads / "tik" / video_id / "videos" / f"{day(1700000000)}_{video_id}.mp4"
        self.assertEqual(clip.read_bytes(), b"clip")
        embed, media = self.requests
        self.assertEqual(str(embed.url), f"https://www.tiktok.com/embed/v2/{video_id}")
        self.assertEqual(embed.headers["user-agent"], http.agents.chrome)
        self.assertEqual(media.headers["referer"], "https://www.tiktok.com/")
        self.assertEqual(media.headers["cookie"], "tt_chain_token=t; sessionid=s")
        self.assertEqual(self.history(), [f"tik:{video_id}"])

    def test_twt_user_timelines_from_the_graphql_api(self):
        self.cookies("twt", auth_token="a", ct0="c")
        photo = {"type": "photo", "media_url_https": "https://pbs.example/p.jpg"}
        video = {"type": "video", "video_info": {"variants": [
            {"content_type": "video/mp4", "bitrate": 1, "url": "https://pbs.example/low.mp4"},
            {"content_type": "video/mp4", "bitrate": 9, "url": "https://pbs.example/high.mp4"},
        ]}}
        pages = {
            "UserByScreenName": {"data": {"user": {"result": {"rest_id": "42"}}}},
            "UserOriginalsTimeline": timeline([
                {"entryId": "tweet-111", "content": {"itemContent": tweet("111", [photo])}},
                {"entryId": "cursor-bottom-0", "content": {"cursorType": "Bottom", "value": "0|end"}},
            ]),
            # The media tab lists the same photo again, which must not be downloaded twice.
            "UserMedia": timeline([
                {"entryId": "grid", "content": {"items": [
                    {"item": {"itemContent": tweet("111", [photo])}},
                    {"item": {"itemContent": tweet("222", [video])}},
                ]}},
            ]),
        }

        def respond(request):
            if request.url.host == "x.com":
                return httpx.Response(200, json=pages[request.url.path.rsplit("/", 1)[-1]])
            return httpx.Response(200, content=request.url.path.encode())

        self.respond = respond
        count = TwtSource().download(parse_target("twt:someone"), self.downloads, headless=True)
        self.assertEqual(count, 2)
        folder = self.downloads / "twt" / "someone"
        self.assertEqual((folder / "posts" / "2026-11-09_111_0.jpg").read_bytes(), b"/p.jpg")
        self.assertEqual((folder / "videos" / "2026-11-09_222_0.mp4").read_bytes(), b"/high.mp4")
        self.assertEqual(list((folder / "images").iterdir()), [])
        self.assertIn("• skip images/2026-11-09_111_0.jpg", self.screen.out.getvalue().splitlines())
        api = [r.url.path.rsplit("/", 1)[-1] for r in self.requests if r.url.host == "x.com"]
        self.assertEqual(api, ["UserByScreenName", "UserOriginalsTimeline", "UserMedia"])
        variables = json.loads(self.requests[1].url.params["variables"])
        self.assertEqual(variables["userId"], "42")
        self.assertTrue(variables["withQuickPromoteEligibilityTweetFields"])
        media = self.media_requests("pbs.example")
        self.assertEqual(sorted(str(r.url) for r in media),
                         ["https://pbs.example/high.mp4", "https://pbs.example/p.jpg?name=orig"])
        # Media requests carry the API client's session headers, as they always have.
        self.assertEqual({r.headers["x-csrf-token"] for r in media}, {"c"})
        self.assertEqual(self.history(), ["twt:111_0", "twt:222_0"])


if __name__ == "__main__":
    unittest.main()
