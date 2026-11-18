"""Telegram (`tlg`): every photo, video or file of a chat, group or channel the account can see.

What it holds: the `Telegram` source, its `TelegramLogin` (by Telegram Web's session in Chrome,
a QR code, or a phone number), and the helpers both use: finding a chat, listing its messages,
and downloading a document in parts that resume. `flnd/docs/sources/tlg` is the guide for people.

Who calls it: the engine, as every source. It talks to Telegram through Telethon (an SDK for
Telegram's own protocol, MTProto), never over HTTP, so it imports `telethon` and `qrcode` lazily,
inside the functions that need them: `flnd --help` never loads them.

The main flow of a download:
1. `connect` makes one Telethon client from the stored session, once per run, on the run's
   asyncio loop, and checks the session still works.
2. The `chat` route finds the chat (`find_chat`): an invite or a username directly, a number as
   an id, else by title among the account's chats.
3. Each category (`photo`, `video`, `file`) lists the chat's messages with Telegram's own
   server-side filter, newest first, one post per message with its one file. The engine stops
   the listing when a sync has caught up or `--when` is passed; the source just lists.
4. The engine calls a file's fetch function only for a message not saved yet: a photo comes
   whole (`download_media`); a document (video or file) comes in parts at once (`_Download`),
   each part's offsets saved beside the partial file so a later run goes on from there.

Routes and categories: one `chat` route, with no pattern: `resolve` reads `tlg:@<username>`,
`tlg:<username>`, `t.me/<username>`, `t.me/s/<username>`, `t.me/+<hash>`,
`t.me/joinchat/<hash>`, `t.me/c/<n>[/<message>]`, `tlg:<id>` and `tlg:<title>`. Its categories
are `photo`, `video` and `file`, each `newest_first`.

How a listing ends: Telethon's `iter_messages` runs out at the chat's oldest message. With
`--when`, it starts after the range's last day (`offset_date`); the engine ends it at the start.

Login (`TelegramLogin`), never cookies:
- `browser` builds a session from Telegram Web's local storage in Chrome's default profile,
  read in a hidden copy of it, and adds no device;
- `qr` shows up to `QR_CODES` codes in the terminal; `phone` asks for the number and the code.
  Both add a device named flnd, and ask a two-step password when the account has one;
- the first login asks for the `api_id` and `api_hash` from my.telegram.org. The session and the
  pair are kept in the source's encrypted secrets; a stored session is what counts as a login.

Site quirks, with the date seen:
- `t.me/c/<n>` holds a channel's id without the `-100` the client API puts before it
  (2026-11-11).
- `t.me/s/<name>` is the web preview of a channel; `t.me/joinchat/<hash>` the old invite form
  (2026-11-11).
- Telegram Web keeps its login as JSON in local storage, in both its A and K clients: the data
  center in `user_auth.dcID` (or `dc`) and a 256-byte key per center as hex (2026-11-11).
- A file request is at most 512 KiB and may not cross a 1 MiB boundary (2026-11-13).
- Telethon's own flood sleep went up to 24 hours before 2026-11-16, so it hid every wait.

Flood waits (Telegram's rate limit, an error naming the seconds to wait): Telethon sleeps a
short one itself (`FLOOD_SLEEP`); a longer one reaches this module, which waits it out through
`site.back_off(retry_after=...)` (shown to the person, capped by the engine, ended by a stop)
and goes on from where it was. Every waiter of one client shares one wait (`_flood_wait`).

Rules a change must keep:
- Everything Telethon does runs on the run's one loop: routes, categories, `connect` and the
  fetch functions are `async def`. `site.back_off` blocks, so it runs on a worker thread.
- A part's offsets may only count bytes the file holds: the file is flushed before they are
  saved, or a resume would skip bytes never written.
- `key` and the file names are what saved files were named and keyed by. Changing either makes
  every saved file download again (tests/data/tlg_names.json pins them).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flnd.source import (
    Error,
    Fetch,
    File,
    Login,
    LoginError,
    NetworkError,
    NotFound,
    Post,
    Site,
    Source,
    Target,
    UsageError,
    target,
)

TELEGRAM_WEB = "https://web.telegram.org/"
# Telegram's production data centers, as every client ships them.
DATA_CENTERS = {
    1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100", 4: "149.154.167.91",
    5: "91.108.56.130",
}
# The port the data centers above are reached on, as Telethon's own default session uses it.
PORT = 443
# A Telegram username: 4 to 32 characters, starting with a letter.
USERNAME = re.compile(r"[A-Za-z]\w{3,31}")
# A QR login token lasts about 30 seconds; this many are shown before giving up.
QR_CODES = 5
# The longest flood wait, in seconds, Telethon sleeps through by itself (its own default). A
# longer one is raised as `FloodWaitError` and waited out by `site.back_off`, where the person
# sees it and a stop ends it. (It was 24 hours before 2026-11-16, so the handlers never ran.)
FLOOD_SLEEP = 60
# Telethon's server-side filter class for each category: Telegram then sends only those messages.
FILTERS = {
    "photo": "InputMessagesFilterPhotos", "video": "InputMessagesFilterVideo",
    "file": "InputMessagesFilterDocument",
}
# Telegram's largest file request; a request may not cross a 1 MiB boundary, so every part
# starts on a multiple of it.
REQUEST = 512 * 1024
# A document this big or more is fetched in `CONNECTIONS` parts at once.
SPLIT_SIZE = 8 * 1024 * 1024
CONNECTIONS = 4
# Dropped connections one part survives, each going on from where it stopped.
RESUMES = 3
# One character per pair of QR modules (top, bottom), where True is a light module.
HALF_BLOCKS = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}


# ---------------------------------------------------------------------------------------------
# Logging in
# ---------------------------------------------------------------------------------------------

class TelegramLogin(Login):
    """Telegram's own login: a Telethon session, kept with the API pair in the source's secrets.

    `browser` turns Telegram Web's login in Chrome into a session, adding no device; `qr` and
    `phone` log a new device named flnd in, and ask questions, so they need a terminal. The
    first login also asks for the `api_id` and `api_hash` from my.telegram.org.
    """

    methods = ("browser", "qr", "phone")

    def start(self, site: Site, how: str, /) -> None:
        """Log in by `how`, and keep the session, the `api_id` and the `api_hash`.

        Args:
            site: the source's login site (`flnd.engine.site.LoginSite`): `ask`, `secrets`,
            `storage`.
            how: "browser", "qr" or "phone".

        Raises:
            LoginError: no terminal to ask in, Telegram refused, no QR code was scanned, or
                Telegram Web in Chrome isn't logged in.
        """
        # qr and phone ask questions, which a run with no terminal can't answer.
        if how != "browser" and not site.interactive:
            raise LoginError(f"tlg: the {how} login asks questions; "
                             f"run flnd login tlg {how} in a terminal")
        api_id, api_hash = credentials(site)
        # Browser login starts from Telegram Web's session; qr and phone add a new device.
        start = from_web_storage(read_telegram_web(site)) if how == "browser" else ""
        # `flnd login` runs on the main thread with no loop running, so a loop of its own is safe.
        string, name = asyncio.run(log_in(site, how, start, api_id, api_hash))
        site.secrets.update(session=string, api_id=str(api_id), api_hash=api_hash)
        site.info("logged in as {name}", name=name)

    def check(self, site: Site, /) -> bool:
        """Whether a Telethon session is stored; the API pair alone is no login."""
        return "session" in site.secrets


# ---------------------------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------------------------

class Telegram(Source):
    """Telegram chats, through Telethon on the run's event loop."""

    code = "tlg"
    name = "Telegram"
    domains = ("t.me", "telegram.org")
    api = 1
    login = TelegramLogin()
    # A chat's name, then the category; the engine sanitizes both as file names.
    folder = "{chat}/{category}"
    fields = ("chat", "chat_id")
    # The chat's id and the message's id: a message id repeats from chat to chat.
    key = "{chat_id}:{post}"
    # Every download shares one MTProto client and its flood waits: more at once only meets the
    # flood limit sooner (Telethon's FAQ).
    limits = {"t.me": 3}

    # `resolve` reads the target's text: no pattern can match a chat title or a `t.me/c/<n>`
    # link, so the route has no pattern of its own, only the form help shows.
    @target(shown="tlg:<@username, chat title or id>", folder="{chat}/{category}")
    async def chat(self, site: Site, chat: str) -> Any:
        """A chat by username, invite link, id or title; its entity is what categories get."""
        return await find_chat(site, chat)

    @chat.category(newest_first=True)
    async def photo(self, site: Site, chat: Any) -> AsyncIterator[Post]:
        """The chat's photos, newest first."""
        async for post in messages(site, chat, "photo"):
            yield post

    @chat.category(newest_first=True)
    async def video(self, site: Site, chat: Any) -> AsyncIterator[Post]:
        """The chat's videos, newest first."""
        async for post in messages(site, chat, "video"):
            yield post

    @chat.category(newest_first=True)
    async def file(self, site: Site, chat: Any) -> AsyncIterator[Post]:
        """The chat's other files (documents, archives, audio), newest first."""
        async for post in messages(site, chat, "file"):
            yield post

    def resolve(self, text: str) -> Target:
        """Read a username, a t.me link, an invite, a chat id or a title as a `chat` target.

        Args:
            text: the text after `tlg:`, or a link on Telegram's domains.

        Returns:
            `Target("tlg:<value>", kind="chat")`: a username without its `@`, an invite as
            `+<hash>`, a `t.me/c/<n>` link as `-100<n>`, or else the stripped text (a title).

        Raises:
            UsageError: a link on Telegram's domains that names no chat, or an `@` without a
                username after it.
        """
        scheme = text.lower().startswith(("http://", "https://"))
        url = urlsplit(text if scheme else f"https://{text}") if "/" in text or scheme else None
        host = (url.hostname or "").lower() if url is not None else ""
        is_tlg = any(host == domain or host.endswith("." + domain) for domain in self.domains)
        if url is not None and is_tlg:
            value = _url_value(url, host)
        elif text.startswith("@"):
            value = text[1:] if USERNAME.fullmatch(text[1:]) else None
        else:
            value = text.strip()
        if value is None:
            forms = ", ".join(self.forms)
            raise UsageError(f"{self.code}: can't read {text!r}; write it like {forms}")
        return Target(f"{self.code}:{value}", kind="chat")

    @asynccontextmanager
    async def connect(self, site: Site) -> AsyncIterator[Any]:
        """Connect one Telethon client for the stored login, on the run's loop.

        Args:
            site: the account's site; its `secrets` hold the session and the API pair.

        Yields:
            The connected client, which every target's `site.client` is.

        Raises:
            LoginError: the stored session no longer works (a logout elsewhere revoked it).
        """
        secrets = site.secrets
        client = _client(secrets["session"], int(secrets["api_id"]), secrets["api_hash"])
        client.flood_sleep_threshold = FLOOD_SLEEP
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise LoginError("the saved login no longer works; run flnd login tlg")
            yield client
        # Disconnected even when the run stops early, so no connection outlives the source.
        finally:
            await client.disconnect()


