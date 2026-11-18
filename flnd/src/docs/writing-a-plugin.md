# Writing a plugin

A **plugin** is the code for one site: one class that says which targets it reads and lists the
posts each target has. flnd does everything else: pacing and retrying requests, downloading,
naming and dating files, skipping what is saved, stopping a sync early, resuming, progress and
logins.

This page builds a small plugin step by step on **plugin API 2**, `flnd.plugin`, tests it with
`flnd.testing`, and says how to add it to flnd. Every `python` block on it runs in the docs
test, so it stays true. `tests/example_plugin.py` is a bigger example to copy from, and the
built-in plugins in `src/plugins/` are real ones. The docstrings of `src/plugin.py` and
`src/testing.py` are the reference. The [glossary](glossary.md) defines the words, and
[architecture.md](architecture.md) shows where a plugin sits in a run.

## 1. The class

The made-up site `example.com` has a JSON API: `api.example.com/users/<name>` gives a profile,
and `api.example.com/users/<id>/posts` gives its posts, newest first, in pages chained by a
`next` cursor.

```python
import asyncio

from flnd.plugin import Ctx, File, Plugin, Post, Request, cookies, route

API = "https://api.example.com"


class Example(Plugin):
    """example.com: a user's photos and videos, from its JSON API."""

    code = "exm"                   # targets are written exm:<user>
    name = "Example"               # the site's name, shown in lists and help
    domains = ("example.com",)     # links on these hosts are its own
    api = 2                        # the plugin API it was written for
    login = cookies("token", url="https://www.example.com/login")
    filename = "{date}_{post}{index}"

    @route("exm:{user}", "example.com/{user}")
    async def user(self, ctx: Ctx, user: str) -> dict:
        """A profile: `exm:example_user`, or `https://www.example.com/example_user/`."""
        return (await ctx.fetch(f"{API}/users/{user}")).json

    @user.category
    async def posts(self, ctx: Ctx, profile: dict):
        """The profile's posts, newest first, page by page."""
        async for page in ctx.pages(f"{API}/users/{profile['id']}/posts", cursor="next"):
            for item in page.json["items"]:
                files = [File(url) for url in item["urls"]]
                yield Post(item["id"], files, date=item["time"])

    def sign(self, request: Request, cookies) -> Request:
        """The login's token as a Bearer header, on the plugin's own domains only."""
        if not cookies.get("token"):
            return request
        token = cookies["token"]
        return request.replace(headers={**(request.headers or {}),
                                        "Authorization": f"Bearer {token}"})
```

What each part does:

- **`code`, `name`, `api` and one route** are required. `code` is what targets start with: 2 to
  16 lowercase letters and digits, starting with a letter. `api = 2` says which plugin API it
  was written for (section 10).
- **`domains`** are the plugin's own hosts and their subdomains. A link on one belongs to the
  plugin. A request to one is a page job: paced, signed by `sign`, and sent with the account's
  cookies. A 401 or 403 from one is a `LoginError`.
- **`login`** says how it logs in. `cookies("token", url=…)` is logged in when one of those
  cookies is set; `url` is the site's login page. Its methods are `browser` (flnd's own window
  on that page), `chrome`, `firefox`, `safari`, `edge`, `brave`, `chromium` (a browser's cookie
  store) and `file` (a cookies.txt). `cookies(..., required=True)` means nothing can be listed
  without a login. A site with its own flow assigns an instance of a `Login` subclass
  (section 8). No `login` means the plugin needs none.
- **`pace`** (0.5 by default) is the least time, in seconds, between two page requests of one
  bucket of one account. A bucket is a host by default; `ctx.fetch(..., bucket="search")` puts
  a request in a bucket of its own.
- **`limits`** caps the files downloading at once from a host and its subdomains, such as
  `limits = {"cdn.example.com": 4}`. Every host's cap adapts: it starts at 8 (or the host's own
  cap, when lower), halves when the host answers 429 or 503, and grows while its throughput
  still rises, up to 32 for a host not named and up to its own cap for one that is. Name a host
  only when it is known to refuse more.
- **`@route(...)`** makes `user` a **route**: a method that reads one kind of target. Each
  pattern is one way to write it: `exm:{user}` after the code, `example.com/{user}` as a link.
  The method's name, `user`, is the target's kind, and its third parameter is named like the
  pattern's placeholder.
- **A route with categories returns what they receive.** It is an `async def` that returns a
  value, such as the profile's JSON, fetched once per target. `@user.category` makes `posts`
  one of its categories: an `async def` that yields `Post`s (and `Target`s, section 6). A route
  without categories, such as a single post's link, yields its posts itself.
- **`newest_first`** promises that the listing comes newest first. It is on by default:
  `@route(..., newest_first=False)` turns it off for a route and the categories that don't say,
  and `@user.category(newest_first=False)` for one category. Only a newest-first listing lets a
  sync stop early, and `--when` stop after 5 posts older than its range. Without it, flnd reads
  the whole listing: slower, but never wrong.
- **`ctx.pages(request, cursor=…, send=…)`** walks a paged listing and yields each page's
  `Response`. `cursor` is a `dig` path into each page's JSON (`"next"`,
  `"data.page_info.end_cursor"`), or a function from the `Response` to the next cursor. `send`
  is the query parameter the cursor goes in (`"cursor"` by default), or a function from the
  cursor to more params. The listing ends on a missing, empty or repeated cursor, or on an empty
  page (no body, or JSON that is null, `{}` or `[]`). flnd keeps the cursor of the category's
  first `pages`, and a run cut short resumes there.
- **`sign(request, cookies)`** runs before each try of a request to the plugin's domains, with
  the account's cookies on them. It returns the request, with headers or a signature added
  (`request.replace(headers=…)`). It is a plain `def`, never `async def`.

A plugin never stops a listing itself. flnd owns the early stops: a sync of a newest-first
listing stops after 5 saved posts in a row (the home's `config.toml` changes the number, as
`stop_after`; see [storage.md](storage.md#settings-configtoml)), or sooner, after the newest
post an earlier run saw and the post after it. `--when` stops a newest-first listing after 5
posts in a row older than its range. A plugin just yields, and flnd closes its generator, which
ends the page reads.

## 2. Mistakes fail when the class is made

flnd checks the class when it is made, so a broken plugin never starts a run. Each mistake is a
`PluginError` worded `<code>.<attribute>: <what>; <the fix>`:

```python
from flnd.plugin import PluginError

