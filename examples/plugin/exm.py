"""example.com as a flnd plugin: a user's posts, newest first, and one post by its link.

Copy this file into your plugin folder (`$FLND_SOURCES`, else `~/.config/flnd/sources/`) and
`flnd download exm:example_user` reads it. The file's name is the source's code.
"""

from flnd.source import File, Post, Site, Source, target

__version__ = "1.0"
API = "https://api.example.com"


class Example(Source):
    """example.com, from its JSON API."""

    code = "exm"
    domains = ("example.com",)
    api = 1
    filename = "{date}_{post}{index}"

    # Routes are tried in order: the post's link first, since `example.com/{user}` takes
    # `example.com/p/…` too.
    @target(r"example.com/p/{id:\w+}")
    def post(self, site: Site, id: str):
        """One post by its link: `https://www.example.com/p/p1`."""
        item = site.fetch(f"{API}/posts/{id}").json
        yield Post(id, [File(url) for url in item["urls"]], date=item["time"])

    @target("exm:{user}", "example.com/{user}")
    def user(self, site: Site, user: str) -> dict:
        """A profile: `exm:example_user`, or `https://www.example.com/example_user`."""
        return site.fetch(f"{API}/users/{user}").json

    @user.category(newest_first=True)
    def posts(self, site: Site, profile: dict):
        """The profile's posts, newest first, page by page."""
        for page in site.pages(f"{API}/users/{profile['id']}/posts", cursor="next"):
            for item in page["items"]:
                yield Post(item["id"], [File(url) for url in item["urls"]], date=item["time"])
