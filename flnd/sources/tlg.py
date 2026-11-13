"""Telegram: every photo, video or file in a chat, group or channel the account can see.

How it works:
  - Telethon, with a user account over MTProto. Not the Bot API, which can't read a group's
    history from before the bot joined and stops at 20 MB, and not Pyrogram, whose upkeep is uncertain.
  - `flnd login`, for Telegram, asks for `api_id` and `api_hash` once (from my.telegram.org), then logs in
    from Telegram Web in Chrome, a QR code, or a phone number and code. The Telethon session is
    kept in flnd's encrypted secrets, never in a `.session` file.
  - `connect` opens one Telethon client for the whole run, on the source's own event loop, and the
    `chat` route finds the chat with it. The `photo`, `video` and `file` categories are async
    generators over `client.iter_messages`, newest first, each message one `Post` with one `File`,
    which Telethon downloads itself into the file the engine opens.
  - The categories are Telegram's own server-side filters, so only what `--from` names is asked for.
  - `--until` is sent to Telegram as `offset_date`; `--since` is the engine's job, which stops
    reading a category once 5 messages in a row are older than it.
  - A flood wait (`FloodWaitError`) in a download is waited out with `site.back_off`, which every
    download shares, and a video or file goes on from where it stopped (`iter_download`'s
    offset), as after a dropped connection; a big one comes in 4 parts at once. Listing never
    meets one: the client sleeps through any flood wait up to a day by itself.

Example:
    flnd download tlg:@examplechannel --from photo,video --time 2026-01-01..
    flnd download "tlg:Lens Club"

Folder layout, under `.flnd/media/tlg/<target>/<chat title>/`:
  photo/  video/  file/    <date>_<message id>[_<original name>]; an album's files sort together
A history row is keyed `<chat id>:<message id>`.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import urlsplit

from flnd import __version__
from flnd.source import (Fetch, FetchError, File, Login, LoginError, Post, Site, Source, Target, TargetError,
                         target)

TELEGRAM_WEB = "https://web.telegram.org/"
# Telegram's production data centers, as every client ships them; Telethon moves on from there itself.
DATA_CENTERS = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100", 4: "149.154.167.91",
                5: "91.108.56.130"}
# The port the data centers above are reached on, as Telethon's own default session uses it.
PORT = 443
# A Telegram username: 4 to 32 characters, starting with a letter.
USERNAME = re.compile(r"[A-Za-z]\w{3,31}")
# A QR login token lasts about 30 seconds; this many are shown before giving up.
QR_CODES = 5
# Telethon itself sleeps through a flood wait up to this long, so iterating a big chat never stops for one.
FLOOD_SLEEP = 24 * 3600
# Telethon's server-side filter class for each category: Telegram then sends only those messages.
FILTERS = {"photo": "InputMessagesFilterPhotos", "video": "InputMessagesFilterVideo",
           "file": "InputMessagesFilterDocument"}
# Telegram's largest file request; a request may not cross a 1 MiB boundary, so every part
# starts on a multiple of it.
REQUEST = 512 * 1024
# A document this big or more is fetched in `CONNECTIONS` parts at once.
SPLIT_SIZE = 8 * 1024 * 1024
CONNECTIONS = 4
# Dropped connections one part survives, each going on from where it stopped.
RESUMES = 3
# One character per pair of QR modules (top, bottom), where True is a light module: a full block is two lit halves.
HALF_BLOCKS = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}


class Telegram(Source):
    """Telegram chats, through Telethon on the group's own event loop."""

    code = "tlg"
    name = "Telegram"
    domains = ("t.me", "telegram.org")
    login_methods = ("browser", "qr", "phone")
    requires_login = True
    # A chat's folder, then the type; both are sanitized as file names, so a title holding `/` or `:` stays one folder.
    folder = "{chat}/{category}"
    # The chat's id and the message's id: a message id repeats from chat to chat, and the key is a history row's.
    key = "{chat_id}:{id}"
    # Every download shares one MTProto client and its flood waits: more at once only meets the
    # flood limit sooner (Telethon's FAQ).
    concurrency = 3

    # `resolve` below reads the target's text, since no pattern can say a chat title or a `t.me/c/<n>`
    # link; the route has no pattern of its own, only the form help shows.
    @target(shown="tlg:<@username, chat title or id>")
    async def chat(self, site: Site, chat: str) -> Any:
        """A chat, by its username, invite link, id or title: `Target("tlg", "chat", "examplechannel")`.

        Args:
            site: the target's site, whose `client` is the connected Telethon client.
            chat: the value `resolve` read: a username, `+<invite hash>`, an id or an exact title.

        Returns:
            The chat's entity, which each category receives.

        Raises:
            FetchError: no chat of the account has that name, or several do (`find_chat`).
        """
        return await find_chat(site.client, chat)

    @chat.category(newest_first=True)
    async def photo(self, site: Site, chat: Any) -> AsyncIterator[Post]:
        """The chat's photos, newest first.

        Args:
            site: the target's site, whose `client` lists the messages.
            chat: the chat's entity, as `chat` returned it.
        """
        async for post in messages(site, chat, "photo"):
            yield post

    @chat.category(newest_first=True)
    async def video(self, site: Site, chat: Any) -> AsyncIterator[Post]:
        """The chat's videos, newest first.

        Args:
            site: the target's site, whose `client` lists the messages.
            chat: the chat's entity, as `chat` returned it.
        """
        async for post in messages(site, chat, "video"):
            yield post

    @chat.category(newest_first=True)
    async def file(self, site: Site, chat: Any) -> AsyncIterator[Post]:
        """The chat's other files (documents, archives, audio), newest first.

        Args:
            site: the target's site, whose `client` lists the messages.
            chat: the chat's entity, as `chat` returned it.
        """
        async for post in messages(site, chat, "file"):
            yield post

    def resolve(self, text: str) -> Target:
        """A username, a t.me link, an invite, a chat id or a chat title, read as the `chat` route's forms."""
        scheme = text.lower().startswith(("http://", "https://"))
        url = urlsplit(text if scheme else f"https://{text}") if "/" in text or scheme else None
        host = (url.hostname or "").lower() if url is not None else ""
        if url is not None and any(host == domain or host.endswith("." + domain) for domain in self.domains):
            # The desktop, mobile and old mobile sites are t.me's own; a link on telegram.org names no chat.
            for prefix in ("www.", "m.", "mobile."):
                host = host.removeprefix(prefix)
            # The `s` segment is the web preview's marker, not part of the name; only t.me has chat links.
            parts = [part for part in url.path.split("/") if part and part != "s"] if host == "t.me" else []
            # A `t.me/c/<n>` link names a channel by a number that Telegram writes with a `-100` prefix.
            if parts[:1] == ["c"] and len(parts) > 1 and parts[1].isdigit():
                value = f"-100{parts[1]}"
            # The old `joinchat/<hash>` invite link, read as the `+<hash>` form that `find_chat` takes.
            elif parts[:1] == ["joinchat"] and len(parts) > 1:
                value = f"+{parts[1]}"
            elif parts and (parts[0].startswith("+") or USERNAME.fullmatch(parts[0])):
                value = parts[0]
            else:
                value = None
        elif text.startswith("@"):
            value = text[1:] if USERNAME.fullmatch(text[1:]) else None
        else:
            value = text.strip()
        if value is None:
            raise TargetError(f"{self.code}: can't read {text!r}; write it like {', '.join(self.forms)}")
        return Target(self.code, "chat", value)

    def login(self, login: Login, method: str) -> None:
        """Log in by `browser` (Telegram Web's login in Chrome), `qr`, or `phone`, and keep the session.

        `browser` reuses Telegram Web's own login rather than adding a device, so Telegram sees one
        session: logging out of Telegram Web logs flnd out too. It works only when you are logged
        in at web.telegram.org in Chrome's default profile, and it asks nothing once `api_id` and
        `api_hash` are stored. `qr` and `phone` add a device named flnd, and need a terminal.

        Args:
            login: the handle to ask through and keep the session in.
            method: one of `login_methods`.

        Raises:
            LoginError: the method needs a terminal and there is none, Telegram Web isn't logged in,
                or Telegram refuses the login.
        """
        # qr and phone ask questions, which a run with no terminal can't answer.
        if method != "browser" and not login.interactive:
            raise LoginError(f"tlg: the {method} login asks questions; run flnd login in a terminal")
        api_id, api_hash = credentials(login)
        # A browser login starts from Telegram Web's own session; qr and phone add a new device to the account.
        start = from_web_storage(read_telegram_web()) if method == "browser" else ""
        string, name = asyncio.run(log_in(login, method, start, api_id, api_hash))
        login.secrets.update(session=string, api_id=str(api_id), api_hash=api_hash)
        login.info("logged in as {name}", name=name)

    def logged_in(self, site: Site) -> bool:
        """Whether a Telethon session is stored."""
        return "session" in site.secrets

    @asynccontextmanager
    async def connect(self, site: Site) -> AsyncIterator[Any]:
        """The Telethon client of the stored login, connected for the whole run, on the source's loop.

        Args:
            site: the target's site, whose secrets hold the session and the API credentials.

        Raises:
            LoginError: Telegram no longer accepts the stored session.
        """
        secrets = site.secrets
        client = _client(secrets["session"], int(secrets["api_id"]), secrets["api_hash"])
        # Telethon sleeps through a flood wait up to FLOOD_SLEEP by itself, so listing never stops for one.
        client.flood_sleep_threshold = FLOOD_SLEEP
        await client.connect()
        try:
            # The stored session can be revoked (a logout elsewhere); then only a new login helps.
            if not await client.is_user_authorized():
                raise LoginError("the saved login no longer works; run flnd login")
            yield client
        # Disconnected even when the run stops early, so no connection outlives the source.
        finally:
            await client.disconnect()


