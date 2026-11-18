"""Telegram on plugin API 2: targets, chats, listings, downloads and logins, on a fake Telethon.

`FakeTelegram` stands in for Telegram's side (chats, messages, files, a login's answers); the
plugin runs for real on `flnd.testing.FakeCtx`. `NamesTest` pins every file name and key against
tests/data/tlg_names.json, so the port renames no saved file.
"""

import asyncio
import base64
import datetime
import io
import json
import shutil
import struct
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakes import EngineCase, make_plugin
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl import types
from test_commands import FlndCase

from flnd.engine import home as _home
from flnd.engine import jobs
from flnd.engine.core import Engine
from flnd.engine.events import Failed
from flnd.engine.options import Options
from flnd.plugin import Hidden, LoginError, NetworkError, NotFound, Part, Range, UsageError
from flnd.plugins import tlg
from flnd.plugins.tlg import FLOOD_SLEEP, Telegram, from_web_storage, half_blocks
from flnd.testing import FakeCtx, check, collect
from flnkit import text as _text

DAY = datetime.date(2026, 1, 5)
SESSION = "1FAKE-SESSION-aGVsbG8"
CREDENTIALS = {"api_id": "12345", "api_hash": "0123456789abcdef0123456789abcdef"}
KIND_OF_FILTER = {"InputMessagesFilterPhotos": "photo", "InputMessagesFilterVideo": "video",
                  "InputMessagesFilterDocument": "file"}


def channel(number, title, username=None, access_hash=77):
    return types.Channel(id=number, title=title, photo=types.ChatPhotoEmpty(), date=None,
                         access_hash=access_hash, username=username)


def message(number, kind, *, day=DAY, name=None, content=b"media"):
    ext = {"photo": ".jpg", "video": ".mp4"}.get(kind, "")
    when = datetime.datetime.combine(day, datetime.time(12), tzinfo=datetime.UTC)
    file = SimpleNamespace(name=name, ext=ext or (name and "." + name.rsplit(".", 1)[-1]),
                           size=len(content))
    # A video or file is a document, which `iter_download` reads; a photo isn't.
    document = None if kind == "photo" else SimpleNamespace(id=number, size=len(content),
                                                            content=content)
    return SimpleNamespace(id=number, date=when, kind=kind, file=file, content=content,
                           document=document)


def web_storage(dc=2, key=bytes(range(256)), *, quoted=True):
    """Telegram Web's local storage: JSON values, as both its web clients write them."""
    value = json.dumps if quoted else str
    return {"dc": value(dc), f"dc{dc}_auth_key": value(key.hex()),
            "user_auth": json.dumps({"dcID": dc, "id": "1"}), "tt-global-state": "{}"}


def decode_session(string):
    # A StringSession: "1", then base64 of dc (1 byte), IPv4 (4), port (2) and the 256-byte key.
    dc, ip, port, key = struct.unpack(">B4sH256s", base64.urlsafe_b64decode(string[1:]))
    return dc, ".".join(map(str, ip)), port, key


class FakeTelegram:
    """Telegram's side; `client` stands in for `TelegramClient`, and records what it was asked."""

    def __init__(self):
        self.authorized, self.password, self.wrong_code = True, None, False
        self.me = types.User(id=1, first_name="Example", last_name="User",
                             username="example_user")
        self.chats = {"examplechannel": channel(1001, "Example Channel", "examplechannel")}
        self.dialogs = [SimpleNamespace(id=-1001001, name="Example Channel",
                                        entity=self.chats["examplechannel"])]
        self.messages = {1001: []}
        self.clients, self.calls, self.reads, self.qr = [], [], [], []
        # Errors each call meets first, by its name; a listing's, after that many messages.
        self.errors: dict[str, list] = {}
        self.list_errors = []
        # The chunks (counted from 1 over the run) a flood wait comes before, mid-download, and
        # those the connection drops before; and the messages listings gave.
        self.chunks, self.floods, self.drops, self.yielded = 0, set(), set(), 0

    def client(self, string, api_id, api_hash):
        made = FakeClient(self, string)
        self.clients.append(made)
        return made


