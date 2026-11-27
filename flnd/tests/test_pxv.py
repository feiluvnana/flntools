"""pixiv (`pxv`): its forms, profile reader, batches, head stop, errors, pace and budget."""

import asyncio
import datetime
import unittest
from urllib.parse import unquote

import no_profiles  # noqa: F401
from plugin_kit import FakeCtx, FakePage, Reply, check, collect

from flnd.errors import Blocked, Changed, Hidden, LoginError, NotFound
from flnd.plugin import Post, Target, UsageError
from flnd.plugins.pxv import Pixiv

SITE = "https://www.pixiv.net"
AJAX = f"{SITE}/ajax"
USER = "12345678"
REFERER = {"Referer": f"{SITE}/"}
CDN = "https://i.pximg.net/img-original/img/2026/01/02/03/04/05"
THUMB = "https://i.pximg.net/c/250x250_80_a2/img-master/img/2026/01/02/03/04/05/{}_square1200.jpg"
LIMIT = "https://s.pximg.net/common/images/limit_{}_360.png"


def stamp(day, hour=12):
    """Unix seconds of 2026-01-<day> at <hour>:00 in pixiv's +09:00."""
    return int(datetime.datetime(2026, 1, day, hour,
                                 tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
               .timestamp())


def work(id, *, kind=0, day=1, hour=12, **more):
    return {"id": str(id), "title": f"title {id}", "illustType": kind, "userName": "Example",
            "userId": USER, "pageCount": 1, "isMasked": False, "url": THUMB.format(id),
            "createDate": f"2026-01-{day:02d}T{hour:02d}:00:00+09:00", **more}


def listed(*ids):
    return {str(id): None for id in ids}


def profile(illusts=(), manga=()):
    return Reply({"error": False, "message": "", "body": {
        "illusts": listed(*illusts) or [], "manga": listed(*manga) or [], "novels": []}})


def batch(works):
    """The `profile/illusts` answer: the works asked for in `ids[]`, by id."""
    def answer(request):
        ids = [part.removeprefix("ids[]=") for part in unquote(request.url).split("?")[1]
               .split("&") if part.startswith("ids[]=")]
        return Reply({"error": False, "body": {"works": {
            id: works[id] for id in ids if id in works}}})
    return answer


def pages(id, count=1):
    return Reply({"error": False, "body": [
        {"urls": {"original": f"{CDN}/{id}_p{n}.png"}} for n in range(count)]})


def ok(body):
    return Reply({"error": False, "message": "", "body": body})


def fail(status, **more):
    return Reply({"error": True, "message": "(any language)", "body": []}, status=status, **more)


def user_ctx(illusts=(), manga=(), works=None, **more):
    """A fake of user 12345678: the id lists, the works of each, and a pages answer for each."""
    works = works if works is not None else {}
    table = {f"{AJAX}/user/{USER}/profile/all": profile(illusts, manga),
             f"{AJAX}/user/{USER}/profile/illusts": batch(works)}
    table.update({f"{AJAX}/illust/{id}/pages": pages(id) for id in works})
    return FakeCtx(Pixiv, f"pxv:{USER}", routes={**table, **more.pop("routes", {})}, **more)


def run(ctx, category):
    async def go():
        plugin = ctx._plugin
        user = await plugin.user(ctx, USER)
        return [item async for item in getattr(plugin, category)(ctx, user)]
    return asyncio.run(go())


def asked(ctx):
    return [url.removeprefix(AJAX) for _, url, _ in ctx.seen.requests]


def ids(found):
    return [item.id for item in found]


def thirds(count, *, kind=0, first=1):
    return {str(first + n): work(first + n, kind=kind, day=1 + n % 28) for n in range(count)}


class FormsTest(unittest.TestCase):
    def test_user_forms(self):
        for text in (f"pxv:{USER}", f"PXV:{USER}", f"https://www.pixiv.net/users/{USER}",
                     f"pixiv.net/users/{USER}", f"https://www.pixiv.net/en/users/{USER}",
                     f"https://www.pixiv.net/ja/users/{USER}/illustrations",
                     f"https://www.pixiv.net/en/users/{USER}/bookmarks/artworks",
                     f"https://www.pixiv.net/member.php?id={USER}",
                     f"https://www.pixiv.net/member_illust.php?id={USER}",
                     f"https://www.pixiv.net/member_illust.php?mode=medium&id={USER}"):
            with self.subTest(text=text):
                self.assertEqual(Pixiv.resolve(text), Target(f"pxv:{USER}", kind="user"))

    def test_artwork_forms(self):
        for text in ("pxv:artwork/5", "https://www.pixiv.net/artworks/5",
                     "https://www.pixiv.net/en/artworks/5", "pixiv.net/i/5",
                     "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=5",
                     "https://www.pixiv.net/member_illust.php?illust_id=5"):
            with self.subTest(text=text):
                self.assertEqual(Pixiv.resolve(text), Target("pxv:artwork/5", kind="artwork"))

    def test_a_name_is_refused_with_the_number_hint(self):
        for text in ("pxv:example_user", "pxv:@example_user", "PXV:example.user"):
            with self.subTest(text=text), self.assertRaises(UsageError) as raised:
                Pixiv.resolve(text)
            self.assertEqual(str(raised.exception),
                             "pxv: a pixiv user is written by its number: pxv:12345678")
            self.assertEqual(raised.exception.hint, "the number in pixiv.net/users/<number>; "
                             "a pixiv.me/<name> link works too")

    def test_unsupported_pages(self):
        for text in ("https://www.pixiv.net/", "https://www.pixiv.net/ranking.php?mode=daily",
                     "https://www.pixiv.net/en/ranking.php", "https://www.pixiv.net/discovery",
                     "https://www.pixiv.net/request", "https://www.pixiv.net/stacc",
                     "https://www.pixiv.net/bookmark_new_illust.php",
                     "https://www.pixiv.net/settings/viewing", "https://www.pixiv.net/dashboard",
                     "https://www.pixiv.net/novel/series/5",
                     "https://www.pixiv.net/user/5/series/9",
                     "https://www.pixiv.net/en/tags/example/artworks",
                     "https://www.pixiv.net/tags/example/artworks",
                     "https://sketch.pixiv.net/@example_user", "https://comic.pixiv.net/works/5"):
            with self.subTest(text=text), self.assertRaisesRegex(
                    UsageError, r"pxv: .*isn't supported|pxv: .*aren't supported"):
                Pixiv.resolve(text)

    def test_what_is_not_pixivs_is_left_alone(self):
        for text in ("pxv:", "https://example.com/users/5", "pixiv.me/example_user"):
            with self.subTest(text=text):
                self.assertIsNone(Pixiv.resolve(text))

    def test_declarations(self):
        self.assertTrue(check(Pixiv))
        self.assertEqual(Pixiv.categories, ("illusts", "manga"))
        self.assertEqual(Pixiv.domains, ("pixiv.net", "pixiv.me"))
        self.assertTrue(Pixiv.login.proves("PHPSESSID"))
        self.assertFalse(Pixiv.login.required)
        self.assertEqual(dict(Pixiv.limits), {"i.pximg.net": 4})
        self.assertEqual((Pixiv.pace, Pixiv.folder, Pixiv.filename, Pixiv.key, Pixiv.fields),
                         (2.0, "{category}", "{date}_{post}_p{num}", "{post}_{num}",
                          ("title", "user")))


class ReaderTest(unittest.TestCase):
    def test_ids_are_sorted_as_numbers_newest_first(self):
        works = {"9": work(9, day=3), "10": work(10, day=4), "100": work(100, day=5)}
        ctx = user_ctx(illusts=(9, 100, 10), works=works)
        self.assertEqual(ids(run(ctx, "illusts")), ["100", "10", "9"])

    def test_both_empty_shapes_of_profile_all(self):
        # An empty list is `[]`; one with works is an object keyed by id.
        ctx = user_ctx(illusts=(), manga=(7,), works={"7": work(7, kind=1)})
        self.assertEqual(ids(run(ctx, "illusts")), [])
        self.assertEqual(ids(run(ctx, "manga")), ["7"])
        # Nothing to batch for an empty list.
        self.assertEqual(asked(ctx), [f"/user/{USER}/profile/all",
                                      f"/user/{USER}/profile/illusts?ids[]=7"
                                      "&work_category=illustManga&is_first_page=0"])
        both = user_ctx(routes={f"{AJAX}/user/{USER}/profile/all": Reply({
            "error": False, "body": {"illusts": {}, "manga": None}})})
        self.assertEqual(ids(run(both, "illusts")), [])

    def test_the_categories_split_by_list_and_illust_type(self):
        works = {"3": work(3, day=3), "2": work(2, kind=2, day=2), "1": work(1, kind=1, day=1)}
        ctx = user_ctx(illusts=(3, 2), manga=(1,), works=works)
        self.assertEqual(ids(run(ctx, "illusts")), ["3"])
        self.assertEqual(ids(run(ctx, "manga")), ["1"])
        # The profile is read once for both categories.
        self.assertEqual(asked(ctx).count(f"/user/{USER}/profile/all"), 1)

    def test_batches_of_100(self):
        works = thirds(250)
        ctx = user_ctx(illusts=range(1, 251), works=works)
        found = run(ctx, "illusts")
        self.assertEqual(len(found), 250)
        self.assertEqual(ids(found)[:2], ["250", "249"])
        sizes = [url.count("ids[]=") for url in asked(ctx) if "profile/illusts" in url]
        self.assertEqual(sizes, [100, 100, 50])

    def test_a_deleted_id_is_skipped(self):
        ctx = user_ctx(illusts=(1, 2), works={"2": work(2)})
        self.assertEqual(ids(run(ctx, "illusts")), ["2"])

    def test_post_fields(self):
        ctx = user_ctx(illusts=(5,), works={"5": work(5, day=2, hour=3)})
        (one,) = run(ctx, "illusts")
        self.assertIsInstance(one, Post)
        self.assertEqual((one.id, one.date, dict(one.headers), one.fields["title"],
                          one.fields["user"], one.lazy),
                         ("5", stamp(2, 3), REFERER, "title 5", "Example", True))
        self.assertNotIn("/illust/5/pages", asked(ctx))

    def test_files_come_lazily_from_pages(self):
        ctx = user_ctx(illusts=(5,), works={"5": work(5, pageCount=2)}, routes={
            f"{AJAX}/illust/5/pages": pages(5, 2)})
        (one,) = run(ctx, "illusts")
        files = asyncio.run(one.files())
        self.assertEqual([f.url for f in files], [f"{CDN}/5_p0.png", f"{CDN}/5_p1.png"])
        self.assertEqual(asked(ctx)[-1], "/illust/5/pages")

    def test_collect_names_and_keys(self):
        ctx = user_ctx(illusts=(5,), works={"5": work(5, day=2)}, routes={
            f"{AJAX}/illust/5/pages": pages(5, 2)})
        found = collect(Pixiv, f"pxv:{USER}", ctx=ctx)
        self.assertEqual([(f.path, f.key) for f in found],
                         [("illusts/2026-01-02_5_p0.png", "5_0"),
                          ("illusts/2026-01-02_5_p1.png", "5_1")])

    def test_requests_carry_the_referer_and_accept_headers(self):
        ctx = user_ctx(illusts=(5,), works={"5": work(5)})
        run(ctx, "illusts")
        for _, url, options in ctx.seen.requests:
            self.assertEqual(options["headers"]["Referer"], f"{SITE}/", url)
            self.assertEqual(options["headers"]["Accept"], "application/json")

    def test_sign_leaves_other_requests_alone(self):
        from flnd.plugin import Request
        plugin = Pixiv()
        page = Request(f"{SITE}/users/5")
        self.assertIs(plugin.sign(page, {}), page)
        cdn = Request("https://i.pximg.net/a.png")
        self.assertIs(plugin.sign(cdn, {}), cdn)


class HeadTest(unittest.TestCase):
    def listing(self, head, count=250):
        # Ids 1..count, the newest id the newest day: 3 batches of 100, 100, 50.
        works = {str(n): work(n, day=1 + (n - 1) * 27 // max(1, count - 1)) for n in
                 range(1, count + 1)}
        ctx = user_ctx(illusts=range(1, count + 1), works=works, head=head)
        run(ctx, "illusts")
        return [url for url in asked(ctx) if "profile/illusts" in url]

    def test_no_head_reads_every_batch(self):
        self.assertEqual(len(self.listing(None)), 3)

    def test_a_batch_older_than_the_head_stops_the_listing(self):
        # Batch 1 is days 28 to 11; the head is on day 28: batch 1's newest is the head's time,
        # which is not older, so batch 2 is asked for; batch 2's newest is older, so no batch 3.
        head = {"id": "250", "ts": stamp(28)}
        self.assertEqual(len(self.listing(head)), 2)

    def test_a_whole_batch_must_be_older_not_equal(self):
        head = {"id": "250", "ts": stamp(1)}
        self.assertEqual(len(self.listing(head)), 3)

    def test_a_caught_up_profile_asks_for_one_batch(self):
        works = {str(n): work(n, day=1) for n in range(1, 201)}
        ctx = user_ctx(illusts=range(1, 201), works=works, head={"id": "200", "ts": stamp(2)})
        run(ctx, "illusts")
        self.assertEqual(asked(ctx)[0], f"/user/{USER}/profile/all")
        self.assertEqual(len([url for url in asked(ctx) if "profile/illusts" in url]), 1)

    def test_a_day_head_compares_days(self):
        works = {str(n): work(n, day=1) for n in range(1, 201)}
        ctx = user_ctx(illusts=range(1, 201), works=works, head={"id": "200", "ts": "2026-01-02"})
        run(ctx, "illusts")
        self.assertEqual(len([url for url in asked(ctx) if "profile/illusts" in url]), 1)

    def test_a_head_with_no_time_never_stops_a_batch(self):
        self.assertEqual(len(self.listing({"id": "250"})), 3)


class SingleTest(unittest.TestCase):
    def single(self, text="pxv:artwork/5", **body):
        shown = {"illustType": 0, "userId": USER, "pageCount": 1, "title": "title 5",
                 "userName": "Example", "createDate": "2026-01-02T12:00:00+09:00",
                 "urls": {"original": f"{CDN}/5_p0.png", "small": THUMB.format(5)}, **body}
        return FakeCtx(Pixiv, text, routes={f"{AJAX}/illust/5": ok(shown),
                                            f"{AJAX}/illust/5/pages": pages(5, 3)})

    def test_one_page_work_is_filed_by_owner_and_category(self):
        ctx = self.single()
        (one,) = collect(Pixiv, "pxv:artwork/5", ctx=ctx)
        self.assertEqual((one.url, one.path, one.key, one.post.owner, one.post.category),
                         (f"{CDN}/5_p0.png", "illusts/2026-01-02_5_p0.png", "5_0", USER,
                          "illusts"))
        self.assertEqual(asked(ctx), ["/illust/5"])

    def test_a_manga_goes_to_the_manga_folder(self):
        ctx = self.single(illustType=1)
        (one,) = collect(Pixiv, "pxv:artwork/5", ctx=ctx)
        self.assertEqual((one.path, one.post.owner, one.post.category),
                         ("manga/2026-01-02_5_p0.png", USER, "manga"))

    def test_a_many_page_work_reads_pages(self):
        ctx = self.single(pageCount=3)
        found = collect(Pixiv, "pxv:artwork/5", ctx=ctx)
        self.assertEqual(len(found), 3)
        self.assertEqual(asked(ctx), ["/illust/5", "/illust/5/pages"])

    def test_an_ugoira_yields_nothing_and_warns(self):
        ctx = self.single(illustType=2)
        self.assertEqual(collect(Pixiv, "pxv:artwork/5", ctx=ctx), [])
        self.assertTrue(any("ugoira" in text for _, text in ctx.seen.logs))

    def test_the_referer_header_is_on_the_post(self):
        ctx = self.single()

        async def go():
            return [item async for item in ctx._plugin.artwork(ctx, "artwork/5")]
        (one,) = asyncio.run(go())
        self.assertEqual((dict(one.headers), one.owner, one.category),
                         (REFERER, USER, "illusts"))

    def test_a_masked_single_work_is_hidden(self):
        ctx = self.single(urls={"small": LIMIT.format("sanity_level")})
        with self.assertRaises(Hidden):
            collect(Pixiv, "pxv:artwork/5", ctx=ctx)

    def test_a_work_with_no_owner_is_changed(self):
        ctx = self.single(userId=None)
        with self.assertRaises(Changed):
            collect(Pixiv, "pxv:artwork/5", ctx=ctx)


class HiddenTest(unittest.TestCase):
    def post(self, **more):
        ctx = user_ctx(illusts=(5,), works={"5": work(5, **more)})
        return ctx, run(ctx, "illusts")[0]

    def test_each_limit_image(self):
        hints = {"sanity_level": "turn on R-18 (and R-18G) in pixiv's settings",
                 "mypixiv": "only the artist's My pixiv friends can see it",
                 "unviewable": None}
        for why, hint in hints.items():
            with self.subTest(why=why):
                ctx, post = self.post(url=LIMIT.format(why))
                with self.assertRaises(Hidden) as raised:
                    asyncio.run(post.files())
                if hint is None:
                    self.assertIsNone(raised.exception.hint)
                else:
                    self.assertIn(hint, raised.exception.hint)
                # The post is listed; nothing but the profile and its batch was asked for.
                self.assertFalse([url for url in asked(ctx) if "/pages" in url])

    def test_is_masked(self):
        _, post = self.post(isMasked=True)
        with self.assertRaises(Hidden):
            asyncio.run(post.files())

    def test_only_that_post_fails(self):
        works = {"5": work(5, day=3, url=LIMIT.format("mypixiv")), "4": work(4, day=2)}
        ctx = user_ctx(illusts=(4, 5), works=works)
        found = collect(Pixiv, f"pxv:{USER}", ctx=ctx)
        self.assertEqual([f.post.id for f in found], ["4"])


class ErrorsTest(unittest.TestCase):
    def listing(self, reply, **more):
        ctx = user_ctx(routes={f"{AJAX}/user/{USER}/profile/all": reply}, **more)
        return ctx, lambda: run(ctx, "illusts")

    def test_a_403_json_answer_is_hidden_not_a_login_error(self):
        _, go = self.listing(fail(403))
        with self.assertRaises(Hidden) as raised:
            go()
        self.assertIn("flnd auth login pxv", raised.exception.hint)
        self.assertNotIsInstance(raised.exception, LoginError)

    def test_a_logged_in_403_has_no_login_hint(self):
        _, go = self.listing(fail(403), cookies={"PHPSESSID": "x"})
        with self.assertRaises(Hidden) as raised:
            go()
        self.assertIsNone(raised.exception.hint)

    def test_a_400_json_answer_is_hidden(self):
        _, go = self.listing(fail(400))
        with self.assertRaises(Hidden):
            go()

    def test_404_is_not_found(self):
        _, go = self.listing(fail(404))
        with self.assertRaises(NotFound):
            go()

    def test_error_on_a_200_is_not_found(self):
        _, go = self.listing(Reply({"error": True, "message": "", "body": []}))
        with self.assertRaises(NotFound):
            go()

    def test_401_is_a_login_error(self):
        _, go = self.listing(Reply("", status=401))
        with self.assertRaises(LoginError) as raised:
            go()
        self.assertIn("flnd auth login pxv", raised.exception.hint)

    def test_429_holds_for_retry_after(self):
        _, go = self.listing(Reply({"error": True}, status=429, headers={"Retry-After": "7"}))
        with self.assertRaises(Blocked):
            go()
        self.assertEqual(Pixiv().blocked(_status(429, {"retry-after": "7"})), 7.0)
        self.assertEqual(Pixiv().blocked(_status(429, {})), 0.0)
        self.assertIsNone(Pixiv().blocked(_status(403, {})))

    def test_html_or_a_missing_body_is_changed(self):
        for reply in (Reply("<html></html>"), Reply({"error": False}), Reply([1]),
                      Reply({"message": "x", "body": {}})):
            _, go = self.listing(reply)
            with self.subTest(reply=reply), self.assertRaises(Changed):
                go()

    def test_the_message_text_is_never_read(self):
        # The same status and `error` give the same answer in any language.
        for message in ("", "(any language)", "not found"):
            _, go = self.listing(Reply({"error": True, "message": message, "body": []},
                                       status=403))
            with self.subTest(message=message), self.assertRaises(Hidden):
                go()


def _status(status, headers):
    class Answer:
        pass
    answer = Answer()
    answer.status, answer.headers = status, headers
    return answer


class PaceTest(unittest.TestCase):
    def test_a_gap_before_each_request_but_the_first_and_a_break_every_100(self):
        ctx = user_ctx(illusts=range(1, 1201), works=thirds(1200))
        run(ctx, "illusts")  # 1 profile + 12 batches
        slept = ctx._clock().slept
        self.assertEqual(ctx.state["budget"]["used"], 13)
        self.assertTrue(all(s <= 2 for s in slept if s < 30))
        ctx2 = user_ctx(illusts=range(1, 1201), works=thirds(1200))
        ctx2._plugin._calls = 99
        run(ctx2, "illusts")
        breaks = [s for s in ctx2._clock().slept if s >= 30]
        self.assertEqual(len(breaks), 1)
        self.assertLessEqual(max(breaks), 60)

    def test_an_hour_budget_pauses_until_the_hour_turns(self):
        works = thirds(250)
        ctx = user_ctx(illusts=range(1, 251), works=works, settings={"requests_per_hour": 2})
        self.assertEqual(len(run(ctx, "illusts")), 250)
        self.assertTrue(any(3000 <= s <= 3601 for s in ctx._clock().slept))
        self.assertTrue(any("hourly request budget" in text for _, text in ctx.seen.logs))
        self.assertEqual(ctx.state["budget"]["cap"], 2)

    def test_the_default_budget_is_900(self):
        ctx = user_ctx(illusts=(1,), works={"1": work(1)})
        run(ctx, "illusts")
        self.assertEqual(ctx.state["budget"]["cap"], 900)


CHECK_HTML = "<html><head><title>Just a moment...</title></head><body></body></html>"
OPEN_HTML = "<html><head><title>pixiv</title></head><body></body></html>"
CHECK_HEAD = {"cf-mitigated": "challenge"}


def by_header(request=None):
    return Reply("<html></html>", status=403, headers=CHECK_HEAD)


def by_html(request=None):
    return Reply(CHECK_HTML)


class CaptchaTest(unittest.TestCase):
    """Cloudflare's check: found by header or HTML, handed to a person once, then HTTP or the
    tab."""

    def setUp(self):
        self.tab = {f"{SITE}/": FakePage(CHECK_HTML)}

    def solving(self, ctx):
        """A person who solves the check when asked: the tab shows pixiv afterwards."""
        asked = []
        orig = ctx.handoff

        async def handoff(message, *, until, timeout=None):  # noqa: ASYNC109
            asked.append((message, timeout))
            ctx._tabs[-1].load(FakePage(OPEN_HTML))
            return await orig(message, until=until, timeout=timeout)

        ctx.handoff = handoff
        return asked

    def challenged(self, check, then, *, times=1):
        """A profile route that is challenged `times` times and then answers `then`."""
        count = {"n": 0}

        def answer(request):
            count["n"] += 1
            return check() if count["n"] <= times else then
        return answer

    def test_a_challenge_is_found_by_header_or_by_html(self):
        script = Reply("<script src='/cdn-cgi/challenge-platform/x'></script>")
        for answer in (by_header, by_html, lambda request: script):
            with self.subTest(answer=answer):
                ctx = user_ctx(routes={f"{AJAX}/user/{USER}/profile/all": answer},
                               pages=self.tab)
                with self.assertRaisesRegex(Blocked, "pxv: pixiv.net wants a captcha"):
                    run(ctx, "illusts")

    def test_a_solved_check_retries_over_http_and_halves_the_budget(self):
        ctx = user_ctx(illusts=(1,), works={"1": work(1)}, pages=self.tab, routes={
            f"{AJAX}/user/{USER}/profile/all": self.challenged(by_header, profile((1,)))})
        asked = self.solving(ctx)
        self.assertEqual(ids(run(ctx, "illusts")), ["1"])
        self.assertEqual(asked, [("Solve the pixiv check in the browser window", 600)])
        self.assertEqual(ctx.state["budget"]["cap"], 450)
        self.assertTrue(any("check solved" in text for _, text in ctx.seen.logs))
        self.assertFalse(any(opts["in_page"] for _, _, opts in ctx.seen.requests))

    def test_a_second_check_switches_to_the_tab(self):
        ctx = user_ctx(illusts=(1,), works={"1": work(1)}, pages=self.tab, routes={
            f"{AJAX}/user/{USER}/profile/all":
                self.challenged(by_html, profile((1,)), times=2)})
        self.solving(ctx)
        self.assertEqual(ids(run(ctx, "illusts")), ["1"])
        flags = [opts["in_page"] for _, _, opts in ctx.seen.requests]
        self.assertEqual(flags, [False, False, True, True])
        self.assertEqual(ctx.state["budget"]["cap"], 450)

    def test_a_check_in_the_tab_fails_every_later_request(self):
        plugin = Pixiv()
        ctx = user_ctx(pages=self.tab, routes={f"{AJAX}/user/{USER}/profile/all": by_html})
        ctx._plugin = plugin
        self.solving(ctx)
        with self.assertRaisesRegex(Blocked, "wants a captcha") as raised:
            run(ctx, "illusts")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual([opts["in_page"] for _, _, opts in ctx.seen.requests],
                         [False, False, True])

    def test_an_unsolved_check_fails_without_a_retry(self):
        plugin = Pixiv()
        first = FakeCtx(plugin, f"pxv:{USER}", pages=self.tab,
                        routes={f"{AJAX}/user/{USER}/profile/all": by_header})
        with self.assertRaisesRegex(Blocked, "^pxv: pixiv.net wants a captcha$") as raised:
            run(first, "illusts")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.hint,
                         "open www.pixiv.net in your browser, solve it, then run again")
        self.assertEqual(len(first.seen.requests), 1)
        again = FakeCtx(plugin, f"pxv:{USER}", routes={})
        with self.assertRaisesRegex(Blocked, "wants a captcha"):
            run(again, "illusts")
        self.assertEqual(again.seen.requests, [])

    def test_two_targets_share_one_solve(self):
        plugin = Pixiv()
        count = {"n": 0}

        def listing(request):
            count["n"] += 1
            return by_header() if count["n"] <= 2 else profile((1,))

        def make(user):
            return FakeCtx(plugin, f"pxv:{user}", pages=self.tab, routes={
                f"{AJAX}/user/{user}/profile/all": listing,
                f"{AJAX}/user/{user}/profile/illusts": batch({"1": work(1)})})

        one, two = make("111"), make("222")
        opened = []
        for ctx in (one, two):
            orig = ctx.open

            async def opening(url, orig=orig):
                opened.append(url)
                return await orig(url)
            ctx.open = opening
        self.solving(one)
        self.solving(two)

        async def both():
            async def listing_of(ctx, user):
                reader = await plugin.user(ctx, user)
                return [item async for item in plugin.illusts(ctx, reader)]
            return await asyncio.gather(listing_of(one, "111"), listing_of(two, "222"))

        results = asyncio.run(both())
        self.assertEqual(len(opened), 1)
        self.assertEqual([ids(found) for found in results], [["1"], ["1"]])


if __name__ == "__main__":
    unittest.main()
