"""Smart sync and the failure report: the one sync rule (stop at the head id or an older time),
the automatic retry, `flnd retry`, `fl.status`, and the samples of changed answers, kept
scrubbed.

Every test runs the engine on a temporary home over a fake network and a fake clock
(`EngineCase`). The `Feed` plugin pages its posts by cursor, the way the sites' APIs do: each
page holds `PAGE` posts, newest first, and its cursor is the last post's id, so a cursor still
works after new posts arrive on top. Each listing signs its file URLs afresh (`sig=s<n>`), as a
CDN's signed URLs are, so a test can tell a fresh URL from a stored one.
"""

import datetime
import io
import os
import sqlite3
import time
import unittest
from contextlib import redirect_stdout

import fakes
from fakes import CDN, Engine, EngineCase, login_cookies
from test_library import registered

from flnd import _library, errors
from flnd import cli as flnd_cli
from flnd.engine import store as _store
from flnd.engine.events import NOTHING_NEW, Saved, Skipped
from flnd.errors import Changed
from flnd.plugin import File, Plugin, Post, Range, route
from flnd.ui.log import ui
from flnkit import net as _net

API = "https://api.example.com"
# Posts a page of the feed holds.
PAGE = 4
DAY = datetime.date(2026, 1, 1)
# 2026-01-02 00:00 UTC, a time the fake clock (2026-01-01) finds right.
T0 = 1767312000


def dated(post_id):
    """The day of a feed post: `pN` is N days before DAY, `nN` and `xN` are newer than any `p`."""
    n = int(post_id[1:])
    return DAY + datetime.timedelta(days={"p": -n, "n": 100 - n, "x": 200 - n}[post_id[0]])


class FeedCase(EngineCase):
    """A home with the `fed` plugin, whose feed (`self.posts`, newest first) pages by cursor."""

    def setUp(self):
        super().setUp()
        self.posts = [f"p{number}" for number in range(1, 10)]
        # The cursor whose page the API fails, as a site's 500 does.
        self.fail_after = None
        # How many times the feed was listed: each listing signs its URLs with the next number.
        self.listings = 0
        # A token each file URL carries as well, once set, as a CDN's signed URLs do.
        self.signed = ""
        # What `ctx.head` gave each listing of `posts`, in order.
        self.heads = []
        self.source = self.feed_source()
        self.routes[f"{API}/feed"] = self.page
        self.engine = Engine(home=self.home, sources={"fed": self.source}, emit=self.events.append)

    def feed_source(self, *, info=None, **declared):
        """The `fed` plugin; `info`, a second category's method, and `declared` its other fields."""
        test = self

        async def user(self, ctx, user):
            return user

        async def posts(self, ctx, user):
            test.listings += 1
            test.heads.append(ctx.head)
            sig = f"s{test.listings}" + (f"&token={test.signed}" if test.signed else "")
            async for response in ctx.pages(f"{API}/feed", cursor="next", send="after"):
                for post_id in response.json["ids"]:
                    yield Post(post_id, [File(f"{CDN}/{post_id}.jpg?sig={sig}")],
                               date=dated(post_id))

        listed = route("fed:{user}", newest_first=False)(user)
        listed.category(newest_first=True)(posts)
        attributes = {"code": "fed", "name": "Feed", "domains": ("example.com",), "api": 2,
                      "key": "{post}", "filename": "{post}", "user": listed, "posts": posts,
                      **declared}
        if info is not None:
            listed.category(info)
            attributes["info"] = info
        return type("Feed", (Plugin,), attributes)

    def page(self, request):
        after = dict(request.url.params).get("after")
        if after is not None and after == self.fail_after:
            return _net.reply(500)
        start = self.posts.index(after) + 1 if after is not None else 0
        ids = self.posts[start:start + PAGE]
        more = start + PAGE < len(self.posts)
        return _net.reply(200, json={"ids": ids, "next": ids[-1] if more else None})

    def pages(self):
        """The cursor each request of the feed sent, in order: None for the newest page."""
        return [dict(request.url.params).get("after") for request in self.requests
                if str(request.url).startswith(f"{API}/feed")]

    def asked(self, post):
        """The requests for a post's file."""
        return [request for request in self.requests
                if str(request.url).split("?")[0] == f"{CDN}/{post}.jpg"]

    def download(self):
        return self.engine.download(["fed:example_user|all"], sync=True)

    def sync(self):
        self.requests.clear()
        self.events.clear()
        return self.engine.sync(["fed:example_user"])

    def saved(self):
        return sorted(key.partition(":")[2] for key in fakes.history(self.home))

    def row(self):
        return fakes.targets(self.home)["fed:example_user"]


