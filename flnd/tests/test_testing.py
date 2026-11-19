"""The public test kit, `flnd.testing`: what a plugin's own tests are written with.

Its docstrings' examples run in tests/test_docs.py; these cover what they don't: `check` over
every built-in plugin, and the fake clock. One test per behaviour the kit promises: `FakeCtx`
has exactly `Ctx`'s members, signs, blocks, holds, fails to open, walls, keeps a cursor through
a 429, and has a 401, an extension and storage; `collect` and `check` on a plugin.
"""

import asyncio
import datetime
import inspect
import unittest
from unittest import mock

import no_profiles  # noqa: F401

from flnd import plugin as v2
from flnd.errors import Blocked, LoginError, NotFound, PluginError, SiteError, UsageError
from flnd.plugins import PLUGINS
from flnd.testing import FakeCtx, FakePage, Reply, Sent, check, collect, fake_clock
from flnkit import clock as _time


class CheckTest(unittest.TestCase):
    def test_every_built_in_plugin_checks_and_check_takes_only_a_plugin_class(self):
        for code, plugin in PLUGINS.items():
            with self.subTest(plugin=code):
                check(plugin)
        with self.assertRaises(PluginError):
            check(object)  # type: ignore[arg-type]


class ClockTest(unittest.TestCase):
    def test_the_fake_clock_moves_only_when_slept_on(self):
        with fake_clock(100.0) as clock:
            self.assertEqual(_time.now(), 100.0)
            _time.sleep(2.5)
            self.assertEqual((clock.now, clock.slept), (102.5, [2.5]))
        self.assertNotEqual(_time.now(), 102.5)

# ---------------------------------------------------------------------------------------------
# Plugin API 2: FakeCtx, and collect and check on a plugin
# ---------------------------------------------------------------------------------------------

BASE = "https://api.example.com"
PAGE = "https://www.example.com/example_user"
WALL = "https://www.example.com/accounts/login/next"


class Gallery(v2.Plugin):
    code, name, api = "gal", "Gallery", v2.API
    domains = ("api.example.com", "www.example.com")
    walls = ("/accounts/login",)
    fields = ("album",)
    folder = "{category}/{album}"
    login = v2.cookies("sessionid", url="https://www.example.com/accounts/login/")

    class Settings(v2.Settings):
        """Its config keys.

        Attributes:
            height: the tallest video wanted.
        """

        height: int = 720

    def sign(self, request, cookies):
        return request.replace(headers={"x-signed": "1"})

    def blocked(self, response):
        return float(response.headers["x-slow"]) if "x-slow" in response.headers else None

    @v2.route("gal:{user}", "example.com/{user}")
    async def user(self, ctx, user):
        """A user's gallery: `gal:example_user`, or `https://www.example.com/example_user`."""
        return (await ctx.fetch(f"{BASE}/users/{user}")).json

    @user.category
    async def posts(self, ctx, profile):
        async for page in ctx.pages(f"{BASE}/users/{profile['id']}/posts", cursor="next"):
            for item in page.json["items"]:
                yield v2.Post(item["id"], [v2.File(f"https://cdn.example.com/{item['id']}.jpg")],
                              date=item.get("time"), pinned=item.get("pinned", False),
                              **{key: item[key] for key in ("album",) if key in item})

    @user.category(own=False)
    async def liked(self, ctx, profile):
        async with ctx.feed("/api/liked") as pages:
            await ctx.open(f"{PAGE}/liked")
            async for page in pages:
                for item in page["items"]:
                    yield v2.Post(item, self.opened(ctx, item), album=None)

    def opened(self, ctx, item):
        """A lazy post's files, read from the post's own page."""
        async def files():
            await ctx.open(f"https://www.example.com/p/{item}")
            found = (await ctx.document()).first("img")
            return [v2.File(found.attr("src"))]
        return files


class Signing(v2.Login):
    methods = ("code",)

    async def start(self, ctx, method):
        ctx.secrets["code"] = await ctx.ask("Code")
        # A task the login starts writes as the login itself.
        await asyncio.gather(self.keep(ctx))

    async def keep(self, ctx):
        ctx.secrets["kept"] = "1"


