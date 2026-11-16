# Telegram (`tlg`)

flnd downloads the photos, videos and other files of a Telegram chat, group or channel that your
account can see. It talks to Telegram as a client of its own, through the Telethon library (an
SDK for Telegram's MTProto protocol), not through a web page. A login is required; no browser is
used to download, only, if you choose it, to read an existing Telegram Web login.

A *target* is what you give `flnd download`; a *category* is one part of a target, chosen with
`--only`. See the [glossary](../glossary.md) and the [CLI guide](../cli.md).

## Targets

Every form below is read by the one `chat` route. No URL pattern can match a chat's title, so the
source reads the text itself instead of through patterns.

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

1. An invite or a username is looked up directly. An invite you haven't joined fails with "join it
   first". A username that can't be looked up falls through to step 3.
2. A number is looked up as an id.
3. Otherwise flnd lists your chats and takes the one whose id matches, or whose title matches
   with case ignored. Two chats with the same title fail with both ids, so you can give one as
   `tlg:<id>`.

## Categories

| Category | What it holds |
| --- | --- |
| `photo` | the chat's photos |
| `video` | the chat's videos |
| `file` | the chat's other files (documents, archives, audio) |

Each is newest first. Without `--only` a chat gets every category; `--only` names one or more
of these.

## Logging in

`flnd login tlg <how>` takes one of three ways:

- `browser`: reads Telegram Web's login from Chrome's `Default` profile, in a headless browser,
  and turns it into a Telethon session. This reuses that login and adds no new device. You must
  be logged in at `web.telegram.org` in that Chrome profile first.
- `qr`: shows a QR code in the terminal to scan in the Telegram app, under Settings, Devices,
  Link Desktop Device. A code lasts about 30 seconds; up to 5 are shown before giving up.
- `phone`: asks for your phone number and the code Telegram sends.

`qr` and `phone` add a device named `flnd`, and ask for your two-step verification password if
the account has one. Both ask questions, so they run only in a terminal.

The first login also asks for an `api_id` and `api_hash`, which you create at `my.telegram.org`
under API development tools. They are kept with the session in flnd's encrypted secrets store.
What proves a login is the stored `session`; the API pair alone does not.

Without a login, a `tlg` target fails with "not logged in; run flnd login tlg". Each run checks
the session when it connects. If it was revoked (a logout from another device, for example), the
run fails with "the saved login no longer works", and only a new login helps.

## How it fetches

All traffic goes over MTProto to Telegram's data centers through one Telethon client per run;
there are no HTTP endpoints. The client connects once per run and disconnects when the run ends,
even when it stops early.

1. **Listing.** Each category asks Telegram for the chat's messages with a server-side filter
   (`InputMessagesFilterPhotos`, `InputMessagesFilterVideo` or `InputMessagesFilterDocument`), so
   Telegram sends only messages of that type, newest first. With `--when`, the listing starts
   just after its end day (UTC); the engine stops it once 5 posts in a row are older than its
   start.
2. **Photos** are downloaded whole with Telethon's `download_media`.
3. **Videos and files** (Telegram documents) are downloaded in parts. A document of 8 MiB or more
   is split into 4 parts fetched at once; a smaller one is one part. Each request is 512 KiB and
   starts on a multiple of 512 KiB.
4. **Resume.** While a document downloads, a small JSON file beside the partial file records how
   many bytes each part has written. A later run goes on from there. A part survives up to 3
   dropped connections, each time going on from where it stopped.

**Flood waits** (Telegram's rate limit, which names the seconds to wait): the client sleeps
through any wait up to 24 hours by itself, while listing or downloading. A longer wait during a
download is waited out with flnd's back-off countdown, and the download tries again.

**Pacing:** `pace` keeps its default, but it applies only to HTTP requests, which this source
doesn't make. `limits` is `{"t.me": 3}`: its files are fetched by its own client, so they count
against its first domain, 3 at once, since every download shares one client and its flood waits,
and more at once only meets the flood limit sooner.

## Where files go

The class declares:

- `folder = "{chat}/{category}"`: `chat` is the chat's title, or a person's name, or else its
  username or id.
- No `filename` of its own. Each file is named `<date>_<message id>`, then `_<original name>`
  when Telegram has one, or else the file's extension. The date is the message's UTC day.
- `key = "{chat_id}:{id}"`: the history key, stored as `tlg:<chat id>:<message id>`, since a
  message id repeats from chat to chat.

Files go under `.flnd/media/tlg/<target>/`, where `<target>` is what the target names (a
username, an invite, an id or a title):

```
.flnd/media/tlg/examplechannel/Example Channel/photo/2026-01-02_1234.jpg
.flnd/media/tlg/examplechannel/Example Channel/file/2026-01-02_1235_report.pdf
```

## Quirks

- **Private chat links.** `t.me/c/<n>` holds a channel's id without its `-100` prefix; flnd adds
  it back. Handled since 2026-11-11.
- **Web preview links.** `t.me/s/<name>` is the web preview of a channel; the `s` is dropped.
  Handled since 2026-11-11.
- **Old invite links.** `t.me/joinchat/<hash>` is read as the newer `t.me/+<hash>`. Handled since
  2026-11-11.
- **Telegram Web's login.** Telegram Web keeps its login in the browser's local storage, as JSON:
  the account's data center in `user_auth` (or `dc`), and a 256-byte auth key per data center.
  flnd builds a Telethon session from them. Handled since 2026-11-11.
- **Request alignment.** Telegram's largest file request is 512 KiB, and a request may not cross a
  1 MiB boundary, so every part starts on a multiple of 512 KiB. Handled since 2026-11-13.
- **Resume safety.** The partial file is flushed before its offsets are saved, so the offsets
  never count bytes the file doesn't hold. A damaged offsets file starts that file again. Handled
  since 2026-11-16 (resuming itself since 2026-11-13).

## Limits

- Only chats your account can see. An invite link you haven't joined is refused; flnd never joins
  a chat for you.
- Links on `web.telegram.org` and links to a single message are not read as their message: a
  `web.telegram.org` link is refused, and a message link downloads the whole chat.
- The source declares no `unsupported` links.
- Flood waits can be long: Telethon may sleep for hours rather than stop.
- The `browser` login reads only Chrome's `Default` profile.
