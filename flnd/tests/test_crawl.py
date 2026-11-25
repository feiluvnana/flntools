"""The crawl (src/engine/crawl.py): one target, from its route to its history rows; searches.

Run through the engine with tests/example_plugin.py and small plugins (API 2) on
`flnkit.net.fake` and a fake clock. No network, no browser.
"""

import asyncio
import datetime
import errno
import gc
import os
import sqlite3
import threading
import time
import unittest
import warnings
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

import fakes
from example_plugin import API, CDN, FIXTURES, Example
from fakes import Engine, EngineCase, make_plugin

from flnd.engine import _engine as engine_module
from flnd.engine import crawl as job
from flnd.engine import jobs as jobs_module
from flnd.engine import run as _run
from flnd.engine import store as _store
from flnd.engine.crawl import Options
from flnd.engine.events import Skipped
from flnd.errors import (
    Blocked,
    DiskError,
    LoginError,
    NetworkError,
    NotFound,
    PluginError,
    SiteError,
    UsageError,
    disk,
)
from flnd.plugin import Browser, File, Plugin, Post, Target, route
from flnd.ui.log import ui
from flnkit import files as files_module
from flnkit import net as _net

# The keys of a history row, as a run writes it today.
ROW_KEYS = ["at", "category", "date", "estimated", "extracted", "files", "folder", "key",
            "kind", "mtime", "path", "post_id", "size", "state", "target", "unpacked"]


def feed(code, *listings, newest_first=False, **declared):
    """A plugin whose `<code>:<user>` route returns the user; `listings` are its categories."""
    @route(f"{code}:{{user}}", newest_first=newest_first)
    async def user(self, ctx, user):
        return user

    for listing in listings:
        user.category(newest_first=newest_first)(listing)
    attributes = {"code": code, "name": code.upper(), "domains": ("example.com",), "api": 2,
                  "filename": "{date}_{post}{index}", "user": user,
                  **{listing.__name__: listing for listing in listings}, **declared}
    return type(f"Feed_{code}", (Plugin,), attributes)


def lazy(files, calls, name):
    """A lazy post's files: `files`, with `name` noted in `calls` each time they are listed."""
    async def listed():
        calls.append(name)
        return files

    return listed


def author(*found):
    """A plugin whose `aut:<name>` route discovers the targets `found`."""
    class Author(Plugin):
        code, name, domains, api = "aut", "Author", ("aut.example",), 2

        @route("aut:{name}", discovers=True)
        async def author(self, ctx, name):
            for text in found:
                yield Target(text)

    return Author


class JobCase(EngineCase):
    """The engine with `Example`'s API answering from `FIXTURES`."""

    def setUp(self):
        super().setUp()
        for url, answer in FIXTURES.items():
            self.routes[url] = self.json_answer(answer)

    def json_answer(self, answer):
        def respond(request):
            data = answer(dict(request.url.params)) if callable(answer) else answer
            return _net.reply(200, json=data)

        return respond

    def history(self, code=None):
        """The history rows, or only those of the plugin `code`."""
        rows = fakes.history(self.home)
        return rows if code is None else {k: v for k, v in rows.items() if k.startswith(f"{code}:")}

    def sql(self, statement, *args):
        """Run one statement on the home's `flnd.db`, as another tool might."""
        db = sqlite3.connect(self.home / _store.DB, isolation_level=None)
        try:
            db.execute(statement, args)
        finally:
            db.close()

    def fetched(self, url):
        return [request for request in self.requests if str(request.url).split("?")[0] == url]

    def cdn_requests(self):
        return [request for request in self.requests if request.url.host == "cdn.example"]

    def files(self, code="exm", name="example_user"):
        folder = self.target_dir(code, name)
        return sorted(
            path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()
        )


class CategoriesTest(JobCase):
    def test_every_category_todays_names_keys_and_row_format(self):
        self.assertEqual(self.run_engine(Example, targets=["exm:example_user"]), 5)
        self.assertEqual(self.files(), ["albums/Summer_2026/A1.jpg", "posts/2026-01-01_B2def.jpg",
                                        "posts/2026-01-02_C1abc_0.jpg",
                                        "posts/2026-01-02_C1abc_1.mp4",
                                        "profile/avatar_42.jpg"])
        rows = self.history()
        self.assertEqual(sorted(rows), ["exm:A1_0", "exm:B2def_0", "exm:C1abc_0", "exm:C1abc_1",
                                        "exm:profile/avatar_42.jpg"])
        # The first file of a post of several holds their count (fixes #2).
        for key in ("exm:C1abc_0", "exm:C1abc_1", "exm:B2def_0"):
            self.assertEqual(sorted(rows[key]), ROW_KEYS)
        self.assertEqual(rows["exm:C1abc_0"]["files"], 2)
        # The route is called once for its three categories.
        self.assertEqual(len(self.fetched(f"{API}/users/example_user")), 1)

    def test_only_wanted_categories_are_called_and_a_route_none_wanted_is_never_called(self):
        self.run_engine(Example, targets=["exm:example_user"],
                        options=Options(categories=("profile",)))
        self.assertEqual(self.files(), ["profile/avatar_42.jpg"])
        self.assertEqual(self.fetched(f"{API}/users/42/posts") + self.fetched(f"{API}/albums/A1"),
                         [])
        called = []

        class Tagged(Plugin):
            code, name, domains, api = "tgd", "Tagged", ("example.com",), 2
            filename = "{date}_{post}{index}"

            @route("tgd:{user}", newest_first=False)
            async def user(self, ctx, user):
                return user

            @user.category(newest_first=False)
            async def posts(self, ctx, user):
                yield Post(user, [File(f"{CDN}/{user}.jpg")])

            @route("example.com/tags/{tag}", newest_first=False)
            async def tag(self, ctx, tag):
                called.append(tag)
                return tag

            @tag.category(newest_first=False)
            async def tagged(self, ctx, tag):
                yield Post(tag, [File(f"{CDN}/{tag}.jpg")])

        tag_url = ["https://example.com/tags/example_tag"]
        self.requests.clear()
        self.run_engine(Tagged, targets=tag_url, options=Options(categories=("posts",)))
        self.assertEqual((called, self.requests), ([], []))
        self.run_engine(Tagged, targets=tag_url)
        self.assertEqual(called, ["example_tag"])

    def test_a_route_without_categories_files_its_posts_in_the_targets_folder(self):
        self.assertEqual(self.run_engine(Example, targets=["https://www.example.com/p/C1abc/"]), 1)
        self.assertEqual(self.files(name="C1abc"), ["2026-01-02_C1abc.jpg"])


class SavedTest(JobCase):
    def test_a_second_run_lists_no_saved_lazy_post_and_downloads_nothing(self):
        self.run_engine(Example, targets=["exm:example_user"])
        self.requests.clear()
        self.assertEqual(self.run_engine(Example, targets=["exm:example_user"]), 0)
        # The album's photos are a lazy post: saved, so its request isn't made again.
        self.assertEqual((self.fetched(f"{API}/albums/A1"), self.cdn_requests()), ([], []))

    def test_a_lazy_post_is_listed_only_when_needed_and_again_when_a_file_failed(self):
        listed = []

        async def posts(plugin, ctx, user):
            for name, posted in (("old", 1735689600), ("new", 1767312000)):
                yield Post(name, lazy([File(f"{CDN}/{name}.jpg")], listed, name), date=posted)

        options = Options(since=date(2026, 1, 1))
        self.run_engine(feed("lzy", posts), targets=["lzy:example_user"], options=options)
        # The 2025 post is outside --time: skipped before its files are listed.
        self.assertEqual(listed, ["new"])
        self.run_engine(feed("lzy", posts), targets=["lzy:example_user"], options=options)
        self.assertEqual(listed, ["new"])

        async def carousel_posts(plugin, ctx, user):
            yield Post("C1abc", lazy([File(f"{CDN}/c{num}.jpg") for num in range(3)], listed,
                                     "C1abc"))

        carousel = feed("crs", carousel_posts, key="{post}_{num}")
        self.routes[f"{CDN}/c1.jpg"] = _net.reply(403)
        self.run_engine(carousel, targets=["crs:example_user"])
        self.assertEqual(sorted(self.history("crs")), ["crs:C1abc_0", "crs:C1abc_2"])
        del self.routes[f"{CDN}/c1.jpg"]
        # The 403 asked for the post's files once more (B6), so the first run listed it twice.
        before = len(listed)
        self.requests.clear()
        self.run_engine(carousel, targets=["crs:example_user"])
        # The retry lists the post once more, for fresh links, and fetches the failed file alone.
        self.assertEqual(len(listed), before + 1)
        self.assertEqual([str(request.url) for request in self.requests], [f"{CDN}/c1.jpg"])
        self.requests.clear()
        self.run_engine(carousel, targets=["crs:example_user"])
        self.assertEqual((len(listed), self.requests), (before + 1, []))

    def test_a_failed_file_is_retried_directly_under_sync(self):
        async def posts(plugin, ctx, user):
            yield Post("new1", [File(f"{CDN}/new1.jpg")], date="2026-02-01")
            yield Post("old1", [File(f"{CDN}/old1.jpg")], date="2026-01-01")

        self.routes[f"{CDN}/old1.jpg"] = _net.reply(500)
        self.run_engine(feed("fed", posts), targets=["fed:example_user"])
        self.assertIn("fed:new1_0", self.history())
        self.assertNotIn("fed:old1_0", self.history())

        del self.routes[f"{CDN}/old1.jpg"]
        self.requests.clear()
        self.run_engine(feed("fed", posts), targets=["fed:example_user"],
                        options=Options(stop_at_saved=True))
        self.assertIn("fed:old1_0", self.history())
        self.assertEqual([str(r.url) for r in self.requests], [f"{CDN}/old1.jpg"])

    def test_a_file_queued_again_is_downloaded_once(self):
        async def twice(plugin, ctx, user):
            yield Post("1", [File(f"{CDN}/1.jpg")])
            yield Post("1", [File(f"{CDN}/1.jpg")])

        self.assertEqual(self.run_engine(feed("twc", twice), targets=["twc:example_user"]), 1)
        self.assertFalse(any(line.startswith(". skip ") for line in self.lines()), self.lines())

        async def posts(plugin, ctx, user):
            yield Post("1", [File(f"{CDN}/1.jpg")])

        async def reels(plugin, ctx, user):
            yield Post("1", [File(f"{CDN}/1.jpg")])
            yield Post("2", [File(f"{CDN}/2.jpg")])

        both = feed("bth", posts, reels)
        self.requests.clear()
        self.assertEqual(self.run_engine(both, targets=["bth:example_user"]), 2)
        # Both categories name it `1_0` (their folders differ), so the second is skipped as queued.
        self.assertEqual(len(self.fetched(f"{CDN}/1.jpg")), 1)
        self.assertEqual(sorted(self.history("bth")), ["bth:1_0", "bth:2_0"])
        # The second category's folder holds a link to the first one's file.
        self.assertEqual(self.files("bth"), ["posts/1.jpg", "reels/1.jpg", "reels/2.jpg"])
        self.assertTrue((self.target_dir("bth", "example_user") / "reels" / "1.jpg").is_symlink())
        # A later run finds both saved, whichever category lists them.
        self.requests.clear()
        self.assertEqual(self.run_engine(both, targets=["bth:example_user"]), 0)
        self.assertEqual(self.cdn_requests(), [])

        async def same_path(plugin, ctx, user):
            yield Post("1", [File(f"{CDN}/1.jpg")])

        async def other(plugin, ctx, user):
            yield Post("2", [File(f"{CDN}/2.jpg")])

        same = feed("sme", same_path, other, folder="", filename="photo")
        # Two keys, one path: the second file would overwrite the first, so it is skipped as queued.
        self.assertEqual(self.run_engine(same, targets=["sme:example_user"]), 1)
        self.assertEqual(self.files("sme"), ["photo.jpg"])
        self.assertEqual(self.fetched(f"{CDN}/2.jpg"), [])