def run(awaitable):
    return asyncio.run(awaitable)


def fake(**given):
    return FakeCtx(Gallery, "gal:example_user", **given)


def answers(*replies):
    """A route answering `replies` in turn, then the last one again."""
    queue = list(replies)
    return lambda request: queue.pop(0) if len(queue) > 1 else queue[0]


def bodies(ctx, items):
    async def taken():
        return [item async for item in items]
    return run(taken())

ITEMS = [{"id": "p2", "time": "2026-01-03", "album": "trip"},
         {"id": "p1", "time": "2026-01-02", "album": None}]
PROFILE = {f"{BASE}/users/example_user": Reply({"id": "42"})}


class FakeCtxTest(unittest.TestCase):
    def test_it_has_exactly_the_members_of_ctx(self):
        members = {name for name in dir(v2.Ctx) if not name.startswith("_")}
        self.assertEqual({name for name in dir(FakeCtx) if not name.startswith("_")}, members)
        for name in sorted(members):
            wanted, found = getattr(v2.Ctx, name), getattr(FakeCtx, name)
            if callable(wanted):
                with self.subTest(name):
                    self.assertEqual(str(inspect.signature(found)),
                                     str(inspect.signature(wanted)))
                    self.assertEqual((inspect.iscoroutinefunction(found),
                                      inspect.isasyncgenfunction(found)),
                                     (inspect.iscoroutinefunction(wanted),
                                      inspect.isasyncgenfunction(wanted)))

    def test_a_post_is_never_tried_again_and_a_page_is_capped(self):
        from flnd.engine import ctx as engine_ctx

        tries = []

        def busy(request):
            tries.append(request)
            return Reply(status=503)

        fake = FakeCtx(Gallery, routes={f"{BASE}/login": busy, f"{BASE}/big": Reply(b"x" * 11)})
        response = run(fake.fetch(v2.Request(f"{BASE}/login", method="POST", check=False)))
        self.assertEqual((response.status, len(tries)), (503, 1))
        with self.assertRaises(v2.SiteError):
            run(fake.fetch(v2.Request(f"{BASE}/login", method="POST")))
        self.assertEqual(len(tries), 2)
        with mock.patch.object(engine_ctx, "PAGE_LIMIT", 10), \
                self.assertRaisesRegex(v2.SiteError, "too big to read as a page"):
            run(fake.fetch(f"{BASE}/big"))

    def test_the_user_agent_is_set_before_sign_sees_the_request(self):
        seen = {}

        class Agent(Gallery):
            code = "gag"
            user_agent = "example-agent/1"

            def sign(self, request, cookies):
                seen.update(request.headers or {})
                return request

        fake = FakeCtx(Agent, routes={f"{BASE}/me": Reply({"ok": 1})})
        run(fake.fetch(v2.Request(f"{BASE}/me", headers={"User-Agent": "mine"})))
        self.assertEqual(seen.get("User-Agent"), "example-agent/1")
        self.assertEqual(fake.seen.requests[0][2]["headers"]["User-Agent"], "example-agent/1")

    def test_a_checked_answer_becomes_the_engines_error(self):
        cdn = "https://cdn.example.com/a.jpg"
        rows = [
            (f"{BASE}/a", Reply(status=401), LoginError),
            (f"{BASE}/a", Reply(status=403), LoginError),
            (cdn, Reply(status=403), SiteError),
            (f"{BASE}/a", Reply(status=404), NotFound),
            (cdn, Reply(status=410), NotFound),
            (f"{BASE}/a", Reply(status=400), SiteError),
            (f"{BASE}/a", Reply(url=WALL), LoginError),
        ]
        for url, reply, error in rows:
            with self.subTest(url=url, reply=reply), self.assertRaises(error) as caught:
                run(fake(routes={url: reply}).fetch(url))
            self.assertIs(isinstance(caught.exception, LoginError), error is LoginError)

    def test_with_check_off_a_refusal_is_returned(self):
        cdn = "https://cdn.example.com/a.jpg"
        for url, status in [(f"{BASE}/a", 401), (f"{BASE}/a", 403), (f"{BASE}/a", 404),
                            (cdn, 410), (f"{BASE}/a", 500), (f"{BASE}/a", 200)]:
            with self.subTest(url=url, status=status):
                ctx = fake(routes={url: Reply({"m": 1}, status=status)})
                self.assertEqual(run(ctx.fetch(v2.Request(url, check=False))).status, status)
        # A 429 is still held and asked again: holds act before `check`.
        ctx = fake(routes={f"{BASE}/a": answers(Reply(status=429), Reply(status=403))})
        self.assertEqual(run(ctx.fetch(v2.Request(f"{BASE}/a", check=False))).status, 403)
        self.assertEqual([seconds for _, seconds, _ in ctx.seen.holds], [30.0])

    def test_a_server_error_is_retried_with_check_on_or_off(self):
        down, up = Reply(status=503), Reply({"ok": 1})
        rows = [
            (False, [down, up], 200, 2),
            (False, [down], 503, 5),
            (True, [down, up], 200, 2),
            (True, [down], SiteError, 5),
        ]
        for checked, replies, wanted, tries in rows:
            with self.subTest(check=checked, replies=len(replies)):
                ctx = fake(routes={f"{BASE}/a": answers(*replies)})
                request = v2.Request(f"{BASE}/a", check=checked)
                if wanted is SiteError:
                    with self.assertRaises(SiteError):
                        run(ctx.fetch(request))
                else:
                    self.assertEqual(run(ctx.fetch(request)).status, wanted)
                self.assertEqual(len(ctx.seen.requests), tries)

    def test_a_url_with_no_route_fails_the_test_and_only_own_domains_are_signed(self):
        with self.assertRaises(AssertionError):
            run(fake().fetch(f"{BASE}/nothing"))
        ctx = fake(routes={f"{BASE}/a": Reply(), "https://cdn.example.com/a": Reply()})
        run(ctx.fetch(f"{BASE}/a"))
        run(ctx.fetch("https://cdn.example.com/a"))
        self.assertEqual([options["headers"] for _, _, options in ctx.seen.requests],
                         [{"x-signed": "1"}, {}])

    def test_a_block_or_a_429_holds_on_the_clock_then_tries_again(self):
        wall = 1767225600 + 59  # the fake clock's wall, plus 59 s
        rows = [
            (Reply(headers={"x-slow": "60"}), 60.0),
            (Reply(headers={"x-slow": "5"}), 30.0),
            (Reply(status=429, headers={"retry-after": "7"}), 7.0),
            (Reply(status=429, headers={"x-rate-limit-reset": str(wall)}), 60.0),
            (Reply(status=429), 30.0),
        ]
        for first, seconds in rows:
            with self.subTest(first=first):
                ctx = fake(routes={f"{BASE}/a": answers(first, Reply({"ok": 1}))})
                self.assertEqual(run(ctx.fetch(f"{BASE}/a")).json, {"ok": 1})
                self.assertEqual([(held, bucket) for _, held, bucket in ctx.seen.holds],
                                 [(seconds, "api.example.com")])
                self.assertEqual((len(ctx.seen.requests), ctx.time.now()), (2, seconds))

    def test_the_third_429_in_a_row_is_a_block_round_and_a_clean_answer_resets_the_count(self):
        many = Reply(status=429, headers={"retry-after": "5"})
        ctx = fake(routes={f"{BASE}/a": answers(many, many, many, Reply())})
        run(ctx.fetch(f"{BASE}/a"))
        self.assertEqual([seconds for _, seconds, _ in ctx.seen.holds], [5.0, 5.0, 30.0])
        clean = fake(routes={f"{BASE}/a": answers(many, many, Reply(), many, many, Reply())})
        run(clean.fetch(f"{BASE}/a"))
        run(clean.fetch(f"{BASE}/a"))
        self.assertEqual([seconds for _, seconds, _ in clean.seen.holds], [5.0] * 4)

    def test_the_fourth_block_round_raises_blocked(self):
        ctx = fake(routes={f"{BASE}/a": Reply(headers={"x-slow": "1"})})
        with self.assertRaises(Blocked):
            run(ctx.fetch(f"{BASE}/a"))
        self.assertEqual([seconds for _, seconds, _ in ctx.seen.holds], [30.0, 120.0, 480.0])
        held = fake()
        for _ in range(3):
            run(held.hold("the feed refused", bucket="feed"))
        with self.assertRaises(Blocked):
            run(held.hold("the feed refused", bucket="feed"))
        run(held.hold("another bucket", bucket="other"))
        run(held.pause(600, "600 s from the last round's start"))
        run(held.hold("the feed refused", seconds=10, bucket="feed"))
        self.assertEqual([hold[1:] for hold in held.seen.holds[-2:]],
                         [(30.0, "other"), (30.0, "feed")])

    def test_open_lands_with_the_pages_status_or_raises(self):
        rows = [
            ("<h1>Hi</h1>", v2.Landed(PAGE, 200)),
            (FakePage(status=500), v2.Landed(PAGE, 500)),
            (FakePage(url=f"{PAGE}/"), v2.Landed(f"{PAGE}/", 200)),
            (FakePage(url="https://www.example.com/accounts/login_example"),
             v2.Landed("https://www.example.com/accounts/login_example", 200)),
            (FakePage(status=404), NotFound),
            (FakePage(status=410), NotFound),
            (FakePage(status=403), LoginError),
            (None, AssertionError),
        ]
        for page, landed in rows:
            with self.subTest(page=page):
                ctx = fake(pages={PAGE: page} if page is not None else {})
                if isinstance(landed, v2.Landed):
                    self.assertEqual(run(ctx.open(PAGE)), landed)
                    continue
                with self.assertRaises(landed):
                    run(ctx.open(PAGE))

    def test_a_wall_is_landed_on_held_twice_then_a_login_error(self):
        ctx = fake(pages={PAGE: FakePage(url=WALL)})
        with self.assertRaises(LoginError):
            run(ctx.open(PAGE))
        self.assertEqual([(seconds, bucket) for _, seconds, bucket in ctx.seen.holds],
                         [(30.0, None), (120.0, None)])
        self.assertEqual((ctx.seen.visited, ctx.time.now()), ([PAGE] * 3, 150.0))
        direct = fake(routes={WALL: Reply({"ok": 1})})
        login = v2.Request(WALL, method="POST")
        self.assertEqual((run(direct.fetch(login)).json, direct.seen.holds), ({"ok": 1}, []))

    def test_pages_keeps_its_cursor_through_a_429_and_ends_on_a_repeated_one(self):
        later = answers(Reply(status=429), Reply({"items": [2], "next": "c2"}))
        ctx = fake(routes={f"{BASE}/posts": lambda request: (
            later(request) if request.params else Reply({"items": [1], "next": "c2"}))})
        async def capped():
            # At most 5 pages, so a listing that never stops fails here instead of hanging.
            found = []
            async for page in ctx.pages(f"{BASE}/posts", cursor="next"):
                found.append(page.json["items"])
                if len(found) == 5:
                    break
            return found

        self.assertEqual(run(capped()), [[1], [2]])
        self.assertEqual([options["params"] for _, _, options in ctx.seen.requests],
                         [None, {"cursor": "c2"}, {"cursor": "c2"}])
        self.assertEqual(len(ctx.seen.holds), 1)

    def test_ask_pops_its_answers_one_of_the_choices_then_refuses(self):
        ctx = fake(answers=["123456", "b", "z"])
        self.assertEqual(run(ctx.ask("Code")), "123456")
        self.assertEqual(run(ctx.ask("Pick", choices=["a", "b"])), "b")
        for label, choices in [("not one of the choices", ["a", "b"]), ("none left", ())]:
            with self.subTest(label), self.assertRaises(UsageError):
                run(ctx.ask("Pick", choices=choices))

    def test_state_settings_and_secrets_keep_the_contract(self):
        state = {}
        ctx = FakeCtx(Gallery, state=state, settings={"height": 1080}, answers=["123456"])
        ctx.state["seen"] = {"ids": [1, 2], "at": None}
        self.assertEqual((state, ctx.settings.height), ({"seen": {"ids": [1, 2], "at": None}},
                                                        1080))
        for value in [{1, 2}, (1, 2), {1: "a"}, b"x"]:
            with self.subTest(value=value), self.assertRaises(PluginError):
                ctx.state["bad"] = value
        with self.assertRaises(UsageError):
            FakeCtx(Gallery, settings={"width": 1})
        with self.assertRaises(PluginError):
            ctx.secrets["code"] = "1"
        signed = type("Signed", (Gallery,), {"code": "sgn", "login": Signing()})
        ctx = FakeCtx(signed, answers=["123456"])
        run(signed.login.start(ctx, "code"))
        self.assertEqual(dict(ctx.secrets), {"code": "123456", "kept": "1"})

    def test_extension_and_storage_read_their_fakes(self):
        ctx = fake(extension={"title": "Hi", "evaluate": lambda js: len(js)},
                   storage={"session": "s"})
        self.assertEqual((run(ctx.extension("title")), run(ctx.extension("evaluate", js="1+1"))),
                         ("Hi", 3))
        self.assertEqual(dict(run(ctx.storage("https://www.example.com"))), {"session": "s"})
        self.assertEqual(dict(run(fake().storage("https://www.example.com"))), {})
        with self.assertRaises(UsageError) as caught:
            run(fake().extension("title"))
        self.assertEqual(caught.exception.hint, "flnd extension install")

    def test_shown_is_true_for_the_selectors_the_page_declares_shown(self):
        page = FakePage('<b class="a"></b><b class="b"></b>', shown=[".a"])
        ctx = fake(pages={PAGE: page})
        self.assertFalse(run(ctx.shown(".a")))
        run(ctx.open(PAGE))
        self.assertEqual([run(ctx.shown(css)) for css in (".a", ".b", ".none")],
                         [True, False, False])

    def test_a_feed_hears_what_the_page_sends_after_it_listens(self):
        page = FakePage('<script type="application/json">{"n": 1}</script><b class="more">',
                        feeds=[("https://www.example.com/api/liked?page=1", 1),
                               Sent("https://www.example.com/api/liked?page=2", 2, click=".more"),
                               Sent("https://www.example.com/api/liked?page=3", 3, scroll=1),
                               Sent("https://www.example.com/api/liked?page=4", 4, scroll=2)])
        ctx = fake(pages={PAGE: page})
        self.assertEqual(bodies(ctx, ctx.load(PAGE, "/api/liked")), [[{"n": 1}], 1, 3, 4])
        self.assertEqual(ctx.seen.scrolls, 3)

        async def clicked():
            async with ctx.feed("/api/liked", scroll=False) as items:
                await ctx.click(".more")
                return [item async for item in items]

        self.assertEqual(run(clicked()), [2])
        run(ctx.open(PAGE))
        self.assertEqual(bodies(ctx, ctx.feed("/api/liked", scroll=False)), [])
        for wrong in [{"click": ".more", "scroll": 1}, {"scroll": 0}]:
            with self.subTest(wrong), self.assertRaises(PluginError):
                Sent("https://www.example.com/api/liked", 0, **wrong)

    def test_it_runs_on_the_active_fake_clock_and_logs(self):
        with fake_clock() as clock:
            ctx = fake()
            run(ctx.pause(86_400, "a day"))
            self.assertEqual((clock.slept, ctx.time.day()), ([86_400], datetime.date(2026, 1, 2)))
        self.assertEqual(ctx.time.now(), 0.0)
        ctx.log("info", "{n} posts", n=2)
        self.assertEqual(ctx.seen.logs, [("info", "2 posts")])
        with self.assertRaises(PluginError):
            ctx.log("error", "no such level")


