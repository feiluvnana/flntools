"""flnd.plugin, plugin API 2, on its own: every check a plugin's class gets when it is made,
routes and categories (patterns, value pairs, single, discovers, own, url), `resolve` and its
refusals, walls by whole segments, the types a plugin yields and reads, the hooks' defaults,
logins, `Ctx`'s members, and the helpers. Made-up plugins on example.com; no network.
"""

import asyncio
import dataclasses
import datetime
import inspect
import unittest

from flnd.plugin import (
    API,
    Browser,
    Ctx,
    File,
    Login,
    NotFound,
    Part,
    Plugin,
    Post,
    Range,
    Request,
    Settings,
    SourceError,
    Stream,
    Target,
    UsageError,
    _wall,
    best,
    cookies,
    dig,
    meta,
    numbered,
    page_info,
    route,
)
from flnkit import dig as kit_dig
from flnkit import html


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
            ("user_agent", lambda: plugin(user_agent=" "), r"exm\.user_agent"),
            ("walls", lambda: plugin(walls="/login"), r"exm\.walls"),
            ("walls", lambda: plugin(walls=("login",)), r"exm\.walls"),
            ("walls", lambda: plugin(walls=("/login?next=1",)), r"exm\.walls"),
            ("walls", lambda: plugin(walls=("/",)), r"exm\.walls"),
            ("fields", lambda: plugin(fields=("post",)), r"exm\.fields: post is the engine's"),
            ("folder", lambda: plugin(folder="../up"), r"exm\.folder: '\.\./up' leaves"),
            ("folder", lambda: plugin(folder="{album}"), r"exm\.folder: unknown field \{album\}"),
            ("key", lambda: plugin(key="{post"), r"exm\.key: '\{post'"),
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
                    SourceError, message):
                make()

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
            with self.subTest(patterns), self.assertRaisesRegex(SourceError, message):
                plugin(user=route(*patterns)(listing))
        with self.assertRaisesRegex(SourceError, r"route <lambda>: take \(self, ctx, value\)"):
            route("exm:{user}")(lambda self, user: None)
        with self.assertRaisesRegex(SourceError, r"a single route reads one post"):
            route("exm:{user}", single=True, discovers=True)(listing)
        with self.assertRaisesRegex(SourceError, r"category <lambda>: take \(self, ctx, profile\)"):
            route("exm:{user}")(profile).category(lambda self, profile: None)
        with self.assertRaisesRegex(SourceError, r"category 'posts' is declared twice"):
            made = route("exm:{user}")(profile)
            made.category(posts)
            made.category(posts)
        with self.assertRaisesRegex(SourceError, r"category posts: url '\{user'"):
            route("exm:{user}")(profile).category(url="{user")(posts)
        options = [
            (lambda: route("exm:{user}", single=1)(listing), r"route listing: single is True"),
            (lambda: route("exm:{user}", discovers="yes")(listing), r"discovers is True or"),
            (lambda: route("exm:{user}", newest_first=None)(listing), r"newest_first is True"),
            (lambda: route("exm:{user}", shown=3)(listing), r"route listing: shown is the form"),
            (lambda: route("exm:{user}")(profile).category(own=0)(posts),
             r"category posts: own is True or False, not 0"),
            (lambda: route("exm:{user}")(profile).category(newest_first=1)(posts),
             r"category posts: newest_first is True or False, not 1"),
        ]
        for make, message in options:
            with self.subTest(message), self.assertRaisesRegex(SourceError, message):
                make()

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
        self.assertEqual(Again.resolve("exa:example_user"), Target("exa:example_user",
                                                                   kind="user"))


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

        self.assertEqual(Titled.resolve("ext:#Example Chat"), Target("ext:Example Chat",
                                                                     kind="chat"))
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
                self.assertEqual(_wall(url, walls), expected)


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
                          pinned.replace(pinned=False).pinned, pinned.listed), (False, True, False,
                                                                                None))
        with self.assertRaises(SourceError):
            Post("1", pinned=1)
        lazy = Post("1", files)
        self.assertTrue(lazy.lazy)
        for made, name in ((post, "id"), (post, "date"), (File("https://a.example.com/x"), "url")):
            with self.subTest(name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(made, name, "changed")
        self.assertEqual(Post("1", (f for f in [File("https://a.example.com/x")])).files,
                         (File("https://a.example.com/x"),))
        for files, message in ((["https://a.example.com/x"], r"wrap each in File"),
                               ("https://a.example.com/x", r"files is a list of File")):
            with self.subTest(files), self.assertRaisesRegex(SourceError, message):
                Post("1", files)
        for empty in (None, "", "  "):
            with self.subTest(id=empty), self.assertRaisesRegex(SourceError, "a post needs an id"):
                Post(empty)
        self.assertEqual(Post(0).id, "0")
        for field in ("post", "category", "num", "plugin"):
            with self.subTest(field), self.assertRaisesRegex(SourceError, f"{field} is the"):
                Post("1", **{field: "x"})

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
            with self.subTest(options), self.assertRaisesRegex(SourceError, message):
                File(**options)

    def test_text_and_stream_files(self):
        note = File.text("info.md", "# Title\n")
        self.assertEqual((note.name, note.content, note.url, note.mirrors), ("info.md",
                                                                              "# Title\n", None,
                                                                              ()))
        live = File.stream("https://cdn.example.com/v.m3u8", headers={"referer": "x"})
        self.assertEqual((live.playlist, dict(live.headers), live.url),
                         ("https://cdn.example.com/v.m3u8", {"referer": "x"}, None))
        with self.assertRaisesRegex(SourceError, r"a playlist's URL"):
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
            with self.subTest(message), self.assertRaisesRegex(SourceError, message):
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
        with self.assertRaisesRegex(SourceError, r"Mutable: mutable default"):
            type("Mutable", (Settings,), {"__annotations__": {"tags": list}, "tags": []})

    def test_ctx_has_exactly_the_contracts_members(self):
        members = {name for name in vars(Ctx) if not name.startswith("_")}
        self.assertEqual(members, {
            "fetch", "pages", "open", "feed", "load", "document", "click", "handoff",
            "extension", "storage", "pause", "hold", "call", "ask", "log", "target", "when",
            "time", "settings", "account", "cookies", "state", "secrets"})
        self.assertEqual(str(inspect.signature(Ctx.pages)),
                         "(self, request: 'str | Request', *, cursor: 'str | Callable[[Response], "
                         "str | None]', send: 'str | Callable[[str], Mapping[str, Any]]' = "
                         "'cursor', bucket: 'str | None' = None, in_page: 'bool' = False) -> "
                         "'AsyncIterator[Response]'")


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

    def test_numbered(self):
        rows = [((7,), {}, "07"), ((7,), {"suffix": "_cover"}, "07_cover"),
                ((2,), {"suffix": ".webp", "width": 3}, "002.webp"), ((123,), {}, "123")]
        for args, options, expected in rows:
            with self.subTest(expected):
                self.assertEqual(numbered(*args, **options), expected)

    def test_dig_is_the_kits(self):
        self.assertIs(dig, kit_dig.dig)
        self.assertEqual(dig({"a": [{"b": 1}]}, "a[0].b"), 1)
        with self.assertRaises(ValueError):
            dig({}, "a[")


if __name__ == "__main__":
    unittest.main()
