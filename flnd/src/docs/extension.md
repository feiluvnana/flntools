# The Chrome extension

flnd's Chrome extension lets flnd use the browser you are logged in with: read a site's cookies,
open tabs, read their pages and the answers they load. It works in Chrome, Chromium, Edge and
Brave. This page says what the extension may do, how flnd talks to it, and how to install it.

The extension and its host are built. Logging in through it (`flnd auth login <code> chrome`)
and letting a plugin's browser verbs run in your Chrome go through `src/engine/chrome.py`
when the plugin or config chooses Chrome.

## The parts

```
  a run (flnd get …)            flnd host                    Chrome
  ┌──────────────────────┐  relay   ┌─────────────────┐  stdio   ┌──────────────────────┐
  │ extension.connect()  │ ───────▶ │ Host: checks    │ ───────▶ │ background.js runs   │
  │ client.call(op, …)   │ ◀─────── │ hello, routes   │ ◀─────── │ the op, page.js in   │
  └──────────────────────┘  socket  │ answers by id   │  native  │ the tab; popup.js    │
                            or pipe └─────────────────┘ messages └──────────────────────┘
```

- **The extension**, `src/extension/`: `manifest.json`, `background.js` (the service worker),
  `page.js` (the functions it runs inside a page), `popup.html` and `popup.js`. They ship
  inside the wheel, in `flnd/extension/`.
- **The host**, `flnd host`: a hidden command that Chrome starts when the extension loads, and
  stops when the extension goes. It talks to the extension on its standard input and output.
- **The relay**: a socket (a named pipe on Windows) on which the host waits for flnd's runs.
  Several runs can share one host at a time.
- **The client**, in `src/engine/extension.py`: what a run uses on its asyncio loop.
  `connect()` reaches the relay and says `hello`; `await client.call(op, **args)` runs one op.

## What the extension may read

| permission | why |
| --- | --- |
| `cookies` | to give flnd a site's cookies, for its requests |
| `tabs` | to open, list and close tabs, and see their address |
| `scripting` | to read a page's HTML, click and scroll in it, read its local storage and list its media |
| `debugger` | to read the answers a page loads (`capture`), run a script (`evaluate`) and take a screenshot; attached only for those, and detached right after |
| `storage` | the extension's own settings |
| `nativeMessaging` | to talk to `flnd host` |

The sites it may read are the built-in plugins' domains (`host_permissions`, which
`make manifest` writes from the plugins). Other plugins' domains are optional permissions: the
popup asks for them, one site at a time, with **Allow**. Chrome shows a bar on a tab while the
debugger is attached to it.

The manifest has a fixed `key`, so the extension's ID, and the one origin the host serves
(`chrome-extension://<id>/`), never change.

## Install

```sh
flnd extension install
```

1. It copies the extension to `~/.config/flnd/extension/unpacked/`, makes a 32-byte secret
   in `~/.config/flnd/extension/token.json` (mode 0600; a secret already there is kept), and
   records the home the popup acts on in `~/.config/flnd/extension/host.json`: `--home`, else
   `$FLND_HOME`, else `.flnd` in the current folder. The secret and `host.json` stay outside
   `unpacked/`, so they are not part of the extension, and its own origin can't fetch
   `chrome-extension://<id>/token.json`.
2. It writes the host's manifest, `com.flnd.host.json`, for every Chromium browser it finds:
   - on macOS, in `~/Library/Application Support/<Google/Chrome | Chromium | Microsoft Edge |
     BraveSoftware/Brave-Browser>/NativeMessagingHosts/`;
   - on Linux, in `~/.config/<google-chrome | chromium | microsoft-edge |
     BraveSoftware/Brave-Browser>/NativeMessagingHosts/`;
   - on Windows, as the key `HKCU\Software\<vendor>\NativeMessagingHosts\com.flnd.host`.

   Its `allowed_origins` is the extension's one origin, so Chrome lets no other extension
   start the host. Its `path` is `flnd-host` (`flnd-host.bat` on Windows) in
   `~/.config/flnd/extension/`, a script that runs `flnd host` with the Python that installed
   it.
3. It prints three steps: open `chrome://extensions`, turn on **Developer mode**, click
   **Load unpacked** and choose `~/.config/flnd/extension/unpacked/`.
4. It waits, with a spinner, up to 10 minutes for the extension to say hello.

`flnd extension uninstall` removes `~/.config/flnd/extension/` (the unpacked extension, the
secret and `host.json`) and the host's manifests. Remove the extension from
`chrome://extensions` yourself.

flnd never follows a symlink here: a `token.json` that is one is refused with an error naming
it, and a folder of the relay's or the extension's that is a symlink is refused before its mode
is set.

## The relay and the secret

