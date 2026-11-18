"""Telegram: targets, logins, and downloads through the engine, on a fake Telethon client. No network.

It fakes Telegram's side only (`FakeTelegram`, `FakeClient`: chats, messages, a login's answers) and
the clock the engine waits on; the route and categories run for real, on the source's own loop.
`NamesTest` pins every file name and history key against tests/data/tlg_names.json, which the
`run(session)` version produced for the same fixtures, so the port can never rename a saved file.
"""

import asyncio
import base64
import datetime
import inspect
import io
import json
import struct
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from fakes import FakeSite, Seen, collect
from playwright.sync_api import Error as PlaywrightError
from telethon.errors import FloodWaitError, PhoneCodeInvalidError, SessionPasswordNeededError
from test_commands import FlndCase, Stdin
from test_engine import EngineCase, make_source

from flnd import _home, _transfer
from flnd._events import Progress
from flnd.engine import Engine
from flnd.kit import text as _text
from flnd.options import Options
from flnd.site import LoginSite
from flnd.source import LoginError, Target, UsageError
from flnd.sources import tlg
from flnd.sources.tlg import (
    FLOOD_SLEEP,
    QR_CODES,
    Telegram,
    TelegramLogin,
    fetcher,
    from_web_storage,
    half_blocks,
    read_telegram_web,
)

DAY = datetime.date(2026, 1, 5)
SESSION = "1FAKE-SESSION-aGVsbG8"
CREDENTIALS = {"api_id": "12345", "api_hash": "0123456789abcdef0123456789abcdef"}
# Which kind of message each of Telegram's server-side filters returns.
KIND_OF_FILTER = {"InputMessagesFilterPhotos": "photo", "InputMessagesFilterVideo": "video",
                  "InputMessagesFilterDocument": "file"}
ROUTES = [
    ("tlg:@examplechannel", ("chat", "examplechannel")),
    ("tlg:examplechannel", ("chat", "examplechannel")),
    ("tlg:Lens Club", ("chat", "Lens Club")),
    ("tlg:  Lens Club  ", ("chat", "Lens Club")),
    ("tlg:-1001234567890", ("chat", "-1001234567890")),
    ("tlg:a/b", ("chat", "a/b")),
    ("tlg:https://example.com/x", ("chat", "https://example.com/x")),
    ("tlg:", ("chat", "")),
    ("tlg:   ", ("chat", "")),
    ("https://t.me/examplechannel", ("chat", "examplechannel")),
    ("t.me/examplechannel", ("chat", "examplechannel")),
    ("tlg:https://t.me/examplechannel", ("chat", "examplechannel")),
    ("https://www.t.me/examplechannel", ("chat", "examplechannel")),
    ("https://t.me/example_channel", ("chat", "example_channel")),
    ("https://t.me/s/examplechannel", ("chat", "examplechannel")),
    ("https://t.me/examplechannel/123", ("chat", "examplechannel")),
    ("https://t.me/examplechannel?single", ("chat", "examplechannel")),
    ("https://t.me/c/1234567890/55", ("chat", "-1001234567890")),
    ("https://t.me/c/1234567890", ("chat", "-1001234567890")),
    ("https://t.me/+AbCdEf123", ("chat", "+AbCdEf123")),
    ("https://t.me/+", ("chat", "+")),
    ("https://t.me/joinchat/AbCdEf123", ("chat", "+AbCdEf123")),
    # `joinchat` alone is 8 letters, so it reads as a username, as it always has.
    ("https://t.me/joinchat", ("chat", "joinchat")),
    ("https://t.me/", "can't read 'https://t.me/'"),
    ("https://t.me/s/", "can't read 'https://t.me/s/'"),
    ("https://t.me/c/abc", "can't read 'https://t.me/c/abc'"),
    ("https://t.me/abc", "can't read 'https://t.me/abc'"),
    ("https://t.me/1abcd", "can't read 'https://t.me/1abcd'"),
    ("https://t.me/ex-ample", "can't read 'https://t.me/ex-ample'"),
    ("https://x.t.me/examplechannel", "can't read 'https://x.t.me/examplechannel'"),
    ("https://telegram.org/examplechannel", "can't read 'https://telegram.org/examplechannel'"),
    ("https://web.telegram.org/", "can't read 'https://web.telegram.org/'"),
    ("tlg:@", "can't read '@'"),
    ("tlg:@bad name", "can't read '@bad name'"),
]