class FilesTest(JobCase):
    def test_text_files_are_written_whole_and_unkeyed(self):
        class Notes(Plugin):
            code, name, domains, api = "nts", "Notes", ("example.com",), 2
            folder = "{post}"

            @route("nts:{user}", newest_first=False)
            async def user(self, ctx, user):
                yield Post("5930", [File.text("info.md", "# Title\n"),
                                    File(mirrors=[f"{CDN}/a.7z", f"{CDN}/b.7z"],
                                         name="loi-5930.7z")])

        self.assertEqual(self.run_engine(Notes, targets=["nts:example_user"]), 1)
        folder = self.target_dir("nts", "example_user")
        self.assertEqual((folder / "5930" / "info.md").read_bytes(), b"# Title\n")
        self.assertEqual((folder / "5930" / "loi-5930.7z").read_bytes(), b"/a.7z")
        self.assertEqual(sorted(self.history()), ["nts:5930_1"])

    def test_a_url_without_an_extension_is_named_by_its_content(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
        self.routes[f"{CDN}/blob"] = _net.reply(200, content=png)

        class Blob(Plugin):
            code, name, domains, api = "blb", "Blob", ("example.com",), 2
            filename = "{date}_{post}{index}"

            @route("blb:{user}", newest_first=False)
            async def user(self, ctx, user):
                yield Post("9", [File(f"{CDN}/blob")], date=1767312000)

        self.run_engine(Blob, targets=["blb:example_user"])
        self.assertEqual(self.files("blb"), ["2026-01-02_9.png"])

    def test_two_files_of_one_post_with_one_name_get_two_paths(self):
        async def posts(plugin, ctx, user):
            yield Post("p1", [File(f"{CDN}/{part}/cover.jpg", name="cover.jpg") for part in "abc"],
                       date="2026-01-02")

        self.run_engine(feed("cvr", posts), targets=["cvr:example_user"])
        self.assertEqual(self.files("cvr"),
                         ["posts/cover.jpg", "posts/cover_1.jpg", "posts/cover_2.jpg"])

    def test_mtime_is_the_posts_day_else_last_modified(self):
        self.routes[f"{CDN}/avatars/42.jpg"] = _net.reply(
            200, content=b"/avatar", headers={"Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"})
        self.run_engine(Example, targets=["exm:example_user"])
        folder = self.target_dir("exm", "example_user")

        def modified(name):
            return datetime.datetime.fromtimestamp(os.stat(folder / name).st_mtime, datetime.UTC)

        self.assertEqual(modified("posts/2026-01-02_C1abc_0.jpg"),
                         datetime.datetime(2026, 1, 2, 12, tzinfo=datetime.UTC))
        self.assertEqual(modified("profile/avatar_42.jpg"),
                         datetime.datetime(2026, 10, 21, 12, tzinfo=datetime.UTC))
        # No date and no Last-Modified: the download day, which is when it was written.
        self.assertEqual(modified("albums/Summer_2026/A1.jpg").date(),
                         datetime.datetime.now(datetime.UTC).date())

    def test_the_history_row_has_its_path_size_and_time(self):
        self.run_engine(Example, targets=["exm:example_user"],
                        options=Options(categories=("posts",)))
        row = self.history()["exm:C1abc_0"]
        saved = self.target_dir("exm", "example_user") / "posts" / "2026-01-02_C1abc_0.jpg"
        self.assertEqual((row["path"], row["size"]), (str(saved), len(b"/C1abc_0.jpg")))
        written = datetime.datetime.fromisoformat(row["at"])
        self.assertEqual(written.utcoffset(), datetime.timedelta(0))
        self.assertEqual(written.microsecond, 0)
        self.assertLess(abs(datetime.datetime.now(datetime.UTC) - written),
                        datetime.timedelta(minutes=1))


class TimeTest(JobCase):
    """`--time`, `--until` and the `newest_first` early stop."""

    def saved(self, plugin, options):
        """The ids of the posts a download of `<code>:example_user` saved."""
        self.run_engine(plugin, targets=[f"{plugin.code}:example_user"], options=options)
        return sorted(key.split(":", 1)[1] for key in self.history(plugin.code))

    def dated_feed(self, code, newest_first, finished):
        # Three old pinned posts on top, then the feed newest first.
        days = [2020, 2021, 2022, 2026, 2026, 2025, 2025, 2025, 2025, 2025, 2025, 2024]

        async def posts(plugin, ctx, user):
            try:
                for number, year in enumerate(days):
                    yield Post(f"{number}_{year}", [File(f"{CDN}/{number}.jpg")],
                               date=datetime.date(year, 6, 1), pinned=number < 3)
            finally:
                finished.append(True)

        return feed(code, posts, newest_first=newest_first, key="{post}")

    def test_a_newest_first_feed_stops_after_5_older_posts_and_any_other_is_read_to_its_end(self):
        since = Options(since=date(2026, 1, 1))
        finished = []
        # The pinned posts don't stop it; five older in a row do, before the 2024 one.
        self.assertEqual(self.saved(self.dated_feed("fed", True, finished), since),
                         ["3_2026", "4_2026"])
        self.assertEqual(finished, [True])
        self.assertEqual(self.fetched(f"{CDN}/11.jpg"), [])
        finished.clear()
        self.assertEqual(self.saved(self.dated_feed("fe2", False, finished), since),
                         ["3_2026", "4_2026"])
        self.assertEqual(finished, [True])

    def test_until_and_undated_posts(self):
        class Dated(Plugin):
            code, name, domains, api, key = "dtd", "Dated", ("example.com",), 2, "{post}"

            @route("dtd:{user}", newest_first=False)
            async def user(self, ctx, user):
                yield Post("late", [File(f"{CDN}/late.jpg")], date="2026-07-01")
                yield Post("undated", [File(f"{CDN}/undated.jpg")])
                yield Post("in", [File(f"{CDN}/in.jpg")],
                           date=datetime.datetime(2026, 3, 1, 23, 59))

        self.assertEqual(self.saved(Dated, Options(until=date(2026, 6, 30))), ["in", "undated"])


def dated(post_id):
    """A later post is newer: `pN` is N days before 2026-03-01, any other id comes after it."""
    if post_id.startswith("p"):
        return date(2026, 3, 1) - datetime.timedelta(days=int(post_id[1:]))
    return date(2026, 3, 2)


class SyncStopTest(JobCase):
    """`sync` stops a `newest_first` category at its head, once its listing has reached its
    end (`tests/test_smart_sync.py` covers the rule in full: times, pins, cuts)."""

    def setUp(self):
        super().setUp()
        self.ids = [f"p{number}" for number in range(8)]
        self.listed, self.finished, self.fail_at = [], [], []
        # Files of a post that has other than its one picture, by id.
        self.post_files: dict[str, list[File]] = {}

        async def posts(plugin, ctx, user):
            try:
                for post_id in list(self.ids):
                    if post_id in self.fail_at:
                        raise SiteError("cut")
                    self.listed.append(post_id)
                    files = self.post_files.get(post_id, [File(f"{CDN}/{post_id}.jpg")])
                    yield Post(post_id, files, date=dated(post_id))
            finally:
                self.finished.append(True)

        self.engine = Engine(home=self.home,
                             sources={"fed": feed("fed", posts, newest_first=True, key="{post}")})

    def record(self):
        row = fakes.targets(self.home).get("fed:example_user")
        return (row.complete or None) if row is not None else None

    def forget_record(self):
        self.sql("UPDATE targets SET complete = '{}' WHERE key = ?", "fed:example_user")

    def sync(self):
        self.listed.clear()
        return self.engine.sync(["fed:example_user"])

    def test_files_queued_by_an_earlier_category_are_not_caught_up(self):
        """fixes #4: a second feed listing the first one's 6 new posts goes on past them."""
        async def posts(plugin, ctx, user):
            for post_id in self.ids:
                yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=dated(post_id))

        async def media(plugin, ctx, user):
            # The new posts `posts` queued, then one only `media` has, then the saved ones.
            for post_id in [*self.ids[:6], *self.deeper, *self.ids[6:]]:
                self.listed.append(post_id)
                yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=dated(post_id))

        self.deeper = []
        engine = Engine(home=self.home,
                        sources={"two": feed("two", posts, media, newest_first=True, key="{post}")})
        self.assertTrue(engine.download(["two:example_user|all"]).ok)
        self.ids = [f"n{number}" for number in range(6)] + self.ids
        self.deeper = ["m1"]
        self.listed.clear()
        report = engine.sync(["two:example_user"])
        self.assertIn("m1", self.listed)
        self.assertIn("2026-03-02_m1.jpg", [path.name for path in report.targets[0].written])

    def test_posts_with_no_files_or_only_text_do_not_end_the_sync_before_its_head(self):
        """A reply with nothing to save, above the head, neither stops the sync nor counts as a
        saved post (twt replies)."""
        self.assertTrue(self.engine.download(["fed:example_user|all"]).ok)
        text = File.text("note.txt", "a note")
        self.ids = ["none1", "text1", "none2", *self.ids]
        self.post_files = {"none1": [], "none2": [], "text1": [text]}
        self.sync()
        self.assertEqual(self.listed, ["none1", "text1", "none2", "p0", "p1", "p2", "p3"])
        self.assertEqual(self.finished[-1], True)

    def test_without_a_record_or_after_an_interrupted_listing_sync_lists_everything(self):
        self.engine.download(["fed:example_user|all"])
        self.forget_record()
        self.listed.clear()
        self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, self.ids)
        # The listing reached its end, so it is recorded now, and the next sync stops.
        self.assertIsNotNone(self.record())
        self.sync()
        self.assertEqual(self.listed, self.ids[:4])

        self.forget_record()
        self.fail_at = ["p6"]
        self.assertFalse(self.engine.download(["fed:example_user|all"]).ok)
        self.assertIsNone(self.record())
        self.fail_at, self.listed = [], []
        self.engine.sync(["fed:example_user"])
        # The first 6 were saved, but nothing says the feed was ever read to its end.
        self.assertEqual(self.listed, self.ids)
        self.assertIsNotNone(self.record())

    def test_download_stops_at_the_head_by_default_and_again_lists_all(self):
        self.engine.download(["fed:example_user|all"])
        self.listed.clear()
        report = self.engine.download(["fed:example_user|all"])
        self.assertEqual(self.listed, self.ids[:4])
        self.assertEqual((report.files, report.targets[0].skipped), (0, 4))
        self.listed.clear()
        report_again = self.engine.download(["fed:example_user|all"], again=True)
        self.assertEqual(self.listed, self.ids)
        self.assertEqual((report_again.files, report_again.targets[0].skipped), (0, 8))

    def test_a_feed_that_is_not_newest_first_is_never_stopped(self):
        listed = []

        async def posts(plugin, ctx, user):
            for number in range(8):
                listed.append(number)
                yield Post(f"q{number}", [File(f"{CDN}/q{number}.jpg")])

        engine = Engine(home=self.home, sources={"pln": feed("pln", posts)})
        engine.download(["pln:example_user|all"])
        listed.clear()
        engine.sync(["pln:example_user"])
        self.assertEqual(len(listed), 8)


