# Telegram (`tlg`)

flnd downloads the photos, videos and other files of a Telegram chat, group or channel that your
account can see. It talks to Telegram as a client of its own, through the Telethon library (an
SDK for Telegram's MTProto protocol), not through a web page. A login is required. A browser is
used only by the `browser` login method, to read an existing Telegram Web login. Downloads never
open a browser. The plugin is `src/plugins/tlg.py`, on plugin API 2.

A *target* is what you give `flnd get`; a *category* is one part of a target, named after
its last `/`. See the [glossary](../glossary.md) and the [CLI guide](../cli.md).

## Targets

Every form below is read by the one `chat` route. Its patterns cover the links and the numeric
ids. The plugin also overrides `resolve`, so that any text after `tlg:` is read by the route, and
a title is looked up among your chats.

| You type | Route | What it downloads |
| --- | --- | --- |
| `tlg:@examplechannel` or `tlg:examplechannel` | `chat` | a chat by its username |
| `https://t.me/examplechannel` | `chat` | the same |
| `https://t.me/s/examplechannel` (web preview) | `chat` | the same |
| `https://t.me/+AbCdEf` | `chat` | a chat by its invite link |
| `https://t.me/joinchat/AbCdEf` | `chat` | the same, older link form |
| `https://t.me/c/1234567890/5` | `chat` | a private chat by id, read as `-1001234567890` |
| `tlg:-1001234567890` | `chat` | a chat by its numeric id |
| `tlg:Example Chat` | `chat` | a chat by its title, among your chats |

A username is 4 to 32 characters and starts with a letter. A message number at the end of a link
(`/5` above) is ignored: the whole chat is the target.

How a chat is found:

1. An invite or a username is looked up directly. An invite you haven't joined fails with "join
   it first". A username that can't be looked up falls through to step 3.
2. A number is looked up as an id. If that fails, it falls through to step 3.
3. Otherwise flnd lists your chats and takes the one whose id matches, or whose title matches
   with case ignored. Two chats with the same title fail with both ids, so you can give one as
   `tlg:<id>`. A number that is none of your chats fails as hidden: the chat is private, or this
   account left it.

A chat found this way is kept in the plugin's state, per account and by the text you typed (case
ignored), with the id and access hash Telegram gave it. A later run finds it again without
listing your chats. If the chat no longer has that name, the lookup runs again.

## Categories

| Category | What it holds |
| --- | --- |
| `photo` | the chat's photos |
| `video` | the chat's videos |
| `file` | the chat's other files (documents, archives, audio) |

Each is newest first. Without categories a chat gets every one; `tlg:<chat>/photo,video` names one
or more of these.

## Logging in

Telegram has a login of its own, not cookies. The plugin declares `login = TelegramLogin()`, a
`Login` subclass whose `methods` are `browser`, `qr` and `phone`. `flnd auth login tlg <method>`
runs its `start`. A login is kept when a session is stored.

- `browser`: reads Telegram Web's login from a hidden copy of Chrome's `Default` profile. It
  opens Telegram Web there, reads its local storage, and turns it into a Telethon session. This
  reuses that login and adds no new device. You must be logged in at `web.telegram.org` in that
  Chrome profile first.
- `qr`: shows a QR code in the terminal to scan in the Telegram app, under Settings, Devices,
  Link Desktop Device. A code lasts about 30 seconds. Up to 5 are shown before giving up.
- `phone`: asks for your phone number and the code Telegram sends.

`qr` and `phone` add a device named `flnd`, and ask for your two-step verification password if
the account has one. Both ask questions, so they run only in a terminal.

The first login also asks for an `api_id` and `api_hash`, which you create at `my.telegram.org`
under API development tools. They are kept with the session in the vault, `metadata/secret.db`,
in the home folder. The key that opens it is kept in the user's key file, outside the home.
What proves a login is the stored session. The API pair alone does not.

Without a login, a `tlg` target fails with "tlg: not logged in", and flnd says to run
`flnd auth login tlg`. Each run checks the session when it connects. If it was revoked (a logout
from another device, for example), the run fails with "tlg: the saved login no longer works", and
only a new login helps.

## How it fetches

All traffic goes over MTProto to Telegram's data centers, through one Telethon client per
account. There are no HTTP endpoints. Telethon's calls are async and run on the run's own loop,
so the plugin needs no thread of its own. The client connects once per run and disconnects when
the run ends, even when it stops early.

