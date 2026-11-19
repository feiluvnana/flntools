# The Chrome extension

flnd's Chrome extension lets flnd use the browser you are logged in with. It reads a site's
cookies, opens tabs, reads their pages and the answers they load, and clicks and scrolls in
them. It works in Chrome, Chromium, Edge and Brave. This page says what the extension may read,
how flnd talks to it, and how to install it.

A plugin's browser tabs run in your Chrome when the plugin, or its `browser` setting, says
`chrome`. `flnd auth login <code> chrome` reads the login cookies the same way.

## The parts

```
  a run (flnd get …)            flnd host                    Chrome
  ┌──────────────────────┐  relay   ┌─────────────────┐  stdio   ┌──────────────────────┐
  │ extension.connect()  │ ───────▶ │ checks hello,   │ ───────▶ │ background.js runs   │
  │ client.call(op, …)   │ ◀─────── │ routes answers  │ ◀─────── │ the op; page.js runs │
  └──────────────────────┘  socket  │ by id           │  native  │ in the tab; popup.js │
                            or pipe └─────────────────┘ messages └──────────────────────┘
```

- **The extension**, `src/extension/`: `manifest.json`, `background.js` (the service worker),
  `page.js` (functions that run inside a page), `popup.html` and `popup.js`. They ship in the
  package as `flnd/extension/`.
- **The host**, `flnd host`: a hidden command that Chrome starts when the extension loads, and
  that stops when the extension goes. It talks to the extension on its standard input and output.
- **The relay**: a socket (a named pipe on Windows) on which the host waits for flnd's runs.
  Several runs can share one host.
- **The client**, `src/engine/extension.py`: what a run uses on its asyncio loop. `connect()`
  reaches the relay and says `hello`. Then `await client.call(op, **args)` runs one op.
- **The browser verbs**, `src/engine/chrome.py`: the tabs, feeds, pages and clicks a plugin uses,
  built on the ops below.

## What the extension may read

The manifest asks for these permissions:

- `cookies`: to give flnd a site's cookies, for its requests.
- `tabs`: to open, list and close tabs, and to see their addresses.
- `scripting`: to read a page's HTML, click and scroll in it, read its local storage, and list
  its media.
- `debugger`: to read the answers a page loads (`capture`), run a script (`evaluate`) and take a
  screenshot. It is attached only for those, and detached right after. Chrome shows a bar on a
  tab while the debugger is attached to it.
- `nativeMessaging`: to talk to `flnd host`.

The sites it may read are the built-in plugins' domains. They are the manifest's
`host_permissions`, which `make manifest` writes from the plugins. The manifest also lists
`optional_host_permissions: ["*://*/*"]`, which only lets the extension ask Chrome later: it
never asks for all sites at once.

The domains of an installed plugin (`flnd plugin install`) are asked for one plugin at a time:

- The extension asks the host (the popup's `needs` question) which installed plugins have
  domains Chrome hasn't granted yet. It asks when the popup opens and when it connects.
- The popup lists them, grouped by plugin: "Allow flnd on example.com? (plugin xyz)". The
  **Allow** click calls `chrome.permissions.request` for exactly that plugin's domains, because
  Chrome wants the user's click.
- An op on a site Chrome hasn't granted fails with a `UsageError` whose hint is `open the flnd
  popup to allow <domain>`. It is never silent.
- `flnd plugin uninstall <code>` makes the extension give back, with `chrome.permissions.remove`,
  the origins no installed plugin needs, the next time it connects. A domain two plugins share
  stays while one of them is installed.

The manifest has a fixed `key`, so the extension's ID never changes. The one origin the host
serves is `chrome-extension://<id>/`. The host refuses any other origin that starts it.

## Install

```sh
flnd extension install
```

It does five things:

1. It copies the extension to `~/.config/flnd/extension/unpacked/`. It makes a 32-byte secret in
   `~/.config/flnd/extension/token.json`, mode 0600. A valid secret already there is kept, so a
   host Chrome runs keeps accepting runs. It writes `host.json` beside it, mode 0600, with the
   home the popup acts on (`--home`, else `$FLND_HOME`, else `.flnd` in the current folder) and
   the extension's ID. The secret and `host.json` stay outside `unpacked/`, so they are not part
   of the extension, and its own origin can't fetch `token.json`.
2. It writes the host's manifest, `com.flnd.host.json`, for each Chromium browser whose profile
   folder it finds:
   - on macOS, in `~/Library/Application Support/<Google/Chrome, Chromium, Microsoft Edge or
     BraveSoftware/Brave-Browser>/NativeMessagingHosts/`;
   - on Linux, in `~/.config/<google-chrome, chromium, microsoft-edge or
     BraveSoftware/Brave-Browser>/NativeMessagingHosts/`;
   - on Windows, as the key `HKCU\Software\<vendor>\NativeMessagingHosts\com.flnd.host`, whose
     value is the manifest's path, in `~/.config/flnd/extension/`.

   Each manifest's `allowed_origins` is the extension's one origin, so Chrome lets no other
   extension start the host. Its `path` is `flnd-host` (`flnd-host.bat` on Windows) in
   `~/.config/flnd/extension/`. That script runs `flnd host` with the Python that installed it.
3. It prints three steps: open `chrome://extensions`, turn on **Developer mode**, and click
   **Load unpacked**, then choose `~/.config/flnd/extension/unpacked/`.
4. It waits up to 10 minutes, with a spinner, for the extension's first `hello`. It tries every
   quarter second. When the extension says hello, it prints `connected: flnd's extension <version>`.
   If the wait runs out, it prints `the extension hasn't said hello yet: it connects once Chrome
   loads it`. Either way the command exits 0, and you can load the extension later.

Install never follows a symlink. A `token.json` that is one is refused with an error that names
it. A folder of the relay's or the extension's that is a symlink, or isn't yours, is refused
before its mode is set.

## Uninstall

```sh
flnd extension uninstall
```

It removes `~/.config/flnd/extension/` (the unpacked extension, the secret and `host.json`) and
the host's manifests, and prints each path it removed. On Windows it also removes the registry
keys. Remove the extension from `chrome://extensions` yourself.

## The relay and the secret

On macOS and Linux the host listens on `$XDG_RUNTIME_DIR/flnd/host.sock`, or else on
`~/.config/flnd/run/host.sock`. The folder is mode 0700 and the socket 0600. A run checks that
the folder is yours and has no access for others before it connects.

On Windows it is the named pipe `\\.\pipe\flnd-host-<user>`. The pipe is owned by your user, its
access list lets in only your user, and it refuses remote clients. A run checks the pipe's owner
before it sends the secret, so another user's pipe of that name never gets it.

A run's first message must be `hello`, with the secret from `token.json`. The file must be
yours and mode 0600. The host refuses a run that doesn't send `hello` first, or sends the wrong
secret. It answers with `the host refused this run: hello must come first` or `the host refused
this run: its token is wrong`, then disconnects. Before `hello`, the host reads at most 64 KiB,
and a run that says nothing for 5 s is disconnected. The host strips the secret before it
forwards `hello` to the extension, so the extension never sees it.

## Messages

Each message is a 4-byte length in the machine's byte order, then that many bytes of compact
JSON. The same framing is used on the relay and on Chrome's pipe.

- An op: `{"id": int, "op": str, "args": {…}}`.
- Its answer: `{"id", "ok": true, "value"}`, or `{"id", "ok": false, "error": {"class",
  "message", "hint"}}`. The client raises the error as the flnd class it names (`UsageError`,
  `LoginError`, `NotFound`, `Hidden`, `Changed`, `NetworkError` or `Blocked`). Any other name is
  raised as `flnd.Error`.
- A run waits up to 120 s for an answer. Without one, the op fails with a `NetworkError`.
- Chrome takes at most 1 MiB in one message from a host. A message over 768 KiB is sent in
  parts, each `{"id", "part": i, "of": n, "chunk": "<base64>"}`, carrying 576 KiB of the JSON.
  The extension and the host both split and join parts. The host passes each answer to its run
  whole. The host refuses an op too large to send, with `… is too large for Chrome`.

The host gives each run's op its own id, so two runs that both send id 1 get their own answers.
A capture a run leaves open when it disconnects is stopped by the host. The host drops a message
from the extension it can't read (not a JSON object, an id that isn't a whole number, a part out
of range, or a bad chunk), notes it in the journal, and keeps serving. It refuses an op the host
doesn't know, or a `capture` op without its number.

## The ops

The host accepts these ops. Each is answered by `background.js`.

- `hello`: args `{"token"}`, which the host checks and strips. Value `{"version"}`, the
  extension's version.
- `cookies`: args `{"domains": [str]}`. Value: a list of cookies, `{name, value, domain, path,
  expires, secure, httpOnly, sameSite}`, for the domains and their subdomains. Without permission
  for those sites, it is a `UsageError` with the hint to allow them in the popup.
- `tabs.open`: args `{"url", "active": false}`. It waits until the tab has loaded, up to 60 s,
  then gives `{"tab", "url", "status"}`. `status` is the page's HTTP status, or null when the
  browser doesn't say. A tab that doesn't load in time is a `NetworkError`.