def _url_value(url: Any, host: str) -> str | None:
    """The chat a Telegram link names, or None when it names none.

    Args:
        url: the link, split.
        host: its host, lowercase.

    Returns:
        `-100<n>` for a private chat link (`t.me/c/<n>`), `+<hash>` for an invite (old
        `joinchat/<hash>` too), the username for a public link; None for anything else,
        including every link not on t.me itself.
    """
    for prefix in ("www.", "m.", "mobile.", "web."):
        host = host.removeprefix(prefix)
    # The `s` segment marks t.me's web preview of a channel (seen 2026-11-11).
    parts = [part for part in url.path.split("/") if part and part != "s"] if host == "t.me" else []
    # `t.me/c/<n>` holds a channel's id without the `-100` every client API puts before it.
    if parts[:1] == ["c"] and len(parts) > 1 and parts[1].isdigit():
        return f"-100{parts[1]}"
    if parts[:1] == ["joinchat"] and len(parts) > 1:
        return f"+{parts[1]}"
    if parts and (parts[0].startswith("+") or USERNAME.fullmatch(parts[0])):
        return parts[0]
    return None


# ---------------------------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------------------------

async def messages(site: Site, chat: Any, kind: str) -> AsyncIterator[Post]:
    """A chat's messages of one category, newest first, each as a post of its one file.

    A flood wait Telethon passes on is waited out, and the listing goes on after the last
    message it gave, so no message is listed twice.

    Args:
        site: the target's site; `site.client` is the connected client.
        chat: the chat's entity, as `find_chat` gave it.
        kind: "photo", "video" or "file".

    Yields:
        One `Post` per message: its id, its file, its date, and the chat's name and id.
    """
    from telethon.errors import FloodWaitError

    # `offset_date` takes the messages before it, and `--when`'s last day is kept: the next day.
    before = None
    if site.time.until:
        until_date = site.time.until + datetime.timedelta(days=1)
        before = datetime.datetime.combine(
            until_date, datetime.time(), tzinfo=datetime.UTC,
        )
    # `offset_id` takes the messages older than it; 0 is from the newest.
    last = 0
    while True:
        try:
            async for message in site.client.iter_messages(
                chat, filter=server_filter(kind), offset_date=before, offset_id=last,
            ):
                last = message.id
                size = message.file.size if message.file else None
                yield Post(
                    message.id,
                    [File(fetcher(site, message), name=file_name(message), size=size)],
                    date=message.date,
                    chat=display_name(chat),
                    chat_id=chat.id,
                )
            return
        except FloodWaitError as error:
            await _flood_wait(site, error)