class FakeClient:
    def __init__(self, telegram, string):
        self.telegram, self.string, self.connected = telegram, string, False
        self.session = SimpleNamespace(save=lambda: self.string)
        # Not Telethon's 60, so a client the plugin didn't set is told apart.
        self.flood_sleep_threshold = 1

    def _call(self, name):
        self.telegram.calls.append(name)
        errors = self.telegram.errors.get(name)
        if errors:
            raise errors.pop(0)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return bool(self.string) and self.telegram.authorized

    async def get_me(self):
        return self.telegram.me if await self.is_user_authorized() else None

    async def qr_login(self):
        return FakeQR(self)

    async def send_code_request(self, phone):
        self.telegram.phone = phone

    async def sign_in(self, phone=None, code=None, *, password=None):
        if password is not None and password != self.telegram.password:
            raise SessionPasswordNeededError(None)
        if password is None and self.telegram.wrong_code:
            raise PhoneCodeInvalidError(None)
        if password is None and self.telegram.password:
            raise SessionPasswordNeededError(None)
        self.telegram.authorized, self.string = True, SESSION

    async def get_entity(self, value):
        self._call("get_entity")
        if isinstance(value, types.InputPeerChannel):
            found = next((chat for chat in self.telegram.chats.values()
                          if chat.id == value.channel_id), None)
        else:
            found = self.telegram.chats.get(str(value).lstrip("@"))
        if found is None:
            raise ValueError(f'No user has "{value}" as username')
        return found

    async def get_dialogs(self):
        self._call("get_dialogs")
        return self.telegram.dialogs

    async def iter_messages(self, entity, *, filter=None, offset_date=None, offset_id=0):
        kind = KIND_OF_FILTER[type(filter).__name__]
        self.telegram.calls.append(("list", kind, offset_date, offset_id))
        listed = 0
        for item in sorted(self.telegram.messages[entity.id], key=lambda item: item.date,
                           reverse=True):
            if (offset_id and item.id >= offset_id) or item.kind != kind or (
                    offset_date is not None and item.date >= offset_date):
                continue
            if self.telegram.list_errors and self.telegram.list_errors[0][0] == listed:
                raise self.telegram.list_errors.pop(0)[1]
            listed += 1
            self.telegram.yielded += 1
            yield item

    async def iter_download(self, document, *, offset, limit, request_size, file_size):
        self._call("iter_download")
        self.telegram.reads.append((offset, limit))
        # A download that asks again for ever fails here, instead of hanging the test.
        assert len(self.telegram.reads) <= 50, "more than 50 download requests"
        end = min(len(document.content), offset + limit * request_size)
        for start in range(offset, end, request_size):
            self.telegram.chunks += 1
            if self.telegram.chunks in self.telegram.floods:
                raise FloodWaitError(None, capture=300)
            if self.telegram.chunks in self.telegram.drops:
                raise ConnectionError("the connection dropped")
            yield document.content[start:start + request_size]
            await asyncio.sleep(0)

    async def download_media(self, item, file=None):
        self._call("download_media")
        assert file is bytes
        return item.content


class FakeQR:
    def __init__(self, client):
        self.client, self.number = client, 1

    @property
    def url(self):
        return f"tg://login?token=token{self.number}"

    async def wait(self):
        outcome = self.client.telegram.qr.pop(0)
        if outcome == "timeout":
            raise TimeoutError()
        if outcome == "password":
            raise SessionPasswordNeededError(None)
        self.client.telegram.authorized, self.client.string = True, SESSION

    async def recreate(self):
        self.number += 1


