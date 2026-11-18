"""LOIBUS: signing, decrypting, targets, the VIP quota, and a post's info, images and archives."""

import base64
import datetime
import itertools
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from fakes import FakeSite, Seen, collect
from test_engine import EngineCase

from flnd.engine import home as _home
from flnd.engine.core import resolve
from flnd.engine.options import Options
from flnd.source import Blocked, Request, SiteError, Target, UsageError
from flnd.sources.loi import (
    API,
    SITE_IV,
    SITE_KEY,
    Loibus,
    Refusal,
    decode_token,
    decrypt_field,
    refusal,
    sign_request,
)
from flnkit import cookies as _cookies
from flnkit import net as _net
from flnkit import vault as _vault

# The site's own key and iv, as loix used them: "https://dl.example/a.zip", encrypted.
ENCRYPTED_URL = "GbQQUMfoM0sFLYH/040yiFqXee34SusjXJybD/DgNfA="


def request(url, cookies=None):
    """A GET request to `url` as `sign` sees it, with the login's `cookies`."""
    return Request("GET", url, {}, None, cookies or {})


class SignTest(unittest.TestCase):
    def test_loix_vectors(self):
        """The web client's signature, byte for byte, pinned from before the port."""
        signed = request("https://api.moegoat.com/api/lois/5930?page=1")
        sign_request(signed, now_ms=1_700_000_000_000, pick=lambda options: options[0],
                     noise=lambda size: "0123456789abcdef01")
        self.assertEqual(signed.headers["x-request-id"], "06056776-a901-4234-856d-789aebcdef50")
        self.assertEqual(signed.headers["x-signature-timestamp"], "1700000000000")
        self.assertEqual(signed.headers["x-request-signature"],
                         "0e7c819a540aaa6c47043c50ae7c278378a26790ec8e09edf05f799fa06c6556")
        self.assertEqual(signed.headers["x-security-challenge"], "turnstile-v1")
        # The same digest as the vault's.
        self.assertEqual(signed.headers["x-request-signature"],
                         _vault.digest("get|lois/5930||1700000000000|loibus-sec-decoy-salt-v1"))

    def test_every_signature_is_new(self):
        first, second = (request("https://api.moegoat.com/api/user/info") for _ in range(2))
        sign_request(first, now_ms=1)
        sign_request(second, now_ms=1)
        self.assertNotEqual(first.headers["x-request-id"], second.headers["x-request-id"])

    def test_an_api_request_is_signed_at_now_and_carries_the_token(self):
        api = request("https://api.moegoat.com/api/lois/1", {"access-token": "Bearer%20tok"})
        with mock.patch("flnd.sources.loi.time.time", return_value=1_700_000_000.0):
            Loibus().sign(api)
        self.assertEqual((api.headers["authorization"], len(api.headers["x-request-id"])),
                         ("Bearer tok", 36))
        self.assertEqual(api.headers["x-signature-timestamp"], "1700000000000")
        self.assertEqual(api.headers["x-request-signature"],
                         _vault.digest("get|lois/1||1700000000000|loibus-sec-decoy-salt-v1"))
        self.assertEqual((api.headers["referer"], api.headers["origin"]),
                         ("https://loix.cc/", "https://loix.cc"))

    def test_another_host_of_the_site_gets_no_token_and_no_signature(self):
        page = request("https://img.moegoat.com/a.jpg", {"access-token": "tok"})
        Loibus().sign(page)
        self.assertNotIn("x-request-id", page.headers)
        self.assertNotIn("authorization", page.headers)
        self.assertEqual(page.headers["referer"], "https://loix.cc/")

    def test_without_a_token_the_request_is_signed_only(self):
        api = request("https://api.moegoat.com/api/lois/1")
        Loibus().sign(api)
        self.assertNotIn("authorization", api.headers)
        self.assertIn("x-request-signature", api.headers)


class FieldsTest(unittest.TestCase):
    def test_decrypt_field(self):
        self.assertEqual(decrypt_field(ENCRYPTED_URL), "https://dl.example/a.zip")
        self.assertEqual(decrypt_field(""), "")
        self.assertEqual(decrypt_field(None), "")

    def test_a_field_that_doesnt_decrypt_raises(self):
        with self.assertRaisesRegex(SiteError, "changed its key"):
            decrypt_field("bm90IGJsb2Nrcw==")

    def test_decode_token(self):
        self.assertEqual(decode_token("Bearer%20abc.def"), "abc.def")
        self.assertEqual(decode_token("abc"), "abc")
        self.assertEqual(decode_token(""), "")


