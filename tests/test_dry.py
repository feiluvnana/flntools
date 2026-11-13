"""flnd/dry.py and `job.plan`: what a download would save, listed for real and not downloaded.

Offline: `tests/example_source.py` on `httpx.MockTransport`. Nothing is downloaded, written or
recorded, and no URL is printed with its query.
"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from example_source import CDN, FIXTURES, Example
from fakes import collect
from test_job import JobCase

from flnd import dry, job
from flnd import site as site_module
from flnd.common import files
from flnd.session import Options, Settings
from flnd.source import File, Post, Range, Source, Target, target


class PlanTest(JobCase):
    def planned(self, options=Options()):
        with site_module.source_run(Settings(home=self.home, options=options, history={}), Example()) as run:
            return list(job.plan(run, Target("exm", "user", "example_user")))

    def test_plan_yields_what_a_download_would_save_and_writes_nothing(self):
        placed = self.planned()
        self.assertEqual([item.path for item in placed],
                         [saved.path for saved in collect(Example, "exm:example_user", json=FIXTURES)])
        self.assertEqual(len(placed), 5)
        # No download, no folder, no history, no settings: the CDN is never asked, nothing is on disk.
        self.assertFalse([request for request in self.requests if str(request.url).startswith(CDN)])
        # Only the pacer's learned speed (`sources.db`) is kept, as any run of the source does.
        self.assertEqual(sorted(path.name for path in self.home.rglob("*") if path.is_file()), ["sources.db"])
        self.assertFalse((self.home / files.MEDIA).exists())

    def test_plan_does_not_skip_saved_files_and_follows_from(self):
        self.run_engine(Example, targets=["exm:example_user"])
        self.assertEqual(len(self.planned()), 5)
        self.assertEqual([item.path for item in self.planned(Options(categories=("profile",)))],
                         ["profile/avatar_42.jpg"])

    def test_plan_closes_the_listing_when_the_caller_stops(self):
        finished = []

        class Feed(Source):
            code, name, domains = "fed", "Feed", ("example.com",)

            @target("{user}")
            def user(self, site, user):
                return user

            @user.category
            def posts(self, site, user):
                try:
                    for number in range(10):
                        yield Post(str(number), [File(f"{CDN}/{number}.jpg")])
                finally:
                    finished.append(True)

        with site_module.source_run(Settings(home=self.home, options=Options(), history={}), Feed()) as run:
            listing = job.plan(run, Target("fed", "user", "example_user"))
            next(listing)
            listing.close()
        self.assertEqual(finished, [True])


class TreeTest(JobCase):
    def lines(self, **options):
        return list(dry.tree(dry.plan(self.home, "exm:example_user", sources={"exm": Example}, **options)))

    def test_the_tree_is_categories_then_posts_then_files_with_urls_without_their_query(self):
        lines = self.lines(categories="posts")
        self.assertEqual(lines[0], "  posts")
        self.assertEqual(lines[1], "    C1abc  2026-01-02")
        self.assertRegex(lines[2], r"^      posts/2026-01-02_C1abc_0\.jpg  https://\S+$")
        self.assertRegex(lines[3], r"^      posts/2026-01-02_C1abc_1\.mp4  https://\S+$")
        self.assertEqual(lines[4], "    B2def  2026-01-01")

    def test_no_signed_query_is_ever_printed(self):
        self.assertNotIn("?", "\n".join(self.lines()))
        self.assertEqual(dry.without_query("https://cdn.example.com/a.jpg?oe=1&sig=secret#x"),
                         "https://cdn.example.com/a.jpg")

    def test_time_and_from_are_read_like_download(self):
        self.assertEqual(self.lines(categories="profile")[0], "  profile")
        # Only undated posts survive a range in the future.
        self.assertEqual([line for line in self.lines(time="2099-01-01,") if line.endswith("undated")],
                         ["    A1  undated", "    42  undated"])
        with self.assertRaises(ValueError):
            self.lines(categories="nothing")
        with self.assertRaises(ValueError):
            self.lines(time="yesterday")

    def test_other_kinds_of_file_are_named(self):
        post = Post("p1", date="2026-01-02")
        files = (File.text("info.md", "# t\n"), File(["https://a.example/x.jpg?t=1", "https://b.example/x.jpg"]),
                 File("https://v.example/v.mp4?sig=1", audio="https://v.example/a.m4a?sig=2"),
                 File(lambda dest, progress: None))
        placed = [job.Placed(post, file, 0, "p1/x", "p1_0", None, {"date": "2026-01-02"}) for file in files]
        self.assertEqual([line.split("  ")[-1] for line in dry.tree(placed)][1:],
                         ["(text, 4 characters)", "https://a.example/x.jpg (+1 mirrors)",
                          "https://v.example/v.mp4 + audio https://v.example/a.m4a", "(fetched by the source)"])


class MainTest(unittest.TestCase):
    def run_main(self, plan_result, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(dry, "plan", plan_result), redirect_stdout(out), redirect_stderr(err):
            code = dry.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_prints_the_target_and_its_tree_and_passes_the_options(self):
        calls = []

        def plan(home, text, **options):
            calls.append((home.name, text, options))
            return iter([job.Placed(Post("p1", date="2026-01-02"), File("https://c.example/a.jpg?x=1"), 0, "p1.jpg",
                                    "p1", None, {"date": "2026-01-02"})])

        code, out, _ = self.run_main(plan, "ins:example_user", "--from", "reels", "--time", "2026-01-01,")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(),
                         ["ins:example_user", "  p1  2026-01-02", "    p1.jpg  https://c.example/a.jpg"])
        name, text, options = calls[0]
        self.assertEqual((name, text, options["categories"]), (".flnd", "ins:example_user", "reels"))
        self.assertEqual(options["time"], Range.parse("2026-01-01,"))

    def test_a_failure_is_one_line_and_exit_1(self):
        def plan(home, text, **options):
            raise ValueError("ins: not logged in; run flnd login")

        code, _, err = self.run_main(plan, "ins:example_user")
        self.assertEqual((code, err), (1, "error: ins: not logged in; run flnd login\n"))


if __name__ == "__main__":
    unittest.main()