The host listens on `$XDG_RUNTIME_DIR/flnd/host.sock`, else `~/.config/flnd/run/host.sock`. The
folder is mode 0700 and the socket 0600, and a run checks that the folder is yours before it
connects. On Windows it is the named pipe `\\.\pipe\flnd-host-<user>`, owned by your user,
whose DACL lets in only your user and which refuses remote clients; a run checks the pipe's
owner before it sends the secret, so another user's pipe of that name never gets it.

A run's first message must be `hello` with the secret from `token.json`, which must be yours
and mode 0600. A run without it, or with a wrong one, is refused and disconnected before any op
reaches the extension. So is a first message over 64 KiB, and a run that says nothing for 5 s.
The secret stays in the host: `hello` reaches the extension without it.

## Messages

Each message is a 4-byte length, in the machine's byte order, then that many bytes of JSON, on
the relay as on Chrome's pipe.

- An op: `{"id": int, "op": str, "args": {…}}`.
- Its answer: `{"id", "ok": true, "value"}`, or `{"id", "ok": false, "error": {"class",
  "message", "hint"}}`. The client raises the error as the `flnd` class it names
  (`UsageError`, `LoginError`, `NotFound`, `Hidden`, `Changed`, `NetworkError`, `Blocked`),
  else as `flnd.Error`.
- Chrome takes at most 1 MiB in one message from a host. A message over 768 KiB is sent in
  parts, `{"id", "part": i, "of": n, "chunk": "<base64>"}`, each 576 KiB of the JSON, and put
  back together on the other side. The extension and the host both split and join; the host
  passes each answer to its run whole.

The host gives each run's op its own id, so two runs that both send id 1 get their own answers.
A capture a run leaves open when it disconnects is stopped by the host. A message from the
extension the host can't read (not a JSON object, an id that isn't a whole number, a part out
of range or with a bad chunk) is dropped, with a debug line in the journal, and the host keeps
serving.

## The ops

| op | args | value |
| --- | --- | --- |
| `hello` | `{"token"}` | `{"version"}`, the extension's |
| `cookies` | `{"domains": [str]}` | `[{name, value, domain, path, expires, secure, httpOnly, sameSite}]`, the domains' and their subdomains' |
| `tabs.open` | `{"url", "active": false}` | `{"tab", "url", "status"}`, once the tab has loaded; `status` is the page's HTTP status, or null |
| `tabs.list` | `{}` | `[{"tab", "url", "title"}]` |
| `tabs.close` | `{"tab"}` | `null` |
| `capture.start` | `{"tab", "match": regex}` | `{"capture"}`; from now on, every answer whose URL matches is kept |
| `capture.read` | `{"capture"}` | `[{"url", "status", "body": base64}]`, the answers since the last read |
| `capture.stop` | `{"capture"}` | `null` |
| `document` | `{"tab"}` | `{"html"}` |
| `click` | `{"tab", "css"}` | `null`; waits up to 10 s for the element, else `Changed` |
| `scroll` | `{"tab", "by": px}` | `{"height"}`, the page's height after it |
| `evaluate` | `{"tab", "script"}` | `{"value"}`, through the debugger's `Runtime.evaluate`; a script that throws is `Changed` |
| `storage` | `{"origin"}` | `{key: value}`, the origin's local storage, from an open tab or one opened for it |
| `screenshot` | `{"tab"}` | `{"png": base64}` |
| `media` | `{"tab"}` | `[{"url", "kind": "video" \| "hls" \| "dash" \| "image", "size"}]`; `size` is the bytes fetched, or null |

`document`, `click`, `scroll`, `storage` and `media` run `page.js` in the tab through
`chrome.scripting`, which needs the site's permission. `capture` and `evaluate` attach the
debugger for as long as they need it. `screenshot` attaches it too, for the one call, since
Chrome's other way (`captureVisibleTab`) needs permission for every site. Ops that start at
once on one tab share one attach, and the debugger is detached when the last one ends.

## The popup

The popup asks flnd, through the host, about the tab it is opened on, for the home recorded at
install:

- `resolve {url}` gives the tab's target and whether the home follows it. **Get** runs
  `flnd get` on it and **Follow** runs `flnd get`, each in a process of its own.
- The tab's `media` are listed, each with **Get**, which sends its link to flnd. A link no
  plugin reads is refused in the popup.
- **Allow** appears on a site a plugin reads but Chrome hasn't allowed yet.

## Testing

`tests/test_extension.py` drives the host, the relay and the client against a fake extension
that answers as `background.js` does: every op, parts, two runs at once, the refusals, the
modes, install and uninstall in a temporary home. It runs in `make check`. With
`FLND_TEST_NO_PROFILES=1` (set by `make test`), this module refuses the real
`~/.config/flnd/`, the browsers' native-messaging folders, the Windows registry and the real
relay.

`make ext` loads the real extension, unpacked, in Playwright's Chromium, in a temporary home,
and runs every op against pages served by `tests/bench/server.py`, four `evaluate`s and a
`screenshot` at once on one tab, and a check that the extension's origin has no `token.json`. It needs Chromium
(`playwright install chromium`) and is never part of `make check`.
