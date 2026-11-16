# Writing a source

A **source** is the code for one site: one class that says which targets it reads and lists the
posts each target has. flnd does everything else: pacing and retrying requests, downloading,
naming and dating files, skipping what is saved, stopping a sync early, resuming, progress and
logins.

This page builds a small source step by step against **source API 1**, `flnd.source`, tests it
with `flnd.testing`, and ships it as a plugin. Every `python` block on it runs in the docs test,
so it stays true. `examples/plugin/` is a whole plugin with its test, `tests/example_source.py`
is a bigger example with the same shape, and the seven built-in sources in `flnd/sources/` are
real ones. `flnd/source.py`'s and `flnd/testing.py`'s docstrings are the reference.

## 1. The class

The made-up site `example.com` has a JSON API: `api.example.com/users/<name>` gives a profile,
and `api.example.com/users/<id>/posts` gives its posts, newest first, in pages chained by a
`next` cursor.

```python
from flnd.source import File, Post, Request, Site, Source, cookies, target

API = "https://api.example.com"


class Example(Source):
    """example.com: a user's photos and videos, from its JSON API."""

    code = "exm"                   # targets are written exm:<user>
    name = "Example"               # shown in help; the class's name by default
    domains = ("example.com",)     # bare links on these hosts belong to this source
    api = 1                        # the source API this was written for
    login = cookies("token")       # logged in when this cookie is there
    filename = "{date}_{post}{index}"

    @target("exm:{user}", "example.com/{user}")
    def user(self, site: Site, user: str) -> dict:
        """A profile: its JSON, which every category below receives."""
        return site.fetch(f"{API}/users/{user}").json

    @user.category(newest_first=True)
    def posts(self, site: Site, profile: dict):
        """The profile's posts, newest first, page by page."""
        for page in site.pages(f"{API}/users/{profile['id']}/posts", cursor="next"):
            for item in page["items"]:
                files = [File(url) for url in item["urls"]]
                yield Post(item["id"], files, date=item["time"])

    def sign(self, request: Request) -> None:
        """The login's token as a Bearer header, on the source's own domains only."""
        if request.cookies.get("token"):
            request.headers["Authorization"] = f"Bearer {request.cookies['token']}"
```

What each part does:

- **`code`, `domains` and `api`** are required, with one route. `code` is what targets start
  with; `domains` decides which links belong to the source, which requests carry its login and
  its `sign` headers, and which of its listing's requests wait for its `pace`. `api = 1` says
  which source API it was written for: a newer flnd refuses an older one with a message.
- **`login`** says how it logs in: `cookies("token")` is logged in when that cookie is there,
  and `flnd login exm chrome` reads it from a browser. `cookies("token", required=True)` means
  nothing can be listed without it. A site with its own flow assigns an instance of a `Login`
  subclass (section 7). No `login` means the source needs none.
