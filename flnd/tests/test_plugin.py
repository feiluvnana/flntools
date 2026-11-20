"""flnd.plugin (plugin API 2) on its own: the checks a plugin's class gets when it is made, routes
and categories, `resolve`, walls, the types a plugin yields and reads, hooks, logins, `Ctx`'s
members and the helpers. Made-up plugins on example.com; no network.
"""

import asyncio
import contextvars
import dataclasses
import datetime
import inspect
import shutil
import threading
import unittest
from unittest import mock

from fakes import Engine, EngineCase
from test_ctx import CtxCase as RunCase
from test_ctx import FakeRaw

from flnd.engine import jobs
from flnd.engine.crawl import Options
from flnd.engine.events import Planned
from flnd.errors import Blocked
from flnd.plugin import (
    API,
    Browser,
    Ctx,
    File,
    Landed,
    Login,
    LoginError,
    NotFound,
    Part,
    Plugin,
    PluginError,
    Post,
    Range,
    Request,
    Settings,
    SiteError,
    Stream,
    Target,
    UsageError,
    best,
    cookies,
    dig,
    meta,
    numbered,
    page_info,
    route,
    wall,
)
from flnd.testing import FakeCtx, Reply, collect
from flnkit import dig as kit_dig
from flnkit import html
from flnkit import net as _net
from flnkit.browser import Page
from flnkit.text import filename


async def listing(self, ctx, user):
    yield Post("p1")


async def profile(self, ctx, user):
    return {"user": user}


async def posts(self, ctx, profile):
    yield Post("p1")


def plugin(**attrs):
    """A plugin class made from `attrs` over a valid one, so each row changes one thing."""
    body = {"code": "exm", "name": "Example", "api": API, "domains": ("example.com",),
            "user": route("exm:{user}", "example.com/{user}")(listing)}
    body.update(attrs)
    return type("Example", (Plugin,), {key: value for key, value in body.items()
                                       if value is not None})


class Example(Plugin):
    code = "exm"
    name = "Example"
    api = API
    domains = ("example.com",)
    login = cookies("sessionid", url="https://www.example.com/accounts/login/")
    browser = Browser.ANY
    walls = ("/accounts/login", "/challenge")
    unsupported = {"example.com/stories/*": "stories aren't kept"}

    @route("example.com/p/{post:[A-Za-z0-9]+}", single=True)
    async def post(self, ctx, post):
        return {"post": post}

    @post.category
    async def photo(self, ctx, profile):
        yield Post(profile["post"])

    @post.category(own=False)
    async def archive(self, ctx, profile):
        yield Post(profile["post"])

    @route(("example.com/permalink.php?story_fbid={post:\\w+}&id={user:\\d+}", "{user}_{post}"),
           newest_first=False)
    async def story(self, ctx, story):
        yield Post(story)

    @route(("example.com/c/{chat:\\d+}", "-100{chat}"), shown="exm:<chat title>")
    async def chat(self, ctx, chat):
        yield Post(chat)

    @route("exm?{words}")
    async def search(self, ctx, words):
        yield Target(f"exm:{words}")

    @route("exm:{user}", "example.com/{user}")
    async def user(self, ctx, user):
        return {"user": user, "tagged_url": f"https://www.example.com/{user}/tagged/"}

    @user.category(url="https://www.example.com/{user}/")
    async def posts(self, ctx, profile):
        yield Post("p1")

    @user.category(own=False, newest_first=False, url="{tagged_url}")
    async def tagged(self, ctx, profile):
        yield Post("t1")


def run(coroutine):
    return asyncio.run(coroutine)


