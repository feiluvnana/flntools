"""Smart sync and the failure report: the stop at nothing new, the gap fill, the automatic retry,
`flnd retry`, `fl.status`, and the samples of changed answers, kept scrubbed.

Every test runs the engine on a temporary home over a fake network and a fake clock
(`EngineCase`). The `Feed` plugin pages its posts by cursor, the way the sites' APIs do: each
page holds `PAGE` posts, newest first, and its cursor is the last post's id, so a cursor still
works after new posts arrive on top. Each listing signs its file URLs afresh (`sig=s<n>`), as a
CDN's signed URLs are, so a test can tell a fresh URL from a stored one.
"""

import asyncio
import datetime
import io
import os
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import fakes
from fakes import CDN, Engine, EngineCase, login_cookies
from test_library import registered

from flnd import _library, errors
from flnd import cli as flnd_cli
from flnd.engine import crawl as job
from flnd.engine import save
from flnd.engine import store as _store
from flnd.engine.events import NOTHING_NEW, RESUMING, Saved, Skipped
from flnd.errors import Changed
from flnd.plugin import File, Plugin, Post, Range, route
from flnd.ui.log import ui
from flnkit import net as _net

API = "https://api.example.com"
# Posts a page of the feed holds.
PAGE = 3
DAY = datetime.date(2026, 1, 1)


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
            sig = f"s{test.listings}" + (f"&token={test.signed}" if test.signed else "")
            async for response in ctx.pages(f"{API}/feed", cursor="next", send="after"):
                for post_id in response.json["ids"]:
                    yield Post(post_id, [File(f"{CDN}/{post_id}.jpg?sig={sig}")], date=DAY)

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
# Nothing new
# ---------------------------------------------------------------------------------------------

class NothingNewTest(FeedCase):
    def test_a_sync_with_nothing_new_makes_one_request(self):
        self.assertTrue(self.download().ok)
        self.assertEqual(self.row().heads, {"posts": "p1"})
        report = self.sync()
        # The head and the post after it are on the first page: one request, no file.
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
        self.assertEqual((report.unchanged, report.skipped, report.saved), (1, 2, 0))

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
        self.assertEqual(self.pages(), [None, "p3", "p6"])

    def test_when_since_stop_leaves_no_stale_cursor_or_gap(self):
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
        self.assertEqual(self.pages(), [None, "p3", "p6"])
        self.assertEqual(self.saved(), sorted(self.posts))
        self.assertEqual(self.row().heads, {"posts": "p1"})

        # `sync` with `--when` on a complete target leaves no fake gap; next sync short-circuits.
        self.requests.clear()
        self.assertTrue(self.engine.sync(["fed:example_user"], time=Range(since=DAY)).ok)
        self.assertEqual(self.row().gaps, {})
        self.assertEqual(self.row().heads, {"posts": "p1"})
        self.assertTrue(self.sync().ok)
        self.assertEqual(self.pages(), [None])


# ---------------------------------------------------------------------------------------------
# The gap fill
# ---------------------------------------------------------------------------------------------

class GapTest(FeedCase):
    def test_a_sync_cut_short_leaves_no_gap_on_the_next(self):
        self.download()
        # Four pages of new posts; the sync is cut after six of them (`--limit`, so the six are
        # whole when it ends).
        self.posts = [f"n{number}" for number in range(1, 13)] + self.posts
        self.engine.download(["fed:example_user|all"], limit=6)
        self.assertEqual(self.saved(), sorted([*self.posts[:6], *self.posts[12:]]))
        self.assertEqual(self.row().gaps, {"posts": ["n6"]})
        # The head stays the old one while a gap is open: the gap lies above it.
        self.assertEqual(self.row().heads, {"posts": "p1"})
        # Two more posts arrive; the site answers again.
        self.posts = ["x1", "x2", *self.posts]
        report = self.sync()
        self.assertTrue(report.ok)
        # The newest down to 5 saved posts in a row (x1, x2 new, n1..n5 saved), then the gap
        # from its cursor down to the old head and the post after it.
        self.assertEqual(self.pages(), [None, "n1", "n4", "n6", "n9", "n12"])
        self.assertEqual(self.saved(), sorted([*self.posts]))
        self.assertEqual((self.row().gaps, self.row().heads), ({}, {"posts": "x1"}))
        self.assertIn(RESUMING, self.events)
        self.assertIn("* fed:example_user: posts: resuming from the last run", self.lines())
        # Nothing is left behind: the next sync has nothing new.
        self.sync()
        self.assertEqual(self.pages(), [None])

    def test_a_sync_the_site_failed_part_way_leaves_no_gap_on_the_next(self):
        self.download()
        self.posts = [f"n{number}" for number in range(1, 13)] + self.posts
        self.fail_after = "n6"
        # n6's file, handed over before the listing failed, waits until it has: it still runs
        # to its end, since the gap's cursor is kept after it.
        failed = threading.Event()
        real_finish, real_file = job.Downloads.finish, save.Saver._file

        async def finish(downloads, error=None):
            if error is not None:
                failed.set()
            return await real_finish(downloads, error)

        async def held(saver, item, *args):
            if item.key == "fed:n6":
                self.assertTrue(await asyncio.to_thread(failed.wait, 30))
            return await real_file(saver, item, *args)

        with mock.patch.object(job.Downloads, "finish", finish), \
                mock.patch.object(save.Saver, "_file", held):
            self.assertFalse(self.sync().ok)
        self.assertIn("n6", self.saved())
        self.assertEqual(self.pages()[:3], [None, "n3", "n6"])
        self.assertEqual(self.row().gaps, {"posts": ["n6"]})
        self.fail_after = None
        self.assertTrue(self.sync().ok)
        self.assertEqual(self.saved(), sorted(self.posts))
        self.assertEqual(self.row().gaps, {})
        self.sync()
        self.assertEqual(self.pages(), [None])

    def test_a_gap_without_a_cursor_reads_down_to_the_head(self):
        listed, cut = [], {"at": None}
        test = self

        async def user(self, ctx, user):
            return user

        async def posts(self, ctx, user):
            for post_id in test.posts:
                if post_id == cut["at"]:
                    raise errors.SiteError("the feed stopped answering")
                listed.append(post_id)
                yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=DAY)

        read = route("lst:{user}", newest_first=False)(user)
        read.category(newest_first=True)(posts)
        source = type("List", (Plugin,), {"code": "lst", "name": "List",
                                          "domains": ("example.com",), "api": 2, "key": "{post}",
                                          "filename": "{post}", "user": read, "posts": posts})
        engine = Engine(home=self.home, sources={"lst": source})
        engine.download(["lst:example_user|all"], sync=True)
        self.posts = [f"n{number}" for number in range(1, 13)] + self.posts
        cut["at"] = "n8"
        self.assertFalse(engine.sync(["lst:example_user"]).ok)
        self.assertEqual(fakes.targets(self.home)["lst:example_user"].gaps, {"posts": [""]})
        cut["at"], listed[:] = None, []
        self.assertTrue(engine.sync(["lst:example_user"]).ok)
        # Past n1..n7, saved, down to the head and the post after it.
        self.assertEqual(listed, [*self.posts[:14]])
        self.assertEqual(len(fakes.history(self.home)), len(self.posts))
        self.assertEqual(fakes.targets(self.home)["lst:example_user"].gaps, {})


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
            self.assertEqual(self.pages(), ["p6", None], attempt)
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
        self.assertEqual(self.pages(), ["p6", None])
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