class RefusalTest(unittest.TestCase):
    def test_what_each_answer_means(self):
        """A rate limit and a used-up day are told apart; a 429 is always a rate limit."""
        cases = [
            (429, None, Refusal.RATE),
            (429, {"code": 429, "message": "daily limit exceeded"}, Refusal.RATE),
            (200, {"code": "429", "message": "slow down"}, Refusal.RATE),
            (200, {"status": "error", "message": "请求过于频繁"}, Refusal.RATE),
            (200, {"status": "error", "message": "daily limit exceeded"}, Refusal.QUOTA),
            (200, {"code": 403, "msg": "今日下载次数已达上限"}, Refusal.QUOTA),
            # A refusal by HTTP status alone, its JSON naming the quota (a fixed bug).
            (403, {"message": "download quota used up"}, Refusal.QUOTA),
            (403, {"message": "VIP only"}, None),
            (200, {"status": "success", "data": {"sv_list": [{"sv_url": "x"}]}}, None),
            (500, None, None),
        ]
        for status, body, expected in cases:
            with self.subTest(status=status, body=body):
                self.assertIs(refusal(status, body), expected)


# Every form loi reads, as people write it, and what it becomes: (kind, value), or a refusal's text.
# The kinds and values are the keys of flnd.db and the history, so a change here is a migration.
ROUTES = [
    ("loi:5930", ("post", "5930")),
    ("loi:https://www.loix.cc/lois/5930?ref=1", ("post", "5930")),
    ("https://loix.cc/lois/5930", ("post", "5930")),
    ("loix.cc/lois/5930", ("post", "5930")),
    ("loix.cc/lois/5930?x=1", ("post", "5930")),
    ("https://www.loix.cc/lois/5930/", ("post", "5930")),
    ("http://loix.cc/lois/5930#top", ("post", "5930")),
    ("https://m.loix.cc/lois/5930", ("post", "5930")),
    ("https://mobile.loix.cc/lois/5930", ("post", "5930")),
    ("https://loix.cc/lois/5930/lois/7", ("post", "5930")),
    ("https://moegoat.com/lois/5930", ("post", "5930")),
    ("https://www.moegoat.com/lois/5930/", ("post", "5930")),
    ("https://loix.cc/lois/0", ("post", "0")),
    ("loi:example_user", "can't read"),
    ("loi:abc", "can't read"),
    ("https://loix.cc/library/loier/7", "can't read"),
    ("https://loix.cc/lois/", "can't read"),
    ("https://loix.cc/lois/abc", "can't read"),
    ("https://loix.cc/", "can't read"),
    ("loix.cc/5930", "can't read"),
]


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in ROUTES:
            with self.subTest(text=text):
                if isinstance(expected, tuple):
                    found = resolve(text, {"loi": Loibus})
                    self.assertEqual((found.source, found.kind, found.value), ("loi", *expected))
                else:
                    with self.assertRaisesRegex(UsageError, f"^loi: {expected}"):
                        resolve(text, {"loi": Loibus})

    def test_a_link_elsewhere_is_not_loi(self):
        with self.assertRaises(UsageError):
            Loibus().resolve("https://example.com/lois/5930")


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
LINKS = {"status": "success", "data": {"sv_list": MIRRORS, "bdp": encrypt("https://pan.example/s/1"),
                                       "bdp_key": encrypt("abcd")}}
USER = {"data": {"id": 1, "vip_id": 1, "svip": 0}}
TOKEN = {"access-token": "Bearer%20tok"}
FOLDER = "example_author/5930_Night_city_set"
LINKS_PATH = "user/loi/download_v6/5930"


def fixtures(**changed):
    answers = {f"{API}lois/5930": {"status": "success", "data": POST}, f"{API}user/info": USER,
               f"{API}{LINKS_PATH}": LINKS}
    answers.update({f"{API}{path}": answer for path, answer in changed.items()})
    return answers


# Recorded from the source before the port: every (path, history key) it queued, for a post with
# a slash, a colon and a question mark in its title and author, whose preview images and archive
# URLs are odd in the ways the site's are.
NASTY = {**POST, "loi_title": "Night/city: set?", "category": {"id": 7, "name": "ex/ample"},
         "images": ["https://images.example/aaa", "https://images.example/bbb.png",
                    "https://images.example/c.webp?x=1"]}