class DeclarationTest(unittest.TestCase):
    """A mistake raises when the class is made, naming the attribute and the fix."""

    def test_each_mistake_is_named(self):
        class NoStart(Login):
            methods = ("qr",)

        class SyncCheck(Login):
            methods = ("qr",)

            async def start(self, ctx, method): ...

            def check(self, ctx):
                return True

        class Pageless(Login):
            methods = ("browser",)

            async def start(self, ctx, method): ...

        class Undescribed(Settings):
            """No attributes here."""

            height: int = 1080

        class Undefaulted(Settings):
            """Attributes:
                height: the tallest video.
            """

            height: int

        def sync_listing(self, ctx, user):
            yield Post("p1")

        async def two(self, ctx, user):
            yield Post(user)

        async def sign(self, request):
            return request

        def resolve(cls, text):
            return None

        def returns(method, category):
            def make():
                made = route("exm:{user}")(method)
                made.category(category)
                return plugin(user=made)

            return make

        rows = [
            ("code", lambda: plugin(code=None), r"Example\.code: missing"),
            ("code", lambda: plugin(code="X"), r"Example\.code: 'X'"),
            ("name", lambda: plugin(name=None), r"exm\.name: set name"),
            ("api", lambda: plugin(api=None), r"exm\.api: missing; set api = 2"),
            ("api", lambda: plugin(api=1), r"written for plugin API 1"),
            ("api", lambda: plugin(api=3), r"needs plugin API 3"),
            ("api", lambda: plugin(api="2"), r"exm\.api: '2'"),
            ("domains", lambda: plugin(domains="example.com"), r"exm\.domains"),
            ("domains", lambda: plugin(domains=("example.com/x",)), r"exm\.domains"),
            ("domains", lambda: plugin(domains=("https:example.com",)),
             r"exm\.domains: 'https:example\.com'.*lowercase hosts, without a scheme"),
            ("domains", lambda: plugin(domains=("Example.com",)), r"exm\.domains: 'Example"),
            ("domains", lambda: plugin(domains=("example.com:8080",)), r"without .* a port"),
            ("route name", lambda: plugin(user=None, date=route("exm:{user}")(listing)),
             r"exm\.date: a route or category named like the date hook replaces it; rename"),
            ("route name", lambda: plugin(user=None, login=route("exm:{user}")(listing)),
             r"exm\.login: .* named like the login declaration replaces it; rename"),
            ("route", lambda: plugin(user=None), r"exm: no route"),
            ("login", lambda: plugin(login="sessionid"), r"exm\.login: set login"),
            ("login", lambda: plugin(login=Login()), r"exm\.login\.methods: set the ways"),
            ("login", lambda: plugin(login=Pageless()), r"exm\.login\.url: the browser method"),
            ("login", lambda: plugin(login=NoStart()), r"exm\.login\.start: implement"),
            ("login", lambda: plugin(login=SyncCheck()), r"exm\.login\.check: make it async"),
            ("pace", lambda: plugin(pace=-1), r"exm\.pace"),
            ("pace", lambda: plugin(pace=True), r"exm\.pace"),
            ("limits", lambda: plugin(limits={"cdn.example.com": 0}), r"exm\.limits"),
            ("browser", lambda: plugin(browser="chrome"), r"exm\.browser.*Browser\.CHROME"),
            ("stealth", lambda: plugin(stealth=1), r"exm\.stealth"),
            ("assets", lambda: plugin(assets=1), r"exm\.assets"),
            ("user_agent", lambda: plugin(user_agent=" "), r"exm\.user_agent"),
            ("walls", lambda: plugin(walls="/login"), r"exm\.walls"),
            ("walls", lambda: plugin(walls=("login",)), r"exm\.walls"),
            ("walls", lambda: plugin(walls=("/login?next=1",)), r"exm\.walls"),
            ("walls", lambda: plugin(walls=("/",)), r"exm\.walls"),
            ("fields", lambda: plugin(fields=("post",)), r"exm\.fields: post is the engine's"),
            ("folder", lambda: plugin(folder="../up"), r"exm\.folder: '\.\./up' leaves"),
            ("folder", lambda: plugin(folder="{album}"), r"exm\.folder: unknown field \{album\}"),
            ("key", lambda: plugin(key="{post"), r"exm\.key: '\{post'"),
            ("key", lambda: plugin(key=""), r"exm\.key: ''; set a template"),
            ("unsupported", lambda: plugin(unsupported={"example.com/{x}": "no"}),
             r"exm\.unsupported: pattern 'example\.com/\{x\}' has \{x\}, but a refusal captures "
             r"nothing; write \* for that segment"),
            ("unsupported", lambda: plugin(unsupported={"example.com/x": ""}), r"exm\.unsupported"),
            ("Settings", lambda: plugin(Settings=dict), r"exm\.Settings: <class 'dict'>"),
            ("Settings", lambda: plugin(Settings=Undescribed),
             r"exm\.Settings\.height: describe it"),
            ("Settings", lambda: plugin(Settings=Undefaulted),
             r"exm\.Settings\.height: give it a default"),
            ("old name", lambda: plugin(needs=("sid",)), r"exm\.needs: plugin API 2 spells it"),
            ("old name", lambda: plugin(scroll="human"), r"exm\.scroll"),
            ("old name", lambda: plugin(single_kinds=("post",)), r"single=True"),
            ("derived", lambda: plugin(routes=()), r"Example\.routes: made from"),
            ("sign", lambda: plugin(sign=lambda self, request: request),
             r"exm\.sign: take \(self, request, cookies\)"),
            ("sign", lambda: plugin(sign=sign), r"exm\.sign: make it def \(not async def\)"),
            ("date", lambda: plugin(date=lambda self, ctx, post: None),
             r"exm\.date: make it async def"),
            ("blocked", lambda: plugin(blocked=sign), r"exm\.blocked: make it def"),
            ("refresh", lambda: plugin(refresh=lambda self, ctx, post: post),
             r"exm\.refresh: make it async def"),
            ("connect", lambda: plugin(connect=two), r"exm\.connect: make it def"),
            ("resolve", lambda: plugin(resolve=resolve), r"exm\.resolve: make it a @classmethod"),
            ("route", lambda: plugin(user=route("exm:{user}")(sync_listing)),
             r"exm\.user: a route without categories lists posts; make it an async def"),
            ("route", returns(listing, posts), r"exm\.user: a route with categories returns"),
            ("category", returns(profile, profile), r"exm\.profile: a category lists posts"),
        ]
        for attribute, make, message in rows:
            with self.subTest(attribute, message=message), self.assertRaisesRegex(
                    PluginError, message):
                make()
        # An empty folder is the target's own; an empty name or key is a mistake (above).
        self.assertEqual(plugin(folder="").folder, "")

    def test_each_pattern_mistake_is_named(self):
        rows = [
            (("cdn.example.com/{user}",),
             r"pattern 'cdn\.example\.com/\{user\}' isn't on its domains"),
            (("abc:{user}",), r"must start with the plugin's code; write it exm:\{user\}"),
            (("exm:{user}", "exm:{user}"), r"is given twice"),
            (("www.example.com/{user}", "example.com/{user}"), r"is given twice"),
            (("exm:{name}",), r"captures 'name', but the method's value parameter is 'user'"),
            (("example.com/{user}/{post}",), r"needs exactly one placeholder, not 2"),
            (("example.com/*",), r"needs exactly one placeholder, not 0"),
            ((("example.com/{a}/{b}", "{a}_{c}"),), r"may name only its placeholders"),
            ((("example.com/{a}", "fixed"),), r"names no placeholder"),
            ((("example.com/{a}", "{a!r}"),), r"may name only its placeholders"),
            (("example.com/{user}/{user}",), r"\{user\} is named twice"),
            (("example.com/{user",), r"a '\{' with no '\}'"),
            (("example.com/user}",), r"a '\}' with no '\{'"),
            (("example.com/{1x}",), r"isn't a placeholder name"),
            (("example.com/{user:(}",), r"pattern 'example\.com/\{user:\(\}'"),
            (("exm:{user}?a={user}",), r"only a link pattern can name a query"),
            (("example.com/x?a",), r"write a query as \?key=\{name\}"),
            ((42,), r"write a pattern, or a \(pattern, value\) pair"),
        ]
        for patterns, message in rows:
            with self.subTest(patterns), self.assertRaisesRegex(PluginError, message):
                plugin(user=route(*patterns)(listing))
        options = [
            (lambda: route("exm:{user}", single=1)(listing), r"route listing: single is True"),
            (lambda: route("exm:{user}", discovers="yes")(listing), r"discovers is True or"),
            (lambda: route("exm:{user}", newest_first=None)(listing), r"newest_first is True"),
            (lambda: route("exm:{user}", shown=3)(listing), r"route listing: shown is the form"),
            (lambda: route("exm:{user}")(profile).category(own=0)(posts),
             r"category posts: own is True or False, not 0"),
            (lambda: route("exm:{user}")(profile).category(newest_first=1)(posts),
             r"category posts: newest_first is True or False, not 1"),
            (lambda: route("exm:{user}")(profile).category(url="{user")(posts),
             r"category posts: url '\{user'"),
            (lambda: route("exm:{user}")(profile).category(lambda self, profile: None),
             r"category <lambda>: take \(self, ctx, profile\)"),
            (lambda: route("exm:{user}")(lambda self, user: None),
             r"route <lambda>: take \(self, ctx, value\)"),
            (lambda: route("exm:{user}", single=True, discovers=True)(listing),
             r"a single route reads one post"),
        ]
        for make, message in options:
            with self.subTest(message), self.assertRaisesRegex(PluginError, message):
                make()
        with self.assertRaisesRegex(PluginError, r"category 'posts' is declared twice"):
            made = route("exm:{user}")(profile)
            made.category(posts)
            made.category(posts)

    def test_a_valid_plugin_derives_its_routes_forms_and_categories(self):
        self.assertEqual([known.kind for known in Example.routes],
                         ["post", "story", "chat", "search", "user"])
        self.assertEqual(Example.categories, ("photo", "archive", "posts", "tagged"))
        self.assertEqual(Example.forms[:3], ("exm:<chat title>", "exm?<words>", "exm:<user>"))
        self.assertIn("https://www.example.com/permalink.php?story_fbid=<post>&id=<user>",
                      Example.forms)
        self.assertEqual(Plugin.Settings, Settings)
        # A base with no code and no route is left alone until a subclass is made.
        self.assertEqual(type("Base", (Plugin,), {}).routes, ())

    def test_a_subclass_keeps_its_bases_routes_and_replaces_one_in_place(self):
        class Again(Example):
            code = "exa"

            @route("exa:{user}", "example.com/{user}")
            async def user(self, ctx, user):
                yield Post(user)

        self.assertEqual([known.kind for known in Again.routes][-1], "user")
        self.assertEqual(Again.resolve("exa:example_user"),
                         Target("exa:example_user", kind="user"))