class AsyncTest(JobCase):
    def test_routes_and_categories_run_on_the_runs_loop_with_no_handover_thread(self):
        threads = []

        class Chats(Plugin):
            code, name, domains, api = "cht", "Chats", ("example.com",), 2
            filename = "{date}_{post}{index}"
            limits = {"cdn.example": 1}

            @route("cht:{chat}")
            async def chat(self, ctx, chat):
                threads.append(threading.current_thread().name)
                return chat

            @chat.category(newest_first=True)
            async def photo(self, ctx, chat):
                for number in range(4):
                    threads.append(threading.current_thread().name)
                    yield Post(str(number), [File(f"{CDN}/{chat}/{number}.jpg")],
                               date=1767312000 - number)

        self.assertEqual(self.run_engine(Chats, targets=["cht:examplechannel"]), 4)
        # The run's one loop: this thread's, outside a `Run`.
        self.assertEqual(set(threads), {threading.current_thread().name})
        self.assertFalse(
            [thread for thread in threading.enumerate() if thread.name.endswith("-saves")]
        )


class FailureTest(JobCase):
    def test_a_category_that_raises_fails_the_target_as_a_plugin_error(self):
        async def posts(plugin, ctx, user):
            yield Post("1", [File(f"{CDN}/1.jpg")])
            raise RuntimeError("the feed broke")

        broken = feed("brk", posts)
        self.run_engine(broken, targets=["brk:example_user"])
        # A bug in a listing is a PluginError: its message names it, its hint says what to do.
        self.assertEqual(self.failures(),
                         ["brk:example_user: unexpected RuntimeError: the feed broke"])
        self.assertEqual(self.events[-1].error.hint, "this is a bug; run with -v and report it")
        report = self.report(broken, targets=["brk:example_user"])
        self.assertEqual(report.failed, (Target("brk:example_user", kind="user"),))
        self.assertIsInstance(report.targets[0].error, PluginError)
        self.assertIsInstance(report.targets[0].error.__cause__, RuntimeError)

    def test_a_site_error_fails_only_its_target_and_a_login_error_stops_the_source(self):
        class Errors(Plugin):
            code, name, domains, api = "err", "Errors", ("example.com",), 2
            filename = "{date}_{post}{index}"
            folder = ""
            # One target at a time, so the one after the refused login hasn't started.
            browser = Browser.ANY

            @route("err:{user}", newest_first=False)
            async def user(self, ctx, user):
                if user == "gone":
                    raise NotFound("HTTP 404 for https://example.com/gone", status=404)
                if user == "locked":
                    raise LoginError("err: the saved login expired", hint="run flnd auth login err")
                return user

            @user.category(newest_first=False)
            async def posts(self, ctx, user):
                if user == "cut":
                    raise SiteError("HTTP 500 for https://example.com/cut", status=500)
                yield Post(user, [File(f"{CDN}/{user}.jpg")])

        names = ["gone", "cut", "good", "locked", "last"]
        report = self.report(Errors, targets=[f"err:{name}" for name in names])
        self.assertEqual(report.files, 1)
        self.assertEqual([target.value for target in report.failed],
                         ["gone", "cut", "locked", "last"])
        self.assertEqual(self.failures(), ["err:gone: HTTP 404 for https://example.com/gone",
                                           "err:cut: HTTP 500 for https://example.com/cut",
                                           "err:locked: err: the saved login expired",
                                           "err:last: err: the saved login expired"])
        # A site's refusal fails its own target; the targets after it still run.
        self.assertIn(("err:good", "posts"), self.listed())
        self.assertEqual(self.files("err", "good"), ["good.jpg"])
        # A refused login stops the source: the target after it fails with the same error, unasked.
        last = report.targets[-1]
        self.assertIs(last.error, report.targets[-2].error)
        self.assertIsInstance(last.error, LoginError)
        self.assertEqual(self.files("err", "last"), [])

    def test_non_retryable_blocked_stops_remaining_targets_on_same_account(self):
        class BotStop(Plugin):
            code, name, domains, api = "bsp", "BotStop", ("example.com",), 2
            filename = "{date}_{post}{index}"
            folder = ""
            browser = Browser.ANY

            @route("bsp:{user}", newest_first=False)
            async def user(self, ctx, user):
                if user == "flagged":
                    raise Blocked("bsp: automated activity detected", retryable=False)
                return user

            @user.category(newest_first=False)
            async def posts(self, ctx, user):
                yield Post(user, [File(f"{CDN}/{user}.jpg")])

        report = self.report(BotStop, targets=["bsp:flagged", "bsp:next"])
        self.assertEqual([t.value for t in report.failed], ["flagged", "next"])
        self.assertIs(report.targets[1].error, report.targets[0].error)
        self.assertIsInstance(report.targets[1].error, Blocked)
        self.assertEqual(self.listed(), set())


class Cut:
    """A body that sends `data`, then drops the connection; or, given `gate`, waits on it after the
    first chunk. An async iterable, so its wait holds up only this transfer, not the loop."""

    def __init__(self, data, *, gate=None, sent=None):
        self.data, self.gate, self.sent = data, gate, sent

    async def __aiter__(self):
        yield self.data[:5]
        if self.sent is not None:
            self.sent.set()
        if self.gate is None:
            raise ConnectionResetError("connection reset")
        await asyncio.to_thread(self.gate.wait, 5)
        yield self.data[5:7]
        yield self.data[7:]


class RefreshTest(JobCase):
    """B6: a 403 or 410 on a file of a lazy post asks its `files` once more and resumes
    with the fresh link."""

    BODY = b"0123456789"
    PHOTO = f"{CDN}/A1/1.jpg"
    ALBUMS = Options(categories=("albums",))

    def setUp(self):
        super().setUp()
        # Each listing of the album signs its photo's link anew, as Instagram's `oe=` links are.
        self.listings = 0

        def album(request):
            self.listings += 1
            return _net.reply(200, json={"urls": [f"{self.PHOTO}?sig={self.listings}"]})

        self.routes[f"{API}/albums/A1"] = album

    def cdn(self, fresh):
        """The photo's CDN: the first link sends half the file, then the connection drops and the
        link expires."""
        def photo(request):
            if request.url.params["sig"] == "1":
                range_header = request.headers.get("range", "")
                if range_header and not range_header.startswith("bytes=0-"):
                    return _net.reply(403)
                headers = {"ETag": '"p1"', "Accept-Ranges": "bytes", "Content-Length": "10"}
                return _net.reply(200, headers=headers, chunks=Cut(self.BODY))
            return fresh(request)

        self.routes[self.PHOTO] = photo

    def test_an_expired_link_calls_lazy_files_once_more_and_resumes(self):
        def fresh(request):
            start = int(request.headers["range"].removeprefix("bytes=").split("-")[0])
            return _net.reply(206, content=self.BODY[start:], headers={
                "ETag": '"p1"', "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-9/10"})

        self.cdn(fresh)
        self.run_engine(Example, targets=["exm:example_user"], options=self.ALBUMS)
        photo = self.target_dir("exm", "example_user") / "albums/Summer_2026/A1.jpg"
        self.assertEqual(photo.read_bytes(), self.BODY)
        self.assertEqual(self.listings, 2)
        last = self.fetched(self.PHOTO)[-1]
        self.assertEqual(last.url.params["sig"], "2")
        # The second link resumes where the first stopped, checked against the same file.
        self.assertTrue(last.headers["range"].startswith("bytes=5-"), last.headers["range"])
        self.assertIn("exm:A1_0", self.history())
        album_dir = self.target_dir("exm", "example_user") / "albums/Summer_2026"
        self.assertEqual([path.name for path in album_dir.iterdir()], ["A1.jpg"])

    def test_once_per_file_never_in_a_loop(self):
        self.cdn(lambda request: _net.reply(410))
        self.run_engine(Example, targets=["exm:example_user"], options=self.ALBUMS)
        self.assertEqual(self.listings, 2)
        self.assertIn("A1.jpg: GET https://cdn.example/A1/1.jpg: HTTP 410", self.failures())
        self.assertNotIn("exm:A1_0", self.history())

    def test_a_first_ctrl_c_during_a_refresh_is_no_fresh_link_and_the_target_stops(self):
        """A rate-limit wait inside `files` that the first Ctrl-C cuts short is the graceful
        stop, not a failure."""
        stop = _run.Stop()
        self.cdn(lambda request: _net.reply(500))

        def album(request):
            self.listings += 1
            if self.listings > 1:
                stop.soon.set()      # the first Ctrl-C, during the refresh's wait
                raise KeyboardInterrupt
            return _net.reply(200, json={"urls": [f"{self.PHOTO}?sig=1"]})

        self.routes[f"{API}/albums/A1"] = album
        report = self.report(Example, targets=["exm:example_user"], options=self.ALBUMS, stop=stop)
        self.assertEqual(str(report.targets[0].error), job.STOPPED)
        self.assertEqual(self.listings, 2)

    def test_a_post_with_its_files_listed_is_not_refreshed(self):
        self.routes[f"{CDN}/C1abc_0.jpg"] = _net.reply(403)
        self.run_engine(Example, targets=["exm:example_user"],
                        options=Options(categories=("posts",)))
        self.assertEqual(len(self.fetched(f"{CDN}/C1abc_0.jpg")), 1)
        self.assertEqual(len(self.fetched(f"{API}/users/42/posts")), 2)
        self.assertIn("2026-01-02_C1abc_0.jpg: GET https://cdn.example/C1abc_0.jpg: HTTP 403",
                      self.failures())