# ---------------------------------------------------------------------------------------------
# What a plugin sees of the head
# ---------------------------------------------------------------------------------------------

class CtxHeadTest(FeedCase):
    def test_a_newest_first_category_sees_none_then_its_head_on_a_sync(self):
        self.assertTrue(self.download().ok)
        self.assertEqual(self.heads, [None])
        self.assertTrue(self.sync().ok)
        self.assertEqual(len(self.heads), 2)
        head = self.heads[1]
        self.assertEqual(head["id"], "p1")
        self.assertIn("ts", head)
        with self.assertRaises(TypeError):
            head["id"] = "x"

    def test_again_sees_none(self):
        self.assertTrue(self.download().ok)
        self.assertTrue(
            self.engine.download(["fed:example_user|all"], sync=True, again=True).ok)
        self.assertEqual(self.heads, [None, None])

    def test_a_category_that_is_not_newest_first_sees_none(self):
        seen = []

        async def info(plugin, ctx, user):
            seen.append(ctx.head)
            yield Post("s1", [File(f"{CDN}/s1.jpg")], date=DAY)

        self.source = self.feed_source(info=info)
        self.engine = Engine(home=self.home, sources={"fed": self.source}, emit=self.events.append)
        self.assertTrue(self.download().ok)
        self.assertTrue(self.sync().ok)
        self.assertEqual(seen, [None, None])
        self.assertEqual(self.heads[0], None)
        self.assertEqual(self.heads[1]["id"], "p1")


# ---------------------------------------------------------------------------------------------
# Nothing new
# ---------------------------------------------------------------------------------------------

