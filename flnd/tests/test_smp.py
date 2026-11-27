"""SimpCity (`smp`): its thread route, paging, links, hosts, pace, budget and captcha."""

import asyncio
import base64
import unittest
from urllib.parse import parse_qs, urlsplit

import no_profiles  # noqa: F401
from plugin_kit import FakeCtx, FakePage, Reply, check, collect

from flnd.errors import Blocked, Changed, LoginError
from flnd.plugin import Post, Target, UsageError
from flnd.plugins.smp import Simp, safe

FORUM = "https://simpcity.cr"
BASE = f"{FORUM}/threads/example-title.123"
STAMP = 1767225600  # 2026-01-01 00:00 UTC
DAY = 86400


def wrap(url):
    return "/out/?to=" + base64.b64encode(url.encode()).decode()


def post(number, body, *, stamp=STAMP, extra="", author="example_user"):
    return (f'<article class="message message--post" data-author="{author}" '
            f'data-content="post-{number}"><header class="message-attribution">'
            f'<time class="u-dt" data-timestamp="{stamp}"></time></header>'
            f'<article class="message-body"><div class="bbWrapper">{body}</div></article>'
            f'{extra}</article>')


def page(*posts, nxt=False, logged="true", title=""):
    more = '<a class="pageNav-jump pageNav-jump--next" href="/x">Next</a>' if nxt else ""
    heading = f'<h1 class="p-title-value"> {title} </h1>' if title else ""
    return (f'<html data-logged-in="{logged}"><body>{heading}<div class="pageNav">{more}</div>'
            f'{"".join(posts)}</body></html>')


def goonbox(id, thumb=True):
    return (f'<a href="https://goonbox.cr/img/name.{id}" class="link"><img class="bbImage" '
            f'src="https://simp2.cuckcapital.cr/p/{id}.md.jpg"></a>')


def turbo(id):
    return f'<iframe class="saint-iframe" src="https://turbo.cr/embed/{id}"></iframe>'


def forum_page(n):
    return f"{BASE}/page-{n}" if n > 1 else f"{BASE}/"


def image_api(id, **more):
    return {f"https://goonbox.cr/api/images/{id}": Reply(
        {"image": {"original_url": f"https://cdn.example.com/{id}.jpg",
                   "original_filename": f"orig{id}.jpg", **more}})}


def routes(**extra):
    return {**image_api("a1"), **image_api("a2"), **image_api("a3"), **extra}


def thread(pages, state=None, settings=None, **more):
    """A fake of a thread with pages by number: the last one answers `/latest`."""
    last = max(pages)
    table = {forum_page(n): Reply(html, url=f"{BASE}/page-{n}#post-1")
             for n, html in pages.items()}
    table[f"{FORUM}/threads/123/latest"] = table[forum_page(last)]
    table[f"{BASE}/latest"] = table[forum_page(last)]
    return FakeCtx(Simp, "smp:123", routes={**table, **routes(), **more}, state=state,
                   settings=settings)


def asked(ctx):
    return [url for _, url, _ in ctx.seen.requests]


def forum_asked(ctx):
    return [url.removeprefix(FORUM) for url in asked(ctx) if url.startswith(FORUM)]


def run(ctx, category):
    async def go():
        plugin = ctx._plugin
        profile = await plugin.thread(ctx, "123")
        return [item async for item in getattr(plugin, category)(ctx, profile)]
    return asyncio.run(go())


def ids(found):
    return [item.id for item in found]


THREE = {
    3: page(post(31, goonbox("a3"), stamp=STAMP + 2 * DAY)),
    2: page(post(21, turbo("t2") + goonbox("a2"), stamp=STAMP + DAY),
            post(22, "text", stamp=STAMP + DAY)),
    1: page(post(11, goonbox("a1"), stamp=STAMP)),
}