class RouteTest(unittest.TestCase):
    def test_routes_and_categories_keep_their_options(self):
        post, story, chat, search, user = Example.routes
        rows = [
            (post.single, True), (post.discovers, False), (post.newest_first, True),
            ([c.name for c in post.categories], ["photo", "archive"]),
            ([c.own for c in post.categories], [True, False]),
            (story.newest_first, False), (search.discovers, True), (search.is_query, True),
            (chat.forms("exm"), ["exm:<chat title>"]),
            ([c.newest_first for c in user.categories], [True, False]),
            ([c.own for c in user.categories], [True, False]),
        ]
        for found, expected in rows:
            with self.subTest(expected=expected):
                self.assertEqual(found, expected)
        # A category without its own newest_first takes its route's.
        older = route("exm:{user}", newest_first=False)(profile)
        older.category(posts)
        older.category(newest_first=True)(listing)
        self.assertEqual([c.newest_first for c in older.categories], [False, True])
        # The route is itself on the class and the plain method, bound, on an instance.
        self.assertIs(Example.__dict__["user"], user)
        self.assertEqual(run(Example().user(None, "example_user"))["user"], "example_user")

    def test_a_category_url_renders_or_opens_nothing(self):
        posts, tagged = Example.routes[-1].categories
        rows = [
            (posts, {"user": "example_user"}, "https://www.example.com/example_user/"),
            (tagged, {"tagged_url": "https://www.example.com/x/tagged/"},
             "https://www.example.com/x/tagged/"),
            (tagged, {"tagged_url": ""}, ""),
            (tagged, {}, ""),
            (Example.routes[0].categories[0], {"post": "p1"}, ""),
        ]
        for category, profile, expected in rows:
            with self.subTest(category.name, profile=profile):
                self.assertEqual(category.link(profile), expected)

    def test_resolve(self):
        rows = [
            ("exm:example_user", "exm:example_user", "user"),
            ("example_user", "exm:example_user", "user"),
            ("@example_user", "exm:example_user", "user"),
            ("https://www.example.com/example_user/tagged", "exm:example_user", "user"),
            ("m.example.com/example_user", "exm:example_user", "user"),
            ("https://www.example.com/p/Abc123/", "exm:Abc123", "post"),
            ("https://www.example.com/permalink.php?story_fbid=abc&id=42", "exm:42_abc", "story"),
            ("example.com/c/123", "exm:-100123", "chat"),
            ("exm? cats and dogs ", "exm?cats and dogs", "search"),
            ("?cats", "exm?cats", "search"),
            ("EXM:example_user", "exm:example_user", "user"),
            ("Exm?cats", "exm?cats", "search"),
        ]
        for text, written, kind in rows:
            with self.subTest(text):
                self.assertEqual(Example.resolve(text), Target(written, kind=kind))
        # A host of the domain no pattern names, the bare domain, and text no pattern reads.
        for text in ("https://cdn.example.com/example_user", "https://www.example.com/",
                     "two words"):
            with self.subTest(text):
                self.assertIsNone(Example.resolve(text))
        # A refusal as specific as a route's pattern wins the tie.
        tied = plugin(unsupported={"example.com/*": "pages aren't kept"})
        for made, text, message in (
                (Example, "https://www.example.com/stories/example_user/1",
                 "exm: stories aren't kept"),
                (Example, "exm?  ", "exm: the query is empty"),
                (tied, "https://www.example.com/example_user", "exm: pages aren't kept")):
            with self.subTest(text), self.assertRaisesRegex(UsageError, message):
                made.resolve(text)
        self.assertEqual(tied.resolve("exm:example_user").kind, "user")
        for base in (Plugin, type("Base", (Plugin,), {})):
            with self.subTest(base), self.assertRaisesRegex(TypeError, "call it on a plugin"):
                base.resolve("exm:example_user")

    def test_an_override_passes_the_rest_on(self):
        class Titled(Example):
            code = "ext"

            @classmethod
            def resolve(cls, text):
                if text.startswith("ext:#"):
                    return Target(f"ext:{text[5:]}", kind="chat")
                return super().resolve(text)

        self.assertEqual(Titled.resolve("ext:#Example Chat"),
                         Target("ext:Example Chat", kind="chat"))
        self.assertEqual(Titled.resolve("ext:example_user").kind, "user")

    def test_walls_match_whole_path_segments(self):
        rows = [
            ("https://www.example.com/challenge/", ("/challenge",), "/challenge"),
            ("https://www.example.com/challenge/123/?next=/", ("/challenge",), "/challenge"),
            ("https://www.example.com/accounts/login?next=x", ("/accounts/login/",),
             "/accounts/login/"),
            ("/login", ("/i/flow/login", "/login"), "/login"),
            ("https://www.example.com/challengers/", ("/challenge",), None),
            ("https://www.example.com/challenge_example/", ("/challenge",), None),
            ("https://www.example.com/login.example", ("/login", "/login.php"), None),
            ("https://www.example.com/login_example", ("/login",), None),
            ("https://www.example.com/example_user/login", ("/login",), None),
        ]
        for url, walls, expected in rows:
            with self.subTest(url, walls=walls):
                self.assertEqual(wall(url, walls), expected)