class NothingNewTest(FeedCase):
    def test_a_sync_with_nothing_new_makes_one_request(self):
        self.assertTrue(self.download().ok)
        self.assertEqual(self.row().heads, {"posts": {"id": "p1", "ts": "2025-12-31"}})
        report = self.sync()
        # The head and three older posts are on the first page: one request, no file.
        self.assertEqual([str(request.url) for request in self.requests], [f"{API}/feed"])
        self.assertEqual(report.files, 0)
        [skipped] = [event for event in self.events if isinstance(event, Skipped)
                     and event.reason == NOTHING_NEW]
        self.assertEqual((skipped.category, skipped.post, skipped.path), ("posts", "", None))
        self.assertIn("* posts: nothing new", self.lines())

    def test_the_library_reports_it(self):
        self.download()
        with _library.open(self.home) as fl:
            self.enterContext(registered(self.source))
            report = fl.get().wait()
        # A listing with nothing new is no file skipped.
        self.assertEqual((report.unchanged, report.skipped, report.saved), (1, 4, 0))

    def test_sync_and_get_check_non_newest_first_and_shortcircuit_posts(self):
        stories = ["s1"]

        async def info(plugin, ctx, user):
            for sid in stories:
                yield Post(sid, [File(f"{CDN}/{sid}.jpg")], date=DAY)

        self.source = self.feed_source(info=info)
        self.engine = Engine(home=self.home, sources={"fed": self.source}, emit=self.events.append)
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text('[defaults]\nfresh = "2h"\n', encoding="utf-8")
        self.assertTrue(self.download().ok)
        self.assertIn("s1", self.saved())

        # Even within `fresh = "2h"`, `sync` still checks `info` (`stories`) while `posts` stops.
        stories.append("s2")
        self.assertTrue(self.sync().ok)
        self.assertEqual(self.pages(), [None])
        self.assertIn("s2", self.saved())

        # Default `get` (`fresh = "0"`) also checks new `info` (`stories`) and stops on `posts`.
        (self.home / "config.toml").write_text("", encoding="utf-8")
        stories.append("s3")
        self.requests.clear()
        self.assertTrue(self.download().ok)
        self.assertEqual(self.pages(), [None])
        self.assertIn("s3", self.saved())

        # `--again` bypasses short-circuiting and lists every page of `posts`.
        self.requests.clear()
        self.assertTrue(
            self.engine.download(["fed:example_user|all"], sync=True, again=True).ok)
        self.assertEqual(self.pages(), [None, "p4", "p8"])

    def test_when_since_stop_leaves_no_stale_cursor(self):
        dates = {"p1": datetime.date(2026, 1, 10)}
        for n in range(2, 10):
            dates[f"p{n}"] = datetime.date(2025, 12, 1)

        async def posts(plugin, ctx, user):
            async for response in ctx.pages(f"{API}/feed", cursor="next", send="after"):
                for pid in response.json["ids"]:
                    yield Post(pid, [File(f"{CDN}/{pid}.jpg")], date=dates[pid])

        async def user(plugin, ctx, user):
            return user

        listed = route("fed:{user}", newest_first=False)(user)
        listed.category(newest_first=True)(posts)
        self.source = type("DatedFeed", (Plugin,), {
            "code": "fed", "name": "Feed", "domains": ("example.com",), "api": 2,
            "key": "{post}", "filename": "{post}", "user": listed, "posts": posts,
        })
        self.engine = Engine(home=self.home, sources={"fed": self.source}, emit=self.events.append)

        # Initial `--when` run (`_list_one`): clears the resume cursor and stays incomplete.
        report = self.engine.download(["fed:example_user|all"], sync=True, time=Range(since=DAY))
        self.assertTrue(report.ok)
        self.assertEqual(self.saved(), ["p1"])
        row = self.row()
        self.assertNotIn("posts", row.complete)
        self.assertFalse(row.cursors.get("posts"))

        # Next full `get` starts from newest (`None`), gets p2..p9, and marks complete.
        self.requests.clear()
        self.assertTrue(self.download().ok)
        self.assertEqual(self.pages(), [None, "p4", "p8"])
        self.assertEqual(self.saved(), sorted(self.posts))
        self.assertEqual(self.row().heads, {"posts": {"id": "p1", "ts": "2026-01-10"}})

        # `sync` with `--when` on a complete target leaves no cursor; next sync short-circuits.
        self.requests.clear()
        self.assertTrue(self.engine.sync(["fed:example_user"], time=Range(since=DAY)).ok)
        self.assertEqual(self.row().cursors, {})
        self.assertEqual(self.row().heads, {"posts": {"id": "p1", "ts": "2026-01-10"}})
        self.assertTrue(self.sync().ok)
        self.assertEqual(self.pages(), [None])


# ---------------------------------------------------------------------------------------------
# The one rule: stop at the head id, or at a strictly older time
# ---------------------------------------------------------------------------------------------