class FormsTest(unittest.TestCase):
    def test_forms(self):
        for text in ("smp:123", "SMP:123", "https://simpcity.cr/threads/example-title.123/",
                     "simpcity.cr/threads/example-title.123/page-4#post-9",
                     "https://simpcity.su/threads/a.b.123/latest", "simpcity.cr/threads/123/"):
            with self.subTest(text=text):
                self.assertEqual(Simp.resolve(text), Target("smp:123", kind="thread"))
        for text in ("smp:abc", "smp:post/5", "https://example.com/threads/a.1/"):
            with self.subTest(text=text):
                self.assertIsNone(Simp.resolve(text))

    def test_refusals(self):
        for text in ("simpcity.cr/forums/x.5/", "simpcity.cr/members/example_user.5/",
                     "simpcity.cr/search/1/", "simpcity.cr/tags/example/", "simpcity.cr/whats-new/",
                     "simpcity.su/watched/threads", "simpcity.cr/conversations/", "simpcity.cr/",
                     "simpcity.cr/account/", "simpcity.cr/posts/5/"):
            with self.subTest(text=text), self.assertRaisesRegex(
                    UsageError, r"give a thread or a post: smp:<id>, smp:post/<id>"):
                Simp.resolve(text)

    def test_declarations(self):
        self.assertEqual(len(check(Simp)), 2)
        self.assertEqual(Simp.categories, ("images", "videos"))
        self.assertTrue(Simp.login.proves("xf_user"))
        self.assertFalse(Simp.login.proves("xf_session"))
        self.assertTrue(Simp.login.required)


class PagingTest(unittest.TestCase):
    def test_first_read_goes_through_latest_and_back_newest_first(self):
        ctx = thread(THREE)
        found = run(ctx, "images")
        self.assertEqual(ids(found), ["31", "21", "11"])
        self.assertEqual(forum_asked(ctx), ["/threads/123/latest",
                                            "/threads/example-title.123/page-2",
                                            "/threads/example-title.123/"])
        self.assertEqual(ctx.state["thread:123"], {"page": 3, "base": "example-title.123"})

    def test_a_sync_starts_at_the_saved_last_page(self):
        ctx = thread(THREE, state={"thread:123": {"page": 3, "base": "example-title.123"}})
        run(ctx, "images")
        self.assertEqual(forum_asked(ctx)[0], "/threads/example-title.123/page-3")
        self.assertNotIn("/threads/123/latest", forum_asked(ctx))

    def test_a_sync_with_a_newer_page_goes_to_the_last_and_back(self):
        pages = {**THREE, 2: page(post(21, goonbox("a2"), stamp=STAMP + DAY), nxt=True)}
        ctx = thread(pages, state={"thread:123": {"page": 2, "base": "example-title.123"}})
        self.assertEqual(ids(run(ctx, "images")), ["31", "21", "11"])
        # Page 2 is read once: from the cache on the way back.
        self.assertEqual(forum_asked(ctx).count("/threads/example-title.123/page-2"), 1)

    def test_the_categories_share_the_pages(self):
        ctx = thread(THREE)
        self.assertEqual(ids(run(ctx, "videos")), ["21"])
        self.assertEqual(ids(run(ctx, "images")), ["31", "21", "11"])
        self.assertEqual(len(forum_asked(ctx)), 3)

    def test_a_limit_stops_reading_pages(self):
        ctx = thread(THREE)
        found = collect(Simp, "smp:123", ctx=ctx, limit=1)
        self.assertEqual([f.post.id for f in found], ["31"])
        # The listing looks one post ahead, which takes the next page.
        self.assertEqual(forum_asked(ctx), ["/threads/123/latest",
                                            "/threads/example-title.123/page-2"])

    def test_referer_is_the_previous_page_of_the_thread(self):
        ctx = thread(THREE)
        run(ctx, "images")
        refs = [headers["headers"].get("Referer") for _, url, headers in ctx.seen.requests
                if url.startswith(FORUM)]
        self.assertEqual(refs, [None, f"{BASE}/page-3", f"{BASE}/page-2"])