try:
    class Broken(Plugin):
        code, name, api, domains = "brk", "Broken", 2, ("example.com",)
        folder = "{target}/{usr}"

        @route("brk:{user}")
        async def user(self, ctx, user):
            yield Post(user)
except PluginError as error:
    message, hint = str(error), error.hint

assert message.startswith("brk.folder: unknown field {usr}; fields are plugin, target, category")
assert hint == "list a post field in brk.fields to use it"
```

It also refuses:

- a missing or malformed `code`, `name` or `api`, `domains` that aren't a tuple of lowercase
  hosts, or no route;
- a link pattern off the plugin's domains, a pattern given twice, and a code pattern that
  doesn't start with the plugin's code;
- a pattern with other than one placeholder, or one named unlike the method's value parameter
  (a `(pattern, value)` pair combines several, section 3), and a route or category method that
  doesn't take `(self, ctx, value)`;
- a route with categories that isn't an `async def` returning a value, and a route without
  categories or a category that isn't an `async def` that yields;
- `single=True` with `discovers=True`, and a route or category named like a hook or a
  declaration (`sign`, `date`, `pace`, …);
- a template that leaves the target's folder (`..`, `/` first, a backslash) or names a field it
  doesn't know, and a `fields` name the engine sets itself;
- a declared `routes`, `forms` or `categories`, which flnd derives;
- a wrong `login`, `pace`, `limits`, `browser`, `stealth`, `user_agent`, `walls` or
  `unsupported`;
- a `Settings` field without a default, or without its line under `Attributes:`;
- a hook that is `async` when it shouldn't be (or the other way), or takes other parameters,
  and a `resolve` that isn't a `classmethod`.

## 3. The forms it reads

flnd builds the help's forms from the patterns, and `resolve` reads a target through them:

```python
assert Example.forms == ("exm:<user>", "https://www.example.com/<user>/")
assert Example.categories == ("posts",)

found = Example.resolve("https://www.example.com/example_user/")
assert (found.plugin, found.kind, found.value) == ("exm", "user", "example_user")
assert str(found) == "exm:example_user"
assert Example.resolve("https://www.example.com/") is None
```

`resolve` is a class method. It gives a `Target` whose `kind` is the route that reads it, or
None when no route does. The code is matched in any case (`EXM:example_user`).

A link pattern matches a link's first path parts, and later parts are allowed, so
`example.com/{user}` also reads `example.com/example_user/tagged`. A link needs no scheme when
it has a slash. `www.`, `m.`, `mobile.`, `web.`, `mbasic.` and `touch.` are dropped from the
host first. Routes are tried in the order the class declares them, and the first match wins,
so a narrower route comes before a catch-all one. A placeholder without a regex matches a user
name (letters, digits, `_` and `.`, one leading `@` dropped); `{id:[A-Za-z0-9]+}` gives its
own regex, and `*` matches any one path segment:

```python
class Ordered(Plugin):
    code, name, api, domains = "ord", "Ordered", 2, ("example.com",)

    @route("example.com/p/{id:[A-Za-z0-9]+}", single=True)
    async def post(self, ctx, id):
        yield Post(id, [File(f"https://cdn.example.com/{id}.jpg")])

    @route("ord:{user}", "example.com/{user}")
    async def user(self, ctx, user):
        async for page in ctx.pages(f"{API}/users/{user}/posts", cursor="next"):
            for item in page.json["items"]:
                yield Post(item["id"], [File(item["url"])])


assert Ordered.resolve("https://m.example.com/p/Abc123").kind == "post"
assert Ordered.resolve("example.com/example_user/tagged").value == "example_user"
assert Ordered.resolve("@example_user").kind == "user"
```

A route's options say what kind of target it reads:

- **`single=True`**: it reads one post (a post's or a video's link), which is never followed.
- **A query route**, `@route("exm?{words}")`, reads `exm?<words>`. It yields the `Target`s it
  finds, and each runs as its own target. A route that isn't a query and does the same says
  `discovers=True`.
- **A `(pattern, value)` pair** gives a target's value from several placeholders:
  `("example.com/{user}/album/{album:\\d+}", "{user}/{album}")`. The method's value parameter
  then needn't match a placeholder's name.
- **`shown=`** is the form help shows instead of the first pattern's, for text no pattern can
  say, and **`folder=`** its files' folder (section 5).

A category's options, as `@user.category(own=False, url="https://www.example.com/{id}/tagged")`:

- **`own`** (True by default) is the account's own content, taken by default. `own=False` is
  other people's, such as a profile's tagged posts: taken only when named, or when every
  category is asked for (`all`).
- **`url=`** is a template over what the route returned: the page the engine opens ahead in a
  warm tab, for a category that opens it. A key the profile lacks gives no page.
- **`newest_first=`** overrides the route's (section 1), and **`folder=`** and **`key=`** give
  its files' own folder and history key (section 5).

A link the plugin shouldn't read, such as a page with nothing to download, goes in
`unsupported = {pattern: reason}`. It is refused with a `UsageError` giving the reason, unless a
route pattern with more literal parts matches too. A refusal's pattern captures nothing: it
writes `*` for a part that varies.

```python
from flnd.plugin import Target, UsageError


class Search(Plugin):
    code, name, api, domains = "sch", "Search", 2, ("example.com",)
    unsupported = {"example.com/settings": "a settings page has nothing to download"}

    @route("sch?{words}")
    async def search(self, ctx, words):
        found = (await ctx.fetch(Request(f"{API}/search", params={"q": words}))).json
        for user in found["users"]:
            yield Target(f"https://www.example.com/{user}/")

    @route(("example.com/{user}/album/{album:\\d+}", "{user}/{album}"), single=True)
    async def album(self, ctx, value):
        yield Post(value, [File(f"https://cdn.example.com/{value}.jpg")])

    @route("sch:{user}", "example.com/{user}")
    async def user(self, ctx, user):
        yield Post(user, [File(f"https://cdn.example.com/{user}.jpg")])


assert str(Search.resolve("sch?cats and dogs")) == "sch?cats and dogs"
assert Search.resolve("https://www.example.com/example_user/album/12").value == "example_user/12"
try:
    Search.resolve("https://www.example.com/settings")