class CutTest(FeedCase):
    """A sync that doesn't reach the head leaves it, so the next one reads down to it."""

    def test_a_sync_cut_short_keeps_its_head_and_the_next_reads_down_to_it(self):
        self.download()
        old_head = {"id": "p1", "ts": "2025-12-31"}
        # Four pages of new posts; the sync is cut after six of them (`--limit`).
        self.posts = [f"n{number}" for number in range(1, 13)] + self.posts
        self.engine.download(["fed:example_user|all"], limit=6)
        self.assertEqual(self.saved(), sorted([*self.posts[:6], *self.posts[12:]]))
        self.assertEqual((self.row().heads, self.row().cursors), ({"posts": old_head}, {}))
        # Two more posts arrive; the site answers again.
        self.posts = ["x1", "x2", *self.posts]
        report = self.sync()
        self.assertTrue(report.ok)
        # The newest down to the old head, on the fifth page; the six saved files cost no
        # request.
        self.assertEqual(self.pages(), [None, "n2", "n6", "n10", "p2"])
        self.assertEqual(self.saved(), sorted(self.posts))
        self.assertEqual([self.asked(post) for post in ("n1", "n6")], [[], []])
        self.assertEqual([len(self.asked(post)) for post in ("n7", "n12", "x1")], [1, 1, 1])
        self.assertEqual(self.row().heads, {"posts": {"id": "x1", "ts": "2026-07-19"}})
        # Nothing is left behind: the next sync has nothing new.
        self.sync()
        self.assertEqual(self.pages(), [None])

    def test_a_sync_the_site_failed_part_way_keeps_its_head_too(self):
        self.download()
        self.posts = [f"n{number}" for number in range(1, 13)] + self.posts
        self.fail_after = "n8"
        self.assertFalse(self.sync().ok)
        self.assertEqual(self.pages()[:3], [None, "n4", "n8"])
        self.assertEqual((self.row().heads, self.row().cursors),
                         ({"posts": {"id": "p1", "ts": "2025-12-31"}}, {}))
        self.fail_after = None
        self.assertTrue(self.sync().ok)
        self.assertEqual(self.saved(), sorted(self.posts))
        self.assertEqual(self.row().heads["posts"]["id"], "n1")
        self.sync()
        self.assertEqual(self.pages(), [None])

    def test_a_first_download_cut_short_keeps_its_cursor_and_a_head_to_read_down_to(self):
        self.engine.download(["fed:example_user|all"], limit=6)
        self.assertEqual(self.row().heads["posts"]["id"], "p1")
        self.assertEqual(self.row().complete, {})
        self.posts = ["n1", "n2", *self.posts]
        self.requests.clear()
        self.assertTrue(self.download().ok)
        # It goes on from its cursor to the end, then reads the newest down to its head.
        self.assertEqual(self.saved(), sorted(self.posts))
        self.assertEqual(self.row().heads["posts"]["id"], "n1")
        self.assertEqual(self.row().cursors, {})
        self.sync()
        self.assertEqual(self.pages(), [None])


class TimelineCase(EngineCase):
    """The `tml` plugin: one newest-first category, `timeline` (id, time, pinned) in its order,
    each post one picture. A time is a Unix time, a day, or None."""

    def setUp(self):
        super().setUp()
        self.timeline = []
        self.read = []
        self.cut_at = None
        test = self

        async def user(self, ctx, user):
            return user

        async def posts(self, ctx, user):
            for post_id, when, pinned in list(test.timeline):
                if post_id == test.cut_at:
                    raise errors.SiteError("the feed stopped answering")
                test.read.append(post_id)
                yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=when, pinned=pinned)

        listed = route("tml:{user}", newest_first=False)(user)
        listed.category(newest_first=True)(posts)
        source = type("Timeline", (Plugin,), {
            "code": "tml", "name": "Timeline", "domains": ("example.com",), "api": 2,
            "key": "{post}", "filename": "{post}", "user": listed, "posts": posts})
        self.engine = Engine(home=self.home, sources={"tml": source})

    def put(self, *posts):
        """The timeline, from (id, time) pairs; a third item, True, pins the post."""
        self.timeline = [(post[0], post[1], len(post) > 2 and post[2]) for post in posts]

    def download(self):
        return self.engine.download(["tml:example_user|all"], sync=True)

    def sync(self, **options):
        self.read.clear()
        self.requests.clear()
        return self.engine.sync(["tml:example_user"], **options)

    def head(self):
        return fakes.targets(self.home)["tml:example_user"].heads["posts"]

    def saved(self):
        return sorted(key.partition(":")[2] for key in fakes.history(self.home))

    def asked(self, post):
        return [request for request in self.requests
                if str(request.url).split("?")[0] == f"{CDN}/{post}.jpg"]

    def sql(self, statement, *args):
        with sqlite3.connect(self.home / _store.DB) as db:
            db.execute(statement, args)
        db.close()


# The head `h`, and six posts older than it, each strictly older than the one before.
OLD = [(f"o{number}", T0 - 100 * number) for number in range(1, 7)]