class PluginCollectTest(unittest.TestCase):
    def ctx(self, items=ITEMS, **given):
        page = FakePage(feeds=[("https://www.example.com/api/liked", {"items": ["l1"]})])
        post = FakePage('<img src="https://cdn.example.com/l1.jpg">',
                        feeds=[("https://www.example.com/api/liked", {"items": ["l2"]})])
        return fake(routes={**PROFILE, f"{BASE}/users/42/posts": Reply({"items": items})},
                    pages={f"{PAGE}/liked": page, "https://www.example.com/p/l1": post},
                    **given)

    def test_it_runs_the_wanted_categories_to_their_end(self):
        rows = [
            ({}, ["posts/trip/p2_0.jpg", "posts/p1_0.jpg"]),
            ({"categories": ["all"]}, ["posts/trip/p2_0.jpg", "posts/p1_0.jpg", "liked/l1_0.jpg"]),
            ({"categories": ["liked"]}, ["liked/l1_0.jpg"]),
            ({"limit": 1}, ["posts/trip/p2_0.jpg"]),
        ]
        for options, paths in rows:
            with self.subTest(**options):
                saved = collect(Gallery, "gal:example_user", ctx=self.ctx(), **options)
                self.assertEqual([found.path for found in saved], paths)
        with self.assertRaises(UsageError):
            collect(Gallery, "gal:example_user", ctx=self.ctx(), categories=["reels"])

    def test_a_lazy_posts_page_opens_in_a_tab_of_its_own(self):
        ctx = self.ctx()
        saved = collect(Gallery, "gal:example_user", ctx=ctx, categories=["liked"])
        self.assertEqual([found.url for found in saved], ["https://cdn.example.com/l1.jpg"])
        self.assertEqual(ctx.seen.visited, [f"{PAGE}/liked", "https://www.example.com/p/l1"])

    def test_a_lazy_posts_gone_files_fail_only_that_post(self):
        listed = []

        async def gone():
            raise NotFound("HTTP 404 for https://www.example.com/p/p2")

        async def posts(plugin, ctx, profile):
            for post in (v2.Post("p2", gone, album=None),
                         v2.Post("p1", [v2.File("https://cdn.example.com/p1.jpg")], album=None)):
                listed.append(post.id)
                yield post

        ctx = self.ctx()
        original, Gallery.posts = Gallery.posts, posts
        try:
            saved = collect(Gallery, "gal:example_user", ctx=ctx)
        finally:
            Gallery.posts = original
        self.assertEqual((listed, [found.post.id for found in saved]), (["p2", "p1"], ["p1"]))
        self.assertEqual([(post, type(error)) for post, error in ctx.seen.failed],
                         [("p2", NotFound)])

    def test_any_other_error_from_a_lazy_posts_files_ends_the_run(self):
        async def refused():
            raise LoginError("gal: HTTP 401 for https://www.example.com/p/p2")

        async def posts(plugin, ctx, profile):
            yield v2.Post("p2", refused, album=None)
            yield v2.Post("p1", [v2.File("https://cdn.example.com/p1.jpg")], album=None)

        ctx = self.ctx()
        original, Gallery.posts = Gallery.posts, posts
        try:
            with self.assertRaises(LoginError):
                collect(Gallery, "gal:example_user", ctx=ctx)
        finally:
            Gallery.posts = original
        self.assertEqual(ctx.seen.failed, [])

    def test_a_pinned_post_is_saved_out_of_order_and_never_ends_the_listing(self):
        def post(day, **more):
            return {"id": f"p{day}", "time": f"2026-01-{day:02d}", "album": None, **more}

        rows = [
            ("pinned above newer posts", None, [post(1, pinned=True), post(3), post(2)],
             ["p1", "p3", "p2"]),
            ("pinned posts older than when", v2.Range(datetime.date(2026, 1, 9)),
             [*(post(day, pinned=True) for day in range(1, 7)), post(10), post(9)],
             ["p10", "p9"]),
            # Older posts 8, 7, 6, a pinned one in range, then 5 and 4: the fifth older post
            # in a row (4) ends the listing, since the pinned post doesn't reset the count. The
            # last post breaks the date order, so a listing that reaches it fails the test.
            ("a pinned post in range keeps the older count", v2.Range(datetime.date(2026, 1, 9)),
             [post(8), post(7), post(6), post(15, pinned=True), post(5), post(4), post(3),
              {"id": "p2", "time": "2026-01-30", "album": None}],
             ["p15"]),
        ]
        for label, when, items, kept in rows:
            with self.subTest(label):
                ctx = self.ctx(items, **({"when": when} if when else {}))
                saved = collect(Gallery, "gal:example_user", ctx=ctx)
                self.assertEqual([found.post.id for found in saved], kept)

    def test_when_keeps_its_days_and_ends_after_older_posts_in_a_row(self):
        items = [{"id": f"p{day}", "time": f"2026-01-{day:02d}", "album": None}
                 for day in range(20, 0, -1)]
        rows = [
            (v2.Range(datetime.date(2026, 1, 18), datetime.date(2026, 1, 19)), ["p19", "p18"],
             8),
            (v2.Range(until=datetime.date(2026, 1, 2)), ["p2", "p1"], 20),
        ]
        for when, kept, listed in rows:
            with self.subTest(when=when):
                ctx = self.ctx(items, when=when)
                seen: list[str] = []
                original = Gallery.posts

                async def counted(plugin, ctx, profile, original=original, seen=seen):
                    async for post in original(plugin, ctx, profile):
                        seen.append(post.id)
                        yield post

                Gallery.posts = counted
                try:
                    saved = collect(Gallery, "gal:example_user", ctx=ctx)
                finally:
                    Gallery.posts = original
                self.assertEqual([found.post.id for found in saved], kept)
                self.assertEqual(len(seen), listed)

    def test_a_files_name_keeps_its_extension_else_takes_its_types_or_its_urls(self):
        jpg, bare = "https://cdn.example.com/p1.jpg", "https://cdn.example.com/p1"

        def typed(kind):
            return Reply(b"x", headers={"content-type": kind})

        calls = []

        def counted(request):
            calls.append(request.url)
            return typed("image/png")

        rows = [
            ("a stem", jpg, {"name": "cover_12"}, None, False, "posts/cover_12.jpg"),
            ("a name with an extension", jpg, {"name": "cover_12.png"}, None, False,
             "posts/cover_12.png"),
            ("a 7-character extension", jpg, {"name": "file.torrent"}, None, False,
             "posts/file.torrent"),
            ("a content type", jpg, {}, typed("image/png"), False, "posts/p1_0.png"),
            ("an unknown content type", jpg, {"name": "cover_12"}, typed("application/x-example"),
             False, "posts/cover_12.jpg"),
            ("octet-stream", jpg, {}, typed("application/octet-stream"), False, "posts/p1_0.jpg"),
            ("text/plain", jpg, {}, typed("text/plain"), False, "posts/p1_0.jpg"),
            ("neither", bare, {"name": "cover_12"}, None, False, "posts/cover_12"),
            ("a function route, never fetched", jpg, {}, counted, False, "posts/p1_0.jpg"),
            ("a function route, fetched once", jpg, {}, counted, True, "posts/p1_0.png"),
        ]
        for label, url, options, content, fetched, path in rows:
            with self.subTest(label):
                calls.clear()

                async def posts(plugin, ctx, profile, options=options, url=url, fetched=fetched):
                    if fetched:
                        await ctx.fetch(url)
                    yield v2.Post("p1", [v2.File(url, **options)], album=None)

                routes = {**PROFILE, **({url: content} if content else {})}
                original, Gallery.posts = Gallery.posts, posts
                try:
                    saved = collect(Gallery, "gal:example_user", ctx=fake(routes=routes))
                finally:
                    Gallery.posts = original
                self.assertEqual([found.path for found in saved], [path])
                # Naming never calls a function route: only the plugin's own fetch did.
                self.assertEqual(len(calls), int(fetched and content is counted))

    def test_it_checks_the_listings_order_and_the_templates_fields(self):
        rows = [
            ([{"id": "p1", "time": "2026-01-02", "album": None},
              {"id": "p2", "time": "2026-01-03", "album": None}], "newer than one before it"),
            ([{"id": "p1", "time": "2026-01-02"}], "gives no album"),
        ]
        for items, message in rows:
            with self.subTest(message), self.assertRaises(PluginError) as caught:
                collect(Gallery, "gal:example_user", ctx=self.ctx(items))
            self.assertIn(message, str(caught.exception))

    def test_check_resolves_each_routes_examples_to_it(self):
        self.assertEqual([str(found) for found in check(Gallery)], ["gal:example_user"] * 2)

        class Wrong(Gallery):
            code = "wrg"

            @v2.route(r"example.com/p/{post:\w+}")
            async def post(self, ctx, post):
                """One post: `https://www.example.com/p/p1`."""
                yield v2.Post(post)

        with self.assertRaises(PluginError) as caught:
            check(Wrong)
        self.assertIn("is read by the route user", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
