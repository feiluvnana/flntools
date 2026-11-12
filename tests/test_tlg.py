"""Telegram: targets, logins, and downloads through the engine, on a fake Telethon client. No network."""

import asyncio
import base64
import datetime
import io
import json
import struct
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakes import FakeSession
from telethon.errors import FloodWaitError, PhoneCodeInvalidError, SessionPasswordNeededError
from test_commands import FlndCase
from test_engine import EngineCase, make_source

from flnd.common import auth
from flnd.common.target import Target, TargetError, resolve
from flnd.engine import Engine
from flnd.session import Options
from flnd.sources import SOURCES
from flnd.sources.base import LoginError
from flnd.sources.tlg import QR_CODES, Telegram, flood_wait, from_web_storage, half_blocks
from shared import fmt

DAY = datetime.date(2026, 1, 5)
SESSION = "1FAKE-SESSION-aGVsbG8"
CREDENTIALS = {"api_id": "12345", "api_hash": "0123456789abcdef0123456789abcdef"}
# Which kind of message each of Telegram's server-side filters returns.
KIND_OF_FILTER = {"InputMessagesFilterPhotos": "photo", "InputMessagesFilterVideo": "video",
                  "InputMessagesFilterDocument": "file"}


def message(number, kind, *, day=DAY, name=None, content=b"media", size=None):
    ext = {"photo": ".jpg", "video": ".mp4"}.get(kind, "")
    when = datetime.datetime.combine(day, datetime.time(12), tzinfo=datetime.timezone.utc)
    file = SimpleNamespace(name=name, ext=ext or (name and "." + name.rsplit(".", 1)[-1]), size=size or len(content))
    return SimpleNamespace(id=number, date=when, kind=kind, file=file, content=content)


def chat(number, title, username=None):
    return SimpleNamespace(id=number, title=title, username=username)


class FakeTelegram:
    """Telegram's side: the account's chats and how a login goes. `client` stands in for `TelegramClient`."""

    def __init__(self):
        self.authorized = False
        self.password = None
        self.qr_outcomes = []
        self.wrong_code = False
        self.me = SimpleNamespace(id=1, first_name="Example", last_name="User", username="example_user")
        self.chats = {"examplechannel": chat(1001, "Example Channel", "examplechannel")}
        self.dialogs = [SimpleNamespace(id=-1001001, name="Example Channel", entity=self.chats["examplechannel"])]
        self.messages = {1001: []}
        self.clients = []
        self.asked = []
        self.yielded = 0
        self.downloads = []
        self.download_errors = []
        self.threads = set()

    def client(self, string, api_id, api_hash):
        client = FakeClient(self, string, api_id, api_hash)
        self.clients.append(client)
        return client


