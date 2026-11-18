"""`Engine.sync` and the saved targets (`flnd.db`'s `targets`): the plan, the records,
`Engine.download`'s use of them, the fetch and the summary.

Each test runs on a temporary `.flnd` with test sources on `flnd.kit.net.fake` (see `EngineCase`),
and the fake screen. `sync=True` marks the target for `flnd sync`; the default keeps it off.
"""

import datetime
import unittest
from unittest import mock

import fakes
from test_engine import CDN, EngineCase, make_source, noise

from flnd import _home
from flnd.engine import Engine, TargetReport
from flnd.kit import net as _net
from flnd.options import Options
from flnd.source import File, Post, Range, Source, Target, target


class Site:
    """A test source's posts: `files` maps a name to its body; its `posts` category yields every one."""

    def __init__(self, case, **files):
        self.case, self.files = case, files

    def source(self, code="aaa"):
        return make_source(code, files=[File(f"{CDN}/{name}", name=name) for name in self.files])

    def serve(self):
        for name, body in self.files.items():
            self.case.routes[f"{CDN}/{name}"] = _net.reply(200, content=body)


def recorded(record):
    """An `Engine._run` that only calls `record(chosen)`, and reports each target done, empty."""
    def run(engine, chosen, *, stop=None, tallies=(), browser_pool=None):
        record(chosen)
        return [TargetReport(target, (), 0) for target, _ in chosen]

    return run


class SyncCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.site = Site(self, **{"a.jpg": noise("JPEG"), "b.jpg": noise("JPEG"), "c.jpg": noise("JPEG")})
        self.site.serve()

    def engine(self, source=None):
        source = source or self.site.source()
        return Engine(home=self.home, sources={source.code: source}, emit=self.events.append)

    def download(self, options=Options(), source=None, texts=("aaa:a",), sync=True):
        """`Engine.download` with `options`' --from and --time."""
        report = self.engine(source).download(
            list(texts), categories=options.categories or "all",
            time=Range(options.since, options.until), sync=sync,
        )
        return report.files

    def sync(self, texts=(), source=None):
        """`Engine.sync`, giving one `TargetReport` per target."""
        return list(self.engine(source).sync(list(texts)).targets)

    def plan(self, targets=()):
        return self.engine()._plan(list(targets))

    def saved(self):
        return fakes.targets(self.home)

    def folder(self):
        return self.target_dir("aaa", "a")

    def names(self):
        return sorted(path.name for path in self.folder().rglob("*") if path.is_file())


class SettingsTest(SyncCase):
    def test_from_and_time_are_never_saved(self):
        self.download(Options(categories=("posts",), since=datetime.date(2026, 1, 1), until=datetime.date(2026, 6, 30)))
        row = self.saved()["aaa:a"]
        # `complete` is what was listed to its end, not a setting: here the one category, `posts`.
        self.assertEqual((row.kind, row.follow, row.only, row.cursors), ("user", True, None, {}))
        self.assertEqual(sorted(row.complete), ["posts"])
        self.assertEqual(self.plan()[0][1], Options())

    def test_every_recorded_target_and_unrecorded_folders(self):
        self.download()
        (self.home / _home.MEDIA / "aaa" / "older").mkdir(parents=True)
        (self.home / _home.MEDIA / "aaa" / "older" / "x.jpg").write_bytes(b"x")
        chosen = dict(self.plan())
        self.assertEqual(chosen[Target("aaa:older", kind="user")], Options())
        # A folder of a source flnd doesn't have, or a file beside the folders, isn't a target.
        (self.home / _home.MEDIA / "zzz" / "b").mkdir(parents=True)
        (self.home / _home.MEDIA / "aaa" / "a.zip").write_bytes(b"")
        self.assertEqual(len(self.plan()), 2)
        self.sync()
        self.assertEqual(sorted(self.saved()), ["aaa:a", "aaa:older"])


    def test_planning_creates_no_file(self):
        self.assertEqual(self.plan(), [])
        self.assertFalse((self.home / _home.DB).exists())


class FetchTest(SyncCase):
    def test_sync_takes_every_category(self):
        runs = []
        self.download(Options(categories=("posts",)))
        record = recorded(lambda chosen: runs.extend(options for _, options in chosen))
        with mock.patch.object(Engine, "_run", record):
            self.sync()
        self.assertEqual(runs, [Options(stop_at_saved=True)])


class HistoryTest(SyncCase):
    def test_a_sync_never_reads_every_saved_file(self):
        """Fix 12: repair, compress and the tally each scanned the whole history, per target."""
        self.download(texts=("aaa:a", "aaa:b", "aaa:c"))
        with mock.patch.object(_home.Home, "records", side_effect=AssertionError("a full scan")):
            lines = self.sync()
        self.assertEqual(len(lines), 3)


class SummaryTest(SyncCase):
    def test_one_line_per_target(self):
        # History keys are per source, so aaa:b, saving the same names, finds them saved and stays empty.
        self.download(texts=("aaa:a", "aaa:b"))
        gone = Site(self, **{"a.jpg": b"", "b.jpg": b""})
        self.requests.clear()
        self.dest.truncate(0)
        self.dest.seek(0)
        lines = self.sync(source=gone.source())
        self.assertEqual([(str(line.target), len(line.written)) for line in lines],
                         [("aaa:a", 0), ("aaa:b", 0)])
        # Each target's listing ends with its counts, which the CLI shows as its line.
        self.assertTrue({("aaa:a", "posts"), ("aaa:b", "posts")} <= self.listed())


class SavedRunStopTest(SyncCase):
    """A sync of a target whose files are all saved lists nothing and requests nothing."""

    NAMES = [f"p{number:02}.jpg" for number in range(12)]

    def feed(self):
        names = self.NAMES

        class Feed(Source):
            code, name, domains, folder, key = "aaa", "AAA", ("aaa.example",), "", "{path}"
            api = 1
            filename = "{date}_{post}{index}"

            @target("aaa:{user}")
            def user(self, site, user):
                return None

            @user.category(newest_first=True)
            def posts(self, site, context):
                for name in names:
                    yield Post(name, [File(f"{CDN}/{name}", name=name)])

        return Feed

    def setUp(self):
        super().setUp()
        for name in self.NAMES:
            self.routes[f"{CDN}/{name}"] = _net.reply(200, content=noise("JPEG"))

    def test_a_target_with_nothing_lost_still_stops_at_saved_posts(self):
        source = self.feed()
        self.engine(source).download(["aaa:a"], categories="all", sync=True)
        self.requests.clear()
        self.sync(source=source)
        self.assertEqual(self.requests, [])


class SyncOptInTest(SyncCase):
    def test_download_without_sync_is_not_synced(self):
        self.download(sync=False)
        self.assertIs(self.saved()["aaa:a"].follow, False)
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.sync(), [])
        self.assertIn("* nothing to sync", self.lines())

    def test_target_sync_ls_and_del(self):
        self.download(sync=False)
        self.assertEqual(
            self.engine().target_sync(["aaa:a"], enabled=True), [Target("aaa:a", kind="user")]
        )
        self.assertEqual([t for t, _ in self.plan()], [Target("aaa:a", kind="user")])
        # Re-downloading without --sync preserves sync=True.
        self.download(sync=False)
        self.assertIs(self.saved()["aaa:a"].follow, True)
        items = self.engine().target_ls()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].sync)
        self.assertEqual(items[0].files, 3)
        self.assertEqual(
            self.engine().target_sync(["aaa:a"], enabled=False), [Target("aaa:a", kind="user")]
        )
        self.assertEqual(self.plan(), [])


if __name__ == "__main__":
    unittest.main()