class HeadRuleTest(TimelineCase):
    def start(self, *posts):
        """Download `h` (or `posts`) and the old posts: the head is the newest of them."""
        self.put(*(posts or [("h", T0)]), *OLD)
        self.assertTrue(self.download().ok)

    def test_a_sync_stops_after_four_old_posts_and_the_head_moves_to_the_newest(self):
        self.start()
        self.assertEqual(self.head(), {"id": "h", "ts": float(T0)})
        self.put(("n2", T0 + 50), ("n1", T0 + 40), ("h", T0), *OLD)
        report = self.sync()
        self.assertEqual(self.read, ["n2", "n1", "h", "o1", "o2", "o3"])
        self.assertEqual(sorted(path.stem for path in report.targets[0].written), ["n1", "n2"])
        self.assertEqual(self.head(), {"id": "n2", "ts": float(T0 + 50)})

    def test_new_posts_in_the_head_second_are_never_missed(self):
        self.start()
        self.put(("n3", T0), ("n2", T0), ("n1", T0), ("h", T0), *OLD)
        self.sync()
        self.assertEqual(self.read[:4], ["n3", "n2", "n1", "h"])
        self.assertTrue({"h", "n1", "n2", "n3"} <= set(self.saved()))

    def test_new_posts_on_the_head_day_are_never_missed_when_the_site_gives_only_days(self):
        day = datetime.date(2026, 1, 1)
        olds = [(f"o{number}", day - datetime.timedelta(days=number)) for number in range(1, 7)]
        self.put(("h", day), *olds)
        self.download()
        self.assertEqual(self.head(), {"id": "h", "ts": "2026-01-01"})
        self.put(("n2", day), ("n1", day), ("h", day), *olds)
        self.sync()
        self.assertEqual(self.read, ["n2", "n1", "h", "o1", "o2", "o3"])
        self.assertTrue({"n1", "n2"} <= set(self.saved()))

    def test_a_day_against_a_time_compares_days_only(self):
        self.start(("h", T0 + 36_000))
        # A new post that gives only its day, the head's day, is not old.
        self.put(("n1", datetime.date(2026, 1, 2)), ("h", T0 + 36_000), *OLD)
        self.sync()
        self.assertEqual(self.read, ["n1", "h", "o1", "o2", "o3"])
        self.assertIn("n1", self.saved())

    def test_a_deleted_head_ends_the_sync_after_four_older_posts(self):
        self.start()
        self.put(("n1", T0 + 40), *OLD)
        self.sync()
        self.assertEqual(self.read, ["n1", "o1", "o2", "o3", "o4"])
        self.assertEqual(self.head(), {"id": "n1", "ts": float(T0 + 40)})

    def test_an_unmarked_old_pinned_post_on_top_is_never_the_head_and_never_ends_a_sync(self):
        self.start(("pin", T0 - 5000), ("h", T0))
        self.assertEqual(self.head()["id"], "h")
        self.put(("pin", T0 - 5000), ("n1", T0 + 30), ("h", T0), *OLD)
        self.sync()
        self.assertEqual(self.read, ["pin", "n1", "h", "o1", "o2", "o3"])
        self.assertIn("n1", self.saved())
        self.assertEqual(self.head()["id"], "n1")

    def test_three_unmarked_old_pins_on_top_then_new_posts_are_all_read(self):
        self.start()
        pins = [("q3", T0 - 7000), ("q2", T0 - 8000), ("q1", T0 - 9000)]
        self.put(*pins, ("n2", T0 + 50), ("n1", T0 + 40), ("h", T0), *OLD)
        self.sync()
        self.assertEqual(self.read[:5], ["q3", "q2", "q1", "n2", "n1"])
        self.assertTrue({"n1", "n2"} <= set(self.saved()))

    def test_an_old_post_between_new_ones_resets_the_count(self):
        self.start()
        self.put(("n3", T0 + 50), ("i1", T0 - 1), ("i2", T0 - 2), ("i3", T0 - 3), ("n2", T0 + 40),
                 ("n1", T0 + 30), ("h", T0), *OLD)
        self.sync()
        self.assertEqual(self.read[:7], ["n3", "i1", "i2", "i3", "n2", "n1", "h"])
        self.assertTrue({"n1", "n2", "n3"} <= set(self.saved()))

    def test_a_pinned_post_that_is_marked_is_neither_old_nor_the_head(self):
        self.start()
        self.put(("old", T0 - 99_999, True), ("n1", T0 + 30), ("h", T0), *OLD)
        self.sync()
        self.assertEqual(self.read, ["old", "n1", "h", "o1", "o2", "o3"])
        self.assertEqual(self.head()["id"], "n1")

    def test_a_post_with_no_date_is_never_old_by_time(self):
        self.start()
        self.put(("u1", None), ("n1", T0 + 30), ("h", T0), *OLD)
        self.sync()
        self.assertEqual(self.read, ["u1", "n1", "h", "o1", "o2", "o3"])
        self.assertIn("u1", self.saved())

    def test_a_listing_with_no_date_is_read_to_its_end(self):
        self.put(("u2", None), ("u1", None))
        self.download()
        self.assertEqual(self.head(), {"id": "u2", "ts": None})
        self.put(("n1", None), ("u2", None), ("u1", None))
        self.sync()
        self.assertEqual(self.read, ["n1", "u2", "u1"])

    def test_a_sync_the_site_cut_leaves_the_head_and_the_next_one_catches_up(self):
        self.start()
        self.put(("n3", T0 + 50), ("n2", T0 + 40), ("n1", T0 + 30), ("h", T0), *OLD)
        self.cut_at = "n1"
        self.assertFalse(self.sync().ok)
        self.assertEqual(self.head(), {"id": "h", "ts": float(T0)})
        self.assertEqual(fakes.targets(self.home)["tml:example_user"].cursors, {})
        self.cut_at = None
        self.assertTrue(self.sync().ok)
        self.assertEqual(self.read[:4], ["n3", "n2", "n1", "h"])
        # The two files saved before the cut are not asked for again.
        self.assertEqual([self.asked("n3"), self.asked("n2")], [[], []])
        self.assertEqual(len(self.asked("n1")), 1)
        self.assertEqual(self.head()["id"], "n3")

    def test_when_still_stops_after_five_posts_older_than_its_start(self):
        self.put(("p1", T0))
        self.download()
        # No head to go by: the older-than-`--when` rule alone ends the listing.
        self.sql("UPDATE targets SET heads = '{}' WHERE key = ?", "tml:example_user")
        old = [(f"o{number}", T0 - 86_400 * (number + 1)) for number in range(8)]
        self.put(("n1", T0 + 10), *old)
        self.sync(time=Range(since=datetime.date(2026, 1, 2)))
        self.assertEqual(self.read, ["n1", "o0", "o1", "o2", "o3", "o4"])
        self.assertEqual(self.saved(), ["n1", "p1"])

    def test_a_head_that_is_not_an_object_counts_as_no_head(self):
        self.start()
        self.sql("UPDATE targets SET heads = ? WHERE key = ?", '{"posts": "h"}',
                 "tml:example_user")
        self.sync()
        self.assertEqual(self.read, ["h", *(name for name, _ in OLD)])
        self.assertEqual(self.head(), {"id": "h", "ts": float(T0)})