async def find_chat(site: Site, value: str) -> Any:
    """Find the chat named by `value`, among what the account can see.

    1. An invite (`+<hash>`) or a username is looked up directly. An invite not joined fails;
       a username that can't be looked up falls through to the titles.
    2. A number is looked up as an id.
    3. Else the account's chats are listed, and the one with that id, or that title (case
       ignored), is taken.

    Args:
        site: the target's site; `site.client` is the connected client.
        value: the target's value, as `Telegram.resolve` gave it.

    Returns:
        The chat's entity.

    Raises:
        NotFound: an invite not joined, or no chat of that id or title.
        UsageError: more than one chat has that title; the message lists their ids.
    """
    from telethon.errors import RPCError

    client = site.client
    if value.startswith("+") or USERNAME.fullmatch(value):
        try:
            name = f"https://t.me/{value}" if value.startswith("+") else value
            return await _flooded(site, lambda: client.get_entity(name))
        except (ValueError, RPCError):
            if value.startswith("+"):
                raise NotFound(
                    f"the invite {value!r} isn't one of your chats", hint="join it first"
                ) from None
    if value.lstrip("-").isdigit():
        try:
            return await _flooded(site, lambda: client.get_entity(int(value)))
        except (ValueError, RPCError):
            pass
    dialogs = await _flooded(site, client.get_dialogs)
    if value.lstrip("-").isdigit():
        found = [dialog for dialog in dialogs if dialog.id == int(value)]
    else:
        found = [dialog for dialog in dialogs if dialog.name.casefold() == value.casefold()]
    if not found:
        raise NotFound(f"no chat {value!r} among your chats")
    if len(found) > 1:
        ids = ", ".join(str(dialog.id) for dialog in found)
        raise UsageError(f"{len(found)} chats are titled {value!r}: {ids}",
                         hint="pass one as tlg:<id>")
    return found[0].entity