async def messages(site: Site, chat: Any, kind: str) -> AsyncIterator[Post]:
    """A chat's messages of one type, newest first, each as a post of its one file.

    Args:
        site: the target's site; its `client` lists the messages and its `time` bounds them.
        chat: the chat's entity.
        kind: `photo`, `video` or `file`: Telegram's filter for it asks only for those messages.

    The post's date is the message's, and `chat` and `chat_id` are the fields the folder and key use.
    """
    # offset_date takes messages before it, and --until is inclusive.
    before = None
    if site.time.until:
        before = datetime.datetime.combine(site.time.until + datetime.timedelta(days=1), datetime.time(),
                                           tzinfo=datetime.timezone.utc)
    async for message in site.client.iter_messages(chat, filter=server_filter(kind), offset_date=before):
        size = message.file.size if message.file else None
        yield Post(message.id, [File(fetcher(site, message), name=file_name(message), size=size)],
                   date=message.date, chat=display_name(chat), chat_id=chat.id)


async def find_chat(client: Any, value: str) -> Any:
    """The chat a target names; a title must match exactly one of the account's chats, whatever its case.

    Args:
        client: the connected Telethon client.
        value: a username, an invite as `+<hash>`, a chat id, or an exact title.

    Raises `FetchError` for a chat that isn't there, and for a title several chats share, listing their ids.
    """
    from telethon.errors import RPCError

    if value.startswith("+") or USERNAME.fullmatch(value):
        try:
            return await client.get_entity(f"https://t.me/{value}" if value.startswith("+") else value)
        # A username Telegram doesn't resolve falls through to the account's dialogs; an invite can only be one of them.
        except (ValueError, RPCError):
            if value.startswith("+"):
                raise FetchError(f"the invite {value!r} isn't one of your chats; join it first") from None
    dialogs = await client.get_dialogs()
    if value.lstrip("-").isdigit():
        found = [dialog for dialog in dialogs if dialog.id == int(value)]
    else:
        found = [dialog for dialog in dialogs if dialog.name.casefold() == value.casefold()]
    if not found:
        raise FetchError(f"no chat {value!r} among your chats")
    if len(found) > 1:
        ids = ", ".join(str(dialog.id) for dialog in found)
        raise FetchError(f"{len(found)} chats are titled {value!r}: {ids}; pass one as tlg:<id>")
    return found[0].entity


