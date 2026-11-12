"""`--classify` (flnd/common/labels.py, the `classify` postprocessor, and the engine's saved setting).

`Media.labels` is replaced by `labelled`, which reads a label from the file's name, so no model
runs. Links are made for real in a temporary folder; the hard-link and symlink failures of FAT,
exFAT and another volume are made by replacing `os.link` and `os.symlink` with ones that raise.
The engine tests run on `EngineCase`'s temporary `.flnd`, fake network and fake screen.
"""

import errno
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from media_samples import needs_pillow
from test_engine import CDN, EngineCase, history, make_source, noise

from flnd import Engine
from flnd import post as postprocess
from flnd.common import labels
from flnd.common.labels import Labeller
from flnd.source import File
from shared import kv, media, theme
from shared._theme import Screen


def labelled(self, **options):
    """`Media.labels` for a test: the labels are the file name's words before an `@`, `food+pet@a.jpg`."""
    words = Path(self.info.path).name.partition("@")[0]
    if "@" not in Path(self.info.path).name:
        return [("other", 0.2)]
    return [(word, 0.5) for word in words.split("+")]


def refused(*args, **kwargs):
    raise OSError(errno.EPERM, "Operation not permitted")


class LabelsCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(dest=io.StringIO(), inp=io.StringIO(""), width=400, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        self.folder = Path(tempfile.mkdtemp()) / "example_user"
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder.parent, True)
        patcher = mock.patch.object(media.Media, "labels", labelled)
        patcher.start()
        self.addCleanup(patcher.stop)

    def file(self, relative, data=b"x"):
        path = self.folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def photo(self, relative):
        from PIL import Image

        path = self.folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), "red").save(path)
        return path

    def views(self):
        return sorted(str(path.relative_to(self.folder / labels.VIEWS)) for path in (self.folder / labels.VIEWS).glob("*/*"))

    def warnings(self):
        return [line for line in self.screen.dest.getvalue().splitlines() if line.startswith("!")]


@needs_pillow
class LinkTest(LabelsCase):
    def test_a_hard_link_per_label_and_the_file_left_alone(self):
        path = self.photo("posts/food+pet@a.jpg")
        fields = postprocess.Fields(kind="image", labeller=Labeller(), folder=self.folder, row={"path": str(path)})
        self.assertEqual(postprocess.classify(path, fields), path)
        self.assertEqual(fields.row["labels"], ["food", "pet"])
        self.assertEqual(self.views(), ["food/food+pet@a.jpg", "pet/food+pet@a.jpg"])
        link = self.folder / "labels" / "food" / path.name
        self.assertFalse(link.is_symlink())
        self.assertEqual(os.stat(link).st_ino, os.stat(path).st_ino)
        self.assertEqual(sorted(p.name for p in (self.folder / "posts").iterdir()), [path.name])

    def test_no_labeller_audio_or_other_files_are_left_alone(self):
        path = self.photo("food@a.jpg")
        for fields in (postprocess.Fields(kind="image", folder=self.folder),
                       postprocess.Fields(kind="audio", labeller=Labeller(), folder=self.folder),
                       postprocess.Fields(kind=None, labeller=Labeller(), folder=self.folder)):
            postprocess.classify(path, fields)
            self.assertNotIn("labels", fields.row)
        self.assertFalse((self.folder / "labels").exists())

    def test_a_symlink_relative_to_its_folder_where_hard_links_fail(self):
        path = self.photo("posts/food@a.jpg")
        with mock.patch.object(labels.os, "link", side_effect=OSError(errno.EXDEV, "Cross-device link")):
            Labeller().link(path, self.folder, ["food"])
        link = self.folder / "labels" / "food" / path.name
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), os.path.join("..", "..", "posts", path.name))
        self.assertEqual(link.read_bytes(), path.read_bytes())

    def test_a_disk_without_links_skips_the_views_with_one_warning_and_keeps_the_labels(self):
        labeller = Labeller()
        rows = []
        with mock.patch.object(labels.os, "link", refused), mock.patch.object(labels.os, "symlink", refused):
            for name in ("food@a.jpg", "pet@b.jpg", "food+pet@c.jpg"):
                path = self.photo(name)
                fields = postprocess.Fields(kind="image", labeller=labeller, folder=self.folder)
                postprocess.classify(path, fields)
                rows.append(fields.row)
        self.assertEqual([row["labels"] for row in rows], [["food"], ["pet"], ["food", "pet"]])
        self.assertEqual(self.views(), [])
        [warning] = self.warnings()
        self.assertIn("labels/ views are skipped for this run", warning)
        self.assertTrue(labeller.unlinked)

    def test_the_same_name_from_another_category_gets_its_category_in_front(self):
        first, second = self.photo("posts/food@a.jpg"), self.photo("tagged/food@a.jpg")
        labeller = Labeller()
        for path in (first, second, first, second):
            labeller.link(path, self.folder, ["food"])
        self.assertEqual(self.views(), ["food/food@a.jpg", "food/tagged_food@a.jpg"])
        self.assertTrue(os.path.samefile(self.folder / "labels/food/tagged_food@a.jpg", second))

    def test_a_model_that_cant_run_stops_classifying_with_one_warning(self):
        labeller = Labeller()

        def no_model(self, **options):
            raise RuntimeError("Media.labels needs its model; run it once in a terminal to download it")

        with mock.patch.object(media.Media, "labels", no_model):
            for name in ("food@a.jpg", "food@b.jpg"):
                self.assertIsNone(labeller.labels(self.photo(name)))
        [warning] = self.warnings()
        self.assertIn("classify stops for this run: Media.labels needs its model", warning)

    def test_a_file_media_cant_read_is_only_skipped(self):
        labeller = Labeller()
        self.assertIsNone(labeller.labels(self.file("food@a.jpg", b"<html>not a photo</html>")))
        self.assertEqual(labeller.labels(self.photo("food@b.jpg")), ["food"])
        self.assertEqual(self.warnings(), [])