except UsageError as error:
    refused = str(error)
assert refused == "sch: a settings page has nothing to download"
```

A site whose targets no pattern can say overrides `resolve` as a `classmethod`, and passes the
rest on to `super().resolve(text)` (section 8).

## 4. Testing it without the network

`flnd.testing` is the test kit. `collect` runs a plugin on a `FakeCtx`, a fake of its `ctx`
that answers each request from `routes`, keyed by URL. It runs the route and its categories as
the engine would, and lists every file a download would save, with its path, history key and
day. Nothing is downloaded, and no home is touched.

```python
from flnd.testing import FakeCtx, Reply, check, collect

ctx = FakeCtx(Example, cookies={"token": "example-token"}, routes={
    f"{API}/users/example_user": Reply({"id": "42"}),
    f"{API}/users/42/posts": Reply({"items": [
        {"id": "p1", "time": "2026-01-02",
         "urls": ["https://cdn.example.com/p1.jpg", "https://cdn.example.com/p1.mp4"]}]}),
})
saved = collect(Example, "exm:example_user", ctx=ctx)
assert [file.path for file in saved] == ["posts/2026-01-02_p1_0.jpg", "posts/2026-01-02_p1_1.mp4"]
assert [file.key for file in saved] == ["p1_0", "p1_1"]
assert str(saved[0].date) == "2026-01-02"

method, url, options = ctx.seen.requests[0]
assert (method, url) == ("GET", f"{API}/users/example_user")
assert options["headers"] == {"Authorization": "Bearer example-token"}
```

`collect(plugin, target, ctx=…, categories=…, limit=…)` lists the route's `own` categories by
default (section 3), the named ones with `categories=["posts"]`, and every one with
`categories=["all"]`. `limit` is the most posts to take, or a discovering route's targets. A
`Target` the listing yields isn't followed. A file listed twice (the same path or key) is
listed once.

A route may also be a function of the `Request` giving a `Reply`, to answer each page of a
listing by its cursor:

```python
def posts_page(request):
    if (request.params or {}).get("cursor") == "c2":
        return Reply({"items": [{"id": "p1", "time": "2026-01-01",
                                 "urls": ["https://cdn.example.com/p1.jpg"]}]})
    return Reply({"items": [{"id": "p2", "time": "2026-01-02",
                             "urls": ["https://cdn.example.com/p2.jpg"]}], "next": "c2"})


ctx = FakeCtx(Example, routes={f"{API}/users/example_user": Reply({"id": "42"}),
                               f"{API}/users/42/posts": posts_page})
assert [file.key for file in collect(Example, "exm:example_user", ctx=ctx)] == ["p2_0", "p1_0"]
```

`check(Example)` checks the class again, and resolves every example target its routes'
docstrings give in backticks (`exm:example_user`, `exm?words`, or a link on its domains). Each
must be read by the route whose docstring gives it, so the examples in a docstring can't go
stale. A form with a placeholder (`exm:<user>`) is no example, and is skipped.

```python
assert [str(found) for found in check(Example)] == ["exm:example_user", "exm:example_user"]
```

### What `FakeCtx` does as the engine does

A `FakeCtx`'s requests, tabs and calls run as jobs of the engine's own scheduler
(`flnd.engine.jobs`), on a fake clock, so the pace, holds, block rounds, wall waits and retries
are the engine's, and pass at once. Every hold is recorded in `ctx.seen.holds`, as
`(reason, seconds, bucket)`.

- `routes` holds the HTTP answers by URL (whole, or without its query), each a `Reply` or a
  function of the `Request` giving one. A `Reply`'s body is bytes as they are, text as UTF-8
  HTML, or anything else as JSON. `pages` holds what `open` shows, each a `FakePage` (or its
  HTML) with its HTML, status, landing URL and feed responses. A URL in neither fails the test
  with an `AssertionError`.
- A request to the plugin's domains is signed, with the plugin's `user_agent` first when it has
  one. `ctx.seen.requests` records each as `(method, url, options)`, with the signed headers.
- With the request's `check` on, a 404 or 410 raises `NotFound`, a 401 or 403 from the
  plugin's domains `LoginError`, and another refusal `SiteError`. With it off
  (`Request(url, check=False)`), the refusal's answer is returned. A 5xx is the server's
  trouble, not a refusal: it is retried with the engine's back-off either way, and with `check`
  off the last answer is returned.
- Before that, seconds from the plugin's `blocked` hook are a block round, and a 429 holds its
  bucket for its `Retry-After` or `x-rate-limit-reset`; the third 429 in a row is a block
  round. A round holds for the larger of the seconds the site asked for and 30 s, 2 min, 8 min
  for the rounds in a row, and the fourth round raises `Blocked`. `ctx.hold` takes a round the
  same way, and waits it out on the clock before it returns.
- Landing on one of the plugin's `walls`, as `open`'s page or a fetch redirected there, holds
  30 s, then 120 s, then raises `LoginError`. A request sent to a wall's path (a login form's
  POST) is no wall.
- `pages` keeps its cursor through a hold, so a 429 asks for the same page again.
- `ask` pops `answers`: one of `choices` when it has any, and `UsageError` when they run out.
  `extension` and `storage` read their own fakes, `settings` sets fields of the plugin's
  `Settings`, and `account` names the account (None runs with no login).
- `state` takes JSON values only, and `secrets` may be written only while `Login.start` runs,
  by it or by the `asyncio` tasks, `ctx.call`s and `asyncio.to_thread`s it starts (not by a
  plain `threading.Thread`, which starts without its context).

```python
from flnd.testing import fake_clock

ctx = FakeCtx(Example, routes={f"{API}/gone": Reply(status=404)})
with fake_clock() as clock:
    asyncio.run(ctx.pause(5, "the page settles"))
    asyncio.run(ctx.hold("the page said to slow down"))
