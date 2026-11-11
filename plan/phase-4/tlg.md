# tlg

Phase 4, step 6. A flnd source for Telegram, fully integrated: its targets go through `flnd download` with the same options as every source, and its login through `flnd login tlg`. It is the first user of the engine's own-login methods, SDK client and async `run`.

```sh
flnd login tlg                                                     # once: asks Telegram Web, QR code, or phone and code
flnd download tlg:@examplechannel                                        # photos and videos (the default categories)
flnd download "tlg:Lens Club" --from photo,file                    # by title
flnd download tlg:-1001234567890 --since 2026-01-01 --unpack       # by id, from a date, unpack archives
flnd download tlg:@examplechannel ins:example_user --zip                # with other sources, in parallel
```

It downloads every photo, video, file or archive from a chat, group or channel your account can see. It resumes where it stopped and waits out Telegram's rate limits. Everything else (paths, skipping, the limit, retries, progress, history, `--unpack`, `--zip`) is the engine's.

## Library

[Telethon](https://docs.telethon.dev), with a user account over MTProto.
- **Not the Bot API:** a bot can't read a group's history from before it joined, and is limited to 20 MB downloads.
- **Not Pyrogram:** its maintenance has been uncertain.

Telethon is asyncio-only. The source uses an async `connect` and async `fetch` functions, and the engine runs that group's downloads as async `worker` tasks.

`telethon` goes in flnd's dependencies with the others (`httpx`, `playwright`, `parsel`), and so does `qrcode`, which encodes the QR login (flnd only draws its matrix). Both are imported lazily, inside the functions that use them, so `flnd --help` never loads Telethon.

The guard test forbids a source from importing `httpx`, Playwright, `worker`, `kv`, `log` and `prompt`. Telethon isn't among them: it is the SDK the source declares through `connect`, and the engine owns its loop. The `browser` login uses `shared.browser`, which isn't forbidden either, because it needs your installed Chrome's profile, not the session's incognito browser.

## The source

```python
class Telegram(Source):
    code = "tlg"
    name = "Telegram"
    domains = ("t.me", "telegram.org")
    wait = flood_wait                          # FloodWaitError.seconds, for the engine's retries
    categories = ("photo", "video", "file")    # its content is naturally typed, so the types are the categories
    default_categories = ("photo", "video")

    login_methods = ("browser", "qr", "phone")
    requires_login = True

    def login(self, session, method): ...      # flnd login tlg [--method browser|qr|phone]
    def logged_in(self, session) -> bool: ...
    def connect(self, session): ...            # an async context manager yielding the Telethon client

    def resolve(self, text) -> Target:         # "@name", a title, a t.me link, or an id -> kind "chat"
        ...

    async def run(self, session) -> None:
        chat = await find_chat(session.client, session.target.value)   # an ambiguous title fails, listing ids
        session.folder = display_name(chat).replace("/", "_")
        before = the day after session.until, at 00:00 UTC, or None    # offset_date is exclusive; --until isn't
        for kind in session.categories:                                 # --from: only the server filters wanted
            async for message in session.client.iter_messages(chat, filter=server_filter(kind), offset_date=before):
                day = message.date.date()
                if session.since and day < session.since:               # older than --since: done with this kind
                    break
                session.save(fetcher(session.client, message), file_name(message), category=kind, date=day,
                             folder=f"{session.folder}/{kind}", key=f"{chat.id}:{message.id}",
                             size=message.file.size if message.file else None)
```

It is about 225 lines of code, 150 of them the download and the rest the three logins, because the engine does the rest.

`run` can be `async def`. The engine awaits it on the group's loop when `connect` is async, and calls it plainly otherwise. This is one more line in the engine, and it lets async SDKs read naturally.

## Login

Telegram has three ways to log in. `flnd login tlg` asks which one when `--method` isn't given:

```sh
flnd login tlg --method browser     # reuse Telegram Web's login from your browser
flnd login tlg --method qr          # scan a QR code with the Telegram app
flnd login tlg --method phone       # phone number, code, 2FA password
```

| Method | How it works | Notes |
| --- | --- | --- |
| `browser` | Telegram Web keeps its login in the page's local storage, not in cookies: an auth key per data center (`dc<N>_auth_key`, hex), and which data center is the account's (`user_auth.dcID`, or `dc`). Both web clients store them JSON-encoded. flnd opens a headless copy of Chrome's default profile (`browser.installed_profile()`), visits `web.telegram.org`, reads `localStorage`, and builds a Telethon `StringSession` from the account's data center (with Telegram's well-known address for it, port 443) and auth key. It then checks the session with `get_me()` | No typing. It reuses Telegram Web's login rather than creating a new device, so logging out of Telegram Web logs flnd out too, and Telegram sees both as one session. The docstring says so. It works only when you're logged in to Telegram Web in Chrome's default profile: `installed_profile` copies only Chrome, so `--browser` and `--profile` don't apply to it |
| `qr` | Telethon's QR login. flnd draws the QR code in the terminal with Unicode half blocks (the light modules lit, for a dark terminal, as `qrencode -t UTF8` does), and you scan it in the Telegram app under Settings → Devices → Link Desktop Device. A code lasts about 30 s; an expired one is replaced, up to 5 codes, and then the login fails. If the account has 2FA, the password is asked afterwards | No phone number or code. A new device appears in your Telegram settings, named after flnd |
| `phone` | asks for the phone number, then the code Telegram sends, then the 2FA password if the account has one (`secret=True`) | Always works |

- **Every method** first asks once for `api_id` and `api_hash` (from my.telegram.org), with `session.ask(..., secret=True)` for the hash, kept in `session.secrets`. They are stored with the session, only once a login works, so a mistyped pair is asked again. Every method ends the same way: Telethon's `StringSession` goes in `session.secrets["session"]`, encrypted at rest, never in a plain `.session` file, and `• tlg: logged in as <name>` is printed.
- **Running it again** replaces the session. Without a terminal (`session.interactive` is false), `qr` and `phone` fail before anything is asked or sent, naming the command to run: `✗ tlg: --method qr asks questions; run flnd login tlg --method qr in a terminal`. `browser` needs no questions once `api_id` and `api_hash` are stored, so it also works without a terminal.
- **Failures** raise `LoginError`, which `flnd login` prints with exit code 1: Telegram refusing a code or password (`tlg: Telegram refused the login: …`), Telegram Web not logged in, or Chrome unreadable (suggesting `--method qr`).
- **`logged_in(session)`** is `"session" in session.secrets`. Telegram has `requires_login = True`, so at download time an interactive run offers the login, and otherwise the target fails with the engine's message.

## Download details

- **Chats:** `resolve` is offline: `@name`, `t.me/name` (and `t.me/s/name`, `t.me/name/<post>`) give the username; `t.me/c/<id>/…` gives `-100<id>`; `t.me/+<hash>` and `t.me/joinchat/<hash>` give `+<hash>`; anything else is a title or an id. At run time a username or invite goes to `get_entity` (an invite only works for a chat you're in), and a title or an id is matched against the account's dialogs, titles case-insensitively; a username nobody has falls back to the titles. A title matching several chats fails, listing their ids (`2 chats are titled 'Lens Club': -1001001, -1001002; pass one as tlg:<id>`), so the next run can pass `tlg:<id>`. `flnd download` never asks.
- **Categories:** `photo`, `video` and `file` map to Telegram's server-side filters: photos, videos, and documents. An archive is a `file`; `--unpack` unpacks it by its name (zip, 7z, rar or tar).
- **Resume:** the history key is `<chat_id>:<message_id>`, so a second run skips what it already has. A partial file never enters the history, because the engine writes through `path.atomic`.
- **Rate limits:** `wait = flood_wait` makes the engine's download retries honor `FloodWaitError.seconds`, instead of its usual backoff. API calls (reading the chat's messages) are Telethon's own: `connect` sets its `flood_sleep_threshold` to a day, so Telethon sleeps through a flood wait rather than raising.
- **Names:** `<date>_<message_id>_<original name>`, or `<date>_<message_id><ext>` for media without a name (photos), in `<target>/<chat title>/<kind>/`. Album files share a date and have consecutive ids, so they sort together.
- **Downloads** are written by `download_media` into the engine's part file, opened by the source, so Telethon never renames it.
- **Many files:** `run` saves on the group's loop, which every download also needs; the engine hands those saves to the pool from another thread, so a long chat never deadlocks the pool ([engine › SDK clients](engine.md#sdk-clients)).
- **Progress:** `fetcher` passes the engine's `progress` callback to Telethon's `progress_callback`, which is the `bar.update(done, total)` shape.

## Engine features this source is the first user of

Each is general and specified in [engine](engine.md):
- reading `session.categories`, `since` and `until`, and `wants`, to ask Telegram only for what's wanted;
- `Source.login`, `login_methods`, `logged_in` and `requires_login`, with `flnd login tlg --method …`;
- `session.ask` / `choose` / `secrets`;
- `Source.connect` and `session.client`;
- `session.save(fetch, …)`;
- an async `run`;
- a source-level `wait`.

## Tests

Tests use a fake Telethon client (an async stub with `iter_messages`, `download_media`, `get_entity` and `get_dialogs`, plus the login calls), patched in for `tlg._client`, and Telethon's real error classes. There is no network and no real login; tlg is unverified against live Telegram.

- **Login, each method,** with a fake Telethon client:
  - `browser` from a fixture local-storage dump: the right data center and key, and a clear error when not logged in to Telegram Web;
  - `qr`, with the token accepted, a timeout, and 2FA afterwards;
  - `phone`, including 2FA.

  In every case the session lands in `secrets`, nothing readable is left on disk, and a missing `--method` is asked.
- **Login errors:** `flnd login ins --method qr` is a usage error (ins has `browser` and `file`); `qr` or `phone` without a terminal fails, naming the command to run.
- **Downloads:**
  - `flnd download tlg:@x --from photo,file --since …` asks Telegram only for photos and documents, and stops at the `--since` date;
  - the default categories download photos and videos only.
- **Resume:** a second run downloads nothing.
- **Rate limits:** a `FloodWaitError(seconds=3)` waits 3 s (fake clock) and retries.
- **Chats:** an ambiguous title fails, listing ids.
- **Categories:** they map to filters, and an archive sent as a file is unpacked with `--unpack`.
- **With other sources:** `flnd download tlg:@x ins:y` runs both groups in parallel, Telegram's on its async loop.
- **`--unpack` and `--zip`:** they work on Telegram downloads through the engine.