class CtrlCTest(JobCase):
    """D8: the first Ctrl-C stops listing and lets running downloads finish; the second stops
    them now.

    The run goes on a thread of its own, as the engine's groups do, and the test thread plays the
    person, setting the stop events as the SIGINT handler does.
    """

    def setUp(self):
        super().setUp()
        self.stop = _run.Stop()

    def start(self, source, text):
        """Run `text` on a thread; returns the thread and a dict that gets its result or error."""
        outcome = {}

        def run():
            try:
                outcome["report"] = self.report(source, targets=[text], stop=self.stop)
            except BaseException as error:  # the test reads it: a second Ctrl-C's KeyboardInterrupt
                outcome["error"] = error

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        return thread, outcome

    def until(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.005)

    def test_the_first_ctrl_c_finishes_running_files_and_closes_the_listing(self):
        listed = []
        closed, started, release = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        async def posts(plugin, ctx, user):
            try:
                for number in range(1, 100):
                    listed.append(number)
                    # Dated, so each post is queued as it is listed, not held for its date.
                    yield Post(str(number), [File(f"{CDN}/{number}.7z", name=f"{number}.7z")],
                               date=datetime.date(2026, 1, 1))
            finally:
                closed.set()

        # One download at a time: post 1's runs, posts 2 to 4 wait in the queue (4 times as
        # many as run, with the target's places held to 1 below).
        slow = feed("slw", posts, folder="", limits={"cdn.example": 1})

        async def first(request):
            started.set()
            await asyncio.to_thread(release.wait, 5)
            return _net.reply(200, content=b"one")

        self.routes[f"{CDN}/1.7z"] = first
        # A target's places follow the most a host's cap grows to, which would queue 128 files.
        self.enterContext(mock.patch.object(jobs_module, "PLACES", 1))
        thread, outcome = self.start(slow, "slw:example_user")
        # Post 5 waits for room in the queue: 1 is downloading and 2 to 4 are queued.
        self.until(lambda: started.is_set() and len(listed) == 5)
        self.stop.soon.set()
        self.until(closed.is_set)
        finishing = "stopping: 1 file finishing (Ctrl-C again to stop now)"
        # Said once, above the run's screen.
        self.until(lambda: f"slw:example_user: {finishing}" in self.dest.getvalue())
        release.set()
        thread.join(10)
        report = outcome["report"]
        self.assertEqual(str(report.targets[0].error), job.STOPPED)
        # An API 2 name with its own extension is kept as it is.
        self.assertEqual(self.files("slw"), ["1.7z"])
        self.assertIn("slw:1_0", self.history())
        # Posts 2 to 4 were queued but not started, so they were dropped; nothing past post 5 was
        # listed.
        self.assertEqual(self.fetched(f"{CDN}/2.7z"), [])
        self.assertEqual(listed, [1, 2, 3, 4, 5])

    def test_the_second_ctrl_c_stops_now_and_keeps_the_part(self):
        sent, gate = threading.Event(), threading.Event()

        async def posts(plugin, ctx, user):
            yield Post("1", [File(f"{CDN}/big.7z", name="big.7z")])

        big_feed = feed("big", posts, folder="")
        body = b"0123456789"

        def big(request):
            range_header = request.headers.get("range", "")
            if range_header and not range_header.startswith("bytes=0-"):
                start = int(range_header.removeprefix("bytes=").split("-")[0])
                return _net.reply(206, content=body[start:], headers={
                    "ETag": '"b1"', "Content-Range": f"bytes {start}-9/10"})
            return _net.reply(200, headers={"ETag": '"b1"', "Content-Length": "10"},
                              chunks=Cut(body, gate=gate, sent=sent))

        self.routes[f"{CDN}/big.7z"] = big
        self.addCleanup(gate.set)
        thread, outcome = self.start(big_feed, "big:example_user")
        self.until(sent.is_set)
        self.stop.soon.set()
        self.stop.now.set()
        # The next chunk arrives and isn't written: the download stops before it, keeping the first.
        gate.set()
        thread.join(10)
        self.assertIsInstance(outcome.get("error"), KeyboardInterrupt)
        folder = self.target_dir("big", "example_user")
        # The part, and beside it the ETag its bytes came under.
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ["big.7z.part", "big.7z.part.json"])

        # The next run resumes the part, sending the ETag so a changed file comes whole.
        self.stop.soon.clear()
        self.stop.now.clear()
        self.run_engine(big_feed, targets=["big:example_user"])
        self.assertEqual((folder / "big.7z").read_bytes(), body)
        last = self.fetched(f"{CDN}/big.7z")[-1]
        self.assertTrue(last.headers["range"].startswith("bytes=5-"))
        self.assertEqual(last.headers["if-range"], '"b1"')
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["big.7z"])


class HeadTest(JobCase):
    """A post a plugin yields in parts (TikTok's photos, all of one id) is one post to a sync."""

    def test_parts_of_one_post_count_once(self):
        listed = []

        async def posts(plugin, ctx, user):
            for post_id in ("c1", "c1", "c1", "p3"):
                listed.append(post_id)
                yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=datetime.date(2026, 1, 3))

        engine = Engine(home=self.home, sources={"hed": feed(
            "hed", posts, newest_first=True, key="{post}", filename="{post}_{num}")})
        report = engine.download(["hed:example_user|all"], limit=1)
        self.assertEqual(listed, ["c1", "c1", "c1", "p3"])
        self.assertEqual(len(report.targets[0].written), 1)


class RetryOrderTest(JobCase):
    def test_no_category_is_retried_in_the_routes_tab_before_the_first_one(self):
        # fbk's `posts` reads the page its route left open: a failure of a later category is
        # listed again right before that category, never before the first.
        called = []

        async def posts(plugin, ctx, user):
            called.append("posts")
            yield Post("p1", [File(f"{CDN}/p1.jpg")], date=datetime.date(2026, 1, 2))

        async def photos(plugin, ctx, user):
            called.append("photos")
            yield Post("f1", [File(f"{CDN}/f1.jpg")], date=datetime.date(2026, 1, 2))

        self.routes[f"{CDN}/f1.jpg"] = _net.reply(403)
        tabs = feed("tab", posts, photos, newest_first=True, key="{post}", filename="{post}_{num}")
        engine = Engine(home=self.home, sources={"tab": tabs})
        engine.download(["tab:example_user"])
        called.clear()
        del self.routes[f"{CDN}/f1.jpg"]
        engine.download(["tab:example_user"])
        self.assertEqual(called, ["posts", "photos", "photos"])
        self.assertIn("tab:f1", self.history())


class OnDiskTest(JobCase):
    def test_a_file_on_disk_whose_key_has_no_row_gets_its_row_and_is_skipped(self):
        # As after `target clean` without `--media`: the file stays, its row goes.
        source = make_plugin("aaa", files=[File(f"{CDN}/a.jpg", name="a.jpg")])
        self.run_engine(source, targets=["aaa:example_user"])
        self.sql("DELETE FROM files")
        self.requests.clear()
        self.assertEqual(self.run_engine(source, targets=["aaa:example_user"]), 0)
        self.assertEqual(self.fetched(f"{CDN}/a.jpg"), [])
        row = self.history()["aaa:a.jpg"]
        self.assertEqual((row["size"], row["post_id"]),
                         ((self.target_dir("aaa", "example_user") / "a.jpg").stat().st_size,
                          "a.jpg"))
        self.assertEqual([event.reason for event in self.events if isinstance(event, Skipped)],
                         ["saved"])


class LazyPostTest(JobCase):
    def test_a_lazy_post_the_site_lost_fails_alone_and_is_never_asked_again(self):
        asked = []

        def files_of(post_id):
            async def listed():
                asked.append(post_id)
                if post_id == "gone":
                    raise NotFound("the post is gone")
                return [File(f"{CDN}/{post_id}.jpg")]
            return listed

        async def posts(plugin, ctx, user):
            for post_id in ("gone", "here"):
                yield Post(post_id, files_of(post_id), date=datetime.date(2026, 1, 2))

        lost = feed("lzy", posts, newest_first=True, key="{post}", filename="{post}_{num}")
        engine = Engine(home=self.home, sources={"lzy": lost}, emit=self.events.append)
        report = engine.download(["lzy:example_user"])
        self.assertIsNone(report.targets[0].error)
        self.assertEqual([path.name for path in report.targets[0].written], ["here_0.jpg"])
        self.assertEqual(self.failures(), ["lzy:example_user: the post is gone"])
        asked.clear()
        engine.download(["lzy:example_user"])
        self.assertEqual(asked, [])

    def test_a_lazy_post_network_error_fails_alone_and_blocked_still_stops(self):
        def files_of(post_id):
            async def listed():
                if post_id == "net":
                    raise NetworkError("could not open")
                if post_id == "blk":
                    raise Blocked("rate limited", wait=30)
                return [File(f"{CDN}/{post_id}.jpg")]
            return listed

        async def posts(plugin, ctx, user):
            yield Post("net", files_of("net"), date=datetime.date(2026, 1, 2))
            yield Post("here", files_of("here"), date=datetime.date(2026, 1, 2))

        source = feed("lzn", posts, newest_first=True, key="{post}", filename="{post}_{num}")
        engine = Engine(home=self.home, sources={"lzn": source}, emit=self.events.append)
        report = engine.download(["lzn:example_user"])
        self.assertIsNone(report.targets[0].error)
        self.assertEqual([path.name for path in report.targets[0].written], ["here_0.jpg"])
        self.assertEqual(self.failures(), ["lzn:example_user: could not open"])

        async def blocked_posts(plugin, ctx, user):
            yield Post("blk", files_of("blk"), date=datetime.date(2026, 1, 2))

        blocked_src = feed("lzb", blocked_posts, newest_first=True, key="{post}",
                           filename="{post}_{num}")
        blocked_report = Engine(home=self.home, sources={"lzb": blocked_src}).download(
            ["lzb:example_user"])
        self.assertIsInstance(blocked_report.targets[0].error, Blocked)