# ---------------------------------------------------------------------------------------------
# Retrying failed files
# ---------------------------------------------------------------------------------------------

class RetryTest(FeedCase):
    def setUp(self):
        super().setUp()
        # p8 is on the third page, which a sync that stops at the head never reads.
        self.routes[f"{CDN}/p8.jpg"] = _net.reply(500)

    def test_a_failed_file_comes_back_on_the_next_run_and_not_after_its_third(self):
        # A failed file fails alone: the target itself ran to its end.
        self.assertTrue(self.download().ok)
        self.assertNotIn("p8", self.saved())
        for attempt in (2, 3, 4):
            self.sync()
            # The failed post's page again, from the cursor of the page it was on, then the
            # newest page for new posts.
            self.assertEqual(self.pages(), ["p4", "p8", None], attempt)
            self.assertTrue(self.asked("p8"), attempt)
            self.assertEqual(self.failures_row()["attempts"], attempt)
        # The first failure and three retries: no more by itself.
        self.sync()
        self.assertEqual((self.pages(), self.asked("p8")), ([None], []))
        # `target retry` marks it retryable again: the next get tries it.
        del self.routes[f"{CDN}/p8.jpg"]
        self.requests.clear()
        self.events.clear()
        self.engine.target_retry(["fed:example_user"])
        report = self.sync()
        self.assertEqual(report.files, 1)
        self.assertIn("p8", self.saved())
        [saved] = [event for event in self.events if isinstance(event, Saved)]
        self.assertEqual((saved.post, saved.retried), ("p8", True))
        with _library.open(self.home) as fl:
            self.assertEqual(fl.status.failures, ())

    def test_a_retry_lists_the_post_again_for_a_fresh_url_never_the_stored_one(self):
        self.download()
        [first] = self.asked("p8")[:1]
        self.assertEqual(dict(first.url.params)["sig"], "s1")
        self.assertEqual(self.failures_row()["url"], f"{CDN}/p8.jpg")
        del self.routes[f"{CDN}/p8.jpg"]
        self.sync()
        # The second listing of the feed is the retry's: its URL is the fresh one.
        self.assertEqual([dict(request.url.params)["sig"] for request in self.asked("p8")],
                         ["s2"])
        self.assertIn("p8", self.saved())
        with _library.open(self.home) as fl:
            self.assertEqual(fl.status.failures, ())

    def test_a_gone_file_is_never_retried(self):
        self.routes[f"{CDN}/p8.jpg"] = _net.reply(404)
        self.download()
        self.sync()
        self.assertEqual((self.pages(), self.asked("p8")), ([None], []))

    def test_a_post_gone_from_its_listing_is_recorded_gone(self):
        self.download()
        self.posts.remove("p8")
        self.sync()
        # From the cursor of its page to the listing's end, without it: it is gone.
        self.assertEqual(self.pages(), ["p4", None])
        self.assertEqual(self.failures_row()["class"], "NotFound")
        with _store.open(self.home) as db:
            self.assertEqual(db.known("fed:p8")[1], _store.GONE)
        self.sync()
        self.assertEqual(self.pages(), [None])

    def test_status_shows_the_last_runs_failures(self):
        self.download()
        with _library.open(self.home) as fl:
            status = fl.status
        self.assertEqual((status.command, status.saved), ("get fed:example_user", 8))
        [failure] = status.failures
        self.assertEqual((failure.target, failure.post, failure.attempts, failure.url),
                         ("fed:example_user", "p8", 1, f"{CDN}/p8.jpg"))
        self.assertIsInstance(failure.error, errors.SiteError)
        self.assertTrue(failure.path.name.startswith("p8"))

    def failures_row(self):
        with _store.open(self.home) as db:
            sql = db._db.connection()
            sql.row_factory = lambda cursor, row: {d[0]: v for d, v in zip(cursor.description, row)}
            return sql.execute("SELECT * FROM failures WHERE key = 'fed:p8'").fetchone()