def message(number, kind, *, day=DAY, name=None, content=b"media", size=None):
    ext = {"photo": ".jpg", "video": ".mp4"}.get(kind, "")
    when = datetime.datetime.combine(day, datetime.time(12), tzinfo=datetime.UTC)
    file = SimpleNamespace(name=name, ext=ext or (name and "." + name.rsplit(".", 1)[-1]), size=size or len(content))
    # A video or file is a document, which `iter_download` fetches; a photo isn't.
    document = None if kind == "photo" else SimpleNamespace(id=number, size=len(content),
                                                            content=content)
    return SimpleNamespace(id=number, date=when, kind=kind, file=file, content=content,
                           document=document)


def chat(number, title, username=None):
    return SimpleNamespace(id=number, title=title, username=username)


class FakeTelegram:
    """Telegram's side: the account's chats and how a login goes. `client` stands in for `TelegramClient`."""

    def __init__(self):
        self.authorized = False
        self.password: str | None = None
        self.phone: str | None = None
        self.code: str | None = None
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
        # (messages listed before it, error): an error `iter_messages` meets after that many.
        self.list_errors = []
        self.threads = set()
        # Each `iter_download` asked for: (offset, requests); and the chunks after which the
        # connection drops, counted over the whole run.
        self.reads = []
        self.drops = set()
        self.chunks = 0
        # The flood waits Telethon slept through itself, in seconds.
        self.slept = []

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

    def _raise(self, error):
        """Raise `error` as Telethon would: a flood wait of `flood_sleep_threshold` seconds or less
        is slept through by Telethon itself (here, counted in `slept`) and never reaches the caller."""
        if isinstance(error, FloodWaitError) and error.seconds <= self.flood_sleep_threshold:
            self.telegram.slept.append(error.seconds)
            return
        raise error

    async def iter_messages(self, entity, *, filter=None, offset_date=None, offset_id=0):
        self.telegram.threads.add(threading.current_thread().name)
        kind = KIND_OF_FILTER[type(filter).__name__]
        self.telegram.asked.append((kind, offset_date))
        listed = 0
        for item in sorted(self.telegram.messages[entity.id], key=lambda item: item.date, reverse=True):
            # Telegram's `offset_id` lists the messages older than it, 0 from the newest.
            if offset_id and item.id >= offset_id:
                continue
            if item.kind == kind and (offset_date is None or item.date < offset_date):
                if self.telegram.list_errors and self.telegram.list_errors[0][0] == listed:
                    self._raise(self.telegram.list_errors.pop(0)[1])
                listed += 1
                self.telegram.yielded += 1
                yield item
            await asyncio.sleep(0)

    async def iter_download(self, document, *, offset=0, limit=None, request_size, file_size=None):
        if self.telegram.download_errors:
            self._raise(self.telegram.download_errors.pop(0))
        if offset == 0:
            self.telegram.downloads.append(document.id)
        self.telegram.reads.append((offset, limit))
        end = min(len(document.content), offset + limit * request_size)
        for start in range(offset, end, request_size):
            self.telegram.chunks += 1
            if self.telegram.chunks in self.telegram.drops:
                raise ConnectionError("the connection dropped")
            yield document.content[start:start + request_size]
            await asyncio.sleep(0)

    async def download_media(self, item, file=None, *, progress_callback=None):
        if self.telegram.download_errors:
            self._raise(self.telegram.download_errors.pop(0))
        self.telegram.downloads.append(item.id)
        if progress_callback is not None:
            progress_callback(len(item.content) // 2, len(item.content))
        assert file is not None
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
            raise TimeoutError()
        if outcome == "password":
            raise SessionPasswordNeededError(None)
        self.client._signed_in()

    async def recreate(self):
        self.number += 1


def store(home, **values):
    with _home.secrets(home, "tlg") as secrets:
        secrets.update(values)


def stored(home):
    with _home.secrets(home, "tlg") as secrets:
        return dict(secrets)


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for given, expected in ROUTES:
            with self.subTest(text=given):
                value = given.removeprefix("tlg:")
                if isinstance(expected, tuple):
                    kind, found = expected
                    self.assertEqual(Telegram().resolve(value), Target(f"tlg:{found}", kind=kind))
                else:
                    with self.assertRaisesRegex(UsageError, f"^tlg: {expected}"):
                        Telegram().resolve(value)

    def test_a_refusal_lists_the_shown_form(self):
        with self.assertRaises(UsageError) as caught:
            Telegram().resolve("@bad name")
        self.assertEqual(str(caught.exception), "tlg: can't read '@bad name'; write it like tlg:<@username, chat title or id>")


class ResolveTest(unittest.TestCase):
    def test_declarations(self):
        self.assertEqual((Telegram.categories, Telegram.single_kinds), (("photo", "video", "file"), ()))
        self.assertIsInstance(Telegram.login, TelegramLogin)
        self.assertEqual((Telegram.login.methods, Telegram.login.required), (("browser", "qr", "phone"), True))
        self.assertEqual((Telegram.folder, Telegram.key), ("{chat}/{category}", "{chat_id}:{post}"))
        # Downloads at once, counted against t.me: every one shares the one MTProto client.
        self.assertEqual(dict(Telegram.limits), {"t.me": 3})

    def test_it_imports_only_the_source_api_the_stdlib_and_its_sdk(self):
        import ast
        import sys

        with open(tlg.__file__, encoding="utf-8") as file:
            tree = ast.parse(file.read())
        names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                 for alias in node.names}
        names |= {node.module for node in ast.walk(tree)
                  if isinstance(node, ast.ImportFrom) and node.module}
        for name in names:
            with self.subTest(name=name):
                top = name.split(".")[0]
                self.assertTrue(name == "flnd.source" or top in sys.stdlib_module_names
                                or top in ("telethon", "qrcode"), name)

    def test_one_route_whose_categories_are_newest_first_async_methods(self):
        (route,) = Telegram.routes
        self.assertEqual([(c.name, c.newest_first) for c in route.categories],
                         [("photo", True), ("video", True), ("file", True)])
        self.assertTrue(inspect.iscoroutinefunction(route.function))


