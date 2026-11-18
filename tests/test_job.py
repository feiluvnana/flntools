"""The job (flnd/job.py): one target of a ported source, from its route to its history rows.

Run through the engine with tests/example_source.py on `flnd.kit.net.fake`, the clocks of
`flnd.kit.net` and site.py faked, and through `job.posts` on a `FakeSite` for the listing rules. Also the
"download Chromium?" question asked up front, an expired link's refresh, and both Ctrl-Cs (with
a CDN that blocks on events, and the run on a thread of its own). No network, no browser.
"""

import asyncio
import datetime
import io
import os
import sqlite3
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import fakes
from example_source import API, CDN, FIXTURES, Example
from fakes import FakeSite, collect
from test_commands import FlndCase
from test_engine import EngineCase, make_source

from flnd import _home, _run, job, sources
from flnd import engine as engine_module
from flnd import site as site_module
from flnd.engine import Engine
from flnd.errors import UsageError
from flnd.kit import clock as _time
from flnd.kit import net as _net
from flnd.options import Options
from flnd.source import (
    File,
    LoginError,
    NotFound,
    Post,
    SiteError,
    Source,
    SourceError,
    Target,
    target,
)
from flnd.ui.log import ui


class JobCase(EngineCase):
    """The engine with `Example`'s API answering from `FIXTURES`; every other URL answers its own path."""

    def setUp(self):
        super().setUp()
        for url, answer in FIXTURES.items():
            self.routes[url] = self.json_answer(url, answer)

    def json_answer(self, url, answer):
        def respond(request):
            data = answer(str(request.url), {"params": dict(request.url.params)}) if callable(answer) else answer
            return _net.reply(200, json=data)

        return respond

    def history(self):
        return fakes.history(self.home)

    def sql(self, statement, *args):
        """Run one statement on the home's `flnd.db`, as another tool might."""
        db = sqlite3.connect(self.home / _home.DB, isolation_level=None)
        try:
            db.execute(statement, args)
        finally:
            db.close()

    def fetched(self, url):
        return [request for request in self.requests if str(request.url).split("?")[0] == url]

    def files(self, code="exm", name="example_user"):
        folder = self.target_dir(code, name)
        return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())