- **`pace`** (0.5 by default) is the least time between two listing requests to the source's
  own domains. **`limits`** caps the files downloading at once from a host and its
  subdomains, such as `limits = {"cdn.example.com": 4}`. Every host's cap adapts: it starts at
  8 (or the host's own cap, when lower), halves when the host answers 429 or 503 or drops
  connections, and grows while its throughput still rises, up to 32 for a host not named and
  up to its own cap for one that is. Name a host only when it is known to refuse more, as
  Telegram's flood limit does. A file the source fetches itself (a `fetch` function) counts
  against its first domain. Every request goes out
  with Chrome's TLS and HTTP/2 fingerprint.
- **`@target(...)`** makes `user` a **route**: a method that reads one kind of target. Each
  pattern is one way to write it: `exm:{user}` after the code, `example.com/{user}` as a link.
  The method's name, `user`, is the target's kind.
- **A route that returns a value has categories.** `@user.category` makes `posts` one of them; it
  receives what the route returned. A route that yields `Post`s itself (a single post's link)
  has none; `@target(..., newest_first=True)` says its own posts come newest first.
- **`newest_first=True`** promises that the listing comes newest first. Only then may a sync stop
  early, and `--when` stop after 5 posts older than its range. Without it, flnd reads the whole
  listing: slower, but never wrong.
- **`site.pages(url, cursor=…, params=…, send=…)`** walks a paged JSON listing. `cursor` is a
  path in each page (`"next"`, `"data.page_info.end_cursor"`) or a function from the page to the
  next cursor; `send` is the query parameter the cursor goes in (`"cursor"` by default), or a
  function from the cursor to more params. The listing ends on a missing or repeated cursor, or
  an empty page. flnd keeps the cursor of an interrupted run, and the next run resumes there.
- **`sign(request)`** runs before each try of a request to the source's domains: it may add
  headers or a signature to `request` (its `method`, `url`, `headers`, `body`, and the login's
  `cookies`).

A source never stops a listing itself. flnd owns the early stops: a sync of a `newest_first`
listing stops after 5 saved posts in a row (`[sources.exm] stop_after = N` in the home's
`config.toml` changes it), or sooner, after the newest post an earlier run saw and the post after
it; and `--when` stops after 5 posts in a row older than its range. A source just yields, and
flnd closes its generator, which ends the page reads or the scrolling.

## 2. Mistakes fail when the class is made

flnd checks the class when it is made, so a broken source never starts a run. Each mistake is a
`SourceError` naming the source, the attribute and the fix:

```python
from flnd.source import SourceError

try:
    class Broken(Source):
        code, domains, api = "brk", ("example.com",), 1
        folder = "{target}/{usr}"

        @target("brk:{user}")
        def user(self, site, user): ...
except SourceError as error:
    message = str(error)

assert message.startswith("brk.folder: unknown field {usr}; fields are source, target, category")
```

It also refuses a missing `code`, `domains` or `api`, a pattern outside `domains` or given twice,
a code pattern without the source's code, a route with categories that yields, a template that
leaves the target's folder, a wrong `login`, `pace`, `limits` or `scroll`, and the names of the
old API (`needs`, `prepare`, `concurrency`, …) with their new spelling.

## 3. The forms it reads

flnd builds the help's forms from the patterns, and `resolve` reads a target through them:

```python
assert Example.forms == ("exm:<user>", "https://www.example.com/<user>/")
assert Example.categories == ("posts",)

found = Example().resolve("https://www.example.com/example_user/")
assert (found.source, found.kind, found.value) == ("exm", "user", "example_user")
```

## 4. Testing it without the network

`flnd.testing` is the test kit. `collect` runs a source on a `FakeSite`, which answers each
request from fixtures keyed by URL without its query; it runs the route and its categories as
the engine would, and lists every file a download would save, with its path, history key and
day. Nothing is downloaded, and no home is touched.

```python
from flnd.testing import FakeSite, check, collect

FIXTURES = {
    f"{API}/users/example_user": {"id": "42"},
    f"{API}/users/42/posts": {
        "items": [{"id": "p1", "time": "2026-01-02",
                   "urls": ["https://cdn.example.com/p1.jpg", "https://cdn.example.com/p1.mp4"]}],
        "next": None,
    },
}

saved = collect(Example, "exm:example_user", json=FIXTURES, only=["posts"])
assert [file.path for file in saved] == ["posts/2026-01-02_p1_0.jpg", "posts/2026-01-02_p1_1.mp4"]
assert [file.key for file in saved] == ["p1_0", "p1_1"]
assert str(saved[0].date) == "2026-01-02"
```

A request no fixture answers raises a 404 `NotFound`, as a missing page would. A fixture may also
be a function of `(url, request)`, to answer each page of a listing by its cursor, or a
`SiteError` to raise. For pages opened in the browser, feeds, cookies, a client or `--when`,
make the `FakeSite` yourself and pass it in; it records what the source did in `site.seen`:

```python
site = FakeSite(json=FIXTURES, cookies={"token": "t"})
collect(Example, "exm:example_user", site=site)
assert [url for _, url, _ in site.seen.requests][0] == f"{API}/users/example_user"
```

`check(Example)` checks the class, and resolves every example target its routes' docstrings
give in backticks (`exm:example_user`, or a link on its domains): each must be read by the route
whose docstring gives it, so the examples in a docstring can't go stale. `fake_clock()` gives a
clock that only moves when something sleeps on it, for code that paces itself.

```python
class Photos(Source):
    code, domains, api = "pho", ("photos.example.com",), 1

    @target("pho:{user}", "photos.example.com/{user}")
    def user(self, site: Site, user: str):
        """A user's photos: `pho:example_user`, or `https://photos.example.com/example_user`."""
        yield from ()

assert [str(found) for found in check(Photos)] == ["pho:example_user", "pho:example_user"]
```

Each built-in source has a test like this in `tests/test_<code>.py`, and `examples/plugin/`
has one for a plugin. Live runs against the real site are done by hand, on test accounts, never
in a unit test.

## 5. Where files go

A file's path under the target's folder, `.flnd/media/<code>/<target>/`, comes from three
templates on the class. A route or a category may give its own `folder`, and a category its own
`key`.

| attribute | default | what it names |
| --- | --- | --- |
| `folder` | `{category}` | the folder under the target's folder |
| `filename` | `{post}_{num}` | the file's name; the content adds the extension |
| `key` | `{post}_{num}` | the history key, which says a file is already saved |

A template may use the fields the engine sets (`source`, `target`, `category`, `post` (the
post's id), `num` (the file's place in its post, from 0), `count`, `index` (`""` for a one-file
post, else `_<num>`), `extension`, `path`, `date` and `query`), and the post fields the source
lists in `fields = ("album", …)`. A field that is empty drops the separator beside it, so
`{date}_{post}` of an undated post is just `<post>`. Every folder and name is made safe for
every OS. The source above saves `.flnd/media/exm/example_user/posts/2026-01-02_p1_0.jpg`.
Changing `key` makes every saved file look new: never change it for a source in use.

## 6. Posts, files and targets

```python
from flnd.source import Target

Post("p2", [File("https://cdn.example.com/p2.jpg")], date=1767312000, title="Example title")
Post("p3", lambda: [File("https://cdn.example.com/p3.jpg")])     # lazy: listed only if new
File(["https://cdn.example.com/a.jpg", "https://mirror.example.com/a.jpg"])   # mirrors
File("https://cdn.example.com/v.mp4", audio="https://cdn.example.com/a.mp4")  # joined by ffmpeg
File("https://cdn.example.com/x.7z", password="example")        # an archive, extracted
File.text("info.md", "# Example title\n")                        # written by the source
Target("https://www.tiktok.com/@example_user")                   # downloaded as if typed
```

- **`date`** takes a date, a datetime, Unix seconds or milliseconds, or an ISO string; flnd reads
  it once, and a source never parses a date itself. An undated post is always kept by `--when`,
  and its files get the date the source's `date` hook finds, if it has one. To test a raw date
  against `--when`, a source writes `value in site.time`.
- **`files`** may be a function. It is called only for a post that isn't saved yet, so a source
  never spends a request on a post flnd already has.
- **`headers`** (`Post(..., headers={"Referer": ...})`) are sent with each of the post's files,
  for a CDN that wants them.
- **`File.dash(manifest)`** turns a DASH manifest into one `File(video, audio=…)` of its best
  streams, or None.
- **`Target(text)`** takes any target, from any source. A search route (`@target("exm?{words}")`)
  yields its results this way, and so may a post that links to another site: flnd resolves it
  on every source and downloads it after the run's own targets. A route that yields targets
  without being a query says `discovers=True`, so `--only` applies to what it finds.

## 7. What `Site` offers

A route acts on the site only through `site`, which `flnd.source.Site` describes in full:

| member | what it does |
| --- | --- |
| `site.fetch(url, params=…, method=…, headers=…, check=…)` | one request, paced and retried, with Chrome's fingerprint; `.json`, `.text` on the answer; a refusal raises a `SiteError` with its `status` (`NotFound` for a 404 or 410, `Blocked` for a 429), a 401 or 403 from the source's own domains a `LoginError`, unless `check=False`; no answer raises `NetworkError` |
| `site.pages(url, cursor=…, params=…, send=…)` | a paged JSON listing, resumed by flnd (section 1) |
| `site.open(url)`, `site.url` | load a page in the target's browser tab (False when it didn't load), and where the tab is now |
| `site.feed(match, scroll=…, lines=…, cursor=…)` | the JSON of the responses whose URL matches, as the page receives them; with `scroll=True` it scrolls for more until the feed ends, which `cursor` reads from the site's own pages (below). Open it before the page that sends them |
| `site.warm(urls)` | start loading pages the source opens next, in background tabs |
| `site.document()`, `site.html(text)` | the tab's page now, or any HTML text, parsed: `.css(…)`, `.first(…)`, `.script(id)`, `.blocks(…)`, `.text`, `.visible` (below) |
| `dig(data, "a.b[].c")` | walk JSON: keys, `[0]`, `[]` for every item, `..[field=value]` at any depth, `||` for the first that finds something (below) |
| `site.wait(css=…, url=…)`, `site.scroll(until=…)` | wait for an element or a URL; scroll, in the source's `scroll` style, until the page stops growing |
| `site.evaluate(js)`, `site.click(css)` | run a script in the page; click an element |
| `site.handoff(until=…, message=…)` | ask a person to do a step in the browser window, such as a captcha |
| `site.pause(seconds, reason)` | wait, as a page that needs time to settle does |
| `site.back_off(reason, retry_after=…)` | wait out a block the site showed in its content: 30 s, 2 min, 8 min for the rounds in a row, then `Blocked` |
| `site.cookies`, `site.secrets` | the login's cookies, and the source's encrypted secrets |
| `site.state` | a small store of the source's own, kept between runs |
| `site.client` | what the source's `connect` hook gave, such as Telegram's client |
| `site.time` | `--when`, for a site that can query a date range itself |
| `site.info(…)`, `site.warn(…)` | a line naming the target |

A source never sleeps, retries, writes files, touches the history, or decides when a listing
stops.

### Reading a page and its JSON

`site.html(text)` parses HTML, and `site.document()` parses the tab's page now; both give a
page with CSS queries and the JSON it embeds. There is no XPath, and no `::text` or `::attr()`.

| on the page | gives |
| --- | --- |
| `page.css(query)` | every element that matches the CSS selector, in page order (a list) |
| `page.first(query)` | the first match, or None |
| `element.text` | all its text, whitespace collapsed |
| `element.attr(name)` | one attribute's value, or None |
| `page.script(id)` | the JSON in `<script id="…">`, or None; `Changed` when it isn't JSON |
| `page.blocks(contains)` | the JSON of every `<script type="application/json">` that holds `contains` |
| `page.visible` | the text a reader sees, without scripts and styles |

`dig(data, path)` walks the decoded JSON. A path is a few forms, chained:

- `a.b` reads a key, and `[0]` an index (`[-1]` is the last);
- `[]` reads the rest from every item of a list, and joins the lists it reaches:
  `edges[].node.media[]` is every media of every node;
- `..[field=value]` finds every dict, at any depth, whose `field` is the text `value`, and reads
  the rest from each, as `[]` does;
- `a || b` gives the first alternative that finds something (not None, "", [] or {}).

A missing key gives None, and a path with `[]` or `..[]` gives a list, empty when nothing
matched. A malformed path raises `SourceError`.

```python
from flnd import _html
from flnd.source import dig

page = _html.parse('''<a href="/video/1">one</a><a href="/about">about</a>
<script id="__NEXT_DATA__" type="application/json">
{"props": {"items": [{"__typename": "Video", "id": "1"}, {"__typename": "Ad"}]}}
</script>''')

assert [a.attr("href") for a in page.css('a[href*="/video/"]')] == ["/video/1"]
data = page.script("__NEXT_DATA__")
assert dig(data, "props.items[].id") == ["1"]
assert dig(data, "..[__typename=Video].id") == ["1"]
assert dig(data, "props.next || props.items[0].id") == "1"
```

A source parses through `site.html`, not `flnd._html`; the example imports it only to run
without a site.

### A feed in the browser: name its responses and its cursor

A site that only shows its data to a logged-in page is read from the page's own API calls. The
source names two things, and flnd does the scrolling:

- **the feed's filter**, `match`: a piece of the URL (`"/graphql"`), a regex, or a function of
  the URL, which picks the responses that carry the listing;
- **its cursor reader**, `cursor`: a small function given each parsed response (each line, with
  `lines=True`) that says what the site said about the next page. It returns `(more, cursor)`
  for one of the listing's pages, where `more` is whether there is another page and `cursor`
  names it (None if the page doesn't), or `None` for a response that says nothing about paging.

```python
from flnd.source import dig


def page_state(page):
    """`(more, cursor)` from one page of example.com's listing; None for anything else."""
    if not isinstance(page, dict) or "page_info" not in page:
        return None
    return bool(dig(page, "page_info.has_next_page")), dig(page, "page_info.end_cursor")


assert page_state({"page_info": {"has_next_page": False, "end_cursor": None}}) == (False, None)
assert page_state({"errors": ["slow down"]}) is None
```

Then, in a category:

```
with site.feed("/graphql", scroll=True, cursor=page_state) as pages:
    if site.open(f"https://www.example.com/{user}/"):
        for page in pages:
            ...
```

The feed scrolls only when no response is waiting, and each scroll waits for the next response
or for the page to grow, not for a fixed time. It ends as soon as the reader sees a page with no
next one, or the same cursor three times; without a reader, it ends after about 5 s of silence
at the bottom.

**How it scrolls** is the class attribute `scroll`: `"human"` (the default) moves the wheel in
uneven steps, pauses about 0.4 s as a reader would, and lets at most two tabs of one login
scroll at once, so the site flags it less as a bot; `"fast"` jumps to the bottom.

## 8. Hooks and logins

A source can override a few methods of `Source`:

| hook | when it runs | used by |
| --- | --- | --- |
| `sign(request)` | before each request to the source's domains, each try (section 1) | ins, twt, loi |
| `connect(site)` | once per run: a context manager whose value is `site.client` | tlg |
| `date(site, post)` | for an undated post, once its category ends: one paced request for its day | fbk |
| `resolve(text)` | for target forms no pattern can say | tlg |

A site with its own login flow subclasses `Login`:

```python
from flnd.source import Login


class CodeLogin(Login):
    """Logs in with a code the site sends to the person's phone."""

    methods = ("phone",)               # flnd login exm phone

    def start(self, site, how):
        site.secrets["session"] = site.ask("Code", description="Sent to your phone")

    def check(self, site):
        return "session" in site.secrets


assert CodeLogin().required and CodeLogin.methods == ("phone",)
```

and assigns it once, `login = CodeLogin()`. During `flnd login`, `site` asks (`ask`), keeps what
the login got (`secrets`, `state`), and reads a web app's saved session from Chrome
(`storage(url)`).

The browser is one per run, launched the first time a target opens a page, and each target
works in its own tab. A source never gets a Playwright object from the verbs above. A route or
category may be `async def`; it then runs on the run's event loop, as Telegram's do. There it
can't call the browser verbs; it awaits `site.tab` for Playwright's own page instead, which is
outside the source API's version promise.

## 9. Adding it to flnd

A source imports only `flnd.source` and the stdlib (and, lazily, an SDK its site needs). Save a
built-in as `flnd/sources/<code>.py`, add its class to `BUILT_IN` in `flnd/sources/__init__.py`,
and run `make manifest`. Then:

1. write `tests/test_<code>.py`, with fixtures for each route and category, and a `ROUTES`
   table with a row for each pattern;
2. write `docs/sources/<code>.md`, in the shape of the other pages there (the docs test fails
   until it exists);
3. run `make check`;
4. try it by hand on a test account: `make try ARGS=exm:example_user`.

## 10. Shipping it as a plugin

A source that isn't built in is a **plugin**. flnd finds them, after its built-ins, in this
order:

1. **A package.** Declare an entry point in the `flnd.sources` group, named by the source's
   code, and publish it; `pip install` it where flnd is installed:

   ```toml
   [project.entry-points."flnd.sources"]
   exm = "flnd_example:Example"
   ```

2. **A file.** Save it as `exm.py` in the plugin folder: `$FLND_SOURCES`, else
   `~/.config/flnd/sources/` (`%APPDATA%\flnd\sources\` on Windows). A source that needs
   more files is a folder, `exm/`, with an `__init__.py` that imports its class; its modules
   import each other relatively (`from .api import read`), since the folder is never put on
   `sys.path`. A file's `__version__` is shown as its version.
3. **A script.** `fl.add(Example)` adds it to one `Flnd` ([library.md](library.md)).

The entry point's name, or the file's, is the source's code, and must match the class's `code`.
That is how `exm:example_user` finds its plugin without importing any other, and a link imports
the plugins only when no built-in reads it. A code that is taken already (by a built-in, or by
a plugin found before it) is refused with a warning: a plugin never replaces another source.
`flnd sources` lists every source with where it came from, its version, and its login.

**Never from a home.** flnd never loads code from a home folder. A home is often `./.flnd` in
the folder you are in, which may be a cloned repository or an unpacked archive: loading
`<home>/sources/*.py` would run whatever that folder holds. Install a plugin as you would any
code, into your own plugin folder or with pip.

**A broken plugin disables only itself.** An import error, or a class that fails its checks (a
bad pattern, a newer source API), disables that plugin alone, with a warning naming its file
and line, which `flnd sources` repeats. An exception inside its listing fails that target alone,
as a `SourceError` whose hint names the plugin. An `async def` route that runs 10 s without
awaiting (a blocking call) holds up the whole run, and is warned about.

This is containment, not a security boundary: a plugin runs in flnd's process, with your
logins, and can do anything you can. Install only plugins you trust.

### Source API versions

A source says which major version of the source API it was written for: `api = 1`. This flnd
has `flnd.source.API = 1`. A plugin for a newer version is refused ("exm needs source API 2;
this flnd has 1: update flnd"), and one for an older version is refused with a pointer here.
Additions that don't break a written source keep the version. When a version changes, this
section lists what to change, version by version; source API 1 is the first.