class FakeClient:
    def __init__(self, telegram, string, api_id, api_hash):
        self.telegram, self.api_id, self.api_hash = telegram, api_id, api_hash
        self.session = SimpleNamespace(save=lambda: self.string)
        self.string = string
        self.connected = False
        self.flood_sleep_threshold = 60

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return bool(self.string) and self.telegram.authorized

    async def get_me(self):
        return self.telegram.me if await self.is_user_authorized() else None

    def _signed_in(self):
        self.telegram.authorized = True
        self.string = SESSION

    async def qr_login(self):
        return FakeQR(self)

    async def send_code_request(self, phone):
        self.telegram.phone = phone

    async def sign_in(self, phone=None, code=None, *, password=None):
        if password is not None:
            if password != self.telegram.password:
                raise SessionPasswordNeededError(None)
            return self._signed_in()
        if self.telegram.wrong_code:
            raise PhoneCodeInvalidError(None)
        self.telegram.code = code
        if self.telegram.password:
            raise SessionPasswordNeededError(None)
        self._signed_in()

    async def get_entity(self, value):
        found = self.telegram.chats.get(str(value).lstrip("@"))
        if found is None:
            raise ValueError(f'No user has "{value}" as username')
        return found

    async def get_dialogs(self):
        return self.telegram.dialogs

    async def iter_messages(self, entity, *, filter=None, offset_date=None):
        self.telegram.threads.add(threading.current_thread().name)
        kind = KIND_OF_FILTER[type(filter).__name__]
        self.telegram.asked.append((kind, offset_date))
        for item in sorted(self.telegram.messages[entity.id], key=lambda item: item.date, reverse=True):
            if item.kind == kind and (offset_date is None or item.date < offset_date):
                self.telegram.yielded += 1
                yield item
            await asyncio.sleep(0)

    async def download_media(self, item, file=None, *, progress_callback=None):
        if self.telegram.download_errors:
            raise self.telegram.download_errors.pop(0)
        self.telegram.downloads.append(item.id)
        progress_callback(len(item.content) // 2, len(item.content))
        file.write(item.content)
        return file


class FakeQR:
    def __init__(self, client):
        self.client, self.number = client, 1

    @property
    def url(self):
        return f"tg://login?token=token{self.number}"

    async def wait(self, timeout=None):
        outcome = self.client.telegram.qr_outcomes.pop(0)
        if outcome == "timeout":
            raise asyncio.TimeoutError()
        if outcome == "password":
            raise SessionPasswordNeededError(None)
        self.client._signed_in()

    async def recreate(self):
        self.number += 1


def store(home, **values):
    with auth.secrets(home, "tlg") as secrets:
        secrets.update(values)


def stored(home):
    with auth.secrets(home, "tlg") as secrets:
        return dict(secrets)


class ResolveTest(unittest.TestCase):
    def test_every_form_is_a_chat(self):
        rows = [
            ("tlg:@examplechannel", "examplechannel"), ("tlg:examplechannel", "examplechannel"),
            ("https://t.me/examplechannel", "examplechannel"), ("t.me/examplechannel", "examplechannel"),
            ("https://t.me/s/examplechannel", "examplechannel"), ("https://t.me/examplechannel/123", "examplechannel"),
            ("tlg:Lens Club", "Lens Club"), ("tlg:-1001234567890", "-1001234567890"),
            ("https://t.me/c/1234567890/55", "-1001234567890"), ("https://t.me/+AbCdEf123", "+AbCdEf123"),
            ("https://t.me/joinchat/AbCdEf123", "+AbCdEf123"),
        ]
        for text, value in rows:
            with self.subTest(text=text):
                self.assertEqual(resolve(text, SOURCES), Target("tlg", "chat", value))

    def test_unreadable_forms_list_the_accepted_ones(self):
        for text in ("https://t.me/", "https://t.me/c/abc", "tlg:@", "tlg:@bad name"):
            with self.subTest(text=text), self.assertRaisesRegex(TargetError, "tlg:@examplechannel"):
                resolve(text, SOURCES)

    def test_declarations(self):
        self.assertEqual((Telegram.categories, Telegram.single_kinds), (("photo", "video", "file"), ()))
        self.assertEqual((Telegram.login_methods, Telegram.requires_login), (("browser", "qr", "phone"), True))
        self.assertIs(Telegram.wait, flood_wait)


class FloodWaitTest(unittest.TestCase):
    def test_flood_waits_are_honored_and_other_errors_wait_as_usual(self):
        self.assertEqual(flood_wait(FloodWaitError(None, capture=3), 1), 3)
        self.assertIsNone(flood_wait(OSError("reset"), 1))


def web_storage(dc=2, key=bytes(range(256)), *, quoted=True):
    """A Telegram Web local-storage dump: values are JSON, as both web clients write them."""
    value = json.dumps if quoted else str
    return {"dc": value(dc), f"dc{dc}_auth_key": value(key.hex()), "dc1_auth_key": value("ab" * 256),
            "user_auth": json.dumps({"dcID": dc, "id": "1"}), "tt-global-state": "{}"}


def decode_session(string):
    # Telethon's StringSession: version "1", then base64 of dc (1 byte), IPv4 (4), port (2) and the 256-byte key.
    dc, ip, port, key = struct.unpack(">B4sH256s", base64.urlsafe_b64decode(string[1:]))
    return dc, ".".join(map(str, ip)), port, key


class WebStorageTest(unittest.TestCase):
    def test_the_accounts_data_center_and_key(self):
        for quoted in (True, False):
            with self.subTest(quoted=quoted):
                dc, ip, port, key = decode_session(from_web_storage(web_storage(4, quoted=quoted)))
                self.assertEqual((dc, ip, port, key), (4, "149.154.167.91", 443, bytes(range(256))))

    def test_not_logged_in_to_telegram_web(self):
        for storage in ({}, {"dc": "2"}, {**web_storage(), "dc2_auth_key": json.dumps("not hex")}):
            with self.subTest(storage=list(storage)), self.assertRaisesRegex(LoginError, "log in at web.telegram.org"):
                from_web_storage(storage)


class HalfBlocksTest(unittest.TestCase):
    def test_two_rows_per_line_light_modules_drawn(self):
        # Dark modules are True; a dark terminal shows the light ones, as `qrencode -t UTF8` does.
        matrix = [[False, True, False], [False, False, True], [True, True, False]]
        self.assertEqual(half_blocks(matrix), "█▄▀\n▄▄█")


class LoginCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.telegram = FakeTelegram()
        patcher = mock.patch("flnd.sources.tlg._client", self.telegram.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def answer(self, *answers):
        self.screen._interactive = True
        self.screen.readkey = iter([key for answer in answers for key in [*answer, "enter"]]).__next__

    def log_in(self, method):
        Engine(home=self.home).login(Telegram(), method)

    def assert_logged_in(self):
        self.assertEqual(stored(self.home), {"session": SESSION, **CREDENTIALS})
        self.assertIn("• tlg: logged in as Example User", self.lines())
        # The session and the hash are sealed in secrets.db: no file holds them readably.
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"FAKE-SESSION", path.read_bytes(), path)
                self.assertNotIn(CREDENTIALS["api_hash"].encode(), path.read_bytes(), path)
        self.assertEqual(list(self.home.rglob("*.session")), [])
        self.assertFalse(any(client.connected for client in self.telegram.clients))


class LoginTest(LoginCase):
    def test_phone_with_two_step_verification(self):
        self.telegram.password = "hunter2"
        self.answer(CREDENTIALS["api_id"], CREDENTIALS["api_hash"], "+15550100", "12345", "hunter2")
        self.log_in("phone")
        self.assert_logged_in()
        self.assertEqual((self.telegram.phone, self.telegram.code), ("+15550100", "12345"))
        client = self.telegram.clients[0]
        self.assertEqual((client.api_id, client.api_hash, client.string), (12345, CREDENTIALS["api_hash"], SESSION))
        screen = self.screen.out.getvalue()
        self.assertNotIn("hunter2", screen)
        self.assertNotIn(CREDENTIALS["api_hash"], screen)

    def test_stored_credentials_are_not_asked_again_and_a_new_login_replaces_the_session(self):
        store(self.home, session="old", **CREDENTIALS)
        self.answer("+15550100", "12345")
        self.log_in("phone")
        self.assert_logged_in()

    def test_qr_accepted_after_a_timeout(self):
        self.telegram.qr_outcomes = ["timeout", "ok"]
        self.answer(*CREDENTIALS.values())
        self.log_in("qr")
        self.assert_logged_in()
        out = self.screen.out.getvalue()
        self.assertEqual(out.count("Settings → Devices → Link Desktop Device"), 2)
        self.assertIn("█", out)

    def test_qr_then_two_step_verification(self):
        self.telegram.qr_outcomes, self.telegram.password = ["password"], "hunter2"
        self.answer(*CREDENTIALS.values(), "hunter2")
        self.log_in("qr")
        self.assert_logged_in()

    def test_qr_never_scanned(self):
        self.telegram.qr_outcomes = ["timeout"] * QR_CODES
        self.answer(*CREDENTIALS.values())
        with self.assertRaisesRegex(LoginError, "no QR code was scanned; run flnd login again"):
            self.log_in("qr")
        self.assertEqual(stored(self.home), {})

    def test_a_refused_code(self):
        self.telegram.wrong_code = True
        self.answer(*CREDENTIALS.values(), "+15550100", "99999")
        with self.assertRaisesRegex(LoginError, "tlg: Telegram refused the login"):
            self.log_in("phone")
        self.assertEqual(stored(self.home), {})

    def test_browser_from_telegram_web(self):
        store(self.home, **CREDENTIALS)
        self.telegram.authorized = True
        with mock.patch("flnd.sources.tlg.read_telegram_web", lambda: web_storage(2)):
            self.log_in("browser")
        client = self.telegram.clients[0]
        self.assertEqual(decode_session(client.string)[:3], (2, "149.154.167.51", 443))
        self.assertEqual(stored(self.home)["session"], client.string)
        self.assertIn("• tlg: logged in as Example User", self.lines())

    def test_browser_when_telegram_web_refuses_the_key(self):
        store(self.home, **CREDENTIALS)
        with mock.patch("flnd.sources.tlg.read_telegram_web", lambda: web_storage(2)), \
                self.assertRaisesRegex(LoginError, "Telegram Web's login no longer works"):
            self.log_in("browser")
        self.assertNotIn("session", stored(self.home))

    def test_browser_without_telegram_web(self):
        store(self.home, **CREDENTIALS)
        with mock.patch("flnd.sources.tlg.read_telegram_web", lambda: {}), \
                self.assertRaisesRegex(LoginError, "not logged in to Telegram Web"):
            self.log_in("browser")

    def test_qr_and_phone_need_a_terminal_and_browser_needs_stored_credentials(self):
        store(self.home, **CREDENTIALS)
        for method in ("qr", "phone"):
            with self.subTest(method=method), self.assertRaisesRegex(
                    LoginError, f"the {method} login asks questions; run flnd login in a terminal"):
                self.log_in(method)
        self.assertEqual(self.telegram.clients, [])
        with auth.secrets(self.home, "tlg") as secrets:
            secrets.clear()
        with self.assertRaisesRegex(LoginError, "no api_id and api_hash yet; run flnd login in a terminal"):
            self.log_in("browser")

    def test_logged_in_is_a_stored_session(self):
        telegram = Telegram()
        session = FakeSession(Target("tlg", "account", ""))
        self.assertFalse(telegram.logged_in(session))
        session.secrets["session"] = SESSION
        self.assertTrue(telegram.logged_in(session))


class LoginCommandTest(FlndCase):
    def choose_telegram(self, *keys):
        """Pick Telegram, the last source, in `flnd login`'s list, then type `keys`."""
        self.keys(*["down"] * list(SOURCES).index("tlg"), "enter", *keys)

    def test_the_source_then_the_method_is_asked(self):
        methods = []
        self.choose_telegram("down", "enter")
        with mock.patch.object(Telegram, "login", lambda self, session, method: methods.append(method)):
            self.assertEqual(self.run_app("login")[0], 0)
        self.assertEqual(methods, ["qr"])
        out = self.screen.out.getvalue()
        self.assertIn("✓ Log in to: Telegram (tlg)", out)
        self.assertIn("✓ Log in to Telegram: With a QR code", out)

    def test_a_failed_login_is_reported(self):
        def refuse(self, session, method):
            raise LoginError("tlg: no QR code was scanned; run flnd login again")

        self.choose_telegram("down", "enter")
        with mock.patch.object(Telegram, "login", refuse):
            code, _, out = self.run_app("login")
        self.assertEqual(code, 1)
        self.assertIn("✗ tlg: no QR code was scanned; run flnd login again", out)

    def test_a_stored_session_is_marked_logged_in(self):
        with auth.secrets(Path(".flnd"), "tlg") as secrets:
            secrets.update(session=SESSION, **CREDENTIALS)
        self.choose_telegram("enter")
        with mock.patch.object(Telegram, "login", lambda self, session, method: None):
            self.run_app("login")
        self.assertIn("Telegram (tlg) · logged in", self.screen.out.getvalue())


class DownloadCase(LoginCase):
    def setUp(self):
        super().setUp()
        store(self.home, session=SESSION, **CREDENTIALS)
        self.telegram.authorized = True
        self.folder = self.target_dir("tlg", "examplechannel") / "Example_Channel"

    def download(self, *targets, **options):
        return self.run_engine(Telegram, targets=list(targets or ["tlg:@examplechannel"]), options=Options(**options))


class DownloadTest(DownloadCase):
    def setUp(self):
        super().setUp()
        self.telegram.messages[1001] = [
            message(11, "photo"), message(12, "photo"), message(13, "video", content=b"video"),
            message(14, "file", name="notes.pdf"), message(5, "photo", day=datetime.date(2025, 12, 1)),
        ]

    def test_only_the_wanted_kinds_are_asked_for_and_since_stops_early(self):
        total = self.download(categories=("photo", "file"), since=datetime.date(2026, 1, 1))
        self.assertEqual(total, 3)
        self.assertEqual(self.telegram.asked, [("photo", None), ("file", None)])
        # The December photo is the last one asked for: older than --since, it ends the photos.
        self.assertEqual(self.telegram.yielded, 4)
        self.assertEqual(sorted(path.relative_to(self.folder).as_posix() for path in self.folder.rglob("*.*")),
                         ["file/2026-01-05_14_notes.pdf", "photo/2026-01-05_11.jpg", "photo/2026-01-05_12.jpg"])

    def test_until_asks_from_the_next_day(self):
        self.download(until=datetime.date(2026, 1, 4))
        midnight = datetime.datetime(2026, 1, 5, tzinfo=datetime.timezone.utc)
        self.assertEqual(self.telegram.asked, [("photo", midnight), ("video", midnight), ("file", midnight)])
        self.assertEqual(self.telegram.downloads, [5])

    def test_without_from_every_kind_and_a_second_run_downloads_nothing(self):
        self.assertEqual(self.download(), 5)
        self.assertEqual([kind for kind, _ in self.telegram.asked], ["photo", "video", "file"])
        self.assertEqual((self.folder / "video" / "2026-01-05_13.mp4").read_bytes(), b"video")
        self.assertEqual(self.download(), 0)
        self.assertEqual(len(self.telegram.downloads), 5)
        # One client per run, closed at its end, on the group's own loop.
        self.assertEqual([client.connected for client in self.telegram.clients], [False, False])
        self.assertEqual(self.telegram.threads, {"flnd-tlg"})
        self.assertEqual(self.telegram.clients[0].flood_sleep_threshold, 24 * 3600)

    def test_a_flood_wait_is_waited_out_and_retried(self):
        self.telegram.messages[1001] = [message(11, "photo")]
        self.telegram.download_errors = [FloodWaitError(None, capture=3)]
        self.assertEqual(self.download(), 1)
        self.assertEqual(self.now, 3)
        self.assertTrue((self.folder / "photo" / "2026-01-05_11.jpg").is_file())

    def test_not_logged_in_fails_the_target(self):
        with auth.secrets(self.home, "tlg") as secrets:
            secrets.clear()
        self.assertEqual(self.download(), 0)
        self.assertIn("✗ tlg:examplechannel: not logged in; run flnd login", self.lines())
        self.assertEqual(self.telegram.clients, [])

    def test_many_files_on_the_async_loop(self):
        # More files than the download pool queues: saving from the loop must not wait on downloads that need it.
        self.telegram.messages[1001] = [message(number, "photo") for number in range(1, 31)]
        finished = []
        runner = threading.Thread(target=lambda: finished.append(self.download()), daemon=True)
        runner.start()
        runner.join(timeout=10)
        self.assertEqual(finished, [30])


class ChatTest(DownloadCase):
    def test_by_title_and_by_id(self):
        self.telegram.messages[1001] = [message(11, "photo")]
        self.assertEqual(self.download("tlg:example channel"), 1)
        by_title = self.target_dir("tlg", fmt.filename("example channel"))
        self.assertTrue((by_title / "Example_Channel" / "photo" / "2026-01-05_11.jpg").is_file())
        self.assertEqual(self.download("tlg:-1001001"), 0)
        self.assertIn("• skip Example_Channel/photo/2026-01-05_11.jpg", self.lines())

    def test_an_ambiguous_title_fails_listing_ids(self):
        other = chat(1002, "Example Channel")
        self.telegram.dialogs.append(SimpleNamespace(id=-1001002, name="Example Channel", entity=other))
        self.assertEqual(self.download("tlg:Example Channel"), 0)
        self.assertIn("✗ tlg:Example Channel: 2 chats are titled 'Example Channel': -1001001, -1001002; "
                      "pass one as tlg:<id>", self.lines())

    def test_a_chat_that_isnt_there(self):
        self.assertEqual(self.download("tlg:nochat"), 0)
        self.assertIn("✗ tlg:nochat: no chat 'nochat' among your chats", self.lines())


def zipped(name, text):
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as archive:
        archive.writestr(name, text)
    return packed.getvalue()


class ArchiveTest(DownloadCase):
    def test_an_archive_sent_as_a_file_is_saved_as_sent(self):
        # flnd no longer unpacks or zips (--unpack and --zip are gone): the archive stays as Telegram sent it.
        packed = zipped("inside.txt", "hello")
        self.telegram.messages[1001] = [message(20, "file", name="set.zip", content=packed)]
        self.assertEqual(self.download(categories=("file",)), 1)
        self.assertEqual([path.name for path in (self.folder / "file").iterdir()], ["2026-01-05_20_set.zip"])
        self.assertEqual((self.folder / "file" / "2026-01-05_20_set.zip").read_bytes(), packed)
        self.assertFalse(self.target_dir("tlg", "examplechannel.zip").exists())


class WithOtherSourcesTest(DownloadCase):
    def test_both_groups_run_at_once_telegram_on_its_loop(self):
        both = threading.Barrier(2, timeout=5)
        self.telegram.messages[1001] = [message(11, "photo")]
        original = FakeClient.iter_messages

        def iter_messages(client, entity, **options):
            if not self.telegram.asked:
                both.wait()
            return original(client, entity, **options)

        other = make_source("aaa", lambda session: both.wait())
        with mock.patch.object(FakeClient, "iter_messages", iter_messages):
            engine = Engine(home=self.home, sources={"tlg": Telegram, "aaa": other})
            total = engine.run(engine.resolve(["tlg:@examplechannel", "aaa:a"]), Options())
        self.assertEqual(total, 1)
        self.assertFalse(both.broken)


if __name__ == "__main__":
    unittest.main()