class PostTest(unittest.TestCase):
    def test_noise_is_left_out_and_each_link_is_once(self):
        quote = ('<blockquote class="bbCodeBlock--quote">' + goonbox("a2") + "</blockquote>")
        unfurl = '<div class="bbCodeBlock--unfurl">' + turbo("tq") + "</div>"
        signature = ('<aside class="message-signature"><div class="bbWrapper">'
                     + goonbox("a3") + "</div></aside>")
        body = goonbox("a1") + goonbox("a1") + quote + unfurl
        ctx = thread({1: page(post(5, body, extra=signature))})
        (found,) = run(ctx, "images")
        files = collect(Simp, "smp:123", ctx=ctx)
        self.assertEqual([f.key for f in files], ["5_0"])
        self.assertEqual(ids(run(ctx, "videos")), [])
        self.assertEqual(found.date, STAMP)

    def test_redirects_decode_plain_and_base64(self):
        plain = '<a href="/out/?url=https%3A%2F%2Fgoonbox.cr%2Fimg%2Fx.a2">x</a>'
        wrapped = f'<a href="{wrap("https://goonbox.cr/img/y.a1")}">y</a>'
        ctx = thread({1: page(post(5, plain + wrapped))})
        files = collect(Simp, "smp:123", ctx=ctx)
        self.assertEqual([f.url for f in files],
                         ["https://cdn.example.com/a2.jpg", "https://cdn.example.com/a1.jpg"])
        # Nothing but the thread, the host's API and nothing of the redirect page.
        self.assertFalse([url for url in asked(ctx) if "/out/" in url])

    def test_unsafe_links_are_refused(self):
        bad = ["http://127.0.0.1/img/a1", "http://localhost/img/a1", "ftp://goonbox.cr/img/a1",
               "https://goonbox.cr.example.com/img/a1", "http://192.168.0.5/img/a1",
               "http://2130706433/img/a1"]
        body = "".join(f'<a href="{url}">x</a><a href="{wrap(url)}">y</a>' for url in bad)
        ctx = thread({1: page(post(5, body))})
        self.assertEqual(run(ctx, "images"), [])
        self.assertEqual(asked(ctx), [f"{FORUM}/threads/123/latest"])
        self.assertFalse(safe("http://[::1]/a"))
        self.assertTrue(safe("https://goonbox.cr/a"))

    def test_nothing_yields_a_target(self):
        ctx = thread(THREE)
        for category in ("images", "videos"):
            self.assertTrue(all(isinstance(item, Post) for item in run(ctx, category)))