class CategoriesTest(JobCase):
    def test_without_from_every_category_and_todays_names_and_keys(self):
        self.assertEqual(self.run_engine(Example, targets=["exm:example_user"]), 5)
        self.assertEqual(self.files(), ["albums/Summer_2026/A1.jpg", "posts/2026-01-01_B2def.jpg",
                                        "posts/2026-01-02_C1abc_0.jpg", "posts/2026-01-02_C1abc_1.mp4",
                                        "profile/avatar_42.jpg"])
        rows = self.history()
        self.assertEqual(sorted(rows), ["exm:A1_0", "exm:B2def_0", "exm:C1abc_0", "exm:C1abc_1",
                                        "exm:profile/avatar_42.jpg"])
        # Today's row format: where it is, its size and modified time, and when.
        # The first file of a post of several holds their count (fixes #2).
        self.assertEqual(sorted(rows["exm:C1abc_0"]),
                         ["at", "before", "category", "date", "estimated", "files", "folder", "hash", "key", "kind", "mtime", "path",
                          "post_id", "shrink", "sidecar", "size", "state", "target", "tools", "unpacked"])
        self.assertEqual(rows["exm:C1abc_0"]["files"], 2)
        self.assertEqual(sorted(rows["exm:C1abc_1"]),
                         ["at", "before", "category", "date", "estimated", "files", "folder", "hash", "key", "kind", "mtime", "path",
                          "post_id", "shrink", "sidecar", "size", "state", "target", "tools", "unpacked"])
        # The route is called once for its three categories.
        self.assertEqual(len(self.fetched(f"{API}/users/example_user")), 1)

    def test_only_the_wanted_categories_are_called(self):
        self.run_engine(Example, targets=["exm:example_user"], options=Options(categories=("profile",)))
        self.assertEqual(self.files(), ["profile/avatar_42.jpg"])
        self.assertEqual(self.fetched(f"{API}/users/42/posts") + self.fetched(f"{API}/albums/A1"), [])

    def test_a_route_none_of_whose_categories_is_wanted_is_never_called(self):
        called = []

        class Tagged(Source):
            code, name, domains = "tgd", "Tagged", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"

            @target("tgd:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post(user, [File(f"{CDN}/{user}.jpg")])

            @target("example.com/tags/{tag}")
            def tag(self, site, tag):
                called.append(tag)
                return tag

            @tag.category
            def tagged(self, site, tag):
                yield Post(tag, [File(f"{CDN}/{tag}.jpg")])

        self.run_engine(Tagged, targets=["https://example.com/tags/example_tag"], options=Options(categories=("posts",)))
        self.assertEqual((called, self.requests), ([], []))
        self.run_engine(Tagged, targets=["https://example.com/tags/example_tag"])
        self.assertEqual(called, ["example_tag"])

    def test_the_engine_sets_category(self):
        saved = collect(Example, "exm:example_user", json=FIXTURES)
        self.assertEqual([item.post.category for item in saved], ["posts", "posts", "posts", "albums", "profile"])

    def test_a_route_without_categories_files_its_posts_in_the_targets_folder(self):
        self.assertEqual(self.run_engine(Example, targets=["https://www.example.com/p/C1abc/"]), 1)
        self.assertEqual(self.files(name="C1abc"), ["2026-01-02_C1abc.jpg"])

    def test_from_with_a_single_post_is_still_the_usage_error(self):
        self.assertEqual(Example.single_kinds, ("post",))
        with self.assertRaises(UsageError) as ctx:
            Engine(home=self.home, sources={"exm": Example}).download(
                ["https://www.example.com/p/C1abc/"], categories="posts"
            )
        self.assertEqual(str(ctx.exception), "--only applies to profiles and chats; exm:C1abc is one post, downloaded whole: "
                                               "give it without --only")


class FromCommandTest(FlndCase):
    def test_from_with_a_post_link_exits_2_through_the_command(self):
        with mock.patch.dict(sources.SOURCES, {"exm": Example}), \
                mock.patch.object(engine_module.Engine, "_run") as run:
            code, _, out = self.run_app("download", "--only", "posts", "https://www.example.com/p/C1abc/")
        self.assertEqual(code, 2)
        self.assertIn("--only applies to profiles and chats; exm:C1abc is one post", out)
        run.assert_not_called()


class SavedTest(JobCase):
    def test_a_second_run_lists_no_saved_lazy_post_and_downloads_nothing(self):
        self.run_engine(Example, targets=["exm:example_user"])
        self.requests.clear()
        self.assertEqual(self.run_engine(Example, targets=["exm:example_user"]), 0)
        # The album's photos are a lazy post: saved, so its request isn't made again.
        self.assertEqual(self.fetched(f"{API}/albums/A1"), [])
        self.assertEqual([request for request in self.requests if request.url.host == "cdn.example"], [])

    def test_a_lazy_post_is_listed_only_when_needed(self):
        listed = []

        class Lazy(Source):
            code, name, domains = "lzy", "Lazy", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"

            @target("lzy:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                for name, posted in (("old", 1735689600), ("new", 1767312000)):
                    yield Post(name, lambda name=name: listed.append(name) or [File(f"{CDN}/{name}.jpg")], date=posted)

        options = Options(since=date(2026, 1, 1))
        self.run_engine(Lazy, targets=["lzy:example_user"], options=options)
        # The 2025 post is outside --time: skipped before its files are listed.
        self.assertEqual(listed, ["new"])
        self.run_engine(Lazy, targets=["lzy:example_user"], options=options)
        self.assertEqual(listed, ["new"])

    def test_a_lazy_post_whose_later_file_failed_is_listed_again(self):
        listed = []

        class Carousel(Source):
            code, name, domains, key = "crs", "Carousel", ("example.com",), "{post}_{num}"
            api = 1
            filename = "{date}_{post}{index}"

            @target("crs:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("C1abc", lambda: listed.append("C1abc") or [File(f"{CDN}/c{num}.jpg") for num in range(3)])

        self.routes[f"{CDN}/c1.jpg"] = _net.reply(403)
        self.run_engine(Carousel, targets=["crs:example_user"])
        self.assertEqual(sorted(self.history()), ["crs:C1abc_0", "crs:C1abc_2"])
        del self.routes[f"{CDN}/c1.jpg"]
        # The 403 asked for the post's files once more (B6), so the first run listed it twice.
        before = len(listed)
        self.requests.clear()
        self.run_engine(Carousel, targets=["crs:example_user"])
        # The retry lists the post once more, for fresh links, and fetches the failed file alone.
        self.assertEqual(len(listed), before + 1)
        self.assertEqual([str(request.url) for request in self.requests], [f"{CDN}/c1.jpg"])
        self.requests.clear()
        self.run_engine(Carousel, targets=["crs:example_user"])
        self.assertEqual((len(listed), self.requests), (before + 1, []))

    def test_failed_file_is_retried_directly_under_sync(self):
        class Feed(Source):
            code, name, domains = "fed", "Feed", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"

            @target("fed:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("new1", [File(f"{CDN}/new1.jpg")], date="2026-02-01")
                yield Post("old1", [File(f"{CDN}/old1.jpg")], date="2026-01-01")

        self.routes[f"{CDN}/old1.jpg"] = _net.reply(500)
        self.run_engine(Feed, targets=["fed:example_user"])
        self.assertIn("fed:new1_0", self.history())
        self.assertNotIn("fed:old1_0", self.history())

        del self.routes[f"{CDN}/old1.jpg"]
        self.requests.clear()
        self.run_engine(Feed, targets=["fed:example_user"], options=Options(stop_at_saved=True))
        self.assertIn("fed:old1_0", self.history())
        self.assertEqual([str(r.url) for r in self.requests], [f"{CDN}/old1.jpg"])

    def test_a_file_queued_twice_is_downloaded_once(self):
        class Twice(Source):
            code, name, domains = "twc", "Twice", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"

            @target("twc:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("1", [File(f"{CDN}/1.jpg")])
                yield Post("1", [File(f"{CDN}/1.jpg")])

        self.assertEqual(self.run_engine(Twice, targets=["twc:example_user"]), 1)
        self.assertFalse(any(line.startswith(". skip ") for line in self.lines()), self.lines())

    def test_a_file_in_two_categories_is_downloaded_once(self):
        class Both(Source):
            code, name, domains = "bth", "Both", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"

            @target("bth:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("1", [File(f"{CDN}/1.jpg")])

            @user.category
            def reels(self, site, user):
                yield Post("1", [File(f"{CDN}/1.jpg")])
                yield Post("2", [File(f"{CDN}/2.jpg")])

        self.assertEqual(self.run_engine(Both, targets=["bth:example_user"]), 2)
        # Both categories name it `1_0` (their folders differ), so the second is skipped as queued.
        self.assertEqual(len(self.fetched(f"{CDN}/1.jpg")), 1)
        self.assertEqual(sorted(self.history()), ["bth:1_0", "bth:2_0"])
        # The second category's folder holds a link to the first one's file.
        self.assertEqual(self.files("bth"), ["posts/1.jpg", "reels/1.jpg", "reels/2.jpg"])
        self.assertTrue((self.target_dir("bth", "example_user") / "reels" / "1.jpg").is_symlink())
        # A later run finds both saved, whichever category lists them.
        self.requests.clear()
        self.assertEqual(self.run_engine(Both, targets=["bth:example_user"]), 0)
        self.assertEqual([request for request in self.requests if request.url.host == "cdn.example"], [])

    def test_two_files_with_one_path_are_downloaded_once(self):
        class Same(Source):
            code, name, domains = "sme", "Same", ("example.com",)
            api = 1
            folder = ""
            filename = "photo"

            @target("sme:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("1", [File(f"{CDN}/1.jpg")])

            @user.category
            def reels(self, site, user):
                yield Post("2", [File(f"{CDN}/2.jpg")])

        # Two keys, one path: the second file would overwrite the first, so it is skipped as queued.
        self.assertEqual(self.run_engine(Same, targets=["sme:example_user"]), 1)
        self.assertEqual(self.files("sme"), ["photo.jpg"])
        self.assertEqual(self.fetched(f"{CDN}/2.jpg"), [])


class FilesTest(JobCase):
    def test_text_files_are_written_whole_and_unkeyed(self):
        class Notes(Source):
            code, name, domains = "nts", "Notes", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"
            folder = "{post}"

            @target("nts:{user}")
            def user(self, site, user):
                yield Post("5930", [File.text("info.md", "# Title\n"), File([f"{CDN}/a.7z", f"{CDN}/b.7z"],
                                                                            name="loi-5930.7z")])

        self.assertEqual(self.run_engine(Notes, targets=["nts:example_user"]), 1)
        folder = self.target_dir("nts", "example_user")
        self.assertEqual((folder / "5930" / "info.md").read_bytes(), b"# Title\n")
        self.assertEqual((folder / "5930" / "loi-5930.7z").read_bytes(), b"/a.7z")
        self.assertEqual(sorted(self.history()), ["nts:5930_1"])

    def test_a_url_without_an_extension_is_named_by_its_content(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
        self.routes[f"{CDN}/blob"] = _net.reply(200, content=png)

        class Blob(Source):
            code, name, domains = "blb", "Blob", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"

            @target("blb:{user}")
            def user(self, site, user):
                yield Post("9", [File(f"{CDN}/blob")], date=1767312000)

        self.run_engine(Blob, targets=["blb:example_user"])
        self.assertEqual(self.files("blb"), ["2026-01-02_9.png"])

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

    def test_the_history_row_has_its_path_size_and_time_and_a_first_file_its_posts_count(self):
        self.run_engine(Example, targets=["exm:example_user"], options=Options(categories=("posts",)))
        row = self.history()["exm:C1abc_0"]
        saved = self.target_dir("exm", "example_user") / "posts" / "2026-01-02_C1abc_0.jpg"
        self.assertEqual(sorted(row), ["at", "before", "category", "date", "estimated", "files", "folder", "hash", "key", "kind", "mtime", "path",
                          "post_id", "shrink", "sidecar", "size", "state", "target", "tools", "unpacked"])
        self.assertEqual(sorted(self.history()["exm:B2def_0"]),
                         ["at", "before", "category", "date", "estimated", "files", "folder", "hash", "key", "kind", "mtime", "path",
                          "post_id", "shrink", "sidecar", "size", "state", "target", "tools", "unpacked"])
        self.assertEqual((row["path"], row["size"]), (str(saved), len(b"/C1abc_0.jpg")))
        written = datetime.datetime.fromisoformat(row["at"])
        self.assertEqual(written.utcoffset(), datetime.timedelta(0))
        self.assertEqual(written.microsecond, 0)
        self.assertLess(abs(datetime.datetime.now(datetime.UTC) - written),
                        datetime.timedelta(minutes=1))
        # Keys are `<code>:<key>`, one per file, a text file none.
        self.assertEqual(sorted(self.history()), ["exm:B2def_0", "exm:C1abc_0", "exm:C1abc_1"])


class PlaceTest(unittest.TestCase):
    def test_two_files_of_one_post_with_one_name_get_two_paths(self):
        files = [File(f"{CDN}/a/cover.jpg", name="cover.jpg"), File(f"{CDN}/b/cover.jpg", name="cover.jpg"),
                 File(f"{CDN}/c/cover.jpg", name="cover.jpg")]
        post = Post("p1", files, date="2026-01-02").filed("posts")
        paths = [placed.path for placed in job.place(Example(), post, files)]
        self.assertEqual(paths, ["posts/cover.jpg", "posts/cover_1.jpg", "posts/cover_2.jpg"])


class TimeTest(unittest.TestCase):
    """`--time` and the `newest_first` early stop, on a `FakeSite`: nothing is downloaded."""

    def listed(self, source, options):
        target = Target(f"{source.code}:example_user", kind="user")
        site = FakeSite(target, options=options)
        return [post.id for post in job.posts(source(), site, target, options, run_async=lambda coro: None)]

    def feed(self, newest_first, finished):
        days = [2020, 2021, 2022, 2026, 2026, 2025, 2025, 2025, 2025, 2025, 2025, 2024]

        class Feed(Source):
            code, name, domains = "fed", "Feed", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"

            @target("fed:{user}")
            def user(self, site, user):
                return user

            @user.category(newest_first=newest_first)
            def posts(self, site, user):
                try:
                    for number, year in enumerate(days):
                        yield Post(f"{number}_{year}", date=datetime.date(year, 6, 1))
                finally:
                    finished.append(True)

        return Feed

    def test_a_newest_first_feed_stops_after_5_older_posts_in_a_row_and_its_finally_runs(self):
        finished = []
        # Three old pinned posts first don't stop it; five older in a row do, before the 2024 one.
        self.assertEqual(self.listed(self.feed(True, finished), Options(since=date(2026, 1, 1))), ["3_2026", "4_2026"])
        self.assertEqual(finished, [True])

    def test_any_other_feed_is_read_to_its_end(self):
        finished = []
        self.assertEqual(self.listed(self.feed(False, finished), Options(since=date(2026, 1, 1))), ["3_2026", "4_2026"])
        self.assertEqual(finished, [True])

    def test_until_and_undated_posts(self):
        class Dated(Source):
            code, name, domains = "dtd", "Dated", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"

            @target("dtd:{user}")
            def user(self, site, user):
                yield Post("late", date="2026-07-01")
                yield Post("undated")
                yield Post("in", date=datetime.datetime(2026, 3, 1, 23, 59))

        self.assertEqual(self.listed(Dated, Options(until=date(2026, 6, 30))), ["undated", "in"])


class SyncStopTest(JobCase):
    """`sync` stops a `newest_first` category after 5 saved posts in a row, only once its listing has reached its end.

    The engine's sooner stop, at the head an earlier run saw, is `HeadTest`'s; these tests forget the head first
    (`forget_head`), so the run of 5 is what stops them.
    """

    def setUp(self):
        super().setUp()
        self.ids = [f"p{number}" for number in range(8)]
        self.listed, self.finished, self.fail_at = [], [], []
        # Files of a post that has other than its one picture, by id.
        self.post_files: dict[str, list[File]] = {}
        test = self

        class Feed(Source):
            code, name, domains = "fed", "Feed", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"
            key = "{post}"

            @target("fed:{user}")
            def user(self, site, user):
                return user

            @user.category(newest_first=True)
            def posts(self, site, user):
                try:
                    for post_id in list(test.ids):
                        if post_id in test.fail_at:
                            raise SiteError("cut")
                        test.listed.append(post_id)
                        files = test.post_files.get(post_id, [File(f"{CDN}/{post_id}.jpg")])
                        yield Post(post_id, files, date=datetime.date(2026, 1, 1))
                finally:
                    test.finished.append(True)

        self.engine = Engine(home=self.home, sources={"fed": Feed})

    def record(self):
        row = fakes.targets(self.home).get("fed:example_user")
        return (row.complete or None) if row is not None else None

    def forget_record(self):
        self.sql("UPDATE targets SET complete = '{}' WHERE key = ?", "fed:example_user")

    def forget_head(self):
        self.sql("UPDATE targets SET heads = '{}' WHERE key = ?", "fed:example_user")

    def test_sync_stops_after_5_saved_posts_in_a_row_with_a_record_and_closes_the_listing(self):
        self.assertTrue(self.engine.download(["fed:example_user"], categories="all").ok)
        self.assertEqual(self.record(), {"posts": "2026-01-01"})
        self.ids = ["n1", "n2", *self.ids]
        self.listed.clear()
        self.finished.clear()
        self.forget_head()
        report = self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, ["n1", "n2", "p0", "p1", "p2", "p3", "p4"])
        self.assertEqual(self.finished, [True])
        self.assertEqual(sorted(path.name for path in report.targets[0].written), ["2026-01-01_n1.jpg", "2026-01-01_n2.jpg"])
        # It stopped early, so the record is the earlier one, still there.
        self.assertIn("posts", self.record())

    def test_sync_keeps_complete_record_and_fast_completes_on_subsequent_runs(self):
        """A caught-up sync stays complete and stops after 5 saved posts on subsequent runs."""
        self.assertTrue(self.engine.download(["fed:example_user"], categories="all").ok)
        self.assertEqual(self.record(), {"posts": "2026-01-01"})
        self.listed.clear()
        self.forget_head()
        report = self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, ["p0", "p1", "p2", "p3", "p4"])
        self.assertEqual(report.files, 0)
        self.assertIn("posts", self.record())
        self.assertEqual(fakes.targets(self.home)["fed:example_user"].cursors, {})
        self.listed.clear()
        self.forget_head()
        report2 = self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, ["p0", "p1", "p2", "p3", "p4"])
        self.assertEqual(report2.files, 0)

    def test_files_queued_by_an_earlier_category_are_not_caught_up(self):
        """fixes #4: a second feed listing the first one's 6 new posts goes on past them."""
        test = self

        class Two(Source):
            code, name, domains = "two", "Two", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"
            key = "{post}"

            @target("two:{user}")
            def user(self, site, user):
                return user

            @user.category(newest_first=True)
            def posts(self, site, user):
                for post_id in test.ids:
                    yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=datetime.date(2026, 1, 1))

            @user.category(newest_first=True)
            def media(self, site, user):
                # The new posts `posts` queued, then one only `media` has, then the saved ones.
                for post_id in [*test.ids[:6], *test.deeper, *test.ids[6:]]:
                    test.listed.append(post_id)
                    yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=datetime.date(2026, 1, 1))

        self.deeper = []
        engine = Engine(home=self.home, sources={"two": Two})
        self.assertTrue(engine.download(["two:example_user"], categories="all").ok)
        self.ids = [f"n{number}" for number in range(6)] + self.ids
        self.deeper = ["m1"]
        self.listed.clear()
        report = engine.sync(["two:example_user"])
        self.assertIn("m1", self.listed)
        self.assertIn("2026-01-01_m1.jpg", [path.name for path in report.targets[0].written])

    def test_posts_with_no_files_or_only_text_neither_count_nor_break_the_saved_run(self):
        """A reply with nothing to save, between saved posts, must not reset the run of 5 (twt replies)."""
        self.assertTrue(self.engine.download(["fed:example_user"], categories="all").ok)
        text = File.text("note.txt", "a note")
        # Saved posts with a no-file post and a text-only post between them: 5 saved still end it.
        self.ids = ["p0", "none1", "p1", "text1", "p2", "none2", "p3", "p4", "p5", "p6", "p7"]
        self.post_files = {"none1": [], "none2": [], "text1": [text]}
        self.listed.clear()
        self.forget_head()
        self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, ["p0", "none1", "p1", "text1", "p2", "none2", "p3", "p4"])
        self.assertEqual(self.finished[-1], True)

    def test_a_saved_run_shorter_than_5_does_not_stop_it(self):
        self.engine.download(["fed:example_user"], categories="all")
        # Remove two saved files in the middle: the 4 saved ones before it never reach 5, then a new one resets.
        for name in ("p2", "p3"):
            next(self.target_dir("fed", "example_user").rglob(f"*{name}.jpg")).unlink()
        for name in ("p2", "p3"):
            self.sql("DELETE FROM files WHERE key = ?", f"fed:{name}")
        self.listed.clear()
        self.forget_head()
        report = self.engine.sync(["fed:example_user"])
        self.assertEqual(len(report.targets[0].written), 2)
        # p0, p1 saved, p2 and p3 fetched again, then p4..p7: 4 saved in a row never stop it.
        self.assertEqual(self.listed, self.ids)

    def test_without_a_record_sync_lists_to_the_end(self):
        self.engine.download(["fed:example_user"], categories="all")
        self.forget_record()
        self.listed.clear()
        self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, self.ids)
        # The listing reached its end, so it is recorded now, and the next sync stops.
        self.assertIsNotNone(self.record())
        self.listed.clear()
        self.forget_head()
        self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, self.ids[:5])

    def test_an_interrupted_listing_records_none_so_sync_lists_everything(self):
        self.fail_at = ["p6"]
        report = self.engine.download(["fed:example_user"], categories="all")
        self.assertFalse(report.ok)
        self.assertIsNone(self.record())
        self.fail_at, self.listed = [], []
        self.engine.sync(["fed:example_user"])
        # The first 6 were saved, but nothing says the feed was ever read to its end.
        self.assertEqual(self.listed, self.ids)
        self.assertIsNotNone(self.record())

    def test_download_stops_at_saved_posts_by_default_and_again_lists_all(self):
        self.engine.download(["fed:example_user"], categories="all")
        self.listed.clear()
        self.forget_head()
        report = self.engine.download(["fed:example_user"], categories="all")
        self.assertEqual(self.listed, self.ids[:5])
        self.assertEqual((report.files, report.targets[0].skipped), (0, 5))
        self.listed.clear()
        report_again = self.engine.download(["fed:example_user"], categories="all", again=True)
        self.assertEqual(self.listed, self.ids)
        self.assertEqual((report_again.files, report_again.targets[0].skipped), (0, 8))

    def test_stop_after_in_the_config_changes_the_run_of_5(self):
        self.engine.download(["fed:example_user"], categories="all")
        self.home.joinpath("config.toml").write_text("[sources.fed]\nstop_after = 3\n")
        self.listed.clear()
        self.forget_head()
        self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, self.ids[:3])
        self.home.joinpath("config.toml").write_text("[sources.fed]\nstop_after = 0\n")
        # A bad setting stops the whole run as a usage error, before the source sends anything.
        with self.assertRaises(UsageError) as caught:
            self.engine.sync(["fed:example_user"])
        self.assertIn("[sources.fed] stop_after is a whole number of at least 1", str(caught.exception))

    def test_a_feed_that_is_not_newest_first_is_never_stopped(self):
        listed = []

        class Plain(Source):
            code, name, domains = "pln", "Plain", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"

            @target("pln:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                for number in range(8):
                    listed.append(number)
                    yield Post(f"q{number}", [File(f"{CDN}/q{number}.jpg")])

        engine = Engine(home=self.home, sources={"pln": Plain})
        engine.download(["pln:example_user"], categories="all")
        listed.clear()
        engine.sync(["pln:example_user"])
        self.assertEqual(len(listed), 8)


class AsyncTest(JobCase):
    def test_async_routes_and_categories_run_on_the_sources_loop_with_no_handover_thread(self):
        threads = []

        class Chats(Source):
            code, name, domains = "cht", "Chats", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"
            limits = {"cdn.example": 1}

            @target("cht:{chat}")
            async def chat(self, site, chat):
                threads.append(threading.current_thread().name)
                return chat

            @chat.category(newest_first=True)
            async def photo(self, site, chat):
                for number in range(4):
                    threads.append(threading.current_thread().name)
                    yield Post(str(number), [File(f"{CDN}/{chat}/{number}.jpg")], date=1767312000 - number)

        self.assertEqual(self.run_engine(Chats, targets=["cht:examplechannel"]), 4)
        self.assertEqual(set(threads), {"flnd-loop"})
        self.assertFalse([thread for thread in threading.enumerate() if thread.name.endswith("-saves")])


class FailureTest(JobCase):
    def test_a_category_that_raises_fails_the_target(self):
        class Broken(Source):
            code, name, domains = "brk", "Broken", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"

            @target("brk:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("1", [File(f"{CDN}/1.jpg")])
                raise RuntimeError("the feed broke")

        self.run_engine(Broken, targets=["brk:example_user"])
        # A bug in a listing is a SourceError: its message names it, its hint says what to do.
        self.assertEqual(self.failures(), ["brk:example_user: unexpected RuntimeError: the feed broke"])
        self.assertEqual(self.events[-1].error.hint, "this is a bug; run with -v and report it")
        report = self.report(Broken, targets=["brk:example_user"])
        self.assertEqual(report.failed, (Target("brk:example_user", kind="user"),))
        self.assertIsInstance(report.targets[0].error, SourceError)
        self.assertIsInstance(report.targets[0].error.__cause__, RuntimeError)


    def test_a_site_error_fails_only_its_target_and_a_login_error_stops_the_source(self):
        class Errors(Source):
            code, name, domains = "err", "Errors", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"
            folder = ""

            @target("err:{user}")
            def user(self, site, user):
                if user == "gone":
                    raise NotFound("HTTP 404 for https://example.com/gone", status=404)
                if user == "locked":
                    raise LoginError("err: the saved login expired", hint="run flnd login err")
                return user

            @user.category
            def posts(self, site, user):
                if user == "cut":
                    raise SiteError("HTTP 500 for https://example.com/cut", status=500)
                yield Post(user, [File(f"{CDN}/{user}.jpg")])

        names = ["gone", "cut", "good", "locked", "last"]
        report = self.report(Errors, targets=[f"err:{name}" for name in names])
        self.assertEqual(report.files, 1)
        self.assertEqual([target.value for target in report.failed], ["gone", "cut", "locked", "last"])
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


class Cut:
    """A body that sends `data`, then drops the connection; or, given `gate`, waits on it after the first chunk.

    An async iterable, so its wait on `gate` holds up only this transfer, not the run's loop.
    """

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
    """B6: a 403 or 410 on a file of a lazy post asks its `files` once more and resumes with the fresh link."""

    BODY = b"0123456789"
    PHOTO = f"{CDN}/A1/1.jpg"

    def setUp(self):
        super().setUp()
        # Each listing of the album signs its photo's link anew, as Instagram's `oe=` links are.
        self.listings = 0

        def album(request):
            self.listings += 1
            return _net.reply(200, json={"urls": [f"{self.PHOTO}?sig={self.listings}"]})

        self.routes[f"{API}/albums/A1"] = album

    def cdn(self, fresh):
        """The photo's CDN: the first link sends half the file, then the connection drops and the link expires."""
        def photo(request):
            sig = request.url.params["sig"]
            headers = {"ETag": '"p1"', "Accept-Ranges": "bytes"}
            if sig == "1":
                range_header = request.headers.get("range", "")
                if range_header and not range_header.startswith("bytes=0-"):
                    return _net.reply(403)
                return _net.reply(200, headers={**headers, "Content-Length": "10"}, chunks=Cut(self.BODY))
            return fresh(request, headers)

        self.routes[self.PHOTO] = photo

    def test_an_expired_link_calls_lazy_files_once_more_and_resumes(self):
        def fresh(request, headers):
            start = int(request.headers["range"].removeprefix("bytes=").split("-")[0])
            return _net.reply(206, content=self.BODY[start:],
                                  headers={**headers, "Content-Range": f"bytes {start}-9/10"})

        self.cdn(fresh)
        self.run_engine(Example, targets=["exm:example_user"], options=Options(categories=("albums",)))
        self.assertEqual((self.target_dir("exm", "example_user") / "albums/Summer_2026/A1.jpg").read_bytes(),
                         self.BODY)
        self.assertEqual(self.listings, 2)
        last = self.fetched(self.PHOTO)[-1]
        self.assertEqual(last.url.params["sig"], "2")
        # The second link resumes where the first stopped, checked against the same file.
        self.assertTrue(last.headers["range"].startswith("bytes=5-"), last.headers["range"])
        self.assertIn("exm:A1_0", self.history())
        self.assertEqual([path.name for path in (self.target_dir("exm", "example_user") / "albums/Summer_2026").iterdir()],
                         ["A1.jpg"])

    def test_once_per_file_never_in_a_loop(self):
        self.cdn(lambda request, headers: _net.reply(410))
        self.run_engine(Example, targets=["exm:example_user"], options=Options(categories=("albums",)))
        self.assertEqual(self.listings, 2)
        self.assertIn("A1.jpg: GET https://cdn.example/A1/1.jpg: HTTP 410", self.failures())
        self.assertNotIn("exm:A1_0", self.history())

    def test_a_first_ctrl_c_during_a_refresh_is_no_fresh_link_and_the_target_stops(self):
        """A rate-limit wait inside `files` that the first Ctrl-C cuts short is the graceful stop, not a failure."""
        stop = _run.Stop()
        self.cdn(lambda request, headers: _net.reply(500))

        def album(request):
            self.listings += 1
            if self.listings > 1:
                stop.soon.set()      # the first Ctrl-C, during the refresh's wait
                raise KeyboardInterrupt
            return _net.reply(200, json={"urls": [f"{self.PHOTO}?sig=1"]})

        self.routes[f"{API}/albums/A1"] = album
        report = self.report(Example, targets=["exm:example_user"], options=Options(categories=("albums",)), stop=stop)
        self.assertEqual(str(report.targets[0].error), job.STOPPED)
        self.assertEqual(self.listings, 2)

    def test_a_refresh_cut_by_the_first_ctrl_c_answers_no_link_and_one_by_the_second_raises(self):
        stop = _run.Stop()

        def files():
            raise KeyboardInterrupt

        stop.soon.set()
        refresh = job._Refresh(Post("A1", files), stop)
        refresh.run()
        self.assertTrue(refresh.done.is_set())
        self.assertEqual((refresh.files, refresh.error), (None, None))
        stop.now.set()
        second = job._Refresh(Post("A1", files), stop)
        with self.assertRaises(KeyboardInterrupt):
            second.run()
        self.assertTrue(second.done.is_set())

    def test_a_post_with_its_files_listed_is_not_refreshed(self):
        self.routes[f"{CDN}/C1abc_0.jpg"] = _net.reply(403)
        self.run_engine(Example, targets=["exm:example_user"], options=Options(categories=("posts",)))
        self.assertEqual(len(self.fetched(f"{CDN}/C1abc_0.jpg")), 1)
        self.assertEqual(len(self.fetched(f"{API}/users/42/posts")), 2)
        self.assertIn("2026-01-02_C1abc_0.jpg: GET https://cdn.example/C1abc_0.jpg: HTTP 403", self.failures())


class CtrlCTest(JobCase):
    """D8: the first Ctrl-C stops listing and lets running downloads finish; the second stops them now.

    The run goes on a thread of its own, as the engine's groups do, and the test thread plays the
    person, setting `net.stopping` and `net.stopping_now` as the SIGINT handler does; the CDN blocks
    on events, so the test decides when each download moves.
    """

    def setUp(self):
        super().setUp()
        self.stop = _run.Stop()
        self.boards = []
        real = job.download

        def spy(run, target, board, options):
            self.boards.append((board, target))
            return real(run, target, board, options)

        patcher = mock.patch.object(job, "download", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        listed, closed, started, release = [], threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class Slow(Source):
            code, name, domains = "slw", "Slow", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"
            folder = ""
            # One download at a time: post 1's runs, posts 2 to 4 wait in the queue (4 times as
            # many as run, with the target's places held to 1 below).
            limits = {"cdn.example": 1}

            @target("slw:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                try:
                    for number in range(1, 100):
                        listed.append(number)
                        # Dated, so each post is queued as it is listed, not held for its date.
                        yield Post(str(number), [File(f"{CDN}/{number}.7z", name=f"{number}.7z")],
                                   date=datetime.date(2026, 1, 1))
                finally:
                    closed.set()

        async def first(request):
            started.set()
            await asyncio.to_thread(release.wait, 5)
            return _net.reply(200, content=b"one")

        self.routes[f"{CDN}/1.7z"] = first
        # A target's places follow the most a host's cap grows to, which would queue 128 files.
        self.enterContext(mock.patch.object(site_module, "MEDIA_MOST", 1))
        thread, outcome = self.start(Slow, "slw:example_user")
        # Post 5 waits for room in the queue: 1 is downloading and 2 to 4 are queued.
        self.until(lambda: started.is_set() and len(listed) == 5)
        self.stop.soon.set()
        self.until(closed.is_set)
        finishing = "stopping: 1 file finishing (Ctrl-C again to stop now)"
        self.until(lambda: self.boards and self.note(*self.boards[0]) == finishing)
        release.set()
        thread.join(10)
        report = outcome["report"]
        self.assertEqual(str(report.targets[0].error), job.STOPPED)
        self.assertEqual(self.files("slw"), ["2026-01-01_1.7z"])
        self.assertIn("slw:1_0", self.history())
        # Posts 2 to 4 were queued but not started, so they were dropped; nothing past post 5 was
        # listed.
        self.assertEqual(self.fetched(f"{CDN}/2.7z"), [])
        self.assertEqual(listed, [1, 2, 3, 4, 5])

    def note(self, board, target):
        """The note under the target's posts line, or None before it opens."""
        line = board._lines.get((target, "posts"))
        return None if line is None else line.note

    def test_the_second_ctrl_c_stops_now_and_keeps_the_part(self):
        sent, gate = threading.Event(), threading.Event()

        class Big(Source):
            code, name, domains = "big", "Big", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"
            folder = ""

            @target("big:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("1", [File(f"{CDN}/big.7z", name="big.7z")])

        body = b"0123456789"

        def big(request):
            range_header = request.headers.get("range", "")
            if range_header and not range_header.startswith("bytes=0-"):
                start = int(range_header.removeprefix("bytes=").split("-")[0])
                return _net.reply(206, content=body[start:],
                                      headers={"ETag": '"b1"', "Content-Range": f"bytes {start}-9/10"})
            return _net.reply(200, headers={"ETag": '"b1"', "Content-Length": "10"},
                                  chunks=Cut(body, gate=gate, sent=sent))

        self.routes[f"{CDN}/big.7z"] = big
        self.addCleanup(gate.set)
        thread, outcome = self.start(Big, "big:example_user")
        self.until(sent.is_set)
        self.stop.soon.set()
        self.stop.now.set()
        # The next chunk arrives and isn't written: the download stops before it, keeping the first.
        gate.set()
        thread.join(10)
        self.assertIsInstance(outcome.get("error"), KeyboardInterrupt)
        folder = self.target_dir("big", "example_user")
        names = sorted(path.name for path in folder.iterdir())
        # The part, and beside it the ETag its bytes came under.
        self.assertEqual(names, ["big.7z.part", "big.7z.part.json"])

        # The next run resumes the part, sending the ETag so a changed file comes whole.
        self.stop.soon.clear()
        self.stop.now.clear()
        self.run_engine(Big, targets=["big:example_user"])
        self.assertEqual((folder / "big.7z").read_bytes(), body)
        last = self.fetched(f"{CDN}/big.7z")[-1]
        self.assertTrue(last.headers["range"].startswith("bytes=5-"))
        self.assertEqual(last.headers["if-range"], '"b1"')
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["big.7z"])

    def test_two_runs_on_one_home_save_a_file_once(self):
        """A second run finds the file's part locked, skips it, and the first run writes its row."""
        sent, gate = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        class Big(Source):
            code, name, domains = "big", "Big", ("example.com",)
            api = 1
            filename = "{date}_{post}{index}"
            folder = ""

            @target("big:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("1", [File(f"{CDN}/big.7z", name="big.7z")])

        body = b"0123456789"
        self.routes[f"{CDN}/big.7z"] = lambda request: _net.reply(
            200, headers={"Content-Length": "10"}, chunks=Cut(body, gate=gate, sent=sent))
        thread, outcome = self.start(Big, "big:example_user")
        self.until(sent.is_set)
        # The first run holds the part, half written: the second run, on the same home, skips it.
        second = self.report(Big, targets=["big:example_user"])
        self.assertEqual(second.files, 0)
        self.assertIn("big.7z: being saved by another run", self.dest.getvalue())
        gate.set()
        thread.join(10)
        self.assertEqual(outcome["report"].files, 1)
        folder = self.target_dir("big", "example_user")
        self.assertEqual((folder / "big.7z").read_bytes(), body)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["big.7z"])
        self.assertEqual(list(self.history()), ["big:1_0"])
        self.assertEqual(len(self.fetched(f"{CDN}/big.7z")), 1)


class HeadTest(JobCase):
    """Smart sync, the head: a sync stops at the newest post an earlier run saw, and the post after it."""

    def setUp(self):
        super().setUp()
        # Each post's day: a later one is newer; a pinned post sits on top with an older day.
        self.posts = [("p3", 3), ("p2", 2), ("p1", 1), ("p0", 1)]
        self.listed = []
        test = self

        class Feed(Source):
            code, name, domains, api, key = "hed", "Heads", ("example.com",), 1, "{post}"

            @target("hed:{user}")
            def user(self, site, user):
                return user

            @user.category(newest_first=True)
            def posts(self, site, user):
                for post_id, day in list(test.posts):
                    test.listed.append(post_id)
                    yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=datetime.date(2026, 1, day))

        self.engine = Engine(home=self.home, sources={"hed": Feed})

    def heads(self):
        return fakes.targets(self.home)["hed:example_user"].heads

    def test_a_full_listing_keeps_its_newest_post_and_a_sync_stops_after_it(self):
        self.assertTrue(self.engine.download(["hed:example_user"], categories="all").ok)
        self.assertEqual(self.heads(), {"posts": "p3"})
        self.listed.clear()
        report = self.engine.sync(["hed:example_user"])
        # The head and the post after it: nothing new, two posts read, not five.
        self.assertEqual(self.listed, ["p3", "p2"])
        self.assertEqual(report.files, 0)
        self.assertIn("* posts: nothing new", self.lines())

    def test_new_posts_above_the_head_are_saved_and_become_the_head(self):
        self.engine.download(["hed:example_user"], categories="all")
        self.posts = [("n1", 5), ("n0", 4), *self.posts]
        self.listed.clear()
        report = self.engine.sync(["hed:example_user"])
        self.assertEqual(self.listed, ["n1", "n0", "p3", "p2"])
        self.assertEqual(sorted(path.stem for path in report.targets[0].written), ["n0_0", "n1_0"])
        self.assertEqual(self.heads(), {"posts": "n1"})

    def test_a_head_pinned_above_new_posts_does_not_end_the_sync(self):
        self.engine.download(["hed:example_user"], categories="all")
        # The site pinned the old head above two new posts.
        self.posts = [("p3", 3), ("n1", 5), ("n0", 4), ("p2", 2), ("p1", 1), ("p0", 1)]
        self.listed.clear()
        report = self.engine.sync(["hed:example_user"])
        self.assertEqual(sorted(path.stem for path in report.targets[0].written), ["n0_0", "n1_0"])
        # The newest post by its day is the head now, not the pinned one listed first.
        self.assertEqual(self.heads(), {"posts": "n1"})

    def test_parts_of_one_post_count_once(self):
        # A source yielding a photo post as one post per photo, all of one id (TikTok's).
        self.posts = [("c1", 5), ("c1", 5), ("c1", 5), ("p3", 3)]
        report = self.engine.download(["hed:example_user"], categories="all", limit=1)
        self.assertEqual(self.listed, ["c1", "c1", "c1", "p3"])
        self.assertEqual(len(report.targets[0].written), 1)

    def test_a_listing_cut_short_keeps_the_head_it_had(self):
        self.engine.download(["hed:example_user"], categories="all")
        self.posts = [("n1", 5), *self.posts]
        self.engine.download(["hed:example_user"], categories="all", limit=1)
        self.assertEqual(self.heads(), {"posts": "p3"})


class UnkeyedLazyPostTest(JobCase):
    """A lazy post keyed by its files' paths (LOIBUS's archives) isn't listed again once saved."""

    def test_its_files_function_runs_once(self):
        calls = []

        class Paths(Source):
            code, name, domains, api, key = "pth", "Paths", ("example.com",), 1, ""

            @target("pth:{user}")
            def user(self, site, user):
                return user

            @user.category
            def archive(self, site, user):
                def files():
                    calls.append(user)
                    return [File(f"{CDN}/a.7z", name="a.7z")]

                yield Post("5930", files)

        engine = Engine(home=self.home, sources={"pth": Paths})
        engine.download(["pth:example_user"], categories="all")
        engine.download(["pth:example_user"], categories="all")
        self.assertEqual(calls, ["example_user"])
        # `--again` asks again.
        engine.download(["pth:example_user"], categories="all", again=True)
        self.assertEqual(len(calls), 2)


class ResumeTest(JobCase):
    """An interrupted listing goes on from its saved cursor, then reads the newest posts."""

    def setUp(self):
        super().setUp()
        # Pages by cursor: None is the newest; each page holds its posts and the next page's cursor.
        self.feed: dict[str | None, tuple[list[str], str | None]] = {
            None: (["p1", "p2", "p3", "p4", "p5"], "c1"),
            "c1": (["p6", "p7"], "c2"),
            "c2": (["p8", "p9"], None),
        }
        self.cursors_fetched: list[str | None] = []
        self.fail_on: str | None = None
        self.refuse = False
        test = self

        class Paged(Source):
            code, name, domains, key = "pgd", "Paged", ("example.com",), "{post}"
            api = 1
            filename = "{date}_{post}{index}"

            @target("pgd:{user}")
            def user(self, site, user):
                return user

            @user.category(newest_first=True)
            def posts(self, site, user):
                # `site.pages` sends the cursor as `cursor=`, and the engine resumes it.
                for page in site.pages(f"{API}/feed", cursor="next"):
                    for post_id in page["ids"]:
                        yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")],
                                   date=datetime.date(2026, 1, 1))

        def feed(request):
            cursor = dict(request.url.params).get("cursor")
            test.cursors_fetched.append(cursor)
            if cursor is not None and test.refuse:
                return _net.reply(400, json={"error": "stale cursor"})
            if test.fail_on is not None and cursor == test.fail_on:
                return _net.reply(500, json={"error": "connection reset"})
            ids, following = test.feed[cursor]
            return _net.reply(200, json={"ids": ids, "next": following})

        self.routes[f"{API}/feed"] = feed
        self.engine = Engine(home=self.home, sources={"pgd": Paged})

    def row(self):
        return fakes.targets(self.home)["pgd:example_user"]

    def interrupted(self):
        self.fail_on = "c2"
        self.assertFalse(self.engine.download(["pgd:example_user"], categories="all").ok)
        self.assertEqual(self.row().cursors, {"posts": "c2"})
        self.assertEqual(self.row().complete, {})
        self.fail_on, self.cursors_fetched = None, []

    def test_the_next_run_starts_at_the_saved_cursor_then_reads_the_newest(self):
        self.interrupted()
        # Two new posts arrived meanwhile, above the ones the first run saved.
        self.feed[None] = (["n1", "n2"], "c0")
        self.feed["c0"] = (["p1", "p2", "p3", "p4", "p5"], "c1")
        report = self.engine.download(["pgd:example_user"], categories="all")
        self.assertTrue(report.ok)
        # c2 to the end first, then the newest. Which of p1..p7 the failed run saved depends on
        # which downloads had started, so only the posts this run must save are checked.
        self.assertEqual(self.cursors_fetched[:3], ["c2", None, "c0"])
        folder = self.target_dir("pgd", "example_user")
        saved = {path.stem.split("_")[-1] for path in folder.rglob("*.jpg")}
        self.assertLessEqual({"n1", "n2", "p8", "p9"}, saved)
        self.assertEqual(self.row().cursors, {})
        self.assertIn("posts", self.row().complete)

    def test_a_cursor_kept_for_a_listing_read_to_its_end_is_a_gap(self):
        self.interrupted()
        # Every post the failed run listed is saved meanwhile, as a later run would have.
        self.fail_on = "c2-unused"
        self.engine.download(["pgd:example_user"], categories="all")
        self.feed[None] = (["n1", "n2"], "c0")
        self.feed["c0"] = (["p1", "p2", "p3", "p4", "p5"], "c1")
        # An earlier flnd kept a cut-short sync's cursor as the listing's cursor.
        self.sql("UPDATE targets SET cursors = ? WHERE key = ?", '{"posts":"c2"}', "pgd:example_user")
        self.cursors_fetched = []
        self.engine.download(["pgd:example_user"], categories="all")
        # The newest down to 5 saved posts in a row, then the gap.
        self.assertEqual(self.cursors_fetched, [None, "c0", "c2"])
        self.assertEqual((self.row().cursors, self.row().gaps), ({}, {}))

    def test_a_refused_cursor_is_dropped_and_the_listing_starts_over(self):
        self.interrupted()
        self.refuse = True
        self.feed = {None: (["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9"], None)}
        report = self.engine.download(["pgd:example_user"], categories="all")
        self.assertTrue(report.ok)
        self.assertEqual(self.cursors_fetched, ["c2", None])
        self.assertIn("* pgd:example_user: posts: the cursor an interrupted run saved was refused; "
                      "listing from the newest", self.lines())
        self.assertEqual(self.row().cursors, {})

    def test_again_ignores_saved_cursors_and_complete_listing_from_newest(self):
        self.interrupted()
        report = self.engine.download(["pgd:example_user"], categories="all", again=True)
        self.assertTrue(report.ok)
        self.assertEqual(self.cursors_fetched, [None, "c1", "c2"])
        self.assertEqual(self.row().cursors, {})
        self.assertIn("posts", self.row().complete)
        self.cursors_fetched.clear()
        self.engine.download(["pgd:example_user"], categories="all", again=True)
        self.assertEqual(self.cursors_fetched, [None, "c1", "c2"])


class MemoryTest(JobCase):
    """The listing memory: an undated post waits for its category's end, and takes any day found."""

    def run_feed(self, *categories, hook=None):
        self.run_engine(self.feed(*categories, hook=hook), targets=["mem:example_user"])
        return self.files("mem")

    def feed(self, *categories, hook=None):
        fn = hook

        class Feed(Source):
            code, name, domains = "mem", "Memory", ("example.com",)
            api = 1
            folder, filename, key = "", "{date}_{post}", "{post}_{num}"

            if fn is not None:
                def date(self, site, post):
                    assert fn is not None
                    return fn(post)

            @target("mem:{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield from categories[0]

            @user.category
            def photos(self, site, user):
                yield from categories[1] if len(categories) > 1 else []

        return Feed

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
        with _home.open(self.home) as db:
            db.save([_home.FileRow("mem:B2_0", "mem:example_user", "/gone.jpg", 1, post_id="B2",
                                 at="2026-01-03T00:00:00+00:00", date="2026-01-03")])
        self.assertIn("2026-01-03_B2.jpg", self.run_feed([undated]))

    def test_a_name_the_site_built_from_the_format_takes_the_day_found(self):
        undated = Post("A1", [File(f"{CDN}/a", name="A1.webp")])
        day = datetime.date(2026, 1, 2)
        dated = Post("A1", [File(f"{CDN}/a", name="2026-01-02_A1.webp")], date=day)
        other = Post("B2", [File(f"{CDN}/b", name="cover.jpg")], date=day)
        files = self.run_feed([undated, dated, other])
        self.assertEqual(files, ["2026-01-02_A1.webp", "cover.jpg"])

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
        engine.target_sync(["mem:example_user"], enabled=True)
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


class LinkTest(JobCase):
    """A file found in a second category's folder is linked there, not downloaded again."""

    def feed(self, posts, reels):
        class Tabs(Source):
            code, name, domains, key = "lnk", "Links", ("example.com",), "{post}_{num}"
            api = 1
            filename = "{date}_{post}{index}"

            @target("lnk:{user}")
            def user(self, site, user):
                return user

            @user.category
            def videos(self, site, user):
                yield from posts

            @user.category
            def reels(self, site, user):
                yield from reels

        return Tabs

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

class BoardTest(unittest.TestCase):
    """The run's status lines: one per target and category, ended once listed and done."""

    def setUp(self):
        self.dest = io.StringIO()
        self.enterContext(ui.scope(dest=self.dest, width=200, color=False))
        self.enterContext(_time.use(_time.Fake()))
        self.target = Target("aaa:example_user", kind="user")

    def lines(self):
        return self.dest.getvalue().splitlines()

    def test_a_line_ends_once_listed_and_nothing_is_pending(self):
        with ui.group() as rows:
            board = job.Board(rows, subject_width=18, label_width=6)
            board.start(self.target, "posts")
            board.count(self.target, "posts", found=2, queued=2)
            board.count(self.target, "posts", queued=-2, downloading=2)
            board.listed(self.target, "posts")
            board.count(self.target, "posts", downloading=-1, saved=1, bytes=2048)
            board.count(self.target, "posts", downloading=-1, failed=1)
        self.assertEqual((board.totals(self.target).saved, board.totals(self.target).failed), (1, 1))

    def test_totals_and_ending_a_target(self):
        other = Target("aaa:example_creator", kind="user")
        with ui.group() as rows:
            board = job.Board(rows)
            board.count(self.target, "posts", found=1, skipped=1)
            board.count(self.target, "reels", found=3, skipped=2, linked=1)
            board.count(other, "posts", found=5, skipped=5)
            self.assertEqual((board.totals(self.target).found, board.totals(self.target).skipped),
                             (4, 3))
            board.end(self.target, failed=True)

    def test_a_link_waiting_for_its_file_keeps_the_line_open(self):
        with ui.group() as rows:
            board = job.Board(rows)
            board.count(self.target, "reels", found=1, skipped=1, later=1)
            board.listed(self.target, "reels")
            board.count(self.target, "reels", later=-1, linked=1)
        self.assertEqual(board.totals(self.target).linked, 1)


class VerboseTest(EngineCase):
    """The engine logs no rows (the CLI draws a run from its events); -v adds its debug lines."""

    def setUp(self):
        super().setUp()
        previous = ui.level
        self.addCleanup(setattr, ui, "level", previous)
        self.source = make_source("aaa", files=[File(f"{CDN}/a.jpg", name="a.jpg"),
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


if __name__ == "__main__":
    unittest.main()
