# tlg

Phase 4, step 6. A flnd source for Telegram, fully integrated: its targets go through `flnd download` with the same options as every source, and its login through `flnd login tlg`. It is the first user of the engine's own-login methods, SDK client and async `run`.

```sh
flnd login tlg                                                     # once: asks Telegram Web, QR code, or phone and code
flnd download tlg:@examplechannel                                        # photos and videos (the default kinds)
flnd download "tlg:Lens Club" --kind photo,archive                 # by title
flnd download tlg:-1001234567890 --since 2026-01-01 --unpack       # by id, from a date, unpack archives
flnd download tlg:@examplechannel ins:example_user --zip                # with other sources, in parallel
```

It downloads every photo, video, file or archive from a chat, group or channel your account can see. It resumes where it stopped and waits out Telegram's rate limits. Everything else (paths, skipping, the limit, retries, progress, history, `--unpack`, `--zip`) is the engine's.

## Library

[Telethon](https://docs.telethon.dev), with a user account over MTProto.
- **Not the Bot API:** a bot can't read a group's history from before it joined, and is limited to 20 MB downloads.
- **Not Pyrogram:** its maintenance has been uncertain.

Telethon is asyncio-only. The source uses an async `connect` and async `fetch` functions, and the engine runs that group's downloads as async `worker` tasks.

`telethon` goes in flnd's dependencies with the others (`httpx`, `playwright`, `parsel`).

## The source

```python
class Telegram(Source):
    code = "tlg"
    name = "Telegram"
    domains = ("t.me", "telegram.org")
    wait = flood_wait                          # FloodWaitError.seconds, for the engine's retries

    login_methods = ("browser", "qr", "phone")
    requires_login = True

    def login(self, session, method): ...      # flnd login tlg [--method browser|qr|phone]
    def logged_in(self, session) -> bool: ...
    def connect(self, session): ...            # an async context manager yielding the Telethon client

    def resolve(self, text) -> Target:         # "@name", a title, a t.me link, or an id -> kind "chat"
        ...

    async def run(self, session) -> None:
        chat = await resolve(session, session.target.value)    # an ambiguous title fails, listing ids
        session.folder = safe_title(chat)
        for kind in session.kinds:                              # --kind: only the server filters wanted
            async for message in session.client.iter_messages(chat, filter=FILTERS[kind], offset_date=session.until):
                if not session.wants(date=message.date.date()):     # older than --since: done with this kind
                    break
                if kind == "archive" and not is_archive(message.document):
                    continue
                session.save(fetcher(session.client, message), file_name(message), kind=kind,
                             date=message.date.date(), folder=f"{session.folder}/{kind}",
                             key=f"{chat.id}:{message.id}", size=media_size(message))
```

The expected size is about 150–200 lines, because the engine does the rest.

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
| `browser` | Telegram Web keeps its login in the page's local storage, not in cookies: an auth key per data center, and which data center is the account's. flnd opens a copy of your browser profile (`browser.installed_profile`), visits `web.telegram.org`, reads those values, and builds a Telethon `StringSession` from the account's data center and auth key. It then checks the session with `get_me()` | No typing. It reuses Telegram Web's login rather than creating a new device, so logging out of Telegram Web logs flnd out too, and Telegram sees both as one session. The docstring says so. It works only when you're logged in to Telegram Web in that browser profile |
| `qr` | Telethon's QR login. flnd draws the QR code in the terminal with Unicode half blocks, and you scan it in the Telegram app under Settings → Devices → Link Desktop Device. If the account has 2FA, the password is asked afterwards | No phone number or code. A new device appears in your Telegram settings, named after flnd |
| `phone` | asks for the phone number, then the code Telegram sends, then the 2FA password if the account has one (`secret=True`) | Always works |

- **Every method** first asks once for `api_id` and `api_hash` (from my.telegram.org), with `session.ask(..., secret=True)` for the hash, kept in `session.secrets`. Every method ends the same way: Telethon's `StringSession` goes in `session.secrets["session"]`, encrypted at rest, never in a plain `.session` file.
- **Running it again** replaces the session. Without a terminal, `qr` and `phone` fail, naming the command to run. `browser` needs no questions once `api_id` and `api_hash` are stored, so it also works without a terminal.
- **`logged_in(session)`** is `"session" in session.secrets`. Telegram has `requires_login = True`, so at download time an interactive run offers the login, and otherwise the target fails with the engine's message.

## Download details

- **Chats:** a title matching several chats fails, listing the candidates with their ids, so the next run can pass `tlg:<id>`. `flnd download` never asks.
- **Kinds:** these map to Telegram's server-side filters: photos, videos, and documents for `file` and `archive`. An archive is a document whose MIME type or extension is zip, 7z, rar or tar.
- **Resume:** the history key is `<chat_id>:<message_id>`, so a second run skips what it already has. A partial file never enters the history, because the engine writes through `path.atomic`.
- **Rate limits:** `wait = flood_wait` makes the engine's retries honor `FloodWaitError.seconds`, instead of its usual backoff.
- **Names:** `<date>_<message_id>_<original name>` in `<chat title>/<kind>/`. Album files share a date and message group, so they sort together.
- **Progress:** `fetcher` passes the engine's `progress` callback to Telethon's `progress_callback`, which is the `bar.update(done, total)` shape.

## Engine features this source is the first user of

Each is general and specified in [engine](engine.md):
- reading `session.kinds`, `since` and `until`, and `wants`, to ask Telegram only for what's wanted;
- `Source.login`, `login_methods`, `logged_in` and `requires_login`, with `flnd login tlg --method …`;
- `session.ask` / `choose` / `secrets`;
- `Source.connect` and `session.client`;
- `session.save(fetch, …)`;
- an async `run`;
- a source-level `wait`.

## Tests

Tests use a fake Telethon client (an async stub with `iter_messages`, `download_media`, `get_entity` and `get_dialogs`). There is no network.

- **Login, each method,** with a fake Telethon client:
  - `browser` from a fixture local-storage dump: the right data center and key, and a clear error when not logged in to Telegram Web;
  - `qr`, with the token accepted, a timeout, and 2FA afterwards;
  - `phone`, including 2FA.

  In every case the session lands in `secrets`, nothing readable is left on disk, and a missing `--method` is asked.
- **Login errors:** `flnd login ins --method qr` is a usage error (ins has `browser` and `file`); `qr` or `phone` without a terminal fails, naming the command to run.
- **Downloads:**
  - `flnd download tlg:@x --kind photo,archive --since …` asks Telegram only for photos and documents, and stops at the `--since` date;
  - the default kinds download photos and videos only.
- **Resume:** a second run downloads nothing.
- **Rate limits:** a `FloodWaitError(seconds=3)` waits 3 s (fake clock) and retries.
- **Chats:** an ambiguous title fails, listing ids.
- **Kinds:** they map to filters, and archives are recognised by MIME type and extension.
- **With other sources:** `flnd download tlg:@x ins:y` runs both groups in parallel, Telegram's on its async loop.
- **`--unpack` and `--zip`:** they work on Telegram downloads through the engine.