class FloodWaitTest(unittest.TestCase):
    def download(self, errors):
        """Run `fetcher`'s download against a client that raises `errors` first, with a site that records back-offs."""
        waits = []
        item = message(11, "photo")

        async def download_media(message, file=None, progress_callback=None):
            if errors:
                raise errors.pop(0)
            assert file is not None
            file.write(b"media")

        client = SimpleNamespace(download_media=download_media)
        site = SimpleNamespace(client=client, back_off=lambda reason, *, retry_after=None: waits.append(
            (reason, retry_after)))
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "file"
            asyncio.run(fetcher(cast(Any, site), item)(dest, lambda done, total: None))
            return waits, dest.read_bytes()

    def test_a_flood_wait_is_backed_off_for_the_seconds_it_names_and_the_download_tried_again(self):
        waits, content = self.download([FloodWaitError(None, capture=3), FloodWaitError(None, capture=7)])
        self.assertEqual(waits, [("Telegram flood wait", 3), ("Telegram flood wait", 7)])
        self.assertEqual(content, b"media")

    def test_another_error_is_not_waited_out(self):
        with self.assertRaises(OSError):
            self.download([OSError("reset")])

    def test_telethon_sleeps_only_short_waits_so_a_long_one_reaches_back_off(self):
        """Bug: `flood_sleep_threshold` was 24 hours, so Telethon slept through every flood wait
        itself and the handlers never ran. Telethon now sleeps 60 s or less; a longer wait reaches
        `site.back_off(retry_after=<its seconds>)`, where the person sees it and a stop ends it."""
        self.assertLessEqual(FLOOD_SLEEP, 60)
        telegram = FakeTelegram()
        telegram.authorized = True
        site = FakeSite(Target("tlg:", kind="account"))
        site.secrets.update(session=SESSION, **CREDENTIALS)

        async def connected():
            with mock.patch("flnd.sources.tlg._client", telegram.client):
                async with Telegram().connect(site) as client:
                    return client.flood_sleep_threshold

        self.assertEqual(asyncio.run(connected()), FLOOD_SLEEP)
        waits, content = self.download([FloodWaitError(None, capture=3600)])
        self.assertEqual((waits, content), ([("Telegram flood wait", 3600)], b"media"))

    def test_a_flood_wait_while_listing_goes_on_after_the_last_message(self):
        telegram = FakeTelegram()
        telegram.messages[1001] = [message(number, "photo", day=datetime.date(2026, 1, number))
                                   for number in (11, 12, 13)]
        telegram.list_errors = [(2, FloodWaitError(None, capture=600))]
        seen = Seen()
        saved = collect(Telegram, "tlg:@examplechannel", options=Options(categories=("photo",)),
                        client=telegram.client(SESSION, 1, "hash"), seen=seen)
        # Newest first, each message once, and the wait went to `back_off`.
        self.assertEqual([item.post.id for item in saved], ["13", "12", "11"])
        self.assertEqual(telegram.yielded, 3)
        self.assertEqual(seen.back_offs, ["Telegram flood wait"])


