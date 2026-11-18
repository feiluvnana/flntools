"""LOIBUS on plugin API 2: signing, decrypting, targets, the VIP quota, info.md and archives."""

import asyncio
import base64
import datetime
import json
import unittest
from unittest import mock

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from flnd.errors import Blocked, NotFound, SiteError
from flnd.plugin import Range, Request
from flnd.plugins.loi import (
    API,
    SITE_IV,
    SITE_KEY,
    Loibus,
    Refusal,
    decode_token,
    decrypt_field,
    refusal,
    signature,
)
from flnd.testing import FakeCtx, Reply, check, collect, fake_clock
from flnkit import net as _net
from flnkit import vault as _vault


def encrypt(text):
    """`text` as the site sends a field: AES-128-CBC under its key and IV, PKCS7, in base64."""
    padder = PKCS7(128).padder()
    padded = padder.update(text.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(SITE_KEY), modes.CBC(SITE_IV)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


POST = {"id": 5930, "loi_title": "Night city: set", "loi_description": "line one\n\nline two",
        "category": {"id": 7, "name": "example_author"}, "date": "2026-11-09 12:00:00",
        "images": ["https://images.example/aaa", "https://images.example/bbb.png"],
        "file_size": "1.2 GB", "open_key": "pass-word", "tags": [{"id": 1, "name": "night"}],
        "pic_count": 2, "video_count": 0, "like_count": 34, "viewer_count": 100,
        "download_count": 5, "vip_requirement": 1}
MIRRORS = [{"sv_name": "VIP line 1", "sv_url": encrypt("https://one.example/loi-abc.7z")},
           {"sv_name": "VIP line 2", "sv_url": encrypt("https://two.example/loi-abc.7z")},
           {"sv_name": "VIP line 1", "sv_url": encrypt("https://one.example/extra.zip")}]
LINKS = {"status": "success", "data": {"sv_list": MIRRORS,
                                       "bdp": encrypt("https://pan.example/s/1"),
                                       "bdp_key": encrypt("abcd")}}
USER = {"data": {"id": 1, "vip_id": 1, "svip": 0}}
TOKEN = {"access-token": "Bearer%20tok"}
FOLDER = "example_author/5930_Night_city_set"
LINKS_PATH = "user/loi/download_v6/5930"
RATE = {"code": 429, "message": "too many requests"}
# Recorded from the API-1 source: every (path, history key) it queued, for a post with a slash,
# a colon and a question mark in its title and author, and odd image and archive URLs.
NASTY = {**POST, "loi_title": "Night/city: set?", "category": {"id": 7, "name": "ex/ample"},
         "images": ["https://images.example/aaa", "https://images.example/bbb.png",
                    "https://images.example/c.webp?x=1"]}
NASTY_LINKS = {"status": "success", "data": {"sv_list": [
    {"sv_name": "A", "sv_url": encrypt("https://one.example/loi%20abc.7z")},
    {"sv_name": "B", "sv_url": encrypt("https://two.example/loi%20abc.7z")},
    {"sv_name": "A", "sv_url": encrypt("https://one.example/extra.zip")},
    {"sv_name": "A", "sv_url": encrypt("https://one.example/")}], "bdp": "", "bdp_key": ""}}
NASTY_FOLDER = "ex_ample/5930_Night_city_set"
NASTY_IMAGES = [f"{NASTY_FOLDER}/images/{name}" for name in ("01.jpg", "02.png", "03.webp")]
NASTY_ARCHIVES = [f"{NASTY_FOLDER}/{name}" for name in ("loi_abc.7z", "extra.zip", "archive.zip")]
# More preview URLs and API 1's names for them: the extension as written, whatever its kind.
PREVIEWS = [(f"https://images.example/{path}", name) for path, name in (
    ("a.JPG", "01.JPG"), ("a.php", "02.php"), ("a.jfif", "03.jfif"), ("d.png", "04.png"),
    ("e", "05.jpg"), ("a.GIF", "06.GIF"))]


def fake(*, plugin=Loibus, cookies=TOKEN, state=None, post=POST, when=Range(), **changed):
    """A FakeCtx for post 5930, on `plugin` (a class, or an instance a test keeps); `changed`
    replaces API paths' answers (a value, a Reply, a function of the request)."""
    answers = {"lois/5930": {"status": "success", "data": post}, "user/info": USER,
               LINKS_PATH: LINKS, **changed}
    routes = {f"{API}{path}": answer if isinstance(answer, Reply) or callable(answer)
              else Reply(answer) for path, answer in answers.items()}
    return FakeCtx(plugin, "loi:5930", routes=routes, cookies=cookies, state=state, when=when)


def saved(categories=("all",), ctx=None, **given):
    """What a download of loi:5930 would save, on `ctx` or a `fake(**given)`."""
    return collect(Loibus, "loi:5930", ctx=ctx or fake(**given), categories=categories)


def paths(files):
    """The path of each file the site serves, leaving out the text files the plugin writes."""
    return [item.path for item in files if item.file.content is None]


def texts(files):
    return {item.path: item.file.content for item in files if item.file.content is not None}


def asked(ctx):
    """The API paths a run asked for, in order."""
    return [url.removeprefix(API) for _, url, _ in ctx.seen.requests]


def warned(ctx):
    return [text for level, text in ctx.seen.logs if level == "warn"]


async def listing(posts):
    return [post async for post in posts]


def archive_files(ctx, post="5930"):
    """The files a download of post `post`'s `archive` would save, on `ctx`."""
    return [item.file for item in collect(Loibus, f"loi:{post}", ctx=ctx,
                                          categories=["archive"])]


def today(ctx):
    return ctx.time.day().isoformat()


class SignTest(unittest.TestCase):
    def test_loix_vectors(self):
        """The web client's signature, byte for byte, pinned from before API 1's port."""
        signed = signature("GET", "https://api.moegoat.com/api/lois/5930?page=1",
                           now_ms=1_700_000_000_000, pick=lambda options: options[0],
                           noise=lambda size: "0123456789abcdef01")
        self.assertEqual(signed, {
            "x-security-challenge": "turnstile-v1",
            "x-request-id": "06056776-a901-4234-856d-789aebcdef50",
            "x-signature-timestamp": "1700000000000",
            "x-request-signature": _vault.digest(
                "get|lois/5930||1700000000000|loibus-sec-decoy-salt-v1")})
        self.assertNotEqual(signature("GET", "https://api.moegoat.com/api/x", now_ms=1),
                            signature("GET", "https://api.moegoat.com/api/x", now_ms=1))

    def test_what_sign_adds_by_host_and_token(self):
        site = {"referer": "https://loix.cc/", "origin": "https://loix.cc"}
        cases = [  # url, cookies, whether signed, the authorization
            ("https://api.moegoat.com/api/lois/1", TOKEN, True, "Bearer tok"),
            ("https://api.moegoat.com/api/lois/1", {}, True, None),
            ("https://img.moegoat.com/a.jpg", {"access-token": "tok"}, False, None),
        ]
        for url, cookies, signed, authorization in cases:
            with self.subTest(url=url, cookies=cookies), \
                    mock.patch("flnd.plugins.loi.time.time", return_value=1_700_000_000.0):
                request = Request(url)
                headers = dict(Loibus().sign(request, cookies).headers)
                self.assertIsNone(request.headers)
                self.assertEqual({name: headers[name] for name in site}, site)
                self.assertEqual(headers.get("authorization"), authorization)
                self.assertEqual("x-signature-timestamp" in headers, signed)
                if signed:
                    self.assertEqual(headers["x-signature-timestamp"], "1700000000000")


class FieldsTest(unittest.TestCase):
    def test_decrypt_field_and_decode_token(self):
        self.assertEqual(decrypt_field(encrypt("https://dl.example/a.zip")),
                         "https://dl.example/a.zip")
        self.assertEqual((decrypt_field(""), decrypt_field(None)), ("", ""))
        self.assertEqual([decode_token(raw) for raw in ("Bearer%20abc.def", "abc", "")],
                         ["abc.def", "abc", ""])
        with self.assertRaisesRegex(SiteError, "changed its key"):
            decrypt_field("bm90IGJsb2Nrcw==")


class RefusalTest(unittest.TestCase):
    def test_what_each_answer_means_and_which_blocks(self):
        """A rate limit in JSON holds a block round; a 429 is the engine's; a quota never holds."""
        cases = [  # status, body, refusal, blocked
            (429, None, Refusal.RATE, None),
            (429, {"code": 429, "message": "daily limit exceeded"}, Refusal.RATE, None),
            (200, {"code": "429", "message": "slow down"}, Refusal.RATE, 0.0),
            (200, {"status": "error", "message": "请求过于频繁"}, Refusal.RATE, 0.0),
            (200, {"status": "error", "message": "daily limit exceeded"}, Refusal.QUOTA, None),
            (200, {"code": 403, "msg": "今日下载次数已达上限"}, Refusal.QUOTA, None),
            (403, {"message": "download quota used up"}, Refusal.QUOTA, None),
            (403, {"message": "VIP only"}, None, None),
            (200, {"status": "success", "data": {"sv_list": [{"sv_url": "x"}]}}, None, None),
            (200, {"message": "rate it", "data": {"id": 1}}, Refusal.RATE, None),
            (500, None, None, None),
        ]
        for status, body, expected, hold in cases:
            with self.subTest(status=status, body=body):
                self.assertIs(refusal(status, body), expected)
                content = b"" if body is None else json.dumps(body).encode()
                response = _net.Response(status, f"{API}{LINKS_PATH}", {}, content)
                self.assertEqual(Loibus().blocked(response), hold)

    def test_only_the_links_answer_holds(self):
        """API 1's scope: a post's refusal that reads like a rate limit is no hold."""
        cases = [  # url, message, blocked
            (f"{API}{LINKS_PATH}", "请稍后再试", 0.0),
            (f"{API}lois/5930", "being moderated", None),
            (f"{API}lois/5930", "请稍后再试", None),
            (f"{API}user/info", "too many requests", None),
            ("https://loix.cc/user/loi/download_v6/5930", "too many requests", None),
        ]
        for url, message, hold in cases:
            with self.subTest(url=url, message=message):
                body = json.dumps({"status": "error", "message": message, "data": None})
                response = _net.Response(200, url, {}, body.encode())
                self.assertEqual(Loibus().blocked(response), hold)


# Every form loi reads, and its value; or None for text it doesn't read.
ROUTES = [
    ("loi:5930", "5930"), ("loi:https://www.loix.cc/lois/5930?ref=1", "5930"),
    ("https://loix.cc/lois/5930", "5930"), ("loix.cc/lois/5930", "5930"),
    ("loix.cc/lois/5930?x=1", "5930"), ("https://www.loix.cc/lois/5930/", "5930"),
    ("http://loix.cc/lois/5930#top", "5930"), ("https://m.loix.cc/lois/5930", "5930"),
    ("https://mobile.loix.cc/lois/5930", "5930"), ("https://loix.cc/lois/5930/lois/7", "5930"),
    ("https://moegoat.com/lois/5930", "5930"), ("https://www.moegoat.com/lois/5930/", "5930"),
    ("https://loix.cc/lois/0", "0"),
    ("loi:example_user", None), ("loi:abc", None), ("https://loix.cc/library/loier/7", None),
    ("https://loix.cc/lois/", None), ("https://loix.cc/lois/abc", None),
    ("https://loix.cc/", None), ("loix.cc/5930", None), ("https://example.com/lois/5930", None),
]


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, value in ROUTES:
            with self.subTest(text=text):
                found = Loibus.resolve(text)
                self.assertEqual(found and (found.text, found.kind),
                                 value and (f"loi:{value}", "post"))

    def test_the_class(self):
        check(Loibus)
        (post,) = Loibus.routes
        self.assertTrue(post.single)
        self.assertEqual([(c.name, c.own) for c in post.categories],
                         [("archive", True), ("photo", True)])
        self.assertEqual((Loibus.login.names, Loibus.login.required), (("access-token",), False))


class NamesTest(unittest.TestCase):
    def test_names_and_keys_are_those_of_api_1(self):
        """The key is the path, and both are API 1's, so no saved file downloads again."""
        cases = {("all",): NASTY_ARCHIVES + NASTY_IMAGES, ("photo",): NASTY_IMAGES,
                 ("archive",): NASTY_ARCHIVES}
        for categories, expected in cases.items():
            with self.subTest(categories=categories):
                files = saved(categories, post=NASTY, **{LINKS_PATH: NASTY_LINKS})
                self.assertEqual(paths(files), expected)
                self.assertEqual([item.key for item in files], [item.path for item in files])
                self.assertEqual(list(texts(files)), [f"{NASTY_FOLDER}/info.md"])
        files = saved(("photo",), post={**POST, "images": [url for url, _ in PREVIEWS]})
        self.assertEqual(paths(files), [f"{FOLDER}/images/{name}" for _, name in PREVIEWS])

    def test_a_post_without_an_author_is_misc(self):
        files = saved(("photo",), post={**POST, "category": None})
        self.assertEqual(list(texts(files)), ["misc/5930_Night_city_set/info.md"])


class RunTest(unittest.TestCase):
    def test_without_a_token_info_and_images_only(self):
        ctx = fake(cookies={})
        files = saved(ctx=ctx)
        self.assertEqual([item.path for item in files], [f"{FOLDER}/info.md",
                                                         f"{FOLDER}/images/01.jpg",
                                                         f"{FOLDER}/images/02.png"])
        self.assertEqual([item.url for item in files[1:]], POST["images"])
        self.assertEqual(asked(ctx), ["lois/5930"])
        self.assertEqual(warned(ctx), ["archives need a login; run flnd login loi"])
        info = texts(files)[f"{FOLDER}/info.md"]
        for line in ("# Night city: set", "`pass-word`", "> line one\n>\n> line two"):
            self.assertIn(line, info)
        self.assertNotIn("Download Sources", info)

    def test_archives_are_grouped_by_file_with_the_password(self):
        ctx = fake()
        files = saved(("archive",), ctx=ctx)
        self.assertEqual(paths(files), [f"{FOLDER}/loi-abc.7z", f"{FOLDER}/extra.zip"])
        self.assertEqual(files[1].url, ("https://one.example/loi-abc.7z",
                                        "https://two.example/loi-abc.7z"))
        self.assertEqual(files[1].file.password, "pass-word")
        self.assertEqual(asked(ctx), ["lois/5930", "user/info", LINKS_PATH])
        self.assertEqual(ctx.state["quota"], {"day": today(ctx), "posts": [5930]})
        info = texts(files)[f"{FOLDER}/info.md"]
        for line in ("- **Server [VIP line 2]**: [Download](https://two.example/loi-abc.7z)",
                     "| Media Count | pictures 2 · videos 0 |",
                     "[Link](https://pan.example/s/1) (Password: `abcd`)"):
            self.assertIn(line, info)

    def test_files_carry_the_sites_referer_and_origin(self):
        for item in saved():
            if item.file.content is None:
                self.assertEqual(dict(item.post.headers or {}),
                                 {"referer": "https://loix.cc/", "origin": "https://loix.cc"})

    def test_listing_asks_for_no_links_and_dates_outside_cost_nothing(self):
        plugin = Loibus()
        ctx = fake(plugin=plugin)
        (listed,) = asyncio.run(listing(plugin.archive(ctx, POST)))
        self.assertTrue(listed.lazy)
        self.assertEqual((asked(ctx), ctx.state), ([], {}))
        ctx = fake(when=Range(since=datetime.date(2026, 11, 10)))
        self.assertEqual(saved(ctx=ctx), [])
        self.assertEqual((asked(ctx), ctx.state), (["lois/5930"], {}))

    def test_a_missing_post_is_not_found_at_once(self):
        """Even one whose message reads like a rate limit: only the links answer holds."""
        for message in ("", "being moderated", "请稍后再试"):
            with self.subTest(message):
                ctx = fake(**{"lois/5930": {"status": "error", "message": message,
                                            "data": None}})
                with self.assertRaisesRegex(NotFound, "post 5930 isn't there"):
                    saved(ctx=ctx)
                self.assertEqual((asked(ctx), ctx.seen.holds), (["lois/5930"], []))

    def test_info_md_is_archives_when_it_is_wanted(self):
        """S8: `photo` writes info.md only without `archive`, so a re-run, whose archive post is
        saved already, never rewrites it without links."""
        plugin = Loibus()
        ctx = fake(plugin=plugin)

        async def photo_after(*categories):
            for name in categories:
                await listing(getattr(plugin, name)(ctx, POST))
            posts = await listing(plugin.photo(ctx, POST))
            return [file.name for post in posts for file in post.files]

        self.assertEqual(asyncio.run(photo_after()), ["info.md", "01.jpg", "02.png"])
        self.assertEqual(asyncio.run(photo_after("archive")), ["01.jpg", "02.png"])
        info = texts(saved(ctx=fake()))[f"{FOLDER}/info.md"]
        self.assertIn("Download Sources", info)


class QuotaTest(unittest.TestCase):
    def test_the_day_and_the_level_decide_the_links_request(self):
        day = "2026-01-01"  # the fake clock's UTC day
        cases = [  # name, state, user/info, files, asks for links, its warning
            ("used up", {"day": day, "posts": list(range(15))}, USER, 1, False,
             "the daily VIP quota is used up (15 of 15); archives skipped"),
            ("svip doubles", {"day": day, "posts": list(range(15))},
             {"data": {"vip_id": 1, "svip": 1}}, 3, True, None),
            ("vip 0 gets none", {}, {"data": {"vip_id": 0}}, 1, False,
             "the account has no VIP level, so no archives a day; archives skipped"),
            ("unknown level asks", {}, Reply(status=500), 3, True, None),
            # A refused info's body is no level, even one that reads like VIP 0.
            ("refused info asks", {}, Reply({"data": {"vip_id": 0}}, status=500), 3, True,
             None),
            ("yesterday forgotten", {"day": "2000-01-01", "posts": list(range(15))}, USER, 3,
             True, None),
            ("refused links", {}, USER, 1, True,
             "the site gives no download links for this post (VIP1 needed)"),
            # A VIP refusal by HTTP 403 is read (check=False), not taken for a logged-out account.
            ("refused links, 403", {}, USER, 1, True,
             "the site gives no download links for this post (VIP1 needed)"),
        ]
        for name, state, user, count, links, warning in cases:
            with self.subTest(name), fake_clock():
                changed = {"user/info": user}
                refused = {"status": "error", "message": "VIP only"}
                if name == "refused links":
                    changed[LINKS_PATH] = refused
                elif name == "refused links, 403":
                    changed[LINKS_PATH] = Reply(refused, status=403)
                ctx = fake(state={"quota": state} if state else {}, **changed)
                self.assertEqual(today(ctx), day)
                self.assertEqual(len(archive_files(ctx)), count)
                self.assertEqual(LINKS_PATH in asked(ctx), links)
                self.assertEqual(warned(ctx), [warning] if warning else [])
                self.assertEqual(5930 in ctx.state.get("quota", {}).get("posts", []), count > 1)

    def test_a_post_counted_today_is_free_and_counted_once(self):
        ctx = fake(state={"quota": {"day": "2026-01-01", "posts": [*range(14), 5930]}})
        self.assertEqual(len(archive_files(ctx)), 3)
        self.assertEqual(len(archive_files(ctx)), 3)
        self.assertEqual(len(ctx.state["quota"]["posts"]), 15)
        self.assertNotIn("user/info", asked(ctx))

    def test_the_level_is_read_once_per_run(self):
        ctx = fake(**{"user/info": USER, "lois/5931": {"data": {**POST, "id": 5931}},
                      "user/loi/download_v6/5931": LINKS})
        archive_files(ctx)
        archive_files(ctx, "5931")
        self.assertEqual(asked(ctx).count("user/info"), 1)
        self.assertEqual(ctx.state["quota"]["posts"], [5930, 5931])

    def test_the_quota_day_is_the_clocks_utc_day(self):
        # 23:30 on 2026-11-18 in UTC-5 is 04:30 on the 19th in UTC.
        at = datetime.datetime(2026, 11, 19, 4, 30, tzinfo=datetime.UTC).timestamp()
        with fake_clock() as clock:
            clock.advance(at - clock.wall)
            ctx = fake()
            archive_files(ctx)
        self.assertEqual(ctx.state["quota"]["day"], "2026-11-19")

    def test_a_daily_quota_answer_marks_the_day_used_up(self):
        used_up = {"status": "error", "code": 403, "message": "daily download limit exceeded"}
        ctx = fake(**{LINKS_PATH: used_up})
        self.assertEqual([item.name for item in archive_files(ctx)], ["info.md"])
        self.assertEqual(ctx.seen.holds, [])
        self.assertTrue(ctx.state["quota"]["exhausted"])
        self.assertIn("the daily VIP quota is used up; archives skipped", warned(ctx))
        ctx.seen.requests.clear()
        archive_files(ctx)
        self.assertEqual(asked(ctx), ["lois/5930"])


class RateTest(unittest.TestCase):
    def test_a_rate_answer_holds_and_the_retry_is_signed_again(self):
        answers = iter([RATE, LINKS])
        ctx = fake(**{LINKS_PATH: lambda request: Reply(next(answers))})
        clock = iter(range(1_700_000_000, 1_700_000_100))
        with mock.patch("flnd.plugins.loi.time.time", lambda: float(next(clock))):
            self.assertEqual(len(archive_files(ctx)), 3)
        self.assertEqual([seconds for _, seconds, _ in ctx.seen.holds], [30])
        tries = [options["headers"] for _, url, options in ctx.seen.requests
                 if url.endswith(LINKS_PATH)]
        self.assertEqual(len({sent["x-request-id"] for sent in tries}), 2)
        self.assertEqual(len({sent["x-signature-timestamp"] for sent in tries}), 2)
        self.assertNotIn("exhausted", ctx.state["quota"])

    def test_a_rate_limit_that_lasts_stops_the_target_uncounted(self):
        ctx = fake(**{LINKS_PATH: RATE})
        with self.assertRaises(Blocked):
            archive_files(ctx)
        self.assertEqual([seconds for _, seconds, _ in ctx.seen.holds], [30, 120, 480])
        self.assertNotIn("quota", ctx.state)


if __name__ == "__main__":
    unittest.main()