# ---------------------------------------------------------------------------------------------
# Samples and logs
# ---------------------------------------------------------------------------------------------

# The secrets the fixtures hold; no log, sample, row or message may show one.
SESSION, TOKEN, CSRF, SIGNED = "s3ss10n-v4lue", "b34r3r-t0k3n", "csrf-v4lue", "url-t0k3n"
SECRETS = (SESSION, TOKEN, CSRF, SIGNED)


class SampleTest(EngineCase):
    """A changed answer is kept, scrubbed, in `logs/<run>/samples/`; a login's never is."""

    def changed_source(self, path):
        async def user(self, ctx, user):
            return user

        async def posts(self, ctx, user):
            answer = await ctx.fetch(f"{API}{path}")
            raise Changed("the feed has no items", response=answer)
            yield  # an async generator

        read = route("chg:{user}", newest_first=False)(user)
        read.category(posts)
        return type("Chg", (Plugin,), {"code": "chg", "name": "Chg", "domains": ("example.com",),
                                       "api": 2, "user": read, "posts": posts})

    def run_changed(self, path, body):
        self.routes[f"{API}{path}"] = _net.reply(200, json=body)
        engine = Engine(home=self.home, sources={"chg": self.changed_source(path)},
                        emit=self.events.append)
        return engine.download(["chg:example_user|all"])

    def samples(self):
        return sorted((self.home / _store.LOGS).glob("*/samples/*"))

    def test_a_changed_answer_is_kept_scrubbed_and_named_in_the_hint(self):
        report = self.run_changed("/feed", {"user": "example_user", "csrf_token": CSRF,
                                            "data": {"session": {"id": SESSION}}})
        [sample] = self.samples()
        text = sample.read_text(encoding="utf-8")
        self.assertIn('"user": "example_user"', text)
        self.assertIn('"csrf_token": "…"', text)
        self.assertNotIn(CSRF, text)
        self.assertNotIn(SESSION, text)
        self.assertEqual(text.splitlines()[0], f"{API}/feed")
        self.assertIn(str(sample), report.targets[0].error.hint)

    def test_a_login_answer_is_never_kept(self):
        self.run_changed("/accounts/login", {"session": SESSION})
        self.assertEqual(self.samples(), [])

    def test_logs_older_than_30_days_are_swept_at_a_run(self):
        old, new = self.home / _store.LOGS / "old-run", self.home / _store.LOGS / "crash-new.txt"
        (old / "samples").mkdir(parents=True)
        new.write_text("x")
        month = time.time() - 31 * 86_400
        os.utime(old, (month, month))
        self.run_changed("/feed", {"items": None})
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


