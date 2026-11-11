# cookies

Phase 4, step 2. `shared/_cookies.py` is everything cookie-related, in one module of its own. Cookies aren't only a browser thing: an API-only source such as loi loads a cookie file and sends a `Cookie:` header without ever opening a browser. `load_from_file`, `to_file` and `to_header` use only the stdlib (`http.cookiejar`). `load_from_browser` reads the browser's own sqlite database and decrypts it with `cryptography`, from the `browser` extra, which it loads lazily. No cookie library is used: `rookiepy`, the first choice, stopped publishing wheels after Python 3.12 and can't be installed on 3.13 or 3.14.

```python
from shared import cookies

jar = cookies.load_from_browser("loix.cc")                  # your installed Chrome
jar = cookies.load_from_browser("loix.cc", browser="firefox")                 # the default Firefox profile
jar = cookies.load_from_file("all.txt")                     # everything in the file: Netscape or JSON
jar = cookies.load_from_file("all.txt", domain="instagram.com")   # only that domain and its subdomains
cookies.to_file(jar, ".flnd/cookies/ins.txt")               # Netscape, mode 0600, atomic
header = cookies.to_header(jar)                             # "sessionid=…; csrftoken=…"
```

A list of cookies is called a **jar** in code. Writing `cookies = cookies.load_from_file(...)` would hide the module behind the variable.

## The shape

Every function takes or returns `list[dict]`, Playwright's cookie shape, with the keys `name`, `value`, `domain`, `path`, `expires`, `secure`, `httpOnly` and `sameSite`. A connected browser takes and gives the same shape: `b.cookies.add(jar)` and `b.cookies.all` ([browser](browser.md#browser)).

## Functions

| Function | Does |
| --- | --- |
| `load_from_browser(domain, *, browser="chrome", profile=None) -> list[dict]` | reads an **installed** browser's cookie database on disk and decrypts the values. It keeps only `domain` and its subdomains (see [Domains](#domains)). `browser` is `"chrome"`, `"chromium"`, `"edge"`, `"brave"` or `"firefox"`. `profile` is the profile folder's name; `None` means `Default` for the Chromium family and the default profile in `profiles.ini` for Firefox |
| `load_from_file(path, *, domain=None) -> list[dict]` | reads a Netscape `cookies.txt`, or a JSON export (a list, as browser extensions write it). The format is detected from the content, not the extension. With `domain`, it keeps only that domain and its subdomains; without it, everything in the file. A broken file raises `ValueError` naming it |
| `to_file(jar, path)` | writes Netscape `cookies.txt` with mode 0600. It writes a temp file and renames it into place, so a crash never leaves half a file. `load_from_file` reads it back unchanged |
| `to_header(jar) -> str` | the value of a `Cookie:` request header |

## Domains

A cookie belongs to `domain` when its own domain is `domain` or a subdomain of it, ignoring a leading dot and letter case. For `instagram.com`, cookies for `instagram.com`, `.instagram.com` and `www.instagram.com` match; `notinstagram.com` and `instagram.com.evil.net` don't.

**There is no `save_to_browser`.**
- **Into a connected browser:** that's `b.cookies.add(jar)`.
- **Into an installed browser's files:** that isn't supported. The browser has to be closed, each value has to be re-encrypted with the browser's own key (on Windows only Chrome itself can do that), and a mistake corrupts your real profile.

## How `load_from_browser` reads

The database is copied to a temp file first, because a running browser locks it. Rows are selected with `host_key` (Chromium) or `host` (Firefox) matching the domain, so a big database isn't read whole.

- **Firefox** stores values in plain text in `cookies.sqlite`, on every platform.
- **The Chromium family** stores values in `Cookies` (or `Network/Cookies`), encrypted with AES-128-CBC. Values starting `v10` or `v11` are decrypted with a key derived by PBKDF2-SHA1 from the browser's password, with the salt `saltysalt` and 16 spaces as the IV. A database at version 24 or later puts the SHA-256 of the host before each value, and that prefix is removed.
  - **macOS:** the password is the Keychain item `<Browser> Safe Storage`, read with the `security` program; the key uses 1003 iterations.
  - **Linux:** the password comes from the desktop keyring through `secret-tool`, or is `peanuts` when there is none; the key uses 1 iteration.
  - **Windows:** not supported. Chrome 127 and later use app-bound encryption, which only Chrome itself can undo, so `load_from_browser` raises `RuntimeError` pointing to `browser.connect(cdp=…)` and `b.cookies.all`. Firefox works.
- **Safari** isn't supported: its binary cookie file sits behind macOS's full-disk-access permission.
- A value that can't be decrypted raises `CookieError(ValueError)` naming the browser, never a cookie with a garbled value.

## Platform notes for `load_from_browser`

These are written in the docstring:

| Platform | Note |
| --- | --- |
| macOS | the first read shows a Keychain prompt ("Chrome Safe Storage"). Allow it once |
| Windows | only Firefox. For the Chromium family, use `browser.connect(cdp=…)`, then `b.cookies.all`: a running browser's cookies need no decryption |
| Linux | the browser's key comes from the desktop keyring |
| any | a running browser may lock its database, so it is copied first |

## Tests

- **`load_from_browser`:**
  - a fixture Chromium database built in the test, encrypted with a known password, for the macOS and Linux key rules, with and without the host-hash prefix;
  - a fixture Firefox database and `profiles.ini`;
  - the domain filter in the SQL;
  - a wrong key raises `CookieError`; Windows Chromium raises the `cdp` message; an unknown browser raises `ValueError`;
  - the Keychain and keyring programs are faked, and never really run;
  - without `cryptography`, the missing-extra `ImportError`.
- **`load_from_file`:**
  - Netscape, including `#HttpOnly_` lines;
  - JSON as a list;
  - detection from the content, with a `.txt` file holding JSON;
  - a broken file names itself.
- **`to_file`:** mode 0600; a round trip through `load_from_file` keeps every key; no temp file is left after a failure.
- **`domain=`** on both loaders: exact, leading dot, subdomain, mixed case, `notinstagram.com` and `instagram.com.evil.net`.
- **`to_header`:** order kept, and an empty jar gives `""`.
- `from shared import cookies` imports neither `cryptography` nor Playwright.
