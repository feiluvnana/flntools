# PLAN: flnd as an engine and its plugins

**Status: complete, 2026-11-18.** Written from `main` at 92e694b in answer to `PROMPT.md`.
`PROMPT.md` wins where the two differ. The user's follow-ups of the same day are folded in,
and every decision is settled: section 2 lists them.

This page is the specification. A builder makes no design choice that isn't written here.

**When something is missing:**
- When this page and the code disagree about **what to build**, this page wins.
- When this page is silent on a point of **how**, the builder keeps today's behaviour and says
  so in the commit message.
- When neither answers, the builder stops and asks the orchestrator (section 15), who asks the
  user.

**Words:**
- "must" means required.
- "kept" means today's behaviour, unchanged.
- Numbers are exact unless they say "about".

The numbers today:
- **Code:** 35,709 lines of Python in `flnd/` and `flncore/`, and 1,263 tests.
- **Benchmarks:** recorded at 1e01feb on an Apple M5. Step 0 records them again at HEAD.

**The goals:**
- 24,000 lines of Python or fewer in `flnd/`, counted with `wc -l` over `flnd/**/*.py`;
- no feature lost by accident (section 9);
- every gated number equal or better;
- `make check` green at every commit.

## Contents

1. What is wrong today
2. Decisions
3. Words used here
4. Layout and module map
5. Contracts: errors, public names, the plugin API, the test kit
6. Contracts: the engine (run, scheduler, crawl, save, resolver, plugins, auth, extension)
7. Contracts: storage and config
8. Contracts: the command line, the screen, the library, the journal
9. Every feature, and where it goes
10. What other tools teach
11. Bugs
12. Speed-ups and benchmarks
13. Conventions (the new AGENTS.md)
14. The steps
15. How the plan is carried out

## 1. What is wrong today

- **Three threads share state through locks.** The listing thread, the loop and the archive
  lane all write to it, and most of the bugs in section 11 live there.
- **Scheduling is spread over a dozen places,** with retries nested three deep:
  - `net.Client.call` retries a request;
  - `_transfer._again` retries the file around it;
  - each source's `back_off` loop retries around both.

  The caps, the places per target, the tab budget, the archive lane and the order of targets
  are each set somewhere else. Targets run one after another.
- **One file is counted three times,** by `_track`, `Board` and `Run`. Each count builds a
  `Progress` event, and the renderer redraws 60 times a second, even when nothing changed.
- **Sources swallow errors and copy each other.**
  - A 404, or a page that fails to load, ends quietly, and the target is recorded complete.
  - `Blocked` is caught and the source keeps requesting.
  - About a dozen helpers are copied between sources.
- **The kit hides dead code.**
  - Nine shims re-export `flncore`, and two of them patch kit classes on import.
  - Four of its modules are dead (`media`, `run`, `config`, `cli`), and so are the segmented
    downloads and the `probes` table.
  - The checks for dead names and docstrings never read `flncore`.
- **The vault says it encrypts, and doesn't.** Its sealing code exists, but nothing calls it.
- **The CLI and prompts can't show an interactive list.** They are argparse and questionary,
  and cyclopts is a dependency only for a module nothing uses.

## 2. Decisions

All are settled. The user, on 2026-11-18: "just do as recommended". Each one-line reason is
the reason the decision was taken.

| # | decision | why |
| --- | --- | --- |
| D1 | `flncore/` folds into `flnd/kit/`; `test_layers` keeps `kit/` standalone | its only user is flnd; the shims double every name; dead code hid outside the checks |
| D2 | Plugin hooks are `async def` | no listing thread, no lock bugs, no 40 µs hop per verb; a plugin can run requests at once |
| D3 | One asyncio loop per run coordinates; pools use the other cores; transfer lanes only if measured | the GIL makes more threads useless for Python code, and the heavy work is already outside Python |
| D4 | `get` follows every target that isn't a single post; `get` with no target syncs every followed target | the brief: "a target flnd already has is synced" |
| D5 | No flags on `get`, and no `-v`; the only flag left is `target clean --media` | the user: "remove the get flag… the verbose flag also no need" |
| D6 | Categories go in the target (`/reels,posts`, `/all`); without them, the plugin's own (`own=True`) | replaces `--only` and `--all` |
| D7 | `when`, `exec`, `rate`, `slowest`, `height` and `switch` are config keys; `limit` and `dry` are library keywords | each is a standing choice or a script's, not a per-command flag |
| D8 | Rich for output; Textual in inline mode for lists and forms; questionary and cyclopts go | a Huh-like look, and interactive tables |
| D9 | The run's screen shows rows only for files being transferred (at most 6), never for queued ones | the user's request |
| D10 | Several accounts per plugin, one of them the default | the brief: "holds every plugin's accounts" |
| D11 | Switching accounts is opt-in per plugin (`switch = true`), off by default | results differ between accounts |
| D12 | The extension is built, and every plugin can use it through `Ctx` | the user's request; it also reads Chrome's cookies under app-bound encryption |
| D13 | Secrets are sealed with AES-256-GCM; the key lives in `~/.config/flnd/key` (mode 0600), never in the home | a copied or committed home leaks nothing, and cron and SSH always work |
| D14 | `flnd.sources` entry points are dropped; `plugin install` is the one way in | one way, and a manifest for every plugin |
| D15 | A built-in `web` plugin takes links no other plugin claims | "any kind of content from any site" |
| D16 | "source" becomes "plugin" everywhere: `flnd.plugin`, `Plugin`, `[plugins.<code>]`, `$FLND_PLUGINS`, `PluginError` | one word for one meaning, matching the brief |
| D17 | The context a plugin acts through is `Ctx`; the route decorator is `route` | `Site` and `@target` collide with `Target` and `ctx.target` |
| D18 | Progress is a snapshot (`run.state`); outcomes are events (`Saved`, `Skipped`, `Failed`, `Planned`); `Progress` goes | an event per count was the flood |
| D19 | `--json`, `--home`, `--again` and the `retry` and `check` commands go (section 9 says where each feature goes) | the journal, `$FLND_HOME`, `target clean`, automatic retries, `status` |
| D20 | `[defaults] exec` replaces `--exec`; it is read only from `~/.config/flnd/config.toml` and runs as a job, never through a shell | gallery-dl's exec, scheduled as a job without a shell or home-folder code |
| D21 | Segments are taken dynamically, only when the host has a free place and no other file waits for it | IDM and aria2; uses capacity that would sit idle |
| D22 | Dedupe hashes only files whose size matches a saved file's | most files are never hashed (P2) |
| D23 | A commit runs on the loop with `busy_timeout = 0`, and `SQLITE_BUSY` re-queues it after 20 ms | the loop never blocks (E18) |
| D24 | Chromium cookie stores read through the keychain go once the extension lands; Firefox, Safari and cookie files stay | the extension does Chrome's job better; the others have no extension |
| D25 | Plugin installs copy a `.py` file or a package folder into the plugin folder and record it in `plugins.json` | help and resolution never import a plugin |
| D26 | Pushing: the orchestrator asks the user once, before the first push, whether to push after each step | outward-facing; AGENTS.md says push only when asked |
| D27 | One checker: ruff, with the rules `F E9 B UP I ASYNC RUF006`. pyright, `make type` and `[tool.pyright]` go. The tests turn a never-awaited coroutine into an error | the user: one tool, not a mix; ruff runs in milliseconds and today's code passes it; `ASYNC` catches blocking calls inside coroutines, the one-loop rule's main risk; a missed `await` fails a test instead of being guessed at by types |

## 3. Words used here

| word | meaning |
| --- | --- |
| plugin | one class, a subclass of `flnd.plugin.Plugin`, handling one site or kind of content; `code` names it (`ins`) |
| target | what a user asks for: `ins:example_user`, `mht?words`, or a link |
| route | a kind of target in a plugin (`user`, `post`), an `async def` decorated with `@route` |
| category | a part of a route's target (`posts`, `reels`), an async generator decorated with `@<route>.category` |
| post | one item a category yields (`Post`), holding files |
| file | one thing to download (`File`) |
| part | `<name>.part`, the file while it downloads; it locks itself |
| job | one unit of work queued to the scheduler |
| pool | what a job waits for: a place under a cap, a token from a pace, a thread |
| cap | the most transfers at once to one host, adapted by AIMD |
| place | one slot under a cap, or one of a target's slots |
| pace | the least time between two page requests for one bucket |
| bucket | a name a plugin gives to an endpoint (`"graphql"`), paced and held on its own; the default bucket is the host |
| hold | a time until which a bucket's or an account's page jobs don't start |
| lane | a thread or process the scheduler runs CPU work on |
| account | one login of one plugin |
| home | the folder flnd keeps state in: `$FLND_HOME`, else `./.flnd` |
| journal | the run's JSON-lines log, `<home>/log/<stamp>_<command>.jsonl` |
| snapshot | `run.state`: the run's counts, files in flight and holds, published every 100 ms |

## 4. Layout and module map

### 4.1 The final tree

```text
flnd/
  __init__.py      public names, each loaded on first use
  __main__.py      python -m flnd
  _library.py      open, Flnd, and the groups Targets, Auth, Plugins, Extension, Config
  cli.py           the commands
  errors.py        the one error hierarchy
  plugin.py        plugin API 2 (public)
  testing.py       the plugin test kit (public)
  engine/
    run.py         Run, the loop's thread, the event channel, Ctrl-C, the journal
    events.py      the events, Report, State, and the rows the reads return
    jobs.py        the scheduler: Job, pools, priorities, retry policy, holds, segments, lanes
    crawl.py       one target: listing, smart sync, early stops, the Ledger
    save.py        a file job: transfer, dedupe, unpack, sidecar, exec, the commit
    resolve.py     target text -> (plugin, Target, categories)
    plugins.py     the plugin manager
    auth.py        accounts, switching, the sealed vault, login methods
    extension.py   the native-messaging host, the protocol, the relay
    store.py       flnd.db: schema, migrations, rows, check and repair
    config.py      config.toml: structs, reading, editing
  kit/             imports only kit/ and flnd/errors.py
    net.py browser.py files.py html.py dig.py text.py clock.py db.py cookies.py vault.py media.py
  ui/              imports only kit/, ui/, engine/events.py and flnd/errors.py
    theme.py out.py screen.py lists.py forms.py
  plugins/         built-ins, importing only flnd.plugin and the standard library
    __init__.py _manifest.py fbk.py ins.py loi.py mht.py tik.py tlg.py twt.py web.py
  extension/       manifest.json background.js page.js popup.html popup.js
  docs/
```

**The import rules,** checked by `tests/test_layers.py` reading every file's imports:
1. `kit/*` imports only `kit/*`, `flnd/errors.py`, the standard library and third-party
   packages.
2. `ui/*` imports only `ui/*`, `kit/*`, `engine/events.py` and `flnd/errors.py`.
3. `engine/*` imports `engine/*`, `kit/*`, `ui/*`, `flnd/errors.py` and `flnd/plugin.py`
   (`engine/events.py` imports only `flnd/errors.py`, `flnd/plugin.py`, `kit/*`, `msgspec` and the
   standard library).
4. `flnd/plugin.py` imports only `kit/*`, `flnd/errors.py` and the standard library.
   `flnd/testing.py` imports `flnd`, `flnd/plugin.py`, `engine/*` and `kit/*`. `flnd/_library.py`
   imports `engine/*`, `kit/*`, `flnd/errors.py` and `flnd/plugin.py`.
5. `flnd/plugins/*` imports only `flnd.plugin` and the standard library, plus its own SDK
   (`telethon`, `qrcode`), imported inside a function.
6. `flnd/cli.py` imports only `flnd`, `flnd/ui/*` and `flnd/plugins/_manifest.py`.
7. `flnd/__init__.py` imports nothing at import time beyond the standard library.

### 4.2 Where every module of today goes

| today | goes to | in step |
| --- | --- | --- |
| `flncore/__init__.py`, `flncore/testing.py` | deleted (the fakes live in their kit modules) | 2 |
| `flncore/net.py` | `kit/net.py`; `Limits`, `Cap` and `Bucket` move to `engine/jobs.py` | 2, 5 |
| `flncore/browser.py` | `kit/browser.py`; the tab budget and scroll gate move to `engine/jobs.py` | 2, 5 |
| `flncore/files.py` | `kit/files.py` (parts, atomic writes, tools, archives) | 2 |
| `flncore/loop.py` | `kit/loop.py`, then deleted (`engine/run.py` owns the loop) | 2, 6 |
| `flncore/log.py` | `ui/out.py` (lines, styles), `ui/screen.py` (drawing), `engine/run.py` (the journal) | 2, 3, 4 |
| `flncore/prompt.py` | `ui/forms.py` | 2, 13 |
| `flncore/clock.py`, `flncore/dig.py` | `kit/clock.py`, `kit/dig.py` | 2 |
| `flncore/errors.py` | merged into `flnd/errors.py` | 2 |
| `flncore/media.py` | rewritten as `kit/media.py` from `_transfer._dash_merge` (one ffmpeg join) | 2 |
| `flncore/config.py`, `run.py`, `cli.py` | deleted (dead) | 2 |
| `flnd/_net.py`, `_browser.py`, `_files.py`, `_loop.py`, `_tools.py`, `_unpack.py`, `_ui.py`, `_prompt.py`, `_time.py` | deleted (shims) | 2 |
| `flnd/_html.py`, `_text.py`, `_db.py`, `_cookies.py`, `_vault.py` | `kit/html.py`, `kit/text.py`, `kit/db.py`, `kit/cookies.py`, `kit/vault.py` | 2 |
| `flnd/engine.py` | `engine/core.py` (temporary), then `resolve.py`, `auth.py`, `crawl.py`, `run.py` | 2, 6, 10, 11 |
| `flnd/job.py` | `engine/job.py` (temporary), then `crawl.py` and `save.py` | 2, 5, 6 |
| `flnd/site.py` | `engine/site.py` (temporary), deleted when API 1 goes | 2, 8 |
| `flnd/options.py` | `engine/options.py` (temporary), then `config.py` and `resolve.py` | 2, 10 |
| `flnd/_home.py` | `engine/home.py` (temporary), then `store.py` and `auth.py` | 2, 10, 11 |
| `flnd/_run.py`, `_events.py` | `engine/run.py`, `engine/events.py` | 2 |
| `flnd/_track.py`, `_transfer.py`, `_steps.py` | `engine/track.py`, `transfer.py`, `steps.py` (temporary), then `save.py` | 2, 5 |
| `flnd/_check.py` | `engine/check.py` (temporary), then `store.py` | 2, 10 |
| `flnd/_plugins.py` | `engine/plugins.py` | 2, 9 |
| `flnd/sources/__init__.py` | `flnd/plugins/__init__.py` (registry stays lazy over manifest) | 8 |
| `flnd/__init__.py`, `flnd/__main__.py` | kept in place, updated for public names | 4, 7, 13 |
| `flnd/_library.py` | kept at `flnd/_library.py`, rewritten | 13 |
| `flnd/source.py` | kept for API 1 until step 8; `flnd/plugin.py` is new in step 7 | 7, 8 |
| `flnd/sources/<code>.py` | `flnd/plugins/<code>.py`, one per commit as it is ported | 8 |
| `flnd/sources/_manifest.py` | `flnd/plugins/_manifest.py` | 8, 9 |
| `flnd/testing.py`, `flnd/cli.py`, `flnd/errors.py` | kept in place, rewritten | 7, 13, 2 |

