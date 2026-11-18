"""Telegram (`tlg`): a chat's photos, videos and files, through Telethon on the run's loop.

Telethon and qrcode are imported inside functions. src/docs/plugins/tlg explains it.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from flnd.plugin import (
    Browser,
    Ctx,
    Error,
    File,
    Hidden,
    Login,
    LoginError,
    NetworkError,
    NotFound,
    Part,
    Plugin,
    Post,
    Target,
    UsageError,
    route,
)

TELEGRAM_WEB = "https://web.telegram.org/"
# Telegram's production data centers, as every client ships them, and the port Telethon uses.
DATA_CENTERS = {
    1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100", 4: "149.154.167.91",
    5: "91.108.56.130",
}
PORT = 443
# A Telegram username: 4 to 32 characters, starting with a letter.
USERNAME = r"[A-Za-z]\w{3,31}"
# A QR login token lasts about 30 seconds; this many are shown before giving up.
QR_CODES = 5
# The longest flood wait Telethon sleeps through by itself (its own default); a longer one is
# raised as `FloodWaitError` and becomes `ctx.hold`. A shorter one held there would spend a
# block round (at least 30 s) on a wait of a few seconds.
FLOOD_SLEEP = 60
FLOOD_WAIT = "Telegram flood wait"
# Telegram's server-side filter for each category, so it sends only those messages.
FILTERS = {
    "photo": "InputMessagesFilterPhotos", "video": "InputMessagesFilterVideo",
    "file": "InputMessagesFilterDocument",
}
# Telegram's largest file request; a request may not cross a 1 MiB boundary (seen 2026-11-13),
# so each one starts on a multiple of it.
REQUEST = 512 * 1024
# The peers a cached chat may be, as Telethon names their types.
PEERS = ("InputPeerChannel", "InputPeerChat", "InputPeerUser")
# One character per pair of QR modules (top, bottom), where True is a light module.
HALF_BLOCKS = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}


# ---------------------------------------------------------------------------------------------
# Logging in
# ---------------------------------------------------------------------------------------------

class TelegramLogin(Login):
    """Telegram's own login: a Telethon session, kept with the API pair in the account's secrets.

    `browser` turns Telegram Web's login into a session and adds no device; `qr` and `phone`
    log in a new device named flnd. The first login asks for the `api_id` and `api_hash`.
    """

    methods = ("browser", "qr", "phone")
    # Where `browser` reads the login from; it opens no window of its own.
    url = TELEGRAM_WEB

    async def start(self, ctx: Ctx, method: str) -> None:
        """Log in by `method`, and keep the session, the `api_id` and the `api_hash`.

        Raises:
            LoginError: Telegram refused, no QR code was scanned, or Telegram Web isn't
                logged in.
            UsageError: a question has nobody to answer it (no terminal).
        """
        api_id, api_hash = await credentials(ctx)
        start = from_web_storage(await read_telegram_web(ctx)) if method == "browser" else ""
        string, name = await log_in(ctx, method, start, api_id, api_hash)
        ctx.secrets.update(session=string, api_id=str(api_id), api_hash=api_hash)
        ctx.log("info", "logged in as {name}", name=name)

    async def check(self, ctx: Ctx) -> bool:
        """Whether a Telethon session is stored; the API pair alone is no login."""
        return "session" in ctx.secrets

    async def whoami(self, ctx: Ctx) -> str | None:
        """The account's username, else its name; None without a session that works."""
        secrets = ctx.secrets
        if "session" not in secrets:
            return None
        client = _client(secrets["session"], int(secrets["api_id"]), secrets["api_hash"])
        await client.connect()
        try:
            me = await client.get_me()
        finally:
            await client.disconnect()
        return None if me is None else (getattr(me, "username", None) or display_name(me))


# ---------------------------------------------------------------------------------------------
# The plugin
# ---------------------------------------------------------------------------------------------