assert clock.slept == [5, 30.0]
assert ctx.seen.holds == [("the page said to slow down", 30.0, None)]
assert asyncio.run(ctx.fetch(Request(f"{API}/gone", check=False))).status == 404
```

`fake_clock()` gives a clock that only moves when something sleeps on it, for the `with`
block: `now`, `wall` (Unix seconds from 2026-01-01 UTC), `slept` (each wait's seconds),
`sleep(seconds)` and `advance(seconds)`. A `FakeCtx` uses the active one, or a fake clock of
its own.

`collect` runs the target to its end, and checks the listing as it goes: a newest-first
listing's dated posts must not get newer, and a post must give every field a template names. A
pinned post (`Post(..., pinned=True)`) is outside the date order, so the check skips it. A
`File.name` with its own extension (up to 7 characters, as `.torrent`) keeps it, and a stem
takes its extension from the content type, then from the URL's extension, then none. The
content type is the `Content-Type` of the file URL's route when it names an image, a video or
an audio file; a function route isn't called for it, but its last answer to the plugin is read.
When a lazy post's files raise `NotFound`, `Changed` or `Hidden`, that post fails alone:
`collect` records it in `ctx.seen.failed` and goes on with the listing.

```python
class Unordered(Plugin):
    code, name, api, domains = "uno", "Unordered", 2, ("example.com",)

    @route("uno:{user}")
    async def user(self, ctx, user):
        yield Post("old", date="2026-01-01")
        yield Post("new", date="2026-01-02")


try:
    collect(Unordered, "uno:example_user")
except PluginError as error:
    message, hint = str(error), error.hint
assert "post new (2026-01-02) is newer than one before it (2026-01-01)" in message
assert hint == "list newest first, or set newest_first=False"
```

### Feeds and clicks

A feed hears only what the tab's page sends after `feed()` is called, as a real tab does, so it
listens before `open` or `click`. What a page sends is its `feeds`: a `(URL, JSON)` pair is
sent as the page loads, `Sent(url, body, click=css)` when `ctx.click(css)` runs, and
`Sent(url, body, scroll=n)` on the page's n-th scroll by a scrolling feed. A response nobody
listens for is lost. A scroll that brings a feed nothing ends it, and so does its `cursor`
reader reading the end. A lazy post's files, the `date` hook and `refresh` open their pages in
a tab of their own, so they never replace the listing's page.

```python
from flnd.testing import FakePage, Sent

feeds = [("https://www.example.com/api/feed?page=1", {"items": [1]}),
         Sent("https://www.example.com/api/feed?page=2", {"items": [2]}, scroll=1),
         Sent("https://www.example.com/api/more", {"items": [3]}, click="button.more")]
page = FakePage("<button class=more>More</button>", feeds=feeds)
ctx = FakeCtx(Example, pages={"https://www.example.com/feed": page})


async def read():
    async with ctx.feed("/api/feed") as pages:
        await ctx.open("https://www.example.com/feed")
        found = [page["items"] async for page in pages]
    async with ctx.feed("/api/more", scroll=False) as more:
        await ctx.click("button.more")
        found += [page["items"] async for page in more]
    return found


assert asyncio.run(read()) == [[1], [2], [3]]
assert ctx.seen.visited == ["https://www.example.com/feed"]
```

Each built-in plugin has a test like this in `tests/`, and a plugin's own test looks the same.
Live runs against the real site are done by hand, on test accounts, never in a unit test.

## 5. Where files go

A file's path under the target's folder, `.flnd/media/<code>/<target>/`, comes from three
templates on the class. A route or a category may give its own `folder`, and a category its own
`key`; the category's wins over the route's, and the route's over the class's.

| attribute | default | what it names |
| --- | --- | --- |
| `folder` | `{category}` | the folder under the target's folder |
| `filename` | `{post}_{num}` | the file's name; the content adds the extension |
| `key` | `{post}_{num}` | the history key, which says a file is already saved |

A template may use the fields the engine sets: `plugin`, `target` (the target's value),
`category`, `post` (the post's id), `num` (the file's place in its post, from 0), `count`,
`index` (`""` for a one-file post, else `_<num>`), `extension`, `path` (in `key` only: the
file's path), `date` and `query` (a query target's words). It may also use the post fields the
plugin lists in `fields = ("album", …)`, which every post must then give (None when it has
none). A field that is empty drops the separator beside it, and a folder level that comes out
empty is left out, so `{date}_{post}` of an undated post is just `<post>`, and
`{category}/{album}` without an album is just the category. Every folder and name is made safe
for every OS. A single route's files have no category, so they sit in the target's folder
itself, as do the files of a `folder = ""`.

The plugin above saves `.flnd/media/exm/example_user/posts/2026-01-02_p1_0.jpg`. Changing `key`
makes every saved file look new: never change it for a plugin in use.

## 6. Posts, files and targets

```python
Post("p2", [File("https://cdn.example.com/p2.jpg")], date=1767312000, title="Example title")
File("https://cdn.example.com/v.mp4", audio="https://cdn.example.com/a.mp4")  # joined by ffmpeg
File(mirrors=["https://cdn.example.com/a.jpg", "https://mirror.example.com/a.jpg"])
File("https://cdn.example.com/x.7z", password="example", size=1024)   # an archive, unpacked
File("https://cdn.example.com/c", name="cover")                         # named, not templated
File.text("info.md", "# Example title\n")                              # written by the plugin
File.stream("https://cdn.example.com/live.m3u8", headers={"Referer": "https://example.com/"})
Target("https://www.example.com/example_user/")                        # run as its own target


async def album_files():
    return [File("https://cdn.example.com/a1.jpg")]

