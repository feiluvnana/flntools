# engine

Phase 4, step 3. flnd splits into an **engine** and **sources**. The engine also absorbs what was `shared.http`: downloads, retries and request signing now live in flnd, and use httpx directly. Changes `flnd/` only, on top of the step 2 modules.

Today each source does everything itself:
- loads its cookies and asks for missing ones;
- builds its own HTTP client and browser;
- opens a download pool;
- decides folders and file names;
- counts the limit;
- logs progress.

Four sources means four copies of that plumbing, mixed into the scraping logic. After this:

1. **The engine** owns everything shared: resolving targets, HTTP, the browser, SDK clients, cookies, logins and secrets, downloads, the history, the limit, and the screen.
2. **A source** only says what it wants and reports what it finds. It never imports httpx, Playwright, `worker`, `kv`, `log` or `prompt`. A test enforces this.

```text
flnd download ins:a twt:b tlg:@c --from posts,photo
      │
      ▼
   engine ── resolve targets ── group by source ── one worker per group
      │
      │   for each target: Session(target, categories/since/until, http, page, client, downloads, history, screen)
      ▼
   source.run(session) ──► session.fetch_json(…) / session.open_page(…) / session.save(…)
                                          ──► engine: paths, skipping, limit, retries, bars, history
```

## Files