A temporary module named above must be gone by the end of the step that replaces it. Step 16
checks that none is left.

## 5. Contracts: errors, public names, the plugin API, the test kit

### 5.1 Errors (`flnd/errors.py`)

```text
Error(Exception)            .hint: str (what to do; "" when nothing)
├── UsageError              exit 2; raised before any request
├── LoginError              exit 3; a 401 or 403 from the plugin's own domains, a declared wall
├── SiteError
│   ├── Blocked             .wait: float | None (seconds the site asked for)
│   ├── NotFound            404 or 410: recorded gone, never asked again
│   ├── Hidden              new: it exists, but this account can't see it
│   └── Changed             the answer can't be read; a scrubbed sample is kept
├── NetworkError            timeout, reset, DNS, TLS; retried
├── DiskError               exit 4; stops the run
├── ToolError               a missing tool: exit 4, the run stops; a failing tool fails its file
└── PluginError             was SourceError; a bug in a plugin; exit 4 for a broken plugin
Stopped(KeyboardInterrupt)  Ctrl-C; exit 130
```

- `str(e)` is one line, and `e.hint` is one line.
- `errors.classify(exception) -> Error` turns any exception into one of these: anything a
  plugin lets escape becomes a `PluginError`.
- `Error.of` is deleted; `classify` is the one way.
- `errors.disk(OSError) -> DiskError` is the one disk mapper (today there are three).

### 5.2 Public names

`flnd`:
- library: `open`, `Flnd`, `Targets`, `Auth`, `Plugins`, `Extension`, `Config`, `Run`, `State`,
  `TargetState`, `FileState`, `HoldState`, `Phase`, `Report`;
- events: `Event`, `Saved`, `Skipped`, `Failed`, `Planned`;
- rows: `Record` (a saved file), `Tracked` (a target), `Account`, `Status`, `Failure`,
  `Checked`, `Installed` (a plugin);
- `Target`;
- every error of 5.1;
- `__version__`.

Removed from `flnd`: `Progress`, `Login` (the row is now `Account`), `SourceError`.

`flnd.plugin`:
- the API: `API = 2`, `Plugin`, `Settings`, `Browser`, `route`, `Route`, `Category`;
- what a plugin yields and reads: `Post`, `File`, `Part`, `Stream`, `Target`, `Request`,
  `Response`, `Landed`, `Range`, `Fetch`, `DateLike`, `Clock`, `Document`, `Element`;
- logging in: `Account`, `Login`, `Cookies`, `cookies`;
- the context: `Ctx`;
- helpers: `dig`, `meta`, `page_info`, `best`, `numbered`;
- the errors `Blocked`, `NotFound`, `Hidden`, `Changed`, `LoginError`, `NetworkError`,
  `SiteError`, `UsageError`, `PluginError`, `Error`.

`Target` and `Account` are defined in `flnd/plugin.py` and re-exported by `engine/events.py`
and `flnd`.

`flnd.testing`: `FakeCtx`, `FakePage`, `Reply`, `Collected`, `Seen`, `collect`, `check`,
`fake_clock`.

`tests/data/api.txt` (`make api`) pins all three. Its diff is reviewed in every step that
changes a public name.

### 5.3 The plugin API (`flnd/plugin.py`, `api = 2`)

**Declarations on the class.** All are `ClassVar`s; a mistake raises `PluginError` when the
class is made, naming the attribute and the fix.

