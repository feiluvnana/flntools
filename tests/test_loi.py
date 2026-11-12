"""LOIBUS: signing, decrypting, targets, and a post's info, images and archives."""

import base64
import datetime
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from fakes import FakeSite, Seen, collect
from test_engine import EngineCase

from flnd.common import auth
from flnd.session import Options
from flnd.source import FetchError, Target, TargetError
from flnd.sources.loi import API, SITE_IV, SITE_KEY, Loibus, decode_token, decrypt_field, sign_request
from shared import cookies, crypto

# The site's own key and iv, as loix used them: "https://dl.example/a.zip", encrypted.
ENCRYPTED_URL = "GbQQUMfoM0sFLYH/040yiFqXee34SusjXJybD/DgNfA="


class SignTest(unittest.TestCase):
    def test_loix_vectors(self):
        request = httpx.Request("GET", "https://api.moegoat.com/api/lois/5930?page=1")
        signed = sign_request(request, now_ms=1_700_000_000_000, pick=lambda options: options[0],
                              noise=lambda size: "0123456789abcdef01")
        self.assertEqual(signed.headers["x-request-id"], "06056776-a901-4234-856d-789aebcdef50")
        self.assertEqual(signed.headers["x-signature-timestamp"], "1700000000000")
        self.assertEqual(signed.headers["x-request-signature"],
                         "0e7c819a540aaa6c47043c50ae7c278378a26790ec8e09edf05f799fa06c6556")
        self.assertEqual(signed.headers["x-security-challenge"], "turnstile-v1")
        self.assertEqual(signed.headers["x-request-signature"],
                         crypto.sha256("get|lois/5930||1700000000000|loibus-sec-decoy-salt-v1"))

    def test_every_signature_is_new(self):
        first = sign_request(httpx.Request("GET", "https://api.moegoat.com/api/user/info"), now_ms=1)
        second = sign_request(httpx.Request("GET", "https://api.moegoat.com/api/user/info"), now_ms=1)
        self.assertNotEqual(first.headers["x-request-id"], second.headers["x-request-id"])

    def test_an_api_request_is_signed_and_carries_the_token(self):
        cookies = {"access-token": "Bearer%20tok"}
        api = Loibus().prepare(httpx.Request("GET", "https://api.moegoat.com/api/lois/1"), cookies)
        self.assertEqual((api.headers["authorization"], len(api.headers["x-request-id"])), ("Bearer tok", 36))
        self.assertEqual((api.headers["referer"], api.headers["origin"]), ("https://loix.cc/", "https://loix.cc"))

    def test_another_host_of_the_site_gets_no_token_and_no_signature(self):
        page = Loibus().prepare(httpx.Request("GET", "https://img.moegoat.com/a.jpg"), {"access-token": "tok"})
        self.assertNotIn("x-request-id", page.headers)
        self.assertNotIn("authorization", page.headers)
        self.assertEqual(page.headers["referer"], "https://loix.cc/")

    def test_without_a_token_the_request_is_signed_only(self):
        api = Loibus().prepare(httpx.Request("GET", "https://api.moegoat.com/api/lois/1"), {})
        self.assertNotIn("authorization", api.headers)
        self.assertIn("x-request-signature", api.headers)


class FieldsTest(unittest.TestCase):
    def test_decrypt_field(self):
        self.assertEqual(decrypt_field(ENCRYPTED_URL), "https://dl.example/a.zip")
        self.assertEqual(decrypt_field(""), "")
        self.assertEqual(decrypt_field(None), "")

    def test_a_field_that_doesnt_decrypt_raises(self):
        with self.assertRaisesRegex(ValueError, "changed its key"):
            decrypt_field("bm90IGJsb2Nrcw==")

    def test_decode_token(self):
        self.assertEqual(decode_token("Bearer%20abc.def"), "abc.def")
        self.assertEqual(decode_token("abc"), "abc")
        self.assertEqual(decode_token(""), "")


# Every form loi reads, as people write it, and what it becomes: (kind, value), or a refusal's text.
# The kinds and values are the keys of targets.db and the history, so a change here is a migration.
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
    ("example_user", "can't read"),
    ("loi:abc", "can't read"),
    ("https://loix.cc/library/loier/7", "can't read"),
    ("https://loix.cc/lois/", "can't read"),
    ("https://loix.cc/lois/abc", "can't read"),
    ("https://loix.cc/", "can't read"),
    ("loix.cc/5930", "can't read"),
    ("https://example.com/lois/5930", "can't read"),
]


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in ROUTES:
            with self.subTest(text=text):
                value = text.removeprefix("loi:")
                if isinstance(expected, tuple):
                    self.assertEqual(Loibus().resolve(value), Target("loi", *expected))
                else:
                    with self.assertRaisesRegex(TargetError, f"^loi: {expected}"):
                        Loibus().resolve(value)


