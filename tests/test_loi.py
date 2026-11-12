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
from fakes import FakeSession
from test_engine import EngineCase

from flnd import auth
from flnd.session import Options
from flnd.sources.base import FetchError
from flnd.sources.loi import API, SITE_IV, SITE_KEY, Loibus, decode_token, decrypt_field, sign_request
from flnd.target import Target, TargetError
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

    def test_only_api_requests_are_signed_and_carry_the_token(self):
        token = {"authorization": "Bearer tok"}
        image = Loibus().sign(httpx.Request("GET", "https://img.example/a.jpg", headers=token))
        self.assertNotIn("x-request-id", image.headers)
        self.assertNotIn("authorization", image.headers)
        api = Loibus().sign(httpx.Request("GET", "https://api.moegoat.com/api/lois/1", headers=token))
        self.assertEqual((api.headers["authorization"], len(api.headers["x-request-id"])), ("Bearer tok", 36))


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

    def test_headers_carry_the_token(self):
        self.assertEqual(Loibus().headers({"access-token": "Bearer%20tok"})["authorization"], "Bearer tok")
        self.assertNotIn("authorization", Loibus().headers({}))
        self.assertEqual(Loibus().headers({})["referer"], "https://loix.cc/")


class ResolveTest(unittest.TestCase):
    def test_an_id_or_a_post_url(self):
        for text in ("5930", "https://loix.cc/lois/5930", "loix.cc/lois/5930?x=1", "https://www.loix.cc/lois/5930/"):
            with self.subTest(text=text):
                self.assertEqual(Loibus().resolve(text), Target("loi", "post", "5930"))

    def test_anything_else_is_refused(self):
        for text in ("example_user", "https://loix.cc/library/loier/7", "https://loix.cc/"):
            with self.subTest(text=text), self.assertRaises(TargetError):
                Loibus().resolve(text)


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


class RunTest(unittest.TestCase):
    def session(self, *, cookies=TOKEN, categories=None, **changed):
        return FakeSession(Target("loi", "post", "5930"), cookies=cookies, json=fixtures(**changed),
                           options=Options(categories=categories))

    def requested(self, session):
        return [url.removeprefix(API) for _, url, _ in session.requests]

    def test_without_a_token_info_and_images_only(self):
        session = self.session(cookies={}, categories=("all",))
        Loibus().run(session)
        self.assertEqual(session.paths(), [f"{FOLDER}/images/01.jpg", f"{FOLDER}/images/02.png"])
        self.assertEqual([item["source"] for item in session.saved], POST["images"])
        self.assertEqual(session.saved[0]["date"], datetime.date(2026, 11, 9))
        self.assertEqual(self.requested(session), ["lois/5930"])
        self.assertIn("! archives need a login; run flnd login", session.messages)
        info = session.written[f"{FOLDER}/info.md"]
        self.assertIn("# Night city: set", info)
        self.assertIn("`pass-word`", info)
        self.assertIn("> line one\n>\n> line two", info)
        self.assertNotIn("Download Sources", info)

    def test_archives_are_grouped_by_file_and_the_password_is_in_info_md(self):
        session = self.session(categories=("archive",))
        Loibus().run(session)
        self.assertEqual(session.paths(), [f"{FOLDER}/loi-abc.7z", f"{FOLDER}/extra.zip"])
        self.assertEqual(session.saved[0]["source"], ["https://one.example/loi-abc.7z", "https://two.example/loi-abc.7z"])
        self.assertEqual(self.requested(session), ["lois/5930", "user/info", "user/loi/download_v6/5930"])
        info = session.written[f"{FOLDER}/info.md"]
        # flnd no longer unpacks (--unpack is gone), so the password lives only in info.md.
        self.assertIn("`pass-word`", info)
        self.assertIn("- **Server [VIP line 2]**: [Download](https://two.example/loi-abc.7z)", info)
        self.assertIn("| Media Count | pictures 2 · videos 0 |", info)
        self.assertIn("[Link](https://pan.example/s/1) (Password: `abcd`)", info)
        self.assertEqual(session.state["quota"], {"day": datetime.date.today().isoformat(), "posts": [5930]})

    def test_from_photo_never_asks_for_links(self):
        session = self.session(categories=("photo",))
        Loibus().run(session)
        self.assertEqual(self.requested(session), ["lois/5930"])
        self.assertEqual(len(session.saved), 2)
        self.assertNotIn("quota", session.state)

    def test_a_used_up_quota_skips_the_archives(self):
        session = self.session(categories=("all",))
        today = datetime.date.today().isoformat()
        session.state["quota"] = {"day": today, "posts": list(range(15))}
        Loibus().run(session)
        self.assertEqual(len(session.saved), 2)
        self.assertNotIn("user/loi/download_v6/5930", self.requested(session))
        self.assertIn("! the daily VIP quota is used up (15 of 15); archives skipped", session.messages)

    def test_a_post_counted_today_is_free_and_yesterday_is_forgotten(self):
        session = self.session(categories=("archive",))
        today = datetime.date.today().isoformat()
        session.state["quota"] = {"day": today, "posts": [*range(14), 5930]}
        Loibus().run(session)
        self.assertEqual(len(session.saved), 2)
        self.assertEqual(len(session.state["quota"]["posts"]), 15)
        session = self.session(categories=("archive",))
        session.state["quota"] = {"day": "2000-01-01", "posts": list(range(15))}
        Loibus().run(session)
        self.assertEqual(session.state["quota"], {"day": today, "posts": [5930]})

    def test_refused_links_are_a_warning(self):
        session = self.session(categories=("archive",), **{"user/loi/download_v6/5930": FetchError("HTTP 403", status=403)})
        Loibus().run(session)
        self.assertEqual(session.paths(), [])
        self.assertIn("! the site gives no download links for this post (VIP1 needed)", session.messages)
        self.assertNotIn("quota", session.state)

    def test_a_post_outside_the_dates_is_skipped(self):
        session = FakeSession(Target("loi", "post", "5930"), json=fixtures(),
                              options=Options(since=datetime.date(2026, 11, 10)))
        Loibus().run(session)
        self.assertEqual((session.paths(), session.written), ([], {}))

    def test_a_missing_post_is_an_error(self):
        session = self.session(**{"lois/5930": {"status": "error", "data": None}})
        with self.assertRaisesRegex(FetchError, "post 5930 isn't there"):
            Loibus().run(session)


# A 7z archive's first bytes, which the server sends under a .zip name; flnd saves it as it comes.
SEVEN_ZIP = b"7z\xbc\xaf\x27\x1c" + b"\0" * 26


class EngineTest(EngineCase):
    """The source through the real engine, on httpx.MockTransport."""

    def setUp(self):
        super().setUp()
        jar = [{"name": "access-token", "value": "Bearer%20tok", "domain": ".loix.cc", "path": "/"}]
        cookies.to_file(jar, auth.cookie_file(self.home, "loi"))
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
        cookies.to_file(jar, path)
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
        with mock.patch("shared._cookies.load_from_browser",
                        lambda domain, *, browser, profile=None: store.get(domain, [])):
            jar = auth.from_browser(Loibus(), browser="chrome")
            self.assertEqual(auth.store(Loibus(), jar, home=self.home, origin="Chrome"),
                             "loi: logged in from Chrome (1 cookies)")
            store.clear()
            with self.assertRaisesRegex(auth.LoginError, "no access-token"):
                auth.store(Loibus(), auth.from_browser(Loibus(), browser="chrome"), home=self.home, origin="Chrome")


if __name__ == "__main__":
    unittest.main()