def display_name(entity: Any) -> str:
    """A chat's title, or a person's name, or else a username or id: the `{chat}` folder.

    Args:
        entity: a Telethon chat, channel or user; for a login, the account itself.

    Returns:
        The first of: the title, the first and last name joined, the username, the id.
    """
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    person = " ".join(filter(None, (first, last)))
    return (
        getattr(entity, "title", None)
        or person
        or getattr(entity, "username", None)
        or str(entity.id)
    )


def server_filter(kind: str) -> Any:
    """Telegram's own filter for a category, so the server sends only those messages."""
    from telethon.tl import types

    return getattr(types, FILTERS[kind])()


def file_name(message: Any) -> str:
    """A message's file name: `<UTC day>_<message id>`, then `_<name>` or else the extension.

    The name is given as the site's (`File(name=...)`), so the source's `filename` template
    never applies; the engine still sanitizes it.

    Args:
        message: a Telethon message that has media; its date, id and file give the name.

    Returns:
        The file name, with no folder.
    """
    stem = f"{message.date.date().isoformat()}_{message.id}"
    file = message.file
    ext = file.ext if file and file.ext else ""
    return f"{stem}_{file.name}" if file and file.name else f"{stem}{ext}"


# ---------------------------------------------------------------------------------------------
# Flood waits
# ---------------------------------------------------------------------------------------------