- `tabs.list`: no args. Value: a list of `{"tab", "url", "title"}` for every open tab.
- `tabs.close`: args `{"tab"}`. Value `null`.
- `capture.start`: args `{"tab", "match": regex}`. Attaches the debugger. Value
  `{"capture"}`. From then on, every answer whose URL matches is kept.
- `capture.read`: args `{"capture"}`. Value: a list of `{"url", "status", "body"}`, the answers
  since the last read. `body` is base64.
- `capture.stop`: args `{"capture"}`. Stops the capture and detaches the debugger when no other
  op needs it. Value `null`.
- `document`: args `{"tab"}`. Value `{"html"}`, the page's HTML as it is now.
- `click`: args `{"tab", "css"}`. It waits up to 10 s for the element, then clicks it. Value
  `null`. Nothing on the page matching the selector is a `Changed`.
- `scroll`: args `{"tab", "by"}`, in pixels. Value `{"height"}`, the page's height after it.
- `evaluate`: args `{"tab", "script"}`. It runs through the debugger's `Runtime.evaluate`, and
  waits for a returned promise. Value `{"value"}`. A script that throws is a `Changed`.
- `storage`: args `{"origin"}`. Value: `{key: value}`, the origin's local storage, read from an
  open tab on that origin. With none open, a background tab is opened for it, then closed.
- `screenshot`: args `{"tab"}`. Value `{"png": base64}`. It attaches the debugger for this one
  call. Chrome's other way, `captureVisibleTab`, needs permission for every site.
- `media`: args `{"tab"}`. Value: a list of `{"url", "kind", "size"}`. `kind` is `video`, `hls`,
  `dash` or `image`. `size` is the bytes the browser fetched, or null.

`document`, `click`, `scroll`, `storage` and `media` run `page.js` in the tab through
`chrome.scripting`. `capture` and `evaluate` attach the debugger for as long as they need it.
Ops that start at once on one tab share one attach, and the debugger is detached when the last
one ends.

The browser verbs in `src/engine/chrome.py` are built on these ops. A tab is `tabs.open` and
`tabs.close`. A feed is `capture.start`, `capture.read`, `capture.stop` and `scroll`. A page's
HTML is `document`, and its clicks are `click`. A page's `fetch` runs inside the tab through
`evaluate`, so it has the page's cookies. The run connects on first use. If that fails, the
run's tabs say `flnd's Chrome extension isn't connected`, with the hint `flnd extension install`.

## The popup

The popup opens on the tab you are looking at. It asks flnd through the host about it, for the
home recorded at install. The questions are:

- `resolve {url}`: the tab's target, and whether the home already follows it. The popup shows the
  target with **Get**, and **Follow** when it isn't followed yet. Both start `flnd get` on the
  target in a process of its own. `flnd get` follows the target, so the two are the same command.
- `media`: the tab's media, answered by the extension itself with the `media` op. Each item has
  **Get**, which sends its link to flnd as a target. A link no plugin reads is refused in the
  popup, with the reason.
- `needs {granted}`: the optional site permissions Chrome has granted, as match patterns. The
  host answers `{ask, drop}`: per installed plugin, the domains still to ask for, and the
  granted patterns no installed plugin needs, which the extension removes. The popup shows each
  plugin in `ask` with **Allow**.

The popup gives up after 30 s with `flnd didn't answer`.

## Library and CLI

- `flnd extension install` and `flnd extension uninstall` are the CLI commands. Hidden from the
  help, `flnd host` is what Chrome starts. It takes the extension's origin as its argument.
- `Flnd.extension` has `install()`, which does the install above and waits for `hello`;
  `uninstall()`, which returns the paths it removed; and `host(origin)`, which serves the
  extension on standard input and output.
- `Flnd.extension.version` asks the host once, with a 10 s limit. It is the connected
  extension's version, or None when it isn't connected. `connected` is True when `version` is
  not None.

## Testing

`tests/test_extension.py` runs in `make check`. It drives the host, the relay and the client
against a fake extension that answers as `background.js` does. It covers every op, parts, two
runs at once, the refusals, install and uninstall in a temporary home. With
`FLND_TEST_NO_PROFILES=1` (set by `make test`), it refuses the real `~/.config/flnd/`, the
browsers' native-messaging folders, the Windows registry and the real relay.

`make ext` loads the real extension, unpacked, in Playwright's Chromium, in a temporary home. It
runs every op through the host against pages from `tests/bench/server.py`. It also runs four
`evaluate` calls and a `screenshot` at once on one tab, and checks that the extension's origin
can't fetch `token.json`. It needs Chromium (`playwright install chromium`). It is never part of
`make check`.
