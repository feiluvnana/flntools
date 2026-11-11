# cookies

Phase 4, step 2. `shared/_cookies.py` is everything cookie-related, in one module of its own. Cookies aren't only a browser thing: an API-only source such as loi loads a cookie file and sends a `Cookie:` header without ever opening a browser. `load_from_file`, `to_file` and `to_header` use only the stdlib (`http.cookiejar`). `load_from_browser` needs `rookiepy` and `cryptography`, from the `browser` extra, and loads them lazily.

```python
from shared import cookies

jar = cookies.load_from_browser("loix.cc")                  # your installed Chrome
jar = cookies.load_from_browser("loix.cc", browser="firefox", profile="default-release")
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
| `load_from_browser(domain, *, browser="chrome", profile="Default") -> list[dict]` | reads an **installed** browser's cookie database on disk and decrypts the values, through `rookiepy`. It keeps only `domain` and its subdomains (see [Domains](#domains)). `browser` is `"chrome"`, `"edge"`, `"brave"`, `"firefox"` or `"safari"` |
| `load_from_file(path, *, domain=None) -> list[dict]` | reads a Netscape `cookies.txt`, or a JSON export (a list, as browser extensions write it). The format is detected from the content, not the extension. With `domain`, it keeps only that domain and its subdomains; without it, everything in the file. A broken file raises `ValueError` naming it |
| `to_file(jar, path)` | writes Netscape `cookies.txt` with mode 0600. It writes a temp file and renames it into place, so a crash never leaves half a file. `load_from_file` reads it back unchanged |
| `to_header(jar) -> str` | the value of a `Cookie:` request header |

## Domains

A cookie belongs to `domain` when its own domain is `domain` or a subdomain of it, ignoring a leading dot and letter case. For `instagram.com`, cookies for `instagram.com`, `.instagram.com` and `www.instagram.com` match; `notinstagram.com` and `instagram.com.evil.net` don't.

**There is no `save_to_browser`.**
- **Into a connected browser:** that's `b.cookies.add(jar)`.
- **Into an installed browser's files:** that isn't supported. The browser has to be closed, each value has to be re-encrypted with the browser's own key (on Windows only Chrome itself can do that), and a mistake corrupts your real profile.

## Platform notes for `load_from_browser`

These are written in the docstring:

| Platform | Note |
| --- | --- |
| macOS | the first read shows a Keychain prompt ("Chrome Safe Storage"). Allow it once |
| Windows | Chrome 127 and later use app-bound encryption. Reading them may need an administrator shell, or fail with a clear message. Firefox and older Chrome work normally. The way around it is `browser.connect(cdp=…)`, then `b.cookies.all`: a running browser's cookies need no decryption |
| Linux | the browser's key comes from the desktop keyring |
| any | a running browser may lock its database; `rookiepy` copies it first |

`rookiepy`'s current browser support is checked again when this is built.

## Tests

- **`load_from_browser`:**
  - against a fixture cookie database with a known key, through `rookiepy`'s test hooks or a fake;
  - the domain filter;
  - the error message when decryption is refused;
  - without `rookiepy`, the missing-extra `ImportError`.
- **`load_from_file`:**
  - Netscape, including `#HttpOnly_` lines;
  - JSON as a list;
  - detection from the content, with a `.txt` file holding JSON;
  - a broken file names itself.
- **`to_file`:** mode 0600; a round trip through `load_from_file` keeps every key; no temp file is left after a failure.
- **`domain=`** on both loaders: exact, leading dot, subdomain, mixed case, `notinstagram.com` and `instagram.com.evil.net`.
- **`to_header`:** order kept, and an empty jar gives `""`.
- `from shared import cookies` imports neither `rookiepy` nor Playwright.