def encrypt(text):
    """`text` as the site sends a field: AES-128-CBC under its key and IV, PKCS7-padded, in base64."""
    padder = PKCS7(128).padder()
    padded = padder.update(text.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(SITE_KEY), modes.CBC(SITE_IV)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


POST = {"id": 5930, "loi_title": "Night city: set", "loi_description": "line one\n\nline two",
        "category": {"id": 7, "name": "example_author"}, "date": "2026-11-09 12:00:00",
        "images": ["https://images.example/aaa", "https://images.example/bbb.png"], "file_size": "1.2 GB",
        "open_key": "pass-word", "tags": [{"id": 1, "name": "night"}], "pic_count": 2, "video_count": 0,
        "like_count": 34, "viewer_count": 100, "download_count": 5, "vip_requirement": 1}
MIRRORS = [{"sv_name": "VIP line 1", "sv_url": encrypt("https://one.example/loi-abc.7z")},
           {"sv_name": "VIP line 2", "sv_url": encrypt("https://two.example/loi-abc.7z")},
           {"sv_name": "VIP line 1", "sv_url": encrypt("https://one.example/extra.zip")}]
LINKS = {"status": "success", "data": {"sv_list": MIRRORS, "bdp": encrypt("https://pan.example/s/1"),
                                       "bdp_key": encrypt("abcd")}}
USER = {"data": {"id": 1, "vip_id": 1, "svip": 0}}
FOLDER = "example_author/5930 Night city: set"
TOKEN = {"access-token": "Bearer%20tok"}


def fixtures(**changed):
    answers = {f"{API}lois/5930": {"status": "success", "data": POST}, f"{API}user/info": USER,
               f"{API}user/loi/download_v6/5930": LINKS}
    answers.update({f"{API}{path}": answer for path, answer in changed.items()})
    return answers


# Recorded from the `run(session)` source before step 6, through its fake session: every (path,
# history key) it queued, and every file it wrote, for a post with a slash, a colon and a question
# mark in its title and author, whose preview images and archive URLs are odd in the ways the site's are.
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
NASTY_PAIRS = {("all",): NASTY_IMAGES + NASTY_ARCHIVES, ("photo",): NASTY_IMAGES, ("archive",): NASTY_ARCHIVES}


def saved(categories=None, *, cookies=TOKEN, seen=None, post=POST, options=None, **changed):
    """What the job would download for post 5930, over `fixtures`, `changed` replacing some of them."""
    answers = fixtures(**changed)
    if "lois/5930" not in changed:
        answers[f"{API}lois/5930"] = {"status": "success", "data": post}
    return collect(Loibus, "loi:5930", json=answers, cookies=cookies, seen=seen,
                   options=options or Options(categories=categories))


def pairs(files):
    """(path, key) of each downloaded file, leaving out the text files the source writes itself."""
    return [(item.path, item.key) for item in files if item.file.content is None]


def texts(files):
    """The path and text of each text file."""
    return {item.path: item.file.content for item in files if item.file.content is not None}


def archive_site(*, cookies=TOKEN, **changed):
    """A FakeSite for post 5930, and what it saw, for running a category on it directly."""
    seen = Seen()
    return FakeSite(Target("loi", "post", "5930"), cookies=cookies, json=fixtures(**changed), seen=seen), seen


class NamesTest(unittest.TestCase):
    def test_names_and_keys_are_those_run_gave(self):
        """Every (path, key) of `run(session)`, recorded before the port, in each `--from`, is what `collect` gives now."""
        for categories, expected in NASTY_PAIRS.items():
            with self.subTest(categories=categories):
                files = saved(categories, post=NASTY, **{"user/loi/download_v6/5930": NASTY_LINKS})
                self.assertEqual(pairs(files), expected)
                self.assertEqual(list(texts(files)), [f"{NASTY_FOLDER}/info.md"])

    def test_a_post_without_an_author_is_misc(self):
        files = saved(("photo",), post={**POST, "category": None})
        self.assertEqual(list(texts(files)), ["misc/5930_Night_city_set/info.md"])

    def test_a_key_is_its_path_and_the_archive_keeps_its_name(self):
        files = saved(("archive",))
        folder = "example_author/5930_Night_city_set"
        self.assertEqual(pairs(files), [(f"{folder}/loi-abc.7z",) * 2, (f"{folder}/extra.zip",) * 2])


class RunTest(unittest.TestCase):
    def test_without_a_token_info_and_images_only(self):
        seen = Seen()
        files = saved(("all",), cookies={}, seen=seen)
        folder = "example_author/5930_Night_city_set"
        self.assertEqual([item.path for item in files], [f"{folder}/info.md", f"{folder}/images/01.jpg",
                                                         f"{folder}/images/02.png"])
        self.assertEqual([item.url for item in files[1:]], POST["images"])
        self.assertEqual(files[1].post.date, "2026-11-09 12:00:00")
        self.assertEqual([url.removeprefix(API) for _, url, _ in seen.requests], ["lois/5930"])
        self.assertIn("! archives need a login; run flnd login", seen.messages)
        info = texts(files)[f"{folder}/info.md"]
        self.assertIn("# Night city: set", info)
        self.assertIn("`pass-word`", info)
        self.assertIn("> line one\n>\n> line two", info)
        self.assertNotIn("Download Sources", info)

    def test_archives_are_grouped_by_file_and_the_password_is_in_info_md(self):
        seen = Seen()
        files = saved(("archive",), seen=seen)
        folder = "example_author/5930_Night_city_set"
        self.assertEqual([item.path for item in files], [f"{folder}/info.md", f"{folder}/loi-abc.7z",
                                                         f"{folder}/extra.zip"])
        self.assertEqual(files[1].url, ("https://one.example/loi-abc.7z", "https://two.example/loi-abc.7z"))
        self.assertEqual([url.removeprefix(API) for _, url, _ in seen.requests],
                         ["lois/5930", "user/info", "user/loi/download_v6/5930"])
        info = texts(files)[f"{folder}/info.md"]
        # flnd no longer unpacks (--unpack is gone), so the password lives only in info.md.
        self.assertIn("`pass-word`", info)
        self.assertIn("- **Server [VIP line 2]**: [Download](https://two.example/loi-abc.7z)", info)
        self.assertIn("| Media Count | pictures 2 · videos 0 |", info)
        self.assertIn("[Link](https://pan.example/s/1) (Password: `abcd`)", info)

    def test_every_category_writes_info_md_and_the_archives_one_has_the_links(self):
        photo, archive = Loibus().photo, Loibus().archive
        site, _ = archive_site()
        first = [item.content for post in photo(site, POST) for item in post.files if item.content]
        second = [item.content for post in archive(site, POST) for item in post.files if item.content]
        self.assertNotIn("Download Sources", first[0])
        self.assertIn("Download Sources", second[0])

    def test_the_archive_counts_the_post_once(self):
        site, _ = archive_site()
        list(Loibus().archive(site, POST))
        list(Loibus().archive(site, POST))
        self.assertEqual(site.state["quota"], {"day": datetime.date.today().isoformat(), "posts": [5930]})

    def test_from_photo_never_asks_for_links(self):
        seen = Seen()
        files = saved(("photo",), seen=seen)
        self.assertEqual([url.removeprefix(API) for _, url, _ in seen.requests], ["lois/5930"])
        self.assertEqual(len(pairs(files)), 2)

    def test_files_carry_the_sites_referer_and_origin(self):
        files = saved(("all",))
        for item in files:
            if item.file.content is None:
                self.assertEqual(dict(item.post.headers), {"referer": "https://loix.cc/", "origin": "https://loix.cc"})

    def test_a_used_up_quota_skips_the_archives(self):
        site, seen = archive_site()
        site.state["quota"] = {"day": datetime.date.today().isoformat(), "posts": list(range(15))}
        posts = list(Loibus().archive(site, POST))
        self.assertEqual([item.name for post in posts for item in post.files], ["info.md"])
        self.assertNotIn("user/loi/download_v6/5930", [url.removeprefix(API) for _, url, _ in seen.requests])
        self.assertIn("! the daily VIP quota is used up (15 of 15); archives skipped", seen.messages)

    def test_a_post_counted_today_is_free_and_yesterday_is_forgotten(self):
        today = datetime.date.today().isoformat()
        site, _ = archive_site()
        site.state["quota"] = {"day": today, "posts": [*range(14), 5930]}
        posts = list(Loibus().archive(site, POST))
        self.assertEqual(len(posts[0].files), 3)
        self.assertEqual(len(site.state["quota"]["posts"]), 15)
        site, _ = archive_site()
        site.state["quota"] = {"day": "2000-01-01", "posts": list(range(15))}
        list(Loibus().archive(site, POST))
        self.assertEqual(site.state["quota"], {"day": today, "posts": [5930]})

    def test_refused_links_are_a_warning(self):
        seen = Seen()
        files = saved(("archive",), seen=seen, **{"user/loi/download_v6/5930": FetchError("HTTP 403", status=403)})
        self.assertEqual(pairs(files), [])
        self.assertIn("! the site gives no download links for this post (VIP1 needed)", seen.messages)

    def test_a_refused_post_is_not_counted(self):
        site, _ = archive_site(**{"user/loi/download_v6/5930": FetchError("HTTP 403", status=403)})
        list(Loibus().archive(site, POST))
        self.assertNotIn("quota", site.state)

    def test_a_post_outside_the_dates_costs_no_quota_and_no_request(self):
        seen = Seen()
        files = saved(seen=seen, options=Options(since=datetime.date(2026, 11, 10)))
        self.assertEqual(files, [])
        self.assertEqual([url.removeprefix(API) for _, url, _ in seen.requests], ["lois/5930"])

    def test_a_missing_post_is_an_error(self):
        with self.assertRaisesRegex(FetchError, "post 5930 isn't there"):
            saved(**{"lois/5930": {"status": "error", "data": None}})


# A 7z archive's first bytes, which the server sends under a .zip name; flnd saves it as it comes.
SEVEN_ZIP = b"7z\xbc\xaf\x27\x1c" + b"\0" * 26


class EngineTest(EngineCase):
    """The source through the real engine, on httpx.MockTransport."""

    def setUp(self):
        super().setUp()
        jar = [{"name": "access-token", "value": "Bearer%20tok", "domain": ".loix.cc", "path": "/"}]
        cookies.Jar(jar).save(auth.cookie_file(self.home, "loi"))
        self.routes.update({f"{API}lois/5930": httpx.Response(200, json={"data": {**POST, "images": []}}),
                            f"{API}user/info": httpx.Response(200, json=USER)})

    def test_a_retried_request_is_signed_again_and_the_archive_is_saved_as_sent(self):
        attempts = []

        def flaky(request):
            attempts.append(dict(request.headers))
            if len(attempts) == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"data": {"sv_list": [
                {"sv_name": "A", "sv_url": encrypt("https://one.example/set.zip")}]}})

        self.routes[f"{API}user/loi/download_v6/5930"] = flaky
        self.routes["https://one.example/set.zip"] = httpx.Response(200, content=SEVEN_ZIP)
        clock = iter(range(1_700_000_000_000, 1_700_000_100_000, 1000))
        with mock.patch("flnd.sources.loi._clock_ms", lambda: next(clock)):
            self.run_engine(Loibus, targets=["loi:5930"], options=Options(categories=("archive",)))
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0]["x-signature-timestamp"], attempts[1]["x-signature-timestamp"])
        self.assertNotEqual(attempts[0]["x-request-id"], attempts[1]["x-request-id"])
        self.assertEqual(attempts[1]["authorization"], "Bearer tok")
        folder = self.target_dir("loi", "5930") / "example_author" / "5930_Night_city_set"
        self.assertEqual((folder / "set.zip").read_bytes(), SEVEN_ZIP)
        self.assertFalse((folder / "set").exists())
        self.assertTrue((folder / "info.md").is_file())
        archive_request = next(request for request in self.requests if request.url.host == "one.example")
        self.assertNotIn("authorization", archive_request.headers)
        self.assertNotIn("x-request-id", archive_request.headers)

    def test_a_403_on_links_is_soft(self):
        self.routes[f"{API}user/loi/download_v6/5930"] = httpx.Response(403, json={"message": "VIP only"})
        self.run_engine(Loibus, targets=["loi:5930"], options=Options(categories=("all",)))
        folder = self.target_dir("loi", "5930") / "example_author" / "5930_Night_city_set"
        self.assertTrue((folder / "info.md").is_file())
        self.assertFalse(any("✗" in line for line in self.lines()), self.lines())


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
        cookies.Jar(jar).save(path)
        return path

    def test_a_cookie_file(self):
        jar = auth.from_file(Loibus(), self.export("access-token", "user-level"))
        self.assertEqual(auth.store(Loibus(), jar, home=self.home, origin="cookies.txt"),
                         "loi: logged in from cookies.txt (2 cookies)")
        self.assertEqual({cookie["domain"] for cookie in auth.load(self.home, "loi")}, {".loix.cc"})

    def test_a_cookie_file_without_the_token(self):
        jar = auth.from_file(Loibus(), self.export("user-level"))
        with self.assertRaisesRegex(auth.LoginError, "no access-token for loix.cc in cookies.txt"):
            auth.store(Loibus(), jar, home=self.home, origin="cookies.txt")

    def test_the_browser(self):
        store = {"loix.cc": [{"name": "access-token", "value": "v", "domain": ".loix.cc", "path": "/"}]}
        with mock.patch("shared._cookies.from_browser",
                        lambda browser="chrome", *, profile=None: cookies.Jar(c for jar in store.values() for c in jar)):
            jar = auth.from_browser(Loibus(), browser="chrome")
            self.assertEqual(auth.store(Loibus(), jar, home=self.home, origin="Chrome"),
                             "loi: logged in from Chrome (1 cookie)")
            store.clear()
            with self.assertRaisesRegex(auth.LoginError, "no access-token"):
                auth.store(Loibus(), auth.from_browser(Loibus(), browser="chrome"), home=self.home, origin="Chrome")


if __name__ == "__main__":
    unittest.main()