class Case(unittest.TestCase):
    def setUp(self):
        self.telegram = FakeTelegram()
        patcher = mock.patch.object(tlg, "_client", self.telegram.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ctx(self, *, logged_in=True, account="example_user", state=None, **options):
        """A context whose account logged in, as the engine's would, from Telegram Web."""
        ctx = FakeCtx(Telegram, account=account, state=state, answers=CREDENTIALS.values(),
                      storage=web_storage(), **options)
        if logged_in:
            asyncio.run(Telegram.login.start(ctx, "browser"))
        return ctx

    def run_target(self, text="tlg:@examplechannel", ctx=None, **options):
        return collect(Telegram, text, ctx=ctx or self.ctx(), **options)

    def ids(self, found):
        return [item.post.id for item in found]


# ---------------------------------------------------------------------------------------------
# Targets and declarations
# ---------------------------------------------------------------------------------------------

ROUTES = [
    ("tlg:@examplechannel", "examplechannel"), ("tlg:examplechannel", "examplechannel"),
    ("tlg:Lens Club", "Lens Club"), ("tlg:  Lens Club  ", "Lens Club"),
    ("tlg:-1001234567890", "-1001234567890"),
    ("tlg:a/b", "a/b"), ("tlg:https://example.com/x", "https://example.com/x"),
    ("https://t.me/examplechannel", "examplechannel"), ("t.me/examplechannel", "examplechannel"),
    ("tlg:https://t.me/examplechannel", "examplechannel"),
    ("https://www.t.me/example_channel", "example_channel"),
    ("https://t.me/s/examplechannel", "examplechannel"),
    ("https://t.me/examplechannel/123?single", "examplechannel"),
    ("https://t.me/c/1234567890/55", "-1001234567890"),
    ("https://t.me/+AbCdEf123", "+AbCdEf123"), ("https://t.me/joinchat/AbCdEf123", "+AbCdEf123"),
    # `joinchat` alone is 8 letters, so it reads as a username, as it always has.
    ("https://t.me/joinchat", "joinchat"),
    # Read by no route: the engine refuses them.
    ("tlg:", None), ("tlg:   ", None), ("tlg:@", None), ("tlg:@bad name", None),
    ("https://t.me/", None), ("https://t.me/s/", None), ("https://t.me/c/abc", None),
    ("https://t.me/abc", None), ("https://t.me/1abcd", None), ("https://t.me/ex-ample", None),
    ("https://x.t.me/examplechannel", None), ("https://telegram.org/examplechannel", None),
    ("https://web.telegram.org/", None),
]


class DeclarationsTest(unittest.TestCase):
    def test_every_form(self):
        for given, value in ROUTES:
            with self.subTest(text=given):
                found = Telegram.resolve(given)
                self.assertEqual(found and (found.kind, found.value),
                                 value and ("chat", value))

    def test_declarations_and_examples(self):
        self.assertEqual((Telegram.categories, Telegram.forms[0]),
                         (("photo", "video", "file"), "tlg:<@username, chat title or id>"))
        self.assertEqual((Telegram.folder, Telegram.key, dict(Telegram.limits)),
                         ("{chat}/{category}", "{chat_id}:{post}", {"t.me": 3}))
        self.assertEqual((Telegram.login.methods, Telegram.login.required),
                         (("browser", "qr", "phone"), True))
        (chat,) = Telegram.routes
        self.assertEqual([(c.name, c.own, c.newest_first) for c in chat.categories],
                         [("photo", True, True), ("video", True, True), ("file", True, True)])
        self.assertEqual(len(check(Telegram)), 5)


# ---------------------------------------------------------------------------------------------
# File names and keys, pinned
# ---------------------------------------------------------------------------------------------

def day(year, month, number):
    return datetime.date(year, month, number)


def names_messages():
    """Names that stress the file name: separators, dots, reserved names, 300 letters, none."""
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


NAMES_CHATS = {
    "plain": channel(1001, "Example Channel", "examplechannel"),
    "slash": channel(1002, "Lens/Club: 2026?"),
    "person": types.User(id=1003, first_name="Ex", last_name="User", access_hash=7),
    "user": types.User(id=1004, username="only_user", access_hash=7),
    "bare": types.User(id=1005, access_hash=7),
    "dots": channel(1006, ".."),
    "big": channel(1234567890123, "Big Chat"),
    "double": channel(1007, "a//b  c"),
    "reserved": channel(1008, "NUL"),
    "neg": channel(-1001234567890, "Negative"),
}
NAMES_OPTIONS = {
    "all": {}, "photo": {"categories": ("photo",)},
    "video-file": {"categories": ("video", "file")},
    "since": {"when": Range(day(2026, 1, 1))}, "until": {"when": Range(None, day(2026, 1, 4))},
    "range": {"when": Range(day(2026, 1, 1), day(2026, 1, 5))},
}


class NamesTest(Case):
    def test_every_path_and_key_is_what_api_1_wrote(self):
        golden = json.loads((Path(__file__).parent / "data" / "tlg_names.json").read_text("utf-8"))
        self.assertEqual(len(golden), len(NAMES_CHATS) * len(NAMES_OPTIONS))
        for chat_name, entity in NAMES_CHATS.items():
            for options_name, options in NAMES_OPTIONS.items():
                self.telegram.chats = {"chatone": entity}
                self.telegram.messages = {entity.id: names_messages()}
                when = options.get("when", Range())
                with self.subTest(chat=chat_name, options=options_name):
                    found = self.run_target("tlg:@chatone", ctx=self.ctx(when=when),
                                            categories=options.get("categories", "all"))
                    self.assertEqual([[item.path, item.key] for item in found],
                                     golden[f"{chat_name}/{options_name}"])


# ---------------------------------------------------------------------------------------------
# Finding a chat
# ---------------------------------------------------------------------------------------------

class ChatTest(Case):
    def setUp(self):
        super().setUp()
        self.telegram.messages[1001] = [message(11, "photo")]

    def test_how_each_value_is_found_or_refused(self):
        left = types.ChannelForbidden(id=1009, access_hash=5, title="Left Chat")
        twins = [SimpleNamespace(id=-1001002, name="Twin", entity=channel(1002, "Twin")),
                 SimpleNamespace(id=-1001003, name="Twin", entity=channel(1003, "Twin"))]
        self.telegram.dialogs += [*twins, SimpleNamespace(id=-1001009, name="Left Chat",
                                                         entity=left)]
        self.telegram.errors["get_entity"] = []
        table = [
            ("tlg:example channel", ["11"], ["get_dialogs"]),
            ("tlg:-1001001", ["11"], ["get_entity", "get_dialogs"]),
            ("tlg:@examplechannel", ["11"], ["get_entity"]),
            ("tlg:Twin", (UsageError, "2 chats are titled 'Twin': -1001002, -1001003"),
             ["get_entity", "get_dialogs"]),
            ("tlg:nochat", (NotFound, "no chat 'nochat' among your chats"),
             ["get_entity", "get_dialogs"]),
            ("tlg:-1009999", (Hidden, "the chat -1009999 isn't among your chats"),
             ["get_entity", "get_dialogs"]),
            ("tlg:Left Chat", (Hidden, "the chat Left Chat is private"), ["get_dialogs"]),
            ("https://t.me/+AbCdEf123", (NotFound, r"the invite '\+AbCdEf123' isn't one"),
             ["get_entity"]),
        ]
        for text, expected, calls in table:
            with self.subTest(text=text):
                self.telegram.calls = []
                if isinstance(expected, list):
                    self.assertEqual(self.ids(self.run_target(text, categories=["photo"])),
                                     expected)
                else:
                    with self.assertRaisesRegex(expected[0], expected[1]):
                        self.run_target(text)
                self.assertEqual([call for call in self.telegram.calls
                                  if isinstance(call, str)], calls)

    def test_a_private_chat_is_hidden_wherever_telegram_says_so(self):
        for where in ("get_entity", "list"):
            with self.subTest(where=where):
                if where == "list":
                    self.telegram.list_errors = [(0, ChannelPrivateError(None))]
                else:
                    self.telegram.errors["get_entity"] = [ChannelPrivateError(None)]
                with self.assertRaisesRegex(Hidden, "is private, or this account left it"):
                    self.run_target()

    def test_a_found_chat_is_cached_per_account_and_checked_again(self):
        state = {}
        self.run_target("tlg:Example Channel", ctx=self.ctx(state=state))
        self.assertEqual(state["chats.example_user.example channel"],
                         {"_": "InputPeerChannel", "channel_id": 1001, "access_hash": 77})
        cases = [("example_user", "Example Channel", ["get_entity"]),
                 # Another account's access hash is no use to this one.
                 ("example_creator", "Example Channel", ["get_dialogs"]),
                 # A chat renamed since no longer answers to the cached title.
                 ("example_creator", "Renamed", ["get_entity", "get_dialogs"])]
        for account, title, calls in cases:
            with self.subTest(account=account, title=title):
                self.telegram.chats["examplechannel"].title = title
                self.telegram.dialogs[0].name = title
                self.telegram.calls = []
                ctx = self.ctx(state=state, account=account)
                if title == "Renamed":
                    with self.assertRaises(NotFound):
                        self.run_target("tlg:Example Channel", ctx=ctx)
                else:
                    self.run_target("tlg:Example Channel", ctx=ctx, categories=["photo"])
                self.assertEqual([c for c in self.telegram.calls if isinstance(c, str)], calls)
        # Each account keeps its own entry: neither evicts the other.
        self.assertEqual(sorted(state), ["chats.example_creator.example channel",
                                         "chats.example_user.example channel"])


# ---------------------------------------------------------------------------------------------
# Listing and flood waits
# ---------------------------------------------------------------------------------------------

class ListingTest(Case):
    def test_until_asks_from_the_next_day_and_categories_list_their_kind(self):
        self.telegram.messages[1001] = [message(11, "photo"), message(13, "video"),
                                        message(5, "photo", day=day(2025, 12, 1))]
        found = self.run_target(ctx=self.ctx(when=Range(None, day(2026, 1, 4))))
        self.assertEqual(self.ids(found), ["5"])
        midnight = datetime.datetime(2026, 1, 5, tzinfo=datetime.UTC)
        self.assertEqual([call[1:3] for call in self.telegram.calls if call[0] == "list"],
                         [("photo", midnight), ("video", midnight), ("file", midnight)])

    def test_a_flood_wait_holds_then_the_listing_goes_on_after_the_last_message(self):
        self.telegram.messages[1001] = [message(n, "photo", day=day(2026, 1, n))
                                        for n in (11, 12, 13)]
        self.telegram.list_errors = [(2, FloodWaitError(None, capture=600))]
        ctx = self.ctx()
        self.assertEqual(self.ids(self.run_target(ctx=ctx, categories=["photo"])),
                         ["13", "12", "11"])
        self.assertEqual(ctx.seen.holds, [("Telegram flood wait", 600, None)])
        self.assertEqual([call[3] for call in self.telegram.calls if call[0] == "list"],
                         [0, 12])

    def test_the_client_sleeps_only_short_waits_and_is_closed_at_the_end(self):
        self.run_target()
        client = self.telegram.clients[-1]
        self.assertEqual((client.flood_sleep_threshold, client.connected), (60, False))

    def test_a_missing_or_revoked_login(self):
        for logged_in, error in ((False, "tlg: not logged in"),
                                 (True, "the saved login no longer works")):
            with self.subTest(error=error):
                ctx = self.ctx(logged_in=logged_in)
                # Revoked elsewhere after the login.
                self.telegram.authorized = not logged_in
                with self.assertRaisesRegex(LoginError, error):
                    self.run_target(ctx=ctx)


# ---------------------------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------------------------

class FetchTest(Case):
    def files(self, *messages):
        self.telegram.messages[1001] = list(messages)
        return {item.post.id: item.file for item in self.run_target(categories="all")}

    def fetch(self, file, offset=0, end=None):
        written = bytearray()

        async def sink(data):
            written.extend(data)

        asyncio.run(file.fetch(Part(sink, offset=offset, end=end)))
        return bytes(written)

    def test_a_document_is_ranged_and_each_part_reads_from_an_aligned_request(self):
        body = bytes(range(40))
        file = self.files(message(21, "video", content=body))["21"]
        self.assertEqual((file.ranged, file.size), (True, 40))
        with mock.patch.object(tlg, "REQUEST", 8):
            for offset, end, reads in ((0, None, [(0, 5)]), (13, 30, [(8, 3)]),
                                       (16, 17, [(16, 1)]), (35, None, [(32, 1)])):
                with self.subTest(offset=offset, end=end):
                    self.telegram.reads = []
                    self.assertEqual(self.fetch(file, offset, end), body[offset:end])
                    self.assertEqual(self.telegram.reads, reads)

    def test_a_flood_wait_holds_and_the_part_goes_on_from_the_bytes_written(self):
        body = bytes(range(40))
        # (offset, end, the chunks a flood wait comes before): before the first, or mid-part.
        table = [(3, None, {1}), (0, None, {2}), (0, 20, {2}), (13, 30, {2, 4})]
        with mock.patch.object(tlg, "REQUEST", 8):
            for offset, end, floods in table:
                with self.subTest(offset=offset, end=end, floods=floods):
                    # A run of its own: its holds are its own block rounds.
                    file = self.files(message(21, "video", content=body))["21"]
                    self.telegram.chunks, self.telegram.floods = 0, set(floods)
                    self.assertEqual(self.fetch(file, offset, end), body[offset:end])

    def test_parts_meeting_one_flood_wait_share_one_hold(self):
        ctx = self.ctx()
        self.telegram.messages[1001] = [message(21, "video", content=bytes(64))]
        (found,) = collect(Telegram, "tlg:@examplechannel", ctx=ctx, categories=["video"])
        self.telegram.errors["iter_download"] = [FloodWaitError(None, capture=300)
                                                 for _ in range(4)]

        async def sink(data):
            pass

        async def parts():
            await asyncio.gather(*(found.file.fetch(Part(sink, offset=n * 16, end=n * 16 + 16))
                                   for n in range(4)))

        with mock.patch.object(tlg, "REQUEST", 8):
            asyncio.run(parts())
        self.assertEqual(ctx.seen.holds, [("Telegram flood wait", 300, None)])

    def test_nothing_sent_is_a_network_error_to_try_again(self):
        file = self.files(message(21, "file", name="a.zip", content=bytes(8)))["21"]
        file.fetch.args[2].size = 16
        with self.assertRaisesRegex(NetworkError, "sent nothing from byte 8 of 16"):
            self.fetch(file, 8)

    def test_a_photo_comes_whole_and_resumes_past_what_the_part_holds(self):
        file = self.files(message(11, "photo", content=b"photo-bytes"))["11"]
        self.assertFalse(file.ranged)
        self.telegram.errors["download_media"] = [FloodWaitError(None, capture=90)]
        self.assertEqual(self.fetch(file), b"photo-bytes")
        self.assertEqual(self.fetch(file, 6), b"bytes")


# ---------------------------------------------------------------------------------------------
# Logging in
# ---------------------------------------------------------------------------------------------

class LoginTest(Case):
    def log_in(self, method, *answers, asked=True, storage=None):
        """Log in by `method`; `asked` answers the API pair first, as a first login asks it."""
        credentials = tuple(CREDENTIALS.values()) if asked else ()
        self.last = ctx = FakeCtx(Telegram, answers=(*credentials, *answers), storage=storage)
        asyncio.run(Telegram.login.start(ctx, method))
        return ctx

    def assert_no_secret(self, ctx, error=None):
        """No secret reaches a log line or an error: a password, the api_hash, a session."""
        said = [text for _, text in ctx.seen.logs]
        if error is not None:
            said += [str(error), error.hint]
        secrets = {"two-step password": "hunter2", "wrong two-step password": "hunter3",
                   "api_hash": CREDENTIALS["api_hash"], "session": SESSION,
                   **{f"stored {name}": value for name, value in ctx.secrets.items()
                      if name != "api_id"}}
        # Named by its label, so a failure never prints the secret.
        for label, secret in secrets.items():
            self.assertFalse(any(secret in text for text in said), label)

    def assert_logged_in(self, ctx):
        self.assertEqual(dict(ctx.secrets), {"session": SESSION, **CREDENTIALS})
        self.assertIn(("info", "logged in as Example User"), ctx.seen.logs)
        self.assertFalse(any(client.connected for client in self.telegram.clients))
        self.assert_no_secret(ctx)

    def test_phone_and_qr(self):
        self.telegram.authorized, self.telegram.password = False, "hunter2"
        ctx = self.log_in("phone", *CREDENTIALS.values(), "+15550100", "12345", "hunter2",
                          asked=False)
        self.assert_logged_in(ctx)
        for outcomes, answers in ((["timeout", "ok"], ()), (["password"], ("hunter2",))):
            with self.subTest(qr=outcomes):
                self.telegram.qr = list(outcomes)
                ctx = self.log_in("qr", *answers)
                self.assert_logged_in(ctx)
                shown = [text for level, text in ctx.seen.logs if "Link Desktop Device" in text]
                self.assertEqual(len(shown), outcomes.count("timeout") + 1)
                self.assertIn("█", shown[0])

    def test_refusals_say_no_secret(self):
        self.telegram.authorized, self.telegram.password = False, "hunter2"
        self.telegram.qr = ["timeout"] * tlg.QR_CODES
        # (method, whether the API pair is answered, the answers after it, the error).
        table = [
            ("qr", True, (), LoginError, "no QR code was scanned"),
            ("phone", True, ("+15550100", "12345", "hunter3"), LoginError,
             "Telegram refused the login"),
            # The api_hash pasted where the api_id goes is not echoed back.
            ("phone", False, (CREDENTIALS["api_hash"],), LoginError, "api_id is a number"),
            ("phone", False, (), UsageError, "nobody can answer"),
        ]
        for method, asked, answers, error, message in table:
            with self.subTest(method=method, error=message):
                with self.assertRaisesRegex(error, message) as caught:
                    self.log_in(method, *answers, asked=asked)
                self.assert_no_secret(self.last, caught.exception)
        self.telegram.password, self.telegram.wrong_code = None, True
        with self.assertRaisesRegex(LoginError, "Telegram refused the login"):
            self.log_in("phone", "+15550100", "99999")

    def test_browser_from_telegram_web(self):
        ctx = self.log_in("browser", storage=web_storage(2))
        self.assertEqual(decode_session(ctx.secrets["session"])[:3], (2, "149.154.167.51", 443))
        self.telegram.authorized = False
        with self.assertRaisesRegex(LoginError, "Telegram Web's login no longer works"):
            self.log_in("browser", storage=web_storage(2))
        with self.assertRaisesRegex(LoginError, "not logged in to Telegram Web"):
            self.log_in("browser", storage={})

    def test_check_and_whoami(self):
        ctx = self.ctx(logged_in=False)
        self.assertEqual(asyncio.run(Telegram.login.check(ctx)), False)
        self.assertIsNone(asyncio.run(Telegram.login.whoami(ctx)))
        ctx = self.ctx()
        self.assertEqual(asyncio.run(Telegram.login.check(ctx)), True)
        self.assertEqual(asyncio.run(Telegram.login.whoami(ctx)), "example_user")

    def test_web_storage_and_the_qr_drawing(self):
        for quoted in (True, False):
            with self.subTest(quoted=quoted):
                self.assertEqual(decode_session(from_web_storage(web_storage(4, quoted=quoted))),
                                 (4, "149.154.167.91", 443, bytes(range(256))))
        for storage in ({}, {"dc": "2"}, {**web_storage(), "dc2_auth_key": json.dumps("x")}):
            with self.subTest(storage=list(storage)), self.assertRaises(LoginError):
                from_web_storage(storage)
        matrix = [[False, True, False], [False, False, True], [True, True, False]]
        self.assertEqual(half_blocks(matrix), "█▄▀\n▄▄█")


# ---------------------------------------------------------------------------------------------
# Through the engine: the login in the home, the files saved, other plugins beside it
# ---------------------------------------------------------------------------------------------

def store(home, **values):
    with _home.secrets(home, "tlg") as secrets:
        secrets.clear()
        secrets.update(values)


def stored(home):
    with _home.secrets(home, "tlg") as secrets:
        return {name: value for name, value in secrets.items() if name != "origin"}


class TelegramCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.telegram = FakeTelegram()
        self.enterContext(mock.patch.object(tlg, "_client", self.telegram.client))

    def fresh(self, **side):
        """A new Telegram side, with `side` set on it, and an empty screen."""
        vars(self.telegram).update(vars(FakeTelegram()), **side)
        self.dest.seek(0)
        self.dest.truncate()


class EngineLoginTest(TelegramCase):
    """`flnd login tlg <method>` through the engine: what it asks, and what the home keeps."""

    def log_in(self, method, *answers, tty=True):
        with mock.patch("sys.stdin.isatty", return_value=tty), \
                mock.patch("flnd.ui.forms.ask", side_effect=list(answers)):
            Engine(home=self.home, sources={"tlg": Telegram}).login("tlg", method)

    def assert_logged_in(self):
        self.assertEqual(stored(self.home), {"session": SESSION, **CREDENTIALS})
        self.assertIn("* tlg: logged in as Example User", self.lines())
        screen = self.dest.getvalue()
        # Named by its label, so a failure never prints the secret.
        for label, secret in (("password", "hunter2"), ("api_hash", CREDENTIALS["api_hash"]),
                              ("session", SESSION)):
            self.assertFalse(secret in screen, label)
        # Kept in secret.db only: no Telethon .session file, no other file holding them.
        for path in self.home.rglob("*"):
            if path.is_file() and path.name != "secret.db":
                data = path.read_bytes()
                self.assertFalse(b"FAKE-SESSION" in data
                                 or CREDENTIALS["api_hash"].encode() in data, path.name)
        self.assertEqual(list(self.home.rglob("*.session")), [])
        self.assertFalse(any(client.connected for client in self.telegram.clients))

    def test_phone_and_qr(self):
        table = [
            # (method, stored first, Telegram's side, the answers)
            ("phone", {}, {"password": "hunter2"},
             (*CREDENTIALS.values(), "+15550100", "12345", "hunter2")),
            # A stored API pair isn't asked again, and the new session replaces the old.
            ("phone", {"session": "old", **CREDENTIALS}, {}, ("+15550100", "12345")),
            ("qr", {}, {"qr": ["timeout", "ok"]}, tuple(CREDENTIALS.values())),
            ("qr", {}, {"qr": ["password"], "password": "hunter2"},
             (*CREDENTIALS.values(), "hunter2")),
        ]
        for method, first, side, answers in table:
            with self.subTest(method=method, side=side):
                store(self.home, **first)
                self.fresh(authorized=False, **side)
                codes = side.get("qr", []).count("timeout") + (method == "qr")
                self.log_in(method, *answers)
                self.assert_logged_in()
                drawn = self.dest.getvalue().count("Settings → Devices → Link Desktop Device")
                self.assertEqual(drawn, codes)

    def test_a_refused_login_keeps_nothing(self):
        table = [("qr", {"qr": ["timeout"] * tlg.QR_CODES}, (), "no QR code was scanned"),
                 ("phone", {"wrong_code": True}, ("+15550100", "99999"),
                  "Telegram refused the login")]
        for method, side, answers, error in table:
            with self.subTest(method=method):
                store(self.home)
                self.fresh(authorized=False, **side)
                with self.assertRaisesRegex(LoginError, error):
                    self.log_in(method, *CREDENTIALS.values(), *answers)
                self.assertEqual(stored(self.home), {})

    def test_browser_from_telegram_web(self):
        table = [(False, web_storage(2), "Telegram Web's login no longer works"),
                 (True, {}, "not logged in to Telegram Web"),
                 (True, web_storage(2), None)]
        for authorized, storage, error in table:
            with self.subTest(error=error):
                store(self.home, **CREDENTIALS)
                self.fresh(authorized=authorized)

                async def read(ctx, storage=storage):
                    return storage

                with mock.patch.object(tlg, "read_telegram_web", read):
                    if error is not None:
                        with self.assertRaisesRegex(LoginError, error):
                            self.log_in("browser")
                        self.assertEqual(stored(self.home), CREDENTIALS)
                        continue
                    self.log_in("browser")
                session = stored(self.home)["session"]
                self.assertEqual(decode_session(session)[:3], (2, "149.154.167.51", 443))
                self.assertIn("* tlg: logged in as Example User", self.lines())

    def test_without_a_terminal_a_first_login_asks_nobody(self):
        for method in Telegram.login.methods:
            with self.subTest(method=method), \
                    self.assertRaisesRegex(UsageError, "nobody can answer"):
                self.log_in(method, tty=False)
        self.assertEqual((self.telegram.clients, stored(self.home)), ([], {}))


class LoginCommandTest(FlndCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict("flnd.engine.core.SOURCES", {"tlg": Telegram}))

    def test_the_way_is_named_or_asked_and_a_refusal_is_reported(self):
        from prompt_lines import select_from_lines

        methods = []

        async def start(ctx, method):
            methods.append(method)
            if method == "browser":
                raise LoginError("tlg: no QR code was scanned", hint="flnd auth login tlg qr")

        # On the login object: `FakeCtx` gives it a `start` of its own, over the class's.
        with mock.patch.object(Telegram.login, "start", start):
            code, _, err = self.run_app("login", "tlg", "qr")
            self.assertEqual(code, 0, err)
            with select_from_lines("3"):
                code, _, err = self.run_app("login", "tlg")
            self.assertEqual(code, 0, err)
            code, _, err = self.run_app("login", "tlg", "browser")
        self.assertEqual(methods, ["qr", "phone", "browser"])
        self.assertEqual(code, 3)
        self.assertIn("x tlg: no QR code was scanned", err)

    def test_a_stored_session_is_listed_as_logged_in(self):
        store(Path(".flnd"), session=SESSION, **CREDENTIALS)
        code, out, _ = self.run_app("login")
        self.assertEqual(code, 0)
        row = next(line for line in out.splitlines() if line.startswith("tlg "))
        self.assertIn("logged in", row)


def zipped(name, text):
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as archive:
        archive.writestr(name, text)
    return packed.getvalue()


class EngineDownloadTest(TelegramCase):
    """Downloads through the engine: its listing, its stops, its saves and its retries."""

    def setUp(self):
        super().setUp()
        self.logged_in()
        self.folder = self.target_dir("tlg", "examplechannel") / "Example_Channel"
        self.telegram.messages[1001] = [
            message(11, "photo"), message(12, "photo"), message(13, "video", content=b"video"),
            message(14, "file", name="notes.pdf"), message(5, "photo", day=day(2025, 12, 1)),
        ]

    def logged_in(self):
        store(self.home, session=SESSION, **CREDENTIALS)

    def download(self, *targets, **options):
        return self.run_engine(Telegram, targets=list(targets or ["tlg:@examplechannel"]),
                               options=Options(**options))

    def saved(self, folder=None):
        folder = folder or self.folder
        return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*.*"))

    def downloads(self):
        return [call for call in self.telegram.calls if call in ("download_media",
                                                                 "iter_download")]

    def test_categories_and_dates_choose_what_is_listed_and_saved(self):
        old = [message(n, "photo", day=day(2025, 1, 1)) for n in range(3, 13)]
        chat = self.telegram.messages[1001]
        table = [
            # (options, the chat's messages (None: setUp's), what is saved, kinds asked,
            #  messages listed)
            ({"categories": ("photo", "file"), "since": day(2026, 1, 1)}, None,
             ["file/2026-01-05_14_notes.pdf", "photo/2026-01-05_11.jpg",
              "photo/2026-01-05_12.jpg"], ["photo", "file"], 4),
            # The engine stops a kind after 5 older messages in a row, not at the first.
            ({"categories": ("photo",), "since": day(2026, 1, 1)},
             [message(1, "photo"), message(2, "photo"), *old],
             ["photo/2026-01-05_1.jpg", "photo/2026-01-05_2.jpg"], ["photo"], 7),
            ({"until": day(2026, 1, 4)}, None, ["photo/2025-12-01_5.jpg"],
             ["photo", "video", "file"], 1),
        ]
        for options, messages, files, kinds, listed in table:
            with self.subTest(options=options):
                shutil.rmtree(self.home)
                self.logged_in()
                self.telegram.messages[1001] = messages or chat
                self.telegram.calls, self.telegram.yielded = [], 0
                self.assertEqual(self.download(**options), len(files))
                self.assertEqual(self.saved(), files)
                self.assertEqual([call[1] for call in self.telegram.calls if call[0] == "list"],
                                 kinds)
                self.assertEqual(self.telegram.yielded, listed)

    def test_every_kind_once_and_a_second_run_downloads_nothing(self):
        self.assertEqual(self.download(), 5)
        self.assertEqual((self.folder / "video" / "2026-01-05_13.mp4").read_bytes(), b"video")
        self.assertEqual(self.download(), 0)
        self.assertEqual(len(self.downloads()), 5)
        # One client per run, closed at its end, sleeping through short flood waits only.
        self.assertEqual([client.connected for client in self.telegram.clients], [False, False])
        self.assertEqual(self.telegram.clients[0].flood_sleep_threshold, FLOOD_SLEEP)

    def test_a_file_that_drops_on_every_try_fails_alone(self):
        self.telegram.messages[1001] = [message(22, "video", content=bytes(40))]
        self.telegram.drops = set(range(1, 100))
        with mock.patch.object(tlg, "REQUEST", 8):
            self.assertEqual(self.download(categories=("video",)), 0)
        # The scheduler's first try of the file, and one more per network wait.
        self.assertEqual(len(self.telegram.reads), 1 + len(jobs.NETWORK_WAITS))
        self.assertEqual(len(self.failures()), 1)

    def test_a_big_document_comes_in_parts_at_once(self):
        body = bytes(range(64)) * 2
        self.telegram.messages[1001] = [message(23, "file", name="big.zip", content=body)]
        with mock.patch.object(tlg, "REQUEST", 8), mock.patch.object(jobs, "SPLIT_FILE", 64), \
                mock.patch.object(jobs, "SPLIT_RANGE", 32):
            self.assertEqual(self.download(categories=("file",)), 1)
        self.assertEqual((self.folder / "file" / "2026-01-05_23_big.zip").read_bytes(), body)
        self.assertEqual(sorted(self.telegram.reads), [(0, 4), (32, 4), (64, 4), (96, 4)])

    def test_an_archive_is_kept_as_sent_and_extracted_beside_itself(self):
        packed = zipped("inside.txt", "hello")
        self.telegram.messages[1001] = [message(20, "file", name="set.zip", content=packed)]
        self.assertEqual(self.download(categories=("file",)), 1)
        self.assertEqual(sorted(path.name for path in (self.folder / "file").iterdir()),
                         ["2026-01-05_20_set", "2026-01-05_20_set.zip"])
        self.assertEqual((self.folder / "file" / "2026-01-05_20_set.zip").read_bytes(), packed)
        self.assertEqual((self.folder / "file" / "2026-01-05_20_set" / "inside.txt")
                         .read_text(), "hello")

    def test_not_logged_in_fails_the_target(self):
        store(self.home)
        self.assertEqual(self.download(), 0)
        self.assertEqual(self.failures(), ["tlg:examplechannel: not logged in"])
        self.assertEqual(self.telegram.clients, [])

    def test_many_files_on_the_runs_loop(self):
        # More files than the download pool queues: a save from the loop never waits on a
        # download that needs the loop.
        self.telegram.messages[1001] = [message(number, "photo") for number in range(1, 31)]
        finished = []
        runner = threading.Thread(target=lambda: finished.append(self.download()), daemon=True)
        runner.start()
        runner.join(timeout=10)
        self.assertEqual(finished, [30])

    def test_a_chat_by_title_and_by_id_shares_its_keys(self):
        self.telegram.messages[1001] = [message(11, "photo")]
        self.assertEqual(self.download("tlg:example channel"), 1)
        by_title = self.target_dir("tlg", _text.filename("example channel")) / "Example_Channel"
        self.assertEqual(self.saved(by_title), ["photo/2026-01-05_11.jpg"])
        self.assertEqual(self.download("tlg:-1001001"), 0)
        # The id's run finds the photo saved under the title, so it counts it skipped.
        [row] = [row for row in self.states[-1].targets if row.text == "tlg:-1001001"]
        self.assertEqual((row.skipped, row.saved), (1, 0))

    def test_a_chat_that_cant_be_told_fails_its_target_with_the_reason(self):
        twin = SimpleNamespace(id=-1001002, name="Example Channel",
                               entity=channel(1002, "Example Channel"))
        self.telegram.dialogs.append(twin)
        for text, error in [
            ("tlg:Example Channel",
             "2 chats are titled 'Example Channel': -1001001, -1001002"),
            ("tlg:nochat", "no chat 'nochat' among your chats"),
        ]:
            with self.subTest(text=text):
                self.events.clear()
                self.assertEqual(self.download(text), 0)
                self.assertEqual(self.failures(), [f"{text}: {error}"])
            if "titled" in error:
                [failed] = [event for event in self.events if isinstance(event, Failed)]
                self.assertEqual(failed.error.hint, "pass one as tlg:<id>")

    def test_telegram_runs_beside_another_plugin_on_the_runs_loop(self):
        order = []
        self.telegram.messages[1001] = [message(11, "photo")]
        original = FakeClient.iter_messages

        def iter_messages(client, entity, **options):
            order.append("tlg")
            return original(client, entity, **options)

        other = make_plugin("aaa", lambda ctx: order.append("aaa"))
        with mock.patch.object(FakeClient, "iter_messages", iter_messages):
            total = self.run_engine(Telegram, other, targets=["tlg:@examplechannel", "aaa:a"])
        self.assertEqual(total, 1)
        self.assertEqual(sorted(set(order)), ["aaa", "tlg"])


if __name__ == "__main__":
    unittest.main()