NASTY_LINKS = {"status": "success", "data": {"sv_list": [
    {"sv_name": "A", "sv_url": encrypt("https://one.example/loi%20abc.7z")},
    {"sv_name": "B", "sv_url": encrypt("https://two.example/loi%20abc.7z")},
    {"sv_name": "A", "sv_url": encrypt("https://one.example/extra.zip")},
    {"sv_name": "A", "sv_url": encrypt("https://one.example/")}], "bdp": "", "bdp_key": ""}}
NASTY_FOLDER = "ex_ample/5930_Night_city_set"
NASTY_IMAGES = [(f"{NASTY_FOLDER}/images/{name}", f"{NASTY_FOLDER}/images/{name}")
                for name in ("01.jpg", "02.png", "03.webp")]
NASTY_ARCHIVES = [(f"{NASTY_FOLDER}/{name}", f"{NASTY_FOLDER}/{name}")
                  for name in ("loi_abc.7z", "extra.zip", "archive.zip")]
NASTY_PAIRS = {("all",): NASTY_IMAGES + NASTY_ARCHIVES, ("photo",): NASTY_IMAGES,
               ("archive",): NASTY_ARCHIVES}


def saved(categories=None, *, cookies=TOKEN, seen=None, post=POST, options=None, state=None,
          **changed):
    """What the job would download for post 5930, over `fixtures`, `changed` replacing some."""
    answers = fixtures(**changed)
    if "lois/5930" not in changed:
        answers[f"{API}lois/5930"] = {"status": "success", "data": post}
    return collect(Loibus, "loi:5930", json=answers, cookies=cookies, seen=seen, state=state,
                   options=options or Options(categories=categories))


def pairs(files):
    """(path, key) of each downloaded file, leaving out the text files the source writes."""
    return [(item.path, item.key) for item in files if item.file.content is None]


def texts(files):
    """The path and text of each text file."""
    return {item.path: item.file.content for item in files if item.file.content is not None}


def asked(seen):
    """The API paths a run asked for, in order."""
    return [url.removeprefix(API) for _, url, _ in seen.requests]


def archive_site(*, cookies=TOKEN, **changed):
    """A FakeSite for post 5930, and what it saw, for running a category on it directly."""
    seen = Seen()
    site = FakeSite(Target("loi:5930", kind="post"), cookies=cookies, json=fixtures(**changed),
                    seen=seen)
    return site, seen


def archive_files(site):
    """The archive category's files for POST: its one lazy post's, listed."""
    (post,) = Loibus().archive(site, POST)
    return list(post.files())


class NamesTest(unittest.TestCase):
    def test_names_and_keys_are_those_of_before_the_port(self):
        for categories, expected in NASTY_PAIRS.items():
            with self.subTest(categories=categories):
                files = saved(categories, post=NASTY, **{LINKS_PATH: NASTY_LINKS})
                self.assertEqual(pairs(files), expected)
                self.assertEqual(list(texts(files)), [f"{NASTY_FOLDER}/info.md"])

    def test_a_post_without_an_author_is_misc(self):
        files = saved(("photo",), post={**POST, "category": None})
        self.assertEqual(list(texts(files)), ["misc/5930_Night_city_set/info.md"])

    def test_a_key_is_its_path_and_the_archive_keeps_its_name(self):
        files = saved(("archive",))
        self.assertEqual(pairs(files), [(f"{FOLDER}/loi-abc.7z",) * 2, (f"{FOLDER}/extra.zip",) * 2])