def web_storage(dc=2, key=bytes(range(256)), *, quoted=True):
    """A Telegram Web local-storage dump: values are JSON, as both web clients write them."""
    value = json.dumps if quoted else str
    return {"dc": value(dc), f"dc{dc}_auth_key": value(key.hex()), "dc1_auth_key": value("ab" * 256),
            "user_auth": json.dumps({"dcID": dc, "id": "1"}), "tt-global-state": "{}"}


def decode_session(string):
    # Telethon's StringSession: version "1", then base64 of dc (1 byte), IPv4 (4), port (2) and the 256-byte key.
    dc, ip, port, key = struct.unpack(">B4sH256s", base64.urlsafe_b64decode(string[1:]))
    return dc, ".".join(map(str, ip)), port, key


class FakeTab:
    """The tab `flnd.kit.browser.local_storage` opens: it lands on `url`, and `evaluate` answers its script."""

    def __init__(self, url, evaluate):
        async def evaluated(script):
            return evaluate(script)

        self.url, self.raw = url, SimpleNamespace(evaluate=evaluated)

    async def goto(self, url, **options):
        return None


class FakeChrome:
    def __init__(self, tab):
        self._tab = tab

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def tab(self, **options):
        return self._tab


class ReadTelegramWebTest(unittest.TestCase):
    def read(self, tab):
        async def launch(**options):
            return FakeChrome(tab)

        # The login site's own `storage`, which reads nothing of the site it's on.
        site = SimpleNamespace(storage=lambda url: LoginSite.storage(cast(Any, None), url))
        # Never the real browser: a real launch would copy the user's own Chrome profile.
        with mock.patch("flnd.kit.browser.launch", launch):
            return read_telegram_web(site)

    def test_the_storage_of_a_tab_that_reached_telegram_web(self):
        self.assertEqual(self.read(FakeTab("https://web.telegram.org/k/", lambda script: {"dc": "2"})), {"dc": "2"})

    def test_a_failed_navigation_is_a_login_error_not_a_traceback(self):
        def evaluate(script):
            raise AssertionError("a tab still on about:blank must not be read")

        with self.assertRaisesRegex(LoginError, "could not open Telegram Web.*try a QR code instead"):
            self.read(FakeTab("about:blank", evaluate))

    def test_a_playwright_error_while_reading_is_a_login_error(self):
        def evaluate(script):
            raise PlaywrightError("Execution context was destroyed")

        with self.assertRaisesRegex(LoginError, "can't read Telegram Web in Chrome.*try a QR code instead"):
            self.read(FakeTab("https://web.telegram.org/a/", evaluate))


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
        tty = mock.patch("sys.stdin.isatty", return_value=True)
        ask = mock.patch("flnd.ui.forms.ask", side_effect=list(answers))
        tty.start()
        ask.start()
        self.addCleanup(ask.stop)
        self.addCleanup(tty.stop)

    def log_in(self, method):  # type: ignore[override]
        Engine(home=self.home, sources={"tlg": Telegram}).login("tlg", method)

    def assert_logged_in(self):
        saved = stored(self.home)
        self.assertEqual({k: v for k, v in saved.items() if k != "origin"}, {"session": SESSION, **CREDENTIALS})
        self.assertIn("* tlg: logged in as Example User", self.lines())
        # The session and the hash are stored in secret.db: no telethon .session file is left.
        for path in self.home.rglob("*"):
            if path.is_file() and path.name != "secret.db":
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
        screen = self.dest.getvalue()
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
        out = self.dest.getvalue()
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
        with self.assertRaisesRegex(LoginError, "no QR code was scanned; run flnd login tlg qr again"):
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
        with mock.patch("flnd.sources.tlg.read_telegram_web", lambda site: web_storage(2)):
            self.log_in("browser")
        client = self.telegram.clients[0]
        self.assertEqual(decode_session(client.string)[:3], (2, "149.154.167.51", 443))
        self.assertEqual(stored(self.home)["session"], client.string)
        self.assertIn("* tlg: logged in as Example User", self.lines())

    def test_browser_when_telegram_web_refuses_the_key(self):
        store(self.home, **CREDENTIALS)
        with mock.patch("flnd.sources.tlg.read_telegram_web", lambda site: web_storage(2)), \
                self.assertRaisesRegex(LoginError, "Telegram Web's login no longer works"):
            self.log_in("browser")
        self.assertNotIn("session", stored(self.home))

    def test_browser_without_telegram_web(self):
        store(self.home, **CREDENTIALS)
        with mock.patch("flnd.sources.tlg.read_telegram_web", lambda site: {}), \
                self.assertRaisesRegex(LoginError, "not logged in to Telegram Web"):
            self.log_in("browser")

    def test_qr_and_phone_need_a_terminal_and_browser_needs_stored_credentials(self):
        store(self.home, **CREDENTIALS)
        for method in ("qr", "phone"):
            with self.subTest(method=method), \
                    mock.patch("sys.stdin.isatty", return_value=False), \
                    self.assertRaisesRegex(
                        LoginError, f"the {method} login asks questions; run flnd login tlg {method} in a terminal"):
                self.log_in(method)
        self.assertEqual(self.telegram.clients, [])
        with _home.secrets(self.home, "tlg") as secrets:
            secrets.clear()
        with mock.patch("sys.stdin.isatty", return_value=False), \
                self.assertRaisesRegex(
                    LoginError, "no api_id and api_hash yet; run flnd login tlg in a terminal"):
            self.log_in("browser")

    def test_logged_in_is_a_stored_session(self):
        login = Telegram.login
        site = FakeSite(Target("tlg:", kind="account"))
        site.secrets.update(CREDENTIALS)
        # The API pair alone is no login.
        self.assertFalse(login.check(site))
        site.secrets["session"] = SESSION
        self.assertTrue(login.check(site))


