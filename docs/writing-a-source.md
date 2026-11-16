# Writing a source

A **source** is the code for one site: one class that says which targets it reads and lists the
posts each target has. flnd does everything else: pacing and retrying requests, downloading,
naming and dating files, skipping what is saved, resuming, progress and logins.

This page builds a small source step by step against today's source API, `flnd.source`. Every
`python` block on it runs in the docs test, so it stays true. `tests/example_source.py` is a
bigger example with the same shape, and the seven built-in sources in `flnd/sources/` are real
ones.

The rebuild moves to source API 1 (`api = 1`, `site.pages`, a `cookies()` login, `--only`) and
adds plugins loaded from the user's folder. Neither is built yet; today a source is a module in
`flnd/sources/` listed in `flnd/sources/__init__.py`.

## 1. The class

The made-up site `example.com` has a JSON API: `api.example.com/users/<name>` gives a profile,
and `api.example.com/users/<id>/posts` gives its posts, newest first, in pages chained by a
`next` cursor.

```python
from flnd.source import File, Post, Site, Source, pages, target

API = "https://api.example.com"


class Example(Source):
    """example.com: a user's photos and videos, from its JSON API."""

    code = "exm"                   # targets are written exm:<user>
    name = "Example"               # shown in help
    domains = ("example.com",)     # bare links on these hosts belong to this source
    needs = ("token",)             # any one of these cookies proves a login

    @target("{user}", "example.com/{user}")
    def user(self, site: Site, user: str) -> dict:
        """A profile: its JSON, which every category below receives."""
        return site.fetch(f"{API}/users/{user}").json

    @user.category(newest_first=True)
    def posts(self, site: Site, profile: dict):
        """The profile's posts, newest first, page by page."""
        for page in pages(
            lambda cursor: site.fetch(f"{API}/users/{profile['id']}/posts",
                                      params={"cursor": cursor or ""}).json,
            lambda page: page.get("next"),
        ):
            for item in page["items"]:
                files = [File(url) for url in item["urls"]]
                yield Post(item["id"], files, date=item["time"])
```

What each part does:

- **`code`, `name`, `domains`** say what the source is. `domains` also decides which requests
  get the login's cookies and the source's pace.
- **`@target(...)`** makes `user` a **route**: a method that reads one kind of target. Each
  pattern is one way to write it. `{user}` alone means `exm:<user>`; `example.com/{user}` is a
  link. The method's name, `user`, is the target's kind.