assert Post("a1", album_files).lazy                  # files listed only if the post is new
```

- **`Post(id, files, date=…, listed=…, headers=…, pinned=…, **fields)`**. `id` is the post's
  id as the site has it. `date` takes a date, a datetime, Unix seconds or milliseconds (as a
  number or digits), or an ISO 8601 text; flnd reads it once, and a plugin never parses a date
  itself. An undated post is always kept by `--when`, and its files get the day the plugin's
  `date` hook finds, if it has one. `listed` is the date the listing is ordered by, when it
  isn't `date`. `pinned=True` marks a post the site shows above its date order. The other
  keywords are the site's own fields (`title`, `caption`, `album`), for templates; a name the
  engine sets (`post`, `date`, `files`, …) is refused.
- **`files`** may be an async function giving the files. It is called only for a post that
  isn't saved yet (a lazy post), so a plugin never spends a request on a post flnd already
  has.
- **`headers`** (`Post(..., headers={"Referer": ...})`) are sent with each of the post's files,
  for a CDN that wants them.
- **`File`** takes exactly one of a `url`, `mirrors` (URLs of the same file, tried in order) and
  `fetch`. `audio` is a DASH audio stream joined with the video. `name` replaces the name
  `filename` makes: a name with its own extension keeps it, a stem gets its extension from the
  content type, else from the URL. `size` is the size in bytes, when the site says it, and
  `password` an archive's password: it is unpacked with it once saved.
- **`File.text(name, text)`** is a text file the plugin writes itself, such as `info.md`, saved
  atomically as UTF-8. **`File.stream(playlist, headers=…)`** is a segmented stream, an HLS
  `.m3u8` or DASH `.mpd` playlist fetched in segments.
- **`File.dash(manifest, height=…)`** turns an inline DASH manifest (MPD XML) into one
  `File(video, audio=audio)` of its best video, at or under `height`, and its best audio. It is
  None when there is no manifest, it isn't XML, it is segmented, or it lacks a video or an
  audio with an https URL.
- **`Target(text)`** takes any target, of any plugin. A query route yields its results this way,
  and so may a post that links to another site: flnd resolves it, and runs it as its own target.
  A route that yields targets without being a query says `discovers=True`.

```python
manifest = """<MPD><Period>
<AdaptationSet mimeType="video/mp4">
<Representation height="720" width="1280" bandwidth="900000">
<BaseURL>https://cdn.example.com/v720.mp4</BaseURL></Representation>
<Representation height="360" width="640" bandwidth="300000">
<BaseURL>https://cdn.example.com/v360.mp4</BaseURL></Representation>
</AdaptationSet>
<AdaptationSet mimeType="audio/mp4"><Representation bandwidth="128000">
<BaseURL>https://cdn.example.com/a.mp4</BaseURL></Representation></AdaptationSet>
</Period></MPD>"""

best_pair = File.dash(manifest)
assert (best_pair.url, best_pair.audio) == ("https://cdn.example.com/v720.mp4",
                                            "https://cdn.example.com/a.mp4")
assert File.dash(manifest, height=480).url == "https://cdn.example.com/v360.mp4"
assert File.dash(None) is None
```

### A file with no URL: `fetch` and `Part`

A file the plugin fetches itself, such as one from an SDK, is `File(fetch=fn)`. The engine calls
`fn(part)` with a `Part`: it writes the bytes from `part.offset` until `part.end` (None for the
file's end) with `await part.write(data)`, and says the whole size with `part.total(size)` once
it knows it. `ranged=True` says `fn` can start and stop anywhere, so the engine may split the
file into parts. A write past `end` raises `ValueError`.

```python
from flnd.plugin import Part

DATA = b"0123456789"


async def served(part):
    part.total(len(DATA))
    await part.write(DATA[part.offset:part.end])


written, sizes = [], []


async def sink(data):
    written.append(data)


asyncio.run(served(Part(sink, offset=2, end=6, sized=sizes.append)))
assert (written, sizes) == ([b"2345"], [10])
assert File(fetch=served, ranged=True, name="data.bin").url is None
```

## 7. What `Ctx` offers

A route acts on the site only through `ctx`, which `flnd.plugin.Ctx` describes in full. Each
call is a scheduler job (a page request, a tab, a call in the thread pool), paced, retried and
held for it.

HTTP:

- **`await ctx.fetch(request, bucket=…, in_page=…)`**: one request, a URL or a
  `Request(url, method=…, params=…, body=…, headers=…, check=…)`. A `body` of bytes or text goes
  as it is, anything else as JSON. On the plugin's domains it is a page job, paced in its
  bucket; elsewhere a file job, under its host's cap. `in_page=True` sends it from the
  account's logged-in tab, as the page's own script would. It returns a `Response`: `.status`,
  `.url` (after redirects), `.headers`, `.content`, `.text`, `.json`, `.ok` and `.html`. A 404
  or 410 raises `NotFound`, a 401 or 403 from the plugin's domains `LoginError`, and another
  refusal `SiteError`; with `check=False` the refusal's answer is returned instead. A block
  still there after the holds raises `Blocked`, and no answer after the tries `NetworkError`.
  An answer over 32 MiB is refused: a file comes as a `File`, never through `fetch`.
- **`ctx.pages(request, cursor=…, send=…, bucket=…, in_page=…)`**: a paged listing, resumed
  by flnd (section 1).

The tab:

- **`await ctx.open(url)`**: load a page in the target's tab, and return where it landed, a
  `Landed(url, status)`. A 404 raises `NotFound`; one of the plugin's `walls`, after the
  wall's waits, `LoginError`.
- **`ctx.feed(match, scroll=…, lines=…, cursor=…)`** and **`ctx.load(url, match, …)`**: the
  JSON of the tab's responses whose URL matches, as they arrive (below).
- **`await ctx.document()`**: the tab's page now, parsed (below).
- **`await ctx.click(css, timeout=10.0)`**: click the first element `css` matches, once it is
  clickable; `NetworkError` when nothing clickable matched in time.
- **`await ctx.warm(urls)`**: start loading pages the listing opens next in warm tabs, while
  the run has room, for pages no category's `url=` names (an album's pages). `open` of one then
  takes its tab. An empty URL opens nothing.
- **`await ctx.handoff(message, until=…, timeout=…)`**: show `message`, and wait for a person to
  do a step in a visible window, such as a captcha. `until` is an async function that is True
  once the step is done. It returns True when `until` came true, False when `timeout` passed
  first.
- **`await ctx.extension(op, **args)`**: one operation of flnd's browser extension in the
  user's Chrome ([extension.md](extension.md)). `UsageError` when the extension isn't
  connected. Not built yet in a run: the engine's relay to the extension isn't wired in, so a
  run always raises the `UsageError` for now.
- **`await ctx.storage(origin)`**: `origin`'s `localStorage` in the user's browser, such as a
  web app's session.

Waits, threads and people:

- **`await ctx.pause(seconds, reason)`**: wait on the run's clock, as a page that needs time
  to settle does.
- **`await ctx.hold(reason, seconds=…, bucket=…)`**: hold the plugin's requests for a block it
  read itself (in a feed, a page or an SDK's answer). It is a block round (section 4): the
  engine holds the bucket, or switches accounts, by its block rules. It returns once the hold
  has ended, so a listing that reads a feed or an SDK goes on only then.
- **`await ctx.call(fn, *args)`**: run a blocking function in the thread pool, such as an
  SDK's, and return its value.
- **`await ctx.ask(message, secret=…, description=…, choices=…)`**: ask the person a question.
  Only `flnd login` asks, in a terminal; anywhere else it raises `UsageError`.
- **`ctx.log(level, message, **fields)`**: a line about the target, at `"debug"`, `"info"` or
  `"warn"`, its `{name}` fields filled from `fields`.

What it reads:

- **`ctx.target`**: the target being run, a `Target` with `.plugin`, `.kind`, `.value` and
  `.query`.
- **`ctx.when`**: the run's `--when`, a `Range(since, until)`. `value in ctx.when` tests any
  date a `Post` takes, for a site that can ask for a range itself; an undated value is in it.
- **`ctx.time`**: the run's clock, `now()`, `wall()` and `day()` (UTC). A plugin never reads
  `time` or `datetime.now` itself, so a test's fake clock holds.
- **`ctx.settings`**: the plugin's `Settings` (section 8).
- **`ctx.account`**: the `Account` the target runs as, None without a login. Not built yet in a
  run: accounts aren't rows of their own yet, so it is None for now.
- **`ctx.cookies`**: the account's cookies on the plugin's domains, read-only.
- **`ctx.state`**: the plugin's own table in `flnd.db`, kept between runs. It takes JSON values
  only (lists, dicts with text keys, text, numbers, booleans, None); anything else is a
  `PluginError`. A run reads it once and writes what changed back as each target ends, so a
  read or a write costs no database call.
- **`ctx.secrets`**: the account's sealed secrets, kept in the home's vault. Anyone may read
  them; only `Login.start` may write them.

A plugin never sleeps, retries, writes files, touches the history, or decides when a listing
stops. Its code is `async def`, and it awaits: a listing that runs 10 s without awaiting holds
up the whole run, and is warned about. Blocking work goes through `ctx.call`.

```python
ctx = FakeCtx(Example, state={})
ctx.state["seen"] = ["p1"]
try:
    ctx.state["ids"] = {"p1", "p2"}