class HostTest(unittest.TestCase):
    def files(self, body, **extra):
        ctx = thread({1: page(post(5, body))}, **extra)
        return collect(Simp, "smp:123", ctx=ctx, categories=["images", "videos"]), ctx

    def test_goonbox_image_and_names(self):
        found, ctx = self.files(goonbox("a1"))
        (one,) = found
        self.assertEqual((one.path, one.url, one.key),
                         ("123/images/2026-01-01_example_user_5_orig" + "a1.jpg", "https://cdn.example.com/a1.jpg",
                          "5_0"))
        self.assertIn("https://goonbox.cr/api/images/a1", asked(ctx))

    def test_goonbox_album_pages(self):
        body = '<a href="https://goonbox.cr/a/album-name.alb9">album</a>'

        def album(request):
            n = parse_qs(urlsplit(request.url).query)["page"][0]
            return Reply({"images": [{"original_url": f"https://cdn.example.com/p{n}.jpg"}],
                          "pagination": {"last_page": 2}})

        found, ctx = self.files(body, **{
            "https://goonbox.cr/api/albums/alb9/images": album})
        self.assertEqual([f.url for f in found],
                         ["https://cdn.example.com/p1.jpg", "https://cdn.example.com/p2.jpg"])
        self.assertEqual([f.path for f in found],
                         ["123/images/2026-01-01_example_user_5_0.jpg",
                          "123/images/2026-01-01_example_user_5_1.jpg"])

    def test_a_threads_title_names_its_folder_and_a_posts_author_its_files(self):
        for title, author, folder, name in [
            ("Example Thread", "example_user", "Example_Thread_123",
             "2026-01-01_example_user_5_origa1.jpg"),
            ("", "example_user", "123", "2026-01-01_example_user_5_origa1.jpg"),
            ("Example Thread", "", "Example_Thread_123", "2026-01-01_5_origa1.jpg"),
        ]:
            with self.subTest(title=title, author=author):
                ctx = thread({1: page(post(5, goonbox("a1"), author=author), title=title)})
                [one] = collect(Simp, "smp:123", ctx=ctx, categories=["images"])
                self.assertEqual(one.path, f"{folder}/images/{name}")
                self.assertEqual(one.key, "5_0")

    def test_a_bare_thumbnail_is_its_full_image(self):
        body = '<img class="bbImage" src="https://simp6.jpg5.su/x/name.md.jpg">'
        found, ctx = self.files(body)
        self.assertEqual([f.url for f in found], ["https://simp6.jpg5.su/x/name.jpg"])
        self.assertEqual(asked(ctx), [f"{FORUM}/threads/123/latest"])

    def test_turbo_is_signed_lazily(self):
        ctx = thread({1: page(post(5, turbo("v1")))}, **{
            "https://turbo.cr/api/sign": Reply({"url": "https://cdn.example.com/v1.mp4?s=1",
                                                "original_filename": "clip.mp4"})})
        found = run(ctx, "videos")
        self.assertEqual(len(found), 1)
        self.assertNotIn("https://turbo.cr/api/sign?v=v1", asked(ctx))
        self.assertTrue(found[0].lazy)
        files = asyncio.run(found[0].files())
        self.assertEqual(files[0].name, "2026-01-01_example_user_5_clip.mp4")
        self.assertEqual(asked(ctx)[-1], "https://turbo.cr/api/sign?v=v1")

    def test_a_host_that_changed_fails_its_files_only(self):
        body = goonbox("a1") + goonbox("a2")
        ctx = thread({1: page(post(5, body))}, **{
            "https://goonbox.cr/api/images/a1": Reply({"image": {"no": 1}})})
        found = collect(Simp, "smp:123", ctx=ctx)
        self.assertEqual([f.url for f in found], ["https://cdn.example.com/a2.jpg"])
        self.assertTrue(any("original_url" in text for _, text in ctx.seen.logs))
        alone = thread({1: page(post(5, goonbox("a1")))}, **{
            "https://goonbox.cr/api/images/a1": Reply({"image": {"no": 1}})})
        with self.assertRaises(Changed):
            asyncio.run(run(alone, "images")[0].files())

    def test_a_host_redirecting_away_is_changed(self):
        ctx = thread({1: page(post(5, goonbox("a1")))}, **{
            "https://goonbox.cr/api/images/a1": Reply({}, url="https://other.example.com/x")})
        with self.assertRaises(Changed):
            asyncio.run(run(ctx, "images")[0].files())


class WallTest(unittest.TestCase):
    def test_ddos_guard_is_a_block_with_a_hint(self):
        ctx = FakeCtx(Simp, "smp:123", routes={
            f"{FORUM}/threads/123/latest": Reply("", status=403,
                                                  headers={"server": "ddos-guard"})})
        with self.assertRaises(Blocked) as raised:
            run(ctx, "images")
        self.assertIn("flnd auth login smp", raised.exception.hint)
        self.assertTrue(ctx.seen.holds)

    def test_logged_out_page_is_a_login_error(self):
        ctx = thread({1: page(post(5, goonbox("a1")), logged="false")})
        with self.assertRaises(LoginError):
            run(ctx, "images")

    def test_a_redirect_to_login_is_a_wall(self):
        ctx = FakeCtx(Simp, "smp:123", routes={
            f"{FORUM}/threads/123/latest": Reply("", url=f"{FORUM}/login/")})
        with self.assertRaises(LoginError):
            run(ctx, "images")
        self.assertTrue(ctx.seen.holds)