class AskedTest(JobCase):
    def test_targets_listing_at_once_keep_every_post_their_date_hook_had_no_day_for(self):
        # One cache a plugin, shared by its targets: neither's save drops the other's posts.
        async def posts(plugin, ctx, user):
            yield Post(f"{user}-p", [File(f"{CDN}/{user}.jpg")])

        async def undated(plugin, ctx, post):
            return None

        engine = Engine(home=self.home, sources={"und": feed(
            "und", posts, newest_first=True, key="{post}", date=undated)})
        engine.download(["und:a", "und:b"], sync=True)
        engine.sync()
        with _store.open(self.home) as db:
            self.assertEqual(sorted(db.state("und")[job.ASKED_KEY]), ["a-p", "b-p"])


class UnkeyedLazyPostTest(JobCase):
    """A lazy post keyed by its files' paths (LOIBUS's archives) isn't listed again once saved."""

    def test_its_files_function_runs_once(self):
        calls = []

        async def archive(plugin, ctx, user):
            yield Post("5930", lazy([File(f"{CDN}/a.7z", name="a.7z")], calls, user))

        engine = Engine(home=self.home, sources={"pth": feed("pth", archive, key="{path}")})
        engine.download(["pth:example_user|all"])
        engine.download(["pth:example_user|all"])
        self.assertEqual(calls, ["example_user"])
        # `--again` asks again.
        engine.download(["pth:example_user|all"], again=True)
        self.assertEqual(len(calls), 2)


class ResumeTest(JobCase):
    """An interrupted listing goes on from its saved cursor, then reads the newest posts."""

    def setUp(self):
        super().setUp()
        # Pages by cursor: None is the newest; each page holds its posts and the next cursor.
        self.feed: dict[str | None, tuple[list[str], str | None]] = {
            None: (["p1", "p2", "p3", "p4", "p5"], "c1"),
            "c1": (["p6", "p7"], "c2"),
            "c2": (["p8", "p9"], None),
        }
        self.cursors_fetched: list[str | None] = []
        self.fail_on: str | None = None
        self.refuse = False

        async def posts(plugin, ctx, user):
            # `ctx.pages` sends the cursor as `cursor=`, and the engine resumes it.
            async for page in ctx.pages(f"{API}/feed", cursor="next"):
                for post_id in page.json["ids"]:
                    yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")],
                               date=dated(post_id))

        def answer(request):
            cursor = dict(request.url.params).get("cursor")
            self.cursors_fetched.append(cursor)
            if cursor is not None and self.refuse:
                return _net.reply(400, json={"error": "stale cursor"})
            if self.fail_on is not None and cursor == self.fail_on:
                return _net.reply(500, json={"error": "connection reset"})
            ids, following = self.feed[cursor]
            return _net.reply(200, json={"ids": ids, "next": following})

        self.routes[f"{API}/feed"] = answer
        self.engine = Engine(home=self.home,
                             sources={"pgd": feed("pgd", posts, newest_first=True, key="{post}")})

    def row(self):
        return fakes.targets(self.home)["pgd:example_user"]

    def interrupted(self):
        self.fail_on = "c2"
        self.assertFalse(self.engine.download(["pgd:example_user|all"]).ok)
        self.assertEqual(self.row().cursors, {"posts": "c2"})
        self.assertEqual(self.row().complete, {})
        self.fail_on, self.cursors_fetched = None, []

    def test_the_next_run_starts_at_the_saved_cursor_then_reads_the_newest(self):
        self.interrupted()
        # Two new posts arrived meanwhile, above the ones the first run saved.
        self.feed[None] = (["n1", "n2"], "c0")
        self.feed["c0"] = (["p1", "p2", "p3", "p4", "p5"], "c1")
        report = self.engine.download(["pgd:example_user|all"])
        self.assertTrue(report.ok)
        # c2 to the end first, then the newest. Which of p1..p7 the failed run saved depends on
        # which downloads had started, so only the posts this run must save are checked.
        self.assertEqual(self.cursors_fetched[:3], ["c2", None, "c0"])
        folder = self.target_dir("pgd", "example_user")
        saved = {path.stem.split("_")[-1] for path in folder.rglob("*.jpg")}
        self.assertLessEqual({"n1", "n2", "p8", "p9"}, saved)
        self.assertEqual(self.row().cursors, {})
        self.assertIn("posts", self.row().complete)

    def test_a_cursor_kept_for_a_listing_read_to_its_end_is_dropped_by_a_sync(self):
        self.interrupted()
        self.fail_on = "c2-unused"
        self.engine.download(["pgd:example_user|all"])
        self.feed[None] = (["n1", "n2"], "c0")
        self.feed["c0"] = (["p1", "p2", "p3", "p4", "p5"], "c1")
        # An earlier flnd kept a cut-short sync's cursor as the listing's cursor.
        self.sql("UPDATE targets SET cursors = ? WHERE key = ?", '{"posts":"c2"}',
                 "pgd:example_user")
        self.cursors_fetched = []
        self.engine.download(["pgd:example_user|all"])
        # The newest down to the head (the first post the last full read saw), and no cursor.
        self.assertEqual(self.cursors_fetched, [None, "c0"])
        self.assertEqual(self.row().cursors, {})

    def test_a_refused_cursor_is_dropped_and_the_listing_starts_over(self):
        self.interrupted()
        self.refuse = True
        self.feed = {None: (["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9"], None)}
        report = self.engine.download(["pgd:example_user|all"])
        self.assertTrue(report.ok)
        self.assertEqual(self.cursors_fetched, ["c2", None])
        self.assertIn("* pgd:example_user: posts: the cursor an interrupted run saved was refused; "
                      "listing from the newest", self.lines())
        self.assertEqual(self.row().cursors, {})

    def test_again_ignores_saved_cursors_and_complete_listing_from_newest(self):
        self.interrupted()
        report = self.engine.download(["pgd:example_user|all"], again=True)
        self.assertTrue(report.ok)
        self.assertEqual(self.cursors_fetched, [None, "c1", "c2"])
        self.assertEqual(self.row().cursors, {})
        self.assertIn("posts", self.row().complete)
        self.cursors_fetched.clear()
        self.engine.download(["pgd:example_user|all"], again=True)
        self.assertEqual(self.cursors_fetched, [None, "c1", "c2"])


class StalledListTest(JobCase):
    """A list the stall guard cut short is not complete: the next sync reads it whole."""

    def run_lists(self, stalled):
        async def posts(plugin, ctx, user):
            yield Post("p1", [File(f"{CDN}/p1.jpg")], date=datetime.date(2026, 1, 1))
            ctx.listing.stalled = stalled

        async def media(plugin, ctx, user):
            yield Post("m1", [File(f"{CDN}/m1.jpg")], date=datetime.date(2026, 1, 1))

        self.engine = Engine(home=self.home, sources={
            "stl": feed("stl", posts, media, newest_first=True, key="{post}")})
        self.assertTrue(self.engine.download(["stl:example_user|all"]).ok)
        return fakes.targets(self.home)["stl:example_user"]

    def test_a_stalled_list_is_left_out_of_the_complete_set(self):
        row = self.run_lists(True)
        self.assertNotIn("posts", row.complete)
        self.assertIn("media", row.complete)

    def test_a_list_that_ends_normally_is_complete(self):
        row = self.run_lists(False)
        self.assertIn("posts", row.complete)
        self.assertIn("media", row.complete)


class MemoryTest(JobCase):
    """The listing memory: an undated post waits for its category's end, and takes any day found."""

    def run_feed(self, *categories, hook=None):
        self.run_engine(self.feed(*categories, hook=hook), targets=["mem:example_user"])
        return self.files("mem")

    def feed(self, *categories, hook=None):
        async def posts(plugin, ctx, user):
            for post in categories[0]:
                yield post

        async def photos(plugin, ctx, user):
            for post in categories[1] if len(categories) > 1 else []:
                yield post

        declared = {"folder": "", "filename": "{date}_{post}", "key": "{post}_{num}"}
        if hook is not None:
            async def dated(plugin, ctx, post):
                return hook(post)

            declared["date"] = dated
        return feed("mem", posts, photos, **declared)

    def test_a_dated_copy_later_in_the_category_names_the_undated_one(self):
        undated = Post("A1", [File(f"{CDN}/a.jpg")])
        dated = Post("A1", [File(f"{CDN}/a.jpg")], date=datetime.date(2026, 1, 2))
        self.assertEqual(self.run_feed([undated, dated]), ["2026-01-02_A1.jpg"])

    def test_an_undated_post_with_no_dated_copy_is_saved_when_its_category_ends(self):
        self.assertEqual(self.run_feed([Post("A1", [File(f"{CDN}/a.jpg")])]), ["A1.jpg"])

    def test_an_earlier_category_or_run_gives_its_day(self):
        dated = Post("A1", [File(f"{CDN}/a.jpg")], date=datetime.date(2026, 1, 2))
        undated = Post("B2", [File(f"{CDN}/b.jpg")])
        self.run_feed([dated])
        # B2's day is known from an earlier run's row for its key.
        with _store.open(self.home) as db:
            db.save([_store.FileRow("mem:B2_0", "mem:example_user", "/gone.jpg", 1, post_id="B2",
                                    at="2026-01-03T00:00:00+00:00", date="2026-01-03")])
        self.assertIn("2026-01-03_B2.jpg", self.run_feed([undated]))

    def test_the_sites_date_hook_dates_an_undated_post(self):
        asked = []
        files = self.run_feed([Post("A1", [File(f"{CDN}/a.jpg")])],
                              hook=lambda post: asked.append(post.id) or datetime.date(2026, 1, 4))
        self.assertEqual((files, asked), (["2026-01-04_A1.jpg"], ["A1"]))
        self.assertFalse(self.history()["mem:A1_0"]["estimated"])

    def test_without_a_hook_the_nearest_dated_neighbour_gives_an_estimated_day(self):
        posts = [Post("N1", [File(f"{CDN}/n1.jpg")], date=datetime.date(2026, 1, 5)),
                 Post("U1", [File(f"{CDN}/u1.jpg")]),
                 Post("N2", [File(f"{CDN}/n2.jpg")], date=datetime.date(2026, 1, 3))]
        self.assertIn("2026-01-05_U1.jpg", self.run_feed(posts))
        self.assertIs(self.history()["mem:U1_0"]["estimated"], True)

    def test_a_block_while_dating_estimates_the_rest_without_failing(self):
        def blocked(post):
            raise SiteError("HTTP 429", status=429)

        posts = [Post("N1", [File(f"{CDN}/n1.jpg")], date=datetime.date(2026, 1, 5)),
                 Post("U1", [File(f"{CDN}/u1.jpg")]), Post("U2", [File(f"{CDN}/u2.jpg")])]
        files = self.run_feed(posts, hook=blocked)
        self.assertEqual(sorted(files),
                         ["2026-01-05_N1.jpg", "2026-01-05_U1.jpg", "2026-01-05_U2.jpg"])
        self.assertEqual(sum("dating stopped" in line for line in self.lines()), 1)

    def test_sync_dates_and_renames_a_file_saved_undated_and_keeps_its_row(self):
        undated = [Post("A1", [File(f"{CDN}/a.jpg")])]
        self.assertEqual(self.run_feed(undated), ["A1.jpg"])
        feed = self.feed(undated, hook=lambda post: datetime.date(2026, 1, 4))
        engine = Engine(home=self.home, sources={"mem": feed})
        engine.target_follow(["mem:example_user"], on=True)
        engine.sync()
        self.assertEqual(self.files("mem"), ["2026-01-04_A1.jpg"])
        row = self.history()["mem:A1_0"]
        self.assertEqual((Path(row["path"]).name, row["date"], row["post_id"]),
                         ("2026-01-04_A1.jpg", "2026-01-04", "A1"))

    def test_a_second_undated_copy_counts_as_a_skip(self):
        self.assertEqual(
            self.run_feed([Post("A1", [File(f"{CDN}/a.jpg")]), Post("A1", [File(f"{CDN}/a.jpg")])]),
            ["A1.jpg"],
        )

    def test_ledger_order_and_dates_not_tracked_on_dated_feed(self):
        posts = [
            Post(f"P{i}", [File(f"{CDN}/p{i}.jpg")], date=datetime.date(2026, 1, 1))
            for i in range(10)
        ]
        self.assertEqual(len(self.run_feed(posts)), 10)

    def test_claims_release_removes_in_flight_dest_and_key(self):
        from flnd.engine.crawl import Claims
        claims = Claims()
        dest = Path(self.home) / "dest.jpg"
        key = "mem:key1"
        self.assertIsNone(claims.claim(dest, key, "dest.jpg"))
        self.assertIn(dest, claims)
        self.assertIn(key, claims)
        claims.release(dest, key)
        self.assertNotIn(dest, claims)
        self.assertNotIn(key, claims)
        self.assertIsNone(claims.claim(dest, key, "dest.jpg"))