def display_name(entity: Any) -> str:
    """A chat's title, or a person's name, or else a username or id."""
    person = " ".join(filter(None, (getattr(entity, "first_name", None), getattr(entity, "last_name", None))))
    return getattr(entity, "title", None) or person or getattr(entity, "username", None) or str(entity.id)


def server_filter(kind: str) -> Any:
    """Telegram's own filter for a category, so the server sends only those messages."""
    from telethon.tl import types

    return getattr(types, FILTERS[kind])()


def file_name(message: Any) -> str:
    """`<date>_<message id>_<original name>`, or `<date>_<message id><ext>` for media without a name."""
    stem = f"{message.date.date().isoformat()}_{message.id}"
    file = message.file
    return f"{stem}_{file.name}" if file and file.name else f"{stem}{file.ext if file and file.ext else ''}"


def fetcher(site: Site, message: Any) -> Fetch:
    """A `fetch(dest, progress)` for the engine: the message's media, downloaded into `dest`.

    A flood wait (Telegram's `FloodWaitError`) is waited out for the seconds it names, through
    `site.back_off`, which every download shares. A video or file then goes on from where it
    stopped, as it does after a dropped connection, and a big one comes in parts at once; a
    photo, small and sized by Telethon, starts again.

    Args:
        site: the target's site, whose `client` downloads and whose `back_off` shares the waits.
        message: the Telethon message whose media is downloaded.
    """
    from telethon.errors import FloodWaitError

    async def fetch(dest: Any, progress: Any) -> None:
        if message.document is not None:
            with open(dest, "wb") as file:
                await _Download(site, message.document, file, progress).run()
            return
        while True:
            try:
                with open(dest, "wb") as file:
                    await site.client.download_media(message, file=file, progress_callback=progress)
                return
            except FloodWaitError as error:
                # Off the loop: the wait is a sleep, and other downloads on the loop have their own to finish.
                await asyncio.to_thread(site.back_off, "Telegram flood wait", retry_after=error.seconds)

    return fetch