class PaceTest(unittest.TestCase):
    def test_gaps_breaks_and_the_budget(self):
        pages = {n: page(post(n * 10, goonbox("a1"), stamp=STAMP + n * DAY)) for n in range(1, 31)}
        ctx = thread(pages)
        run(ctx, "images")
        slept = ctx._clock().slept
        breaks = [s for s in slept if s >= 45]
        self.assertEqual(len(breaks), 1)
        self.assertLessEqual(max(breaks), 90)
        # Every other wait is the pace's or a gap of at most JITTER seconds.
        self.assertTrue(all(s <= 12 for s in slept if s < 45))
        self.assertEqual(ctx.state["budget"]["used"], 30)

    def test_an_hour_budget_pauses_until_the_hour_turns(self):
        ctx = thread(THREE, settings={"pages_per_hour": 2})
        self.assertEqual(ids(run(ctx, "images")), ["31", "21", "11"])
        self.assertTrue(any(3000 <= s <= 3601 for s in ctx._clock().slept))
        self.assertTrue(any("hourly page budget" in text for _, text in ctx.seen.logs))
        self.assertEqual(ctx.state["budget"]["used"], 1)


CAPTCHA = f"{FORUM}/captcha?id=1&dir=abc"


class CaptchaTest(unittest.TestCase):
    def setUp(self):
        self.shown = {"open": False}

    def table(self, solved):
        def latest(request):
            if self.shown["open"]:
                return Reply(page(post(5, goonbox("a1"))), url=f"{BASE}/page-1")
            return Reply("<html><form></form></html>", url=CAPTCHA)
        return {f"{FORUM}/threads/123/latest": latest, **routes()}

    def page_for(self, solved):
        html = page(post(5, "x")) if solved else "<html><form id='captcha'></form></html>"
        return {f"{FORUM}/threads/123/latest": FakePage(html, url=CAPTCHA)}

    def test_a_solved_captcha_goes_on_and_halves_the_budget(self):
        ctx = FakeCtx(Simp, "smp:123", routes=self.table(True), pages=self.page_for(True))

        orig = ctx.open

        async def opening(url):
            self.shown["open"] = True
            return await orig(url)

        ctx.open = opening
        self.assertEqual(ids(run(ctx, "images")), ["5"])
        self.assertEqual(ctx.state["budget"]["cap"], 150)
        self.assertTrue(any("captcha solved" in text for _, text in ctx.seen.logs))

    def test_a_captcha_nobody_solves_fails_every_later_request(self):
        plugin = Simp()
        first = FakeCtx(plugin, "smp:123", routes=self.table(False), pages=self.page_for(False))
        with self.assertRaisesRegex(Blocked, "smp: simpcity.cr wants a captcha") as raised:
            run(first, "images")
        self.assertFalse(raised.exception.retryable)
        self.assertIn("solve it, then run again", raised.exception.hint)
        again = FakeCtx(plugin, "smp:123", routes=self.table(False))
        with self.assertRaisesRegex(Blocked, "wants a captcha"):
            run(again, "images")
        self.assertEqual(again.seen.requests, [])

    def test_two_targets_share_one_handoff(self):
        plugin = Simp()
        pages = self.page_for(True)
        one = FakeCtx(plugin, "smp:123", routes=self.table(True), pages=pages)
        two = FakeCtx(plugin, "smp:123", routes=self.table(True), pages=pages)
        opened = []

        def opening(ctx):
            orig = ctx.open

            async def go(url):
                opened.append(url)
                self.shown["open"] = True
                return await orig(url)
            return go

        one.open, two.open = opening(one), opening(two)

        async def both():
            async def listing(ctx):
                profile = await plugin.thread(ctx, "123")
                return [item async for item in plugin.images(ctx, profile)]
            plugin._threads.clear()
            return await asyncio.gather(listing(one), listing(two))

        results = asyncio.run(both())
        self.assertEqual(len(opened), 1)
        self.assertEqual([ids(found) for found in results], [["5"], ["5"]])

    def test_a_captcha_that_comes_back_fails_fast(self):
        ctx = FakeCtx(Simp, "smp:123", routes={
            f"{FORUM}/threads/123/latest": Reply("<html></html>", url=CAPTCHA), **routes()},
            pages=self.page_for(True))
        with self.assertRaisesRegex(Blocked, "wants a captcha"):
            run(ctx, "images")
        self.assertEqual(len(forum_asked(ctx)), 2)


if __name__ == "__main__":
    unittest.main()