class LinkTest(JobCase):
    """A file found in a second category's folder is linked there, not downloaded again."""

    def feed(self, posts, reels):
        async def videos(plugin, ctx, user):
            for post in posts:
                yield post

        async def reels_of(plugin, ctx, user):
            for post in reels:
                yield post

        reels_of.__name__ = "reels"
        return feed("lnk", videos, reels_of, key="{post}_{num}")

    def clip(self, post_id="V1"):
        return Post(post_id, [File(f"{CDN}/{post_id}.mp4")], date=datetime.date(2026, 1, 2))

    def test_a_file_in_two_categories_is_saved_once_and_symlinked_relative(self):
        self.run_engine(self.feed([self.clip()], [self.clip()]), targets=["lnk:example_user"])
        folder = self.target_dir("lnk", "example_user")
        link = folder / "reels" / "2026-01-02_V1.mp4"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), os.path.join("..", "videos", "2026-01-02_V1.mp4"))
        self.assertEqual(link.read_bytes(), (folder / "videos" / "2026-01-02_V1.mp4").read_bytes())
        self.assertEqual(len(self.fetched(f"{CDN}/V1.mp4")), 1)

    def test_a_second_copy_in_one_category_is_skipped_not_linked(self):
        self.run_engine(self.feed([self.clip(), self.clip()], []), targets=["lnk:example_user"])
        self.assertEqual(self.files("lnk"), ["videos/2026-01-02_V1.mp4"])

    def test_a_refused_symlink_falls_back_to_a_hard_link_then_to_one_warning(self):
        with mock.patch.object(Path, "symlink_to", side_effect=OSError("no symlinks here")):
            self.run_engine(self.feed([self.clip()], [self.clip()]), targets=["lnk:example_user"])
        link = self.target_dir("lnk", "example_user") / "reels" / "2026-01-02_V1.mp4"
        self.assertTrue(link.is_file() and not link.is_symlink())
        self.assertEqual(link.stat().st_nlink, 2)
        with mock.patch.object(Path, "symlink_to", side_effect=OSError("no")), \
                mock.patch.object(os, "link", side_effect=OSError("no links on this volume")):
            both = [self.clip("V2"), self.clip("V3")]
            self.run_engine(self.feed(both, both), targets=["lnk:example_user"])
        self.assertEqual(sum("can't link files" in line for line in self.lines()), 1)

    def test_sync_removes_a_dangling_link_and_the_next_run_makes_it_again(self):
        feed = self.feed([self.clip()], [self.clip()])
        self.run_engine(feed, targets=["lnk:example_user"])
        folder = self.target_dir("lnk", "example_user")
        (folder / "videos" / "2026-01-02_V1.mp4").unlink()
        Engine(home=self.home, sources={"lnk": feed}).sync()
        link = folder / "reels" / "2026-01-02_V1.mp4"
        self.assertTrue(link.is_symlink() and link.exists())


class VerboseTest(EngineCase):
    """The engine logs no rows (the CLI draws a run from its events); -v adds its debug lines."""

    def setUp(self):
        super().setUp()
        previous = ui.level
        self.addCleanup(setattr, ui, "level", previous)
        self.source = make_plugin("aaa", files=[File(f"{CDN}/a.jpg", name="a.jpg"),
                                                File(f"{CDN}/b.jpg", name="b.jpg")])

    def test_at_info_a_run_prints_no_rows_and_no_debug_lines(self):
        self.run_engine(self.source, targets=["aaa:example_user"])
        lines = self.lines()
        self.assertFalse(any(line.startswith(". ") for line in lines), lines)
        self.assertFalse(any("a.jpg" in line or "posts" in line for line in lines), lines)
        self.assertIn(("aaa:example_user", "posts"), self.listed())

    def test_under_v_each_target_and_skip_is_written(self):
        ui.level = "debug"
        self.run_engine(self.source, targets=["aaa:example_user"])
        self.assertTrue(any(line.startswith(". aaa:example_user: 2 files saved")
                            for line in self.lines()), self.lines())
        self.dest.truncate(0)
        self.dest.seek(0)
        self.run_engine(self.source, targets=["aaa:example_user"])
        self.assertIn(". skip a.jpg", self.lines())


class DiscoveredTest(EngineCase):
    """Discovered and linked targets run as targets of their own, each on its plugin's instance."""

    def test_a_target_a_route_discovers_runs_on_its_own_plugin(self):
        # E12: an author's page of one plugin listing a target of another plugin.
        files = [File(f"{CDN}/a.jpg", name="a.jpg")]
        self.run_engine(author("bbb:example_creator"), make_plugin("bbb", files=files),
                        targets=["aut:example_user"])
        self.assertEqual(self.failures(), [])
        self.assertTrue((self.target_dir("bbb", "example_creator") / "a.jpg").is_file())

    def test_a_target_a_listing_links_to_sees_its_parents_instance(self):
        made = []

        class Linking(Plugin):
            code, name, domains, api = "lnk", "Linking", ("lnk.example",), 2
            folder, key = "", "{path}"

            @route("lnk:{user}", newest_first=False)
            async def user(self, ctx, user):
                made.append(self)
                return user

            @user.category(newest_first=False)
            async def posts(self, ctx, user):
                if user == "1":
                    yield Post("1", [File(f"{CDN}/1.jpg", name="1.jpg")])
                    yield Target("lnk:2")

        self.run_engine(Linking, targets=["lnk:1"])
        self.assertEqual(len(made), 2)
        self.assertIs(made[0], made[1])

    def test_a_discovered_target_takes_its_own_plugins_listing_place(self):
        # A browser plugin lists one target at a time, also one another plugin's route found.
        running, peak = [0], [0]

        async def files(ctx, context):
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            # Let the loop run any other listing that could start now.
            for _ in range(50):
                await asyncio.sleep(0)
            running[0] -= 1
            return []

        browser = make_plugin("bbb", files=files, browser=True)
        self.run_engine(browser, author("bbb:2"), targets=["bbb:1", "aut:example_user"])
        self.assertEqual(self.failures(), [])
        self.assertEqual(peak[0], 1)

    def test_two_browser_plugins_discovering_each_others_targets_finish_each_target_once(self):
        # Each lists one target at a time. A child waiting for the other plugin's place while
        # its parent held its own would wait forever, for the other's child waiting back.
        listed = []

        def plugin(own, other):
            class Lister(Plugin):
                code, name, domains, api = own, own.upper(), (f"{own}.example",), 2
                folder, key, browser = "", "{path}", Browser.ANY

                @route(f"{own}:list/{{name}}", discovers=True)
                async def lists(self, ctx, name):
                    yield Target(f"{other}:{name}")

                @route(f"{own}:{{user}}", newest_first=False)
                async def user(self, ctx, user):
                    listed.append(f"{own}:{user}")
                    return user

                @user.category(newest_first=False)
                async def posts(self, ctx, user):
                    return
                    yield

            return Lister

        first, second = plugin("aaa", "bbb"), plugin("bbb", "aaa")
        done = []
        runner = threading.Thread(target=lambda: done.append(self.run_engine(
            first, second, targets=["aaa:list/x", "bbb:list/y", "aaa:y"])), daemon=True)
        runner.start()
        runner.join(20)
        self.assertEqual(done, [0], "the run never finished")
        self.assertEqual(sorted(listed), ["aaa:y", "bbb:x"])

    def test_a_full_disk_ends_the_targets_a_listing_handed_to_the_run(self):
        ran = []

        async def slow(ctx, context):
            # A listing that would go on until the run ends it.
            await asyncio.wait_for(asyncio.Event().wait(), 5)
            return []

        real = files_module.sweep

        def sweep(folder):
            if folder.name == "full":
                raise disk(OSError(errno.ENOSPC, "No space left on device"), folder)
            return real(folder)

        handed = make_plugin("bbb", ran.append, files=slow, browser=True)
        full = make_plugin("ccc", files=[File(f"{CDN}/a.jpg", name="a.jpg")])
        self.enterContext(mock.patch.object(files_module, "sweep", sweep))
        with self.assertRaises(DiskError):
            self.report(author(*(f"bbb:{number}" for number in range(10))), handed, full,
                        targets=["aut:example_user", "ccc:full"])
        # The one listing when the disk filled may have begun; the nine waiting never do.
        self.assertLessEqual(len(ran), 1)


class HandedTest(unittest.TestCase):
    def test_a_handed_target_cancelled_before_its_task_starts_leaves_no_coroutine(self):
        """A full disk may cancel a handed target's task before it ran a step: no coroutine
        of it is left never awaited."""
        runner = engine_module._Runner(mock.Mock(), mock.Mock())

        async def handed():
            await runner.follow(Target("bbb:example_user", kind="user"), Options(), inside="aut")
            [task] = runner._handed
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            runner._handed.clear()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            asyncio.run(handed())
            gc.collect()
        self.assertEqual([str(warning.message) for warning in caught
                          if "never awaited" in str(warning.message)], [])


