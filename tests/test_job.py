"""The job (flnd/job.py): one target of a ported source, from its route to its history rows.

Run through the engine with tests/example_source.py on `httpx.MockTransport`, the clocks of
net.py and site.py faked, and through `job.posts` on a `FakeSite` for the listing rules. Also the
"download Chromium?" question asked up front, an expired link's refresh, and both Ctrl-Cs (with
a CDN that blocks on events, and the run on a thread of its own). No network, no browser.
"""

import datetime
import os
import threading
import time
import unittest
from datetime import date
from unittest import mock

import httpx

from example_source import API, CDN, FIXTURES, Example
from fakes import FakeSite, collect
from test_commands import FlndCase
from test_engine import EngineCase, compressing

from flnd import cli as flnd_cli
from flnd import engine as engine_module
from flnd import Engine, job, site as site_module
from flnd.common import net
from flnd.common.compress import Choice
from flnd.session import Options, single_targets
from flnd.source import FetchError, File, LoginError, Post, Source, Target, target
from shared import kv


class JobCase(EngineCase):
    """The engine with `Example`'s API answering from `FIXTURES`; every other URL answers its own path."""

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(site_module, "_sleep", self.sleep),
                        mock.patch.object(site_module, "_clock", lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for url, answer in FIXTURES.items():
            self.routes[url] = self.json_answer(url, answer)

    def json_answer(self, url, answer):
        def respond(request):
            data = answer(str(request.url), {"params": dict(request.url.params)}) if callable(answer) else answer
            return httpx.Response(200, json=data)

        return respond

    def history(self):
        with kv.open(self.home / "history.db", table="downloads") as history:
            return dict(history.items())

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
        # Today's row format: where it is, its size, and when.
        self.assertEqual(sorted(rows["exm:C1abc_0"]), ["at", "path", "size"])
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

            @target("{user}")
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
        message = single_targets([Target("exm", "post", "C1abc")], {"exm": Example})
        self.assertEqual(message, "--from applies to profiles and chats; exm:C1abc is one post, downloaded whole: "
                                  "give it without --from")
        self.assertIsNone(single_targets([Target("exm", "user", "example_user")], {"exm": Example}))


class FromCommandTest(FlndCase):
    def test_from_with_a_post_link_exits_2_through_the_command(self):
        with mock.patch.dict(flnd_cli.SOURCES, {"exm": Example}), \
                mock.patch.object(engine_module.Engine, "_run") as run:
            code, _, out = self.run_app("download", "--from", "posts", "https://www.example.com/p/C1abc/")
        self.assertEqual(code, 2)
        self.assertIn("--from applies to profiles and chats; exm:C1abc is one post", out)
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

            @target("{user}")
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

    def test_a_file_queued_twice_is_downloaded_once(self):
        class Twice(Source):
            code, name, domains = "twc", "Twice", ("example.com",)

            @target("{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("1", [File(f"{CDN}/1.jpg")])
                yield Post("1", [File(f"{CDN}/1.jpg")])

        self.assertEqual(self.run_engine(Twice, targets=["twc:example_user"]), 1)
        self.assertIn("• skip posts/1.jpg", self.lines())

    def test_a_file_in_two_categories_is_downloaded_once(self):
        class Both(Source):
            code, name, domains = "bth", "Both", ("example.com",)

            @target("{user}")
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
        self.assertEqual(self.files("bth"), ["posts/1.jpg", "reels/2.jpg"])
        # A later run finds both saved, whichever category lists them.
        self.requests.clear()
        self.assertEqual(self.run_engine(Both, targets=["bth:example_user"]), 0)
        self.assertEqual([request for request in self.requests if request.url.host == "cdn.example"], [])

    def test_two_files_with_one_path_are_downloaded_once(self):
        class Same(Source):
            code, name, domains = "sme", "Same", ("example.com",)
            folder = ""
            filename = "shared"

            @target("{user}")
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
        self.assertEqual(self.files("sme"), ["shared.jpg"])
        self.assertEqual(self.fetched(f"{CDN}/2.jpg"), [])


class FilesTest(JobCase):
    def test_text_files_are_written_whole_and_unkeyed(self):
        class Notes(Source):
            code, name, domains = "nts", "Notes", ("example.com",)
            folder = "{id}"

            @target("{user}")
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
        self.routes[f"{CDN}/blob"] = httpx.Response(200, content=png)

        class Blob(Source):
            code, name, domains = "blb", "Blob", ("example.com",)

            @target("{user}")
            def user(self, site, user):
                yield Post("9", [File(f"{CDN}/blob")], date=1767312000)

        self.run_engine(Blob, targets=["blb:example_user"])
        self.assertEqual(self.files("blb"), ["2026-01-02_9.png"])

    def test_mtime_is_the_posts_day_else_last_modified(self):
        self.routes[f"{CDN}/avatars/42.jpg"] = httpx.Response(
            200, content=b"/avatar", headers={"Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"})
        self.run_engine(Example, targets=["exm:example_user"])
        folder = self.target_dir("exm", "example_user")

        def modified(name):
            return datetime.datetime.fromtimestamp(os.stat(folder / name).st_mtime, datetime.timezone.utc)

        self.assertEqual(modified("posts/2026-01-02_C1abc_0.jpg"),
                         datetime.datetime(2026, 1, 2, 12, tzinfo=datetime.timezone.utc))
        self.assertEqual(modified("profile/avatar_42.jpg"),
                         datetime.datetime(2026, 10, 21, 12, tzinfo=datetime.timezone.utc))
        # No date and no Last-Modified: the download day, which is when it was written.
        self.assertEqual(modified("albums/Summer_2026/A1.jpg").date(), datetime.datetime.now(datetime.timezone.utc).date())

    def test_the_history_row_has_its_path_size_and_time_only(self):
        self.run_engine(Example, targets=["exm:example_user"], options=Options(categories=("posts",)))
        row = self.history()["exm:C1abc_0"]
        saved = self.target_dir("exm", "example_user") / "posts" / "2026-01-02_C1abc_0.jpg"
        self.assertEqual(sorted(row), ["at", "path", "size"])
        self.assertEqual((row["path"], row["size"]), (str(saved), len(b"/C1abc_0.jpg")))
        written = datetime.datetime.fromisoformat(row["at"])
        self.assertEqual(written.utcoffset(), datetime.timedelta(0))
        self.assertEqual(written.microsecond, 0)
        self.assertLess(abs(datetime.datetime.now(datetime.timezone.utc) - written), datetime.timedelta(minutes=1))
        # Keys are `<code>:<key>`, one per file, a text file none.
        self.assertEqual(sorted(self.history()), ["exm:B2def_0", "exm:C1abc_0", "exm:C1abc_1"])

    def test_compress_runs_before_the_history_row_is_written(self):
        with compressing(return_value=None) as compress:
            self.run_engine(Example, targets=["exm:example_user"],
                            options=Options(categories=("profile",), image=Choice("webp", "mid")))
        compress.assert_called_once()
        row = self.history()["exm:profile/avatar_42.jpg"]
        self.assertEqual(row["compressed"], "none")


class TimeTest(unittest.TestCase):
    """`--time` and the `newest_first` early stop, on a `FakeSite`: nothing is downloaded."""

    def listed(self, source, options):
        target = Target(source.code, "user", "example_user")
        site = FakeSite(target, options=options)
        return [post.id for post in job.posts(source(), site, target, options, run_async=None)]

    def feed(self, newest_first, finished):
        days = [2020, 2021, 2022, 2026, 2026, 2025, 2025, 2025, 2025, 2025, 2025, 2024]

        class Feed(Source):
            code, name, domains = "fed", "Feed", ("example.com",)

            @target("{user}")
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

            @target("{user}")
            def user(self, site, user):
                yield Post("late", date="2026-07-01")
                yield Post("undated")
                yield Post("in", date=datetime.datetime(2026, 3, 1, 23, 59))

        self.assertEqual(self.listed(Dated, Options(until=date(2026, 6, 30))), ["undated", "in"])


class SyncStopTest(JobCase):
    """`sync` stops a `newest_first` category after 5 saved posts in a row, only once its listing has reached its end."""

    def setUp(self):
        super().setUp()
        self.ids = [f"p{number}" for number in range(8)]
        self.listed, self.finished, self.fail_at = [], [], []
        test = self

        class Feed(Source):
            code, name, domains = "fed", "Feed", ("example.com",)
            key = "{id}"

            @target("{user}")
            def user(self, site, user):
                return user

            @user.category(newest_first=True)
            def posts(self, site, user):
                try:
                    for post_id in list(test.ids):
                        if post_id in test.fail_at:
                            raise FetchError("cut")
                        test.listed.append(post_id)
                        yield Post(post_id, [File(f"{CDN}/{post_id}.jpg")], date=datetime.date(2026, 1, 1))
                finally:
                    test.finished.append(True)

        self.engine = Engine(home=self.home, sources={"fed": Feed})

    def record(self):
        with kv.open(self.home / "targets.db", table="targets") as rows:
            return rows.get("fed:example_user", {}).get("complete")

    def forget_record(self):
        with kv.open(self.home / "targets.db", table="targets") as rows:
            row = dict(rows["fed:example_user"])
            row.pop("complete")
            rows["fed:example_user"] = row

    def test_sync_stops_after_5_saved_posts_in_a_row_with_a_record_and_closes_the_listing(self):
        self.assertTrue(self.engine.download(["fed:example_user"]).ok)
        self.assertEqual(self.record(), {"posts": datetime.date.today().isoformat()})
        self.ids = ["n1", "n2", *self.ids]
        self.listed.clear(), self.finished.clear()
        report = self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, ["n1", "n2", "p0", "p1", "p2", "p3", "p4"])
        self.assertEqual(self.finished, [True])
        self.assertEqual(sorted(path.name for path in report.targets[0].written), ["2026-01-01_n1.jpg", "2026-01-01_n2.jpg"])
        # It stopped early, so the record is the earlier one, still there.
        self.assertIn("posts", self.record())

    def test_a_saved_run_shorter_than_5_does_not_stop_it(self):
        self.engine.download(["fed:example_user"])
        # Remove two saved files in the middle: the 4 saved ones before it never reach 5, then a new one resets.
        for name in ("p2", "p3"):
            next(self.target_dir("fed", "example_user").rglob(f"*{name}.jpg")).unlink()
        with kv.open(self.home / "history.db", table="downloads") as history:
            for name in ("p2", "p3"):
                del history[f"fed:{name}"]
        self.listed.clear()
        report = self.engine.sync(["fed:example_user"])
        self.assertEqual(len(report.targets[0].written), 2)
        # p0, p1 saved, p2 and p3 fetched again, then p4..p7: 4 saved in a row never stop it.
        self.assertEqual(self.listed, self.ids)

    def test_without_a_record_sync_lists_to_the_end(self):
        self.engine.download(["fed:example_user"])
        self.forget_record()
        self.listed.clear()
        self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, self.ids)
        # The listing reached its end, so it is recorded now, and the next sync stops.
        self.assertIsNotNone(self.record())
        self.listed.clear()
        self.engine.sync(["fed:example_user"])
        self.assertEqual(self.listed, self.ids[:5])

    def test_an_interrupted_listing_records_none_so_sync_lists_everything(self):
        self.fail_at = ["p6"]
        report = self.engine.download(["fed:example_user"])
        self.assertFalse(report.ok)
        self.assertIsNone(self.record())
        self.fail_at, self.listed = [], []
        self.engine.sync(["fed:example_user"])
        # The first 6 were saved, but nothing says the feed was ever read to its end.
        self.assertEqual(self.listed, self.ids)
        self.assertIsNotNone(self.record())

    def test_download_never_stops_at_saved_posts(self):
        self.engine.download(["fed:example_user"])
        self.listed.clear()
        report = self.engine.download(["fed:example_user"])
        self.assertEqual(self.listed, self.ids)
        self.assertEqual((report.files, report.targets[0].skipped), (0, 8))

    def test_a_feed_that_is_not_newest_first_is_never_stopped(self):
        class Plain(Source):
            code, name, domains = "pln", "Plain", ("example.com",)

            @target("{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                for number in range(8):
                    self.listed.append(number)
                    yield Post(f"q{number}", [File(f"{CDN}/q{number}.jpg")])

        Plain.listed = []
        engine = Engine(home=self.home, sources={"pln": Plain})
        engine.download(["pln:example_user"])
        Plain.listed.clear()
        engine.sync(["pln:example_user"])
        self.assertEqual(len(Plain.listed), 8)


class AsyncTest(JobCase):
    def test_async_routes_and_categories_run_on_the_sources_loop_with_no_handover_thread(self):
        threads = []

        class Chats(Source):
            code, name, domains = "cht", "Chats", ("example.com",)
            concurrency = 1

            @target("{chat}")
            async def chat(self, site, chat):
                threads.append(threading.current_thread().name)
                return chat

            @chat.category(newest_first=True)
            async def photo(self, site, chat):
                for number in range(4):
                    threads.append(threading.current_thread().name)
                    yield Post(str(number), [File(f"{CDN}/{chat}/{number}.jpg")], date=1767312000 - number)

        self.assertEqual(self.run_engine(Chats, targets=["cht:examplechannel"]), 4)
        self.assertEqual(set(threads), {"flnd-cht"})
        self.assertFalse([thread for thread in threading.enumerate() if thread.name.endswith("-saves")])


class FailureTest(JobCase):
    def test_a_category_that_raises_fails_the_target(self):
        class Broken(Source):
            code, name, domains = "brk", "Broken", ("example.com",)

            @target("{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("1", [File(f"{CDN}/1.jpg")])
                raise RuntimeError("the feed broke")

        self.run_engine(Broken, targets=["brk:example_user"])
        self.assertIn("✗ brk:example_user: the feed broke", self.lines())
        report = self.report(Broken, targets=["brk:example_user"])
        self.assertEqual(report.failed, (Target("brk", "user", "example_user"),))
        self.assertEqual(report.targets[0].error, "the feed broke")


    def test_a_route_or_category_that_raises_a_source_error_fails_only_its_target(self):
        class Errors(Source):
            code, name, domains = "err", "Errors", ("example.com",)
            folder = ""

            @target("{user}")
            def user(self, site, user):
                if user == "gone":
                    raise FetchError("HTTP 404 for https://example.com/gone", status=404)
                if user == "locked":
                    raise LoginError("err: run flnd login err")
                return user

            @user.category
            def posts(self, site, user):
                if user == "cut":
                    raise FetchError("HTTP 500 for https://example.com/cut", status=500)
                if user == "expired":
                    raise LoginError("err: the saved login expired")
                yield Post(user, [File(f"{CDN}/{user}.jpg")])

        names = ["gone", "locked", "cut", "expired", "good", "last"]
        report = self.report(Errors, targets=[f"err:{name}" for name in names])
        self.assertEqual(report.files, 2)
        self.assertEqual([target.value for target in report.failed], ["gone", "locked", "cut", "expired"])
        lines = self.lines()
        for line in ("✗ err:gone: HTTP 404 for https://example.com/gone", "✗ err:locked: err: run flnd login err",
                     "✗ err:cut: HTTP 500 for https://example.com/cut", "✗ err:expired: err: the saved login expired",
                     "✓ err:good", "✓ err:last"):
            self.assertIn(line, lines)
        self.assertEqual(self.files("err", "good"), ["good.jpg"])


class Cut(httpx.SyncByteStream):
    """A body that sends `data`, then drops the connection; or, given `gate`, waits on it after the first chunk."""

    def __init__(self, data, *, gate=None, sent=None):
        self.data, self.gate, self.sent = data, gate, sent

    def __iter__(self):
        yield self.data[:5]
        if self.sent is not None:
            self.sent.set()
        if self.gate is None:
            raise httpx.ReadError("connection reset")
        self.gate.wait(5)
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
            return httpx.Response(200, json={"urls": [f"{self.PHOTO}?sig={self.listings}"]})

        self.routes[f"{API}/albums/A1"] = album

    def cdn(self, fresh):
        """The photo's CDN: the first link sends half the file, then the connection drops and the link expires."""
        def photo(request):
            sig = request.url.params["sig"]
            headers = {"ETag": '"p1"', "Accept-Ranges": "bytes"}
            if sig == "1":
                if "range" in request.headers:
                    return httpx.Response(403)
                return httpx.Response(200, headers={**headers, "Content-Length": "10"}, stream=Cut(self.BODY))
            return fresh(request, headers)

        self.routes[self.PHOTO] = photo

    def test_an_expired_link_calls_lazy_files_once_more_and_resumes(self):
        def fresh(request, headers):
            start = int(request.headers["range"].removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=self.BODY[start:],
                                  headers={**headers, "Content-Range": f"bytes {start}-9/10"})

        self.cdn(fresh)
        self.run_engine(Example, targets=["exm:example_user"], options=Options(categories=("albums",)))
        self.assertEqual((self.target_dir("exm", "example_user") / "albums/Summer_2026/A1.jpg").read_bytes(),
                         self.BODY)
        self.assertEqual(self.listings, 2)
        last = self.fetched(self.PHOTO)[-1]
        self.assertEqual(last.url.params["sig"], "2")
        # The second link resumes where the first stopped, checked against the same file.
        self.assertEqual((last.headers["range"], last.headers["if-range"]), ("bytes=5-", '"p1"'))
        self.assertIn("exm:A1_0", self.history())
        self.assertEqual([path.name for path in (self.target_dir("exm", "example_user") / "albums/Summer_2026").iterdir()],
                         ["A1.jpg"])

    def test_once_per_file_never_in_a_loop(self):
        self.cdn(lambda request, headers: httpx.Response(410))
        self.run_engine(Example, targets=["exm:example_user"], options=Options(categories=("albums",)))
        self.assertEqual(self.listings, 2)
        self.assertIn("✗ albums/Summer_2026/A1.jpg: HTTP 410 for https://cdn.example/A1/1.jpg", self.lines())
        self.assertNotIn("exm:A1_0", self.history())

    def test_a_first_ctrl_c_during_a_refresh_is_no_fresh_link_and_the_target_stops(self):
        """A rate-limit wait inside `files` that the first Ctrl-C cuts short is the graceful stop, not a failure."""
        self.addCleanup(net.stopping.clear)
        self.cdn(lambda request, headers: httpx.Response(500))

        def album(request):
            self.listings += 1
            if self.listings > 1:
                net.stopping.set()      # the first Ctrl-C, during the refresh's wait
                raise KeyboardInterrupt
            return httpx.Response(200, json={"urls": [f"{self.PHOTO}?sig=1"]})

        self.routes[f"{API}/albums/A1"] = album
        report = self.report(Example, targets=["exm:example_user"], options=Options(categories=("albums",)))
        self.assertEqual(report.targets[0].error, job.STOPPED)
        self.assertEqual(self.listings, 2)

    def test_a_refresh_cut_by_the_first_ctrl_c_answers_no_link_and_one_by_the_second_raises(self):
        for event in (net.stopping, net.stopping_now):
            self.addCleanup(event.clear)

        def files():
            raise KeyboardInterrupt

        net.stopping.set()
        refresh = job._Refresh(Post("A1", files))
        refresh.run()
        self.assertTrue(refresh.done.is_set())
        self.assertEqual((refresh.files, refresh.error), (None, None))
        net.stopping_now.set()
        second = job._Refresh(Post("A1", files))
        with self.assertRaises(KeyboardInterrupt):
            second.run()
        self.assertTrue(second.done.is_set())

    def test_a_post_with_its_files_listed_is_not_refreshed(self):
        self.routes[f"{CDN}/C1abc_0.jpg"] = httpx.Response(403)
        self.run_engine(Example, targets=["exm:example_user"], options=Options(categories=("posts",)))
        self.assertEqual(len(self.fetched(f"{CDN}/C1abc_0.jpg")), 1)
        self.assertEqual(len(self.fetched(f"{API}/users/42/posts")), 2)
        self.assertIn("✗ posts/2026-01-02_C1abc_0.jpg: HTTP 403 for https://cdn.example/C1abc_0.jpg", self.lines())


class CtrlCTest(JobCase):
    """D8: the first Ctrl-C stops listing and lets running downloads finish; the second stops them now.

    The run goes on a thread of its own, as the engine's groups do, and the test thread plays the
    person, setting `net.stopping` and `net.stopping_now` as the SIGINT handler does; the CDN blocks
    on events, so the test decides when each download moves.
    """

    def setUp(self):
        super().setUp()
        for event in (net.stopping, net.stopping_now):
            self.addCleanup(event.clear)
        self.rows = []
        real = job.download

        def spy(run, target, row, **options):
            self.rows.append(row)
            return real(run, target, row, **options)

        patcher = mock.patch.object(job, "download", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, source, text):
        """Run `text` on a thread; returns the thread and a dict that gets its result or error."""
        outcome = {}

        def run():
            try:
                outcome["report"] = self.report(source, targets=[text])
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

        class Slow(Source):
            code, name, domains = "slw", "Slow", ("example.com",)
            folder = ""
            # One download at a time: post 1's runs, post 2's waits in the queue.
            concurrency = 1

            @target("{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                try:
                    for number in range(1, 100):
                        listed.append(number)
                        yield Post(str(number), [File(f"{CDN}/{number}.7z", name=f"{number}.7z")])
                finally:
                    closed.set()

        def first(request):
            started.set()
            release.wait(5)
            return httpx.Response(200, content=b"one")

        self.routes[f"{CDN}/1.7z"] = first
        thread, outcome = self.start(Slow, "slw:example_user")
        # Post 3 waits for room in the pool: 1 is downloading and 2 is queued.
        self.until(lambda: started.is_set() and len(listed) == 3)
        net.stopping.set()
        self.until(closed.is_set)
        self.until(lambda: self.rows and self.rows[0].caption == "stopping: 1 file finishing (Ctrl-C again to stop now)")
        release.set()
        thread.join(10)
        report = outcome["report"]
        self.assertEqual(report.targets[0].error, job.STOPPED)
        self.assertEqual(self.files("slw"), ["1.7z"])
        self.assertIn("slw:1_0", self.history())
        # Post 2 was queued but not started, so it was dropped; nothing past post 3 was listed.
        self.assertEqual(self.fetched(f"{CDN}/2.7z"), [])
        self.assertEqual(listed, [1, 2, 3])
        self.assertIn("✗ slw:example_user stopped", self.lines())

    def test_the_second_ctrl_c_stops_now_and_keeps_the_part(self):
        sent, gate = threading.Event(), threading.Event()

        class Big(Source):
            code, name, domains = "big", "Big", ("example.com",)
            folder = ""

            @target("{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post("1", [File(f"{CDN}/big.7z", name="big.7z")])

        body = b"0123456789"

        def big(request):
            if "range" in request.headers:
                start = int(request.headers["range"].removeprefix("bytes=").rstrip("-"))
                return httpx.Response(206, content=body[start:],
                                      headers={"ETag": '"b1"', "Content-Range": f"bytes {start}-9/10"})
            return httpx.Response(200, headers={"ETag": '"b1"', "Content-Length": "10"},
                                  stream=Cut(body, gate=gate, sent=sent))

        self.routes[f"{CDN}/big.7z"] = big
        thread, outcome = self.start(Big, "big:example_user")
        self.until(sent.is_set)
        net.stopping.set()
        net.stopping_now.set()
        # The next chunk arrives and is written; the download stops there, before the last one.
        gate.set()
        thread.join(10)
        self.assertIsInstance(outcome.get("error"), KeyboardInterrupt)
        folder = self.target_dir("big", "example_user")
        names = sorted(path.name for path in folder.iterdir())
        self.assertEqual(len(names), 2, names)
        self.assertTrue(names[0].startswith(".big.7z.") and names[0].endswith(".part"), names)
        self.assertEqual(names[1], names[0] + ".json")

        # The next run resumes the part.
        net.stopping.clear()
        net.stopping_now.clear()
        self.run_engine(Big, targets=["big:example_user"])
        self.assertEqual((folder / "big.7z").read_bytes(), body)
        self.assertEqual(self.fetched(f"{CDN}/big.7z")[-1].headers["range"], "bytes=7-")
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["big.7z"])


class BrowserUpFrontTest(JobCase):
    """The "download Chromium?" question, asked before any work when a chosen target's source can need a browser."""

    def setUp(self):
        super().setUp()
        self.asked = []
        patcher = mock.patch.object(engine_module, "_check_browser",
                                    lambda: self.asked.append(len(self.requests)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_source_that_only_fetches_asks_nothing(self):
        self.run_engine(Example, targets=["exm:example_user"])
        self.assertEqual(self.asked, [])

    def test_a_ported_source_that_loads_pages_asks_once_before_any_request(self):
        class Paged(Source):
            code, name, domains = "pgd", "Paged", ("example.com",)

            @target("{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                yield Post(user, [File(f"{CDN}/{user}.jpg")])

            @user.category
            def stories(self, site, user):
                if site.goto("https://www.example.com/stories/"):
                    yield Post(user)

        with mock.patch.object(site_module.TargetSite, "goto", return_value=False):
            self.run_engine(Paged, targets=["pgd:example_user", "pgd:example_creator"])
        self.assertEqual(self.asked, [0])
        # Without the category that loads a page, the same source asks nothing.
        self.run_engine(Paged, targets=["pgd:example_user"], options=Options(categories=("posts",)))
        self.assertEqual(self.asked, [0])

    def test_a_helper_the_category_calls_counts(self):
        self.assertTrue(engine_module.needs_browser(Uses, Target("use", "user", "example_user"), Options()))

    def test_the_six_sources(self):
        from flnd.sources import SOURCES

        needs = {code: engine_module.needs_browser(source, Target(code, "user", "x"), Options())
                 for code, source in SOURCES.items()}
        self.assertEqual(needs, {"tik": True, "fbk": True, "ins": True, "twt": True, "loi": False, "tlg": False})


def load_in_page(site, url):
    """A module-level helper, as sources have, that sends a request from the page."""
    return site.fetch(url, in_page=True)


class Uses(Source):
    code, name, domains = "use", "Uses", ("example.com",)

    @target("{user}")
    def user(self, site, user):
        return user

    @user.category
    def posts(self, site, user):
        yield from self.helper(site)

    def helper(self, site):
        load_in_page(site, "https://www.example.com/api")
        yield from ()


if __name__ == "__main__":
    unittest.main()