class RunTest(unittest.TestCase):
    def test_without_a_token_info_and_images_only(self):
        seen = Seen()
        files = saved(("all",), cookies={}, seen=seen)
        self.assertEqual([item.path for item in files], [f"{FOLDER}/info.md",
                                                         f"{FOLDER}/images/01.jpg",
                                                         f"{FOLDER}/images/02.png"])
        self.assertEqual([item.url for item in files[1:]], POST["images"])
        self.assertEqual(files[1].post.date, "2026-11-09 12:00:00")
        self.assertEqual(asked(seen), ["lois/5930"])
        self.assertIn("! archives need a login; run flnd login loi", seen.messages)
        info = texts(files)[f"{FOLDER}/info.md"]
        self.assertIn("# Night city: set", info)
        self.assertIn("`pass-word`", info)
        self.assertIn("> line one\n>\n> line two", info)
        self.assertNotIn("Download Sources", info)

    def test_archives_are_grouped_by_file_with_the_password(self):
        seen = Seen()
        files = saved(("archive",), seen=seen)
        self.assertEqual([item.path for item in files], [f"{FOLDER}/info.md",
                                                         f"{FOLDER}/loi-abc.7z",
                                                         f"{FOLDER}/extra.zip"])
        self.assertEqual(files[1].url, ("https://one.example/loi-abc.7z",
                                        "https://two.example/loi-abc.7z"))
        self.assertEqual(files[1].file.password, "pass-word")
        self.assertEqual(asked(seen), ["lois/5930", "user/info", LINKS_PATH])
        info = texts(files)[f"{FOLDER}/info.md"]
        self.assertIn("`pass-word`", info)
        self.assertIn("- **Server [VIP line 2]**: [Download](https://two.example/loi-abc.7z)", info)
        self.assertIn("| Media Count | pictures 2 · videos 0 |", info)
        self.assertIn("[Link](https://pan.example/s/1) (Password: `abcd`)", info)

    def test_every_category_writes_info_md_and_the_archives_one_has_the_links(self):
        site, _ = archive_site()
        first = [item.content for post in Loibus().photo(site, POST) for item in post.files
                 if item.content]
        second = [item.content for item in archive_files(site) if item.content]
        self.assertNotIn("Download Sources", first[0])
        self.assertIn("Download Sources", second[0])

    def test_the_archive_counts_the_post_once(self):
        site, _ = archive_site()
        archive_files(site)
        archive_files(site)
        self.assertEqual(site.state["quota"], {"day": datetime.date.today().isoformat(),
                                               "posts": [5930]})

    def test_only_photo_never_asks_for_links(self):
        seen = Seen()
        files = saved(("photo",), seen=seen)
        self.assertEqual(asked(seen), ["lois/5930"])
        self.assertEqual(len(pairs(files)), 2)

    def test_files_carry_the_sites_referer_and_origin(self):
        for item in saved(("all",)):
            if item.file.content is None:
                self.assertEqual(dict(item.post.headers or {}),
                                 {"referer": "https://loix.cc/", "origin": "https://loix.cc"})

    def test_a_used_up_quota_skips_the_archives(self):
        site, seen = archive_site()
        site.state["quota"] = {"day": datetime.date.today().isoformat(), "posts": list(range(15))}
        self.assertEqual([item.name for item in archive_files(site)], ["info.md"])
        self.assertNotIn(LINKS_PATH, asked(seen))
        self.assertIn("! the daily VIP quota is used up (15 of 15); archives skipped",
                      seen.messages)

    def test_an_svip_account_gets_twice_its_level(self):
        site, seen = archive_site(**{"user/info": {"data": {"vip_id": 1, "svip": 1}}})
        site.state["quota"] = {"day": datetime.date.today().isoformat(), "posts": list(range(15))}
        self.assertEqual(len(archive_files(site)), 3)
        self.assertIn(LINKS_PATH, asked(seen))

    def test_a_post_counted_today_is_free_and_yesterday_is_forgotten(self):
        today = datetime.date.today().isoformat()
        site, seen = archive_site()
        site.state["quota"] = {"day": today, "posts": [*range(14), 5930]}
        self.assertEqual(len(archive_files(site)), 3)
        self.assertEqual(len(site.state["quota"]["posts"]), 15)
        # Taking it again costs nothing, so the account isn't asked for its level.
        self.assertNotIn("user/info", asked(seen))
        site, _ = archive_site()
        site.state["quota"] = {"day": "2000-01-01", "posts": list(range(15))}
        archive_files(site)
        self.assertEqual(site.state["quota"], {"day": today, "posts": [5930]})

    def test_refused_links_are_a_warning(self):
        seen = Seen()
        files = saved(("archive",), seen=seen, **{LINKS_PATH: SiteError("HTTP 403", status=403)})
        self.assertEqual(pairs(files), [])
        self.assertIn("! the site gives no download links for this post (VIP1 needed)",
                      seen.messages)

    def test_a_refused_post_is_not_counted(self):
        site, _ = archive_site(**{LINKS_PATH: SiteError("HTTP 403", status=403)})
        archive_files(site)
        self.assertNotIn("quota", site.state)

    def test_an_unknown_level_still_asks_for_links(self):
        """The account's info refused: its limit is unknown, so the links are asked for."""
        site, seen = archive_site(**{"user/info": SiteError("HTTP 500", status=500)})
        self.assertEqual(len(archive_files(site)), 3)
        self.assertIn(LINKS_PATH, asked(seen))

    def test_a_missing_post_is_an_error(self):
        with self.assertRaisesRegex(SiteError, "post 5930 isn't there"):
            saved(("all",), **{"lois/5930": {"status": "error", "data": None}})