class QueryAndLimitDryTest(EngineCase):
    """Query targets, target-yielding routes, `--limit`, `--dry`, and early close."""

    def _query_source(self, *, closed: list[bool]):
        class Searchable(Plugin):
            code, name, domains, api = "srch", "Searchable", ("srch.example",), 2

            @route(r"srch:{id:\d+}", newest_first=False)
            async def post(self, ctx, id):
                return id

            @post.category(newest_first=False)
            async def photo(self, ctx, id):
                yield Post(id, [File(f"{CDN}/{id}.jpg", name=f"{id}.jpg")])

            @post.category(newest_first=False)
            async def extra(self, ctx, id):
                yield Post(id, [File(f"{CDN}/{id}_extra.jpg", name=f"{id}_extra.jpg")])

            @route("srch:list/{name}", discovers=True)
            async def listing(self, ctx, name):
                yield Target("srch:1", kind="post")
                yield Target("srch:2")

            @route("srch?{query}")
            async def query(self, ctx, query: str):
                try:
                    if "action" in query:
                        yield Target("srch:1", kind="post")
                        yield Target("srch:2", kind="post")
                        yield Target("srch:3", kind="post")
                    if "comedy" in query:
                        yield Target("srch:2", kind="post")
                        yield Target("srch:4", kind="post")
                finally:
                    closed.append(True)

        return Searchable

    def test_query_route_dedupes_and_respects_from(self):
        closed = []
        for number in range(1, 5):
            self.routes[f"{CDN}/{number}.jpg"] = _net.reply(200, content=str(number).encode())
        engine = Engine(home=self.home, sources={"srch": self._query_source(closed=closed)})

        report = engine.download(["srch?action comedy|photo"], limit=4, folder="{target}/{category}")
        self.assertTrue(report.ok)
        self.assertEqual([r.target.value for r in report.targets], ["1", "2", "3", "4"])
        self.assertEqual(len(closed), 1)
        self.assertFalse((self.target_dir("srch", "1") / "extra").exists())

    def test_limit_closes_the_query_generator_early_and_again_reaches_every_target(self):
        closed, seen_options = [], []
        self.routes[f"{CDN}/1.jpg"] = _net.reply(200, content=b"1")
        engine = Engine(home=self.home, sources={"srch": self._query_source(closed=closed)})
        # --limit 1 on query route stops after 1 target and closes the generator.
        report = engine.download(["srch?action|photo"], limit=1, folder="{target}/{category}")
        self.assertEqual(len(report.targets), 1)
        self.assertEqual(closed, [True])

        orig_download = job.download

        async def spy(run, target, board, options, **listed):
            seen_options.append(options)
            return await orig_download(run, target, board, options, **listed)

        with mock.patch("flnd.engine.crawl.download", spy):
            engine.download(["srch:1|photo", "srch?action|photo"], limit=1, again=True,
                            folder="{target}/{category}")
        self.assertTrue(seen_options)
        self.assertTrue(all(opt.again for opt in seen_options))

    def test_dry_run_on_query_and_normal_target_writes_nothing(self):
        engine = Engine(home=self.home, sources={"srch": self._query_source(closed=[])})

        report = engine.download(["srch?action|photo"], limit=2, dry=True, folder="{target}/{category}")
        self.assertTrue(report.ok)
        output = "\n".join(self.lines())
        self.assertIn("srch:1", output)
        self.assertIn("srch:2", output)
        self.assertFalse((self.home / _store.DB).exists())
        self.assertFalse((self.home / _store.MEDIA).exists())

        report_normal = engine.download(["srch:1|photo"], dry=True)
        self.assertTrue(report_normal.ok)
        self.assertIn("1.jpg", "\n".join(self.lines()))
        self.assertFalse((self.home / _store.MEDIA).exists())

    def test_limit_on_normal_target_and_target_yielding_route(self):
        class Multi(Plugin):
            code, name, domains, api = "mlt", "Multi", ("mlt.example",), 2

            @route("mlt:{user}", newest_first=False)
            async def user(self, ctx, user):
                return user

            @user.category(newest_first=False)
            async def posts(self, ctx, user):
                for n in range(1, 5):
                    yield Post(str(n), [File(f"{CDN}/{n}.jpg", name=f"{n}.jpg")])

        for n in range(1, 5):
            self.routes[f"{CDN}/{n}.jpg"] = _net.reply(200, content=str(n).encode())
        engine = Engine(home=self.home, sources={"mlt": Multi})
        report = engine.download(["mlt:example_user|all"], limit=2)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.targets[0].written), 2)

        engine2 = Engine(home=self.home, sources={"srch": self._query_source(closed=[])})
        report2 = engine2.download(["srch:list/all|all"], limit=1)
        self.assertTrue(report2.ok)
        self.assertTrue((self.target_dir("srch", "1") / "photo" / "1.jpg").is_file())
        self.assertFalse((self.target_dir("srch", "2") / "photo" / "2.jpg").exists())


class SyncedQueryTest(EngineCase):
    """Synced queries: 25 cap, known targets marked seen, `--dry`, Ctrl-C / `--limit` close,
    failing route."""

    def _paged_source(self, pages, *, closed, seen_pages, fail_on=None):
        class Paged(Plugin):
            code, name, domains, api = "pgd", "Paged", ("pgd.example",), 2

            @route(r"pgd:{id:\d+}", newest_first=False)
            async def post(self, ctx, id):
                return id

            @post.category(newest_first=False)
            async def photo(self, ctx, id):
                yield Post(id, [File(f"{CDN}/{id}.jpg", name=f"{id}.jpg")])

            @route("pgd?{query}")
            async def query(self, ctx, query: str):
                try:
                    for page_num, ids in enumerate(pages):
                        seen_pages.append(page_num)
                        if fail_on is not None and query == fail_on:
                            raise RuntimeError("search failed")
                        for item_id in ids:
                            yield Target(f"pgd:{item_id}", kind="post")
                finally:
                    closed.append(True)

        return Paged

    def _empty_source(self):
        return self._paged_source([], closed=[], seen_pages=[])

    def test_three_syncs_of_a_search_of_60_reach_all_60(self):
        # E3: a sync walks past the targets earlier syncs saw, so each takes the next 25 new ones.
        pages = [list(range(1, 31)), list(range(31, 61))]
        src = self._paged_source(pages, closed=[], seen_pages=[])
        for n in range(1, 61):
            self.routes[f"{CDN}/{n}.jpg"] = _net.reply(200, content=str(n).encode())
        Engine(home=self.home, sources={"pgd": self._empty_source()}).download(
            ["pgd?cats|photo"], sync=True, folder="{target}/{category}")
        engine = Engine(home=self.home, sources={"pgd": src})
        written = [len(engine.sync().targets[0].written) for _ in range(3)]
        self.assertEqual(written, [25, 25, 10])
        self.assertEqual(len(fakes.targets(self.home)["pgd:cats"].seen), 60)

    def test_a_known_target_is_marked_seen_and_not_touched_and_dry_marks_nothing(self):
        src = self._paged_source([[1, 2]], closed=[], seen_pages=[])
        self.routes[f"{CDN}/1.jpg"] = _net.reply(200, content=b"1")
        self.routes[f"{CDN}/2.jpg"] = _net.reply(200, content=b"2")
        engine = Engine(home=self.home, sources={"pgd": src})
        # User already downloaded pgd:1 (not synced).
        engine.download(["pgd:1|photo"])
        Engine(home=self.home, sources={"pgd": self._empty_source()}).download(
            ["pgd?cats|photo"], sync=True, folder="{target}/{category}")

        self.requests.clear()
        report = engine.sync()
        self.assertTrue(report.ok)
        # Only pgd:2 was downloaded; pgd:1 was not touched and was marked seen.
        self.assertEqual([p.name for p in report.targets[0].written], ["2.jpg"])
        self.assertEqual(fakes.targets(self.home)["pgd:cats"].seen, ["pgd:1", "pgd:2"])

    def test_dry_lists_targets_and_marks_no_result_seen(self):
        src = self._paged_source([[1, 2]], closed=[], seen_pages=[])
        engine = Engine(home=self.home, sources={"pgd": src})
        report = engine.download(["pgd?cats|photo"], dry=True, sync=True, folder="{target}/{category}")
        self.assertTrue(report.ok)
        self.assertFalse((self.home / _store.DB).exists())

    def test_ctrl_c_and_limit_close_generator_and_keep_partial_seen(self):
        pages = [[1], [2], [3]]
        closed, seen_pages = [], []
        src = self._paged_source(pages, closed=closed, seen_pages=seen_pages)
        self.routes[f"{CDN}/1.jpg"] = _net.reply(200, content=b"1")
        self.routes[f"{CDN}/2.jpg"] = _net.reply(200, content=b"2")
        engine = Engine(home=self.home, sources={"pgd": src})
        # --limit 1 stops after page 0 and closes the generator without asking for page 1.
        report = engine.download(["pgd?cats|photo"], limit=1, sync=True, folder="{target}/{category}")
        self.assertTrue(report.ok)
        self.assertEqual(seen_pages, [0])
        self.assertEqual(closed, [True])
        self.assertEqual(fakes.targets(self.home)["pgd:cats"].seen, ["pgd:1"])

        # Ctrl-C during item 2 stops before page 2 and keeps item 1 seen.
        closed.clear()
        seen_pages.clear()
        real_download = job.download
        stop_ref: list[_run.Stop] = []
        real_stopping = _run.stopping

        @contextmanager
        def capture_stopping(state=None):
            with real_stopping(state) as stop:
                stop_ref.append(stop)
                yield stop

        async def stop_on_second(run, child, board, opts, **listed):
            if child.value == "2":
                stop_ref[0].soon.set()
            return await real_download(run, child, board, opts, **listed)

        with mock.patch("flnd.engine.crawl.download", stop_on_second), \
                mock.patch("flnd.engine._engine.stopping", capture_stopping), \
                self.assertRaises(KeyboardInterrupt):
            engine.download(["pgd?dogs|photo"], sync=True, folder="{target}/{category}")
        self.assertEqual(seen_pages, [0, 1])
        self.assertEqual(closed, [True])
        self.assertEqual(fakes.targets(self.home)["pgd:dogs"].seen, ["pgd:1"])

    def test_failing_route_fails_target_and_next_target_still_runs(self):
        src = self._paged_source([[1]], closed=[], seen_pages=[], fail_on="bad")
        self.routes[f"{CDN}/1.jpg"] = _net.reply(200, content=b"1")
        Engine(home=self.home, sources={"pgd": self._empty_source()}).download(
            ["pgd?bad|photo", "pgd?good|photo"], sync=True, folder="{target}/{category}")

        report = Engine(home=self.home, sources={"pgd": src}).sync()
        self.assertFalse(report.ok)
        self.assertEqual([str(t) for t in report.failed], ["pgd?bad"])
        self.assertEqual([p.name for p in report.targets[1].written], ["1.jpg"])