except PluginError as error:
    message = str(error)
assert message.startswith("state['ids']: set isn't a JSON value")
ctx.log("info", "{n} stories", n=3)
assert ctx.seen.logs == [("info", "3 stories")]
assert asyncio.run(ctx.call(sum, [1, 2])) == 3
assert "2026-01-02" in ctx.when
```

### Reading a page and its JSON

`response.html` parses an answer's body as HTML, and `await ctx.document()` parses the tab's
page now; both give a `Document`, with CSS queries and the JSON the page embeds. There is no
XPath, and no `::text` or `::attr()`.

| on the page | gives |
| --- | --- |
| `page.css(query)` | every element that matches the CSS selector, in page order (a list) |
| `page.first(query)` | the first match, or None |
| `element.text` | all its text, whitespace collapsed |
| `element.attr(name)` | one attribute's value, or None |
| `page.script(id)` | the JSON in `<script id="…">`, or None; `Changed` when it isn't JSON |
| `page.blocks(contains)` | the JSON of each `<script type="application/json">` with `contains` |
| `page.visible` | the text a reader sees, without scripts and styles |

`meta(document)` gives the JSON of every `<script type="application/json">` block, with each
`__bbox.result` it holds unwrapped, as Meta's pages embed them. `dig(data, path)` walks the
decoded JSON. A path is a few forms, chained:

- `a.b` reads a key, and `[0]` an index (`[-1]` is the last);
- `[]` reads the rest from every item of a list, and joins the lists it reaches:
  `edges[].node.media[]` is every media of every node;
- `..[field=value]` finds every dict, at any depth, whose `field` is the text `value`, and reads
  the rest from each, as `[]` does;
- `a || b` gives the first alternative that finds something (not None, "", [] or {}).

A missing key gives None, and a path with `[]` or `..[]` gives a list, empty when nothing
matched. A malformed path raises `ValueError`, which fails the target as the plugin's bug.

```python
from flnd.plugin import dig, meta

HTML = """<a href="/video/1">one</a><a href="/about">about</a>
<script id="__NEXT_DATA__" type="application/json">
{"props": {"items": [{"__typename": "Video", "id": "1"}, {"__typename": "Ad"}]}}
</script>
<script type="application/json">{"require": [{"__bbox": {"result": {"id": "2"}}}]}</script>"""

ctx = FakeCtx(Example, routes={"https://www.example.com/videos": Reply(HTML)})
page = asyncio.run(ctx.fetch("https://www.example.com/videos")).html

assert [a.attr("href") for a in page.css('a[href*="/video/"]')] == ["/video/1"]
data = page.script("__NEXT_DATA__")
assert dig(data, "props.items[].id") == ["1"]
assert dig(data, "..[__typename=Video].id") == ["1"]
assert dig(data, "props.next || props.items[0].id") == "1"
assert meta(page)[1] == {"id": "2"}
```

More helpers come with `flnd.plugin`:

- **`best(streams, height=…)`**: of a list of `Stream(url, height=…, width=…, bitrate=…,
  audio=…)`, the one with the most pixels, then the most bits, at or under `height`; heights are
  compared when a stream lacks a width. None when none fits.
- **`numbered(n, suffix=…, width=2)`**: `n` zero-padded, then `suffix`, as given.
- **`wall(url, walls)`**: the wall of `walls` that `url` lands on, matched by whole path
  segments, or None. The engine and `FakeCtx` both ask it.
- **`page_info(path)`**: a feed's `cursor` reader for a GraphQL
  `{has_next_page, end_cursor}` object at a `dig` path (below).

```python
from flnd.plugin import Stream, best, numbered, wall

streams = [Stream("https://cdn.example.com/720.mp4", height=720, width=1280),
           Stream("https://cdn.example.com/1080.mp4", height=1080, width=1920)]