- **A route that returns a value has categories.** `@user.category` makes `posts` one of them; it
  receives what the route returned. A route that yields `Post`s itself (a single post's link)
  has none.
- **`newest_first=True`** promises that the category lists the newest posts first. Only then
  may a sync stop after 5 saved posts in a row, and `--time` stop after 5 posts older than its
  range. Without it, flnd reads the whole listing: slower, but never wrong.
- **`pages(fetch, cursor)`** walks a paged listing. It also saves the cursor when a run is
  interrupted, so the next run resumes there.

When the class is made, flnd checks it. A pattern outside `domains`, a pattern given twice, or a
route method with the wrong arguments raises `TypeError` at import, naming the mistake.

## 2. The forms it reads

flnd builds the help's forms from the patterns, and `resolve` reads a target through them:

```python
assert Example.forms == ("exm:<user>", "https://www.example.com/<user>/")
assert Example.categories == ("posts",)

target = Example().resolve("https://www.example.com/example_user/")
assert (target.source, target.kind, target.value) == ("exm", "user", "example_user")
```

## 3. Testing it without the network

`tests/fakes.py` runs a source on a `FakeSite`, which answers each request from fixtures keyed
by URL without its query. `collect` runs the route and its categories as the engine would, and
lists every file it would save, with its path and history key. Nothing is downloaded.

```python
from fakes import collect
from flnd.options import Options

FIXTURES = {
    f"{API}/users/example_user": {"id": "42"},
    f"{API}/users/42/posts": {
        "items": [{"id": "p1", "time": "2026-01-02",
                   "urls": ["https://cdn.example.com/p1.jpg", "https://cdn.example.com/p1.mp4"]}],
        "next": None,
    },
}

saved = collect(Example, "exm:example_user", json=FIXTURES,
                options=Options(categories=("posts",)))
assert [file.path for file in saved] == ["posts/2026-01-02_p1_0.jpg", "posts/2026-01-02_p1_1.mp4"]
assert [file.key for file in saved] == ["p1_0", "p1_1"]
```

A request no fixture answers raises a 404 `NotFound`, as a missing page would. Each built-in
source has a test like this in `tests/test_<code>.py`. Live runs against the real site are done
by hand, on test accounts, never in a unit test.

## 4. Where files go

A file's path under the target's folder, `.flnd/media/<code>/<value>/`, comes from three
templates on the class. A category may give its own `folder` and `key`.

| attribute | default | what it names |
| --- | --- | --- |
| `folder` | `{category}` | the folder under the target's folder |
| `filename` | `{date}_{id}{index}` | the file's name; the content adds the extension |
| `key` | `{id}_{num}` | the history key, which says a file is already saved |

A template can use the post's `id` and `date`, any field the post was given
(`Post(id, files, title=…)`), and the fields the engine sets: `category`, `num` (the file's
place in its post, from 0), `count`, `index` (`""` for a one-file post, else `_<num>`),
`extension` and `path`. A field that is empty drops the separator beside it, so `{date}_{id}`
of an undated post is just `<id>`. The source above saves
`.flnd/media/exm/example_user/posts/2026-01-02_p1_0.jpg`.

## 5. Posts and files

```python
Post("p2", [File("https://cdn.example.com/p2.jpg")], date=1767312000, title="Example title")
Post("p3", lambda: [File("https://cdn.example.com/p3.jpg")])     # lazy: listed only if new
File(["https://cdn.example.com/a.jpg", "https://mirror.example.com/a.jpg"])   # mirrors
File("https://cdn.example.com/v.mp4", audio="https://cdn.example.com/a.mp4")  # DASH, merged
File.text("info.md", "# Example title\n")                        # written by the source
```

- **`date`** takes a date, a datetime, Unix seconds or milliseconds, or an ISO string. An undated
  post is always kept by `--time`, and its files get the date the source's `date` hook finds, if
  it has one.
- **`files`** may be a function. It is called only for a post that isn't saved yet, so a source
  never spends a request on a post flnd already has.
- **`headers`** (`Post(..., headers={"Referer": ...})`) are sent with each of the post's files,
  for a CDN that wants them.

## 6. What `Site` offers

A route acts on the site only through `site`, which `flnd.source.Site` describes in full:

| member | what it does |
| --- | --- |
| `site.fetch(url, params=…, method=…, headers=…, check=…)` | one request, paced and retried; `.json`, `.text`, `.html` on the answer; a refusal raises a `SiteError` with its `status` (`NotFound` for a 404 or 410, `Blocked` for a 429) unless `check=False`; no answer raises `NetworkError` |
| `site.goto(url)`, `site.page` | load a page in the target's browser tab, and use the tab |
| `site.handoff(until=…, message=…)` | ask a person to do a step in the browser window, such as a captcha |
| `site.cookies`, `site.secrets` | the login's cookies, and the source's encrypted secrets |
| `site.state` | a small store of the source's own, kept between runs |
| `site.client` | what the source's `connect` hook gave, such as Telegram's client |
| `site.time` | `--time`, for a site that can query a date range itself |
| `site.info(…)`, `site.warn(…)` | a line naming the target |
| `site.back_off(reason, retry_after=…)` | wait out a rate limit the site reported in its own way |

A source never sleeps, retries, writes files or touches the history itself.

## 7. Hooks

A source can override a few methods of `Source`:

| hook | when it runs | used by |
| --- | --- | --- |
| `prepare(request, cookies)` | before each request to the source's domains: add headers or a signature | ins, twt, loi |
| `connect(site)` | once per run: a context manager whose value is `site.client` | tlg |
| `login(login, method)` | `flnd auth login` for a source with its own login flow | tlg |
| `date(site, post)` | for an undated post, once its category ends: one paced request for its day | fbk |
| `logged_in(site)` | whether a login is saved; by default, one of `needs` is in the cookies | tlg |
| `resolve(text)` | for target forms no pattern can say | tlg |

A route or category may be `async def`; it then runs on the source's own event loop, as
Telegram's do.

## 8. Adding it to flnd

Save the module as `flnd/sources/<code>.py`, import its class in `flnd/sources/__init__.py` and
add it to `SOURCES`. Then:

1. write `tests/test_<code>.py`, with fixtures for each route and category;
2. write `docs/sources/<code>.md`, in the shape of the other pages there (the docs test fails
   until it exists);
3. run `make check`;
4. try it by hand on a test account: `make try ARGS=exm:example_user`.
