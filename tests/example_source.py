"""Example (example.com): a complete small source over fixtures, and the template a new source starts from.

It is run, not only read: tests/test_job.py drives it through the engine on `httpx.MockTransport`,
and through `fakes.collect` on `FIXTURES`. Copy it to `flnd/sources/<code>.py` to start a source:
keep its shape and replace the site's knowledge (its URLs and JSON paths).

What the made-up site gives, and the quirks a real one has that this one copies:
  - a JSON API on api.example.com that wants the login's `token` cookie as a Bearer header, which
    `prepare` adds for the source's own domains only, never for the CDN;
  - a profile's posts, newest first, in pages chained by a `next` cursor (`pages`);
  - albums whose photos need one request each, made only for an album not saved yet (a lazy post);
  - one profile picture, kept under its own name and keyed by its path, as sources did before step 6.

Folder layout, under `.flnd/media/exm/<user>/`:
  posts/2026-01-02_<id>_0.jpg     a post's files, `_<num>` only when it has more than one
  albums/<title>/<id>_0.jpg        an album's photos (the album has no date)
  profile/avatar_<id>.jpg          the profile picture
A single post's link (`example.com/p/<code>`) is filed in its own target folder, without a category.
"""

from __future__ import annotations

from typing import Iterator

from flnd.source import File, Post, Site, Source, pages, target

API = "https://api.example.com"
CDN = "https://cdn.example"
# The CDN refuses a request without the site's Referer, as Instagram's and TikTok's do.
CDN_HEADERS = {"Referer": "https://www.example.com/"}


class Example(Source):
    """Example's profiles and single posts, from its JSON API."""

    code, name = "exm", "Example"
    domains = ("example.com",)
    # Any one of these cookies proves a login.
    needs = ("token",)
    # A post's category, and an album's title when it has one: "posts", "albums/<title>".
    folder = "{category}/{album}"

    @target(r"example.com/p/{code:[\w-]+}")
    def post(self, site: Site, code: str) -> Iterator[Post]:
        """One post, by its link: a route without categories yields its posts itself."""
        yield post_of(site.fetch(f"{API}/posts/{code}").json)

    @target("{user}", "example.com/{user}")
    def user(self, site: Site, user: str) -> dict:
        """A profile: its JSON, which every category below receives, fetched once per target."""
        return site.fetch(f"{API}/users/{user}").json

    @user.category(newest_first=True)
    def posts(self, site: Site, profile: dict) -> Iterator[Post]:
        """The profile's posts, newest first, page by page."""
        for page in pages(lambda cursor: site.fetch(f"{API}/users/{profile['id']}/posts",
                                                    params={"cursor": cursor or ""}).json,
                          lambda page: page.get("next")):
            for item in page["items"]:
                yield post_of(item)

    @user.category
    def albums(self, site: Site, profile: dict) -> Iterator[Post]:
        """Each album, its photos listed (one request) only when the album isn't saved yet."""
        for album in profile.get("albums", []):
            yield Post(album["id"], lambda album=album: album_files(site, album["id"]), album=album["title"],
                       headers=CDN_HEADERS)

    @user.category(folder="profile", filename="avatar_{id}", key="")
    def profile(self, site: Site, profile: dict) -> Iterator[Post]:
        """The profile picture, named and keyed as before step 6: `profile/avatar_<id>.jpg`."""
        if profile.get("picture"):
            yield Post(profile["id"], [File(profile["picture"], headers=CDN_HEADERS)])

    def prepare(self, request, cookies):
        """The login's token as a Bearer header, on the source's own domains only."""
        if cookies.get("token"):
            request.headers["Authorization"] = f"Bearer {cookies['token']}"
        return request


def post_of(item: dict) -> Post:
    """One post of the API, with its files in order; the engine reads `taken_at` as a date."""
    return Post(item["id"], [File(url) for url in item["urls"]], date=item.get("taken_at"), headers=CDN_HEADERS)


def album_files(site: Site, album_id: str) -> list[File]:
    """An album's photos, from its own request."""
    return [File(url) for url in site.fetch(f"{API}/albums/{album_id}").json["urls"]]


# The API's answers for `example_user`, by URL without its query, as `fakes.collect` takes them.
# 1767312000 is 2026-01-02 and 1767225600 2026-01-01, both UTC.
FIXTURES = {
    f"{API}/users/example_user": {"id": "42", "picture": f"{CDN}/avatars/42.jpg",
                                  "albums": [{"id": "A1", "title": "Summer: 2026"}]},
    f"{API}/users/42/posts": lambda url, request: {
        "": {"items": [{"id": "C1abc", "taken_at": 1767312000, "urls": [f"{CDN}/C1abc_0.jpg", f"{CDN}/C1abc_1.mp4"]}],
             "next": "page2"},
        "page2": {"items": [{"id": "B2def", "taken_at": "1767225600000", "urls": [f"{CDN}/B2def.jpg"]}],
                  "next": "page2"},
    }[request["params"]["cursor"]],
    f"{API}/albums/A1": {"urls": [f"{CDN}/A1/1.jpg"]},
    f"{API}/posts/C1abc": {"id": "C1abc", "taken_at": 1767312000, "urls": [f"{CDN}/C1abc_0.jpg"]},
}

# What each form reads, as every source's test has: (text, (kind, value)).
ROUTES = [
    ("exm:example_user", ("user", "example_user")),
    ("https://www.example.com/example_user/", ("user", "example_user")),
    ("https://example.com/p/C1abc", ("post", "C1abc")),
]