class _Download:
    """One document's bytes into an open file, in parts at once, each going on where it stopped."""

    def __init__(self, site: Site, document: Any, file: Any, progress: Any):
        self.site, self.document, self.file, self.progress = site, document, file, progress
        self.size: int = document.size
        self.parts = parts(self.size)
        # The bytes each part has written so far; one loop runs every part, so no lock.
        self.done = [0] * len(self.parts)

    async def run(self) -> None:
        """Fetch every part at once; one failing stops the others."""
        tasks = [asyncio.ensure_future(self._part(number)) for number in range(len(self.parts))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _part(self, number: int) -> None:
        """Fetch one part, waiting out flood waits and going on after up to `RESUMES` drops."""
        from telethon.errors import FloodWaitError

        start, end = self.parts[number]
        drops = 0
        while self.done[number] < end - start:
            try:
                await self._read(number, start + self.done[number], end)
            except FloodWaitError as error:
                await asyncio.to_thread(self.site.back_off, "Telegram flood wait",
                                        retry_after=error.seconds)
            # Telethon reconnects by itself; the part goes on from its last whole request.
            except ConnectionError:
                drops += 1
                if drops > RESUMES:
                    raise

    async def _read(self, number: int, position: int, end: int) -> None:
        """Write the part's bytes from `position` up to `end`, as Telegram sends them."""
        requests = -(-(end - position) // REQUEST)
        before = self.done[number]
        async for chunk in self.site.client.iter_download(
                self.document, offset=position, limit=requests, request_size=REQUEST,
                file_size=self.size):
            # Each part's writes land at its own place, between two awaits of the one loop.
            self.file.seek(position)
            self.file.write(chunk)
            position += len(chunk)
            self.done[number] += len(chunk)
            self.progress(sum(self.done), self.size)
        if self.done[number] == before:
            raise FetchError(f"Telegram sent nothing from byte {position} of {self.size}")


def parts(size: int) -> list[tuple[int, int]]:
    """Where each part of a document starts and ends: `CONNECTIONS` for a big one, else one."""
    count = CONNECTIONS if size >= SPLIT_SIZE else 1
    # Each part a whole number of requests, so every request starts on a multiple of its size.
    step = max(-(-size // count // REQUEST) * REQUEST, REQUEST)
    return [(start, min(start + step, size)) for start in range(0, size, step)]


def credentials(login: Login) -> tuple[int, str]:
    """The stored `api_id` and `api_hash`, or asked for when there are none yet.

    Raises:
        LoginError: none are stored and there is no terminal to ask in, or the `api_id` isn't a number.
    """
    secrets = login.secrets
    if "api_id" in secrets and "api_hash" in secrets:
        return int(secrets["api_id"]), secrets["api_hash"]
    if not login.interactive:
        raise LoginError("tlg: no api_id and api_hash yet; run flnd login in a terminal")
    where = "From my.telegram.org, under API development tools"
    api_id = login.ask("Telegram api_id", description=where).strip()
    if not api_id.isdigit():
        raise LoginError(f"tlg: api_id is a number, not {api_id!r}")
    return int(api_id), login.ask("Telegram api_hash", secret=True, description=where).strip()


async def log_in(login: Login, method: str, start: str, api_id: int, api_hash: str) -> tuple[str, str]:
    """Log a client in by `method`, from the session string `start`, and give its new string and the account's name.

    Args:
        login: the handle to ask questions through (a code, a password).
        method: "browser", "qr" or "phone"; "browser" asks nothing, since `start` already holds its login.
        start: the session string to begin from; "" for a new device.
        api_id: the Telegram app's id, from my.telegram.org.
        api_hash: the Telegram app's hash, from my.telegram.org.

    Returns:
        The client's session string, to store, and the account's display name.
    """
    from telethon.errors import RPCError

    client = _client(start, api_id, api_hash)
    await client.connect()
    try:
        if method == "qr":
            await by_qr(login, client)
        elif method == "phone":
            await by_phone(login, client)
        me = await client.get_me()
        if me is None:
            raise LoginError("tlg: Telegram Web's login no longer works; log in at web.telegram.org again")
        # The session string is what the source stores and reads back on later runs.
        return client.session.save(), display_name(me)
    # Telegram's refusal is shown as a login error; any other exception is not caught here and reaches the caller.
    except RPCError as error:
        raise LoginError(f"tlg: Telegram refused the login: {error}") from None
    finally:
        await client.disconnect()


async def by_qr(login: Login, client: Any) -> None:
    """Log in by showing QR codes until one is scanned, `QR_CODES` at most; Telegram's two-step password is asked after.

    Args:
        login: the handle to show the codes through.
        client: the connected Telethon client to log in.
    """
    from telethon.errors import SessionPasswordNeededError

    qr = await client.qr_login()
    for _ in range(QR_CODES):
        login.info("scan this in the Telegram app, under Settings → Devices → Link Desktop Device:\n{qr}",
                   qr=draw_qr(qr.url))
        try:
            await qr.wait()
            return
        # A code that ran out is replaced by a new one, and the scan is waited for again.
        except asyncio.TimeoutError:
            await qr.recreate()
        except SessionPasswordNeededError:
            return await two_step(login, client)
    raise LoginError("tlg: no QR code was scanned; run flnd login again")


async def by_phone(login: Login, client: Any) -> None:
    """Log in by the phone number and the code Telegram sends; the two-step password is asked when the account has one.

    Args:
        login: the handle to ask the number, the code and the password through.
        client: the connected Telethon client to log in.
    """
    from telethon.errors import SessionPasswordNeededError

    phone = login.ask("Phone number", description="With the country code, such as +44 7700 900123")
    await client.send_code_request(phone)
    code = login.ask("Login code", description="Telegram sent it to your Telegram app, or by SMS")
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        await two_step(login, client)


async def two_step(login: Login, client: Any) -> None:
    """Finish a login with the account's two-step verification password, which isn't echoed.

    Args:
        login: the handle to ask the password through, with no echo.
        client: the connected Telethon client to sign in.
    """
    await client.sign_in(password=login.ask("Two-step verification password", secret=True))


def read_telegram_web() -> dict[str, str]:
    """Telegram Web's local storage, from a copy of Chrome's default profile, in a browser nobody sees."""
    from urllib.parse import urlparse

    from shared import browser

    try:
        # Headless: the read happens without a window, so nothing is shown to the user.
        with browser.open(profile="Default", headless=True) as chrome:
            page = chrome.open(TELEGRAM_WEB, idle=True)
            # `Browser.open` logs a failed navigation and returns the tab still on about:blank, whose
            # evaluate would raise; so check where the tab is and say it plainly.
            if urlparse(page.url).hostname != urlparse(TELEGRAM_WEB).hostname:
                raise LoginError("tlg: Chrome could not open Telegram Web; check the connection, "
                                 "or try a QR code instead")
            try:
                return page.raw.evaluate("Object.assign({}, localStorage)")
            # What evaluate raises when the tab navigated away or closed meanwhile.
            except browser.RawError as error:
                raise LoginError(f"tlg: can't read Telegram Web in Chrome: {error}; try a QR code instead") from None
    except (OSError, RuntimeError, ValueError) as error:
        raise LoginError(f"tlg: can't read Telegram Web in Chrome: {error}; try a QR code instead") from None


def from_web_storage(storage: Mapping[str, str]) -> str:
    """A Telethon `StringSession` from Telegram Web's local storage: the account's data center and its auth key.

    Both web clients keep `user_auth` (with `dcID`), `dc`, and `dc<N>_auth_key` as hex, JSON-encoded.
    Raises `LoginError` when Telegram Web isn't logged in.
    """
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    # Web storage holds JSON-encoded values; the auth key itself is hex inside that JSON string.
    values = {name: _json_or_text(value) for name, value in storage.items()}
    user = values.get("user_auth")
    # `dcID` in `user_auth` is the account's data center; `dc` is the fallback when `user_auth` lacks it.
    dc = (user.get("dcID") if isinstance(user, dict) else None) or values.get("dc")
    key = values.get(f"dc{dc}_auth_key")
    try:
        auth_key = bytes.fromhex(key) if isinstance(key, str) and user else b""
    # A key that isn't hex is treated as absent, which is the not-logged-in error below.
    except ValueError:
        auth_key = b""
    # A Telegram auth key is 256 bytes (2048 bits); any other length is not a key.
    if len(auth_key) != 256 or str(dc) not in map(str, DATA_CENTERS):
        raise LoginError("tlg: not logged in to Telegram Web in Chrome; log in at web.telegram.org first, "
                         "or log in with a QR code")
    string_session = StringSession()
    string_session.set_dc(int(str(dc)), DATA_CENTERS[int(str(dc))], PORT)
    string_session.auth_key = AuthKey(auth_key)
    return string_session.save()


def draw_qr(text: str) -> str:
    """`text` as a QR code in Unicode half blocks, two rows of modules per line."""
    import qrcode

    code = qrcode.QRCode(border=2)
    code.add_data(text)
    return half_blocks(code.get_matrix())


def half_blocks(matrix: Sequence[Sequence[bool]]) -> str:
    """Draw a QR matrix (True is dark), lighting the light modules, as `qrencode -t UTF8` does for a dark terminal."""
    light = [[not dark for dark in row] for row in matrix]
    if len(light) % 2:
        light.append([True] * len(light[0]))
    lines = zip(light[::2], light[1::2])
    return "\n".join("".join(HALF_BLOCKS[pair] for pair in zip(top, bottom)) for top, bottom in lines)


def _json_or_text(value: str) -> Any:
    """`value` decoded as JSON, or as it is when it isn't JSON."""
    try:
        return json.loads(value)
    except ValueError:
        return value


def _client(string: str, api_id: int, api_hash: str) -> Any:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # A device named flnd, in the account's settings, for a QR or phone login. Any, because Telethon's own
    # annotations call some coroutines None.
    return TelegramClient(StringSession(string), api_id, api_hash, device_model="flnd", app_version=__version__)