class SecretsTest(FeedCase):
    """Every log line, sample, row and message a run produces is grepped for the secrets."""

    def setUp(self):
        super().setUp()
        self.enterContext(ui.scope(dest=self.dest, level="debug"))

        def sign(self, request, cookies):
            return request.replace(headers={**(request.headers or {}),
                                            "Authorization": f"Bearer {TOKEN}"})

        async def info(self, ctx, user):
            answer = await ctx.fetch(f"{API}/info")
            raise Changed(f"no items in {_redacted(answer.url)}", response=answer)
            yield  # an async generator

        self.source = self.feed_source(login=login_cookies("sessionid"), sign=sign, info=info)
        self.engine = Engine(home=self.home, sources={"fed": self.source}, emit=self.events.append)
        self.log_in("fed", sessionid=SESSION)
        self.routes[f"{API}/info"] = _net.reply(200, json={"csrf": CSRF, "auth": TOKEN})
        self.routes[f"{CDN}/p8.jpg"] = _net.reply(503)

    def test_no_secret_reaches_a_log_a_sample_a_row_or_a_message(self):
        self.signed = SIGNED
        self.download()
        self.sync()
        outputs = {"log": self.dest.getvalue()}
        outputs["events"] = "\n".join(repr(event) for event in self.events)
        outputs["messages"] = "\n".join(f"{event.error} {event.error.hint}" for event in self.events
                                        if hasattr(event, "error"))
        for argv in (["status"],):
            out = io.StringIO()
            with redirect_stdout(out):
                flnd_cli.main(["--home", str(self.home), *argv])
            outputs[" ".join(argv)] = out.getvalue()
        for path in sorted((self.home / _store.LOGS).rglob("*")):
            if path.is_file():
                outputs[str(path)] = path.read_text(encoding="utf-8", errors="replace")
        for name in ("flnd.db", "flnd.db-wal"):
            if (self.home / name).exists():
                outputs[name] = (self.home / name).read_bytes().decode("latin-1")
        # The run did produce what is grepped: a sample, and failures in the report.
        self.assertTrue(any("samples" in name for name in outputs), list(outputs))
        self.assertIn("p8", outputs["status"])
        for name, text in outputs.items():
            for secret in SECRETS:
                self.assertNotIn(secret, text, f"{secret!r} in {name}")


def _redacted(url):
    from flnkit.text import redact

    return redact(url)


if __name__ == "__main__":
    unittest.main()