class Telegram(Plugin):
    """Telegram chats, through one Telethon client per account on the run's loop."""

    code = "tlg"
    name = "Telegram"
    domains = ("t.me", "telegram.org")
    api = 2
    login = TelegramLogin()
    browser = Browser.NONE
    # A chat's name, then the category; the engine sanitizes both as file names.
    folder = "{chat}/{category}"
    fields = ("chat", "chat_id")
    # The chat's id and the message's id: a message id repeats from chat to chat.
    key = "{chat_id}:{post}"
    # Every download shares one MTProto client and its flood waits: more at once only meets the
    # flood limit sooner (Telethon's FAQ).
    limits = {"t.me": 3}

    def __init__(self) -> None:
        # The connected client of each account, by name ("" without one); and the flood wait
        # each account is in, shared by every listing and download that meets it.
        self._clients: dict[str, Any] = {}
        self._waits: dict[str, asyncio.Future[None]] = {}

    @route("tlg:{chat:[^@].*}", "tlg:@{chat:" + USERNAME + "}",
           # `t.me/c/<n>` holds a channel's id without the `-100` client APIs put before it,
           # `joinchat/` is the old invite form, and `s/` the web preview (seen 2026-11-11).
           ("t.me/c/{chat:\\d+}", "-100{chat}"), ("t.me/joinchat/{chat:[\\w-]+}", "+{chat}"),
           "t.me/s/{chat:" + USERNAME + "}", "t.me/{chat:\\+[\\w-]*}",
           "t.me/{chat:" + USERNAME + "}",
           shown="tlg:<@username, chat title or id>")
    async def chat(self, ctx: Ctx, chat: str) -> Mapping[str, Any]:
        """A chat by username, invite, id or title: `tlg:@examplechannel`,
        `https://t.me/examplechannel`, `https://t.me/+AbCdEf123`,
        `https://t.me/c/1234567890/55`, `tlg:-1001234567890` or `tlg:Example Channel`."""
        entity = await self._find(ctx, self._client(ctx), chat)
        return {"entity": entity, "chat": display_name(entity), "chat_id": entity.id}

    @classmethod
    def resolve(cls, text: str) -> Target | None:
        """Read `text` by the routes, the value after `tlg:` stripped as the engine strips it."""
        code, colon, value = text.strip().partition(":")
        if colon and code.lower() == cls.code:
            text = f"{cls.code}:{value.strip()}"
        return super().resolve(text)

    @chat.category
    async def photo(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The chat's photos, newest first."""
        async for post in self._messages(ctx, profile, "photo"):
            yield post

    @chat.category
    async def video(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The chat's videos, newest first."""
        async for post in self._messages(ctx, profile, "video"):
            yield post

    @chat.category
    async def file(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The chat's other files (documents, archives, audio), newest first."""
        async for post in self._messages(ctx, profile, "file"):
            yield post

    @asynccontextmanager
    async def connect(self, ctx: Ctx) -> AsyncIterator[None]:
        """Connect the account's Telethon client for the run, and disconnect it at the end.

        Raises:
            LoginError: no session is stored, or it no longer works (revoked elsewhere).
        """
        secrets, who = ctx.secrets, _who(ctx)
        if "session" not in secrets:
            raise LoginError("tlg: not logged in", hint="flnd auth login tlg")
        client = _client(secrets["session"], int(secrets["api_id"]), secrets["api_hash"])
        client.flood_sleep_threshold = FLOOD_SLEEP
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise LoginError("tlg: the saved login no longer works",
                                 hint="flnd auth login tlg")
            self._clients[who] = client
            yield
        # Disconnected even when the run stops early, so no connection outlives the run.
        finally:
            self._clients.pop(who, None)
            await client.disconnect()

    def _client(self, ctx: Ctx) -> Any:
        client = self._clients.get(_who(ctx))
        if client is None:
            raise LoginError("tlg: not logged in", hint="flnd auth login tlg")
        return client

    # -- Flood waits -----------------------------------------------------------------------

    async def _flood(self, ctx: Ctx, error: Any) -> None:
        """Hold for a `FloodWaitError`, one hold per account however many calls met it.

        Each `ctx.hold` is a block round, so four segments meeting one wait would otherwise
        spend every round at once and fail as blocked.
        """
        who = _who(ctx)
        waiting = self._waits.get(who)
        if waiting is None:
            waiting = asyncio.ensure_future(ctx.hold(FLOOD_WAIT, seconds=error.seconds))
            self._waits[who] = waiting
            waiting.add_done_callback(lambda _: self._waits.pop(who, None))
        # Shielded: one waiter cancelled (a segment whose file failed) leaves the others' hold.
        await asyncio.shield(waiting)

    async def _flooded(self, ctx: Ctx, call: Callable[[], Awaitable[Any]]) -> Any:
        """`await call()`, holding for each flood wait and calling it again."""
        from telethon.errors import FloodWaitError

        while True:
            try:
                return await call()
            except FloodWaitError as error:
                await self._flood(ctx, error)

    # -- Finding a chat ----------------------------------------------------------------------

    async def _find(self, ctx: Ctx, client: Any, value: str) -> Any:
        """The chat `value` names: an invite directly; else the cached peer, else a lookup.

        Raises:
            NotFound: an invite not joined, or no chat of that title.
            Hidden: a private chat, one the account left, or an id it can't see.
            UsageError: more than one chat has that title.
        """
        from telethon.errors import ChannelPrivateError, RPCError

        if value.startswith("+"):
            try:
                return _visible(await self._flooded(
                    ctx, lambda: client.get_entity(f"https://t.me/{value}")), value)
            except ChannelPrivateError:
                raise _private(value) from None
            except (ValueError, RPCError):
                raise NotFound(f"the invite {value!r} isn't one of your chats",
                               hint="join it first") from None
        entity = await self._cached(ctx, client, value)
        if entity is None:
            entity = _visible(await self._lookup(ctx, client, value), value)
            ctx.state[_cache_key(ctx, value)] = _peer(entity)
        return entity

    async def _cached(self, ctx: Ctx, client: Any, value: str) -> Any:
        """The chat this account found for `value` before, if it still has that name.

        An access hash is the account's own, so each account keeps its own entries.
        """
        from telethon.errors import ChannelPrivateError, RPCError
        from telethon.tl import types

        kept = ctx.state.get(_cache_key(ctx, value))
        if not isinstance(kept, dict) or kept.get("_") not in PEERS:
            return None
        fields = {name: field for name, field in kept.items() if name != "_"}
        try:
            peer = getattr(types, kept["_"])(**fields)
            entity = await self._flooded(ctx, lambda: client.get_entity(peer))
        except ChannelPrivateError:
            raise _private(value) from None
        # A peer this account can no longer read is a miss: the lookup tells why.
        except (TypeError, ValueError, RPCError):
            return None
        return _visible(entity, value) if _named(entity, value) else None

    async def _lookup(self, ctx: Ctx, client: Any, value: str) -> Any:
        """A username or an id looked up directly, else the account's chats by id or title."""
        from telethon.errors import ChannelPrivateError, RPCError

        number = value.lstrip("-").isdigit()
        if number or re.fullmatch(USERNAME, value):
            try:
                return await self._flooded(
                    ctx, lambda: client.get_entity(int(value) if number else value))
            except ChannelPrivateError:
                raise _private(value) from None
            # A username nobody has may still be a title; an id may be among the chats.
            except (ValueError, RPCError):
                pass
        dialogs = await self._flooded(ctx, client.get_dialogs)
        found = [dialog for dialog in dialogs
                 if (dialog.id == int(value) if number
                     else dialog.name.casefold() == value.casefold())]
        if not found and number:
            raise Hidden(f"the chat {value} isn't among your chats",
                         hint="it is private, or this account left it")
        if not found:
            raise NotFound(f"no chat {value!r} among your chats")
        if len(found) > 1:
            ids = ", ".join(str(dialog.id) for dialog in found)
            raise UsageError(f"{len(found)} chats are titled {value!r}: {ids}",
                             hint="pass one as tlg:<id>")
        return found[0].entity

    # -- Listing -----------------------------------------------------------------------------

    async def _messages(self, ctx: Ctx, profile: Mapping[str, Any],
                        kind: str) -> AsyncIterator[Post]:
        """A chat's messages of one category, newest first, each a post of its one file.

        After a flood wait, the listing goes on after the last message it gave.
        """
        from telethon.errors import ChannelPrivateError, FloodWaitError

        client, chat = self._client(ctx), profile["entity"]
        # `offset_date` takes the messages before it, and `when`'s last day is kept.
        before = None
        if ctx.when.until:
            before = datetime.datetime.combine(ctx.when.until + datetime.timedelta(days=1),
                                               datetime.time(), tzinfo=datetime.UTC)
        # `offset_id` takes the messages older than it; 0 is from the newest.
        last = 0
        while True:
            try:
                async for message in client.iter_messages(
                        chat, filter=server_filter(kind), offset_date=before, offset_id=last):
                    last = message.id
                    yield self._post(ctx, client, message, profile)
                return
            except FloodWaitError as error:
                await self._flood(ctx, error)
            except ChannelPrivateError:
                raise _private(profile["chat"]) from None

    def _post(self, ctx: Ctx, client: Any, message: Any, profile: Mapping[str, Any]) -> Post:
        """A message as a post: a document fetched in ranges, a photo whole."""
        document = message.document
        if document is not None:
            file = File(fetch=functools.partial(self._document, ctx, client, document),
                        name=file_name(message), size=document.size, ranged=True)
        else:
            file = File(fetch=functools.partial(self._photo, ctx, client, message),
                        name=file_name(message), size=message.file.size if message.file else None)
        return Post(message.id, [file], date=message.date, chat=profile["chat"],
                    chat_id=profile["chat_id"])

    # -- Downloading -------------------------------------------------------------------------

    async def _document(self, ctx: Ctx, client: Any, document: Any, part: Part) -> None:
        """Write a document's bytes from `part.offset` to its end, holding for flood waits."""
        from telethon.errors import FloodWaitError

        at, end = part.offset, document.size if part.end is None else part.end
        while at < end:
            start = at
            try:
                async for chunk in _chunks(client, document, at, end):
                    await part.write(chunk)
                    at += len(chunk)
            # The next request goes on from the bytes written, however far the last one got.
            except FloodWaitError as error:
                await self._flood(ctx, error)
                continue
            if at == start:
                raise NetworkError(f"Telegram sent nothing from byte {at} of {document.size}",
                                   retryable=True)

    async def _photo(self, ctx: Ctx, client: Any, message: Any, part: Part) -> None:
        """Write a photo from `part.offset`: Telegram keeps a photo in sizes, not as a document,
        so it comes whole, and a resume skips what the part holds."""
        data = await self._flooded(ctx, lambda: client.download_media(message, file=bytes))
        if not data:
            raise NetworkError(f"Telegram sent no photo for message {message.id}",
                               retryable=True)
        part.total(len(data))
        await part.write(data[part.offset:])


async def _chunks(client: Any, document: Any, at: int, end: int) -> AsyncIterator[bytes]:
    """The document's bytes from `at` up to `end`, as Telegram sends them.

    A request starts on a multiple of `REQUEST`, so it asks from the one before `at` and drops
    the bytes before it.
    """
    offset = at - at % REQUEST
    skip = at - offset
    async for chunk in client.iter_download(
            document, offset=offset, limit=-(-(end - offset) // REQUEST), request_size=REQUEST,
            file_size=document.size):
        chunk = chunk[skip:][:end - at]
        skip = 0
        if chunk:
            at += len(chunk)
            yield chunk


def _who(ctx: Ctx) -> str:
    """The account's name, which keys its client and its flood wait; "" without one."""
    return ctx.account.name if ctx.account is not None else ""


def _cache_key(ctx: Ctx, value: str) -> str:
    return f"chats.{_who(ctx)}.{value.casefold()}"


def _private(value: str) -> Error:
    return Hidden(f"the chat {value} is private, or this account left it")


def _visible(entity: Any, value: str) -> Any:
    """`entity`, unless it is a chat the account was removed from or left."""
    if type(entity).__name__ in ("ChannelForbidden", "ChatForbidden"):
        raise _private(value)
    return entity


def _peer(entity: Any) -> dict[str, Any]:
    """What finds the chat again without listing the account's chats: its id and access hash."""
    from telethon import utils

    return utils.get_input_peer(entity).to_dict()


def _named(entity: Any, value: str) -> bool:
    """Whether `entity` still has the id, username or title `value`."""
    from telethon import utils

    if value.lstrip("-").isdigit():
        return int(value) in (entity.id, utils.get_peer_id(entity))
    names = (display_name(entity), getattr(entity, "username", None) or "")
    return value.casefold() in {name.casefold() for name in names}


def display_name(entity: Any) -> str:
    """A chat's title, or a person's name, or else a username or id: the `{chat}` folder."""
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    person = " ".join(filter(None, (first, last)))
    return (getattr(entity, "title", None) or person or getattr(entity, "username", None)
            or str(entity.id))


def server_filter(kind: str) -> Any:
    """Telegram's own filter for a category, so the server sends only those messages."""
    from telethon.tl import types

    return getattr(types, FILTERS[kind])()


def file_name(message: Any) -> str:
    """A message's file name: `<UTC day>_<message id>`, then `_<name>` or else the extension.

    It is what saved files were named, so changing it downloads every file again
    (tests/data/tlg_names.json pins it).
    """
    stem = f"{message.date.date().isoformat()}_{message.id}"
    file = message.file
    ext = file.ext if file and file.ext else ""
    return f"{stem}_{file.name}" if file and file.name else f"{stem}{ext}"


# ---------------------------------------------------------------------------------------------
# The login's steps
# ---------------------------------------------------------------------------------------------

async def credentials(ctx: Ctx) -> tuple[int, str]:
    """The stored `api_id` and `api_hash`, or asked for when there are none yet.

    Raises:
        LoginError: the `api_id` typed isn't a number.
        UsageError: none are stored and nobody can answer.
    """
    secrets = ctx.secrets
    if "api_id" in secrets and "api_hash" in secrets:
        return int(secrets["api_id"]), secrets["api_hash"]
    where = "From my.telegram.org, under API development tools"
    api_id = (await ctx.ask("Telegram api_id", description=where)).strip()
    if not api_id.isdigit():
        raise LoginError("tlg: api_id is a number", hint="copy it from my.telegram.org")
    api_hash = await ctx.ask("Telegram api_hash", secret=True, description=where)
    return int(api_id), api_hash.strip()


async def log_in(ctx: Ctx, method: str, start: str, api_id: int,
                 api_hash: str) -> tuple[str, str]:
    """Log a client in by `method`, from the session `start` ("" for a new device).

    Returns:
        The session string to store, and the account's name to show.

    Raises:
        LoginError: Telegram refused (a wrong code, a revoked Telegram Web key), or no QR code
            was scanned.
    """
    from telethon.errors import RPCError

    client = _client(start, api_id, api_hash)
    await client.connect()
    try:
        if method == "qr":
            await by_qr(ctx, client)
        elif method == "phone":
            await by_phone(ctx, client)
        me = await client.get_me()
        if me is None:
            raise LoginError("tlg: Telegram Web's login no longer works",
                             hint="log in at web.telegram.org again")
        return client.session.save(), display_name(me)
    # Telegram's refusal is shown as a login error; any other error reaches the caller.
    except RPCError as error:
        raise LoginError(f"tlg: Telegram refused the login: {error}") from None
    finally:
        await client.disconnect()


async def by_qr(ctx: Ctx, client: Any) -> None:
    """Log in by showing QR codes until one is scanned, up to `QR_CODES` of them.

    Raises:
        LoginError: none of the codes was scanned in time.
    """
    from telethon.errors import SessionPasswordNeededError

    qr = await client.qr_login()
    for _ in range(QR_CODES):
        ctx.log("info", "scan this in the Telegram app, under Settings → Devices → Link "
                "Desktop Device:\n{qr}", qr=draw_qr(qr.url))
        try:
            await qr.wait()
            return
        # A code that ran out is replaced by a new one, and the scan is waited for again.
        except TimeoutError:
            await qr.recreate()
        except SessionPasswordNeededError:
            return await two_step(ctx, client)
    raise LoginError("tlg: no QR code was scanned", hint="flnd auth login tlg qr")


async def by_phone(ctx: Ctx, client: Any) -> None:
    """Log in by the phone number and the code Telegram sends."""
    from telethon.errors import SessionPasswordNeededError

    phone = await ctx.ask("Phone number",
                          description="With the country code, such as +44 7700 900123")
    await client.send_code_request(phone)
    code = await ctx.ask("Login code",
                         description="Telegram sent it to your Telegram app, or by SMS")
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        await two_step(ctx, client)


async def two_step(ctx: Ctx, client: Any) -> None:
    """Finish a login with the account's two-step verification password."""
    await client.sign_in(password=await ctx.ask("Two-step verification password", secret=True))


async def read_telegram_web(ctx: Ctx) -> Mapping[str, str]:
    """Telegram Web's local storage in the browser.

    Raises:
        LoginError: the browser or the page couldn't be read.
    """
    try:
        return await ctx.storage(TELEGRAM_WEB)
    except UsageError:
        raise
    except (Error, OSError, RuntimeError, ValueError) as error:
        raise LoginError(f"tlg: can't read Telegram Web in the browser: {error}",
                         hint="flnd auth login tlg qr") from None


def from_web_storage(storage: Mapping[str, str]) -> str:
    """A Telethon session string from Telegram Web's local storage.

    Telegram Web keeps, as JSON values (seen 2026-11-11, in both its A and K clients), the
    account's data center in `user_auth.dcID` (or `dc`), and a 256-byte auth key per data
    center, as hex, under `dc<n>_auth_key`.

    Raises:
        LoginError: the storage holds no usable login.
    """
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    values = {name: _json_or_text(value) for name, value in storage.items()}
    user = values.get("user_auth")
    dc = (user.get("dcID") if isinstance(user, dict) else None) or values.get("dc")
    key = values.get(f"dc{dc}_auth_key")
    try:
        auth_key = bytes.fromhex(key) if isinstance(key, str) and user else b""
    # A key that isn't hex is treated as absent, which is the not-logged-in error below.
    except ValueError:
        auth_key = b""
    # A Telegram auth key is 256 bytes (2048 bits); any other length is not a key.
    if len(auth_key) != 256 or str(dc) not in map(str, DATA_CENTERS):
        raise LoginError("tlg: not logged in to Telegram Web",
                         hint="log in at web.telegram.org first, or flnd auth login tlg qr")
    session = StringSession()
    session.set_dc(int(str(dc)), DATA_CENTERS[int(str(dc))], PORT)
    session.auth_key = AuthKey(auth_key)
    return session.save()


def draw_qr(text: str) -> str:
    """`text` as a QR code in Unicode half blocks, with a quiet border of 2 modules."""
    import qrcode

    code = qrcode.QRCode(border=2)
    code.add_data(text)
    return half_blocks(code.get_matrix())


def half_blocks(matrix: Sequence[Sequence[bool]]) -> str:
    """Draw a QR matrix (True is dark) two rows per line, lighting the light modules, as
    `qrencode -t UTF8` does for a terminal's light-on-dark text."""
    light = [[not dark for dark in row] for row in matrix]
    if len(light) % 2:
        light.append([True] * len(light[0]))
    lines = zip(light[::2], light[1::2])
    return "\n".join(
        "".join(HALF_BLOCKS[pair] for pair in zip(top, bottom)) for top, bottom in lines
    )


def _json_or_text(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value


def _version() -> str:
    """flnd's version, as the device's app version; "0" in a checkout that isn't installed."""
    from importlib import metadata

    try:
        return metadata.version("flntools")
    except metadata.PackageNotFoundError:
        return "0"


def _client(string: str, api_id: int, api_hash: str) -> Any:
    """A Telethon client for the session `string` ("" for a new one), not yet connected."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # A device named flnd in the account's settings, for a QR or phone login.
    return TelegramClient(StringSession(string), api_id, api_hash, device_model="flnd",
                          app_version=_version())