class TypesTest(unittest.TestCase):
    def test_target(self):
        rows = [
            ("ins:example_user", "ins", "example_user", False, "ins:example_user"),
            ("mht?romance", "mht", "romance", True, "mht?romance"),
            ("https://www.example.com/example_user", "", "https://www.example.com/example_user",
             False, "https://www.example.com/example_user"),
            ("tlg", "", "tlg", False, "tlg"),
            ("tlg:", "tlg", "", False, "tlg"),
        ]
        for written, code, value, query, shown in rows:
            with self.subTest(written):
                found = Target(written)
                self.assertEqual((found.plugin, found.value, found.query, str(found)),
                                 (code, value, query, shown))
        self.assertEqual({Target("exm:a", kind="user"), Target("exm:a", kind="user")},
                         {Target("exm:a", kind="user")})
        self.assertNotEqual(Target("exm:a"), Target("exm:a", kind="user"))
        with self.assertRaises(TypeError):
            Target("exm:a", "user")

    def test_post(self):
        async def files():
            return [File("https://cdn.example.com/a.jpg")]

        post = Post(9001, [File("https://cdn.example.com/a.jpg")], date="2026-01-02",
                    album="Trip")
        self.assertEqual((post.id, post.listed, post.lazy, post.fields["album"]),
                         ("9001", "2026-01-02", False, "Trip"))
        moved = post.replace(date="2026-02-03", album="Home")
        self.assertEqual((moved.listed, moved.fields["album"], post.fields["album"]),
                         ("2026-02-03", "Home", "Trip"))
        self.assertEqual(Post("1", date=1, listed=2).replace(date=3).listed, 2)
        pinned = Post("1", pinned=True)
        self.assertEqual((post.pinned, pinned.replace(date=3).pinned,
                          pinned.replace(pinned=False).pinned, pinned.listed),
                         (False, True, False, None))
        with self.assertRaises(PluginError):
            Post("1", pinned=1)
        self.assertTrue(Post("1", files).lazy)
        for made, name in ((post, "id"), (post, "date"), (File("https://a.example.com/x"), "url")):
            with self.subTest(name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(made, name, "changed")
        self.assertEqual(Post("1", (f for f in [File("https://a.example.com/x")])).files,
                         (File("https://a.example.com/x"),))
        for files, message in ((["https://a.example.com/x"], r"wrap each in File"),
                               ("https://a.example.com/x", r"files is a list of File")):
            with self.subTest(files), self.assertRaisesRegex(PluginError, message):
                Post("1", files)
        for empty in (None, "", "  "):
            with self.subTest(id=empty), self.assertRaisesRegex(PluginError, "a post needs an id"):
                Post(empty)
        self.assertEqual(Post(0).id, "0")
        # `category` is a Post argument now (read on a single route), no longer a refused field.
        for field in ("post", "num", "plugin"):
            with self.subTest(field), self.assertRaisesRegex(PluginError, f"{field} is the"):
                Post("1", **{field: "x"})
        single = Post("1", owner="example_user", category="posts")
        self.assertEqual((single.owner, single.category), ("example_user", "posts"))
        self.assertEqual(single.replace(id="2").category, "posts")
        self.assertEqual((Post("1").owner, Post("1").category), (None, None))

    def test_file_takes_exactly_one_source(self):
        async def fetch(part):
            await part.write(b"x")

        made = [File("https://a.example.com/x", size=3),
                File(mirrors=["https://a.example.com/x.7z", "https://b.example.com/x.7z"],
                     password="pw", audio="https://a.example.com/a.mp4"),
                File(fetch=fetch, ranged=True, size=10, name="doc.pdf")]
        self.assertEqual([(f.url, f.mirrors, f.fetch is not None, f.ranged) for f in made],
                         [("https://a.example.com/x", (), False, False),
                          (None, ("https://a.example.com/x.7z", "https://b.example.com/x.7z"),
                           False, False),
                          (None, (), True, True)])
        rows = [
            ({}, r"exactly one of url, mirrors and fetch, not none"),
            ({"url": "https://a.example.com/x", "fetch": fetch}, r"not url and fetch"),
            ({"mirrors": "https://a.example.com/x"}, r"a list of URLs, not one"),
            ({"mirrors": ["https://a.example.com/x", 3]}, r"takes URLs"),
            ({"url": 3}, r"takes a URL"),
            ({"fetch": "x"}, r"takes an async function"),
            ({"fetch": fetch, "audio": "https://a.example.com/a"}, r"a fetch file has none"),
            ({"url": "https://a.example.com/x", "ranged": True}, r"ranged=True\) is for a fetch"),
            ({"url": "https://a.example.com/x", "size": -1}, r"whole number of bytes"),
        ]
        for options, message in rows:
            with self.subTest(options), self.assertRaisesRegex(PluginError, message):
                File(**options)

    def test_text_and_stream_files(self):
        note = File.text("info.md", "# Title\n")
        self.assertEqual((note.name, note.content, note.url, note.mirrors),
                         ("info.md", "# Title\n", None, ()))
        live = File.stream("https://cdn.example.com/v.m3u8", headers={"referer": "x"})
        self.assertEqual((live.playlist, dict(live.headers), live.url),
                         ("https://cdn.example.com/v.m3u8", {"referer": "x"}, None))
        with self.assertRaisesRegex(PluginError, r"a playlist's URL"):
            File.stream("v.m3u8")
        self.assertEqual(note, File.text("info.md", "# Title\n"))
        self.assertIn("info.md", repr(note))

    def test_dash(self):
        def representation(mime, url, *, height=0, bandwidth=1000):
            return (f'<Representation mimeType="{mime}" height="{height}" '
                    f'bandwidth="{bandwidth}"><BaseURL>{url}</BaseURL></Representation>')

        def manifest(*videos, audios=(), extra=""):
            return ('<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>'
                    f'<AdaptationSet>{"".join(videos)}{extra}</AdaptationSet>'
                    f'<AdaptationSet>{"".join(audios)}</AdaptationSet></Period></MPD>')

        cdn = "https://cdn.example.com"
        sample = manifest(representation("video/mp4", f"{cdn}/360.mp4", height=640),
                          representation("video/mp4", f"{cdn}/1080.mp4", height=1920),
                          representation("video/mp4", f"{cdn}/1080b.mp4", height=1920,
                                         bandwidth=9000),
                          representation("video/mp4", "720.mp4", height=1280),
                          audios=[representation("audio/mp4", f"{cdn}/a64.mp4", bandwidth=64),
                                  representation("audio/mp4", f"{cdn}/a95.mp4", bandwidth=95)])
        rows = [
            (sample, None, (f"{cdn}/1080b.mp4", f"{cdn}/a95.mp4")),
            (sample, 1280, (f"{cdn}/360.mp4", f"{cdn}/a95.mp4")),
            (sample, 100, None),
            (manifest(representation("video/mp4", f"{cdn}/v.mp4", height=720),
                      extra='<SegmentTemplate media="seg-$Number$.m4s"/>'), None, None),
            (manifest(representation("video/mp4", f"{cdn}/v.mp4", height=720)), None, None),
            (manifest(representation("video/mp4", f"{cdn}/v/", height=720),
                      audios=[representation("audio/mp4", f"{cdn}/a.mp4")]), None, None),
            ("not xml", None, None), ("", None, None), (None, None, None), ("<MPD/>", None, None),
        ]
        for text, height, expected in rows:
            with self.subTest(height=height, expected=expected):
                found = File.dash(text, height=height)
                self.assertEqual(None if found is None else (found.url, found.audio), expected)

    def test_part_stops_at_its_end(self):
        written, sizes = [], []

        async def sink(data):
            written.append(data)

        async def fill(part):
            await part.write(b"abc")
            part.total(10)
            await part.write(b"de")

        part = Part(sink, offset=3, end=8, sized=sizes.append)
        run(fill(part))
        self.assertEqual((written, sizes, part.offset, part.end), ([b"abc", b"de"], [10], 3, 8))
        with self.assertRaisesRegex(ValueError, r"run past the part's end, 8"):
            run(part.write(b"f"))
        run(Part(sink).write(b"x" * 100))
        for made in (lambda: Part(sink, offset=-1), lambda: Part(sink, offset=5, end=4),
                     lambda: part.total(-1), lambda: part.total(True)):
            with self.subTest(made), self.assertRaises(ValueError):
                made()

    def test_values(self):
        request = Request("https://api.example.com/me", headers={"a": "1"})
        changed = request.replace(method="POST", check=False)
        self.assertEqual((changed.url, changed.method, changed.check, request.method),
                         ("https://api.example.com/me", "POST", False, "GET"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.url = "x"
        span = Range(since=datetime.date(2026, 1, 1))
        self.assertEqual(("2026-03-01" in span, "2025-12-31" in span, None in span,
                          "soon" in span), (True, False, True, True))


class HooksTest(unittest.TestCase):
    def test_defaults(self):
        async def files():
            return [File("https://cdn.example.com/a.jpg")]

        async def defaults():
            example = Example()
            request = Request("https://api.example.com/me")
            self.assertIs(example.sign(request, {"sessionid": "x"}), request)
            async with example.connect(None) as entered:
                self.assertIsNone(entered)
            self.assertIsNone(await example.date(None, Post("1")))
            self.assertIsNone(example.blocked(None))
            fresh = await example.refresh(None, Post("1", files))
            self.assertEqual(fresh.files, (File("https://cdn.example.com/a.jpg"),))
            with self.assertRaisesRegex(NotFound, r"exm: post 1's links expired"):
                await example.refresh(None, Post("1"))

        run(defaults())

    def test_logins(self):
        class Jar:
            cookies = {"sid": "1"}

        login = cookies("sid", "token", url="https://www.example.com/login")
        self.assertEqual((login.names, login.required, "chrome" in login.methods),
                         (("sid", "token"), False, True))
        self.assertTrue(run(login.check(Jar())))
        self.assertIsNone(run(login.whoami(Jar())))
        self.assertFalse(run(Login().check(Jar())))
        with self.assertRaisesRegex(UsageError, "phone: cookies are read"):
            run(login.start(Jar(), "phone"))
        for names, url, message in (((), "https://x.example.com", r"takes the cookie names"),
                                    (("sid",), "http://x.example.com", r"an https URL")):
            with self.subTest(message), self.assertRaisesRegex(PluginError, message):
                cookies(*names, url=url)

    def test_settings_subclasses_are_frozen_keyword_only_dataclasses(self):
        class Own(Settings):
            """Example's keys.

            Attributes:
                height: the tallest video, in pixels.
                audio (bool): keep the audio.
            """

            height: int = 1080
            audio: bool = True

        made = plugin(Settings=Own)
        self.assertEqual(made.Settings(height=720), Own(height=720, audio=True))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Own().height = 1
        with self.assertRaises(TypeError):
            Own(720)
        with self.assertRaisesRegex(PluginError, r"Mutable: mutable default"):
            type("Mutable", (Settings,), {"__annotations__": {"tags": list}, "tags": []})

    def test_ctx_has_exactly_the_contracts_members(self):
        members = {name for name in vars(Ctx) if not name.startswith("_")}
        self.assertEqual(members, {
            "fetch", "pages", "open", "feed", "load", "document", "warm", "click", "shown",
            "handoff", "extension", "storage", "pause", "hold", "call", "ask", "log", "target",
            "when", "time", "settings", "account", "cookies", "state", "secrets", "request"})
        self.assertEqual(str(inspect.signature(Ctx.pages)),
                         "(self, request: 'str | Request', *, cursor: 'str | Callable[[Response], "
                         "str | None]', send: 'str | Callable[[str], "
                         "Mapping[str, Any] | Request]' = 'cursor', bucket: 'str | None' = None, in_page: 'bool' = False, "
                         "resume: 'bool' = False) -> 'AsyncIterator[Response]'")


class HelpersTest(unittest.TestCase):
    def test_meta_unwraps_bbox(self):
        page = html.parse(
            '<script type="application/json">{"other": 1}</script>'
            '<script type="application/json">{"a": [{"__bbox": {"result": {"data": 2}}},'
            ' {"__bbox": {"result": {"data": 3, "__bbox": {"result": 4}}}}]}</script>'
            '<script type="application/json">{broken</script><script>{"x": 5}</script>')
        self.assertEqual(meta(page), [{"other": 1}, {"data": 2},
                                      {"data": 3, "__bbox": {"result": 4}}])

    def test_page_info(self):
        read = page_info("data.user.media.page_info")
        rows = [
            ({"data": {"user": {"media": {"page_info": {"has_next_page": True,
                                                         "end_cursor": "c2"}}}}}, (True, "c2")),
            ({"data": {"user": {"media": {"page_info": {"has_next_page": False,
                                                         "end_cursor": ""}}}}}, (False, None)),
            ({"data": {}}, None),
            ([1, 2], None),
        ]
        for value, expected in rows:
            with self.subTest(expected=expected):
                self.assertEqual(read(value), expected)

    def test_best(self):
        streams = [Stream("a", height=720, width=1280, bitrate=5),
                   Stream("b", height=1080, width=1920, bitrate=1),
                   Stream("c", height=1080, width=1920, bitrate=9),
                   Stream("d", height=1920, width=1080, bitrate=2)]
        # With a width missing anywhere, heights are compared: a 1080p stream without one beats
        # a 640x360 with one, and bits break a tie.
        mixed = [Stream("small", height=360, width=640, bitrate=9),
                 Stream("tall", height=1080, bitrate=1), Stream("taller", height=1080, bitrate=2)]
        rows = [(streams, None, "c"), (streams, 1080, "c"), (streams, 720, "a"),
                (streams, 480, None), (mixed, None, "taller"), (mixed, 720, "small"),
                (mixed[:2], None, "tall")]
        for candidates, height, expected in rows:
            with self.subTest(height=height, expected=expected):
                found = best(candidates, height=height)
                self.assertEqual(None if found is None else found.url, expected)
        self.assertIsNone(best([]))

    def test_numbered_and_dig(self):
        rows = [((7,), {}, "07"), ((7,), {"suffix": "_cover"}, "07_cover"),
                ((2,), {"suffix": ".webp", "width": 3}, "002.webp"), ((123,), {}, "123")]
        for args, options, expected in rows:
            with self.subTest(expected):
                self.assertEqual(numbered(*args, **options), expected)
        self.assertIs(dig, kit_dig.dig)
        self.assertEqual(dig({"a": [{"b": 1}]}, "a[0].b"), 1)
        with self.assertRaises(ValueError):
            dig({}, "a[")


# The engine's `Ctx` (src/engine/ctx.py), on a real plugin run with tests/test_ctx.py's fake
# browser and `flnkit.net.fake`.

API_URL = "https://api.example.com"
PROFILE = "https://www.example.com/example_user/"


class Wired(Example):
    """`Example` with a token signed onto its own domains and a fixed User-Agent."""

    code = "exw"
    user_agent = "example-agent/1"
    walls = ("/accounts/login", "/challenge", "/login", "/login.php")

    def sign(self, request, cookies):
        return request.replace(headers={**(request.headers or {}), "X-Signed": "1"})


class CtxCase(RunCase):
    """tests/test_ctx.py's case, its `Ctx` for `exw:example_user`."""

    def ctx(self, plugin=Wired, **options):
        return super().ctx(plugin, **options)


class PagesTest(CtxCase):
    def collected(self, run, ctx):
        async def pages():
            return [page.json async for page in ctx.pages(f"{API_URL}/posts", cursor="next")]

        return self.call(run, pages())

    def listing(self, start):
        from flnd.engine.ctx import Listing

        return Listing(start, label="exw:example_user: posts")

    def test_a_429_on_a_resumed_page_keeps_its_cursor(self):
        """S6: the saved cursor outlives a rate limit; the next run resumes from it."""
        self.serve = lambda request: _net.reply(429, headers={"retry-after": "1"})
        run, ctx = self.ctx()
        ctx.listing = listing = self.listing("c42")
        with self.assertRaises(Blocked):
            self.collected(run, ctx)
        self.assertEqual((listing.cursor, listing.refused), ("c42", False))

    def test_a_refused_cursor_lists_from_the_newest_and_a_page_moves_it(self):
        def serve(request):
            cursor = request.url.params.get("cursor")
            if cursor == "stale":
                return _net.reply(400)
            return _net.reply(200, json={"items": [cursor], "next": "c2" if cursor is None
                                         else None})

        self.serve = serve
        run, ctx = self.ctx()
        ctx.listing = listing = self.listing("stale")
        pages = self.collected(run, ctx)
        self.assertEqual(pages, [{"items": [None], "next": "c2"}, {"items": ["c2"], "next": None}])
        self.assertEqual((listing.refused, listing.cursor, listing.read), (True, "c2", 2))


class OpenTest(CtxCase):
    def opened(self, statuses, url=PROFILE):
        self.raw = FakeRaw(statuses)
        run, ctx = self.ctx()
        return run, ctx, lambda: self.call(run, ctx.open(url))

    def landed(self, lands, name):
        """`open` of `example.com/<name>/`, its tab landing where `lands` says."""
        url = f"https://www.example.com/{name}/"
        _, _, opened = self.opened([200] * 4, url)
        original = self.raw.goto

        async def goto(target, **options):
            answer = await original(target, **options)
            self.raw.url = lands.get(target, target)
            return type(answer)(status=answer.status, url=self.raw.url, headers={})

        self.raw.goto = goto
        return opened

    def test_a_404_page_is_not_found_and_a_page_says_where_it_landed(self):
        """S5: `open` keeps the page's status: a 404 raises `NotFound`, never a quiet False."""
        _, _, opened = self.opened([200, 404])
        self.assertEqual(opened(), Landed(PROFILE, 200))
        with self.assertRaises(NotFound):
            opened()

    def test_landed_keeps_the_pages_own_status(self):
        _, _, opened = self.opened([203])
        self.assertEqual(opened(), Landed(PROFILE, 203))

    def test_landing_on_a_wall_is_held_twice_then_a_login_error(self):
        wall = "https://www.example.com/accounts/login/?next=/example_user/"
        opened = self.landed({PROFILE: wall}, "example_user")
        with self.assertRaises(LoginError):
            opened()
        self.assertEqual(len(self.raw.loaded), 3)
        self.assertGreaterEqual(self.now, 150.0)

    def test_a_profile_named_like_a_wall_is_read_as_a_profile(self):
        # The engine's wall rule matches whole path segments, so these names are no wall.
        self.assertEqual(self.landed({}, "login.example")().status, 200)
        for name in ("challenge_example", "challengers"):
            with self.subTest(name=name):
                self.assertEqual(self.landed({}, name)().status, 200)
                self.assertEqual(self.raw.loaded, [f"https://www.example.com/{name}/"])
        self.assertEqual(self.landed({}, "login_example")().url,
                         "https://www.example.com/login_example/")


class FetchTest(CtxCase):
    def fetched(self, request, *, serve=None, plugin=Wired, **options):
        if serve is not None:
            self.serve = serve
        run, ctx = self.ctx(plugin)
        return run, self.call(run, ctx.fetch(request, **options))

    def test_signed_and_its_user_agent_on_its_own_domains_only(self):
        run, _ = self.fetched(f"{API_URL}/me")
        self.call(run, self.ctx()[1].fetch("https://cdn.example/a.jpg"))
        own, other = self.requests
        self.assertEqual((own.headers["x-signed"], own.headers["user-agent"]),
                         ("1", "example-agent/1"))
        self.assertNotIn("x-signed", other.headers)
        self.assertNotEqual(other.headers.get("user-agent"), "example-agent/1")

    def test_a_refusal_unchecked_is_read_and_never_expires_the_account(self):
        for status in (401, 403, 404, 410):
            with self.subTest(status=status):
                run, response = self.fetched(Request(f"{API_URL}/private", check=False),
                                             serve=lambda request, status=status:
                                             _net.reply(status))
                self.assertEqual(response.status, status)
                self.assertEqual(run.scheduler.expired, set())

    def test_a_checked_403_is_a_login_error_and_expires_the_account(self):
        self.serve = lambda request: _net.reply(403)
        run, ctx = self.ctx()
        with self.assertRaises(LoginError):
            self.call(run, ctx.fetch(f"{API_URL}/private"))
        self.assertEqual(run.scheduler.expired, {("exw", None)})

    def test_a_5xx_is_tried_again_and_the_last_answer_read(self):
        def serve(request):
            self.requests.append(request)
            return _net.reply(503)

        _, response = self.fetched(Request(f"{API_URL}/busy", check=False), serve=serve)
        self.assertEqual((response.status, len(self.requests)), (503, 5))

    def test_a_post_is_never_tried_again(self):
        """A POST's 5xx comes back at once: the server may have acted (a 2FA code sent)."""
        def serve(request):
            self.requests.append(request)
            return _net.reply(503)

        _, response = self.fetched(Request(f"{API_URL}/login", method="POST", check=False),
                                   serve=serve)
        self.assertEqual((response.status, len(self.requests)), (503, 1))
        with self.assertRaises(SiteError):
            self.fetched(Request(f"{API_URL}/login", method="POST", body={"code": "1"}),
                         serve=serve)
        self.assertEqual(len(self.requests), 2)

    def test_blocked_holds_for_what_it_reads(self):
        class Strict(Wired):
            code = "exs"

            def blocked(self, response):
                return 60.0 if b"slow down" in response.content else None

        answers = iter([_net.reply(200, content=b"slow down"), _net.reply(200, json={"ok": 1})])
        _, response = self.fetched(f"{API_URL}/me", plugin=Strict,
                                   serve=lambda request: next(answers))
        self.assertEqual(response.json, {"ok": 1})
        self.assertGreaterEqual(self.now, 60.0)

    def test_a_page_body_is_capped(self):
        from flnd.engine import ctx

        with mock.patch.object(ctx, "PAGE_LIMIT", 10), \
                self.assertRaisesRegex(SiteError, "too big to read as a page"):
            self.fetched(f"{API_URL}/big", serve=lambda request: _net.reply(200, content=b"x" * 11))

    def test_a_redirect_to_a_wall_is_one_and_a_direct_request_is_not(self):
        wall = "https://www.example.com/accounts/login/"

        def serve(request):
            self.requests.append(request)
            if request.url.path == "/moved":
                return _net.reply(302, headers={"Location": wall})
            return _net.reply(200, json={"path": request.url.path})

        _, response = self.fetched(wall, serve=serve)
        self.assertEqual(response.json, {"path": "/accounts/login/"})
        with self.assertRaises(LoginError):
            self.fetched("https://www.example.com/moved", serve=serve)
        self.assertGreaterEqual(self.now, 150.0)

    def test_blocked_and_a_login_are_read_on_its_own_domains_only(self):
        class Strict(Wired):
            code = "exs"

            def blocked(self, response):
                return 60.0

        for url in ("https://cdn.example/a.jpg", "https://notexample.com/a",
                    "https://example.com.evil/a"):
            with self.subTest(url):
                self.requests.clear()
                with self.assertRaises(SiteError) as raised:
                    self.fetched(url, plugin=Strict, serve=lambda request: (
                        self.requests.append(request), _net.reply(403))[1])
                self.assertNotIsInstance(raised.exception, LoginError)
                self.assertEqual(self.now, 0.0)
                self.assertNotIn("x-signed", self.requests[0].headers)

    def test_a_post_signing_error_names_no_secret(self):
        class Leaky(Wired):
            code = "exl"

            def sign(self, request, cookies):
                return {"Authorization": "Bearer TOKEN-SECRET"}

        with self.assertRaises(PluginError) as raised:
            self.fetched(f"{API_URL}/me", plugin=Leaky)
        self.assertNotIn("TOKEN-SECRET", str(raised.exception))
        self.assertIn("dict", str(raised.exception))

    def test_an_in_page_answer_is_capped_and_pages_go_in_page(self):
        from flnd.engine import ctx

        big = _net.Response(200, f"{API_URL}/feed", {}, b"x" * 11)
        with mock.patch.object(Page, "fetch", new=mock.AsyncMock(return_value=big)), \
                mock.patch.object(ctx, "PAGE_LIMIT", 10), \
                self.assertRaisesRegex(SiteError, "too big to read as a page"):
            self.fetched(f"{API_URL}/feed", in_page=True)
        pages = [_net.Response(200, f"{API_URL}/feed", {}, b'{"next": "c2"}'),
                 _net.Response(200, f"{API_URL}/feed", {}, b'{"next": null}')]
        run, made = self.ctx()

        async def listed():
            return [page.json async for page in made.pages(f"{API_URL}/feed", cursor="next",
                                                           in_page=True)]

        with mock.patch.object(Page, "fetch", new=mock.AsyncMock(side_effect=pages)) as sent:
            self.assertEqual(self.call(run, listed()), [{"next": "c2"}, {"next": None}])
        self.assertEqual(sent.call_args.args[0], f"{API_URL}/feed?cursor=c2")
        self.assertEqual(self.requests, [])

    def test_a_redirect_off_its_domains_carries_nothing_sign_added(self):
        """A signed request followed to another host goes unsigned."""
        from flnd.engine import ctx
        from flnd.engine.config import PluginConfig

        def serve(request):
            self.requests.append(request)
            if request.url.host == "api.example.com":
                return _net.reply(302, headers={"Location": "https://cdn.example/elsewhere"})
            return _net.reply(200, content=b"ok")

        self.serve = serve
        jar = [{"name": "token", "value": "TOKEN-SECRET", "domain": ".example.com", "path": "/"}]
        session = ctx._network(Wired(), jar, PluginConfig(), handles=4)
        client = _net.Client(session)

        async def fetched():
            try:
                return await client.fetch(f"{API_URL}/me")
            finally:
                await session.close()

        self.assertEqual(asyncio.run(fetched()).url, "https://cdn.example/elsewhere")
        first, second = self.requests
        self.assertIn("x-signed", first.headers)
        self.assertNotIn("x-signed", second.headers)
        self.assertNotIn("TOKEN-SECRET", str(dict(second.headers)))


class ReadsTest(CtxCase):
    def test_secrets_are_written_only_during_a_login(self):
        from flnd.engine.ctx import start

        class Phone(Login):
            methods = ("phone",)

            async def start(self, ctx, method):
                ctx.secrets["session"] = "s1"

        run, ctx = self.ctx()
        with self.assertRaisesRegex(PluginError, "only during Login.start"):
            ctx.secrets["session"] = "s0"
        self.call(run, start(Phone(), ctx, "phone"))
        self.assertEqual(dict(ctx.secrets), {"session": "s1"})

    def test_a_task_a_login_started_cant_write_after_it(self):
        from flnd.engine.ctx import start

        left = {}

        class Phone(Login):
            methods = ("phone",)

            async def start(self, ctx, method):
                gate = asyncio.Event()

                async def later():
                    await gate.wait()
                    ctx.secrets["late"] = "x"

                left["task"], left["gate"] = asyncio.get_running_loop().create_task(later()), gate

        run, made = self.ctx()
        self.call(run, start(Phone(), made, "phone"))

        async def release():
            left["gate"].set()
            await left["task"]

        with self.assertRaisesRegex(PluginError, "only during Login.start"):
            self.call(run, release())

    def test_its_cookies_are_its_domains_only(self):
        jar = [{"name": "sid", "value": "1", "domain": ".example.com", "path": "/"},
               {"name": "cdn", "value": "2", "domain": "cdn.example", "path": "/"}]
        run, made = self.ctx()
        run.session.add(jar)
        self.assertEqual(dict(made.cookies), {"sid": "1"})
        seen = {}

        class Seeing(Wired):
            code = "exc"

            def sign(self, request, cookies):
                seen.update(cookies)
                return request

        run, made = self.ctx(Seeing)
        run.session.add(jar)
        self.call(run, made.fetch(f"{API_URL}/me"))
        self.assertEqual(seen, {"sid": "1"})

    def test_a_fetch_takes_only_the_browser_cookies_of_its_host(self):
        run, made = self.ctx()
        made._tabs._tabs = object()
        browser = [{"name": "sid", "value": "1", "domain": ".example.com", "path": "/"},
                   {"name": "other", "value": "2", "domain": ".example.org", "path": "/"}]
        with mock.patch.object(type(made._tabs), "cookies", return_value=browser):
            self.call(run, made.fetch(f"{API_URL}/me"))
        made._tabs._tabs = None
        self.assertEqual(set(run.session.names), {"sid"})

    def test_ask_runs_off_the_loop(self):
        from flnd.engine.ctx import Ctx
        from flnd.ui import forms

        run, _ = self.ctx()
        run.settings.interactive = True
        made = Ctx(run, Target("exw:", kind="account"), login=True)
        ticked = threading.Event()

        def typed(message, *, secret=False):
            return "typed" if ticked.wait(5) else "the loop was blocked"

        async def asked():
            async def tick():
                await asyncio.sleep(0)
                ticked.set()

            ticker = asyncio.ensure_future(tick())
            answer = await made.ask("Code?")
            await ticker
            return answer

        with mock.patch.object(forms, "interactive", return_value=True), \
                mock.patch.object(forms, "ask", side_effect=typed):
            self.assertEqual(self.call(run, asked()), "typed")

    def test_state_takes_json_values_and_time_has_a_day(self):
        _, ctx = self.ctx()
        ctx.state["seen"] = {"a": [1, None]}
        self.assertEqual(ctx.state["seen"], {"a": [1, None]})
        with self.assertRaises(PluginError):
            ctx.state["bad"] = ("a", "tuple")
        self.assertEqual(ctx.time.day(), datetime.date(2026, 1, 1))

    def test_warm_loads_the_pages_a_listing_opens_next(self):
        run, ctx = self.ctx()
        self.make_raw = lambda: FakeRaw([200])
        self.call(run, ctx.warm(["https://www.example.com/a/", "", "https://www.example.com/b/"]))
        self.assertEqual(sorted(ctx._tabs._tabs._warmed), ["https://www.example.com/a/",
                                                           "https://www.example.com/b/"])


# The engine end to end (`fakes.EngineCase`): file names, a single route's categories, and
# ranged fetches.

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 60
CDN_URL = "https://cdn.example"


class Saving(Plugin):
    """A post of the files `files` holds, one route per case of the engine's naming."""

    code = "exn"
    name = "Saving"
    api = API
    domains = ("example.com",)
    filename = "{post}_{num}"
    # Set by each test: the files of the one post.
    files: tuple = ()

    @route("exn:{user}")
    async def user(self, ctx, user):
        yield Post("p1", type(self).files, date="2026-01-02")

    @route("example.com/p/{post:\\w+}", single=True)
    async def post(self, ctx, post):
        return {"post": post}

    @post.category
    async def photo(self, ctx, profile):
        yield Post(profile["post"], [File(f"{CDN_URL}/{profile['post']}.jpg")])

    @post.category(own=False)
    async def archive(self, ctx, profile):
        yield Post(profile["post"], [File(f"{CDN_URL}/{profile['post']}.zip", name="a.zip")])


class Owned(Plugin):
    """A profile and single posts that name their owner and category."""

    code = "exo"
    name = "Owned"
    api = API
    domains = ("owned.example",)
    filename = "{post}_{num}"
    # Set by each test: who owns the single post, and its category.
    who, kind = "example_user", "posts"

    @route("owned.example/p/{post:\\w+}", single=True)
    async def post(self, ctx, post):
        yield Post(post, [File(f"{CDN_URL}/p1.jpg")], owner=self.who, category=self.kind)

    @route("exo:{user}")
    async def user(self, ctx, user):
        return {"user": user}

    @user.category
    async def posts(self, ctx, profile):
        yield Post("p1", [File(f"{CDN_URL}/p1.jpg")])

    @user.category(own=False)
    async def reels(self, ctx, profile):
        return
        yield


class OwnerTest(EngineCase):
    def saved(self, target, who="example_user", kind="posts", **options):
        self.routes[f"{CDN_URL}/p1.jpg"] = _net.reply(200, content=JPEG)
        with mock.patch.multiple(Owned, who=who, kind=kind):
            files = self.run_engine(Owned, targets=[target], options=Options(**options))
        folder = self.home / "media" / "exo"
        return files, sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*")
                             if path.is_file() and not path.name.endswith(".json"))

    def test_a_single_post_goes_to_its_owners_category_folder(self):
        link = "https://owned.example/p/p1"
        rows = [  # owner, category, options, where it goes
            ("example_user", "posts", {}, "example_user/posts/p1_0.jpg"),
            ("example_user", "reels", {"folder": "reels:clips/{category}"},
             "example_user/clips/reels/p1_0.jpg"),
            ("example_user", "posts", {"folder": "reels:clips"}, "example_user/posts/p1_0.jpg"),
            ("x/y", "posts", {}, f"{filename('x/y')}/posts/p1_0.jpg"),
            (None, "posts", {}, "p1/p1_0.jpg"),
        ]
        for who, kind, options, expected in rows:
            with self.subTest(who=who, options=options):
                files, found = self.saved(link, who, kind, **options)
                self.assertEqual(found, [expected])
                self.assertEqual("no owner found" in "".join(self.lines()), who is None)
                shutil.rmtree(self.home)

    def test_a_post_saved_by_link_is_found_in_place_by_its_profile(self):
        self.assertEqual(self.saved("https://owned.example/p/p1")[0], 1)
        files, found = self.saved("exo:example_user")
        self.assertEqual((files, found), (0, ["example_user/posts/p1_0.jpg"]))


class EngineTest(EngineCase):
    def saved(self, *files, targets=("exn:example_user",), **options):
        Saving.files = files
        self.addCleanup(setattr, Saving, "files", ())
        self.run_engine(Saving, targets=list(targets), **options)
        folder = self.home / "media" / "exn"
        return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*")
                      if path.is_file() and not path.name.endswith(".json"))

    def test_a_file_is_named_as_the_kit_names_it(self):
        """5.3: a name with its extension keeps it; a stem takes its content type's, then its
        URL's media extension, then none; then the bytes rename one of another known format.
        The kit, which has no bytes, names every other row the same."""
        rows = [  # (file, answer's Content-Type, bytes, name saved)
            (File(f"{CDN_URL}/a.png", name="cover_12.gif"), "", b"plain", "cover_12.gif"),
            (File(f"{CDN_URL}/b", name="avatar_42"), "image/png", b"plain", "avatar_42.png"),
            (File(f"{CDN_URL}/c.png"), "", JPEG, "p1_2.jpg"),
            (File(f"{CDN_URL}/d.php", name="01"), "", b"plain", "01"),
            (File(f"{CDN_URL}/e"), "", b"plain", "p1_4"),
            (File(f"{CDN_URL}/f.jpg"), "image/png", b"plain", "p1_5.png"),
            (File(f"{CDN_URL}/g.mp4"), "", b"plain", "p1_6.mp4"),
        ]
        routes = {}
        for file, kind, body, _ in rows:
            headers = {"Content-Type": kind} if kind else {}
            self.routes[file.url] = _net.reply(200, content=body, headers=headers)
            routes[file.url] = Reply(body, headers=headers)
        found = self.saved(*[file for file, *_ in rows])
        self.assertEqual(found, sorted(f"example_user/{name}" for *_, name in rows))
        kit = collect(Saving, "exn:example_user", ctx=FakeCtx(Saving, routes=routes))
        self.assertEqual([found.path for found, (_, _, body, _) in zip(kit, rows) if body != JPEG],
                         [name for _, _, body, name in rows if body != JPEG])

    def test_a_dry_run_shows_the_path_that_is_saved(self):
        self.routes[f"{CDN_URL}/d.php"] = _net.reply(200, content=b"plain")
        Saving.files = (File(f"{CDN_URL}/d.php", name="01"),)
        self.addCleanup(setattr, Saving, "files", ())
        events = []
        Engine(home=self.home, sources={"exn": Saving}, emit=events.append).download(
            ["exn:example_user"], dry=True)
        planned = [event.path.name for event in events if isinstance(event, Planned)]
        self.assertEqual(planned, ["01"])
        self.assertEqual(self.saved(*Saving.files), ["example_user/01"])

    def ranged(self, count):
        """`count` ranged fetch files of 4 small segments each, saved at once on one host."""
        size = 4 * (64 << 10) + 123
        blob = (bytes(range(256)) * (size // 256 + 1))[:size]

        async def fetch(part):
            end = part.end if part.end is not None else size
            for at in range(part.offset, end, 10000):
                await part.write(blob[at:min(at + 10000, end)])

        files = [File(fetch=fetch, size=size, ranged=True, name=f"f{n}.bin") for n in range(count)]
        with mock.patch.object(jobs, "SPLIT_FILE", 128 << 10), \
                mock.patch.object(jobs, "SPLIT_RANGE", 64 << 10):
            outcome = {}
            context = contextvars.copy_context()
            worker = threading.Thread(target=context.run, args=(
                lambda: outcome.setdefault("found", self.saved(*files)),), daemon=True)
            worker.start()
            worker.join(60)
        self.assertNotIn(worker, threading.enumerate(), f"{count} ranged files hung")
        folder = self.home / "media/exn/example_user"
        return outcome["found"], [(folder / f"f{n}.bin").read_bytes() == blob
                                  for n in range(count)]

    def test_many_ranged_fetches_on_one_host_all_finish_whole(self):
        for count in (8, 16):
            with self.subTest(count=count):
                found, whole = self.ranged(count)
                self.assertEqual(len(found), count)
                self.assertTrue(all(whole))

    def test_a_ranged_fetch_is_one_transfer_when_its_host_has_no_room(self):
        size = 3 * jobs.SPLIT_RANGE
        asked = []

        async def fetch(part):
            asked.append((part.offset, part.end))
            await part.write(bytes(size - part.offset))

        with mock.patch.object(jobs.Scheduler, "room", return_value=False):
            self.saved(File(fetch=fetch, size=size, ranged=True, name="big.bin"))
        self.assertEqual(asked, [(0, None)])

    def test_a_ranged_fetchs_sidecar_is_written_per_piece_by_one_writer(self):
        from flnkit import files as _files

        kept = []
        real = _files.Part.keep

        def keep(part, meta):
            kept.append(threading.get_ident())
            return real(part, meta)

        with mock.patch.object(_files.Part, "keep", keep):
            found, whole = self.ranged(1)
        self.assertTrue(all(whole))
        # Four segments under 4 MiB each: the plan once, and each segment's end.
        self.assertLessEqual(len(kept), 5)

    def test_a_discovering_routes_categories_run_and_their_targets_are_followed(self):
        class Finder(Plugin):
            code = "exf"
            name = "Finder"
            api = API
            domains = ("example.net",)

            @route("exf:{user}", discovers=True)
            async def user(self, ctx, user):
                return {"user": user}

            @user.category
            async def following(self, ctx, profile):
                yield Target("exn:example_user")

        self.routes[f"{CDN_URL}/a.jpg"] = _net.reply(200, content=JPEG)
        Saving.files = (File(f"{CDN_URL}/a.jpg"),)
        self.addCleanup(setattr, Saving, "files", ())
        self.run_engine(Finder, Saving, targets=["exf:example_user"])
        self.assertTrue((self.home / "media/exn/example_user/p1_0.jpg").is_file(),
                        self.failures())

    def test_a_single_route_takes_its_own_categories_by_default(self):
        self.routes[f"{CDN_URL}/P9.jpg"] = _net.reply(200, content=JPEG)
        found = self.saved(targets=("https://example.com/p/P9",))
        self.assertEqual(found, ["P9/photo/P9_0.jpg"])
        self.routes[f"{CDN_URL}/P9.zip"] = _net.reply(200, content=b"PK\x05\x06" + b"\0" * 18)
        found = self.saved(targets=("https://example.com/p/P9",),
                           options=Options(categories=("archive",)))
        self.assertIn("P9/archive/a.zip", found)

    def test_a_ranged_fetch_comes_in_parts_that_each_stop_at_their_end(self):
        size = 3 * jobs.SPLIT_RANGE
        blob = bytes(range(256)) * (size // 256)
        asked = []

        async def fetch(part):
            asked.append((part.offset, part.end))
            end = part.end if part.end is not None else size
            for at in range(part.offset, end, 1 << 20):
                await part.write(blob[at:min(at + (1 << 20), end)])

        found = self.saved(File(fetch=fetch, size=size, ranged=True, name="big.bin"))
        self.assertEqual(found, ["example_user/big.bin"])
        self.assertEqual((self.home / "media/exn/example_user/big.bin").read_bytes(), blob)
        step = jobs.SPLIT_RANGE
        self.assertEqual(sorted(asked), [(0, step), (step, 2 * step), (2 * step, size)])


if __name__ == "__main__":
    unittest.main()