class FixedBugsTest(unittest.TestCase):
    """The quota bugs of the source before the port, each pinned."""

    def test_a_vip0_account_takes_no_links(self):
        """VIP level 0 has a known limit of 0, which once read as "unknown" and fetched links."""
        seen = Seen()
        files = saved(("archive",), seen=seen, **{"user/info": {"data": {"vip_id": 0}}})
        self.assertEqual(pairs(files), [])
        self.assertNotIn(LINKS_PATH, asked(seen))
        self.assertIn("! the account has no VIP level, so no archives a day; archives skipped",
                      seen.messages)

    def test_a_rate_limit_answer_backs_off_and_asks_again(self):
        answers = iter([{"code": 429, "message": "too many requests"}, LINKS])
        site, seen = archive_site(**{LINKS_PATH: lambda url, request: next(answers)})
        self.assertEqual(len(archive_files(site)), 3)
        self.assertEqual(seen.back_offs, ["LOIBUS rate limit"])
        self.assertEqual(asked(seen).count(LINKS_PATH), 2)
        self.assertNotIn("exhausted", site.state["quota"])

    def test_a_rate_limit_that_lasts_stops_the_target(self):
        """The engine's back-off raises on its fourth wait in a row, and that ends the post."""
        site, seen = archive_site(**{LINKS_PATH: {"code": 429, "message": "too many requests"}})

        def back_off(reason, *, retry_after=None):
            seen.back_offs.append(reason)
            if len(seen.back_offs) > 3:
                raise Blocked(f"loi: {reason}, after 3 waits")

        site.back_off = back_off
        with self.assertRaisesRegex(Blocked, "after 3 waits"):
            archive_files(site)
        self.assertEqual(len(seen.back_offs), 4)
        self.assertNotIn("quota", site.state)

    def test_a_daily_quota_answer_marks_the_day_used_up(self):
        used_up = {"status": "error", "code": 403, "message": "daily download limit exceeded"}
        site, seen = archive_site(**{LINKS_PATH: used_up})
        self.assertEqual([item.name for item in archive_files(site)], ["info.md"])
        self.assertEqual(seen.back_offs, [])
        self.assertTrue(site.state["quota"]["exhausted"])
        self.assertIn("! the daily VIP quota is used up; archives skipped", seen.messages)
        # The next post skips the links request: the day is used up.
        seen.requests.clear()
        archive_files(site)
        self.assertNotIn(LINKS_PATH, asked(seen))

    def test_the_archive_post_is_lazy(self):
        """Listing the archive asks for no links and counts nothing; only its files do."""
        site, seen = archive_site()
        (post,) = Loibus().archive(site, POST)
        self.assertTrue(post.lazy)
        self.assertEqual(asked(seen), [])
        self.assertNotIn("quota", site.state)

    def test_a_post_outside_the_dates_costs_no_quota_and_no_request(self):
        seen, state = Seen(), {}
        files = saved(("all",), seen=seen, state=state,
                      options=Options(categories=("all",), since=datetime.date(2026, 11, 10)))
        self.assertEqual(files, [])
        self.assertEqual(asked(seen), ["lois/5930"])
        self.assertNotIn("quota", state)


# A 7z archive's first bytes, which the server sends under a .zip name; flnd saves it as it comes.
SEVEN_ZIP = b"7z\xbc\xaf\x27\x1c" + b"\0" * 26


