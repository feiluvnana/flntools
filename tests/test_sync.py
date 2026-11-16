"""`Engine.sync` and the saved target settings (flnd/targets.py): the plan, the records,
`Engine.download`'s use of them, the fetch and the summary.

Each test runs on a temporary `.flnd` with test sources on `httpx.MockTransport` (see `EngineCase`),
and the fake screen. `sync=True` marks the target for `flnd sync`; the default keeps it off.
"""

import datetime
import unittest
from unittest import mock

import httpx
from test_engine import CDN, EngineCase, make_source, noise

from flnd.engine import Engine, TargetReport
from flnd.source import Range
from flnd.common import paths
from flnd.options import Options
from flnd.source import File, Post, Source, Target, target
from flnd import _db as store

class Site:
    """A test source's posts: `files` maps a name to its body; its `posts` category yields every one."""

    def __init__(self, case, **files):
        self.case, self.files = case, files

    def source(self, code="aaa"):
        return make_source(code, files=[File(f"{CDN}/{name}", name=name) for name in self.files])

    def serve(self):
        for name, body in self.files.items():
            self.case.routes[f"{CDN}/{name}"] = httpx.Response(200, content=body)


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
        with paths.table(self.home / paths.TARGETS, "targets") as targets:
            return dict(targets.items())

    def folder(self):
        return self.target_dir("aaa", "a")

    def names(self):
        return sorted(path.name for path in self.folder().rglob("*") if path.is_file())


class SettingsTest(SyncCase):
    def test_from_and_time_are_never_saved(self):
        self.download(Options(categories=("posts",), since=datetime.date(2026, 1, 1), until=datetime.date(2026, 6, 30)))
        row = self.saved()["aaa:a"]
        # `complete` is what was listed to its end, not a setting: here the one category, `posts`.
        self.assertEqual(sorted(row), ["at", "complete", "kind", "sync"])
        self.assertEqual(sorted(row["complete"]), ["posts"])
        self.assertEqual(self.plan()[0][1], Options())

    def test_every_recorded_target_and_unrecorded_folders(self):
        self.download()
        (self.home / paths.MEDIA / "aaa" / "older").mkdir(parents=True)
        (self.home / paths.MEDIA / "aaa" / "older" / "x.jpg").write_bytes(b"x")
        chosen = dict(self.plan())
        self.assertEqual(chosen[Target("aaa", "user", "older")], Options())
        # A folder of a source flnd doesn't have, or a file beside the folders, isn't a target.
        (self.home / paths.MEDIA / "zzz" / "b").mkdir(parents=True)
        (self.home / paths.MEDIA / "aaa" / "a.zip").write_bytes(b"")
        self.assertEqual(len(self.plan()), 2)
        self.sync()
        self.assertEqual(sorted(self.saved()), ["aaa:a", "aaa:older"])

    def test_planning_creates_no_file(self):
        self.assertEqual(self.plan(), [])
        self.assertFalse((self.home / paths.TARGETS).exists())


class FetchTest(SyncCase):
    def test_sync_takes_every_category(self):
        runs = []
        self.download(Options(categories=("posts",)))
        record = recorded(lambda chosen: runs.extend(options for _, options in chosen))
        with mock.patch.object(Engine, "_run", record):
            self.sync()
        self.assertEqual(runs, [Options(stop_at_saved=True)])


class HistoryTest(SyncCase):
    def test_the_history_is_read_once_per_run(self):
        """Fix 12: repair, compress and the tally each scanned the whole history, per target."""
        self.download(texts=("aaa:a", "aaa:b", "aaa:c"))
        real_iter, real_items, scans = store.Table.__iter__, store.Table.items, []

        def counted_iter(s):
            scans.append(s)
            return real_iter(s)

        def counted_items(s):
            scans.append(s)
            return real_items(s)

        with (
            mock.patch.object(store.Table, "__iter__", counted_iter),
            mock.patch.object(store.Table, "items", counted_items),
        ):
            lines = self.sync()
        history_scans = [s for s in scans if s.name == "downloads"]
        self.assertEqual(len(lines), 3)
        self.assertLessEqual(len(history_scans), 1)


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

            @target("{user}")
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
            self.routes[f"{CDN}/{name}"] = httpx.Response(200, content=noise("JPEG"))

    def test_a_target_with_nothing_lost_still_stops_at_saved_posts(self):
        source = self.feed()
        self.engine(source).download(["aaa:a"], categories="all", sync=True)
        self.requests.clear()
        self.sync(source=source)
        self.assertEqual(self.requests, [])


class SyncOptInTest(SyncCase):
    def test_download_without_sync_is_not_synced_and_legacy_rows_count_as_on(self):
        self.download(sync=False)
        self.assertIs(self.saved()["aaa:a"]["sync"], False)
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.sync(), [])
        self.assertIn("* nothing to sync", self.lines())
        # A legacy row without a `sync` key counts as synced.
        with paths.table(self.home / paths.TARGETS, "targets") as targets:
            row = dict(targets["aaa:a"])
            del row["sync"]
            targets["aaa:a"] = row
        self.assertEqual([t for t, _ in self.plan()], [Target("aaa", "user", "a")])

    def test_target_sync_ls_and_del(self):
        self.download(sync=False)
        self.assertEqual(
            self.engine().target_sync(["aaa:a"], enabled=True), [Target("aaa", "user", "a")]
        )
        self.assertEqual([t for t, _ in self.plan()], [Target("aaa", "user", "a")])
        # Re-downloading without --sync preserves sync=True.
        self.download(sync=False)
        self.assertIs(self.saved()["aaa:a"]["sync"], True)
        items = self.engine().target_ls()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].sync)
        self.assertEqual(items[0].files, 3)
        self.assertEqual(
            self.engine().target_sync(["aaa:a"], enabled=False), [Target("aaa", "user", "a")]
        )
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.engine().target_del(["aaa:a"]), [Target("aaa", "user", "a")])
        self.assertNotIn("aaa:a", self.saved())


if __name__ == "__main__":
    unittest.main()
