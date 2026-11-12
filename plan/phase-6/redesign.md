# redesign

Phase 6, steps 4–8, 10 and 13 ([order](README.md#order)). This was the Phase 7 proposal, written 2026-11-12. The user merged it into Phase 6 that day, so there is no Phase 7. **This file wins over every other Phase 6 file, [simplify](simplify.md) included.** The other files still hold what this one doesn't change: pace, the cli, ins and flows, classify, ui and comments. The choices still open are in [Decisions](#decisions), each with a recommendation; ask the user before building a step that needs one.

The redesign makes flntools pleasant to write against, at all three of its layers:
1. **the source API**: what a source writes to tell flnd about a site;
2. **the engine API**: how the CLI (and anything else in Python) drives flnd;
3. **the `shared` API**: the toolkit both are built on, redesigned to be fluent and consistently named, and hardened.

All three follow one set of [API conventions](#2-api-conventions): verbs for actions, nouns for reads, one vocabulary, fluent objects where a value is worked on.

It takes ideas from **gallery-dl** (the architecture flnd adopted in Phase 6) and from **download managers such as IDM** (resume, split downloads, per-host connection limits, refreshing expired links). Every idea is either taken, with where it goes, or rejected, with why ([gallery-dl](#b5-what-flnd-takes-from-gallery-dl), [IDM](#b6-what-flnd-takes-from-download-managers-idm)).

---

## Contents

- [0. Where things stand](#0-where-things-stand)
- [1. Goals, measures and non-goals](#1-goals-measures-and-non-goals)
- [2. API conventions](#2-api-conventions)
- [Part A: the source API](#part-a-the-source-api)
- [Part B: the engine API](#part-b-the-engine-api)
- [Part C: the `shared` API](#part-c-the-shared-api)
- [Where each part is built](#where-each-part-is-built)
- [Decisions](#decisions)
- [Rejected](#rejected)
- [Tests](#tests)

---

## 0. Where things stand

Checked on 2026-11-12, when the user merged this into Phase 6:
- **Built and committed:** step 1 (fixes, `dc471b0`), step 2 (setup, `f13c182`) and step 3 (`e8aa96c`). Step 3 holds the cli, `pace.py` (not yet called by requests), `auth.py`, Chrome only, the new options and saved settings.
- **In the working tree, uncommitted:** every category without `--from`, and `--from` refused for a single post ([A4](#a4-categories-are-methods)).
- **Not built:** everything in this file. `Source` still has `run(session)` and its fields, and `session.save` is still how files are reported. There's no `items.py`, `job.py` or `sources/common.py`. So Part A takes the place of the old step 3's architecture part, and no source is rewritten twice.

### What a source author has to learn, phase by phase

| | Phase 5 (code today) | Phase 6 (before the redesign) | **Redesign** |
| --- | --- | --- | --- |
| `Source` fields | 18 | 11 | **9**, plus 1 optional (`unsupported`) |
| `Source` hooks | 7 (`resolve`, `run`, `headers`, `sign`, `login`, `logged_in`, `connect`) | 5 (`resolve`, `items`, `prepare`, `login`, `connect`) | **3** (`prepare`, `login`, `connect`), plus `resolve` as an escape hatch for tlg only |
| how targets are read | hand-written `resolve` with `link`/`name`/`unreadable` | the same | **`@target("instagram.com/p/{code}")` patterns** |
| how categories are chosen | `categories` + `default_categories` + `session.wants(category=)` at every step (19 calls) | `categories` dict + `wants` | **`@user.category` methods**: the engine calls the ones `--from` asked for, or every one without it |
| session members | 35 | 19 | **13** (`site`) + 4 on `login` |
| item types | `session.save(...)` with 9 keywords | `Post(meta: dict, files)`, `File(source, meta: dict, …)` | **`Post(id, files, date=…, **fields)`**, **`File(url, …)`**: typed keywords, not dicts |
| imports in a source | 5–7 lines from `flnd.session`, `flnd.sources.base`, `flnd.target`, `flnd.dash`, `flnd.sources.relay` | about 4 | **1**: `from flnd.source import …` (and `flnd.sources.common` for Meta's sites) |
| date handling | every source converts timestamps itself (3 buggy copies) | `day()` helper in `common.py` | **the engine reads any date**: `Post(date=post["taken_at"])` |
| `--time` early stop | each source breaks out itself (`if not session.wants(date=…): break`) | the same | **the engine stops a `newest_first` feed** |

---

## 1. Goals, measures and non-goals

### Goals

1. **A source reads like a description of the site.** Its routes say which URLs it takes, its categories say what it can download, and each category is a generator of posts. Nothing in a source decides where a file goes, whether it's wanted, whether it's saved already, when to stop, or how to retry.
2. **Every rule has exactly one home.** Today the `--from` rule lives in 19 `session.wants` calls, and the `--time` rule in each source's loop break. The date rule is in three converters, and the target rule in six `resolve` functions. After the redesign, each of these is in one function in the engine.
3. **The engine has one method per command.** `Engine.download`, `sync`, `login` and `logout` take the CLI's own text or typed values, and return a `Report`. The CLI passes its arguments through, and turns a report into lines and an exit status.
4. **One good API everywhere.** `shared`, `flnd.source` and the engine follow the same [conventions](#2-api-conventions): fluent, concise, verbs for actions and nouns for reads, one word for one meaning. Size doesn't constrain it: the user removed the line ceiling on 2026-11-12.
5. **Hardened.** Every public `shared` name is pinned by a snapshot test. Untrusted input has size limits. Big downloads survive a dropped connection and a Ctrl-C. No module opens more connections to one host than a browser would.

### Measures (checked by tests where they can be)

- A new source with one route and two categories, like loi, is **under 60 lines** before its site-specific parsing.
- A source module imports from `flnd.source`, `flnd.sources.common`, `shared` and the stdlib only. A test enforces it.
- `Site` has 13 members, and a test checks `FakeSite` has exactly those.
- `tests/test_names.py` passes: every public name in `shared`, `flnd.source` and `flnd` follows the [conventions](#24-enforced).
- `flnd/cli.py` has no logic that a test of `Engine` can't reach: every branch it has is text parsing or printing.

### Non-goals

- No plugin system and no third-party sources: `SOURCES` stays one explicit list ([Rejected](#rejected)).
- No config files, filter expressions, or new `download` options. Phase 6's command line stays exactly as decided.
- No async API in `shared` (README "Out of scope"); async stays only where tlg needs it.
- No history migration: keys keep their forms, as [simplify](simplify.md#3-no-history-migrations) decided.

---

## 2. API conventions

The user asked on 2026-11-12 for an API that is good and useful above all: **fluent, concise, verbs for actions, nouns for reads, consistent, elegant**. Size doesn't constrain it: the user removed `shared`'s line ceiling that day. These rules apply to everything public: every `shared` module, `flnd.source` (what sources use) and `flnd`'s engine API. They replace the Naming section of [plan/README.md](../README.md#naming) once approved.

### 2.1 Words

| # | Rule | Yes | No |
| --- | --- | --- | --- |
| W1 | **A verb changes something, waits for something, or talks to the network or a person.** Files, state, a page, a socket or a prompt | `save`, `open`, `close`, `fetch`, `download`, `compress`, `scroll`, `wait`, `ask` | `selection.get_text()` |
| W2 | **Everything else is a noun.** It's a property when it takes no argument, and a method named for what it returns when it takes one. What a read costs is said in its docstring, not in its name | `page.url`, `jar.header`, `clip.width`, `store.keys(prefix="")`, `clip.labels(top=3)` | `get_keys`, `to_header`, `kind_of` |
| W3 | **One word when one word says it**, two only when one is ambiguous | `cookies.load`, `crypto.sha256`, `log.group`, `cookies.from_browser`, `worker.as_completed` | `load_from_file`, `hash_sha256`, `log.multi` |
| W4 | **The familiar word wins**: the one the stdlib, Unix or the wrapped library already uses | `open`/`close`, `as_completed`, `goto`, `css`, `xpath`, `sha256`, `zip`/`unzip`/`ls`, `replace` | `race`, `connect` for launching a browser |
| W5 | **A boolean property is an adjective or a participle** | `ok`, `finished`, `interactive`, `animated`, `closed` | `is_ok`, `done` (a count) |
| W6 | **Plural for a collection, singular for one** | `pages`, `names`, `keys()`; `first`, `page` | `all_pages` |
| W7 | **No prefixes that only say "function"**: `get_`, `do_`, `to_`, `load_from_`, `make_`, `create_` | `jar.header` | `cookies.to_header(jar)` |

### 2.2 One vocabulary

The same word means the same thing everywhere, and nothing else means that thing:

| Word | Means | Where |
| --- | --- | --- |
| `open(…)` | start a live resource, which `close()` and `with` end | `kv.open`, `browser.open`, `media.open`, `Browser.open(url)` (a new tab) |
| `load(path)` / `save(dest)` | read a file into a value / write a value to a file, atomically | `cookies.load`, `Jar.save`, `Media.save` |
| `parse(text)` | text into a value, raising `ValueError` | `selector.parse`, `Range.parse`, `Choice.parse` |
| `fetch(url)` | one network request, returning a `Response` | `http.fetch`, `Page.fetch`, `site.fetch` |
| `download(url, dest)` | network to a file | `http.download` |
| `check()` | validate, raising the module's error | `Media.check` |
| `wait(…)` | block until a condition | `Page.wait` |
| `ask` / `confirm` / `choose` | ask a person | `prompt.*`, flnd's `Login` |
| `on_<event>(…)` | subscribe; returns a `Subscription` (`stop()`, `with`) | `Page.on_response`, `Page.on_request` |
| `replace(**changes)` | a copy with fields changed (`str.replace`, `dataclasses.replace`) | `Post.replace` |
| `filter(…)` | a copy with fewer items | `Jar.filter` |
| `dest` | where output goes | every writer; never `out`, `dest_dir` or `path` |
| `timeout` | seconds, as a `float` | everything that blocks |
| `progress` | a callback `progress(done, total)` | everything slow; never a label |
| `label` | the text of a progress row `shared` draws itself | `worker`, `http.download` |
| `level` | `best`, `high`, `mid` or `low`, as the CLI names them | `Media.compress`; replaces `preset` |
| `match` | a `str` (substring), compiled regex or predicate over a URL | `on_response`, `on_request`, `watch_responses` |
| `until` | a predicate that ends a loop | `Page.scroll`, `Page.handoff` |

### 2.3 Shapes

| # | Rule | Example |
| --- | --- | --- |
| S1 | **Fluent objects for things you work on; functions for one-shot jobs.** A value that's inspected and transformed several ways is an object, and each transform returns a new object of the same kind, so calls chain. A job that's one call from start to finish stays a function | objects: `Selection`, `Jar`, `Media`, `Store`, `Page`. Functions: `archive.zip`, `http.download`, `crypto.encrypt`, `worker.as_ordered` |
| S2 | **Transforms never write over their input; only `save` writes.** A chain makes temporary files as it goes, and `save(dest)` (or `save()`, meaning over the original) ends it atomically | `media.open("a.mov").trim(end=10).compress("mid").save("a.mp4")` |
| S3 | **The main input is positional; options are keyword-only**, named from the vocabulary | `http.download(url, dest, *, parts="auto")` |
| S4 | **State is a settable property; transitions are verbs** | `bar.total = 10`, then `bar.advance()`, `bar.finish()`; `log.level = "debug"` |
| S5 | **Every live resource is a context manager with `close()`, and every wrapper has `.raw`** (Rule 2, unchanged) | `with kv.open(path) as store:`, `page.raw` |
| S6 | **One error base per module, `<module>.Error`**, with `<Kind>Error` under it. Errors about untrusted, damaged or refused input also subclass `shared.UnsafeError`, `shared.BrokenError` or `shared.RefusedError` ([C4](#c4-errors)) | `except media.Error`, `except shared.UnsafeError` |
| S7 | **One spelling.** No aliases, no deprecated names kept: `shared` has one user, changed in the same commit | — |

### 2.4 Enforced

`tests/test_names.py` walks the [API snapshot](#c5-namespace-and-the-api-snapshot) and fails on:
- a public name with a W7 prefix;
- a module whose errors don't share a `<module>.Error` base;
- a public function whose options after the first parameter aren't keyword-only;
- an option named outside the vocabulary for its meaning (`out=`, `dest_dir=`, `preset=`, a `progress=` that isn't callable).

A rename shows in review as a one-line diff of `tests/data/api.txt`.

---

## Part A: the source API

### A1. One module for source authors: `flnd/source.py`

Everything a source uses is importable from one place:

```python
from flnd.source import Source, target, Post, File, Pace, Site, Login, FetchError, LoginError, TargetError, pages
```

| Name | What it is | Was |
| --- | --- | --- |
| `Source` | the base class: 9 fields, 3 hooks | `flnd.sources.base.Source` |
| `target` | the route decorator ([A3](#a3-targets-are-routes)) | `resolve`, `flnd.target.link`, `name`, `unreadable`, `kinds_of_target`, `examples` |
| `Post`, `File` | what a category yields ([A5](#a5-post-and-file-typed-keywords-not-dicts)) | `session.save`, `session.write`; Phase 6's `flnd/items.py` |
| `Pace` | how fast a source may go | `flnd/pace.py` (unchanged, re-exported) |
| `Site` | the handle a source acts through ([A6](#a6-the-site-handle-13-members)) | the `Session` protocol in `flnd/session.py` |
| `Login` | the handle `login` asks through ([A7](#a7-the-login-handle)) | `Session.ask`, `interactive` |
| `FetchError`, `LoginError`, `TargetError` | the three errors a source raises | three modules |
| `pages` | the one pagination loop ([A8](#a8-helpers-pages-and-the-common-module)) | four hand-written cursor loops |

`flnd/source.py` holds the declarations only, about 230 lines with the route matcher. The engine's implementations (`TargetSite`, the job) live elsewhere and are never imported by a source. A test walks every `flnd/sources/*.py` import and fails on anything else (it already fails on httpx, Playwright, `worker`, `kv`, `log` and `prompt`).

`flnd/target.py` merges into `flnd/source.py`: `Target` and `TargetError` move, and the route matcher replaces `link`, `name` and `unreadable`. `owner()` (finding a URL's source by domain) moves to the engine, which is its only caller.

### A2. The `Source` declaration

```python
class Instagram(Source):
    code, name = "ins", "Instagram"
    domains = ("instagram.com", "instagr.am")
    cookies = ("sessionid",)
    pace = Pace(1.0, budgets={"/graphql": (275, 600), "/api/v1/": (75, 660)})
    folder = "{category}/{album}"
    unsupported = {"instagram.com/stories/*": "story links aren't supported; give the profile, and its stories come with it"}
```

| Field | Means | Default | Phase 6 |
| --- | --- | --- | --- |
| `code`, `name` | the target prefix and the name of its slot in the vault; the name in help | required | — |
| `domains` | the site's hosts, short-link hosts included | required | — |
| `cookies` | cookie names, any one of which proves a cookie login | `()` | — |
| `logins` | the `flnd login` methods | `("browser", "file")` | — |
| `pace` | `Pace(floor, budgets)`, or a number for the floor | `0.5` | — |
| `folder`, `filename`, `key` | format strings over a post's fields ([A5](#formats)) | `"{category}"`, `"{date}_{id}{index}"`, `"{id}_{num}"` | — |
| `unsupported` | `{pattern: message}`: URLs on the site that `resolve` refuses with a specific message, such as story links or Twitter's `/home` | `{}` | **new**: replaces `NOT_PROFILES` (twt), `is_profile` (fbk) and the hand-written refusals (ins, fbk) |
| `examples` | | | **goes**: help shows the forms the routes accept ([A3](#generated-forms-instead-of-examples), [D4](#decisions)) |
| `categories` | | | **goes**: the `@route.category` methods are the categories ([A4](#a4-categories-are-methods)) |

`filename` has no extension: the content decides it, as since Phase 5. `{index}` is `""` for a one-file post and `_<num>` otherwise. `{date}` is `YYYY-MM-DD`, and an empty field with its separator is dropped ([formats](#formats)).

Hooks:

| Hook | Means | Phase 6 |
| --- | --- | --- |
| `prepare(request, cookies) -> request` | add headers and sign a request to the source's own `domains`, before every attempt; never called for CDNs | — |
| `login(login, method)` | a login of the source's own (tlg's QR and phone), through the [`Login`](#a7-the-login-handle) handle | took the whole session |
| `connect(site)` | the SDK client for `site.client`, a context manager or an async one; raises `LoginError` without a login | — |
| `resolve(text) -> Target` | **optional**: only for a source whose forms no pattern can say (tlg's chat titles and `-100` ids). The default matches the routes | was required for every source |
| `items(session)` | | **goes**: the routes and categories replace it |

### A3. Targets are routes

A route is a method decorated with `@target(*patterns)`. The method's name is the target's kind (`user`, `post`, `video`, `chat`). These are the kind names in use today, so `targets.db` and the history keep their keys. Its one parameter after `site` receives the value the pattern captured:

```python
@target("instagram.com/p/{code}", "instagram.com/reel/{code}", "instagram.com/tv/{code}", "instagram.com/*/p/{code}")
def post(self, site: Site, code: str) -> Iterator[Post]:
    yield self.media_post(site, code)

@target("{user}", "instagram.com/{user}")
def user(self, site: Site, user: str) -> Account:
    return Account.load(site, user)
```

#### Pattern syntax

The whole syntax, in five rules:

| Rule | Example | Matches |
| --- | --- | --- |
| A pattern that **starts with a domain** matches URLs on that host. The scheme is optional, `www.`, `m.` and `mobile.` are dropped, and so are a trailing `/`, `#…` and any query the pattern doesn't name. Extra path segments after the pattern are allowed. | `instagram.com/{user}` | `https://www.instagram.com/example_user/reels/?hl=en` |
| A pattern **without a domain** matches the text after `code:`. | `{user}` | `ins:example_user`, `ins:@example_user` |
| **`{name}`** is one path segment, captured. By default it's a user name: `[\w.]+`, with one leading `@` dropped, as `target.name` does today. | `tiktok.com/@{user}` | `tiktok.com/@example_creator` |
| **`{name:regex}`** is one segment matching the regex. **`*`** is any one segment, not captured. | `tiktok.com/*/video/{id:\d+}`, `{id:\d{16,}}` | `tiktok.com/@x/video/7301…`, `tik:7301234567890123456` |
| **`?key={name}`** captures a query parameter. | `facebook.com/profile.php?id={user:\d+}` | `facebook.com/profile.php?id=100012345678901` |

Then:
- **Every pattern has exactly one placeholder.** Its value is `Target.value`, which names the target's folder and starts its history keys. Two captures would make a target's identity ambiguous: tik's video URL holds a user and an id, so the user is a `*`.
- **Order decides.** The source's `unsupported` patterns are tried first. Then routes are tried in the order the class declares them, and within a route its patterns in order. `post` comes before `user`, so `instagram.com/p/ABC` is never read as the user `p`. The most specific pattern wins: a route pattern with more literal segments than the matching `unsupported` one (`{…}` and `*` aren't literal; the host counts as one) is read instead of refused, so twt's `x.com/i` refusal doesn't catch `x.com/i/web/status/<id>` (user, 2026-11-12).
- **A URL on the source's domains that nothing matches** is followed once with a HEAD request and matched again: short links, as Phase 6 decided. A second miss is a `TargetError` listing the forms.
- **Mistakes fail at import, not at run time.** When the class is created, each pattern is compiled, and each placeholder must name the method's parameter. Two routes with the same pattern are an error.

The matcher is about 70 lines in `flnd/source.py`. It replaces about 110 lines of `resolve` across five sources and `flnd/target.py`'s `link`, `name` and `unreadable`.

#### The six sources as routes

| Source | Routes, in order |
| --- | --- |
| ins | `post`: `instagram.com/p/{code}`, `/reel/{code}`, `/reels/{code}`, `/tv/{code}`, `/*/p/{code}`; `user`: `{user}`, `instagram.com/{user}`. Unsupported: `instagram.com/stories/*` |
| twt | `post`: `x.com/*/status/{id:\d+}`, `twitter.com/*/status/{id:\d+}`; `user`: `{user}`, `x.com/{user}`, `twitter.com/{user}`. Unsupported: `x.com/home`, `x.com/explore`, `x.com/i/*`, `x.com/settings`, … (today's `NOT_PROFILES`), with "that's not a profile" |
| tik | `video`: `{id:\d{16,}}`, `tiktok.com/*/video/{id:\d+}`, `tiktok.com/*/photo/{id:\d+}`; `user`: `{user}`, `tiktok.com/@{user}` |
| fbk | `user`: `{user}`, `facebook.com/profile.php?id={user:\d+}`, `facebook.com/people/*/{user:\d+}`, `facebook.com/{user}`. Unsupported: `facebook.com/*/posts/*`, `facebook.com/watch*`, `facebook.com/reel/*` with "single posts and videos aren't supported yet; give the profile" |
| loi | `post`: `{id:\d+}`, `loix.cc/lois/{id:\d+}` |
| tlg | keeps its own `resolve` (titles with spaces, `t.me/c/<n>` becoming `-100<n>`, `+invite`), returning `Target("tlg", "chat", value)`; its `chat` route has no patterns: `@target()` |

#### Generated forms instead of `examples`

Help and `TargetError` list each pattern as people write it, with the placeholder in angle brackets:

```
ins:<user>  https://www.instagram.com/<user>/  https://www.instagram.com/p/<code>/  https://www.instagram.com/reel/<code>/
```

So the forms can't drift from what's accepted. That drift happens today: ins's `/tv/` is accepted but not listed. A route can carry a hand-written form for help when a pattern reads badly: `@target(..., shown="tik:<video id>")`. The README keeps its real-looking examples ([D4](#decisions)).

### A4. Categories are methods

A route that has categories returns a **context**: whatever the categories share, such as a loaded profile, a user id or a Telegram chat. Each category is a method decorated with `@<route>.category`, taking `(site, context)` and yielding `Post`s:

```python
@target("{user}", "instagram.com/{user}")
def user(self, site: Site, user: str) -> Account:
    return Account.load(site, user)          # the profile page, once: its id, picture, highlights

@user.category(newest_first=True)
def posts(self, site: Site, account: Account) -> Iterator[Post]:
    yield from self.grid(site, account, "posts")

@user.category(newest_first=True)
def reels(self, site, account): yield from self.grid(site, account, "reels")

@user.category
def stories(self, site, account): yield from self.reel_posts(site, [account.id])

@user.category
def highlights(self, site, account):
    for highlight in account.highlights:
        for post in self.reel_posts(site, [highlight.id]):
            yield post.replace(album=highlight.title)

@user.category
def profile(self, site, account):
    yield Post(account.id, [File(account.picture, headers=CDN)])

@user.category(newest_first=True)                      # other people's content, taken too without --from
def tagged(self, site, account): yield from self.grid(site, account, "tagged")

@user.category(newest_first=True)
def reposts(self, site, account): yield from self.grid(site, account, "reposts")
```

`@route.category` is the `@property` / `@x.setter` idiom: the route object in the class body registers its categories. The decorator returns the function unchanged, so it stays an ordinary method that tests can call.

| Option | Means | Default |
| --- | --- | --- |
| `newest_first` | the category yields posts newest first, so the engine may stop early on `--time` and in `sync` ([A9](#a9-time-and-early-stops-the-engines-job)) | `False`: never stop early. A wrong `True` would silently lose posts, so it is set only where the site's order is known |
| `name` | the category's name, when it isn't a valid Python name | the method's name |

What the engine does with them:
- **Without `--from` it calls every category; with it, only the ones named**, in declaration order (decided by the user, 2026-11-12, replacing Phase 6's own-content default; already in the code). There is no per-category default. `session.wants(category=…)` goes from all 19 places it's called. It isn't replaced by anything: a category that isn't asked for is simply never called.
- **It calls the route once per target**, and only when at least one category is taken. `--from profile` on a twt user costs one `UserByScreenName`, as today.
- **It sets `category` on every post** a category yields, so no source writes `"category": "posts"` again.
- **`Source.categories` becomes a class property**, derived from the routes. Help and `--from` checking read it as they read the field today.
- A route **without** categories (ins `post`, twt `post`, tik `video`) is itself a generator of posts with no category, so its files land in the target's folder: `.flnd/downloads/ins/<code>/`, as the README's layout says.
- **`--from` with a post link is a usage error** (decided by the user, 2026-11-12: strict, not clever). A category such as `tagged` describes a profile's tab, not a post, so a post link has none. If `--from` is given and any target's route has no categories, `download` stops before anything starts, with exit status 2:
  ```
  flnd: --from applies to profiles and chats; https://www.instagram.com/p/C1abcDEFghi/ is one post, downloaded whole: give it without --from
  ```
  The check runs in `Engine.download` right after `resolve`, where `unknown_categories` already runs, so the CLI and library callers get the same `UsageError`. Today the same command silently downloads nothing and exits 0, because the post's files are filed under `posts` and `--from tagged` filters them out.
- **Async works the same.** tlg's `chat` route is `async def` and returns the Telethon entity, and its `photo`, `video` and `file` categories are async generators. The engine runs them on the source's loop, as Phase 6 runs an async `items`.

### A5. `Post` and `File`: typed keywords, not dicts

```python
class Post:
    def __init__(self, id: str | int, files: Sequence[File] | Callable[[], Iterable[File]] = (), *,
                 date: DateLike = None, headers: Mapping[str, str] | None = None, **fields: Any): ...
    def replace(self, **fields: Any) -> Post: ...

class File:
    def __init__(self, url: str | Sequence[str] | Fetch, *, audio: str | None = None,
                 headers: Mapping[str, str] | None = None, name: str | None = None, size: int | None = None): ...
    @classmethod
    def text(cls, name: str, text: str) -> File: ...
```

- **`id`** is the post's id as the site shows it. It is required, so a post can't lose its key by a typo in a dict.
- **`files`** is a list or a function. As Phase 6's [simplify](simplify.md#1-one-item-type-post) decided, the function is called only for a post that passes `--time` and whose first key isn't saved, so ins's media info request costs nothing for a saved post. It is also how an **expired link is refreshed** ([B6](#b6-what-flnd-takes-from-download-managers-idm)).
- **`date`** takes anything a site gives: a `date`, a `datetime`, Unix seconds or milliseconds as an int, float or digit string, or None. The engine reads it with the one `day()` rule. That rule refuses dates before 2004 and after next year, which catches the 2011 dates a small Instagram `pk` gives. The `day()` helper Phase 6 put in `common.py` goes, and so do ins `taken_on`, fbk `posted_on` and tik `posted_on`.
- **`headers`** on a post apply to each of its files that has none. Most sources send one CDN Referer for every file.
- **`**fields`** are the site's own fields for the formats: `album`, `title`, `author`, `user`. A field whose name the engine sets (`category`, `num`, `count`, `index`, `extension`) is a `TypeError` at once, so a source can't overwrite one by accident.
- **`File.name`** names a file the site already names, which the formats must not rename: loi's archives, tlg documents. **`File.text`** is a text file such as loi's `info.md`, written atomically.
- **No `meta` dicts**, no `num` (the engine numbers files in order), no `extension` (the content decides) and no `kind` (sniffed). A source never sets any of them.
- Both are frozen. `Post.replace(**fields)` returns a copy ([2.2](#22-one-vocabulary)), for a helper that builds a post and a category that adds `album`.

#### Formats

`folder`, `filename` and `key` are `str.format` strings over the post's fields plus the engine's: `id`, `date`, `category`, `num`, `count`, `index`, and the source's `**fields`. One rule makes optional fields painless: **a field that is missing or empty becomes `""`, and an empty path segment, or a separator left dangling by an empty field, is dropped.** So ins's single folder format covers every category:

```python
folder = "{category}/{album}"      # posts → "posts", highlights → "highlights/<title>", a single post → ""
filename = "{date}_{id}{index}"    # an undated post → "<id>", not "_<id>"
```

`fmt.filename` is still applied to every part, so a site's title can never leave the target's folder.

### A6. The `Site` handle: 13 members

The parameter every route and category gets is renamed from `session` to **`site`** ([D2](#decisions)). "Session" means three other things in this code: httpx's client, Telethon's session string (`secrets["session"]`) and a login session. And `site.fetch`, `site.goto` and `site.warn` read as what they are.

| Group | Members | Phase 6 |
| --- | --- | --- |
| target | `target`, `cookies`, `time` | `categories`, `since`, `until`, `wants` go; **`time`** is new: a `Range(since, until)` with `in`, for a source that can ask its server for a range (tlg's `offset_date`) |
| requests | **`fetch(url, *, method="GET", headers=None, soft=False, in_page=False, **kw) -> Response`** | `fetch_json` and `fetch_html` become one verb returning [`http.Response`](#http-new-fetch-download-response): `site.fetch(url).json`, `site.fetch(url).html`. With `soft=True`, a 401, 403 or 404 gives None |
| browser | `goto(url, *, idle, timeout) -> bool`, `handoff(*, until, message)`, `page` | — ; `responses` was already moved out of the core by the user (2026-11-12): it is `watch_responses` in `flnd/sources/common.py` ([A8](#a8-helpers-pages-and-the-common-module)) |
| SDK | `client` | — |
| stores | `state`, `secrets` | — |
| people | `info`, `warn`, **`back_off(reason, *, retry_after=None)`** | `interactive` and `ask` move to `Login`; `throttled`, a participle, becomes the verb it is ([W1](#21-words)): count a rate limit and wait it out |

That's 13 members: Phase 6's 19, less `categories`, `since`, `until`, `wants`, `interactive` and `ask` (6), plus `time`, with the two fetches merged into one.

- **Misuse becomes a type error.** Today `session.ask` raises `RuntimeError` during `flnd download`. After the redesign a download's `Site` has no `ask`, so pyright refuses the call before it runs.
- `FakeSite` in `tests/fakes.py` implements exactly these 13. A test compares its public names with the protocol's, as Phase 6 planned for `FakeSession`.

### A7. The `Login` handle

```python
class Login(Protocol):
    @property
    def interactive(self) -> bool: ...            # in a terminal, so ask() can ask
    def ask(self, message: str, *, secret: bool = False, description: str = "") -> str: ...
    @property
    def secrets(self) -> MutableMapping[str, str]: ...
    def info(self, message: str, **fields: Any) -> None: ...
```

`Login.ask` uses `prompt.ask`'s names (`secret=`, `description=`), so a source asks the way a script does. `Source.login(self, login: Login, method: str)`. Only tlg implements it. Cookie sources keep the engine's `browser` and `file` logins, as today.

### A8. Helpers: `pages` and the common module

**`pages`** is the one pagination loop. ins, fbk and tik replay their site's pagination request ([ins](ins.md#three-ways-to-reach-an-api)), and twt pages its GraphQL timelines. That's four hand-written cursor loops, each with its own "same cursor twice" bug risk:

```python
def pages(fetch: Callable[[str | None], Any], cursor: Callable[[Any], str | None]) -> Iterator[Any]:
    """Each page: fetch(None), then fetch(c) while the last page gives a cursor c not seen before.

        for page in pages(lambda c: site.fetch(API, params=variables(c)).json, lambda page: page["next"]):
            yield from posts_of(page)
    """
```

It is about 10 lines and stops on a repeated or empty cursor. Phase 6's [scripts](scripts.md#rejected) rejected `session.paginate` because twt was its only caller. Replay makes four callers, and as a plain function it adds no `Site` member.

**`flnd/sources/common.py`** keeps the source helpers that aren't core:
- `watch_responses(site, match, *, scroll=False)`. The user decided on 2026-11-12 that it isn't core: it's not a `Site` member and not a `shared` `Page.responses`. It's an iterator of matching JSON responses, built on `site.page`'s `on_response` and `scroll`, and only the scroll fallbacks use it.
- `meta_results(site, url)` (Phase 6), shared by Meta's two sites.
- The DASH picker that was `flnd/dash.py`.

Everything general is in `flnd.source`.

### A9. Time and early stops: the engine's job

Three rules, all in the job, none in a source:

1. **`--time` filters posts.** A post dated outside the range is skipped before its `files` are listed. A post with no date is kept, as today.
2. **A `newest_first` category stops early on `--time`.** After **5 posts in a row older than `since`**, the engine closes the category's generator. Python runs the generator's `finally` blocks, and no more pages are requested. Why 5: pinned posts come first and may be old, and Instagram and TikTok pin up to 3 and X up to 1, so 5 in a row can only mean the feed has gone past the range. It replaces each source's own `break`. Today those breaks are written differently in ins, twt and tlg, and missing in tik.
3. **`sync` stops a `newest_first` category at saved posts**, gallery-dl's `skip: "abort:N"`: after **5 already-saved posts in a row**, sync has caught up with that feed. This is the big win for `sync`. Today it lists a 3,000-post account to its end to find 4 new posts.
   - **Only when the last listing of that category reached its end.** `targets.db` records `complete: {category: day}` when a category's generator ends by itself, not by Ctrl-C or an error. Without that record, an interrupted first download, whose newest 100 posts are saved, would make every later sync stop at the top and never fetch the rest.
   - `download` never stops at saved posts: it lists everything and skips what's saved, as today.

### A10. Before and after

#### loi (the smallest source)

Today, about 25 lines of declarations, `resolve` and `run` dispatch. After (the site parsing below them is unchanged):

```python
class Loibus(Source):
    code, name = "loi", "LOIBUS"
    domains = ("loix.cc",)
    cookies = ("access-token",)
    folder = "{author}/{id} {title}/{part}"
    filename = "{name}"

    @target("{id:\\d+}", "loix.cc/lois/{id:\\d+}")
    def post(self, site: Site, id: str) -> dict:
        return site.fetch(f"{API}/lois/{id}").json["data"]

    @post.category
    def photo(self, site, post):
        yield Post(post["id"], [info_file(post, None), *previews(post)], date=post["created_at"],
                   author=post["author"], title=post["title"])

    @post.category
    def archive(self, site, post):
        if not decode_token(site.cookies.get("access-token", "")):
            site.warn("archives need a VIP login; run flnd login")
            return
        links = self.links(site, post)
        yield Post(post["id"], [info_file(post, links), *archives(links)], date=post["created_at"],
                   author=post["author"], title=post["title"])

    def prepare(self, request, cookies): ...   # today's headers + sign, unchanged
```

Both categories list `info.md`, which holds the unzip password, so `--from archive` alone still writes it. The history key makes sure it's written once, which needs no special mechanism.

#### tlg (async, with its own `resolve`)

```python
    def resolve(self, text: str) -> Target: ...            # unchanged: titles, -100 ids, +invites

    @target()
    async def chat(self, site: Site, chat: str) -> Any:
        return await find_chat(site.client, chat)

    @chat.category(newest_first=True)
    async def photo(self, site, chat): async for post in messages(site, chat, "photo"): yield post

    @chat.category(newest_first=True)
    async def video(self, site, chat): async for post in messages(site, chat, "video"): yield post

    @chat.category(newest_first=True)
    async def file(self, site, chat): async for post in messages(site, chat, "file"): yield post
```

`messages()` passes `site.time.until` to Telegram as `offset_date`. The `since` break goes, because rule 2 of [A9](#a9-time-and-early-stops-the-engines-job) does it.

### A11. Testing and developing a source

- **`collect(source, text, *, json=, html=, pages=, options=) -> list[Saved]`** in `tests/fakes.py`. It resolves the text, runs the route and the wanted categories on a `FakeSite`, expands lazy `files`, and applies the formats. Each `Saved(path, url, post)` is what the job would download. A source test then reads as a table of expectations:

  ```python
  saved = collect(Instagram, "ins:example_user", json=FIXTURES, options=Options(categories=("reels",)))
  self.assertEqual([s.path for s in saved], ["reels/2026-01-02_C1abc_0", "reels/2026-01-02_C1abc_1"])
  ```

- **Route tables.** Each source test has a `ROUTES` table, gallery-dl's `#url` / `#class` results in miniature: `("https://www.instagram.com/reel/C1abc/", ("post", "C1abc"))`. One shared test runs every table, and checks that every pattern is covered by at least one row.
- **`scripts/dev.py try <target> [--from …]`**, a developer task and not a `flnd` option, as gallery-dl's `--simulate` and `--dump-json` are. It runs the source for real (network, browser, login), downloads nothing, writes no history, and prints the post tree with each file's planned path and URL. It's the quickest way to see what a source change does on a live account.
- **`tests/example_source.py`**: a complete small source over fixtures, which doubles as the template a new source starts from and as the job tests' fake source. It's code that's run, not a dead example.

---

## Part B: the engine API

### B1. One method per command

```python
from flnd import Engine, Range, Choice

engine = Engine(home=Path(".flnd"))
report = engine.download(["ins:example_user", "https://x.com/example_handle"],
                         categories="reels,tagged", time="2026-01-01,", image="webp,mid")
report = engine.sync()                                  # every target downloaded so far
engine.login("ins", "browser", browser="chrome")        # a cookie login, no questions
engine.login("tlg", "qr")                               # a source's own login asks through Login
engine.logout(["ins", "tik"])
```

| Method | Takes | Returns | Today |
| --- | --- | --- | --- |
| `resolve(texts) -> list[Target]` | target texts | targets, or `TargetError` at the first bad one | — |
| `download(targets, *, categories=None, time=Range(), image=None, video=None, audio=None, classify=None) -> Report` | targets or texts. `image`, `video`, `audio`: a `Choice`, `"off"`, or None to keep the saved setting. `classify`: True, `"off"` or None | a `Report` | `Engine.run(targets, Options)` plus `sync.download(engine, resolved, options, given)` in the CLI, plus `engine.failed` |
| `sync(targets=()) -> Report` | targets, or none for every one | a `Report` with each target's repaired and missing counts | `sync.plan` + `sync.run` + `_status` |
| `login(code, method, *, browser=None, profile=None, path=None) -> str` | the method's answers | the "logged in" message, or `LoginError` | half in `cli.py` (`auth.store`, `auth.from_file`, `auth.from_browser`) |
| `logout(codes) -> list[str]` | codes, or `["all"]` | the codes that had a login | `auth.logout` in a CLI loop |

- **Text or values, read in one place.** Each option takes the CLI's own text (`time="2026-01-01,"`, `image="webp,mid"`, `categories="reels,tagged"`) or a value (`Range`, `Choice`, a tuple). The engine reads text with `Range.parse` and `Choice.parse`, which today live in `cli._time` and `compress.parse_choice`. So the CLI passes its arguments through untouched, and a script writes what a person would type. A bad value raises `ValueError`, which the CLI shows as a usage error.
- **`Report`**, frozen:
  ```python
  @dataclass(frozen=True)
  class Report:
      targets: tuple[TargetReport, ...]
      @property
      def files(self) -> int: ...                 # files written, over every target
      @property
      def failed(self) -> tuple[Target, ...]: ...
      @property
      def ok(self) -> bool: ...                   # no target failed: the CLI's exit status

  @dataclass(frozen=True)
  class TargetReport:
      target: Target
      written: tuple[Path, ...]
      skipped: int                                # already saved, or filtered out
      repaired: int = 0                           # sync only
      missing: int = 0                            # sync only: files the site no longer lists
      settings: str = ""                          # "--image webp,mid (saved)"
      error: str | None = None
  ```
  It replaces `Engine.run`'s `int`, `Engine.failed` (state that piled up across runs), and `sync.Line`. The CLI prints the summary from it and returns `0 if report.ok else 1`.
- **The CLI is text in, text out.** `flnd/cli.py` keeps only argument parsing, `login`'s questions and printing. Every decision (saved settings, grouping targets by settings, applying a changed setting to saved files) is in the engine, where tests reach it without a terminal.
- **`flnd/__init__.py` exports** `Engine`, `Report`, `TargetReport`, `Target`, `Range`, `Choice` and `TargetError`, and nothing else. This isn't for imaginary outside users. It is the CLI's own interface, written down, and what the tests import.

### B2. Saved settings move into the engine

`flnd/sync.py` (381 lines) splits:
- **`flnd/settings.py`**, about 80 lines: `targets.db` rows (each target's saved `--image`, `--video`, `--audio` and `--classify`, and from [A9](#a9-time-and-early-stops-the-engines-job) its `complete` categories), reading Phase 5's rows, and `announce`.
- **`flnd/repair.py`**, about 110 lines: `sync`'s repair (missing, empty or broken files downloaded again, stale `.part` files removed, modified times set) and its tally.
- `Engine.download` and `Engine.sync` call both. `sync.py` goes.

### B3. The job: one fixed pipeline

`flnd/job.py`, for one target. It's about 220 lines, against Phase 6's planned 180, because it takes on the early stops and the link refresh:

```
route(site, value) ──► for each wanted category, in order:
    for post in category(site, context):                        ← the source
        set category; read date                                 ← A5
        --time filter; newest_first early stop                  ← A9 rules 1–2
        first key saved? (sync: saved-run stop)                 ← A9 rule 3
        files = post.files() if lazy                            ← simplify #1
        for num, file: name from the formats; skip if saved or queued
        queue the download ─► net.download (resume, parts, host limit)  ← B6
            on an expired link: post.files() once more, same num        ← B6
        postprocess: compress → classify → mtime                ← Phase 6
        history row written last
    category ended by itself → settings.complete(category)
```

- **Postprocessors keep Phase 6's protocol**, `(path, fields) -> path`, in a fixed order. There's no user-configured chain: flnd's options decide which ones run.
- **Ctrl-C is graceful, then immediate.** The first Ctrl-C stops listing: every category generator is closed, so its `finally` blocks run. Downloads already running finish, and the live rows say `stopping: 3 files finishing (Ctrl-C again to stop now)`. A second Ctrl-C stops at once and keeps the resumable `.part` files ([B6](#b6-what-flnd-takes-from-download-managers-idm)). Today the first Ctrl-C drops everything queued.
- **Errors keep their meaning.** A failed file is logged and its target goes on. A route or category raising `FetchError` or `LoginError` fails its target. A rate limit is waited out and is never a failure.

### B4. Modules and dependency rules

The engine side's helpers live in the `flnd/common/` package and the helpers sources share in `flnd/sources/common/` (layout decided by the user, 2026-11-12).

| Module | Holds | About (lines of code) |
| --- | --- | --- |
| `flnd/__init__.py` | the engine API's exports | 10 |
| `flnd/source.py` | everything a source imports ([A1](#a1-one-module-for-source-authors-flndsourcepy)), and the route matcher | 230 |
| `flnd/site.py` | `TargetSite` and `LoginSite`, the engine's `Site` and `Login` | 280 |
| `flnd/job.py` | the pipeline ([B3](#b3-the-job-one-fixed-pipeline)) | 220 |
| `flnd/engine.py` | `Engine`'s five methods, source groups, `Report` | 230 |
| `flnd/settings.py`, `flnd/repair.py` | from `sync.py` ([B2](#b2-saved-settings-move-into-the-engine)) | 80, 110 |
| `flnd/post.py` | compress, classify, mtime | 60 |
| `flnd/common/net.py` | the client, the one retry layer, back-off; downloads delegate to `http.download` ([C2](#http-new-fetch-download-response)) | 200 (from about 300) |
| `flnd/common/pace.py`, `flnd/common/auth.py`, `flnd/common/compress.py`, `flnd/common/files.py` | as Phase 6 leaves them; `files.write_atomic` goes for `shared.atomic` | — |
| `flnd/cli.py` | parsing and printing | 200 (from 281) |
| `flnd/sources/*.py`, `flnd/sources/common/` (`dash.py`, `relay.py`) | the sources | about 1,050 (Phase 6 planned about 1,150) |

The rules, each one a test:
1. A source imports only `flnd.source`, the `flnd.sources.common` package (`flnd.sources.common.dash`, `flnd.sources.common.relay`), `shared` and the stdlib.
2. `flnd.source` imports nothing from the engine side: not `site`, `job`, `engine`, `net` or `settings`.
3. `cli.py` imports `flnd` (the API), `shared` and `flnd.sources` (for help), and nothing else from flnd.
4. Nothing imports `cli.py`.

### B5. What flnd takes from gallery-dl

Phase 6 already took the core: items and a job, format strings, an archive (history) keyed by format, postprocessors, sleep and 429 rules, and per-site flows. The redesign looks again at what's left:

| gallery-dl | Redesign |
| --- | --- |
| each extractor's `pattern` regex, and `example` URLs used by tests | **taken**, as `@target` patterns and the generated forms ([A3](#a3-targets-are-routes)), plus `ROUTES` tables ([A11](#a11-testing-and-developing-a-source)) |
| `test/results/<site>.py`: URLs with expected counts and fields | **taken**, small: the `ROUTES` tables and `collect()` expectations, on fixtures instead of live sites |
| `--simulate`, `--get-urls`, `--dump-json` | **taken as a dev task**, `scripts/dev.py try` ([A11](#a11-testing-and-developing-a-source)), not a `flnd` option |
| `skip: "abort:N"` | **taken for `sync` only**, with the complete-listing guard ([A9](#a9-time-and-early-stops-the-engines-job)) |
| the `date-after` filter with an abort on order | **taken**, as `newest_first` ([A9](#a9-time-and-early-stops-the-engines-job)) |
| `cookies-update`: write back cookies the site rotated | **taken.** At the end of a run, a cookie source's saved jar, encrypted in the vault ([B7](#b7-every-credential-encrypted)), gets the cookies the site refreshed during it: twt's `ct0`, tik's `msToken`, ins's `csrftoken`. That's only for names already in the jar, so a run never adds a tracking cookie to a login. Today they're kept only in memory, so every run starts from the login-day cookies and ages towards a logout. About 20 lines in `auth.py` |
| the HTTP downloader resumes `.part` files with `Range` | **taken**, in `http.download` ([C2](#http-new-fetch-download-response)) |
| `extractor.find(url)` over all patterns | **taken**: the engine finds a URL's source by domain, then its route |
| `Message.Queue`, `parent-metadata` | stays out (Phase 6 removed `Queue`) |
| `metadata` postprocessor (JSON sidecars), `exec`, `zip` | **rejected**: no caller, and `--zip` went in Phase 6 |
| `--filter` expressions, `--range`, config files, `-o` options | **rejected**: Phase 6's command line is decided |
| the ytdl downloader (HLS, DASH by yt-dlp) | **rejected**: media rejects HLS ([phase-5](../phase-5/media.md#rejected)); DASH is already merged |
| `rate` (a bandwidth limit) | **rejected**: see IDM below |

### B6. What flnd takes from download managers (IDM)

IDM and its kind are built around the file transfer, which is the part flnd treats most simply today. `net.download` makes one connection per file and doesn't resume: a dropped connection restarts the file, and a failed attempt deletes its `.part`.

| Feature | Redesign | Why |
| --- | --- | --- |
| **Resume** (HTTP `Range`) | **taken.** A dropped connection resumes from the bytes it has, with `Range: bytes=<n>-` and `If-Range: <ETag or Last-Modified>`. A 200 instead of a 206 means the file changed, so it restarts. A `.part` and its small `.part.json` (URL, validator, size) also survive Ctrl-C and a crash, so the next run resumes it | loi's archives are GB-sized, and fbk and ins videos are hundreds of MB. Today a drop at 90% costs the whole file again, up to 3 times |
| **Split downloads** (several connections per file) | **taken, bounded.** A file of 16 MB or more, from a server that sends `Accept-Ranges: bytes` and a length, downloads in up to 4 ranges written into one preallocated `.part`, then is checked as one file. CDN downloads only, never the source's own domains | CDNs often cap the speed of each connection, and big videos are where flnd spends its time |
| **A connection limit per host** | **taken**: never more than **6 connections to one host** across every target and file at once, Chrome's own limit per host. Split ranges use free slots only | it keeps split downloads from looking like an attack, and makes "4 downloads at once" mean something when several targets share a CDN |
| **Refresh an expired link** (IDM's "refresh download address") | **taken.** A 403 or 410 from a signed CDN URL (ins and fbk `oe=`, tik's `x-expires`) on a post whose `files` was a function calls that function once more, takes the file with the same `num`, and resumes where it stopped | after a long rate-limit wait, or resuming the next day, a post's URLs have expired. Re-listing the whole feed for them is what flnd would otherwise do |
| a persistent download queue | **deferred** ([D7](#decisions)): resume plus history plus `sync`'s saved-run stop cover the same "pick up where I stopped" need | re-listing costs listing requests only, and lazy posts cost nothing when saved |
| a speed limit | **rejected**: no caller asked for it, and the OS or router does it better | — |
| a scheduler, shutdown when done | **rejected**: `cron` / Task Scheduler running `flnd sync`, documented in the README | — |
| browser download capture, clipboard watching | **rejected**: out of scope; `cat list.txt \| flnd download` takes lists | — |
| a proxy setting | **already there**: httpx reads `HTTPS_PROXY` and `ALL_PROXY`; the README says so | — |
| duplicates by content hash | **deferred**: the same file under two posts is rare, and hard links (as `classify` makes) would be the way | — |

### B7. Every credential encrypted

Decided by the user on 2026-11-12: **every credential flnd keeps is encrypted, cookies included.** Today only `secrets.db` is: Telegram's session string and API credentials. Each cookie source's login sits beside it as plain text in `.flnd/cookies/<code>.txt` (mode 0600). So the encryption protects one source in six, and someone who copies `.flnd/` gets every cookie login anyway.

- **One store for every credential.** A source's cookie jar is one more value in its encrypted vault, under the name `cookies`, beside tlg's `session`, `api_id` and `api_hash`. It's sealed the same way: AES-GCM, bound to its slot `<code>:cookies`, so a value copied into another source's slot doesn't decrypt. `.flnd/cookies/` goes.
- **`auth` reads and writes only the vault:**
  - `auth.store` (a login), `auth.load` (each run) and `logged_in` all go through it;
  - so do `logout` (one `clear()` per source) and the [cookie write-back](#b5-what-flnd-takes-from-gallery-dl) at the end of a run;
  - a cookie file someone gives `flnd login` is read once and never copied into `.flnd/`.
- **Existing logins move once.** The first run after this change finds each `.flnd/cookies/<code>.txt` or `.json`, stores its jar in the vault, and deletes the plain file only after the vault write succeeded. When the folder ends up empty, it's removed too. So nobody has to log in again. It is a credentials migration, not a history one, so Phase 6's "no history migrations" still holds.
- **What it protects against**, honestly: someone who copies `secrets.db`, or a backup of it, without `secret.key`. It doesn't protect against someone who can read the whole `.flnd/` folder, since the key is in it. That's the same limit Phase 6 accepted when it kept the vault, and it now covers every login rather than one. Keeping the key somewhere else (the OS keychain) is [D13](#decisions).
- `Jar.save` stays in `shared` for scripts; flnd no longer calls it.

---

## Part C: the `shared` API

Each module is redesigned against the [conventions](#2-api-conventions). The aim is what a script writer would want to type, not only what flnd calls today. That follows Phase 6's rule that `shared` is judged by usefulness. There is no line ceiling any more (user, 2026-11-12).

### C1. A tour

The whole toolkit in one screen, as a script would use it:

```python
from shared import archive, browser, cookies, crypto, fmt, http, kv, log, media, prompt, selector, worker

jar = cookies.from_browser("chrome").filter(domain="example.com")
page = http.fetch("https://example.com/feed", headers={"Cookie": jar.header})
titles = page.html.css("article h2::text").all

with browser.open(profile="Default") as chrome:
    tab = chrome.open("https://example.com/")
    tab.wait(".grid")
    links = tab.document.css(".grid a::attr(href)").all

with log.progress("photos", total=len(links)) as bar:
    for result in worker.as_completed(lambda url: http.download(url, "photos/"), links, concurrency=4):
        bar.advance()

clip = media.open("photos/clip.mov")
if clip.kind == "video" and clip.duration > 60:
    clip.trim(end=60).compress("mid", format="mp4").save("clip.mp4")

with kv.open(".cache.db") as store:
    store.set(crypto.sha256(clip.path), clip.labels(top=3), ttl=86_400)

archive.zip(["photos", "clip.mp4"], "out.7z", password=prompt.ask("Password", secret=True))
log.ok("{n} saved, {size}", n=fmt.count(len(links), "photo"), size=fmt.size(clip.size))
```

### C2. Module by module

Each module's table lists every public name today and what it becomes. **—** means unchanged.

#### `media`: a fluent `Media` object

Today it's 12 module functions over paths, plus three sub-namespaces (`media.image`, `media.video`, `media.audio`), and each call probes the file again. The redesign gives one object, opened once:

```python
clip = media.open("in.mov")              # probes once; raises media.BrokenError / UnsafeError
clip.kind, clip.format, clip.size        # "video", "mov", 48_211_004
clip.width, clip.height, clip.duration, clip.fps, clip.codec, clip.created, clip.location, clip.tags
clip.check()                             # decodes it all; raises media.BrokenError; returns clip

out = clip.trim(start=5, end=65).compress("mid", format="mp4")    # each step a new Media, in a temp file
if out.size < clip.size:
    out.save("clip.mp4")                                            # only save writes

media.open("a.jpg").compress("low", format="webp").save()           # over the original, atomically
media.open("v.mp4").merge(audio="a.m4a").save("joined.mp4")         # DASH video + audio
frames = media.open("v.mp4").frames(every=2.0, dest="frames/")     # list[Path]
labels = media.open("a.jpg").labels(top=3)                          # [("food", 0.91), ...]
```

| Today | Redesign |
| --- | --- |
| `probe(path) -> Info` | `media.open(path) -> Media`, whose fields are `Info`'s fields as properties |
| `check(path)` | `Media.check() -> Media` |
| `sniff(path) -> str \| None` | — (`media.sniff`: one read of the magic bytes, no probe) |
| `kind_of(extension)` | **`media.kind(name)`**: `"image"`, `"video"`, `"audio"` or None, from a name or an extension |
| `compress(src, *, dest, format, preset, quality, resolution, fps, codec, audio_bitrate, strip, progress)` | **`Media.compress(level="mid", *, format=None, codec=None, quality=None, resolution=None, fps=None, audio_bitrate=None, strip=True, progress=None) -> Media`**. `preset` becomes `level`, the CLI's word |
| `convert(src, *, dest, quality, fps, codec)` | `Media.convert(format, *, quality=90, fps=None, codec=None) -> Media` |
| `image.resize`, `image.crop` | `Media.resize(*, width=None, height=None)`, `Media.crop(box)` |
| `video.trim`, `video.frames`, `video.merge` | `Media.trim(*, start=0.0, end=None)`, `Media.frames(*, dest, every=None, at=None) -> list[Path]`, `Media.merge(*others, audio=None)` |
| `audio.extract` | **`Media.audio(*, bitrate=None) -> Media`**: the sound track, as a new `Media` |
| `classify(path, *, labels, top, threshold)` (Phase 6) | **`Media.labels(*, top=3, threshold=…, choices=LABELS) -> list[tuple[str, float]]`**: a read, so a noun ([W2](#21-words)) |
| — | **`Media.save(dest=None) -> Path`**: atomic; without `dest`, over the file the chain started from. **`Media.close()`** / `with`: drops the chain's temporary files. They're also dropped when the object is collected |
| `Info`, `Preset`, `Container` | `Info` stays as `Media.info` for callers that want the record; `Preset` and `Container` become private |
| `BrokenError`, `UnsafeError` | — , under `media.Error` and the [shared bases](#c4-errors); **`media.KindError`** for a transform that doesn't apply (trimming a photo) |

- **A transform checks its kind** and raises `KindError` with what it applies to ("trim is for videos and audio; a.jpg is an image"). That replaces the three sub-namespaces, which only existed to say which kind a function took.
- **Chaining is real work, not a lazy plan.** Each step runs ffmpeg or Pillow and writes a temporary file beside its destination, on the same disk, so `save` is a rename. A lazy pipeline that fused the steps into one ffmpeg call was considered, and [rejected](#rejected).
- flnd's use becomes `out = original.compress(level, format=fmt)`, then `if out.size < original.size: out.save()`, and its check is `media.open(path).check()`.

#### `cookies`: a `Jar`

```python
jar = cookies.load("cookies.txt")                         # Netscape or JSON, detected
jar = cookies.from_browser("chrome", profile="Default")   # every cookie of that profile
ig = jar.filter(domain="instagram.com")                   # Jar → Jar
ig["sessionid"], "sessionid" in ig, ig.names, len(ig)     # by name; iterating gives Cookie dicts
ig.header                                                 # "sessionid=…; csrftoken=…"
ig.merge(refreshed).save("ins-cookies.txt")               # newer wins; atomic, mode 0600
cookies.profiles("chrome")                                # ["Default", "Profile 1"]
```

| Today | Redesign |
| --- | --- |
| `load_from_file(path, *, domain)` | `cookies.load(path) -> Jar`, then `.filter(domain=)` |
| `load_from_browser(domain, *, browser, profile)` | `cookies.from_browser(browser="chrome", *, profile=None) -> Jar` |
| `to_file(jar, path)` | `Jar.save(dest)` |
| `to_header(jar)` | `Jar.header` |
| `profiles(browser)` | — |
| `list[Cookie]` everywhere | `Jar`, an immutable collection: `filter(*, domain=None, names=None)`, `merge(other)`, `names`, `header`, `[name]`, `in`, `len`, iteration, `.raw` (the list of dicts) |
| `CookieError` | `cookies.Error`. A file over 16 MB is refused (`cookies.UnsafeError`) |

`Browser.cookies` returns the live browser's `Jar` too, and `Browser.add_cookies(jar)` takes one, so a jar moves between files, browsers and HTTP headers without conversion.

#### `http` (new): `fetch`, `download`, `Response`

```python
page = http.fetch("https://api.example/items", params={"page": 2})     # Response; raises on 4xx/5xx
page.status, page.headers, page.url, page.text, page.json, page.html  # html is a Selection
http.fetch(url, soft=True)                                            # None for 401, 403 and 404

http.download("https://cdn.example/v.mp4", "videos/")                 # a folder: named from the URL
http.download([mirror_a, mirror_b], "f.7z", client=client, progress=bar.update, check=lambda p: media.open(p).check())
```

- **`fetch(url, *, method="GET", client=None, headers=None, params=None, data=None, json=None, soft=False, attempts=3, timeout=30.0) -> Response | None`**. With `soft=True`, a 401, 403 or 404 returns None; pyright makes the caller handle it.
- **`download(url, dest, *, client=None, headers=None, size=None, parts="auto", resume=True, attempts=3, timeout=30.0, check=None, progress=None, label=None) -> Path`**:
  - **sources**: mirrors are tried in order;
  - **resume and split**: [B6](#b6-what-flnd-takes-from-download-managers-idm);
  - **checks**: a size check, then the caller's `check`;
  - **writing**: an atomic rename; `dest` may be a folder.
- **`Response`** is one type for all three ways to fetch: `http.fetch`, `Page.fetch` and flnd's `site.fetch`. Its fields are `status`, `ok`, `url`, `headers`, `text`, `json` (raises `http.Error` if the body isn't JSON) and `html` (a `Selection`).
- **Errors:** `http.Error`; `RefusedError(status)`, also a `shared.RefusedError`; `RateLimitedError(retry_after)`, raised and not slept, so flnd's back-off decides; `IncompleteError`; `ChangedError` (the file changed during a resume and a restart failed too).
- **At most 6 connections per host** in the process, Chrome's own limit, shared by every caller.
- Cookies, signing, pacing and back-off stay the caller's, through `client=`.

This brings back the module name Phase 4 removed (`http` had grown into a client framework). It returns as two verbs and a result type ([D3](#decisions)).

#### `browser`

```python
with browser.open(profile="Default") as chrome:          # Google Chrome; offers Chromium when missing
    tab = chrome.open("https://example.com/", idle=True)  # a new tab (Browser.open → Page)
    tab.goto("https://example.com/feed")                  # the same tab elsewhere; the status, or None
    tab.wait(".grid")                                     # or wait(url="**/feed/"), wait(idle=True)
    with tab.on_response("graphql", lambda data, response: found.append(data)):
        tab.scroll(until=lambda: len(found) > 100)
    tab.fetch("/api/items", headers={"x-csrf": token}).json     # sent by the page itself
    tab.document.css("h1").text, tab.url, tab.title
```

| Today | Redesign |
| --- | --- |
| `connect(*, profile, cdp, headless, viewport, locale, timezone, args, stealth)` | **`browser.open(*, profile=None, cdp=None, headless=False, viewport=(1280, 900), locale=None, timezone=None, args=(), stealth=True) -> Browser`**. `profile` takes an installed profile's name, which it copies itself, or a folder. `headless` defaults to False: what flnd and the user both want |
| `installed_profile(browser, profile)` | **goes**: `open(profile="Default")` does it |
| `Browser.open_page(url, *, idle, timeout)` | **`Browser.open(url=None, *, idle=False, timeout=30.0) -> Page`** ([W3](#21-words)) |
| `Browser.pages`, `Browser.close` | — |
| `Browser.cookies.all`, `.add(jar)`, `.clear_all()` | **`Browser.cookies -> Jar`**, **`Browser.add_cookies(jar)`**, **`Browser.clear_cookies()`** |
| `Page.goto` | — (`int \| None`, Phase 6) |
| `Page.wait_for_selector`, `wait_for_url`, `wait_for_load` | **`Page.wait(css=None, *, url=None, idle=False, timeout=30.0)`**: one verb |
| `Page.on_response(match, handler, *, lines)`, `on_request(match, handler)` | — , returning a **`Subscription`** (`stop()`, `with`) instead of a bare function |
| `Page.scroll(*, by, count, limit, more, idle, delay, pause, max, progress, onscroll)` (11 options) | **`Page.scroll(*, within=None, until=None, count=None, idle=10.0, wait=3.0, pause=0.3, on_step=None) -> int`** (7). `by` → `within`, a CSS selector or a locator; `more` → `until`, inverted to the vocabulary; `limit` and `max` go, since `until` covers both; `delay` → `wait`; `onscroll` → `on_step`; `progress` → `count` |
| `Page.handoff(*, until, message, poll, timeout)` | — |
| `Page.fetch` (Phase 6, a tuple) | returns an **`http.Response`** |
| `Page.selector` | **`Page.document`**: the live page as a `Selection` |
| `Page.url`, `title`, `raw`, `close` | — |

#### `selector`

| Today | Redesign |
| --- | --- |
| `select(source, query="")` | **`selector.parse(source) -> Selection`** ([2.2](#22-one-vocabulary)): HTML, JSON text, bytes, a response, or data already parsed |
| `Selection.css`, `xpath`, `jmespath`, `jsonpath`, `script(id=, pattern=)` | — |
| `first`, `all`, `text`, `raw` | — , plus **`json`**: the text parsed as JSON, as a `Selection` (`page.document.script(id="__NEXT_DATA__").json.jmespath("props")`) |

Text over 64 MB is refused with `selector.UnsafeError`.

#### `kv`

| Today | Redesign |
| --- | --- |
| `kv.open(path, *, table="default") -> KV` | — , returning a **`Store`** (a clearer noun); WAL mode and a 5 s busy timeout, so two `flnd` commands at once don't fail with "database is locked" |
| `KV.get_keys(prefix="")` | **`Store.keys(prefix="")`**: the mapping's own `keys`, with an optional prefix |
| `KV.set(key, value, *, ttl=None)`, mapping operations, `raw`, `close`, `with` | — |
| — | **`Store.table(name) -> Store`**: another table in the same file and connection. flnd keeps its database files as they are, since Phase 6 rules out history migrations |

#### `crypto`

| Today | Redesign |
| --- | --- |
| `hash_md5`, `hash_sha1`, `hash_sha256`, `hash_sha512`, `hash_blake2b` | **`crypto.md5`, `sha1`, `sha256`, `sha512`, `blake2b`**: hashlib's own names. Each takes bytes, text, a `Path` (streamed) or a file object, and returns hex |
| `encrypt_aes_gcm`, `decrypt_aes_gcm` | **`crypto.encrypt(data, key, *, context=b"")`**, **`crypto.decrypt(blob, key, *, context=b"")`**: AES-256-GCM, the only cipher offered. The key is 32 random bytes, which the stdlib's `secrets.token_bytes(32)` gives |
| `encrypt_aes_cbc`, `decrypt_aes_cbc` | **go private**, into `cookies` (Chrome's cookie store, their one user) ([D5](#decisions)) |
| `random_key(size=32)` | **goes from the public API** (user, 2026-11-12). Its one caller is flnd's `auth`, which makes the vault key; it calls the stdlib's `secrets.token_bytes(32)` instead, since the stdlib already does the job. A script that needs a key for `encrypt` does the same |
| `derive_scrypt(password, salt, *, size=32)` | **removed** (user, 2026-11-12): it has no caller, and flnd's keys are random, never typed |
| `CryptoError` | `crypto.Error` |

#### `worker`

| Today | Redesign |
| --- | --- |
| `map(fn, *iterables, **options) -> list` | **`worker.as_ordered`** (user, 2026-11-12): every result in the input's order, as a list, raising `TaskError` when any failed. It's the pair of `as_completed`, so the two names say how they differ |
| `race(fn, *iterables, **options) -> Iterator[Result]` | **`worker.as_completed`**: the stdlib's word for "results as they finish" ([W4](#21-words)) |
| `pool(**options) -> Pool`; `submit`, `results`, `submitted`, `close`, `join`, `raw` | — |
| option `progress="label"` | **`label=`**: `progress` is always a callback ([2.2](#22-one-vocabulary)) |
| options `concurrency`, `retries`, `timeout`, `processes` | — |
| `Result.item`, `.value`, `.error`, `.ok`; `TaskError` | — ; `TaskError` under `worker.Error` |

#### `log`

| Today | Redesign |
| --- | --- |
| `debug`, `info`, `ok`, `warn`, `error`, `level` | — |
| `progress(label, *, total, show) -> ProgressBar` | — , returning a **`Progress`** |
| `ProgressBar.advance(n)`, `update(n, total)`, `finish()`, `fail()`, `done`, `total`, `caption`, `finished` | **`Progress.advance(n=1)`, `finish()`, `fail()`**; settable **`count`**, **`total`** and **`caption`**; **`finished`**. `update` goes, because setting `count` and `total` says it ([S4](#23-shapes)). `done` (a number) becomes `count`, so it can't be read as a boolean |
| `multi(title) -> Group`; `Group.add(label, *, total, show)` | **`log.group(title="")`**, `Group.add` — |

#### `prompt`

Five noun functions become three verbs, the same three flnd's `Login` handle has:

| Today | Redesign |
| --- | --- |
| `input(message, …)`, `secret(message, …)` | **`prompt.ask(message, *, secret=False, default=None, placeholder="", description="", validate=None) -> str`** |
| `confirm(message, …)` | — |
| `choice(message, options, …)`, `choices(message, options, *, min, max, …)` | **`prompt.choose(message, options, *, many=False, default=None, filter=False, min=0, max=None, description="", validate=None)`**: one value, or a list with `many=True` (typed with overloads) |
| `defaults()`, `keymap`, `Keymap` | — |
| `PromptError` | `prompt.Error` |

#### `fmt`, `style`, `theme`, `cli`, `archive`, `bundler`

| Module | Change |
| --- | --- |
| `fmt` | — (`size`, `duration`, `date`, `filename` are already nouns), plus **`fmt.count(n, noun)`**: `"1 file"`, `"3 files"`. flnd prints "1 files" today |
| `style` | `url(target)` becomes **`style.link(url)`**; `rainbow` goes (Phase 6) |
| `theme` | — |
| `cli` | — ; `UsageError` sits under `cli.Error` |
| `archive` | — , plus **`archive.ls(path, *, password=None) -> list[Entry]`** (user, 2026-11-12: the Unix word, as `zip` and `unzip` are), the archive's index without extracting, under the same safety checks (`Entry(name, size, kind)`); the errors sit under `archive.Error` |
| `bundler` | `build(…, out="dist")` becomes `dest="dist"` |
| `_atomic` (private) | becomes public **`atomic`**: `atomic.write(dest, data, *, mode=0o666)` and `with atomic.writing(dest) as part:`. flnd's `files.write_atomic` and `open_new` go |

### C3. The cost of the renames

The only caller is flnd, and the renames are mechanical:
- `selector.select` → `parse`: 41 call sites.
- `media`: about 25.
- `cookies`: about 12.
- `prompt`: 8.
- `crypto`: 6.
- the rest: about 20.

Each module's rename is one commit: the module, its tests, flnd's call sites and the API snapshot together, with `check` green. The snapshot diff is the review.

### C4. Errors

```python
# shared/__init__.py
class Error(Exception): "the base of every error shared raises"
class UnsafeError(Error, ValueError):  "input refused because trusting it could do harm: traversal, a bomb, a decoder exploit"
class BrokenError(Error, ValueError):  "input that is damaged or isn't what it claims: a cut-off video, a bad archive"
class RefusedError(Error):             "a remote end or a person said no: an HTTP 4xx, a wrong password"
```

Every module has `Error(shared.Error)`, and its specific errors sit under it and under the cross-cutting base that fits. For example `class UnsafeError(Error, shared.UnsafeError)` in `archive`, `media`, `cookies` and `selector`.

So a caller can choose its level:
- one module: `except media.Error`;
- one kind of problem across every module: `except shared.UnsafeError`;
- anything from `shared`: `except shared.Error`.

`cli.UsageError` and `worker.TaskError` keep their names, under their module's `Error`.

### C5. Namespace and the API snapshot

- **Flat, 18 modules**, listed in three groups in `shared/__init__.py`'s docstring and plan/README.md:
  - terminal: `style`, `fmt`, `theme`, `log`, `prompt`, `cli`;
  - files and data: `archive`, `atomic`, `crypto`, `kv`, `media`, `bundler`;
  - web: `http`, `browser`, `cookies`, `selector`;
  - plus `worker`.

  Sub-packages were considered and [rejected](#rejected).
- **`media.image`, `media.video` and `media.audio` go** into `Media`'s methods.
- **`__all__` in every module**, and the **API snapshot**: `scripts/dev.py api` writes `tests/data/api.txt`, every public name in `shared`, `flnd.source` and `flnd` with its signature. `tests/test_api.py` fails when the code and the snapshot differ, and `tests/test_names.py` checks the [conventions](#24-enforced) over it.

### C6. Hardening checklist

Each item is one test, written to fail first:

| Area | Rule |
| --- | --- |
| untrusted sizes | `cookies.load` refuses files over 16 MB, and `selector.parse` text over 64 MB. `archive` and `media` limits are unchanged |
| timeouts | every blocking public call takes `timeout=` seconds: `http.fetch`, `http.download`, `Page.wait`, `Page.fetch`, `kv.open`'s busy timeout |
| threads | each public function's docstring says whether it may be called from several threads, and `kv.Store`, `http` and `log` have tests that do |
| files | every write in `shared` goes through `atomic`, and a test greps for `open(..., "w")` outside it |
| temporary files | a `Media` chain leaves no temporary file after `save`, `close`, an exception or collection |
| names from sites | `fmt.filename` is applied to every path part built from remote data (flnd's formats, `archive` entry names, `http.download` into a folder), and a test sends `../`, `CON`, `a:b` and 300-character names through each |
| secrets | no `shared` function puts a cookie value, key or password in an exception message or a log line |
| imports | `from shared import log` loads no third-party module, and neither does `from flnd.source import …` |

---

## Where each part is built

The order is in [README › Order](README.md#order); each step ends with `check` green and is committed on its own, as `<action>:<summary>`:

| Step | Builds |
| --- | --- |
| 4 | Part C, [the `shared` API](#part-c-the-shared-api): the API snapshot, `test_names.py` and the error tree first; then one commit per module, in the order `crypto`, `kv`, `selector`, `cookies`, `worker`, `log`, `prompt`, `browser`, `media`, the small ones, public `atomic`; and `http.fetch` and `http.Response`, which `site.fetch` returns |
| 5 | [A1–A3](#a1-one-module-for-source-authors-flndsourcepy): `flnd.source`, the route matcher, `unsupported`, the generated forms and the `ROUTES` tables, still dispatching to today's `run` |
| 6 | [A4–A11](#a4-categories-are-methods) and [B3](#b3-the-job-one-fixed-pipeline): categories, `Post`/`File`, `Site`/`Login` and the job, with `example_source.py` and `test_job.py` first. Then the sources, one commit each: loi, twt, tik, ins, fbk, tlg. It takes in [session](session.md)'s rules (`goto` checked and paced, scrolling on the page's time, the pace called around every request), [scripts](scripts.md)' helpers (`meta_results`, the DASH picker, `watch_responses`, in `sources/common.py`) and [dates](dates.md) (`Post.date` and the `mtime` postprocessor) |
| 7 | [B1, B2, B4](#b1-one-method-per-command) and [B7](#b7-every-credential-encrypted): `Engine.download/sync/login/logout`, `Report`, `settings.py` and `repair.py`, a thin CLI, the dependency-rule tests; and every credential in the vault |
| 8 | [B6](#b6-what-flnd-takes-from-download-managers-idm) and `http.download`: resume, split downloads, the per-host limit, refreshing expired links, graceful Ctrl-C; live checks on each CDN (fbcdn, cdninstagram, tiktokcdn, twimg, loi's mirrors) for `Range` and 4 connections |
| 10 | [A9 rule 3](#a9-time-and-early-stops-the-engines-job) and [B5](#b5-what-flnd-takes-from-gallery-dl): `sync`'s saved-run stop and `complete` records, cookie write-back, `scripts/dev.py try` |
| 13 | Docs: `flnd/source.py`'s docstring becomes "writing a source"; the README gains the proxy line and the `cron` example; `plan/README.md`'s Naming section becomes the [conventions](#2-api-conventions) |

---

## Decisions

Every decision is now settled (user, 2026-11-12). Also decided by the user on 2026-11-12, and written into the sections above:
- D3, D5, D10, D11 and D12, the step 4 decisions, each as recommended;
- D2, D4, D6, D7, D8 and D13, each as recommended;
- the "download Chromium?" question is asked up front, before any work starts, when a chosen target's source needs a browser, as [cli](cli.md#chrome-only) says. The engine knows that from the routes, so no `Source` field is added; built in step 6;
- every category without `--from`, and `--from` refused for a single post (A4);
- the line ceiling removed;
- `watch_responses` as a source helper, not core (A8);
- every credential encrypted (B7);
- `crypto.key` leaves the public API and `crypto.derive` goes;
- `worker.as_ordered` and `archive.ls`.

1. ~~When to build Part A?~~ **Decided 2026-11-12: inside Phase 6.** The user merged the Phase 7 proposal into Phase 6, so Part A takes the place of the old step 3's architecture part, and nothing is built twice.
2. ~~Rename `session` to `site`?~~ **Decided 2026-11-12: yes.** Asked: rename `session` to `site` (`Session` → `Site`) in every source and test? **Recommended: yes.** "Session" already means httpx's client and Telethon's session string in this code, and the rename is mechanical, done while every source is rewritten anyway.
3. ~~Bring back `shared.http`?~~ **Decided 2026-11-12: yes, bring it back.** Asked: as `fetch`, `download` and `Response` (Phase 4 removed it), or keep resume and split downloads private inside `flnd/net.py`? **Recommended: bring it back.** Fetching and downloading are what a script most often writes badly, and one `Response` type for `http.fetch`, `Page.fetch` and `site.fetch` makes every fetch read the same.
4. ~~Drop `Source.examples`?~~ **Decided 2026-11-12: yes, generated forms.** Asked: drop `Source.examples` for forms generated from the routes? **Recommended: yes**, with `shown=` for the rare pattern that reads badly. `flnd --help` then lists `ins:<user>` rather than `ins:example_user`. The README keeps real-looking examples.
5. ~~Remove CBC from the public API?~~ **Decided 2026-11-12: yes.** Asked: remove `crypto.encrypt_aes_cbc` / `decrypt_aes_cbc` from the public API (CBC moves privately into `cookies`)? **Recommended: yes**: safe by default means the only public cipher is the authenticated one. This goes against Phase 6's api table, which kept CBC as "the pair of `decrypt_aes_cbc`". Changing it is why this is a decision.
6. ~~Split downloads where?~~ **Decided 2026-11-12: only where live checks pass.** Asked: split downloads, live: up to 4 ranges for files of 16 MB or more, from CDNs only, under the 6-per-host limit. Turn on for every source, or only where live checks pass? **Recommended: only where they pass**, as a per-source list in `net.py` that starts empty and grows with each check.
7. ~~A persistent download queue?~~ **Decided 2026-11-12: defer.** Asked: a persistent download queue (IDM-style `pending.db`)? **Recommended: defer.** Resume, the history and `sync`'s saved-run stop cover the need. Reconsider if a live run shows re-listing costing real budget.
8. ~~Graceful first Ctrl-C?~~ **Decided 2026-11-12: yes.** Asked: graceful first Ctrl-C (finish running files, then stop; a second Ctrl-C stops now)? **Recommended: yes.** It changes what Ctrl-C does today, which is to stop at once and drop the queue.
9. ~~Adopt the conventions?~~ **Decided 2026-11-12: yes.** They're the user's request, written as rules `test_names.py` checks.
10. ~~One `Media` object?~~ **Decided 2026-11-12: yes.** Asked: `media` becomes one `Media` object (`media.open(path)` with chained transforms and `save`), replacing its 12 functions and the `image`, `video` and `audio` namespaces? **Recommended: yes.** It's the biggest rename in Part C, about 25 call sites in flnd.
11. ~~`ask`, `confirm` and `choose`?~~ **Decided 2026-11-12: yes.** Asked: `prompt`'s five functions become `ask`, `confirm` and `choose` (`secret=True` and `many=True` as options)? **Recommended: yes**, and flnd's `Login` handle uses the same three.
12. ~~A visible `browser.open`?~~ **Decided 2026-11-12: yes.** Asked: `browser.open` defaults to a visible window (`headless=False`), as flnd already runs it, and `installed_profile` folds into `open(profile="Default")`? **Recommended: yes.**
13. ~~The vault key in the OS keychain?~~ **Decided 2026-11-12: not now.** Asked: keep the vault key in the OS keychain (macOS Keychain, Windows DPAPI, the Secret Service on Linux) instead of `.flnd/secret.key`? Then a copy of the whole `.flnd/` folder holds no usable login. **Recommended: not now.** It needs the `keyring` package or code for each platform, and a Linux desktop without a keyring service would need a fallback anyway. Revisit if `.flnd/` is ever synced or backed up somewhere shared.

---

## Rejected

- **A plugin system or entry points for sources:** flnd has six sources, all in the repository. `SOURCES` stays one explicit list, which is one line to add.
- **One class per route, as gallery-dl has one extractor per pattern:** it spreads one site over six classes and needs `Queue` to hand a profile to its tabs. Routes plus categories as methods keep one site in one class, with no `Queue`.
- **Categories as data (`categories = {...}`) plus `site.wants`:** that's Phase 6's shape. It keeps the rule in 19 places and lets a declared category and the code that serves it drift apart.
- **Two placeholders per pattern:** a target needs one identity for its folder and history.
- **`Post(meta={...})` dicts:** a typo in a key is silent. Keywords fail at the call.
- **`site.paginate` as a `Site` member:** a plain `pages()` function does it without growing the handle.
- **Grouping `shared` into sub-packages** (`shared.web.http`): a level on every import for 18 modules that fit on one line ([C5](#c5-namespace-and-the-api-snapshot)).
- **A lazy `Media` pipeline** that fuses chained steps into one ffmpeg run: faster for long chains, but every step's errors and kinds would surface only at `save`, and flnd's chains are one step long.
- **Keeping both spellings during the renames:** see [S7](#23-shapes).
- **Deprecation aliases for renamed `shared` names:** one user, changed in the same commit.
- **A bandwidth limit, a scheduler, clipboard or browser capture, auto-shutdown:** see [B6](#b6-what-flnd-takes-from-download-managers-idm).
- **gallery-dl's filter expressions, ranges, metadata files and config:** see [B5](#b5-what-flnd-takes-from-gallery-dl).
- **Segmenting dynamically** (IDM splits the biggest remaining range when one finishes): static ranges get most of the gain in a fraction of the code. Revisit only if a live check shows one slow range holding files back.

---

## Tests

New or changed, besides each moved module's existing tests:

- **`tests/test_source.py`**:
  - **patterns**: the five rules; `www.` and `m.` dropped; extra segments, query and fragment ignored; `@` stripped; regex placeholders; `?key=`; `*`.
  - **matching and errors**: order decides; `unsupported` beats a route; a short link is followed once and a second miss is a `TargetError` listing the generated forms.
  - **checks at class creation**: a pattern whose placeholder isn't a parameter, a duplicate pattern, or two placeholders each raise.
  - **categories**: `@route.category` keeps declaration order; without `--from` every category is called; `Source.categories` is derived from the routes; a `Post` field that collides with an engine field raises `TypeError`.
  - **formats**: an empty field and its separator are dropped.
  - **dates**: `day()` reads seconds, milliseconds and digit strings, and refuses 2011 and next-next year.
- **`tests/test_job.py`**, with `example_source.py`:
  - only the wanted categories are called, and the route once per target and not at all when nothing is wanted;
  - `category` is set by the engine;
  - the `newest_first` stop comes after 5 older posts with 3 pinned old ones first, and the generator's `finally` runs;
  - `sync` stops after 5 saved posts only with a `complete` record, and an interrupted listing records none;
  - an expired link calls lazy `files` once more and resumes;
  - the first Ctrl-C finishes running files, and the second stops now, keeping the `.part`.
- **`tests/test_engine.py`**:
  - `download`, `sync`, `login`, `logout` and `Report` (`ok`, `failed`, `files`);
  - `--from` with a post link among the targets raises the usage error before any request, whether the other targets are profiles or not; the same link without `--from` downloads whole;
  - `"off"` against None for a saved setting;
  - `Range.parse` and `Choice.parse` errors are `ValueError`s the CLI shows as usage errors.
- **`tests/test_http.py`**, on an `httpx.MockTransport`:
  - **resume**: a resume after a cut sends `Range` and `If-Range`; a 200 to a range restarts; a `.part.json` from an earlier run resumes;
  - **split and limits**: a split file reassembles byte for byte; there are never more than 6 connections to one host across threads;
  - **errors**: a 429 raises `RateLimitedError` without sleeping; mirrors are tried in order.
- **`tests/test_api.py`** and **`tests/test_names.py`**: the API snapshot and the [conventions](#24-enforced) over it.
- **`tests/test_media.py`**: chains (`trim` then `compress` then `save`), `save()` over the original, `KindError` for trimming a photo, and no temporary file left after `save`, `close`, an exception or collection.
- **`tests/test_cookies.py`**: `Jar.filter`, `merge` (newer wins), `header`, and `save` with mode 0600.
- **`tests/test_auth.py`**:
  - a login stores its jar only in the vault, and no file appears under `.flnd/`;
  - a Phase 6 `.flnd/cookies/ins.txt` is moved into the vault on the first run and then deleted, but is kept when the vault write fails;
  - `logout` clears the jar with the other secrets;
  - a jar copied into another source's slot doesn't decrypt;
  - `crypto` has no `key` or `derive`. **`tests/test_layers.py`**: the four import rules of [B4](#b4-modules-and-dependency-rules).
- **Sources:** each has its `ROUTES` table and `collect()` tests. Every source test that passes before step 2 passes after it, with the same file names and history keys.
- **The [C6](#c6-hardening-checklist) checklist**, one test per row.
- **Live, by hand,** on the [test accounts](README.md#order): one download per source after step 2; one `sync` that stops at saved posts after step 6; and resume, split and refresh on each CDN after step 5.