class LoginCommandTest(FlndCase):
    def test_the_way_is_named_or_asked(self):
        from prompt_lines import select_from_lines

        methods = []
        with mock.patch.object(TelegramLogin, "start", lambda self, site, how: methods.append(how)):
            self.assertEqual(self.run_app("login", "tlg", "qr")[0], 0)
            self.stdin.stop()
            self.stdin = mock.patch("sys.stdin", Stdin("", tty=True))
            self.stdin.start()
            with select_from_lines("3"):
                code, _, err = self.run_app("login", "tlg")
        self.assertEqual(code, 0, err)
        self.assertEqual(methods, ["qr", "phone"])

    def test_a_failed_login_is_reported(self):
        def refuse(self, site, how):
            raise LoginError("tlg: no QR code was scanned; run flnd login tlg qr again")

        with mock.patch.object(TelegramLogin, "start", refuse):
            code, _, out = self.run_app("login", "tlg", "qr")
        self.assertEqual(code, 3)
        self.assertIn("error: tlg: no QR code was scanned; run flnd login tlg qr again", out)

    def test_a_stored_session_is_listed_as_logged_in(self):
        with _home.secrets(Path(".flnd"), "tlg") as secrets:
            secrets.update(session=SESSION, **CREDENTIALS)
        code, out, _ = self.run_app("login")
        self.assertEqual(code, 0)
        row = next(line for line in out.splitlines() if line.startswith("tlg "))
        self.assertIn("logged in", row)


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

    def test_only_the_wanted_kinds_are_asked_for_and_since_filters(self):
        total = self.download(categories=("photo", "file"), since=datetime.date(2026, 1, 1))
        self.assertEqual(total, 3)
        self.assertEqual(self.telegram.asked, [("photo", None), ("file", None)])
        # The December photo is read and dropped by the engine, which stops only after 5 old ones in a row.
        self.assertEqual(self.telegram.yielded, 4)
        self.assertEqual(sorted(path.relative_to(self.folder).as_posix() for path in self.folder.rglob("*.*")),
                         ["file/2026-01-05_14_notes.pdf", "photo/2026-01-05_11.jpg", "photo/2026-01-05_12.jpg"])

    def test_since_stops_reading_a_kind_after_five_older_messages_in_a_row(self):
        old = datetime.date(2025, 1, 1)
        self.telegram.messages[1001] = [message(1, "photo"), message(2, "photo"),
                                        *[message(number, "photo", day=old) for number in range(3, 13)]]
        self.assertEqual(self.download(categories=("photo",), since=datetime.date(2026, 1, 1)), 2)
        # The two new photos and 5 old ones, not the 10 old ones the chat holds.
        self.assertEqual(self.telegram.yielded, 7)

    def test_until_asks_from_the_next_day(self):
        self.download(until=datetime.date(2026, 1, 4))
        midnight = datetime.datetime(2026, 1, 5, tzinfo=datetime.UTC)
        self.assertEqual(self.telegram.asked, [("photo", midnight), ("video", midnight), ("file", midnight)])
        self.assertEqual(self.telegram.downloads, [5])

    def test_without_from_every_kind_and_a_second_run_downloads_nothing(self):
        self.assertEqual(self.download(), 5)
        self.assertEqual([kind for kind, _ in self.telegram.asked], ["photo", "video", "file"])
        self.assertEqual((self.folder / "video" / "2026-01-05_13.mp4").read_bytes(), b"video")
        self.assertEqual(self.download(), 0)
        self.assertEqual(len(self.telegram.downloads), 5)
        # One client per run, closed at its end, on the run's loop.
        self.assertEqual([client.connected for client in self.telegram.clients], [False, False])
        self.assertEqual(self.telegram.threads, {"flnd-loop"})
        self.assertEqual(self.telegram.clients[0].flood_sleep_threshold, FLOOD_SLEEP)

    def test_a_flood_wait_is_waited_out_and_retried(self):
        self.telegram.messages[1001] = [message(11, "photo")]
        self.telegram.download_errors = [FloodWaitError(None, capture=120)]
        self.assertEqual(self.download(), 1)
        self.assertEqual((self.now, self.telegram.slept), (120, []))
        self.assertTrue((self.folder / "photo" / "2026-01-05_11.jpg").is_file())

    def test_a_short_flood_wait_is_telethons_own(self):
        self.telegram.messages[1001] = [message(11, "photo")]
        self.telegram.download_errors = [FloodWaitError(None, capture=FLOOD_SLEEP)]
        self.assertEqual(self.download(), 1)
        self.assertEqual((self.now, self.telegram.slept), (0, [FLOOD_SLEEP]))

    def test_a_video_goes_on_from_where_a_flood_wait_or_a_drop_stopped_it(self):
        body = bytes(range(40))
        self.telegram.messages[1001] = [message(21, "video", content=body)]
        # A flood wait first, then the connection drops at the third and the sixth chunk.
        self.telegram.download_errors = [FloodWaitError(None, capture=300)]
        self.telegram.drops = {3, 6}
        with mock.patch.object(tlg, "REQUEST", 8):
            self.assertEqual(self.download(categories=("video",)), 1)
        self.assertEqual(self.now, 300)
        self.assertEqual((self.folder / "video" / "2026-01-05_21.mp4").read_bytes(), body)
        # Each pass asks from the last whole chunk, never from 0 again.
        self.assertEqual(self.telegram.reads, [(0, 5), (16, 3), (32, 1)])

    def test_a_part_dropped_more_than_resumes_times_fails_the_file(self):
        self.telegram.messages[1001] = [message(22, "video", content=bytes(40))]
        self.telegram.drops = set(range(1, 100))
        with mock.patch.object(tlg, "REQUEST", 8):
            self.assertEqual(self.download(categories=("video",)), 0)
        # Each of the engine's tries of the file gives the part its own resumes.
        self.assertEqual(len(self.telegram.reads), _transfer.ATTEMPTS * (tlg.RESUMES + 1))

    def test_a_big_document_comes_in_parts_at_once(self):
        body = bytes(range(64)) * 2
        self.telegram.messages[1001] = [message(23, "file", name="big.zip", content=body)]
        with mock.patch.object(tlg, "REQUEST", 8), mock.patch.object(tlg, "SPLIT_SIZE", 64):
            self.assertEqual(self.download(categories=("file",)), 1)
        self.assertEqual((self.folder / "file" / "2026-01-05_23_big.zip").read_bytes(), body)
        self.assertEqual(sorted(self.telegram.reads), [(0, 4), (32, 4), (64, 4), (96, 4)])
        self.assertEqual(tlg.parts(10), [(0, 10)])

    def test_a_damaged_offsets_file_starts_the_download_over(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "video.mp4"
            dest.write_bytes(b"abc")
            sidecar = Path(f"{dest}.json")
            for damaged in ("[]", "7", '"text"', "{"):
                with self.subTest(damaged=damaged):
                    sidecar.write_text(damaged)
                    self.assertIsNone(tlg._load_offsets(dest, sidecar, 16))

    def test_the_offsets_file_never_counts_bytes_still_in_the_buffer(self):
        on_disk = []

        async def iter_download(document, **options):
            yield bytes(range(8))
            yield bytes(range(8, 16))

        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "video.mp4"
            dest.write_bytes(b"abc")
            sidecar = Path(f"{dest}.json")
            site = SimpleNamespace(client=SimpleNamespace(iter_download=iter_download))

            def save(path, data):
                # What a crash right now would leave: the file on disk against the offsets saved.
                on_disk.append((dest.stat().st_size, json.loads(data)["done"][0]))

            with open(dest, "r+b") as file, mock.patch.object(tlg, "REQUEST", 8), \
                    mock.patch.object(tlg, "_save", side_effect=save):
                download = tlg._Download(site, SimpleNamespace(size=16), file, lambda *_: None,
                                         sidecar=sidecar, done=[3])
                asyncio.run(download.run())
        self.assertTrue(all(size >= done for size, done in on_disk), on_disk)

    def test_not_logged_in_fails_the_target(self):
        with _home.secrets(self.home, "tlg") as secrets:
            secrets.clear()
        self.assertEqual(self.download(), 0)
        self.assertIn("tlg:examplechannel: not logged in", self.failures())
        self.assertEqual(self.telegram.clients, [])

    def test_many_files_on_the_async_loop(self):
        # More files than the download pool queues: saving from the loop must not wait on downloads that need it.
        self.telegram.messages[1001] = [message(number, "photo") for number in range(1, 31)]
        finished = []
        runner = threading.Thread(target=lambda: finished.append(self.download()), daemon=True)
        runner.start()
        runner.join(timeout=10)
        self.assertEqual(finished, [30])


def day(year, month, number):
    return datetime.date(year, month, number)


def names_messages():
    """Messages whose names stress the file name: separators, dots, reserved names, a 300-letter name, no name at all."""
    return [
        message(11, "photo"), message(12, "photo"), message(13, "photo", day=day(2026, 1, 4)),
        message(14, "video", content=b"v"), message(15, "video", day=day(2025, 12, 31)),
        message(16, "file", name="notes.pdf"), message(17, "file", name="a/b.pdf"),
        message(18, "file", name="../evil.txt"), message(19, "file", name="ünï cödé?.pdf"),
        message(20, "file", name="README"), message(21, "file", name="x" * 300 + ".zip"),
        message(22, "file", name="CON.txt"), message(23, "file", name="   "),
        message(24, "photo", day=day(2025, 12, 1)), message(10 ** 10, "video", day=day(2026, 1, 6)),
        message(25, "file", name="a:b*c.7z", day=day(2026, 1, 1)),
    ]


# Chats whose title, name or id stress the folder and the key: separators, dots, a reserved name, no title.
NAMES_CHATS = {
    "plain": chat(1001, "Example Channel", "examplechannel"),
    "slash": chat(1002, "Lens/Club: 2026?", None),
    "person": SimpleNamespace(id=1003, first_name="Ex", last_name="User", username=None),
    "user": SimpleNamespace(id=1004, username="only_user"),
    "bare": SimpleNamespace(id=1005),
    "dots": chat(1006, "..", None),
    "big": chat(1234567890123, "Big Chat", None),
    "double": chat(1007, "a//b  c", None),
    "reserved": chat(1008, "NUL", None),
    "neg": chat(-1001234567890, "Negative", None),
}
NAMES_OPTIONS = {
    "all": {},
    "photo": {"categories": ("photo",)},
    "video-file": {"categories": ("video", "file")},
    "since": {"since": day(2026, 1, 1)},
    "until": {"until": day(2026, 1, 4)},
    "range": {"since": day(2026, 1, 1), "until": day(2026, 1, 5)},
}


class NamesTest(unittest.TestCase):
    def test_every_path_and_key_is_what_run_session_wrote(self):
        golden = json.loads((Path(__file__).parent / "data" / "tlg_names.json").read_text(encoding="utf-8"))
        self.assertEqual(len(golden), len(NAMES_CHATS) * len(NAMES_OPTIONS))
        for chat_name, entity in NAMES_CHATS.items():
            for options_name, options in NAMES_OPTIONS.items():
                telegram = FakeTelegram()
                telegram.chats = {"chatone": entity}
                telegram.messages = {entity.id: names_messages()}
                with self.subTest(chat=chat_name, options=options_name):
                    saved = collect(Telegram, "tlg:@chatone", options=Options(**options),
                                    client=telegram.client("", 1, "hash"))
                    self.assertEqual([[item.path, item.key] for item in saved], golden[f"{chat_name}/{options_name}"])


class ChatTest(DownloadCase):
    def test_by_title_and_by_id(self):
        self.telegram.messages[1001] = [message(11, "photo")]
        self.assertEqual(self.download("tlg:example channel"), 1)
        by_title = self.target_dir("tlg", _text.filename("example channel"))
        self.assertTrue((by_title / "Example_Channel" / "photo" / "2026-01-05_11.jpg").is_file())
        self.assertEqual(self.download("tlg:-1001001"), 0)
        # The file is already saved under the title, so the id run's photo counts it skipped.
        steps = [event for event in self.events if isinstance(event, Progress)
                 and str(event.target) == "tlg:-1001001" and event.category == "photo"]
        self.assertEqual((steps[-1].skipped, steps[-1].saved), (1, 0))

    def test_an_ambiguous_title_fails_listing_ids(self):
        other = chat(1002, "Example Channel")
        self.telegram.dialogs.append(SimpleNamespace(id=-1001002, name="Example Channel", entity=other))
        self.assertEqual(self.download("tlg:Example Channel"), 0)
        self.assertEqual(self.failures(), ["tlg:Example Channel: 2 chats are titled "
                                           "'Example Channel': -1001001, -1001002"])
        self.assertEqual(self.events[-1].error.hint, "pass one as tlg:<id>")

    def test_a_chat_that_isnt_there(self):
        self.assertEqual(self.download("tlg:nochat"), 0)
        self.assertEqual(self.failures(), ["tlg:nochat: no chat 'nochat' among your chats"])


def zipped(name, text):
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as archive:
        archive.writestr(name, text)
    return packed.getvalue()


class ArchiveTest(DownloadCase):
    def test_an_archive_sent_as_a_file_is_kept_as_sent_and_extracted_beside_itself(self):
        packed = zipped("inside.txt", "hello")
        self.telegram.messages[1001] = [message(20, "file", name="set.zip", content=packed)]
        self.assertEqual(self.download(categories=("file",)), 1)
        self.assertEqual(sorted(path.name for path in (self.folder / "file").iterdir()),
                         ["2026-01-05_20_set", "2026-01-05_20_set.zip"])
        self.assertEqual((self.folder / "file" / "2026-01-05_20_set.zip").read_bytes(), packed)
        self.assertEqual((self.folder / "file" / "2026-01-05_20_set" / "inside.txt").read_text(), "hello")
        self.assertFalse(self.target_dir("tlg", "examplechannel.zip").exists())


class WithOtherSourcesTest(DownloadCase):
    def test_groups_run_sequentially_with_telegram_on_its_loop(self):
        order = []
        self.telegram.messages[1001] = [message(11, "photo")]
        original = FakeClient.iter_messages

        def iter_messages(client, entity, **options):
            if not self.telegram.asked:
                order.append("tlg")
            return original(client, entity, **options)

        other = make_source("aaa", lambda site: order.append("aaa"))
        with mock.patch.object(FakeClient, "iter_messages", iter_messages):
            total = self.run_engine(Telegram, other, targets=["tlg:@examplechannel", "aaa:a"])
        self.assertEqual(total, 1)
        self.assertEqual(order, ["tlg", "aaa"])


if __name__ == "__main__":
    unittest.main()