| name | type | default | meaning |
| --- | --- | --- | --- |
| `code` | `str` | required | `[a-z][a-z0-9]{1,15}`; what targets start with |
| `name` | `str` | required | shown in lists and help |
| `domains` | `tuple[str, ...]` | `()` | the plugin's own domains: paced, signed, cookies kept; a link on one is its |
| `api` | `int` | required, `2` | the API version |
| `login` | `Login \| None` | `None` | how it logs in; `None` means no login |
| `pace` | `float` | `0.5` | seconds between two page requests of one bucket of one account |
| `limits` | `Mapping[str, int]` | `{}` | host → the most files at once to it (raises its cap's ceiling) |
| `browser` | `Browser` | `Browser.NONE` | `NONE` (HTTP only), `ANY` (flnd's Chromium or the user's Chrome; defaults to Chromium), `CHROME` (needs the extension) |
| `stealth` | `bool` | `True` | hide automation in its tabs, applied per tab by an init script |
| `user_agent` | `str \| None` | `None` | User-Agent sent to its domains (HTTP only); `None` keeps the client's Chrome UA |
| `walls` | `tuple[str, ...]` | `()` | URL path prefixes meaning "log in": landing on one raises `LoginError` after retry |
| `folder` | `str` | `"{category}"` | a template under `media/<code>/<target>/` |
| `filename` | `str` | `"{post}_{num}"` | a template; the extension is added from the content |
| `key` | `str` | `"{post}_{num}"` | the history key under `<code>:` |
| `fields` | `tuple[str, ...]` | `()` | the post fields templates may use, besides the engine's |
| `unsupported` | `Mapping[str, str]` | `{}` | link or code patterns it refuses, mapped to the reason in `resolve`'s `UsageError` |
| `Settings` | a `Settings` subclass | `Settings` | its own config keys: a frozen msgspec struct; each field has a default and an `Attributes:` line in the docstring |

The engine's template fields stay as today: `plugin` (renamed from `source`), `target`,
`category`, `post`, `num`, `count`, `index`, `extension`, `path`, `date`, `query`.

**Routes and categories.**

```python
@route(*patterns: str | tuple[str, str], newest_first: bool = True, single: bool = False,
       discovers: bool = False, folder: str | None = None, shown: str | None = None)
async def user(self, ctx: Ctx, user: str) -> Mapping[str, Any]: ...

@user.category(own: bool = True, url: str | None = None, newest_first: bool | None = None,
               folder: str | None = None, key: str | None = None)
async def reels(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post | Target]: ...
```

- **Patterns** keep today's syntax (`ins:{user}`, `instagram.com/{user}`): a plain string has
  one placeholder named like the method's third parameter. An item may also be a
  `(pattern, value_template)` pair (`tuple[str, str]`) when a route combines multiple
  placeholders (`("facebook.com/permalink.php?story_fbid={post:\\w+}&id={user:\\d+}",
  "{user}_{post}")`) or adds a prefix (`("t.me/c/{chat:\\d+}", "-100{chat}")`).
- **`shown`** replaces the first pattern in help when the route accepts custom text (`tlg`).
- **What a route returns** is passed to every category as `profile`. A route with no
  categories is an `async def` generator yielding `Post | Target` itself.
- **`single=True`** marks a one-post route (a post or a video link). It is never followed,
  and a target of it with `/categories` is a `UsageError`. This replaces today's
  `single_kinds`.
- **`discovers=True`** marks a route whose categories yield `Target`s, which are run as
  their own targets (searches, a user's list of others).
- **`own`:**
  - `True` means the account's own content, and is the default selection;
  - `False` means other people's (tagged, reposts, liked), taken only with `/all` or when
    named.
- **`url`** is a template over `profile`. When the next wanted category has one, the
  scheduler opens it ahead in a warm tab while the current category runs.

**Types.**
- **`Post(id: str | int, files: Sequence[File] | Callable[[], Awaitable[Sequence[File]]] = (),
  *, date: DateLike = None, listed: DateLike = None,
  headers: Mapping[str, str] | None = None, **fields)`:**
  - converts `id` to `str`; has `.replace(**changes) -> Post` and `.lazy: bool`;
  - `files` as a coroutine function is a *lazy* post, whose files are listed only once the
    post is known to be new;
  - `date` names the files;
  - `listed` is the date the listing is ordered by (default: `date`).
- **`File(url: str | None = None, *, mirrors: Sequence[str] = (), fetch: Fetch | None = None,
  audio: str | None = None, name: str | None = None, size: int | None = None,
  password: str | None = None)`:**
  - exactly one of `url`, `mirrors` and `fetch` is given;
  - `audio` is a DASH audio stream to merge with the video (when `mirrors` holds a DASH video
    followed by a progressive fallback, `audio` merges only with `mirrors[0]`, fixing S9);
  - `name` overrides the stem `filename` makes;
  - `password` is an archive's.

  It also has `File.text(name: str, text: str) -> File`, `File.dash(manifest: str | None, *,
  height: int | None = None) -> File | None` (parses an inline MPD XML manifest, picking the
  best video at or under `height` and best audio), and `File.stream(playlist: str, *,
  headers: Mapping[str, str] | None = None) -> File` (an HLS `.m3u8` or segmented DASH
  `.mpd` playlist URL fetched as concurrent segment jobs).
- **`Fetch`:** `Callable[[Part], Awaitable[None]]`. `Part` has `offset: int`,
  `async write(data: bytes) -> None` and `total(size: int) -> None`, and resumes from
  `offset`. It replaces tlg's own part code.
- **`Target(text: str, *, kind: str = "")`:** has `.plugin: str`, `.kind: str`,
  `.value: str`, `.query: bool`; yielded by a route or category to run as its own target.
- **`Request(url: str, *, method: str = "GET", params: Mapping[str, Any] | None = None,
  body: bytes | str | Any = None, headers: Mapping[str, str] | None = None,
  check: bool = True)`:** a frozen struct with `.replace(**changes) -> Request`.
- **`Response`:** `status`, `url`, `headers`, `text`, `json`, `content`, `ok`, `html` (a
  `kit/html.py` document).
- **`Landed(url: str, status: int)`:** where `open` ended up.
- **`Stream`:** `url: str`, `height: int = 0`, `width: int = 0`, `bitrate: int = 0`,
  `audio: str | None = None`.
- **`Range`:** the run's `when`, as a date range; a category may read `ctx.when`.

**`Ctx`, the only way a plugin acts.** Each method below runs as the listed scheduler job, or
on the loop when its job kind is `none`.

| member | signature | job kind |
| --- | --- | --- |
| `fetch` | `async fetch(request: str \| Request, *, bucket: str \| None = None, in_page: bool = False) -> Response` | `page` on the plugin's domains, `file`-pooled elsewhere |
| `pages` | `pages(request: str \| Request, *, cursor: str \| Callable[[Response], str \| None], send: str \| Callable[[str], Mapping[str, Any]] = "cursor", bucket: str \| None = None) -> AsyncIterator[Response]` | `page`, one per page; the engine keeps and resumes the cursor |
| `open` | `async open(url: str) -> Landed` | `tab`; a 404 raises `NotFound`; landing on a wall retries once, then raises `LoginError` |
| `feed` | `feed(match: str \| Pattern \| Callable[[str], bool], *, scroll: bool = True, lines: bool = False, cursor: Callable[[Any], tuple[bool, str \| None] \| None] \| None = None) -> AsyncIterator[Any]` | `tab`; attaches its response listener immediately when called (usable with `async with` before `click`/`open`, or `async for`) |
| `load` | `load(url: str, match, *, scroll: bool = True, lines: bool = False, cursor=None) -> AsyncIterator[Any]` | `tab`; attaches `feed` before `open(url)`, yields `meta(document)`, then `feed` items |
| `document` | `async document() -> Document` | `tab` |
| `click` | `async click(css: str, *, timeout: float = 10.0) -> None` | `tab` |
| `handoff` | `async handoff(message: str, *, until: Callable[[], Awaitable[bool]], timeout: float \| None = None) -> bool` | `tab`; a person does a step (a captcha) in a visible window |
| `extension` | `async extension(op: str, **args) -> Any` | `tab`; an op of 6.8; `UsageError` with the hint `flnd extension install` when it isn't connected |
| `storage` | `async storage(origin: str) -> Mapping[str, str]` | `tab`; reads `origin`'s `localStorage` via extension `storage`, or Chrome profile before step 12 |
| `pause` | `async pause(seconds: float, reason: str) -> None` | none; sleeps on the run's clock so a page settles and `fake_clock` advances |
| `hold` | `async hold(reason: str, *, seconds: float \| None = None, bucket: str \| None = None) -> None` | none; sets a block hold or switches accounts under 6.2 for feed, DOM or SDK blocks (`FloodWaitError`) |
| `call` | `async call(fn: Callable[..., T], *args) -> T` | `call`, in the thread pool |
| `ask` | `async ask(message: str, *, secret: bool = False, description: str = "", choices: Sequence[str] = ()) -> str` | `UsageError` when nobody can answer (no terminal) |
| `log` | `log(level: str, message: str, **fields) -> None` | none; `level` is `"debug"`, `"info"` or `"warn"` |
| `target`, `when`, `time`, `settings`, `account` | properties | `Target`; `Range`; `Clock` (`.now()`, `.wall()`, `.day()`); `Settings`; `Account` or `None` |
| `cookies` | `Mapping[str, str]` | the account's cookies on the plugin's domains, read-only |
| `state` | `MutableMapping[str, Any]` | the plugin's own table in `flnd.db`; JSON values, each set one statement |
| `secrets` | `MutableMapping[str, str]` | the account's sealed secrets; writable only during a login |

Removed from today's `Site`:
- `wait`, `scroll`, `tab` and `html`: unused, or replaced by `document`;
- `evaluate`: now the extension op `evaluate`;
- `warm`: replaced by `url=` on a category;
- `back_off`: replaced by `blocked()` and `ctx.hold()`;
- `url`: replaced by `Landed.url`;
- `interactive`, `info` and `warn`: replaced by `ask`, which raises with nobody to answer,
  and by `log`;
- `client`.

**Hooks**, all optional:

| hook | signature | when |
| --- | --- | --- |
| `resolve` | `@classmethod resolve(cls, text: str) -> Target \| None` | matches `text` against the class's compiled patterns and `unsupported` rules when called on `cls` |
| `sign` | `sign(self, request: Request, cookies: Mapping[str, str]) -> Request` | before each try of a request to its domains; returns the request with headers added |
| `connect` | `connect(self, ctx) -> AbstractAsyncContextManager[None]` | once per run, around the plugin's targets (tlg's client) |
| `date` | `async date(self, ctx, post: Post) -> date \| None` | a post with no date, before its files are named |
| `blocked` | `blocked(self, response: Response) -> float \| None` | each answer from its domains; seconds to hold, or `None` when it isn't a block |
| `refresh` | `async refresh(self, ctx, post: Post) -> Post` | a file's link expired (403 or 410 after starting) or ran below `slowest`; returns the post with fresh files |

The default `refresh` lists the post's lazy files again, and raises `NotFound` for a post
that isn't lazy.

**`Login`:**
- **`class Login`** has:
  - `methods: tuple[str, ...]`;
  - `names: tuple[str, ...]`, the cookies, any one of which proves a login;
  - `url: str`, the login page for the `browser` method;
  - `required: bool = True`.
- **Its hooks:**
  - `async start(self, ctx, method: str) -> None`: fills `ctx.secrets`, or raises
    `LoginError`;
  - `async check(self, ctx) -> bool`: may `fetch`; the default is "any of `names` cookies
    present in `ctx.cookies`";
  - `async whoami(self, ctx) -> str | None`: the account's name on the site.
- **`cookies(*names, url, required=False) -> Cookies`** is the login of a cookie site. Its
  methods are `("browser", "chrome", "file", "firefox", "safari")`, and before step 12 also
  `"edge"`, `"brave"` and `"chromium"`.

**Helpers:**
- `meta(document) -> list[Any]`: the JSON in a page's `<script type="application/json">`
  blocks, with Meta's `__bbox` unwrapped. It replaces fbk's and ins's copies.
- `page_info(path: str) -> Callable[[Any], tuple[bool, str | None] | None]`: a cursor reader
  for `{has_next_page, end_cursor}` at a `dig` path (`None` if missing).
- `best(streams: Sequence[Stream], *, height: int | None = None) -> Stream | None`: the
  stream with the most pixels, then the most bits, at or under `height`; `None` means no cap.
- `numbered(n: int, *, suffix: str = "", width: int = 2) -> str`: `"07"` or `"07_cover"`. It
  replaces loi's and mht's copies.
- `dig(data, path)`: today's. A malformed path raises `PluginError`.

### 5.4 The test kit (`flnd/testing.py`)

- **`FakeCtx(plugin: type[Plugin] | Plugin, target: str | Target = "", *,
  routes: Mapping[str, Reply | Callable] | None = None,
  pages: Mapping[str, FakePage | str] | None = None,
  account: str | None = None, cookies: Mapping[str, str] | None = None,
  when: Range = Range(), answers: Sequence[str] = (),
  state: MutableMapping[str, Any] | None = None,
  storage: Mapping[str, str] | None = None,
  extension: Mapping[str, Any] | None = None)`.**
  - `Reply(body: Any = None, *, status: int = 200,
    headers: Mapping[str, str] | None = None, url: str = "")` answers `fetch` and `pages`;
  - `FakePage(html: str = "", *, status: int = 200, url: str = "",
    feeds: Sequence[tuple[str, Any]] = ())` answers `open`, `document` and `feed`;
  - `Seen` (`ctx.seen`) records `requests`, `visited`, `scrolls`, `logs` and `holds`.

  `FakeCtx` behaves like the engine:
  - a 401 or 403 from the plugin's domains raises `LoginError`;
  - `blocked` returning seconds or `ctx.hold` records the hold in `ctx.seen.holds` and moves
    on with the fake clock;
  - `open` returns `Landed` with the page's status, raising `NotFound` and `LoginError` as
    the engine does;
  - `ask` pops `answers`, and raises `UsageError` when they run out;
  - `extension` and `storage` read the given fakes.
- `Collected(path: str, url: Any, post: Post, key: str, file: File)` with `.date: date | None`.
- `collect(plugin, target: str, *, ctx: FakeCtx | None = None,
  categories: Sequence[str] | None = None, limit: int | None = None) -> list[Collected]` runs
  a target to its end, checks that a `newest_first` category's dated posts are non-increasing
  and that every template field is filled, and returns the `Collected` files.
- `check(plugin)` runs the class checks and verifies that every backticked example target in
  each route's docstring resolves to that route.
- `fake_clock()` is today's, and also fakes `clock.day()` (K6).

## 6. Contracts: the engine

### 6.1 The run (`engine/run.py`)

- **Threads.**
  - `Run` starts one thread, `flnd-loop`, running one asyncio loop for the whole command.
  - The caller's thread is the *reader*.
  - On a terminal, `ui/screen.py` runs one more thread, `flnd-screen`.
  - There are no other long-lived threads; the pools of 6.2 are the scheduler's.
- **The event channel** holds 256 events. Producers are only on the loop, and `await
  put(event)` waits when the channel is full. The reader takes events with a blocking get.
  At the end, `END` is put after the last event; the loop has no other producer, so `END`
  is always last (fixes E10 and E11).
- **`run.state`** is an immutable `State` struct (6.2), swapped in by the loop every 100 ms
  when something changed. Readers on any thread read the reference.
- **Ctrl-C** is handled on the reader's thread when it is the main thread:
  1. the first sets `stop.soon`: no new listing work, queued files dropped, files already
     started finish;
  2. the second sets `stop.now`: every job is cancelled and parts are kept;
  3. the third runs the registered terminal restorers, then `os._exit(130)`.
- **`run.suspend()`** stops new jobs from starting; running ones go on. **`resume()`** undoes
  it.
- **Unstarted runs.** Dropping a `Run` that was never iterated, awaited or stopped emits a
  `ResourceWarning`.
- **The journal** belongs to the run: one file handle, and `write(record)` takes a lock and
  writes one whole line (fixes E9). Records are encoded with an `enc_hook` that writes a
  `Path` as a string and an `Error` as `{"class", "message", "hint"}` (fixes E8).

### 6.2 The scheduler (`engine/jobs.py`)

**`Job`** is a plain class with these fields:
- `kind: Kind` (`PAGE`, `FILE`, `TAB`, `CALL`, `HASH`, `UNPACK`, `MERGE`, `EXEC`, `WRITE`);
- `plugin: str`, `account: str | None`, `bucket: str`, `host: str`;
- `target: str | None`;
- `priority: int`;
- `size: int | None`, the bytes when known;
- `work: Callable[[], Awaitable[T]]`.

`await scheduler.run(job) -> T` waits for the job's pools in order, runs `work` under the
retry policy, and releases the pools.

**Pools and their numbers:**

| kind | waits for | numbers |
| --- | --- | --- |
| `PAGE` | the hold of (plugin, account, bucket), then its pace token | one-token bucket at `1 / pace`; `pace` from config, else the class (0.5 s) |
| `FILE` | the target's place, then the host's cap, then the rate limit | places per target `max(32, max(limits.values(), default=0))`; queue per target 4 × places; cap starts at 8, grows to 32 or `limits[host]` |
| `TAB` | the run's tab budget, and for a scrolling feed the account's scroll gate | 6 tabs; 2 scrolling tabs per account; one target reuses one tab; a warm tab opens only when at least 2 slots are free and never blocks an active target |
| `CALL` | the thread pool | `min(32, os.cpu_count() + 4)` threads |
| `HASH` | the hasher pool | `os.cpu_count()` threads |
| `UNPACK` | the archive lane | 1 thread |
| `MERGE` | the merge lane | `max(1, os.cpu_count() // 2)` ffmpeg processes at once |
| `EXEC` | the exec lane | 1 at a time, in the order the files were saved |
| `WRITE` | the writer of the file's device (`os.stat(folder).st_dev`) | 1 thread per device; 4 MiB pieces; a transfer pauses at 8 MiB unwritten and resumes at 4 |

**The AIMD cap per host** is today's `net.Cap`, moved:
- it starts at 8;
- a 429, a 503, a reset, a timeout or a short body halves it, once per round;
- every 10 clean transfers close a window;
- it grows by 1 while throughput rises by at least half of what one more place would add;
- a step that didn't help is taken back, and the cap holds for 100 transfers;
- a window with a free place and nobody waiting is dropped.

**Priorities:** 0 a retried failure, 1 listing (`PAGE`, `TAB`), 2 a file, 3 a post step
(`HASH`, `UNPACK`, `MERGE`, `EXEC`). Lower runs first, and equal priorities run first in,
first out.

**The retry policy**, the only one (`_transfer._again`, the sources' `back_off` loops and
`Client.call`'s loop are deleted):

| what happened | what the scheduler does |
| --- | --- |
| `NetworkError`, or a 5xx | try again after 1, 2, 4, 8 s, each × a random 0.5–1.5; then fail the job |
| 429 | hold the bucket for `Retry-After` or `x-rate-limit-reset` (at most 3600 s; else 30 s), and halve its rate for 20 clean requests; a third 429 in a row on a bucket counts as a block round |
| `blocked()` returned seconds, `ctx.hold(...)`, or a block round | with switching on and another account free: move the job to it. Else hold (plugin, account, bucket) for the larger of the seconds and the round's wait (30, 120, 480 s); a fourth round fails the job with `Blocked`. Rounds reset after 600 s with no block |
| `LoginError` | landing on `plugin.walls` in `open()` retries once after a 30 s hold first. With switching on and another account: move the job. Else fail it, and mark the account `expired` |
| `Hidden` | with switching on: move the job. Else fail it: a file is recorded failed, a target fails |
| `NotFound` | on a target's first page with switching on: move the job. Else a file is recorded gone, and a target fails |
| `Changed` | fail the job, save a scrubbed sample, never retry |
| `DiskError`, a missing tool, or `PluginError` from a broken class | stop the run (exit 4) |
| a file failed for any reason above | a `failures` row; the next runs retry it, up to 3 runs (`RETRIES = 3`) |

**Segments**, for a `FILE` job:
- A file whose size is at least 16 MiB, from a host that answered `Accept-Ranges: bytes` or a
  206, may take more places.
- **When to split.** Every 1 s, from 2 s after the transfer started, if the host's cap has a
  free place, no other `FILE` job waits for that host, and the file has fewer than 8
  segments, then the largest remaining range of at least 8 MiB is split in half. The new
  half becomes a segment job.
- **When a segment ends,** the same rule runs at once.
- **Mirrors.** Segments are taken from the URL that answered first. Another mirror is used
  for a segment only when its `Content-Length` and `ETag` match the first URL's; a segment
  that fails on another mirror falls back to the first URL.
- **Writing.** Each segment writes at its offset into the one part. The part's sidecar,
  `<name>.part.json`, holds `{"etag", "last_modified", "size", "segments": [[start, end,
  written], …]}`, written after each 4 MiB piece is on disk, so a later run resumes each
  segment.
- The dead `_Segments` and the `Client(segments=)` flag are deleted in step 2.

**`slowest`.** A transfer whose average over the last 30 s is below `[defaults] slowest`
(bytes a second; 0 means off) is cancelled. Its bytes are kept, `refresh` gets a fresh link,
and it goes on from where it was. This counts as one of its 4 network tries.

**`rate`.** One byte bucket for every `FILE` job of the run, `[defaults] rate` bytes a second
(0 means off), holding at most 1 s of bytes.

**The free-space check.** Before a `FILE` job with a known size starts:
- free space comes from `shutil.disk_usage` of the file's folder, cached 1 s per device;
- it must be at least the file's size, plus the bytes still due from files in flight on that
  device, plus 64 MiB;
- if it isn't, the run stops with `DiskError("not enough free space for <file>: <n> needed,
  <m> free")`.

**Targets at once:**
- a plugin whose `browser` isn't `NONE` lists one target per account at a time;
- an HTTP-only plugin lists 4 at a time;
- the run runs at most 16 active targets at once: a target holds its slot while `LISTING` or
  while it has more than `places` `FILE` jobs queued.

A target's files keep downloading while the next target lists once the queue falls below
`places`.

**`State`**, the snapshot, made of frozen msgspec structs:

```python
class State:            # what the screen and a script read
    version: int        # +1 each publish
    elapsed: float      # seconds since the run started
    targets: tuple[TargetState, ...]
    files: tuple[FileState, ...]      # in flight, oldest first, at most 6
    more: int                         # files in flight beyond those 6
    holds: tuple[HoldState, ...]
    now: str                          # what is happening, one line
    saved: int; skipped: int; failed: int; bytes: int; speed: float   # whole run

class TargetState:
    text: str; categories: tuple[str, ...]; phase: Phase   # WAITING LISTING DOWNLOADING DONE FAILED
    found: int; saved: int; skipped: int; failed: int; bytes: int; speed: float

class FileState:
    target: str; path: str; done: int; total: int | None; speed: float

class HoldState:
    plugin: str; account: str | None; bucket: str; until: float; reason: str; count: int
```

- Speeds are bytes a second over the last 3 s.
- `now` is the newest hold if one is active. Otherwise it is the newest listing step,
  `"<target> · <category>, page <n>"`.

### 6.3 The crawl (`engine/crawl.py`)

Today's listing policy, kept whole, with today's constants:
- `RETRY_WINDOW = 100`, `RETRIES = 3`, `HEAD_IN_A_ROW = 2`, `OLDER_IN_A_ROW = 5`,
  `stop_after = 5`, `FOLLOW_DEPTH = 2`, `MAX_SYNC_NEW = 25`.

What it does:
1. **Failures first.** Failed rows in `failures` with `retryable = 1` and `attempts < RETRIES`
   (`3`), plus whole-target failures (`post_id = ''`), are listed again from their page's
   cursor and queued at priority 0.
2. **Each wanted category, in declaration order:**
   - resume a saved cursor, then catch up from the newest;
   - consecutive `Post`s with the same `post.id` count once toward `HEAD_IN_A_ROW`,
     `stop_after` and `OLDER_IN_A_ROW`;
   - a sync stops after the head and the post after it (`HEAD_IN_A_ROW`), or after
     `stop_after` saved posts in a row;
   - with `when`, a `newest_first` category stops after `OLDER_IN_A_ROW` older posts in a
     row;
   - a sync cut short keeps a gap, which the next sync fills.
3. **The Ledger** gives each file its path and key:
   - a file whose key is in `files` is `Skipped(reason="saved")`;
   - check on disk with a single `stat()` (`try: st = dest.stat()` instead of `is_file()` +
     `stat()`, fixing P8): when `st.st_size > 0` and the key isn't in `files` (such as after
     `target clean` without `--media`), record the row in `files` (`hash = None`) and emit
     `Skipped(reason="saved")`;
   - a file saved under another category is linked;
   - undated posts are held until the category ends (`release`), taking their nearest dated
     neighbour's day (`estimated = 1`); `Plugin.date(ctx, post)` then fills posts still
     undated and saved rows in `files_undated` (`redate`), caching misses for 30 days in
     `ctx.state["redate.asked"]` and stopping on `Blocked` or `LoginError`.
4. **Searches and discovered targets** run as their own targets, each with its own plugin and
   account (fixes E12), up to `FOLLOW_DEPTH`.
   - A search sync takes at most `MAX_SYNC_NEW` new targets.
   - It walks past seen ones, and doesn't stop at the first, so a later sync reaches the
     rest (fixes E3).
5. **`limit=N`** stops a normal target after `N` new posts are queued, and a `discovers=True`
   route after `N` targets are discovered.
6. **`dry=True`** lists and plans as usual, sends `Planned(target, post, path, state="new" |
   "saved", category=category)` for each file (with `post=""` and `path=None` for a
   discovered target), and writes nothing to disk or to `flnd.db`.

### 6.4 The save (`engine/save.py`)

One `FILE` job (with its segments) does the following, in order:
1. **Open the part**, `<folder>/<name>.part`. It opens with `O_EXCL` semantics and locks
   itself. A part held by another run is `Skipped(reason="busy")`.
2. **Transfer**, following the mirrors in order. For DASH, video and audio download as two
   jobs and are joined by one `MERGE` job (`kit/media.py`). Empty audio saves the video alone
   (fixes E7).
3. **The content check, as today:**
   - a web page where media was expected fails the file;
   - the extension comes from the content's magic bytes;
   - the part's mtime is set to the post's date at noon UTC;
   - the part is renamed onto the final name.
4. **Dedupe** (`[defaults] dedupe = true`):
   - look up saved files with the same `size` (the `files_size` index);
   - when there are none, the file is unique and has no hash;
   - when there are, `HASH` jobs hash this file and each candidate that has no hash yet, and
     store the hashes;
   - an equal hash replaces this file with a hard link to the saved one; where hard links
     fail, both copies stay.
5. **The commit.** All writes go through the one `store.py` connection on the loop with
   `busy_timeout = 0` and `wal_autocheckpoint = 0`. When the post's last file settles, all its
   rows go in one transaction on the loop; after every 1,000 committed rows and on run close,
   a `CALL` job runs `PRAGMA wal_checkpoint(PASSIVE)`. On `SQLITE_BUSY` from another process,
   the commit is queued again after 20 ms, and the loop never waits (fixes E18).
6. **Unpack.** An archive (zip, 7z, rar, tar) is extracted beside itself by the `UNPACK` lane,
   and its row is updated.
7. **Sidecar** (`[defaults] sidecars = true`): `<name>.json` with the post's fields.
8. **`Saved`** is sent.
9. **`exec`.** `[defaults] exec` is read only from `~/.config/flnd/config.toml` (if set in a
   home's `config.toml`, it is ignored with a warning). When set, an `EXEC` job runs it with
   `{path}`, `{target}` and `{post}` filled in and `$FLND_PATH`, `$FLND_TARGET` and
   `$FLND_POST` set, via `subprocess.run(list)` and never through a shell. A failure is a
   warning in the journal; the file stays saved.

### 6.5 The resolver (`engine/resolve.py`)

```text
target      := plugin-form | search | link
plugin-form := code ":" value [ "/" categories ]
search      := code "?" query [ "/" categories ]
categories  := "all" | name ( "," name )*
link        := "http://" … | "https://" …
```

- **`code`** must be a known plugin, else `UsageError("no plugin named <code>")`, with the
  codes as the hint.
- **A link** belongs to the first plugin whose `domains` match its host, once one of
  `www.`, `m.`, `mobile.`, `web.`, `mbasic.` or `touch.` is dropped.
  - Built-ins are tried first, then installed plugins by code.
  - A link no plugin claims goes to `web`.
  - A short link no route reads is followed once with HEAD.
  - A link takes default categories. To choose categories, use the plugin form.
- **Categories:**
  - an unknown name is `UsageError("<code> has no category <name>")`, with the route's
    categories as the hint;
  - `/all` is every category;
  - no slash means the stored categories of a known target, else the route's `own=True`
    ones;
  - categories on a `single` route are a `UsageError`;
  - for `plugin-form`, `/categories` splits at the last `/` only when the route has categories
    or `discovers=True` (and for catch-all patterns like `tlg:{chat:.+}`, only when every
    comma-separated token after `/` is `"all"` or one of the route's categories, keeping
    `tlg:News / Updates` whole);
  - for `search` and `discovers=True` routes (`ins:me/reels`, `mht?words/cover`), the names
    must belong to the plugin's categories and are passed to every discovered target.
- **The key** of a target is `code:value`, with the code lowercased and the value as the
  route's pattern (or `(pattern, value_template)` pair) formats it. Categories are not part
  of the key.
- **Resolving reads only the manifest** (`plugins/_manifest.py` and `plugins.json`), never a
  plugin module, until the target runs. `unsupported` accepts link and code patterns (checked
  by specificity before catch-all routes), and multi-placeholder or prefixed targets (`fbk`
  `permalink.php`, `tlg` `t.me/c/`) use `(pattern, value_template)` pairs stored in the
  manifest. `Plugin.resolve(cls, text)` runs those same compiled rules when called directly.

### 6.6 The plugin manager (`engine/plugins.py`)

- **The folder** is `$FLND_PLUGINS`, else `~/.config/flnd/plugins/`, else
  `%APPDATA%\flnd\plugins\` on Windows. It holds `<code>.py` files, `<code>/` packages and
  `plugins.json`.
- **`plugins.json`:**

  ```json
  {"version": 1, "plugins": {"exm": {"name": "Example", "api": 2,
    "domains": ["example.com"], "file": "exm.py", "sha256": "…", "installed": "2026-11-18T…Z",
    "routes": [{"name": "user", "patterns": ["exm:{user}", "example.com/{user}"],
                "single": false, "categories": [{"name": "posts", "own": true}]}]}}}
  ```

  `plugins/_manifest.py` holds the built-ins in the same shape, generated by `make manifest`,
  and `tests/test_manifest.py` checks it.
- **`install(path)`:**
  1. `path` must be a `.py` file or a folder holding `__init__.py`, and neither inside a
     `.flnd` home folder nor matching `*_cookies.txt`, else `UsageError`.
  2. Import it from a temporary copy, under the module name `_flnd_plugin_<code>`.
  3. It must hold exactly one `Plugin` subclass that passes its checks.
  4. A built-in's code is refused. An installed code is replaced.
  5. Copy it to the folder, atomically.
  6. Write its entry, with the file's SHA-256.
  7. Return `Installed`.
- **Loading.** A plugin is imported the first time one of its targets runs. When the file's
  SHA-256 differs from its entry, it is checked again and the entry rewritten. A file in the
  folder without an entry is ignored, with the warning "<file> isn't installed: flnd plugin
  install <file>".
- **The API version:** `api == 2` loads. A higher one is disabled with the hint "needs a newer
  flnd". A lower one is disabled with the names it must change, from `OLD_NAMES`.
- **A broken plugin** (an import error, a failed check) is disabled alone, with its own file
  and line (fixes S7: the prefix test compares with a path separator).
- **`uninstall(code)`** removes the file or folder and its entry. Its accounts and targets
  stay, and a warning says so. A built-in is a `UsageError`.

### 6.7 The auth manager (`engine/auth.py`)

**Accounts** are rows of `accounts` (7.1). `name` is `Login.whoami`. When that is `None`, the
form asks for it (`example_user`). Without a terminal it is `account<n>`.

**Login methods:**

| method | what it does |
| --- | --- |
| `browser` | opens a visible flnd Chromium window at `login.url`; polls the cookies every 1 s until any of `login.names` exists on the plugin's domains (at most 10 min); saves them; closes the window |
| `chrome` | the extension's `cookies` op for the plugin's domains; `UsageError` with the hint `flnd extension install` when it isn't connected |
| `file <path>` | a Netscape `cookies.txt` or a JSON export, at most 16 MiB; only the plugin's domains are kept |
| `firefox`, `safari` | today's store readers (Safari needs Full Disk Access; the hint says so) |
| `edge`, `brave`, `chromium` | today's keychain readers; removed in step 12 |
| a plugin's own | `Login.start(ctx, method)`: tlg's `browser`, `qr` and `phone` |

- **No method given:** a terminal shows the login form, and no terminal is a `UsageError`
  listing the methods.
- **After a login,** `Login.check` must pass, else `LoginError`.
- **The engine stores every account,** warns at a run's start about cookies that expire
  within 3 days, writes the cookies a site refreshed back to the vault at the run's end, and
  logs out (deleting the account's secrets and its row).

**Switching** (`switch = true` for the plugin) follows 6.2's table.
- A job moves to the next account of the plugin, in `auth list` order, that is `ok` and not
  held. Any open tab for that target is closed and reopened under the new account's cookies;
  a plugin with `connect` (`tlg`) enters `connect` for the new account; a resumed `pages`
  cursor refused with 4xx restarts that category from the newest page (`cursor = None`), while
  `Ledger` skips posts already saved.
- A post whose files were `Hidden` is listed again under the new account, since media links
  are signed per session.
- With every account held, the job waits for the earliest hold to end.
- The account that worked is written to `targets.account`, and is tried first next time.
- Each move is a journal record `{"type": "switch", "plugin", "from", "to", "reason"}`, and
  the `now` line shows it.

**The sealed vault** (`kit/vault.py`, using its existing `encrypt` and `decrypt`):
- **A value** is `bytes([1]) + nonce(12) + AES-256-GCM(ciphertext + tag)`, with associated
  data `f"{account_id}\0{name}".encode()`. A value moved to another slot doesn't decrypt.
- **The key** is 32 random bytes, one per user, shared by every home, stored in
  `~/.config/flnd/key` (or `%APPDATA%\flnd\key` on Windows), mode 0600, written atomically.
  The OS keychain in `kit/vault.py` is deleted in step 11.
- **When there is no key file** and a login (or the schema-2 migration) writes a secret, the
  key file is created. Rows sealed under an earlier key stay, and raise the `LoginError`
  below until their account logs in again.
- **The key is never inside a home.**
- **A sealed value whose key is missing or wrong** (`~/.config/flnd/key` is missing or
  `decrypt` raises `InvalidTag`) raises `LoginError("the vault's key is gone")` with the
  hint `flnd auth login <plugin>`. The sealed rows are kept, never deleted: they decrypt
  again once the key file is restored, and a new login of that account overwrites them.
- **Migration:** `secrets(slot, name, value TEXT)` becomes `secrets(account INTEGER, name
  TEXT, sealed BLOB)`. Every plugin code in `flnd.db`'s `logins` or `secret.db`'s `slot`
  gets a default `accounts` row (`name = 'default'`, `is_default = 1`), and every plaintext
  value is sealed under its `account.id` in one transaction when schema 2 first opens it.

### 6.8 The extension (`flnd/extension/`, `engine/extension.py`)

- **The files:**
  - `manifest.json`: Manifest V3, with a fixed `key` so the extension ID never changes;
    permissions `cookies`, `tabs`, `debugger`, `storage`, `scripting` and `nativeMessaging`;
    `host_permissions` are the built-ins' domains, written by `make manifest`; plugins'
    domains are `optional_host_permissions`;
  - `background.js`: the service worker;
  - `page.js`;
  - `popup.html` and `popup.js`.

  They ship inside the wheel.
- **The host.** `flnd host` is a hidden command that Chrome starts. Its manifest is
  `com.flnd.host.json`, with `allowed_origins` set to that one extension. It is written:
  - on macOS, to `~/Library/Application Support/<Google/Chrome | Chromium |
    Microsoft Edge | BraveSoftware/Brave-Browser>/NativeMessagingHosts/`;
  - on Linux, to `~/.config/<google-chrome | chromium | microsoft-edge |
    BraveSoftware/Brave-Browser>/NativeMessagingHosts/`;
  - on Windows, as the key `HKCU\Software\<vendor path>\NativeMessagingHosts\com.flnd.host`.
- **The relay.** The host listens on `$XDG_RUNTIME_DIR/flnd/host.sock`, else
  `~/.config/flnd/run/host.sock` (folder mode 0700, socket mode 0600). On Windows it is the
  named pipe `\\.\pipe\flnd-host-<user>` with a DACL allowing only the current user SID. A run
  connects there and sends the shared secret from `~/.config/flnd/extension/token.json`
  (mode 0600) in `hello` before any other op runs.
- **Messages** are `{"id": int, "op": str, "args": {…}}`, answered by `{"id", "ok": true,
  "value"}` or `{"id", "ok": false, "error": {"class", "message"}}`. An answer over 768 KiB
  is sent in parts `{"id", "part": i, "of": n, "chunk": "<base64>"}`, since Chrome caps a
  host's message at 1 MB.
- **The ops:**

  | op | args | value |
  | --- | --- | --- |
  | `hello` | `{"token"}` | `{"version"}` |
  | `cookies` | `{"domains": [str]}` | `[{name, value, domain, path, expires, secure, httpOnly, sameSite}]` |
  | `tabs.open` | `{"url", "active": false}` | `{"tab", "url", "status"}` |
  | `tabs.list` | `{}` | `[{"tab", "url", "title"}]` |
  | `tabs.close` | `{"tab"}` | `null` |
  | `capture.start` | `{"tab", "match": regex}` | `{"capture"}` |
  | `capture.read` | `{"capture"}` | `[{"url", "status", "body": base64}]`, the bodies since the last read |
  | `capture.stop` | `{"capture"}` | `null` |
  | `document` | `{"tab"}` | `{"html"}` |
  | `click` | `{"tab", "css"}` | `null` |
  | `scroll` | `{"tab", "by": px}` | `{"height"}` |
  | `evaluate` | `{"tab", "script"}` | `{"value"}`, through `chrome.debugger`'s `Runtime.evaluate` |
  | `storage` | `{"origin"}` | `{key: value}`, the origin's local storage |
  | `screenshot` | `{"tab"}` | `{"png": base64}` |
  | `media` | `{"tab"}` | `[{"url", "kind": "video"\|"hls"\|"dash"\|"image", "size"}]` |
- **The browser backend.** With `browser = "chrome"` (the plugin's `Browser.CHROME`, or the
  config over `ANY`), `open`, `feed`, `document` and `click` run as these ops, and `feed` is
  `capture.start` plus `capture.read`. `document`, `click` and `scroll` use
  `chrome.scripting`; `chrome.debugger` is attached only for `capture.start`..`capture.stop`
  and `evaluate`, and detached right after.
- **The popup** asks flnd through the host, using the default home recorded at install
  (`~/.config/flnd/extension/host.json`):
  - `resolve {url}` returns the target and whether it is followed; the buttons are **Get**
    (runs `flnd get <target>`) and **Follow**;
  - `media {tab}`, whose items each have **Get**, which sends the URL to the `web` plugin.
- **Install.** `flnd extension install`:
  1. copies the files to `~/.config/flnd/extension/`, generates a 32-byte shared secret in
     `~/.config/flnd/extension/token.json` (mode 0600), and records the default home
     (`$FLND_HOME`, else `Path.cwd() / ".flnd"`) in `host.json`;
  2. writes the host manifest for every Chromium browser found;
  3. prints three numbered steps (open `chrome://extensions`, turn on Developer mode, Load
     unpacked with that path);
  4. waits, with a spinner, up to 10 minutes for `hello`.

  `uninstall` removes the files and the host manifests.
- **Tests:**
  - `tests/test_extension.py` drives the protocol against a fake extension, in
    `make check`;
  - `make ext` loads the real extension unpacked in Playwright's Chromium against
    `tests/bench/server.py`. It is never part of `make check`.

## 7. Contracts: storage and config

### 7.1 `flnd.db`, schema 2

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;  -- version = '2'
CREATE TABLE files (
    key TEXT PRIMARY KEY, target TEXT NOT NULL, plugin TEXT NOT NULL,
    post_id TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0, mtime INTEGER, date TEXT,
    estimated INTEGER NOT NULL DEFAULT 0, kind TEXT, files INTEGER, hash BLOB,
    state TEXT NOT NULL DEFAULT 'saved',            -- saved | gone | link
    unpacked TEXT NOT NULL DEFAULT '', folder TEXT, sidecar TEXT,
    at TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
CREATE INDEX files_post ON files (target, post_id);
CREATE INDEX files_size ON files (size) WHERE state = 'saved';
CREATE INDEX files_hash ON files (hash) WHERE hash IS NOT NULL;
CREATE INDEX files_undated ON files (target) WHERE state = 'saved' AND (date IS NULL OR estimated);
CREATE VIEW posts AS
    SELECT target, post_id, min(category) AS category, max(date) AS date,
           max(count(*), coalesce(max(files), 1)) AS files, max(at) AS at
    FROM files WHERE state = 'saved' GROUP BY target, post_id;
CREATE TABLE targets (
    key TEXT PRIMARY KEY, plugin TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL,
    follow INTEGER NOT NULL DEFAULT 0, categories TEXT NOT NULL DEFAULT '[]', query TEXT,
    complete TEXT NOT NULL DEFAULT '{}', cursors TEXT NOT NULL DEFAULT '{}',
    seen TEXT NOT NULL DEFAULT '[]', last_sync TEXT NOT NULL DEFAULT '',
    files INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
    heads TEXT NOT NULL DEFAULT '{}', gaps TEXT NOT NULL DEFAULT '{}', account TEXT
) WITHOUT ROWID;
CREATE TABLE runs (
    id INTEGER PRIMARY KEY, command TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
    saved INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE failures (
    run INTEGER, key TEXT NOT NULL, target TEXT NOT NULL, post_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '', file TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '', class TEXT NOT NULL, message TEXT NOT NULL,
    hint TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 1, at TEXT NOT NULL,
    cursor TEXT, retryable INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX failures_target ON failures (target, key);
CREATE INDEX failures_run ON failures (run);
CREATE UNIQUE INDEX failures_key ON failures (key);
CREATE TABLE state (plugin TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY (plugin, key)) WITHOUT ROWID;
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY, plugin TEXT NOT NULL, name TEXT NOT NULL,
    origin TEXT NOT NULL,                           -- browser | chrome | file | … | qr | phone
    at TEXT NOT NULL, is_default INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',              -- ok | held | expired
    held_until TEXT, expires TEXT, UNIQUE (plugin, name)
);
```

**The migration from version 1 to 2** runs in one transaction, when a home of version 1 is
first opened:
1. rename `source` to `plugin` in `files`, `targets` and `state`, and recreate the `posts`
   view;
2. rename `targets.options` to `categories`, keeping its JSON `"only"` list (or `"categories"`
   if already present, else `[]`) as the JSON list `targets.categories`;
3. drop the `shrink`, `before`, `format` and `tools` columns, and the `probes` table;
4. add `files_size`, `targets.account` and `accounts`;
5. turn each `logins` row (and any `secret.db` `slot` not in `logins`) into an `accounts` row
   (`name = 'default'`, `is_default = 1`), then drop `logins`;
6. set `meta.version = '2'`.

`secret.db` is migrated as 6.7 says.

**The test.** `tests/test_store.py` builds a version-1 home with today's schema text, opens
it, and checks every row.

Pragmas are kept: WAL, `synchronous = NORMAL`, `fullfsync` on macOS,
`cache_size = 64 MiB`. One shared store connection per `Home` uses `wal_autocheckpoint = 0`
(checkpointed passively on a `CALL` thread every 1,000 committed rows and on close) and
`busy_timeout = 0` for loop commits, with `busy_timeout = 30000` for connections opened by
other processes or by read-only commands.

**The home's layout** is kept:
- `media/<code>/<target>/…`;
- `metadata/flnd.db` and `metadata/secret.db`;
- `log/`: journals and crash files, and `<run>/samples` kept 30 days;
- `config.toml`.

`cache/` goes.

### 7.2 `config.toml`

Every key is below. A wrong value is a `UsageError` naming the key, and an unknown key is a
warning. A setting resolves in this order: a library argument, then `<home>/config.toml`,
then `~/.config/flnd/config.toml`, then the default (except `[defaults] exec`, which is read
only from `~/.config/flnd/config.toml`).

| key | type | default | meaning |
| --- | --- | --- | --- |
| `[defaults] dedupe` | bool | `true` | hard-link a file whose bytes are already saved (`"link"`/`"off"` warned and mapped) |
| `[defaults] sidecars` | bool | `false` | write `<name>.json` beside each file |
| `[defaults] when` | str | `""` | a year, month or day, `a..b` with either end open, or `Nd`/`Nw`/`Ny`; `""` means every day |
| `[defaults] exec` | list[str] | `[]` | a program and its arguments, run after each saved file (read only from `~/.config/flnd/config.toml`; ignored with a warning in a home's `config.toml`) |
| `[defaults] rate` | str | `"0"` | the run's speed limit: `"20 MB/s"`, `"500 KB/s"`; `"0"` means off |
| `[defaults] slowest` | str | `"0"` | restart a transfer below this speed for 30 s; `"0"` means off |
| `[defaults] height` | int | `0` | the tallest video stream to pick; `0` means the best |
| `[defaults] switch` | bool | `false` | switch accounts, for every plugin that doesn't set its own |
| `[plugins.<code>] pace` | float | the class's | seconds between page requests of one bucket |
| `[plugins.<code>] limits` | table | `{}` | `{host = n}`, merged over the class's |
| `[plugins.<code>] proxy` | str | none | `http`, `https`, `socks5` or `socks5h` URL, for HTTP (`PAGE` and `FILE`) requests; warns once when opening an unproxied browser |
| `[plugins.<code>] stop_after` | int | `5` | a sync stops after this many saved posts in a row |
| `[plugins.<code>] browser` | str | `"chromium"` for `ANY`, else the class's | `"chromium"` or `"chrome"`, only when the class is `ANY` |
| `[plugins.<code>] switch` | bool | `[defaults] switch` | as above, for this plugin |
| `[plugins.<code>] folder`, `filename` | str | the class's | templates, checked against the fields before any request |
| `[plugins.<code>] <field>` | per `Settings` | per `Settings` | the plugin's own settings |

- **Gone:**
  - `[defaults] again`;
  - `[vault] key`;
  - `[sources.<code>]`, which is read once as `[plugins.<code>]` with a warning to rename it;
  - `scroll`.
- **The two files.** `<home>/config.toml` holds the home's settings. The user file,
  `~/.config/flnd/config.toml` (`%APPDATA%\flnd\config.toml` on Windows), holds settings
  shared by every home, and is the only place `exec` is read from.
- **`flnd config`** writes every key to `<home>/config.toml`, except `exec`, which it writes
  to the user file. Its "where it comes from" column names the file a value was read from
  (`home`, `user` or `default`). It edits with tomlkit (a new dependency), keeping comments.
  Each write is atomic and checked by the same structs. `Flnd.config` keys use dotted names
  (`"defaults.when"`, `"plugins.ins.pace"`, with bare `"when"` accepted for `[defaults]`).
- **Names** are cut to 255 bytes, never in the middle of a UTF-8 character, keeping the
  extension.

## 8. Contracts: the command line, the screen, the library, the journal

### 8.1 Commands

| command | does | exit |
| --- | --- | --- |
| `flnd get [targets…]` | runs the targets; adds a target that isn't single to the followed ones; no target from the arguments or stdin syncs every followed target (or prints `"nothing to sync"` and exits `0` when none are followed) | 0, or 1 when a file or target failed, 2, 3, 4, 130 |
| `flnd target list` | the list of 8.3; on a pipe, a plain table | 0 |
| `flnd target clean <targets…> [--media]` | forgets the targets: their rows, files rows, failures, cursors, heads and gaps. `--media` also deletes their recorded files, sidecars, links (`state = 'link'`), extracted archive `folder`s and folders left empty. Unrecorded folders under `media/` are never auto-followed (`_unrecorded` goes). A terminal asks first ("Forget 2 targets and delete 1.2 GB?"); a pipe doesn't | 0, 2 |
| `flnd auth list` | the list of 8.3 | 0 |
| `flnd auth login <plugin> [method] [path]` | logs an account in (6.7); `path` is only for `file` | 0, 2, 3 |
| `flnd auth logout <plugin> [account]` | logs one account out, or the default when none is named; a terminal asks first; a pipe doesn't | 0, 2 |
| `flnd plugin list` | the list of 8.3 | 0 |
| `flnd plugin install <file\|folder>` | 6.6 | 0, 2, 4 |
| `flnd plugin uninstall <code>` | 6.6; a terminal asks first; a pipe doesn't | 0, 2 |
| `flnd extension install` / `uninstall` | 6.8 | 0, 2 |
| `flnd status` | the last run (command, start, length, saved, failed); failures grouped by target (10 shown, then "and N more"); accounts that are held, expired or expiring; the disk against the history. On a terminal with something to repair: "Repair N rows? (y/N)" runs `check(fix=True)` | 0 when all is well, else 1 |
| `flnd config` | the table of every key, its value, its default and where it comes from; `enter` edits a value in a form. On a pipe, the table | 0, 2 |
| `flnd host` | hidden: the extension's host | — |
| `flnd -h`/`--help`, `flnd <command> -h`/`--help`, `flnd -V`/`--version` | help; the version | 0 |

- **Targets on stdin.** When stdin isn't a terminal, its lines are added to the arguments.
  Blank lines and lines starting with `#` are skipped. With no arguments and an empty stdin
  (cron's `/dev/null`), `get` syncs every followed target.
- **The help.**
  - `flnd -h` fits in 24 lines, and `tests/test_cli.py` pins that.
  - `flnd get -h` adds one line per plugin: its code, its target forms and its categories,
    with own ones first.
  - The help is drawn by a Rich formatter over argparse, in the theme.
- **Errors** print as 8.2 shows. A `flnd.Error` never shows a traceback. Any other exception
  writes `<home>/log/crash-<stamp>.txt`, prints "flnd hit a bug: <one line>" with the hint
  "details in <path>", and exits 4.

### 8.2 The screen (`flnd/ui/`)

**The theme (`ui/theme.py`)** is the only place colours and glyphs are written:

| token | value | used for |
| --- | --- | --- |
| `text` | `#D0D0D0` | body text |
| `dim` | `#767676` | hints, keys, borders, zeros |
| `focus` | `#7D84D6` | the cursor, titles, bars, spinners |
| `good` | `#02BF87` | `✓`, answers |
| `bad` | `#ED567A` | `✗`, errors |
| `warn` | `#E8C468` | warnings, holds |
| glyphs | `•` `❯` `✓` `✗` `·` `●` `○` `★` `┃` | bullets, cursor, done, failed, waiting, followed, not followed, default, form bar |
| spinner | `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` at 10 fps | in progress |
| border | Rich `box.ROUNDED`, padding `(0, 1)` | panels and detail views |

- **Without colour:** `NO_COLOR`, a dumb terminal or a pipe turn colour off.
- **Without Unicode:** a terminal that isn't UTF-8 gets ASCII instead: `*`, `>`, `v`, `x`,
  `-`, `o`, `+`, `|`, and the spinner `|/-\`.

**Output (`ui/out.py`):**
- `out.line(text)`;
- `out.warn(text)`, which folds repeats: the same text within 10 s becomes "text (×n)";
- `out.error(error)`, which prints the message, then `hint` on its own line;
- `out.table(rows, columns)`.

No module but `ui/` writes to stdout or stderr.

**The run's screen (`ui/screen.py`).** The `flnd-screen` thread wakes every 100 ms and redraws
with Rich `Live(auto_refresh=False)` when `run.state.version` changed, and not otherwise.
Lines from other threads (warnings) go through its queue and print above the live block.

- **The header:** `flnd get · <n> targets · <mm:ss>`.
- **The table**, one row per target, holding:
  - a glyph by phase: `·` waiting, the spinner while listing or downloading, `✓` done,
    `✗` failed;
  - `target`, at most 28 cells, elided in the middle;
  - `categories`, at most 14 cells;
  - `saved`, `skipped` and `failed`, right-aligned, with `–` before any count;
  - `size`, and `speed` (`–` when idle).
- **The file rows** (`State.files`, at most 6):
  - the spinner, and the path under the target's folder, at most 24 cells, elided on the
    left;
  - a 30-cell bar (no bar when the size is unknown), the percentage, the size, and the speed.

  Then `+ <more> more` when `more > 0`. Queued files never show.
- **The `now` line,** `now  <State.now>`, in `warn` during a hold.
- **The foot:** `<speed> · <saved> saved · <failed> failed`, with the keys on the right:
  `ctrl+c stop · twice: stop now`.
- **On a short terminal,** when the height is under the rows plus 8, targets past the room
  fold into `+ <n> targets`.
- **On a pipe,** nothing is drawn while the run goes, and the end summary is printed.
- **The end summary:**
  - `✓` (`✗` when anything failed), then `done in <t> · <n> targets · <saved> saved (<size>)
    · <skipped> already saved · <failed> failed`;
  - each failure as `✗ <target> · <path>`, the message and the hint, at most 10, then
    `details  flnd status`.

**Lists (`ui/lists.py`, Textual, inline):**

| list | columns | extra keys |
| --- | --- | --- |
| targets | target, ●/○ followed, categories, files, size, last run | `f` follow or unfollow; `r` mark exhausted failures retryable (the next `get` tries them); `a` asks for a target and runs `get` on it |
| accounts | plugin, name, ★ default, origin, status (ok / held n min / expires in n days / expired), saved | `*` make default; `a` opens the login form |
| plugins | code, name, built-in or installed, API, domains, targets | `a` asks for a path and installs; `d` is shown dim and does nothing on a built-in |

- **Keys in every list:** `↑↓` move, `/` filters by text, `enter` opens a detail panel
  (`esc` closes it), `d` deletes after a confirmation, and `q` quits. The keys show in the
  bottom line.
- **On a pipe,** each list prints its columns as a plain table.

**Forms (`ui/forms.py`, Textual, inline, in Huh's look):**
- `select(title, options)`, `text(title, secret=False)` and `confirm(title)`, inside a bar
  `┃` with a title.
- **Keys:** `↑↓` choose, `enter` confirm, `esc` back. Ctrl-C raises `Stopped`.
- **Without a terminal,** `select` and `text` raise `UsageError` naming what was needed;
  `confirm` runs only on a terminal (`target clean`, `auth logout` and `plugin uninstall`
  proceed on a pipe without prompting).
- **Speed:** each list and form must open in 250 ms or less (`ui.open` benchmark). If Textual
  misses that, step 13 replaces it with a small list on Rich with raw key input, keeping the
  same keys and looks.

**What it looks like:**

```text
  flnd 0.2.0 · download whole accounts from the web

  get <targets…>            download the targets; one flnd has is synced; none: sync all
  target list               the targets flnd knows
  target clean <targets…>   forget targets; --media also deletes their files
  auth list                 every account          auth login <plugin>    log one in
  plugin list               the plugins            plugin install <path>  add one
  extension install         connect Chrome         status                 the last run
  config                    every setting

  plugins  fbk ins loi mht tik tlg twt web               a plugin's targets: flnd get -h
```

```text
  flnd get · 3 targets · 01:42

  target                     categories     saved  skipped  failed      size     speed
  ✓ ins:example_user         posts reels      412    1,208       0    2.1 GB         –
  ⠹ twt:example_creator      media             37       12       1    180 MB   31 MB/s
  · tik:example_handle       videos             –        –       –         –         –

  ⠹ media/1290_2.mp4         ━━━━━━━━━━━━━━━━━━━━╺━━━━━━━━━   64%    38 MB   12 MB/s
  ⠹ media/1287_1.mp4         ━━━━━━━╺━━━━━━━━━━━━━━━━━━━━━━   23%   210 MB   16 MB/s
    + 4 more

  now  twt:example_creator · media, page 4 · held 30 s: 429 from api.example.com (×3)

  42 MB/s · 449 saved · 1 failed                              ctrl+c stop · twice: stop now
```

```text
  ✓ done in 4m 12s · 3 targets · 1,021 saved (3.4 GB) · 1,220 already saved · 1 failed

  ✗ twt:example_creator · media/1290_2.mp4
    the site answered 404: the file is gone
    hint  nothing to do; flnd won't ask for it again

  details  flnd status
```

```text
  Targets · 3                                                          / filter

  ❯ ins:example_user        ● followed   posts reels    1,620 files    2.1 GB    2h ago
    twt:example_creator     ● followed   media             49 files    180 MB    2h ago
    tik:example_handle      ○            videos             0 files         –     never

  ↑↓ move · enter details · f follow · r retry · a add · d delete · / filter · q quit
```

```text
  Accounts · 3                                                         / filter

  ❯ ins   example_user       ★ default   browser   ok                 saved 3d ago
    ins   example_handle                 chrome    held 4 min         saved 1d ago
    twt   example_creator    ★ default   file      expires in 2 days  saved 20d ago

  ↑↓ move · * default · a add · d log out · / filter · q quit
```

```text
  ┃ Log in to Instagram
  ┃
  ┃ How?
  ┃ ❯ In a browser window      flnd opens Chromium; log in as usual
  ┃   From your Chrome         through the extension
  ┃   From a cookie file       a cookies.txt you exported
  ┃
  ┃ Account
  ┃ > example_user

  ↑↓ choose · enter confirm · esc back
```

```text
  ✗ ins:example_user needs a login
    hint  flnd auth login ins
```

### 8.3 The library (`flnd/_library.py`)

```python
flnd.open(home: str | Path | None = None) -> Flnd   # None: $FLND_HOME or ./.flnd
Flnd.home: Path
Flnd.close() -> None
Flnd.get(*targets: str | Target, limit: int | None = None, when: str | None = None,
         dry: bool = False) -> Run
Flnd.targets -> Targets     # iterate: Tracked rows; .list(*, plugin: str | None = None)
    .follow(*targets: str | Target) -> list[Tracked]
    .unfollow(*targets: str | Target) -> list[Tracked]
    .retry(*targets: str | Target) -> list[Tracked]
    .clean(*targets: str | Target, media: bool = False) -> list[Tracked]
Flnd.auth -> Auth           # iterate: Account rows
    .login(plugin: str, *, method: str | None = None, path: str | None = None,
           name: str | None = None) -> Account
    .logout(plugin: str, *, name: str | None = None) -> Account
    .default(plugin: str, name: str) -> Account
Flnd.plugins -> Plugins     # iterate: Installed rows
    .install(path: str | Path) -> Installed;  .uninstall(code: str) -> Installed
Flnd.extension -> Extension # .install(), .uninstall(), .connected: bool, .version: str | None
Flnd.config -> Config       # mapping "defaults.when" / "plugins.ins.pace" -> value;
                            # .set(key, value), .unset(key)
Flnd.status -> Status | None
Flnd.check(*, fix: bool = False) -> Checked
Flnd.files(*, target: str | Target | None = None, kind: str | None = None,
           since: date | str | None = None) -> list[Record]
Flnd.add(plugin: type[Plugin]) -> None                   # for this process only
Flnd.plugin(code: str) -> AbstractContextManager[Ctx]    # a Ctx for a script
Run: iterate (events) · async for · await · .state · .report · .wait() -> Report
     .stop() · .suspend() · .resume()
```

The events and rows are frozen msgspec structs (`Target` and `Account` are defined in
`flnd/plugin.py` and re-exported by `engine/events.py` and `flnd`):
- `Saved(target: Target, category: str, post: str, path: Path, date: date | None, size: int,
  retried: bool = False)`;
- `Skipped(target: Target, category: str, post: str, path: Path | None = None,
  reason: Literal["saved", "busy", "nothing new"] = "saved")`;
- `Failed(target: Target, error: Error, post: str = "", path: Path | None = None,
  category: str = "")`;
- `Planned(target: Target, post: str, path: Path | None, state: Literal["new", "saved"],
  category: str = "")`.
- `Report(saved: int = 0, skipped: int = 0, bytes: int = 0, planned: int = 0,
  failed: tuple[Failed, ...] = (), stopped: bool = False, retried: int = 0, resumed: int = 0,
  unchanged: int = 0)` with `@property def ok(self) -> bool`.
- `Account(id: int, plugin: str, name: str, origin: str, at: str, is_default: bool = False,
  status: Literal["ok", "held", "expired"] = "ok", held_until: str | None = None,
  expires: str | None = None)`.
- `Tracked(target: Target, follow: bool = False, at: str = "", files: int = 0, size: int = 0,
  seen: int = 0, categories: tuple[str, ...] = (), complete: dict[str, str] = …,
  query: bool = False, account: str | None = None)`.
- `Installed(code: str, name: str, origin: str, where: str = "", version: str = "",
  api: int = 2, domains: tuple[str, ...] = (), forms: tuple[str, ...] = (),
  categories: tuple[str, ...] = (), logins: tuple[str, ...] = (), disabled: str = "")`.
- `Record`, `Failure`, `Status` (with `Flnd.status` returning `None` when the home has never
  run) and `Checked` keep today's fields.

### 8.4 The journal

- **Where:** one file per command, `<home>/log/<UTC stamp>_<command>.jsonl`, deleted after 30
  days.
- **Each line** is one JSON object: `{"at": "<ISO UTC>", "type": "<Saved | Skipped | Failed |
  Planned | log | switch | hold | run>", …fields}`.
- **What goes in it:** every event, every log line at every level (debug included), every
  hold and every switch. The first record is `{"type": "run", "command", "version",
  "targets"}`, and the last is `{"type": "run", "report": {…}}`.
- **Secrets never go in:** `kit/text.redact` strips userinfo, queries and fragments from URLs
  (including URLs inside messages), and `kit/text.scrub` scrubs secret-looking keys and
  header values in dicts and text before writing.

## 9. Every feature, and where it goes

| today | where it goes |
| --- | --- |
| `download`, `follow`, `sync` | `get` (D4) |
| `unfollow`, `ls` | `target list` (`f`); `fl.targets.unfollow` |
| `retry` | every `get` retries failures first (up to 3 runs); `r` in `target list` and `fl.targets.retry(*targets)` reset `attempts` to `0` |
| `check [--fix]`, `status` | `status`, which offers the repair; `fl.check(fix=True)` |
| `login`, `logout`, `sources` | `auth login`, `auth logout`, `plugin list` |
| `--only`; other people's content | in the target: `code:value/reels,posts`, `code?query/all`, or on a discovering route (`ins:me/reels`); default: the plugin's own (`own=True`) |
| `--when` | `[defaults] when`; `when=` |
| `--limit`, `--dry` | `limit=` (posts per target, or targets per search), `dry=`; `make try` runs `fl.get(..., dry=True)` |
| `--exec` | `[defaults] exec` (D20) |
| `--again` | `target clean`, then `get` (on-disk files are recorded in `flnd.db` with 1 `stat` and skipped, not re-downloaded); `Engine._unrecorded` (auto-following orphan `media/` folders) is dropped so cleaned targets stay forgotten |
| `--json`, `--home`, `-v` | the journal, `$FLND_HOME`, the journal and crash files |
| stdin targets, exit codes, Ctrl-C in three stages | kept (8.1, 6.1) |
| smart sync, early stops, searches, discovered links | kept (6.3; E3 and E12 fixed) |
| lazy posts, refreshing an expired link | kept; `refresh` is a hook |
| dedupe, links across categories, unpack, sidecars, redating | kept, as jobs; hashing only on a size match |
| parts that lock themselves, mirrors, DASH, the content check | kept |
| `Source.user_agent`, `Route(shown=)`, `Post.replace`, `File.dash`, `Site.ask(description=)` | kept on `Plugin`, `@route`, `Post`, `File` and `Ctx.ask` |
| `Site.pause`, `Site.back_off`, `Site.storage` | `Ctx.pause(seconds, reason)`, `Ctx.hold(reason, seconds=)` alongside `Plugin.blocked(response)`, and `Ctx.storage(origin)` |
| segmented downloads (never on) | dynamic segments (6.2, P13) |
| AIMD caps, pace, 429 holds | the scheduler's pools; holds per bucket |
| per-site `proxy`, `pace`, `limits`, `stop_after` | `[plugins.<code>]`; `scroll` goes |
| `[defaults] dedupe`, `sidecars` | kept; `again` and `[vault] key` go |
| — | new: plugin `Settings`, `folder`/`filename`, `rate`, `slowest`, `height`, the free-space check |
| scrolling like a person, the scroll gate, warm tabs | kept; warm tabs come from `url=` |
| ins's `in_page` fetch | kept (`fetch(in_page=True)`) |
| expiry warnings, refreshed cookies written back | kept (6.7) |
| cookie files, Firefox, Safari, tlg's `browser`, `qr` and `phone` | kept |
| Chromium stores read with the keychain | `chrome`, through the extension (D24) |
| one login per source, in plaintext | several accounts, opt-in switching, a sealed vault |
| plugin folder, entry points, manifest | `plugin install` and `plugins.json`; entry points go |
| samples of unreadable answers, crash files | kept |
| the library, `Flnd.site(code)`, `flnd.testing` | kept (8.3: `Flnd.home`, `Flnd.close()`, `Flnd.targets.list(plugin=)`, `Run` `ResourceWarning`); `fl.plugin(code)`; `FakeCtx` |
| `FLND_TEST_NO_PROFILES` | kept in `Makefile` and `kit/cookies.py`, `kit/vault.py`, `kit/browser.py`, `engine/plugins.py`, `engine/extension.py`; isolates browser profiles, keychain and `~/.config/flnd/` in tests |
| `make try`, `dist`, `cdn-real` | kept; new `make live`, `make ext` |
| the Chrome extension (planned, never built) | built (6.8) |
| unknown links (refused) | the `web` plugin |

## 10. What other tools teach

| tool | taken | not taken |
| --- | --- | --- |
| yt-dlp | the lazy extractor manifest (`_manifest.py`, `plugins.json`); concurrent HLS/DASH fragments (`File.stream`); `--throttled-rate` (`slowest`); format sort (`height`, `best`); output templates (`folder`, `filename`); `--break-on-existing` (the head); curl_cffi impersonation; expected errors print one line and a hint; live tests by hand (`make live`) | post-processors that embed things, SponsorBlock, `--match-filter`, self-update |
| gallery-dl | a config table per extractor that overrides paths and pace; exec after a file; `skip = "abort:N"` (`stop_after`); `Queue` (`Target`) | filter expressions, `--range` (`limit=` covers it), zip and ugoira |
| IDM | dynamic segmentation; a browser button and a link grabber (the popup); refreshing an expired address; a speed limit; a free-space check | the queue timer (cron covers it); the site grabber (plugins do) |
| aria2 | splitting only above a minimum size; segments over several mirrors; restarting below a lowest speed; a control file to resume (the part's sidecar) | BitTorrent, Metalink |
| instaloader, twscrape | limits per endpoint (buckets); a pool of accounts switched on a limit; fast update (the head) | one tool per site |
| JDownloader | the link grabber (the popup); handing a captcha to a person (`handoff`) | reconnecting the router |

**`make live ARGS=<code>`** comes from yt-dlp's live tests. It is a hand check on the test
accounts, never run in `make check`:
- it runs each route of the plugin on the target listed in `~/.config/flnd/live.toml`, which
  stays outside the repo;
- it checks that at least one file was found per category, that every template field was
  filled, and that dates parsed;
- it downloads nothing (`dry=True`).

## 11. Bugs

Each bug gets a test that fails first, and is fixed in the step named, in the same commit as
that test. A row marked *suspected* is dropped, with a note in the commit message, if its
test passes on today's code.

| id | where | what fails | the test that shows it | step |
| --- | --- | --- | --- | --- |
| E1 | job.py:1713, :1571 | a full disk is swallowed; exit 1, not 4 | a fake `ENOSPC` on a part's write: the run raises `DiskError`, exit 4 | 5 |
| E2 | engine.py:229, :557, :611 | Ctrl-C leaves the `runs` row at 0 saved | stop after 3 saved: the row says 3 | 10 |
| E3 | engine.py:1098 + job.py:2511 | a search sync never gets past its first 25 | a search of 60: three syncs reach all 60 | 6 |
| E4 | engine.py:813, :847 | a mixed run loses the categories and the stop for followed targets | a search and a user with categories: both stored | 10 |
| E5 | _track.py:87 | one `Lock` shared by every track, which emits while holding it | two tracks have different locks; a full channel doesn't block another file | 5 |
| E6 | _track.py:96 + _transfer.py:344 | retrying a merge raises `ValueError` | ffmpeg fails once, retryably: the file saves | 5 |
| E7 | _transfer.py:359 | empty audio fails 3 times | empty audio: the video is saved alone | 5 |
| E8 | flncore/log.py:315-324, :335 | the journal writes `Path` and errors as their repr | a journaled `Saved` decodes with a string `path` | 1 |
| E9 | flncore/log.py:704 | journal lines from four threads interleave | 4 threads × 1,000 writes: 4,000 whole lines | 1 |
| E10 | _run.py:365, :433 | the last `Saved` events are lost behind END | 100 events from the loop at the end: the reader gets all 100 | 6 |
| E11 | _run.py:385 | the overflow queue has no bound | a reader that doesn't read: at most 256 events queued | 6 |
| E12 | job.py:3080 | a link to another source runs on the parent's `SourceRun` | an ins post yielding a twt target: twt runs it | 6 |
| E13 | _steps.py:436 + job.py:1378 | tracks stay UNPACKING; each target overwrites `tracks` | two targets with an archive each: both `Saved` | 5 |
| E14 | _events.py:427 + _check.py:67 | `check --fix` is ok while unknown files remain | an unknown file after `fix`: not ok, exit 1 | 10 |
| E15 | job.py:1582, :1639 | a `DiskError` in `_commit` is lost | a commit raising `DiskError`: the run stops, exit 4 | 5 |
| E16 | engine.py:1307 | `except BaseException` swallows errors from `connect`'s exit | `connect`'s exit raises: the run reports it | 6 |
| E17 | job.py:524, :445 | board labels are set outside their lock | gone with the board; the screen reads `State` only | 4 |
| E18 | job.py:1623, :1900 | SQLite on the loop blocks every transfer, for up to 30 s | another connection holds the write lock: the loop's longest stall is ≤ 100 ms | 5 |
| E19 | engine.py:1104 | `skipped` adds targets and files together | a sync with 2 seen targets and 3 skipped files: skipped = 3 | 10 |
| E20 | flncore/log.py:566, :729 | the ticker never stops; a log line forces a redraw; widths in `len()` | an idle screen writes 0 bytes; a wide CJK label fits its cells | 4 |
| S1 | tik.py:664 | a 404 on `tik:<id>` counts as done | a fake 404: `Failed` with `NotFound`, recorded gone | 8 |
| S2 | twt.py:305, :369 | a deleted tweet or a 429 counts as done | 404: gone; 429: a hold, then retried | 8 |
| S3 | ins.py:245, :886 | a post with no media isn't `NotFound`; a page per post while rate-limited | no media: `NotFound`; while `Blocked`: no page opens | 8 |
| S4 | twt.py:509, fbk.py:474, :728, :790 | a tab that fails to load records the listing complete | `open` raises: the target fails, nothing is recorded complete | 8 |
| S5 | site.py:448 | `open` drops the status | a 404 page: `NotFound` | 7 |
| S6 | site.py:979 | a 429 while resuming throws away the saved cursor | 429 on a resumed page: the cursor is kept | 7 |
| S7 | _plugins.py:343, :356 | a prefix test without a separator | a plugin `flnd_example` that fails: the error names its own file and line | 9 |
| S8 | loi.py:167, :243 | a re-run rewrites `info.md` without links | two runs: the links stay | 8 |
| S9 | ins.py:1351-1352 | the progressive MP4 is joined with the DASH audio | the DASH video fails: the MP4 saves alone (audio on `mirrors[0]` only) | 8 |
| S10 | tik.py:245, :280 | `stories` alone opens a feed nobody reads | `/stories`: no post-list feed opens | 8 |
| S11 | _cookies.py:163, _net.py:10 | an import patches kit classes for the whole process | importing `kit/cookies` leaves `kit/browser`'s class unchanged | 2 |
| S12 | testing.py:178, :249, :438, :453 | the fake never blocks, always opens, has no 401 or storage | 5.4's behaviours, one test each | 7 |
| S13 | ins.py:215 | `self.api` shadows the API version | gone: the client is `self.client` | 8 |
| S14 | _home.py:1632-1639 | a dot is added to host-only `__Host-` cookies | a `__Host-` cookie keeps a domain with no dot | 11 |
| S15 | flncore/dig.py:51-54 | a malformed path raises `Changed` | `dig(x, "a[")` raises `SourceError` (renamed `PluginError` in step 7) | 2 |
| S16 | _vault.py:19 and others | the docs say "encrypted"; the vault is plaintext | `secret.db` holds no plaintext value after a login | 11 |
| K1 | flncore/files.py:985 | `filename` keeps `\` | `filename("a\\b") == "a_b"` | 1 |
| K2 | flncore/__init__.py:47 | `flncore.dig` and `.log` give back modules | gone with the package; `kit/dig.dig` is a function | 2 |
| K3 | _db.py:282, :304 | reads after `close()` return no row | a read after `close()` raises | 1 |
| K4 | _home.py:1127 | two samples saved at once get the same name | 2 threads × 50 samples: 100 files | 1 |
| K5 | _home.py:897 | `date.today()` is local time | under a fake clock at 23:30 UTC−5, the stamp is the UTC day | 1 |
| K6 | flncore/clock.py:159 | `day()` ignores the fake clock | `day()` under `fake_clock` gives the fake day | 1 |
| K7 | _transfer.py:324 | DASH parts are opened on the loop | a loop stall check: no `open` on the loop thread | 5 |
| K8 | flncore/media.py, run.py, cli.py | broken dead modules | deleted | 2 |
| K9 | flncore/net.py:1484-1491 | one hasher thread for the whole process | 4 files hashed at once use 4 threads | 5 |

## 12. Speed-ups and benchmarks

Each speed-up is measured before and after with `make bench`. Both sets of numbers go in the
commit, and `--gate` stays green. Step 0 adds every new benchmark and re-records every
baseline on a quiet machine. That `tests/bench/gates.json` record is the "before" of every
row: a number quoted below was recorded at 1e01feb, and only shows the scale.

| id | change | measured by | before | goal | step |
| --- | --- | --- | ---: | ---: | --- |
| P1 | per-request CPU: a cookie header cached per domain and dropped on `Set-Cookie`; no `urllib.Request` per request; no cookie copy in `prepare`; then reused curl handles | `net.small_8_at_once`, `net.small_1_at_once` | 617, 871 ms | ≤ 300 ms | 3 |
| P2 | hash only on a size match | `net.big_rate`, `e2e.*` | 2.40 GiB/s | ≥ 4 GiB/s | 5 |
| P3 | no listing thread | `cpu.loop_bridge`, `e2e.latency_500_posts` | 40 µs, 4.85 s | removed, ≤ 4.0 s | 6 |
| P4 | targets at once | `e2e.sync_100_targets` | step 0 | ≤ 1/4 | 6 |
| P5 | warm tabs opened together, cookies in one call, no unread bodies | `browser.open_tab`, `browser.warm_4` | step 0 | warm_4 ≤ 1/3 | 6 |
| P6 | one writer per device | `net.two_disks` | step 0 | the fast disk within 10 % of alone | 5 |
| P7 | the screen from a snapshot | `ui.overhead`, `ui.frame`, `ui.idle_bytes` | step 0 | ≤ 3 %, ≤ 1 ms, 0 | 4 |
| P8 | one `stat` per check that a file is on disk | `test_fs` counts | 2–3 | 1 | 6 |
| P9 | a commit never blocks the loop | `e2e.db_contended` | step 0 | stall ≤ 100 ms | 5 |
| P10 | progress as a snapshot | `cpu.advance_*`, `e2e.per_file` | step 0 | lower | 4 |
| P11 | styled help stays fast | `startup.help`, `startup.import_flnd_plugin` | step 0 (`startup.import_flnd_source` for the second) | ≤ 60 ms, no worse than step 0's `import_flnd_source` | 7 |
| P12 | slower since the last rebuild | `db.insert`, `db.prefix_scan_10k` | 2.3 µs, 15.6 ms | ≤ 2.0 µs, ≤ 12 ms | 10 |
| P13 | dynamic segments | `net.per_conn_limit` | step 0 | ≥ 4× | 5 |
| P14 | a hasher pool | `net.big_4_at_once` | step 0 | ≥ 2.5× | 5 |
| P15 | transfer lanes, only if P1 leaves the loop CPU-bound | `net.small_8_at_once` with 1, 2 and 4 lanes | after P1 | ≤ 1/2 at 4 lanes, or the step is dropped | 15 |
| P16 | HLS segments at once | `net.hls_200` | step 0 | ≥ 6× serial | 14 |
| P17 | lists and forms open fast | `ui.open` | step 0 | ≤ 250 ms | 13 |

**The new benchmarks** (added in step 0, each in its group's script, each with a `--smoke`
size). Step 0 adds `"ui"` to `GROUPS` in `tests/bench/run.py` and makes `--gate` treat
`baseline == 0` as failing when `value > 0` without dividing by zero:
- `ui.overhead` (`%`, lower): 100,000 fake file events through a `Run`, screen on versus off,
  as percentage added.
- `ui.frame` (`ms`, lower): one draw of a 20-target, 6-file screen (times `flncore/log.py` at
  step 0; `ui/screen.py` in step 4).
- `ui.idle_bytes` (`B`, lower): bytes written in 5 s while nothing changes (times
  `flncore/log.py` at step 0; `0 B` in step 4).
- `ui.open` (`ms`, lower): subprocess start plus `import textual` at step 0; from process
  start to a list's first frame in step 13 (goal ≤ 250 ms).
- `e2e.sync_100_targets` (`s`, lower): 100 followed targets of an HTTP-only plugin with
  nothing new, 150 ms a page, through the real engine on the network fake (goal ≤ 1/4 of
  step 0's time).
- `e2e.db_contended` (`ms`, lower): a run of 500 files while another process holds `flnd.db`'s
  write lock for 2 s; the loop's longest stall (goal ≤ 100 ms).
- `net.two_disks` (`%`, lower): overhead on the fast target's time when a second target writes
  to a disk throttled to 20 MB/s vs alone (goal ≤ 10 %).
- `net.per_conn_limit` (`x`, higher): throughput ratio for 1 GiB from a loopback server capping
  each connection at 20 MB/s vs one connection (`1.0x` at step 0; goal ≥ `4.0x` in step 5).
- `net.big_4_at_once` (`x`, higher): throughput ratio for four 256 MiB files with dedupe
  hashing forced vs one (`1.0x` at step 0; goal ≥ `2.5x` in step 5).
- `net.hls_200` (`x`, higher): speed-up ratio for a 200-segment playlist at 40 ms latency
  each vs fetching serially (`1.0x` at step 0; goal ≥ `6.0x` in step 14).
- `browser.warm_4` (`s`, lower): opening 4 warm tabs beside one target's own tab, the most
  §6.2's budget allows (serial at step 0; goal ≤ 1/3 of step 0 in step 6).

## 13. Conventions (the new AGENTS.md)

Step 0 rewrites AGENTS.md to exactly these sections.

- **Commits:**
  - `<action>:<summary>`, with the actions `feat`, `fix`, `perf`, `refactor`, `docs`,
    `test`, `chore` and `style`; a lowercase, imperative summary with no period;
  - no co-author trailer;
  - signed as feiluvnana with SSH, through the repo-local config; never touch the global
    config;
  - one logical change per commit;
  - `git pull --rebase` first, and commit only your own paths (`git commit -- <paths>`);
  - push only when the user said to (D26).
- **Checks:**
  - `make check` (ruff, then the tests) is green at every commit. Ruff is the one checker
    (D27): `select = ["F", "E9", "B", "UP", "I", "ASYNC", "RUF006"]` in `pyproject.toml`.
    There is no type checker; type hints are for readers. `make test` runs Python with
    `-W "error:coroutine:RuntimeWarning"`, so a never-awaited coroutine fails its test;
  - `make api` after any public change, and its diff is the review;
  - `make bench ARGS=--gate` for anything touching speed;
  - `make check PY=3.11` before a release.
- **Privacy, unchanged:**
  - never read, print or commit `*_cookies.txt` or `.flnd/`;
  - never pass site text to a shell;
  - no secret in a log, an error, a comment, a doc or a test;
  - only example handles (`example_user`, `example_creator`, `example_handle`,
    `example.page`, `examplechannel`, `example.com`);
  - live checks only by hand, on the test accounts; unit tests fake the network, the
    browser and the clock, and `FLND_TEST_NO_PROFILES=1` (set by `make test`) refuses real
    browser profiles, the real OS keychain and `~/.config/flnd/`.
- **Layout:** section 4.1 and its import rules. `kit/` never imports `engine/`, `ui/` or
  `plugins/`, and `ui/` never imports `plugins/`.
- **Concurrency:**
  - one loop per run;
  - plugin code is async;
  - only `engine/jobs.py` (and `ctx.pause` on the run's clock) retries, sleeps, limits, or
    picks threads or processes;
  - a commit is one per post and on the loop, and never blocks it.
- **Output:**
  - everything printed goes through `flnd/ui` and its theme;
  - no `print`, and no colour or glyph outside `theme.py`;
  - debug goes to the journal only.
- **Flags:** a flag only when the target, the config and the library are all wrong for the
  choice.
- **Naming:**
  - short names;
  - an action is a verb (no `get_`, `to_`, `try_`);
  - a read is a property or a method with keyword filters;
  - one word, one meaning ("plugin", "retry" as the verb, `hint` as the advice, "hold" for
    a timed wait, `Record` for a saved file's row);
  - at most 5 keyword-only options;
  - a resource is a context manager;
  - no aliases.

  `tests/test_names.py` checks the public surface, and its allowlist may only shrink.
- **Errors:** 5.1. A user error never shows a traceback. `Stopped` derives from
  `KeyboardInterrupt`.
- **Bugs:** the failing test comes first, in the same commit as its fix.
- **Speed:**
  - a benchmark before the change it measures;
  - a `perf:` commit quotes both runs;
  - a change that makes a gated number worse says why;
  - never claim a speed-up you didn't measure;
  - the counting guards in `make check` (requests, `stat` calls, SQL statements, imported
    modules) stay.
- **Dead code:** the dead-name, docstring and API-snapshot checks cover all of `flnd/`.
- **Dependencies:** each one has a reason beside it in `pyproject.toml`. After this plan
  they are curl_cffi, playwright, selectolax, cryptography, telethon, cryptg, qrcode, rich,
  msgspec, tomlkit and textual (kept only if `ui.open ≤ 250 ms` in step 13).
- **Writing:**
  - a module docstring of 1–3 lines naming its `flnd/docs` page;
  - a public name gets a summary, then `Args`, `Returns` and `Raises` only when they add
    something;
  - comments cover only what the code can't say;
  - a behaviour change updates its `flnd/docs` page in the same commit;
  - lines of at most 100 characters;
  - plain English.
- **Models:** mechanical work (moves, renames, call sites, snapshots, doc edits that follow a
  decision) goes to the low-cost model (`model: "haiku"`). Design, new behaviour, debugging,
  security-sensitive code, reviews and new docs pages use the default model.

## 14. The steps

Each step lists what changes, the tests it adds, the benchmarks it runs, when it is done, and
its commits.

**Every step is done only when:**
- `make check` is green;
- `make bench ARGS=--gate` is green, if the step touches speed;
- `make api` was run and its diff matches the step;
- the line count is reported in the last commit's message;
- no temporary module of 4.2 that this step replaces is left.

### Step 0. Baseline and rules
- **Changes:**
  - add `textual` to `pyproject.toml`, and `"ui"` in `GROUPS` plus `baseline == 0` handling
    to `tests/bench/run.py`;
  - add the new benchmarks of section 12 (keeping `startup.import_flnd_source` until step 7
    renames it to `startup.import_flnd_plugin`), and run `make bench ARGS=--record` at HEAD;
  - rewrite AGENTS.md to section 13.
- **Done when:** `tests/bench/gates.json` holds every existing metric and every new
  benchmark of section 12, and AGENTS.md matches section 13.
- **Commits:** `test:benchmark the screen, many targets, a held database, disks and segments`,
  then `docs:rewrite the agent guidelines for the engine and its plugins`.

### Step 1. Kit fixes
- **Changes:** E8, E9, K1 and K3–K6, each in the module where it lives today.
- **Tests:** each bug's test from section 11, in that module's test file.
- **Commits:** one `fix:` per bug, for example `fix:write paths and errors to the journal as
  data`.

### Step 2. One package (haiku for the moves)
- **Changes:**
  - move the modules as 4.2 says for step 2, with `git mv` (keeping `flnd/sources/__init__.py`
    in `flnd/sources/` until step 8);
  - fix the imports, and delete the nine shims (S11: the parser and the jar's error type
    become arguments);
  - delete `flncore/config.py`, `run.py`, `cli.py`, `testing.py` and `__init__.py` (K2, K8);
  - fix `kit/dig.py` to raise `SourceError` on a malformed path (S15, renamed `PluginError`
    in step 7);
  - rewrite `kit/media.py` from `_transfer._dash_merge`;
  - delete:
    - `_Segments` and `Client(segments=)`;
    - `net.Element` and `Document`;
    - the live-stats leftovers (`_Line.posts/streak/how/listing`, `Board.seen`, `Board.row`,
      `Chain.jobs`, `_Lane.running`, `SourceRun.load`, `_Row.host/resumed`, `UI._live_*`,
      `Item.raw/part`);
    - `options.selected`, `Phase.HELD` and `LINKED`;
    - `Limits.holding` and `_held` (removing the `with self.limits.holding(...)` block at
      `net.py:1238` and the stale `.pauses` mentions at `net.py:12` and `site.py:247`);
  - merge the duplicates: `redact`, `scrub`, `size`, `duration` and `count` go to
    `kit/text.py`; `filename` stays one copy; `disk()` stays one copy; `with_query` stays
    one copy;
  - remove cyclopts from `pyproject.toml`;
  - one checker (D27): remove `pyright` from the dev group, `[tool.pyright]` from
    `pyproject.toml`, and the `type` recipe from the `Makefile`; add `ASYNC` and `RUF006` to
    ruff's `select` (fixing what they find); add `-W "error:coroutine:RuntimeWarning"` to the
    `test` recipe;
  - update `Makefile` (`lint`, `dist`), `pyproject.toml` (`packages = ["flnd"]`),
    `tests/api_snapshot.py` and `tests/test_api.py` (drop `CORE_API_MODULES` and
    `tests/data/flncore-api.txt`); delete `tests/test_core_cli.py`,
    `tests/data/flncore-help.txt` and `flnd/docs/flncore.md` (and its link in
    `flnd/docs/README.md`); update `flnd/docs/writing-a-source.md` (`from flnd.kit import
    html`);
  - widen `test_layers` to 4.1's rules (keeping temporary API-1 exemptions for
    `flnd/source.py` and `flnd/sources/*` until step 8), `test_names`' dead-name check to
    all of `flnd/`, `api_snapshot` (`PROJECT_PACKAGES = ("flnd",)`), and `test_docs`'
    docstring check to match section 13;
  - the vault's sealing code stays, for step 11.
- **Bench:** `startup.*`.
- **Done when:** `flncore/` is gone, `flnd/kit/` and `flnd/engine/` exist with 4.2's
  temporary modules, and nothing imports a shim.
- **Commits:**
  - `refactor:move the kit into flnd/kit`;
  - `refactor:move the engine side into flnd/engine`;
  - `refactor:delete the kit's dead modules and code`;
  - `refactor:check layers, dead names and docstrings across the whole package`.

### Step 3. Theme and output
- **Changes:**
  - `ui/theme.py` and `ui/out.py` (8.2);
  - the help formatter, with the help pinned to 24 lines;
  - every line `cli.py` prints goes through `ui/out.py`;
  - P1 in `kit/net.py`: a cookie header per domain, cached and dropped on `Set-Cookie`; no
    `urllib.Request` per request; `prepare` no longer copies cookies; then reused curl
    handles if the goal isn't met.
- **Tests:** `tests/test_ui.py` (the theme's tokens, folded warnings, plain output on a pipe,
  ASCII output), and the help's length.
- **Bench:** `startup.help` (gate for P11), and `net.small_*` (P1).
- **Commits:**
  - `feat:draw every line flnd prints from one theme`;
  - `perf:build the cookie header once per domain` (with its numbers);
  - `perf:reuse curl handles across requests`, if it is needed.

### Step 4. The run's screen
- **Changes:**
  - `engine/events.py` gets `State`, `TargetState`, `FileState` and `HoldState`;
  - the job module publishes `State` every 100 ms;
  - `ui/screen.py` (8.2) with its `flnd-screen` thread;
  - `Progress` and `job.Board` are deleted, along with `flncore/log.py`'s renderer and its
    60 Hz ticker;
  - the CLI draws the screen and prints the end summary.
- **Tests:** `tests/test_screen.py`, using pyte for rows, elision by cell width, the file rows
  capped at 6, an idle screen writing 0 bytes, and the summary on a pipe. E17 and E20.
- **Bench:** `ui.*` (P7), `cpu.advance_*`, `e2e.per_file` (P10).
- **Commits:** `feat:show the run as a summary drawn from a snapshot`, then `refactor:remove
  progress events and the board`.

### Step 5. The scheduler
- **Changes:**
  - `engine/jobs.py` holds `Job`, `Kind` and the pools of 6.2: `Cap`, `Limits` and `Bucket`
    moved from `kit/net.py`, the tab budget and the scroll gate from `kit/browser.py`, and
    the lanes and writers;
  - the retry table, the holds with buckets, the segments, `rate`, `slowest`, the free-space
    check, and the hasher pool;
  - `engine/save.py` (6.4) replaces `Downloads` and the downloading half of `job.py`, and
    `track.py`, `transfer.py` and `steps.py`, which are deleted;
  - dedupe by size;
  - `exec`.
- **Tests:**
  - `tests/test_jobs.py`: each pool's numbers, priorities, every row of the retry table,
    holds per bucket, segments splitting and resuming from the sidecar, `rate`, `slowest`,
    the free-space stop;
  - `tests/test_save.py`: the save's 9 stages, dedupe by size, the commit on `SQLITE_BUSY`;
  - E1, E5, E6, E7, E13, E15, E18, K7, K9.
- **Bench:** `net.*` (P2, P6, P13, P14), `e2e.*` (P9).
- **Commits:**
  - `feat:schedule every request as a job`;
  - `refactor:retry in one policy`;
  - `feat:save files as jobs`;
  - `perf:hash only files whose size matches a saved one`;
  - `perf:split big files into segments while the host has room`;
  - `perf:write each device on its own thread`;
  - `perf:hash on one thread per core`;
  - `feat:limit the run's speed and check free space first`;
  - `feat:run a program after each saved file`.

### Step 6. The async engine
- **Changes:**
  - `engine/crawl.py` (6.3) runs listings as coroutines on the loop (fixing E3 and E12), and
    checks on-disk files in `Ledger` with one `dest.stat()` (P8);
  - `engine/run.py` (6.1) owns the loop, the event channel and `Run`'s unstarted
    `ResourceWarning`;
  - the worker thread, `kit/loop.py`, the loop bridge and `_Refresh` are deleted;
  - API-1 sources run through `engine/site.py`'s temporary bridge, which runs a sync source's
    route on a `CALL` thread and turns each `site.<verb>` into a scheduler job;
  - active targets are bounded and run at once (6.2);
  - the browser's warm tabs open together, cookies are added in one call, and bodies are
    read only for an open feed (P5).
- **Tests:** `tests/test_crawl.py` (smart sync, early stops, gaps, searches),
  `tests/test_run.py` (the channel, `END` last, Ctrl-C stages, suspend) and a `stat`-count
  guard for `Ledger` in `tests/test_fs.py` (P8). E3, E10, E11, E12, E16.
- **Bench:** `cpu.loop_bridge` (gone), `e2e.latency_500_posts` (P3), `e2e.sync_100_targets`
  (P4), `browser.*` (P5), `test_fs` counts (P8).
- **Commits:**
  - `feat:list targets on the run's loop`;
  - `perf:list targets at once`;
  - `perf:open warm tabs together and skip unread bodies`.
- **Checkpoint:** the orchestrator stops after step 6 and reports to the user before starting
  step 7 (plugin API 2).

### Step 7. Plugin API 2
- **Changes:**
  - `flnd/plugin.py` (5.3), with `Hidden` and `PluginError` added to `flnd/errors.py`
    (`SourceError` renamed); `Target` and `Account` live in `flnd/plugin.py`;
  - `engine/` runs `api = 2` plugins natively;
  - `flnd/testing.py` gets `FakeCtx` (5.4);
  - `open` returns `Landed` and raises `NotFound` and `LoginError` (S5);
  - `pages` keeps the cursor on a 429 (S6);
  - update `pyproject.toml` (`extend-immutable-calls` adds `flnd.plugin.Range`),
    `tests/test_names.py` (`registered` recognizes `@route`), `tests/api_snapshot.py`
    (`API_MODULES` includes `flnd.plugin`), and `tests/bench/startup.py`
    (`startup.import_flnd_plugin`, P11).
- **Tests:** `tests/test_plugin.py` (every declaration check, routes, categories, hooks,
  helpers) and `tests/test_testing.py`. S5, S6, S12.
- **Bench:** `startup.import_flnd_plugin` (P11).
- **Done when:** `tests/example_source.py` is rewritten as `tests/example_plugin.py` on API 2
  and passes through the real engine.
- **Commits:** `feat:add plugin api 2`, then `feat:make the test kit behave like the engine`.

### Step 8. The plugins on API 2
- **Order,** one commit each: `mht` (adds `mht:author/{author}` and `mht:genre/{genre}`),
  `loi` (S8), `tlg` (its part code goes to `Fetch`), `tik` (S1, S10), `twt` (S2, S4),
  `fbk` (S4), `ins` (S3, S9, S13).
- **Each port:**
  1. moves `flnd/sources/<code>.py` to `flnd/plugins/<code>.py`;
  2. becomes async;
  3. replaces its copied helpers with 5.3's (`meta`, `page_info`, `numbered`, `best`,
     `load`);
  4. deletes its back-off loops for `blocked` and `ctx.hold`;
  5. declares `walls`, `browser`, `own` and `url`;
  6. raises `Hidden` where its site hides content from an account;
  7. moves its tests to `tests/test_<code>.py` on `FakeCtx`;
  8. quotes its lines before and after.

  While porting one plugin per commit, the registry and `tests/source_manifest.py` read from
  both `flnd/sources/` and `flnd/plugins/`.
- **Then:** delete `flnd/source.py`, `engine/site.py`, the bridge, `flnd/sources/` and
  `test_layers.py`'s temporary API-1 exemptions; update imports in `engine/*`,
  `flnd/_library.py`, `flnd/cli.py`, `Makefile` (`docs`), `pyproject.toml` (drop
  `flnd.source.Range`), `tests/api_snapshot.py`, `tests/source_manifest.py` and
  `tests/test_docs.py` (`SourcePageTest`, `TERMS`); rename `flnd/docs/sources/` to `plugins/`
  and `writing-a-source.md` to `writing-a-plugin.md` (with `flnd.plugin` blocks), and
  regenerate the manifest.
- **Commits:** `refactor:port <name> to plugin api 2`, then `refactor:remove source api 1`.

### Step 9. The plugin manager
- **Changes:** `engine/plugins.py` (6.6): the folder, `plugins.json`, `install`,
  `uninstall`, loading, the API range, disabling, S7. Entry points go.
- **Tests:** `tests/test_plugins.py`:
  - install a file and a package;
  - replace one; refuse a built-in; refuse `.flnd` and `*_cookies.txt`;
  - an edited file is checked again;
  - a file without an entry is ignored;
  - API 1 and API 3 are disabled with their hints;
  - resolution imports no plugin.
- **Commit:** `feat:install and uninstall plugins`.

### Step 10. Targets and the store
- **Changes:**
  - `engine/resolve.py` (6.5);
  - `engine/store.py` holds schema 2 and its migration (7.1), and `check` and repair from
    `engine/check.py`;
  - `engine/config.py` (7.2), with the templates;
  - `engine/options.py` and `engine/check.py` are deleted, and `flnd.options.Options` is
    dropped from `pyproject.toml` (auth and vault code in `engine/home.py` and
    `engine/core.py` stays until step 11).
- **Tests:** `tests/test_resolve.py`, `tests/test_store.py` (the migration from a version-1
  home) and `tests/test_config.py`. E2, E4, E14, E19.
- **Bench:** `db.*` (P12).
- **Commits:**
  - `feat:read categories from the target`;
  - `feat:move flnd.db to schema 2`;
  - `feat:let config override a plugin's templates`.

### Step 11. The auth manager
- **Changes:**
  - `engine/auth.py` (6.7): accounts, the default, the methods (`browser` is new), expiry,
    write-back, logout, switching;
  - the sealed vault with `~/.config/flnd/key` (deleting the OS keychain code from
    `kit/vault.py`), and the migration of `secret.db`;
  - delete `engine/home.py` and `engine/core.py`.
- **Tests:** `tests/test_auth.py`:
  - each method;
  - for each trigger of switching, two faked accounts;
  - every account held;
  - the account that worked is kept;
  - the key in `~/.config/flnd/key`, wrong and missing;
  - plaintext is sealed on first open;
  - no plaintext in `secret.db`.

  S14, S16.
- **Commits:**
  - `feat:keep several accounts per plugin`;
  - `feat:log in through a browser window`;
  - `feat:switch accounts when one is blocked or can't see a target`;
  - `feat:seal secrets with a key kept outside the home`.

### Step 12. The extension
- **Changes:**
  - `flnd/extension/` and `engine/extension.py` (6.8): the host, the relay, the ops, the
    `chrome` method, the Chrome backend for `Ctx`'s browser verbs, the popup;
  - `make ext` in the Makefile;
  - then delete the keychain readers for Chrome, Edge, Brave and Chromium in
    `kit/cookies.py`.
- **Tests:** `tests/test_extension.py`, the protocol against a fake extension: every op,
  chunked answers, a second run sharing the host, a refused origin.
- **Commits:**
  - `feat:add the chrome extension and its host`;
  - `feat:log in and browse through the extension`;
  - `refactor:remove the keychain readers of chromium cookie stores`.

### Step 13. The commands
- **Changes:**
  - `flnd/cli.py` on 8.1's tree;
  - `flnd/_library.py` on 8.3;
  - choose Textual inline (8.2) or Rich with raw key input from step 0's `ui.open` benchmark
    (≤ 250 ms, P17) before writing `ui/lists.py` and `ui/forms.py`;
  - `status` with its repair;
  - `config` with tomlkit;
  - stdin;
  - the exit codes;
  - remove questionary (and textual if Rich was chosen), add tomlkit;
  - update `Makefile`'s `try` recipe (`python -c` calling `fl.get(..., dry=True)` in `.try`),
    `flnd/docs/cli.md`, `flnd/docs/library.md` and `README.md` so `tests/test_docs.py` stays
    green.
- **Tests:**
  - `tests/test_cli.py`: every command, on a pipe and on a pyte terminal; the exit codes;
    stdin; the help's length;
  - `tests/test_lists.py` and `tests/test_forms.py`, with Textual's `run_test` pilot (or raw
    terminal input if Rich fallback was chosen).
- **Bench:** `startup.help`, `ui.open` (P17).
- **Commits:**
  - `feat:group the commands into get, target, auth, plugin, extension, status and config`;
  - `feat:work in the target, auth and plugin lists`;
  - `feat:ask with forms in the style of huh`.

### Step 14. The web plugin and HLS
- **Changes:**
  - `flnd/plugins/web.py`: direct file links; HLS (`.m3u8`) and DASH (`.mpd`) through
    `File.stream`; a page's `og:video`, `og:image` and `<video src>`; the popup's media;
  - add `flnd/docs/plugins/web.md` and link it in `flnd/docs/README.md`;
  - `make live` (section 10).
- **Tests:** `tests/test_web.py`.
- **Bench:** `net.hls_200` (P16).
- **Commits:** `feat:download any link with the web plugin`, then `test:add the hand live
  check`.

### Step 15. Transfer lanes (only if P1 left the loop CPU-bound)
- **When:** after step 3, compare `net.small_8_at_once` against its goal. If it is ≤ 300 ms,
  this step is skipped, and a note in step 16's commit says so.
- **Changes:** `engine/jobs.py` gets N lane processes (`multiprocessing` with spawn), each
  with its own loop and curl session. The scheduler sends each a `FILE` job's URL, headers,
  cookies and part path, and gets back bytes and the result. N is `os.cpu_count() // 2`, at
  most 4.
- **Bench:** P15. The step is kept only if it reaches its goal; otherwise it is reverted.
- **Commit:** `perf:run file transfers in lane processes`.

### Step 16. Docs and size
- **Changes:**
  - rewrite `flnd/docs/`: `architecture.md` (sections 3–7 of this plan, as built),
    `extension.md`, `errors.md`, `storage.md`, `performance.md` and `glossary.md`, and
    polish `cli.md`, `library.md`, `writing-a-plugin.md` and `plugins/<code>.md`;
  - polish the README;
  - check that no temporary module is left;
  - report the line count against 24,000;
  - run `make bench ARGS=--record`.
- **Done when:** `tests/test_docs.py` is green on the new pages.
- **Commits:** `docs:describe flnd as built`, then `chore:record the benchmarks`.

## 15. How the plan is carried out

One session is the **orchestrator**. It writes no implementation code. For each step, in
order:
1. **Brief a builder subagent** with the step's section, the contracts it touches (by
   section number), the bugs and speed-ups it owns, the rules of section 13, and the commit
   messages. Mechanical parts (step 2's moves, renames, snapshot updates) go to
   `model: "haiku"`; everything else uses the default model.
2. **Verify** what the builder returns:
   - `git diff` matches the step and nothing else;
   - `make check` is green;
   - `make bench ARGS=--gate` is green when the step touches speed, with the numbers in the
     commit messages;
   - the `make api` diff matches the step;
   - each bug of the step has its test, and the test failed before the fix (the builder
     shows the failing run);
   - the step's "done when" holds.

   A finding goes back to the same builder (`SendMessage`), until the step passes.
3. **Commit** the step's commits as section 14 names them, with only that step's paths.
4. **Before the first push,** ask the user whether to push after each step (D26).
5. **Keep a progress note** in memory: the step number, the last commit, and anything open.
6. **Stop and ask the user** after step 6 before starting step 7 (plugin API 2), and
   whenever this plan doesn't answer a question, when a goal in section 12 can't be met
   without dropping a feature, or when a step would delete user data.