assert best(streams).url == "https://cdn.example.com/1080.mp4"
assert best(streams, height=720).url == "https://cdn.example.com/720.mp4"
assert best(streams, height=480) is None
assert (numbered(7), numbered(7, suffix="_cover", width=3)) == ("07", "007_cover")
assert wall("https://www.example.com/challenge/1", ("/challenge",)) == "/challenge"
assert wall("https://www.example.com/challengers/", ("/challenge",)) is None
```

### A feed in the browser: name its responses and its cursor

A site that only shows its data to a logged-in page is read from the page's own API calls. The
plugin names two things, and flnd does the scrolling:

- **the feed's filter**, `match`: a piece of the URL (`"/graphql"`), a compiled regex searched in
  it, or a function of the URL, which picks the responses that carry the listing;
- **its cursor reader**, `cursor`: a small function given each parsed response (each line, with
  `lines=True` for a body of one JSON value per line) that says what the site said about the
  next page. It returns `(more, cursor)` for one of the listing's pages, where `more` is whether
  there is another page and `cursor` names it (None if the page doesn't), or None for a
  response that says nothing about paging. `page_info(path)` makes one for GraphQL.

```python
from flnd.plugin import page_info

page_state = page_info("data.media.page_info")
assert page_state({"data": {"media": {"page_info": {"has_next_page": True,
                                                    "end_cursor": "c2"}}}}) == (True, "c2")
assert page_state({"errors": ["slow down"]}) is None


class Feeds(Plugin):
    code, name, api, domains = "fds", "Feeds", 2, ("example.com",)

    @route("fds:{user}")
    async def user(self, ctx, user):
        async with ctx.feed("/graphql", cursor=page_state) as pages:
            await ctx.open(f"https://www.example.com/{user}/")
            async for page in pages:
                for item in page["data"]["media"]["items"]:
                    yield Post(item["id"], [File(item["url"])])


def media(items, more):
    return {"data": {"media": {"items": items, "page_info": {"has_next_page": more}}}}


sent = [("https://www.example.com/graphql?q=1",
         media([{"id": "m2", "url": "https://cdn.example.com/m2.jpg"}], True)),
        Sent("https://www.example.com/graphql?q=2",
             media([{"id": "m1", "url": "https://cdn.example.com/m1.jpg"}], False), scroll=1)]
ctx = FakeCtx(Feeds, pages={"https://www.example.com/example_user/": FakePage(feeds=sent)})
assert [file.key for file in collect(Feeds, "fds:example_user", ctx=ctx)] == ["m2_0", "m1_0"]
assert ctx.seen.scrolls == 1
```

`async with ctx.feed(...)` listens from there, so it comes before the `open` or `click` that
makes the page send. `ctx.load(url, match, …)` does both: it opens `url` with a feed listening,
and yields `meta(document)` first, then the feed's items.

A scrolling feed (`scroll=True`, the default) scrolls only when no response is waiting, and
each scroll waits for the next response or for the page to grow, not for a fixed time. It ends
as soon as the reader sees a page with no next one, or the same cursor three times; without a
reader, it ends after about 5 s of quiet at the bottom. The engine scrolls every feed like a
person: uneven steps, short pauses, and at most two scrolling tabs of one account at once.
`scroll=False` waits once for the network to go quiet (up to 10 s), gives what came, and ends.

## 8. Hooks and logins

A plugin can override a few methods of `Plugin`. Each is checked when the class is made: its
kind (`def` or `async def`) and its parameters.

| hook | kind | when it runs, and what it gives |
| --- | --- | --- |
| `sign(request, cookies)` | `def` | each try of a request to its domains: the request |
| `connect(ctx)` | `def` | once per run, around its targets: an async context manager |
| `date(ctx, post)` | `async def` | a post with no date, before its files are named: its day |
| `blocked(response)` | `def` | each answer from its domains: seconds to hold, or None |
| `refresh(ctx, post)` | `async def` | a file's link expired or ran too slow: the post, fresh |
| `resolve(text)` | `classmethod` | text no pattern can say: a `Target`, or None |

- **`connect`** suits an SDK's client: decorate an async generator with
  `contextlib.asynccontextmanager`, connect before the `yield`, and disconnect after it, in a
  `finally`. Its value isn't given to `ctx`; the plugin keeps what it opened.
- **`blocked`** sees `fetch`'s and `pages`' answers, and `open`'s main document (its HTML is
  read only for a plugin with a `blocked` hook of its own), never a feed item. Seconds are a
  block round, as a 429 is (section 4); 0.0 holds by the block rules alone.
- **`refresh`** by default lists a lazy post's files again, and raises `NotFound` for a post
  that isn't lazy.
- **`resolve`** passes text it doesn't read on to `super().resolve(text)`, which reads the
  patterns.
- **`date`** and **`refresh`** open their pages in a tab of their own, never the listing's.

```python
class Dated(Plugin):
    code, name, api, domains = "dtd", "Dated", 2, ("example.com",)
    filename = "{date}_{post}"

    @route("dtd:{user}")
    async def user(self, ctx, user):
        for id in ("p2", "p1"):
            yield Post(id, [File(f"https://cdn.example.com/{id}.jpg")])

    async def date(self, ctx, post):
        return (await ctx.fetch(f"{API}/posts/{post.id}")).json["day"]

    def blocked(self, response):
        return 60.0 if b"slow down" in response.content else None


answers = iter([Reply("<p>slow down</p>"), Reply({"day": "2026-01-02"})])
ctx = FakeCtx(Dated, routes={f"{API}/posts/p2": lambda request: next(answers),
                             f"{API}/posts/p1": Reply({"day": "2026-01-01"})})
saved = collect(Dated, "dtd:example_user", ctx=ctx)
assert [file.path for file in saved] == ["2026-01-02_p2.jpg", "2026-01-01_p1.jpg"]
assert [seconds for _, seconds, _ in ctx.seen.holds] == [60.0]
```

### Logins

A site with its own login flow subclasses `Login`. It sets `methods`, the ways
`flnd login <code> <method>` accepts, and implements `async def start(self, ctx, method)`.
`start` asks through `ctx.ask`, and keeps what it got in `ctx.secrets`; it raises `LoginError`
when the site refused or the person gave up. `check(ctx)` says whether the account is logged
in (by default, whether one of its `names` is a cookie), and `whoami(ctx)` gives the account's
name on the site, or None. All three are `async def`. A login is required by default:
`login = CodeLogin(required=False)` lets the plugin list without one.

```python
from flnd.plugin import Login


class CodeLogin(Login):
    """Logs in with a code the site sends to the person's phone."""

    methods = ("phone",)               # flnd login cod phone

    async def start(self, ctx, method):
        ctx.secrets["session"] = await ctx.ask("Code", description="Sent to your phone")

    async def check(self, ctx):
        return "session" in ctx.secrets