| File | Holds |
| --- | --- |
| `flnd/engine.py` | `Engine`: resolves and runs targets, owns resources, groups sources, counts results |
| `flnd/session.py` | `Session` (a `typing.Protocol`), the download `Options`, and the engine's implementation, `TargetSession` |
| `flnd/net.py` | what was `shared.http`, now private to flnd: the httpx client factory, the single retry layer, signing, backing off, mirrors, atomic downloads with progress |
| `flnd/vault.py` | encrypted login data (see [Secrets](#secrets)). It isn't named `secrets.py`, because a bundled `flnd/__main__.py` puts `flnd/` on the import path, where that name would hide the stdlib `secrets` that `crypto.random_key` uses |
| `flnd/login.py` | the `browser` and `file` cookie logins, where each source's cookie file lives, the legacy domain fix-up, and logout. Reading and writing files is `shared.cookies` |
| `flnd/target.py` | `Target`, and reading the text people paste (see [Targets](#targets)) |
| `flnd/sources/base.py` | `Source`: what a source declares, and its optional hooks; `FetchError` |
| `flnd/sources/*.py` | the sources: scraping logic only |
| `flnd/sources/relay.py` | the GraphQL results Instagram and Facebook embed in a page's HTML on its first load (`__bbox.result`), read like the responses that come later; the first page of a tab, a post, a story and a profile header arrive only this way |
| `flnd/cli.py` | the commands: `download`, `login` and `logout`; `install-browser` comes with [packaging](packaging.md). The `auth` group and `set-cookie` are gone; a cookie file is the `file` login method |
| `flnd/cookies.py`, `flnd/utils.py` | deleted |

## The source

```python
class Instagram(Source):
    code = "ins"
    name = "Instagram"
    domains = ("instagram.com",)
    needs = ("sessionid",)                               # any one of these cookies is enough

    def headers(self, cookies):
        return {"x-ig-app-id": "936619743392459"}

    def run(self, session):
        user = session.target.value
        session.on_response("graphql", self.on_feed)              # before open_page: the first load counts too
        session.open_page(f"https://www.instagram.com/{user}/")
        for tab, path in TABS:
            session.folder = tab
            session.goto(f"https://www.instagram.com/{user}/{path}")
            session.scroll(idle=4, delay=2.0)
            if session.full:
                break
        session.folder = None                            # late responses are ignored, not misfiled

    def on_feed(self, session, data, response):
        for node in session.select(data).jmespath("data.user.edge_owner_to_timeline_media.edges[].node").all:
            for url, name in self.media(node):
                session.save(url, name, category=session.folder, date=posted_at(node))
```

**Declared on the class**

| Attribute | Meaning | Default |
| --- | --- | --- |
| `code`, `name`, `domains` | as in the registry | required |
| `short_links` | short-link hosts, also in `domains` (`vm.tiktok.com`, `fb.watch`), followed with one `HEAD` before `resolve` | `()` |
| `kinds_of_target`, `examples` | see [Targets](#targets) | `("user",)`, `()` |
| `categories`, `default_categories` | what `--from` can take, and what it takes by default; see [Download options](#download-options) | `()`, `()` |
| `requires_login` | `True` when the source can't work at all without logging in (tlg) | `False` |
| `login_methods` | how `flnd login` can log in to this source | `("browser", "file")` |
| `user_agent` | the user agent for this source's HTTP requests only, for a site that refuses httpx's own. The browser always keeps its own | `None` |
| `viewport`, `browser_args`, `stealth` | for this source's browser | `(1280, 900)`, `()`, `True` |
| `concurrency` | how many files download from this source at once. A whole CDN hit eight at a time looks like a bot | `4` |
| `pause` | seconds between two targets of this source, with ±40% jitter, so a list of accounts isn't read back to back | `5.0` |
| `needs` | cookie names, **any one** of which is enough. If none is present, the engine asks for a cookie file, or warns | `()` |
| `wait` | the retry wait: seconds, doubled on each retry, or `(error, attempt) -> float \| None` (Telegram reads `FloodWaitError.seconds`). The engine reads it from the class, so a plain function works | `1.0` |

**Methods**

| Method | Required | Meaning |
| --- | --- | --- |
| `run(self, session)` | yes | the scraping. It takes no options of its own; the [download options](#download-options) reach it through the session. It may be `async def` when `connect` is async |
| `headers(self, cookies) -> Mapping[str, str]` | no | extra headers for every API request |
| `sign(self, request) -> request` | no | runs on **every attempt**, retries included, so timestamped signatures stay fresh (loi) |
| `resolve(self, text) -> Target` | yes | turns any accepted form (a name, an id, a URL) into a `Target`; see [Targets](#targets) |
| `login(self, session, method)`, `logged_in(self, session) -> bool` | no | the defaults read the browser's cookies; see [Login](#login) |
| `connect(self, session)` | no | see [SDK clients](#sdk-clients) |

`login`, `logged_in` and `connect` get a session for the account rather than a target: its `target` is `Target(code, "account", "")`.

## The session

The only way a source acts.

| Group | Members |
| --- | --- |
| target | `target`, `cookies` (read-only `{name: value}`), `categories`, `since`, `until`, and `wants(category=None, date=None)` |
| API | `fetch_json(url, *, method="GET", headers=None, soft=False, **kw)`, `fetch_html(url, **kw) -> Selection`, `select(value) -> Selection`. They are named `fetch_` because they make a request. Per-request `headers=` override the source's (Instagram's mobile user-agent for stories). With `soft=True`, 401, 403 and 404 return `None` instead of raising. Any other failure raises `FetchError` (from `flnd.sources.base`), whose `status` is the refusing HTTP status, or `None` when no response came or it wasn't JSON. `http` is the raw httpx client |
| browser | `open_page(url)` connects the browser on first use (`browser.connect` with the source's settings, then `b.cookies.add` with the source's jar) and opens the page. Later calls reuse the same browser. Also `goto` (which opens the page when none is open), `on_response`, `on_request`, `scroll`, `wait_for_*` and `handoff`, which are the [browser](browser.md) page methods with flnd's spinner and limit wired in. Handlers get the session first, `handler(session, data, response)`, and may be added before `open_page`, so nothing from the first load is missed. `page` is the current `browser.Page`, and `browser` the `browser.Browser` |
| SDK | `client`: what `connect` produced |
| results | `save(source, name, *, category, date=None, folder=None, key=None, password=None, size=None, headers=None) -> bool`, `write(name, text) -> bool`, `folder`, `full`. `headers=` are sent with that file only, such as a CDN's `Referer` |
| state | `state` (a `kv` table for this source, in `.flnd/sources.db`), `secrets` (encrypted; see [Secrets](#secrets)) |
| people | `ask(message, *, secret=False, description="")`, `choose(message, options)`, `info(…)`, `warn(…)`, `challenged()`. `info` and `warn` name the target. `ask` and `choose` work during `flnd login` only; during a download they raise `RuntimeError`. `interactive` says whether they can ask: during `flnd login`, in a terminal |

`save` reports a file and returns `False` when it is skipped (already there, already queued, or filtered out by `--from`/`--since`/`--until`), the limit is reached, or `folder` is `None`. `folder` starts as `""`, the target's own folder. Its `source` is:
- a URL;
- a list of mirror URLs, tried in order;
- or a `fetch(dest, progress)` function, sync or async, for media without a URL (Telegram).

The engine decides:
- **the path:** `<downloads>/<code>/<target>/<folder>/<fmt.filename(name)>`, with every part of `folder` through `fmt.filename` too, so nothing a site sends can leave the target's folder;
- **skipping:** files that already exist; files whose history row (by `key`) names a file that still exists; and files already queued in this run, by path or by `key`, so the same file found in two places (Twitter's two timelines) is saved once;
- the limit, retries and the progress bar;
- **the history row:** keyed by `key`, which defaults to `folder/name`, because CDN URLs are signed and change.

`password=` is kept for `--unpack`. `write(name, text)` saves a text file such as loi's `info.md`, atomically.

## Downloads

What was `shared.http`, kept private in `flnd/net.py`:

- **One retry layer.** API calls retry connection errors and 5xx through the source's client, twice; a 429 is [backed off](#downloads) instead. File downloads run in the download pool, and each pool task retries its file twice itself, including errors raised while the body streams and a body shorter than its `Content-Length`; the client doesn't retry them. A dead connection costs 3 attempts, not the 9 the review measured. The pool's own `retries` stays off, because its waits can't follow the source's `wait`.
- **Retry waits** follow the source's `wait`. A server's `Retry-After` on a 5xx is honored, up to 60 s.
- **Atomic downloads with progress.** Each file is written to `.name.<random>.part` with mode 0644, then renamed. One `log` bar per file, sized from `Content-Length`.
- **Mirrors.** Each failed mirror writes `log.warn("mirror N failed; trying next")`.
- **Signing.** `Source.sign` runs inside the retry loop, before every attempt.
- **Backing off.** A rate limit never stops a source. On a 429, or a challenge a source reports through `session.challenged()`, the engine pauses that source and then tries again, as often as it takes. The wait is the server's `Retry-After` when it sent one, or the time its `x-rate-limit-reset` names (Twitter's) plus 5 s, with no cap; otherwise 30 s, then 60 s, 120 s, 240 s, … up to 15 min, each with ±20% jitter. Each wait gets one `log.warn`, such as `! ins: rate limited; waiting 2m (attempt 3)`, with `fmt.duration`, and a progress row counts it down, so the screen never looks frozen. Every request and download of that source waits out the same pause, and downloads that hit the same 429 together count once. A request that goes through resets the back-off. A 429 is not one of the three attempts. Ctrl-C ends a wait at once, in every thread. `concurrency` and `pause` are unchanged by it.
- **Locale and timezone.** `flnd download --locale fr-FR --timezone Europe/Paris` sets the browser's language and timezone for every source, through `browser.connect(locale=, timezone=)`. Without them, the browser keeps the system's. They should match where the connection really comes from, because a mismatch with the IP's country is a common bot signal; the help text says so.
- **User agents.** The browser always reports its own. The HTTP client sends the source's `user_agent` when it sets one, written in that source, and httpx's own otherwise.

## Targets

A target names what to download. Each source resolves its own forms, and everything people paste works:

```sh
flnd download ins:example_user ins:@example_user          # a username, with or without @
flnd download https://www.instagram.com/example_user/      # a URL: the domain names the source, so no prefix is needed
flnd download fbk:100012345678901                           # a numeric id
flnd download https://www.facebook.com/profile.php?id=100012345678901
flnd download https://www.tiktok.com/@example_creator/video/7301234567890123456   # one video
flnd download https://www.instagram.com/p/C1abcDEFghi/ https://x.com/example_handle/status/1790000000000000000   # one post, one tweet
```

**Resolution:**
1. **`code:value`** goes to that source's resolver. `value` may itself be a URL (`ins:https://…`).
2. **A bare URL**, with or without `https://` (`instagram.com/example_user`), finds its source by domain, with the same rule as [cookies › Domains](cookies.md#domains), matched against every source's `domains`. `www.`, `m.` and `mobile.` hosts match, and a short-link host (`vm.tiktok.com`) is listed in that source's `domains`.
3. **A bare value that isn't a URL** is a usage error, because it could belong to any source: `✗ which source is "example_user"? Write it as ins:example_user, twt:example_user, …`.

`Source.resolve(self, text) -> Target` turns one form into a `Target(source, kind, value)`:
- `kind` is what the target points to: `user`, `post`, `video` or `chat`, from the source's `kinds_of_target`;
- `value` is the clean form used for folders and the history, such as `example_user`, never the URL.

A form the source can't download yet is refused with what to do instead (`✗ fbk: single posts and videos aren't supported yet; give the profile, for example fbk:example.page`). An unrecognized form lists the accepted ones, from the source's `examples`. Resolution is offline, except for short links (`vm.tiktok.com/…`, `fb.watch/…`), which are followed with one `HEAD` request.

| Source | Accepts | Resolves to |
| --- | --- | --- |
| ins | `example_user`, `@example_user`, `instagram.com/example_user`, with any query string (`?hl=en`) | `user` |
| ins | `instagram.com/p/<code>`, `/reel/<code>`, `/tv/<code>`, `instagram.com/example_user/p/<code>` | `post` (the code) |
| ins | `instagram.com/stories/…` | refused (the profile's stories come with the profile) |
| fbk | `example.page`, a numeric id, `facebook.com/example.page` (and its tabs, `/photos`), `facebook.com/profile.php?id=…`, `fb.com/…`, `m.facebook.com/…` | `user` (a name or an id) |
| fbk | `facebook.com/<user>/posts/…`, `/watch/…`, `fb.watch/…` | refused for now (single posts and videos) |
| twt | `example_handle`, `@example_handle`, a numeric user id, `x.com/example_handle`, `twitter.com/example_handle` | `user` |
| twt | `x.com/example_handle/status/<id>`, `twitter.com/…/status/<id>`, `x.com/i/web/status/<id>` | `post` (the tweet id) |
| tik | `example_creator`, `@example_creator`, `tiktok.com/@example_creator` | `user` |
| tik | `tiktok.com/@…/video/<id>`, a video id (a number of 16 or more digits), `vm.tiktok.com/…` | `video` |
| loi | `5930`, `loix.cc/lois/5930` | `post` |
| tlg | `@examplechannel`, `t.me/examplechannel`, a chat title, `-1001234567890`, `t.me/c/1234567890/…`, `t.me/+…` (invite) | `chat` |

What used to be implicit becomes explicit and tested: "a long number is a video id" for tik, and "a number is a user id" for twt. twt used to read a number of more than 10 digits as one tweet; one tweet is now always a `/status/` URL.

A single post or video lands in `<downloads>/<code>/<id>/`, and counts as the source's `posts` (tik: `videos`), so `--from` filters it like any other item.

**Declared on the source:**
- `kinds_of_target`, for example `("user", "video")` for tik;
- `examples`: one example per accepted form, used in `--help` and in error messages;
- `resolve`.

`flnd download --help` builds its "Target format" section from every source's `examples`, so it always matches what is accepted.

## Download options

`flnd download` has one set of options, and **every source supports all of them**. There are no per-source options.

```sh
flnd download ins:example_user --from reels,tagged --since 2026-01-01
flnd download tlg:@examplechannel loi:5930 --from photo,archive --until 2026-06-30 --unpack
flnd download ins:example_user tik:example_creator --from all
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--from` | each source's `default_categories` | which categories to take: a comma list or repeated, or `all` for every category of every source |
| `--since`, `--until` | none | only items dated in this range, inclusive (a `date` in ISO format) |
| `--unpack` | off | unpack each saved archive (by its name: `.zip`, `.7z`, `.rar`, `.tar…`, parts) into a folder named after it, with `archive.unzip` and the password the source gave. An archive an earlier run saved is unpacked too, after the target's downloads, when its folder doesn't exist yet |
| `--zip` | off | zip each target's folder with `archive.zip` when the target ends |

**Categories.** Each source declares its `categories` and, among them, its `default_categories`: the account's own content. Other people's content (tagged, reposts, liked, favorites, replies) is opt-in. `profile` is the account's pictures: Instagram's HD profile picture, TikTok's profile picture, X's avatar and banner, and Facebook's profile and cover photos. TikTok's liked and favorites exist only when the account made them public; otherwise one info line says so, and it isn't an error. A name that no source of the given targets has is a usage error listing the names per source. A name only some sources have applies to those and is ignored by the others; a source left with none of its own names takes its defaults, as without `--from`. `flnd --help` lists every source's categories, marking the defaults.

| Source | Categories (default ones in bold) |
| --- | --- |
| ins | **posts**, **reels**, tagged, reposts, **stories**, **highlights**, **profile** |
| fbk | **posts**, **photos**, tagged, **albums**, **reels**, **videos**, **stories**, **profile** |
| tik | **videos**, **stories**, **playlists**, reposts, liked, favorites, **profile** |
| twt | **posts**, **media**, **highlights**, reposts, replies, **profile** |
| loi | **photo**, archive ([loi](loi.md)) |
| tlg | **photo**, **video**, file ([tlg](tlg.md)) |

Sources whose content is naturally typed use those types as categories: loi's VIP archives are `--from archive`, and Telegram's are photo, video and file. A category is where an item came from, not where it is saved: a video on Facebook's tagged tab is `tagged`, though it lands in `videos/`.

**How sources support them.** A source reports each item's category and date, and the engine applies the filters:

```python
session.save(url, name, category="reels", date=posted)      # skipped unless reels are wanted and posted is in range
if not session.wants(category="tagged"):                     # not asked for: don't even open the tab
    continue
if not session.wants(date=posted):                           # older than --since: stop scrolling early
    break
```

- `session.save(…, category=…, date=…)`: `category` is required, and one the source doesn't declare raises `ValueError`. `date` may be `None` when the site gives none, and such items are kept whatever the range. Skipped items return `False`, like files that already exist.
- `session.wants(category=None, date=None) -> bool` lets a source skip work early: a tab that wasn't asked for isn't opened, and a feed scraper stops scrolling once posts are older than `--since`. Using it is optional, because `save` filters anyway, so a source that ignores it is still correct, only slower.
- `session.categories`, `session.since` and `session.until` are readable, for sources that pass them to an API (Telegram's server filters).

loi's old `--full` is `--from archive`. Archives are off by default, as before, because `archive` isn't one of loi's default categories.

## Questions

Only `flnd login` asks questions. `flnd download` never does: missing targets are a usage error, as in any command-line tool, options keep their defaults, and a source that isn't logged in gets a hint, not a question. Bare `flnd` prints help.

## Login

```sh
flnd login ins                                   # asks: from your browser, or from a cookie file?
flnd login ins --method browser --browser firefox
flnd login twt --method file --path ~/Downloads/cookies.txt
flnd login tlg                                   # asks: from Telegram Web, a QR code, or phone and code?
flnd logout ins
flnd logout --all
```

`flnd login <source> [--method …] [--path …] [--browser …] [--profile Default]`

- **Methods.** Each source declares its `login_methods`. When `--method` is missing and there is more than one, `login` asks which with `prompt.choice`. Without a terminal, it is a usage error that lists the methods. A method the source doesn't have is a usage error too.
- **Questions after the method.** Each method asks only for what it needs and wasn't given: `file` asks for the path, and `browser` asks which browser. Without a terminal, a missing one is a usage error naming the flag.

**Cookie sources** (ins, twt, fbk, tik, loi) have two methods, built into the engine, so they need no login code of their own:

| Method | What it does |
| --- | --- |
| `browser` | `cookies.load_from_browser(domain, browser=…, profile=…)` for each of the source's `domains` |
| `file` | `cookies.load_from_file(path, domain=…)` for each of the source's `domains`. A full browser export works, because only this source's cookies are kept |

Both methods then:
1. check that one of the source's `needs` cookies is there, which proves the login;
2. apply flnd's legacy domain fix-up: every domain gets a leading dot, so every subdomain sees the cookie, and every path defaults to `/`;
3. save the cookies with `cookies.to_file` to `.flnd/cookies/<code>.txt`, removing an old `<code>.json`;
4. report `✓ ins: logged in from Chrome (14 cookies)`. A missing `needs` cookie gets `✗ twt: no auth_token for x.com in Chrome; log in there first` instead.

If the browser can't be read at all (see [cookies](cookies.md#platform-notes-for-load_from_browser)), it says so and suggests `--method file`.

At download time the engine reads `.flnd/cookies/<code>.txt`, or an older `<code>.json` (and the `E:/` folders flnd used to read), with the same fix-up, because files written before `flnd login` lack it.

**A source with its own way of logging in** overrides `Source.login(self, session, method)` and lists its own `login_methods`; the engine then calls it for every method, even one named `browser`. Today that's Telegram, with `browser` (Telegram Web's saved login), `qr` and `phone` ([tlg](tlg.md#login)). It asks through `session.ask` and `session.choose` (checking `session.interactive` first when a method can't work without answers), stores the result in `session.secrets`, and raises `LoginError` (from `flnd.sources.base`; `flnd.login.LoginError` is the same class) when the login fails, which `flnd login` reports with exit code 1. Its session's target is the account, which prints as the source's code alone (`tlg`), so `session.info("logged in as …")` reads `• tlg: logged in as …`.

**Logged in or not at download time.** `Source.logged_in(self, session) -> bool` defaults to "one of `needs` is in the source's cookie file". When it is `False`, the engine prints one hint and never asks:
- **`requires_login = False`** (the default): `! ins: not logged in, using public access; run flnd login ins`, then the download continues, as flnd does today.
- **`requires_login = True`** (tlg): the target fails with `✗ tlg: not logged in; run flnd login tlg`.

`flnd logout <source>` removes that source's cookie file and secrets, and `flnd logout --all` removes them for every source.

## Secrets

Login data lives in `.flnd/`, so `rm -rf .flnd` removes every trace.
- **Storage:** `.flnd/secrets.db` is a `kv` table. Each value is sealed with `crypto.encrypt_aes_gcm`, with the context `<source>\0<key>`, so a value can't be moved to another slot.
- **The key:** `.flnd/secret.key`, mode 0600, created on the first read or write of a secret, with a temp file and an atomic link, so it is never left empty. Two runs racing both end up with the key that was linked first. A key file of the wrong size raises `ValueError` naming it. The review found the old code could leave it empty.
- **What it protects against:** someone who copies only `secrets.db`. It doesn't protect against someone who can read the whole folder, which is the same exposure as the cookie files next to it. The docstring says so.
- **Access:** `session.secrets` is a mapping scoped to the source.

## SDK clients

For services with an SDK instead of URLs. `Source.connect(session)` returns a context manager, or an async one. The engine enters it once per source group, before the first target, and exits it after the last. `session.client` is the result. Anything async in a group (an async `connect`, an async `run`, an async `fetch`) runs on one event loop of that group's own, in a thread started on first use, because an SDK client only works on the loop that made it. A `fetch` download still takes a download-pool slot, and waits there for its coroutine on that loop. A `save` made on that loop (from an async `run`) never waits for room in the pool, because the downloads it would wait for need the same loop: a thread of the target's hands those files to the pool instead, and `full` counts saves, not pool tasks. Other groups keep their threads.

## The engine

```python
engine = Engine(home=Path(".flnd"), limit=None, headless=False, locale=None, timezone=None)
total = engine.run(engine.resolve(texts), options)
```

`home` holds everything: `downloads/`, `cookies/`, `history.db`, `sources.db`, `secrets.db` and `secret.key`.

1. `resolve` reads every target ([Targets](#targets)), stopping on the first bad one with `TargetError`. `flnd download` calls it before anything starts, and reports a bad target as a usage error.
2. `run` reads the [download options](#download-options) once; every session sees the same values.
3. Group the targets by source, and run the groups with `worker.race`, one at a time per source (`concurrency` = the number of groups).
4. For each group:
   1. load the source's cookie file once, and build its one HTTP client from it. Nothing is asked;
   2. check `logged_in`, as in [Login](#login), then `connect` if the source has it;
   3. for each target, after a `pause` from the second one on: open a `Session` and call `run`. Cookies the site set in the browser are copied into the client before each API request and when the browser closes, so they reach later requests and targets;
   4. close the browser if one was opened, and wait for the downloads;
   5. end the target's row at `ok` or `error`.
5. Return the total; `flnd download` logs it.

## Testing

- **Sources are tested without network or browser.** `Session` is a `Protocol`, so pyright checks that `FakeSession` (in `tests/fakes.py`) matches it. `FakeSession` records `save` calls, serves `fetch_json` and `fetch_html` from fixtures, and feeds `on_response` handlers directly.
- **The engine:**
  - same-source targets never overlap;
  - the `needs` hint, and `requires_login` failing the targets;
  - one client per group, and cookies added later reach it;
  - the browser connects only on `open_page`;
  - the limit and `folder = None`;
  - `--from` (defaults, `all`, a name some sources lack, an unknown name), `--since` and `--until` filtering in `save`, items without a date, and `wants`;
  - target resolution: every row of the targets table, a bare URL per source, `www.`/`m.` hosts, query strings, a bare value refused with suggestions, refused forms with their hint, and the short-link `HEAD` (mocked);
  - `--unpack` and `--zip`, and `--unpack` on an archive saved by an earlier run;
  - `download` never asks: missing targets are a usage error, and a source that isn't logged in gets the hint;
  - `login`:
    - both cookie methods, with `domain` filtering on a full export;
    - a missing `needs` cookie, and an unreadable browser;
    - asking for the method only when there are several, and for `--path` / `--browser` only when missing;
    - usage errors without a terminal, and a method the source doesn't have;
    - Telegram's override;
  - `logout` for one source and with `--all`;
  - login, logout and secrets: a sealed round trip, a value moved to another slot fails, and the key-file race;
  - an async `connect` and `run`.
- **Downloads:**
  - mirrors;
  - signing on every attempt;
  - 3 attempts at most for a dead connection;
  - a streaming error retried by the pool;
  - no `.part` left behind;
  - dropped tasks logged.
- **The guard test:** no module under `flnd/sources/` imports `httpx`, `playwright`, `shared.worker`, `shared.kv`, `shared.log` or `shared.prompt` when it runs. Imports under `if TYPE_CHECKING:` are only read by checkers, so `base.py` may name `httpx.Request` in an annotation.

## Migration

One source at a time, starting with Facebook, the smallest. Each step keeps the suite green.
- **What doesn't change:** the scraping logic: URLs, JSON paths, tabs, scroll settings, names and folders.
- **What goes:** client and browser setup, cookie loading, `Downloads`, path building, limit checks, and the captcha spinner.
- **Size:** each source should end up around 150–300 lines. A larger one points to a missing session method.