class FolderOptionTest(JobCase):
    """`Options.folder` places files under `media/<plugin>/<rendered>/`, collapsing searches."""

    def media_files(self, code):
        root = self.home / _store.MEDIA / code
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def test_normal_target_with_static_and_templated_folder(self):
        # Overrides plugin, route, category and config.toml folder templates.
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text('[plugins.exm]\nfolder = "ignored/{category}"\n',
                                               encoding="utf-8")
        engine = Engine(home=self.home, sources={"exm": Example})
        report = engine.download(["exm:example_user"], folder="custom/sub")
        self.assertTrue(report.ok)
        self.assertEqual(self.media_files("exm"), [
            "custom/sub/2026-01-01_B2def.jpg",
            "custom/sub/2026-01-02_C1abc_0.jpg",
            "custom/sub/2026-01-02_C1abc_1.mp4",
            "custom/sub/A1.jpg",
            "custom/sub/avatar_42.jpg",
        ])

        other = self.home.parent / "home2"
        other.mkdir()
        report2 = Engine(home=other, sources={"exm": Example}).download(
            ["exm:example_user|posts"], folder="{target}/{date}")
        self.assertTrue(report2.ok)
        root2 = other / _store.MEDIA / "exm"
        self.assertEqual(
            sorted(p.relative_to(root2).as_posix() for p in root2.rglob("*") if p.is_file()),
            ["example_user/2026-01-01/2026-01-01_B2def.jpg",
             "example_user/2026-01-02/2026-01-02_C1abc_0.jpg",
             "example_user/2026-01-02/2026-01-02_C1abc_1.mp4"])

    def test_search_collapses_discovered_targets_and_sweeps_safely(self):
        class Finder(Plugin):
            code, name, domains, api = "fnd", "Finder", ("fnd.example",), 2
            folder = "{category}"
            filename = "{post}"
            key = "{post}"

            @route("fnd?{query}", discovers=True)
            async def search(self, ctx, query):
                yield Target("fnd:p/1")
                yield Target("fnd:p/2")

            @route("fnd:p/{pid}", single=True)
            async def post(self, ctx, pid):
                yield Post(pid, [File(f"{CDN}/{pid}.jpg")], date=date(2026, 3, 1))

        self.routes[f"{CDN}/1.jpg"] = _net.reply(200, content=b"one")
        self.routes[f"{CDN}/2.jpg"] = _net.reply(200, content=b"two")
        swept: list[Path] = []
        with mock.patch.object(files_module, "sweep", side_effect=swept.append):
            report = Engine(home=self.home, sources={"fnd": Finder}).download(
                ["fnd?@example_handle -from:example_handle"], folder="example_handle/tagged")
        self.assertTrue(report.ok)
        self.assertEqual(self.media_files("fnd"), [
            "example_handle/tagged/1.jpg", "example_handle/tagged/2.jpg"])
        self.assertTrue(swept)
        self.assertTrue(all(
            p == self.home / _store.MEDIA / "fnd" / "example_handle" / "tagged" for p in swept))

        # Dynamic leading segment skips sweeping the whole plugin root.
        other = self.home.parent / "home_dyn"
        other.mkdir()
        swept.clear()
        with mock.patch.object(files_module, "sweep", side_effect=swept.append):
            report2 = Engine(home=other, sources={"fnd": Finder}).download(
                ["fnd?@example_handle filter:media"], folder="{query}")
        self.assertTrue(report2.ok)
        self.assertEqual(swept, [])
        root2 = other / _store.MEDIA / "fnd"
        self.assertEqual(
            sorted(p.relative_to(root2).as_posix() for p in root2.rglob("*") if p.is_file()),
            ["@example_handle_filter_media/1.jpg", "@example_handle_filter_media/2.jpg"])

    def test_render_time_injection_cannot_escape_plugin_media(self):
        class Sneaky(Plugin):
            code, name, domains, api = "snk", "Sneaky", ("snk.example",), 2
            fields = ("label",)
            filename = "{post}"
            key = "{post}"

            @route("snk?{query}", discovers=True)
            async def search(self, ctx, query):
                yield Target("snk:p/1")

            @route("snk:p/{pid}", single=True)
            async def post(self, ctx, pid):
                yield Post(pid, [File(f"{CDN}/1.jpg")], label="../../evil\\x/C:drive")

        self.routes[f"{CDN}/1.jpg"] = _net.reply(200, content=b"safe")
        engine = Engine(home=self.home, sources={"snk": Sneaky})
        report = engine.download(["snk?../escape/a/b"], folder="{query}/{label}")
        self.assertTrue(report.ok)
        plugin_root = (self.home / _store.MEDIA / "snk").resolve()
        written = [p for rep in report.targets for p in rep.written]
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].resolve().is_relative_to(plugin_root))
        self.assertEqual(self.media_files("snk"), ["escape_a_b/evil_x_C_drive/1.jpg"])

        # Direct containment check refuses traversal components even if injected manually.
        for bad in ("../escape/a.jpg", "a/../../b.jpg", "C:evil/a.jpg", "a\\b/c.jpg", "a\x00b"):
            with self.subTest(bad=bad), self.assertRaises(UsageError):
                job.checked_dest(plugin_root, bad, check=True)

    def test_dry_plan_uses_custom_folder(self):
        events = []
        engine = Engine(home=self.home, sources={"exm": Example}, emit=events.append)
        report = engine.download(["exm:example_user|posts"], dry=True, folder="{target}/{date}")
        self.assertTrue(report.ok)
        planned = sorted(
            e.path.relative_to(self.home / _store.MEDIA / "exm").as_posix()
            for e in events if getattr(e, "path", None) is not None
        )
        self.assertEqual(planned, [
            "example_user/2026-01-01/2026-01-01_B2def.jpg",
            "example_user/2026-01-02/2026-01-02_C1abc_0.jpg",
            "example_user/2026-01-02/2026-01-02_C1abc_1.mp4",
        ])
        self.assertFalse((self.home / _store.MEDIA).exists())

    def test_query_short_circuits_on_head_and_again_and_when(self):
        from flnd.plugin import Range

        pages = [[10, 9], [8, 7, 6, 5, 4], [3, 2, 1]]
        closed, seen_pages, seen_when = [], [], []

        class Paged(Plugin):
            code, name, domains, api = "pgd", "Paged", ("pgd.example",), 2

            @route(r"pgd:{id:\d+}", newest_first=False)
            async def post(self, ctx, id):
                return id

            @post.category(newest_first=False)
            async def photo(self, ctx, id):
                yield Post(id, [File(f"{CDN}/{id}.jpg", name=f"{id}.jpg")])

            @route("pgd?{query}")
            async def query(self, ctx, query: str):
                seen_when.append(ctx.when)
                try:
                    for page_num, ids in enumerate(pages):
                        seen_pages.append(page_num)
                        if query == "crash" and page_num == 1:
                            raise RuntimeError("boom")
                        for item_id in ids:
                            yield Target(f"pgd:{item_id}", kind="post")
                finally:
                    closed.append(True)

        for n in range(1, 13):
            self.routes[f"{CDN}/{n}.jpg"] = _net.reply(200, content=str(n).encode())

        # 1. Initial download runs all pages, sets head="pgd:10" and complete=["query"].
        engine = Engine(home=self.home, sources={"pgd": Paged})
        r1 = engine.download(["pgd?cats|photo"], sync=True, folder="{target}/{category}")
        self.assertTrue(r1.ok)
        self.assertEqual(seen_pages, [0, 1, 2])
        row = fakes.targets(self.home)["pgd:cats"]
        self.assertEqual(row.heads.get("query"), "pgd:10")
        self.assertIn("query", row.complete)

        # 2. Next run with 1 new item (11) before head (10, 9): short-circuits on page 0!
        pages[0] = [11, 10, 9]
        seen_pages.clear()
        self.requests.clear()
        r2 = engine.download(["pgd?cats|photo"], sync=True, folder="{target}/{category}")
        self.assertTrue(r2.ok)
        self.assertEqual(seen_pages, [0])
        self.assertEqual([p.name for p in r2.targets[0].written], ["11.jpg"])
        self.assertEqual(fakes.targets(self.home)["pgd:cats"].heads.get("query"), "pgd:11")

        # 3. engine.sync() also short-circuits on page 0 and forwards --when.
        pages[0] = [12, 11, 10]
        seen_pages.clear()
        seen_when.clear()
        r3 = engine.sync(time=Range(date(2026, 1, 1), date(2026, 1, 2)))
        self.assertTrue(r3.ok)
        self.assertEqual(seen_pages, [0])
        self.assertEqual(seen_when, [Range(date(2026, 1, 1), date(2026, 1, 2))])

        # 4. --again bypasses seen_keys and short-circuiting and reads all pages.
        seen_pages.clear()
        r4 = engine.download(["pgd?cats|photo"], again=True, folder="{target}/{category}")
        self.assertTrue(r4.ok)
        self.assertEqual(seen_pages, [0, 1, 2])

        # 5. A completed search with no head reads every page, and keeps a head after.
        with _store.Store(self.home) as s:
            s.progress(Target("pgd:cats", kind="query"), cursors={}, complete=["query"],
                       heads={})
        sql = sqlite3.connect(self.home / _store.DB)
        try:
            sql.execute("UPDATE targets SET heads = '{}' WHERE key = 'pgd:cats'")
            sql.commit()
        finally:
            sql.close()
        pages[0] = [12, 11]
        seen_pages.clear()
        r5 = engine.sync()
        self.assertTrue(r5.ok)
        self.assertEqual(seen_pages, [0, 1, 2])
        self.assertEqual(fakes.targets(self.home)["pgd:cats"].heads.get("query"), "pgd:12")

        # 6. Crash partway through (> SEEN_FLUSH=10) preserves already-flushed seen items.
        pages_big = [list(range(1, 13)), [99]]
        pages[:] = pages_big
        for n in range(1, 13):
            self.routes[f"{CDN}/{n}.jpg"] = _net.reply(200, content=str(n).encode())
        r6 = engine.download(["pgd?crash|photo"], sync=True, folder="{target}/{category}")
        self.assertFalse(r6.ok)
        self.assertGreaterEqual(len(fakes.targets(self.home)["pgd:crash"].seen), 10)


if __name__ == "__main__":
    unittest.main()
