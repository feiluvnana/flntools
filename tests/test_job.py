"""The job (flnd/job.py): one target of a ported source, from its route to its history rows.

Run through the engine with tests/example_source.py on `httpx.MockTransport`, the clocks of
net.py and site.py faked, and through `job.posts` on a `FakeSite` for the listing rules. Also the
"download Chromium?" question asked up front. No network, no browser.
"""

import datetime
import os
import threading
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
from flnd import job, site as site_module
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