# The flood wait in progress for each client, by `id(client)`, while it lasts: a wait is for the
# whole account, so the parts and files that meet it together share it. Only the run's loop
# touches it, so it needs no lock.
_WAITS: dict[int, asyncio.Future[None]] = {}


async def _flood_wait(site: Site, error: Any) -> None:
    """Wait out a `FloodWaitError` through `site.back_off`, sharing one wait per client.

    `back_off` sleeps on the calling thread, so it runs on a worker thread and the loop goes on.
    Sharing matters: four parts that meet one flood wait at once would otherwise count as four
    back-off rounds in a row, and the fourth would raise `Blocked`.

    Args:
        site: the target's site.
        error: the `FloodWaitError`; its `seconds` are what Telegram asked for.

    Raises:
        Blocked: the site kept blocking (see `Site.back_off`).
    """
    key = id(site.client)
    waiting = _WAITS.get(key)
    if waiting is None:
        waiting = asyncio.ensure_future(asyncio.to_thread(
            site.back_off, "Telegram flood wait", retry_after=error.seconds,
        ))
        _WAITS[key] = waiting
        waiting.add_done_callback(lambda _: _WAITS.pop(key, None))
    # Shielded: one waiter cancelled (a part whose sibling failed) leaves the others' wait.
    await asyncio.shield(waiting)


async def _flooded(site: Site, call: Callable[[], Awaitable[Any]]) -> Any:
    """`await call()`, waiting out each flood wait Telethon passes on and calling it again.

    Args:
        site: the site, used to tell the user about each wait.
        call: makes a fresh awaitable per try, since an awaitable can be awaited only once.

    Returns:
        What `call()` returns, once it succeeds.
    """
    from telethon.errors import FloodWaitError

    while True:
        try:
            return await call()
        except FloodWaitError as error:
            await _flood_wait(site, error)


# ---------------------------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------------------------

def fetcher(site: Site, message: Any) -> Fetch:
    """A `fetch(dest, progress)` for the engine: the message's media, downloaded into `dest`.

    A document (video or file) comes in parts that resume (`_Download`); a photo, which Telegram
    keeps in sizes rather than as one document, comes whole through `download_media`.

    Args:
        site: the target's site.
        message: the message.

    Returns:
        The coroutine function the engine runs on the run's loop.
    """
    async def fetch(dest: Any, progress: Any) -> None:
        if message.document is not None:
            dest_path = Path(dest)
            sidecar = Path(f"{dest_path}.json")
            done = _load_offsets(dest_path, sidecar, message.document.size)
            mode = "r+b" if done is not None else "wb"
            with await asyncio.to_thread(open, dest_path, mode) as file:
                await _Download(
                    site, message.document, file, progress, sidecar=sidecar, done=done,
                ).run()
            return
        await _flooded(site, lambda: _photo(site, message, dest, progress))

    return fetch


async def _photo(site: Site, message: Any, dest: Any, progress: Any) -> None:
    """Write a photo to `dest`, from its start: a photo is small, so it never resumes."""
    with await asyncio.to_thread(open, dest, "wb") as file:
        await site.client.download_media(message, file=file, progress_callback=progress)