1. **Listing.** Each category asks Telegram for the chat's messages with a server-side filter
   (`InputMessagesFilterPhotos`, `InputMessagesFilterVideo` or `InputMessagesFilterDocument`), so
   Telegram sends only messages of that type, newest first. With `--when`, the listing starts
   just after its last day (UTC). The plugin makes no other stop: the engine ends the listing
   once 5 posts in a row are older than `--when`'s start, or, in a sync, once it has caught up.
2. **Photos** are downloaded whole with Telethon's `download_media`.
3. **Videos and files** (Telegram documents) are downloaded as ranged parts. When the engine has
   room, a document of 16 MiB or more is split into up to 4 parts of at least 8 MiB each, fetched
   at once. A smaller one, or one with no room, is one part. Each request is 512 KiB, and starts
   on a multiple of 512 KiB. Where a part begins in the middle of one, the request starts at the
   boundary before it, and the bytes before the part's start are dropped.
4. **Resume.** A partly written file keeps a record of how many bytes each part has written, so
   a later run goes on from there. A dropped connection is retried by the engine, from the bytes
   already written.

**Flood waits** (Telegram's rate limit, which names the seconds to wait): the client sleeps
through a wait of 60 seconds or less by itself. A longer one, while listing, finding a chat or
downloading, is a block hold, `ctx.hold("Telegram flood wait", seconds=...)`, which the run shows
and waits out. A hold lasts the longer of the seconds Telegram named and the engine's round wait
(30 s, 2 min, 8 min). The listing then resumes after the last message it gave, and a download from
where it stopped. The files and parts that meet one flood wait together share one hold per
account.

**Pacing and limits.** The plugin makes no HTTP requests, so `pace` does not apply to it. Its
`limits` are `{"t.me": 3}`: every download shares one Telegram client and its flood waits, so
more than 3 at once would only meet the flood limit sooner.

## Where files go

The class declares:

- `folder = "{chat}/{category}"`: `chat` is the chat's title, or a person's name, or else its
  username or id.
- No `filename` of its own. Each file is named by the plugin as `<date>_<message id>`, then
  `_<original name>` when Telegram has one, or else the file's extension. The date is the
  message's UTC day.
- `fields = ("chat", "chat_id")`: the post fields the templates use.
- `key = "{chat_id}:{post}"`: the history key, stored as `tlg:<chat id>:<message id>`, since a
  message id repeats from chat to chat. `tests/data/tlg_names.json` pins every name and key.

Files go under `.flnd/media/tlg/<target>/`, where `<target>` is what the target names (a
username, an invite, an id or a title):

```
.flnd/media/tlg/examplechannel/Example Channel/photo/2026-01-02_1234.jpg
.flnd/media/tlg/examplechannel/Example Channel/file/2026-01-02_1235_report.pdf
```

## Quirks

- **Private chat links.** `t.me/c/<n>` holds a channel's id without the `-100` prefix that the
  client APIs use. flnd adds it back.
- **Web preview links.** `t.me/s/<name>` is the web preview of a channel. The `s` is dropped.
- **Old invite links.** `t.me/joinchat/<hash>` is read as the newer `t.me/+<hash>`.
- **Telegram Web's login.** Telegram Web keeps its login in the browser's local storage, as JSON:
  the account's data center in `user_auth.dcID` (or `dc`), and a 256-byte auth key per data
  center, as hex, under `dc<n>_auth_key`. flnd builds a Telethon session from them. A login
  without a usable key is refused with "not logged in to Telegram Web".
- **Request alignment.** Telegram's largest file request is 512 KiB, and a request may not cross a
  1 MiB boundary. So every request starts on a multiple of 512 KiB, as described above.
- **Resume safety.** A damaged record of a file's progress starts that file again, rather than
  trusting it.

## Limits

- Only chats your account can see. An invite link you haven't joined fails with "join it first",
  as `NotFound`. flnd never joins a chat for you. A private chat, or one the account left, fails
  as `Hidden`.
- A link to a single message is not read as its message: it downloads the whole chat. A
  `web.telegram.org` link matches no route, and is refused.
- The plugin declares no `unsupported` links.
- Flood waits can be long. Each is shown and waited out, for the seconds Telegram names or the
  engine's round wait if that is longer, and then tried again. Rounds are counted in a row. The
  count starts again after 10 minutes with no flood wait, and a fourth round in a row fails the
  job as blocked.
- The `browser` login reads only Chrome's `Default` profile.

## Testing

Unit tests fake the network, the browser and the clock; the Telethon client is faked too. Live
checks are done by hand, on a test account, and never in unit tests. A change to this plugin must
keep the routes and the link forms above, the three categories and their server filters, the
flood-wait holds, and the file names and history keys that `tests/data/tlg_names.json` pins, so
that files already saved are not saved again.