@needs_pillow
class RebuildTest(LabelsCase):
    def rows(self, *paths, labels_of=None):
        return [(path.name, {"path": str(path), "labels": (labels_of or {}).get(path.name)
                             or Path(path).name.partition("@")[0].split("+")}) for path in paths]

    def test_missing_links_come_back_and_dangling_symlinks_go(self):
        kept = self.photo("posts/food@a.jpg")
        rows = self.rows(kept)
        labeller = Labeller()
        labeller.link(kept, self.folder, ["food"])
        (self.folder / "labels/food" / kept.name).unlink()
        (self.folder / "labels/pet").mkdir()
        (self.folder / "labels/pet/dangling.jpg").symlink_to("../../posts/nothing.jpg")
        labels.rebuild(self.folder, rows, labeller)
        # Back for the file that's there; gone for the broken symlink, then its empty folder.
        self.assertEqual(self.views(), [f"food/{kept.name}"])
        self.assertFalse((self.folder / "labels/pet").exists())

    def test_a_file_whose_only_copy_is_its_hard_link_is_put_back_and_linked_again(self):
        # Deleting a file by hand leaves its data in labels/ alone: rebuild must move it back,
        # never delete it as a stale link. Both of its labels' links are its names.
        gone = self.photo("posts/pet+food@b.jpg")
        data = gone.read_bytes()
        labeller = Labeller()
        labeller.link(gone, self.folder, ["pet", "food"])
        gone.unlink()
        rows = self.rows(gone)
        labels.rebuild(self.folder, rows, labeller)
        self.assertEqual(gone.read_bytes(), data)
        self.assertEqual(self.views(), [f"food/{gone.name}", f"pet/{gone.name}"])
        for label in ("food", "pet"):
            self.assertTrue(os.path.samefile(self.folder / "labels" / label / gone.name, gone))
        self.assertEqual(rows[0][1]["path"], str(gone))

    def test_a_link_to_a_file_still_held_elsewhere_is_removed(self):
        elsewhere = self.photo("food@a.jpg")
        (self.folder / "labels/food").mkdir(parents=True)
        os.link(elsewhere, self.folder / "labels/food/food@a.jpg")
        labels.rebuild(self.folder, [], Labeller())
        self.assertFalse((self.folder / "labels").exists())
        self.assertTrue(elsewhere.is_file())

    def test_a_last_copy_no_row_names_is_never_deleted(self):
        (self.folder / "labels/food").mkdir(parents=True)
        orphan = self.folder / "labels/food/mine.jpg"
        orphan.write_bytes(b"only here")
        labels.rebuild(self.folder, [], Labeller())
        self.assertEqual(orphan.read_bytes(), b"only here")
        labels.remove(self.folder)
        self.assertEqual(orphan.read_bytes(), b"only here")

    def test_off_puts_a_last_copy_back_before_deleting_labels(self):
        gone = self.photo("posts/food@a.jpg")
        Labeller().link(gone, self.folder, ["food"])
        gone.unlink()
        self.assertTrue(labels.remove(self.folder, self.rows(gone)))
        self.assertTrue(gone.is_file())
        self.assertFalse((self.folder / "labels").exists())

    def test_a_label_the_row_no_longer_has_is_removed(self):
        path = self.photo("posts/food+pet@a.jpg")
        labeller = Labeller()
        labeller.link(path, self.folder, ["food", "pet"])
        labels.rebuild(self.folder, self.rows(path, labels_of={path.name: ["pet"]}), labeller)
        self.assertEqual(self.views(), [f"pet/{path.name}"])

    def test_nothing_left_removes_labels_and_remove_deletes_only_links(self):
        path = self.photo("food@a.jpg")
        Labeller().link(path, self.folder, ["food"])
        self.assertTrue(labels.remove(self.folder))
        self.assertFalse((self.folder / "labels").exists())
        self.assertTrue(path.is_file())
        self.assertFalse(labels.remove(self.folder))
        labels.rebuild(self.folder, [], Labeller())
        self.assertFalse((self.folder / "labels").exists())

    def test_apply_labels_only_what_has_no_labels_then_rebuilds(self):
        new, old, sound = self.photo("food@a.jpg"), self.photo("pet@b.jpg"), self.file("tone.mp3", b"ID3")
        saved = {"a": {"path": str(new)}, "b": {"path": str(old), "labels": ["car"]}, "c": {"path": str(sound)}}
        done = labels.apply(saved, list(saved.items()), self.folder, Labeller(), label="aaa:example_user")
        self.assertEqual(done, 1)
        self.assertEqual(saved["a"]["labels"], ["food"])
        self.assertEqual(saved["b"]["labels"], ["car"])
        self.assertNotIn("labels", saved["c"])
        self.assertEqual(self.views(), [f"car/{old.name}", f"food/{new.name}"])