class EngineTest(EngineCase):
    """The source through the real engine, on `flnkit.net.fake`."""

    def setUp(self):
        super().setUp()
        jar = [{"name": "access-token", "value": "Bearer%20tok", "domain": ".loix.cc", "path": "/"}]
        _home.save(self.home, "loi", _cookies.Jar(jar))
        self.routes.update({f"{API}lois/5930": _net.reply(200, json={"data": {**POST, "images": []}}),
                            f"{API}user/info": _net.reply(200, json=USER)})

    def test_a_retried_request_is_signed_again_and_the_archive_is_saved_as_sent(self):
        attempts = []

        def flaky(request):
            attempts.append(dict(request.headers))
            if len(attempts) == 1:
                return _net.reply(503)
            return _net.reply(200, json={"data": {"sv_list": [
                {"sv_name": "A", "sv_url": encrypt("https://one.example/set.zip")}]}})

        self.routes[f"{API}{LINKS_PATH}"] = flaky
        self.routes["https://one.example/set.zip"] = _net.reply(200, content=SEVEN_ZIP)
        # Each call a second later, so each try is signed at its own time.
        clock = itertools.count(1_700_000_000.0, 1.0)
        with mock.patch("flnd.sources.loi.time.time", lambda: next(clock)):
            self.run_engine(Loibus, targets=["loi:5930"], options=Options(categories=("archive",)))
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0]["x-signature-timestamp"], attempts[1]["x-signature-timestamp"])
        self.assertNotEqual(attempts[0]["x-request-id"], attempts[1]["x-request-id"])
        self.assertEqual(attempts[1]["authorization"], "Bearer tok")
        folder = self.target_dir("loi", "5930") / "example_author" / "5930_Night_city_set"
        self.assertEqual((folder / "set.zip").read_bytes(), SEVEN_ZIP)
        self.assertTrue((folder / "info.md").is_file())
        archive_request = next(request for request in self.requests
                               if request.url.host == "one.example")
        self.assertNotIn("authorization", archive_request.headers)
        self.assertNotIn("x-request-id", archive_request.headers)

    def test_a_403_on_links_is_soft(self):
        self.routes[f"{API}{LINKS_PATH}"] = _net.reply(403, json={"message": "VIP only"})
        self.run_engine(Loibus, targets=["loi:5930"], options=Options(categories=("all",)))
        folder = self.target_dir("loi", "5930") / "example_author" / "5930_Night_city_set"
        self.assertTrue((folder / "info.md").is_file())
        self.assertEqual(self.failures(), [])


class LoginTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home = self.root / ".flnd"

    def export(self, *names):
        jar = [{"name": name, "value": "v", "domain": "loix.cc", "path": "/"} for name in names]
        jar.append({"name": "access-token", "value": "other", "domain": "example.com", "path": "/"})
        path = self.root / "cookies.txt"
        _cookies.Jar(jar).save(path)
        return path

    def test_the_login_is_the_access_token_cookie(self):
        self.assertEqual(Loibus.login.names, ("access-token",))
        self.assertFalse(Loibus.login.required)

    def test_a_cookie_file(self):
        jar = _home.from_file(Loibus(), self.export("access-token", "user-level"))
        self.assertEqual(_home.store(Loibus(), jar, home=self.home, origin="cookies.txt"),
                         "loi: logged in from cookies.txt (2 cookies)")
        self.assertEqual({cookie["domain"] for cookie in _home.load(self.home, "loi")}, {".loix.cc"})

    def test_a_cookie_file_without_the_token(self):
        jar = _home.from_file(Loibus(), self.export("user-level"))
        with self.assertRaisesRegex(_vault.LoginError, "no access-token for loix.cc in cookies.txt"):
            _home.store(Loibus(), jar, home=self.home, origin="cookies.txt")

    def test_the_browser(self):
        store = {"loix.cc": [{"name": "access-token", "value": "v", "domain": ".loix.cc",
                              "path": "/"}]}

        def load(browser="chrome", *, profile=None):
            return _cookies.Jar(cookie for jar in store.values() for cookie in jar)

        with mock.patch("flnkit.cookies.load", load):
            jar = _home.from_browser(Loibus(), browser="chrome")
            self.assertEqual(_home.store(Loibus(), jar, home=self.home, origin="Chrome"),
                             "loi: logged in from Chrome (1 cookie)")
            store.clear()
            with self.assertRaisesRegex(_vault.LoginError, "no access-token"):
                _home.store(Loibus(), _home.from_browser(Loibus(), browser="chrome"),
                            home=self.home, origin="Chrome")


if __name__ == "__main__":
    unittest.main()