def _load_offsets(dest: Path, sidecar: Path, size: int) -> list[int] | None:
    """The bytes each part of `dest` holds, from its offsets file; None to start over.

    Args:
        dest: the partial file.
        sidecar: its offsets file, `{"size": <bytes>, "done": [<bytes per part>]}`.
        size: the document's size now.

    Returns:
        The offsets; None when either file is missing, or the offsets are damaged or for
        another size or part count, so the download starts again from 0.
    """
    if not dest.is_file() or not sidecar.is_file():
        return None
    try:
        state = json.loads(sidecar.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    part_list = parts(size)
    done = state.get("done")
    if state.get("size") != size or not isinstance(done, list) or len(done) != len(part_list):
        return None
    if not all(
        isinstance(n, int) and 0 <= n <= end - start for n, (start, end) in zip(done, part_list)
    ):
        return None
    return list(done)


def _save(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically: a temporary file beside it, then a rename.

    A crash leaves the old offsets or the new ones, never half of them.

    Args:
        path: the file to write; its folder must exist.
        data: the bytes the file should hold.
    """
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class _Download:
    """One document's bytes into an open file, in parts at once, each going on where it stopped.

    Args:
        site: the target's site.
        document: Telethon's document: its `size`, and what `iter_download` reads.
        file: the partial file, open for writing at any place.
        progress: `progress(done, total)` in bytes, for the engine.
        sidecar: the offsets file to keep while downloading; None keeps none.
        done: the bytes each part holds already, from `_load_offsets`; None starts at 0.
    """

    def __init__(
        self,
        site: Site,
        document: Any,
        file: Any,
        progress: Any,
        *,
        sidecar: Path | None = None,
        done: list[int] | None = None,
    ):
        self.site, self.document, self.file, self.progress = site, document, file, progress
        self.size: int = document.size
        self.parts = parts(self.size)
        self.sidecar = sidecar
        # The bytes each part has written so far; one loop runs every part, so no lock.
        self.done = list(done) if done is not None else [0] * len(self.parts)

    async def run(self) -> None:
        """Fetch every part at once; one failing cancels the others, and its error is raised."""
        if sum(self.done) > 0:
            self.progress(sum(self.done), self.size)
        tasks = [asyncio.ensure_future(self._part(number)) for number in range(len(self.parts))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if self.sidecar is not None:
            self.sidecar.unlink(missing_ok=True)

    async def _part(self, number: int) -> None:
        """Fetch one part, waiting out flood waits and going on after up to `RESUMES` drops.

        Args:
            number: the part's index in `self.parts`.

        Raises:
            ConnectionError: the connection dropped more than `RESUMES` times.
        """
        from telethon.errors import FloodWaitError

        start, end = self.parts[number]
        drops = 0
        while self.done[number] < end - start:
            try:
                await self._read(number, start + self.done[number], end)
            except FloodWaitError as error:
                await _flood_wait(self.site, error)
            # Telethon reconnects by itself; the part goes on from its last whole request.
            except ConnectionError:
                drops += 1
                if drops > RESUMES:
                    raise

    async def _read(self, number: int, position: int, end: int) -> None:
        """Write the part's bytes from `position` up to `end`, as Telegram sends them.

        A request must start on a multiple of `REQUEST`, so it asks from the last whole request
        before `position` and drops the bytes the part holds already.

        Args:
            number: the part's index in `self.parts`, whose count of bytes done grows.
            position: the byte of the document to write from.
            end: the byte the part ends before.

        Raises:
            NetworkError: Telegram sent nothing at all (retryable by the engine).
        """
        offset = position - (position % REQUEST)
        skip = position - offset
        requests = -(-(end - offset) // REQUEST)
        before = self.done[number]
        async for chunk in self.site.client.iter_download(
                self.document, offset=offset, limit=requests, request_size=REQUEST,
                file_size=self.size):
            if skip:
                chunk = chunk[skip:]
                skip = 0
                if not chunk:
                    continue
            # Each part's writes land at its own place, between two awaits of the one loop.
            self.file.seek(position)
            self.file.write(chunk)
            position += len(chunk)
            self.done[number] += len(chunk)
            if self.sidecar is not None:
                # The offsets may only count bytes the file holds, or a resume would skip them.
                self.file.flush()
                payload = json.dumps({"size": self.size, "done": self.done}).encode("utf-8")
                _save(self.sidecar, payload)
            self.progress(sum(self.done), self.size)
        if self.done[number] == before:
            raise NetworkError(f"Telegram sent nothing from byte {position} of {self.size}",
                               retryable=True)


def parts(size: int) -> list[tuple[int, int]]:
    """Where each part of a document starts and ends: `CONNECTIONS` for a big one, else one."""
    count = CONNECTIONS if size >= SPLIT_SIZE else 1
    # Each part a whole number of requests, so every request starts on a multiple of its size.
    step = max(-(-size // count // REQUEST) * REQUEST, REQUEST)
    return [(start, min(start + step, size)) for start in range(0, size, step)]


# ---------------------------------------------------------------------------------------------
# The login's steps
# ---------------------------------------------------------------------------------------------

def credentials(site: Site) -> tuple[int, str]:
    """The stored `api_id` and `api_hash`, or asked for when there are none yet.

    Args:
        site: the site that holds the stored secrets and asks the user when they are missing.

    Returns:
        `(api_id, api_hash)`.

    Raises:
        LoginError: none are stored and there is no terminal to ask in, or the `api_id` typed
            isn't a number.
    """
    secrets = site.secrets
    if "api_id" in secrets and "api_hash" in secrets:
        return int(secrets["api_id"]), secrets["api_hash"]
    if not site.interactive:
        raise LoginError("tlg: no api_id and api_hash yet; run flnd login tlg in a terminal")
    where = "From my.telegram.org, under API development tools"
    api_id = site.ask("Telegram api_id", description=where).strip()
    if not api_id.isdigit():
        raise LoginError(f"tlg: api_id is a number, not {api_id!r}")
    return int(api_id), site.ask("Telegram api_hash", secret=True, description=where).strip()


async def log_in(site: Site, how: str, start: str, api_id: int, api_hash: str) -> tuple[str, str]:
    """Log a client in by `how`, from the session string `start` ("" for a new device).

    Args:
        site: the site, used to ask the user and to show messages.
        how: the login method: `"qr"` or `"phone"` log a new device in; `"browser"` only
            checks the session `start` holds.
        start: the session string to start from, or "" for a new device.
        api_id: the app's numeric id from my.telegram.org.
        api_hash: the app's secret hash from my.telegram.org.

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
        if how == "qr":
            await by_qr(site, client)
        elif how == "phone":
            await by_phone(site, client)
        me = await client.get_me()
        if me is None:
            raise LoginError(
                "tlg: Telegram Web's login no longer works; log in at web.telegram.org again"
            )
        return client.session.save(), display_name(me)
    # Telegram's refusal is shown as a login error; any other error reaches the caller.
    except RPCError as error:
        raise LoginError(f"tlg: Telegram refused the login: {error}") from None
    finally:
        await client.disconnect()


async def by_qr(site: Site, client: Any) -> None:
    """Log in by showing QR codes until one is scanned, up to `QR_CODES` times.

    Args:
        site: the site, used to show each QR code and to ask for a two-step password.
        client: a connected Telethon client.

    Returns:
        Nothing: the client is logged in once it returns (after the two-step password, when
        the account has one).

    Raises:
        LoginError: none of the codes was scanned in time.
    """
    from telethon.errors import SessionPasswordNeededError

    qr = await client.qr_login()
    instruction = (
        "scan this in the Telegram app, under Settings → Devices → Link Desktop Device:\n{qr}"
    )
    for _ in range(QR_CODES):
        site.info(instruction, qr=draw_qr(qr.url))
        try:
            await qr.wait()
            return
        # A code that ran out is replaced by a new one, and the scan is waited for again.
        except TimeoutError:
            await qr.recreate()
        except SessionPasswordNeededError:
            return await two_step(site, client)
    raise LoginError("tlg: no QR code was scanned; run flnd login tlg qr again")


async def by_phone(site: Site, client: Any) -> None:
    """Log in by the phone number and the code Telegram sends.

    Args:
        site: the site, used to ask the user for the number, the code and the password.
        client: a connected Telethon client.
    """
    from telethon.errors import SessionPasswordNeededError

    phone = site.ask("Phone number", description="With the country code, such as +44 7700 900123")
    await client.send_code_request(phone)
    code = site.ask("Login code", description="Telegram sent it to your Telegram app, or by SMS")
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        await two_step(site, client)


async def two_step(site: Site, client: Any) -> None:
    """Finish a login with the account's two-step verification password."""
    await client.sign_in(password=site.ask("Two-step verification password", secret=True))


def read_telegram_web(site: Any) -> dict[str, str]:
    """Telegram Web's local storage, from a hidden copy of Chrome's default profile.

    Args:
        site: the login site; its `storage(url)` reads the profile.

    Returns:
        The page's local storage, by key, as text.

    Raises:
        LoginError: Chrome, its profile or the page couldn't be read, or the page didn't load
            on Telegram Web (no network, or a redirect).
    """
    err_msg = "tlg: can't read Telegram Web in Chrome: {error}; try a QR code instead"
    try:
        storage = site.storage(TELEGRAM_WEB)
    except (Error, OSError, RuntimeError, ValueError) as error:
        raise LoginError(err_msg.format(error=error)) from None
    if storage is None:
        raise LoginError("tlg: Chrome could not open Telegram Web; check connection, "
                         "or try a QR code instead")
    return storage


def from_web_storage(storage: Mapping[str, str]) -> str:
    """A Telethon `StringSession` from Telegram Web's local storage.

    Telegram Web keeps, as JSON values (seen 2026-11-11, in both the A and K clients): the
    account's data center in `user_auth.dcID` (or `dc`), and a 256-byte auth key per data
    center, as hex, under `dc<n>_auth_key`.

    Args:
        storage: Telegram Web's local storage, by key, as `read_telegram_web` gives it.

    Returns:
        The session string to store: the data center's address and the auth key.

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
        raise LoginError(
            "tlg: not logged in to Telegram Web in Chrome; log in at web.telegram.org first, "
            "or log in with a QR code"
        )
    string_session = StringSession()
    string_session.set_dc(int(str(dc)), DATA_CENTERS[int(str(dc))], PORT)
    string_session.auth_key = AuthKey(auth_key)
    return string_session.save()


def draw_qr(text: str) -> str:
    """`text` as a QR code in Unicode half blocks, two rows of modules per line.

    Args:
        text: what the code holds: the `tg://login?token=...` URL of a QR login.

    Returns:
        The code's lines, joined by newlines, with a quiet border of 2 modules.
    """
    import qrcode

    code = qrcode.QRCode(border=2)
    code.add_data(text)
    return half_blocks(code.get_matrix())


def half_blocks(matrix: Sequence[Sequence[bool]]) -> str:
    """Draw a QR matrix (True is dark), lighting the light modules, as `qrencode -t UTF8` does.

    A terminal's text is light on dark, so the light modules are drawn and the dark ones left
    as the background; an odd last row is paired with a light one.

    Args:
        matrix: the code's modules, row by row, True for dark.

    Returns:
        One line of text per two rows of modules.
    """
    light = [[not dark for dark in row] for row in matrix]
    if len(light) % 2:
        light.append([True] * len(light[0]))
    lines = zip(light[::2], light[1::2])
    return "\n".join(
        "".join(HALF_BLOCKS[pair] for pair in zip(top, bottom)) for top, bottom in lines
    )


def _json_or_text(value: str) -> Any:
    """`value` decoded as JSON, or as it is when it isn't JSON."""
    try:
        return json.loads(value)
    except ValueError:
        return value


def _version() -> str:
    """flnd's version, as the device's app version; "0" when flnd isn't installed (a checkout).

    Returns:
        The installed version string, or "0".
    """
    from importlib import metadata

    try:
        return metadata.version("flntools")
    except metadata.PackageNotFoundError:
        return "0"


def _client(string: str, api_id: int, api_hash: str) -> Any:
    """A Telethon client for session string `string` ("" for a new one), not yet connected.

    Args:
        string: the stored session string, or "" to start a new session.
        api_id: the app's numeric id from my.telegram.org.
        api_hash: the app's secret hash from my.telegram.org.

    Returns:
        A `TelegramClient`; the caller connects it and disconnects it.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # A device named flnd in the account's settings, for a QR or phone login.
    return TelegramClient(
        StringSession(string), api_id, api_hash, device_model="flnd", app_version=_version(),
    )