@needs_pillow
class EngineTest(EngineCase):
    def setUp(self):
        super().setUp()
        # The up-front question answered yes, and the model "loaded".
        for patcher in (mock.patch.object(media.Media, "labels", labelled),
                        mock.patch.object(media, "prepare_labels", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bodies = {"food@a.jpg": noise("JPEG"), "pet+food@b.jpg": noise("JPEG")}
        for name, body in self.bodies.items():
            self.routes[f"{CDN}/{name}"] = httpx.Response(200, content=body)
        self.source = make_source("aaa", files=[File(f"{CDN}/{name}", name=name) for name in self.bodies])
        self.engine = Engine(home=self.home, sources={"aaa": self.source})
        self.folder = self.target_dir("aaa", "example_user")

    def views(self):
        return sorted(str(path.relative_to(self.folder / "labels")) for path in (self.folder / "labels").glob("*/*"))

    def saved(self):
        with kv.open(self.home / "targets.db", table="targets") as targets:
            return dict(targets.items())["aaa:example_user"]

    def test_download_links_records_and_saves_the_setting(self):
        report = self.engine.download(["aaa:example_user"], classify=True)
        self.assertEqual(report.files, 2)
        self.assertEqual(self.views(), ["food/food@a.jpg", "food/pet+food@b.jpg", "pet/pet+food@b.jpg"])
        rows = {Path(row["path"]).name: row for row in history(self.home).values()}
        self.assertEqual(rows["pet+food@b.jpg"]["labels"], ["pet", "food"])
        self.assertIs(self.saved()["classify"], True)
        # The saved setting round trip: the plan reads it back, and a run says it uses it.
        [(_, options)] = self.engine._plan([])
        self.assertTrue(options.classify)
        self.screen.dest.truncate(0)
        self.engine.download(["aaa:example_user"])
        self.assertIn("aaa:example_user: --classify (saved)", self.screen.dest.getvalue())
        self.assertIs(self.saved()["classify"], True)

    def test_turning_it_on_labels_the_files_already_saved(self):
        self.engine.download(["aaa:example_user"])
        self.assertFalse((self.folder / "labels").exists())
        self.assertIs(self.saved()["classify"], False)
        self.engine.download(["aaa:example_user"], classify="on")
        self.assertEqual(self.views(), ["food/food@a.jpg", "food/pet+food@b.jpg", "pet/pet+food@b.jpg"])
        self.assertTrue(all("labels" in row for row in history(self.home).values()))

    def test_off_removes_labels_and_clears_the_setting(self):
        self.engine.download(["aaa:example_user"], classify=True)
        self.engine.download(["aaa:example_user"], classify="off")
        self.assertFalse((self.folder / "labels").exists())
        self.assertIs(self.saved()["classify"], False)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["food@a.jpg", "pet+food@b.jpg"])
        [(_, options)] = self.engine._plan([])
        self.assertFalse(options.classify)

    def test_sync_rebuilds_missing_links_and_removes_stale_ones(self):
        self.engine.download(["aaa:example_user"], classify=True)
        (self.folder / "labels/pet/pet+food@b.jpg").unlink()
        stray = self.folder / "labels/food/old.jpg"
        stray.symlink_to("../../old.jpg")
        [report] = self.engine.sync().targets
        self.assertEqual(self.views(), ["food/food@a.jpg", "food/pet+food@b.jpg", "pet/pet+food@b.jpg"])
        # The links aren't counted as files of their own.
        self.assertIn("aaa:example_user: 2 files", self.screen.dest.getvalue())
        self.assertIsNone(report.error)

    def test_sync_puts_back_a_file_deleted_by_hand_from_its_link(self):
        self.engine.download(["aaa:example_user"], classify=True)
        original = self.folder / "food@a.jpg"
        original.unlink()
        before = len(self.requests)
        self.engine.sync()
        self.assertTrue(original.is_file())
        self.assertTrue(os.path.samefile(original, self.folder / "labels/food/food@a.jpg"))
        # Back from its link, so neither repaired nor downloaded again.
        self.assertFalse(any(str(request.url).endswith("food@a.jpg") for request in self.requests[before:]))
        self.assertIn("0 repaired, 0 missing", self.screen.dest.getvalue())

    def test_the_model_is_asked_about_before_any_download_and_a_no_turns_classify_off(self):
        events = []

        def declined():
            events.append(("asked", len(self.requests)))
            raise RuntimeError("Media.labels needs its model, and its download was declined")

        def never(self, **options):
            raise AssertionError("Media.labels ran though classify was off")

        with mock.patch.object(media, "prepare_labels", declined), mock.patch.object(media.Media, "labels", never):
            report = self.engine.download(["aaa:example_user"], classify=True)
        self.assertEqual(events, [("asked", 0)])
        self.assertEqual(report.files, 2)
        self.assertFalse((self.folder / "labels").exists())
        warnings = [line for line in self.screen.dest.getvalue().splitlines() if "classify" in line and line.startswith("!")]
        self.assertEqual(warnings, ["! classify is off for this run: Media.labels needs its model, and its download "
                                    "was declined"])
        # The setting is saved all the same: it is this run that goes without.
        self.assertIs(self.saved()["classify"], True)

    def test_without_a_terminal_nothing_is_asked_and_the_run_goes_on_unlabelled(self):
        from shared import _classify

        with mock.patch.object(media, "prepare_labels", _classify.prepare), \
                mock.patch.object(_classify, "_loaded", None), mock.patch.object(_classify, "_failed", None), \
                mock.patch.object(_classify, "_declined", set()), \
                mock.patch.object(_classify, "_folder", lambda: self.home / "no-models"), \
                mock.patch("shared.prompt.confirm") as confirm:
            report = self.engine.download(["aaa:example_user"], classify=True)
        confirm.assert_not_called()
        self.assertEqual(report.files, 2)
        self.assertFalse((self.folder / "labels").exists())
        self.assertIn("classify is off for this run: Media.labels needs", self.screen.dest.getvalue())

    def test_a_saved_classify_asks_up_front_too_in_sync(self):
        self.engine.download(["aaa:example_user"], classify=True)
        with mock.patch.object(media, "prepare_labels") as prepare:
            self.engine.sync()
            self.engine.download(["aaa:example_user"])
        self.assertEqual(prepare.call_count, 2)

    def test_classify_other_than_on_or_off_is_a_usage_error(self):
        with self.assertRaisesRegex(ValueError, "^--classify takes on or off, not 'yes'"):
            self.engine.download(["aaa:example_user"], classify="yes")
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