class Coded(Plugin):
    code, name, api, domains = "cod", "Coded", 2, ("example.com",)
    login = CodeLogin()

    @route("cod:{user}")
    async def user(self, ctx, user):
        yield Post(user, [File(f"https://cdn.example.com/{user}.jpg")])


ctx = FakeCtx(Coded, answers=["123456"])
asyncio.run(Coded.login.start(ctx, "phone"))
assert ctx.secrets["session"] == "123456" and asyncio.run(Coded.login.check(ctx))
try:
    ctx.secrets["session"] = "changed"           # outside start: refused
except PluginError as error:
    message = str(error)
assert message == "ctx.secrets is written only during Login.start"
```

A login with a `browser` method sets `url`, the https page that method uses. During
`flnd login`, a login may also read a web app's saved session from the user's browser with
`ctx.storage(origin)`, as Telegram's reads Telegram Web's.

### Settings, the browser, walls

- **`Settings`** are the plugin's own config keys, under `[plugins.<code>]` in the home's
  `config.toml`. A plugin declares a nested `class Settings(Settings)`, with a default for each
  field and an `Attributes:` line for each in its docstring; it is made a frozen, keyword-only
  dataclass. `ctx.settings` gives it, and `FakeCtx(settings={"height": 720})` sets fields for a
  test. Not built yet in a run: the engine gives the defaults, and doesn't read `config.toml`'s
  values into `ctx.settings` yet.

  ```
  from flnd.plugin import Settings

  class Tall(Plugin):
      ...

      class Settings(Settings):
          """Tall's own keys.

          Attributes:
              height: the tallest video to take, in pixels; 0 takes the tallest there is.
          """

          height: int = 0
  ```

- **`browser`** says which browser the plugin's tabs need: `Browser.NONE` (HTTP only, no tab,
  the default), `Browser.ANY` (flnd's Chromium, or the user's Chrome) or `Browser.CHROME` (the
  user's Chrome, through the extension). **`stealth`** (True by default) hides automation in its
  tabs.
- **`user_agent`** is the User-Agent of its HTTP requests to its own domains; None keeps the
  client's.
- **`walls`** are URL paths that mean "log in" or a stopped account, such as
  `("/accounts/login", "/challenge")`, matched by whole path segments: `/challengers/` is not the
  wall `/challenge`. Landing on one holds 30 s, then 120 s, then fails as a `LoginError`.

```python
from flnd.plugin import Browser, LoginError


class Walled(Plugin):
    code, name, api, domains = "wal", "Walled", 2, ("example.com",)
    browser = Browser.ANY
    walls = ("/login",)

    @route("wal:{user}")
    async def user(self, ctx, user):
        await ctx.open(f"https://www.example.com/{user}/")
        yield Post(user, [File(f"https://cdn.example.com/{user}.jpg")])


login_page = FakePage(url="https://www.example.com/login?next=example_user")
ctx = FakeCtx(Walled, pages={"https://www.example.com/example_user/": login_page})
try:
    collect(Walled, "wal:example_user", ctx=ctx)
except LoginError as error:
    message = str(error)
assert message.startswith("wal: landed on the wall /login")
assert [seconds for _, seconds, _ in ctx.seen.holds] == [30.0, 120.0]
```

## 9. Adding it to flnd as a built-in

A built-in plugin is `src/plugins/<code>.py`. It imports only `flnd.plugin` and the standard
library; an SDK its site needs (`telethon`, `qrcode`) is imported inside a function.
`tests/test_layers.py` checks it. Then:

1. write its tests in `tests/`, on `FakeCtx`, with fixtures for each route and category, and a
   `ROUTES` table with a row for each form, as `tests/example_plugin.py` has;
2. run `make manifest` after changing its domains, routes or categories: help and target
   resolution read the manifest instead of importing every plugin, and `tests/test_manifest.py`
   fails until it matches;
3. write its page, `src/docs/plugins/<code>.md`, in the shape of the other pages there (the
   built-in ones, such as [plugins/ins.md](plugins/ins.md));
4. run `make check`;
5. try it by hand on a test account: `make try ARGS=exm:example_user`, which lists what a
   download would save without saving anything.

## 10. Shipping it as a plugin

A plugin that isn't built in comes from outside flnd's own code. Today one way is built:

- **A script.** `fl.add(Example)` adds the class to one `Flnd` ([library.md](library.md)). Its
  targets resolve and download like a built-in's. A code a built-in or another plugin has
  already is refused, never replaced.

Installing a plugin for every run isn't built yet for plugin API 2: `flnd plugin install` and
its plugin folder come in step 9 of [PLAN.md](../../PLAN.md) (section 6.6 there).

**Never from a home.** flnd never loads code from a home folder. A home is often `./.flnd` in
the folder you are in, which may be a cloned repository or an unpacked archive: loading code
from it would run whatever that folder holds. Install a plugin as you would any code.

**A broken plugin fails alone.** A class that fails its checks raises when it is made, so it
never runs. An exception that escapes its listing becomes a `PluginError` and fails that target
alone. An `async def` listing that runs 10 s without awaiting (a blocking call) holds up the
whole run, and is warned about.

This is containment, not a security boundary: a plugin runs in flnd's process, with your
logins, and can do anything you can. Install only plugins you trust.

### Plugin API versions

A plugin says which major version of the plugin API it was written for: `api = 2`. This flnd
has `flnd.plugin.API = 2`. A plugin for a newer version is refused ("exm needs plugin API 3;
this flnd has 2", with the hint "update flnd"). A plugin written for API 1 is refused with a
hint to port it. A class that still declares a name this API dropped
(`needs`, `requires_login`, `login_methods`, `logged_in`, `concurrency`, `prepare`, `scroll`,
`single_kinds`) is refused with how this API spells it:

```python
try:
    class Dropped(Plugin):
        code, name, api, domains = "drp", "Dropped", 2, ("example.com",)
        prepare = None

        @route("drp:{user}")
        async def user(self, ctx, user):
            yield Post(user)
except PluginError as error:
    message = str(error)
assert message == "drp.prepare: plugin API 2 spells it sign(self, request, cookies)"
```

When the version changes, this section lists what to change, version by version.
