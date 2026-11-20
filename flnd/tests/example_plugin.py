"""Example (example.com): a complete small plugin on API 2 over fixtures, and the template a new
plugin starts from.

It is run, not only read: tests/test_crawl.py drives it through the real engine on
`flnkit.net.fake`, and tests/test_plugin.py through `flnd.testing.collect` on `FIXTURES`.
Copy it to start a plugin: keep its shape and replace the site's knowledge (its URLs and JSON
paths). SPEC.md#writing-a-plugin walks through it.

What the made-up site gives, and the quirks a real one has that this one copies:
  - a JSON API on api.example.com that wants the login's `token` cookie as a Bearer header,
    which `sign` adds for the plugin's own domains only, never for the CDN;
  - a profile's posts, newest first, in pages chained by a `next` cursor (`ctx.pages`, which
    the engine resumes after an interrupted run);
  - albums whose photos need one request each, made only for an album not saved yet (a lazy
    post);
  - one profile picture, kept under its own name and keyed by its path.

Folder layout, under `.flnd/media/exm/<user>/`:
  posts/2026-01-02_<id>_0.jpg     a post's files, `_<num>` only when it has more than one
  albums/<title>/<id>_0.jpg        an album's photos (the album has no date)
  profile/avatar_<id>.jpg          the profile picture
A single post's link (`example.com/p/<code>`) is filed in its own target folder, without a
category.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from flnd.plugin import Ctx, File, Plugin, Post, Request, cookies, route

API = "https://api.example.com"
CDN = "https://cdn.example"
# The CDN refuses a request without the site's Referer, as Instagram's and TikTok's do.
CDN_HEADERS = {"Referer": "https://www.example.com/"}


class Example(Plugin):
    """Example's profiles and single posts, from its JSON API."""

    code = "exm"
    name = "Example"
    domains = ("example.com",)
    api = 2
    # Logged in when this cookie is there.
    login = cookies("token", url="https://www.example.com/login")
    # A post's category, and an album's title when it has one: "posts", "albums/<title>".
    folder = "{category}/{album}"
    fields = ("album",)
    # The day, the post, and the file's number when the post has more than one.
    filename = "{date}_{post}{index}"

    @route(r"example.com/p/{code:[\w-]+}", single=True)
    async def post(self, ctx: Ctx, code: str) -> AsyncIterator[Post]:
        """One post, by its link: `https://example.com/p/C1abc`."""
        yield post_of((await ctx.fetch(f"{API}/posts/{code}")).json)

    @route("exm:{user}", "example.com/{user}")
    async def user(self, ctx: Ctx, user: str) -> Mapping[str, Any]:
        """A profile: `exm:example_user`, or `https://www.example.com/example_user/`. Its JSON,
        which every category below receives, is fetched once per target."""
        return (await ctx.fetch(f"{API}/users/{user}")).json

    @user.category
    async def posts(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The profile's posts, newest first, page by page."""
        async for page in ctx.pages(f"{API}/users/{profile['id']}/posts", cursor="next"):
            for item in page.json["items"]:
                yield post_of(item)

    @user.category(newest_first=False)
    async def albums(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """Each album, its photos listed (one request) only when the album isn't saved yet."""
        for album in profile.get("albums", []):
            yield Post(album["id"], album_files(ctx, album["id"]), album=album["title"],
                       headers=CDN_HEADERS)

    @user.category(folder="profile", key="{path}", newest_first=False)
    async def profile(self, ctx: Ctx, profile: Mapping[str, Any]) -> AsyncIterator[Post]:
        """The profile picture, named and keyed by its path: `profile/avatar_<id>.jpg`."""
        if profile.get("picture"):
            picture = File(profile["picture"], name=f"avatar_{profile['id']}.jpg")
            yield Post(profile["id"], [picture], headers=CDN_HEADERS, album=None)

    def sign(self, request: Request, cookies: Mapping[str, str]) -> Request:
        """The login's token as a Bearer header, on the plugin's own domains only."""
        if not cookies.get("token"):
            return request
        return request.replace(headers={**(request.headers or {}),
                                        "Authorization": f"Bearer {cookies['token']}"})


def post_of(item: Mapping[str, Any]) -> Post:
    """One post of the API, with its files in order; the engine reads `taken_at` as a date."""
    return Post(item["id"], [File(url) for url in item["urls"]], date=item.get("taken_at"),
                headers=CDN_HEADERS, album=None)


def album_files(ctx: Ctx, album: str) -> Any:
    """An album's photos, from its own request, made only when the album is new."""
    async def files() -> list[File]:
        return [File(url) for url in (await ctx.fetch(f"{API}/albums/{album}")).json["urls"]]

    return files


# The API's answers for `example_user`, by URL without its query: each a JSON value, or a
# function of the request's params giving one. 1767312000 is 2026-01-02 and 1767225600
# 2026-01-01, both UTC.
FIXTURES = {
    f"{API}/users/example_user": {"id": "42", "picture": f"{CDN}/avatars/42.jpg",
                                  "albums": [{"id": "A1", "title": "Summer: 2026"}]},
    f"{API}/users/42/posts": lambda params: {
        "": {"items": [{"id": "C1abc", "taken_at": 1767312000,
                        "urls": [f"{CDN}/C1abc_0.jpg", f"{CDN}/C1abc_1.mp4"]}],
             "next": "page2"},
        "page2": {"items": [{"id": "B2def", "taken_at": "1767225600000",
                             "urls": [f"{CDN}/B2def.jpg"]}],
                  "next": "page2"},
    }[params.get("cursor", "")],
    f"{API}/albums/A1": {"urls": [f"{CDN}/A1/1.jpg"]},
    f"{API}/posts/C1abc": {"id": "C1abc", "taken_at": 1767312000, "urls": [f"{CDN}/C1abc_0.jpg"]},
}

# What each form reads, as every plugin's test has: (text, (kind, value)).
ROUTES = [
    ("exm:example_user", ("user", "example_user")),
    ("https://www.example.com/example_user/", ("user", "example_user")),
    ("https://example.com/p/C1abc", ("post", "C1abc")),
]
