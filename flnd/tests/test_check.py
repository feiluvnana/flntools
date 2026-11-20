"""`fl.check()` and `flnd check [--fix]`: the history against the disk, and the repair.

Each home is made by a real run on a faked network (`test_chain.ChainCase`), then changed on
disk by hand, the way a crash, a person or another program would.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import time
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from test_chain import ChainCase

import flnd
from flnd import cli
from flnd.engine import store as _store


class CheckCase(ChainCase):
    def check(self, *, fix=False):
        with _store.open(self.home) as db:
            return _store.check(self.home, db, fix=fix)

    def saved_two(self):
        self.config('[defaults]\ndedupe = "off"\n')
        self.engine().download(["aaa:a|all", "aaa:b|all"])

    def orphan(self, name="lost.jpg", body=b"orphan", folder="a"):
        """A whole file with no row, as a crash between its rename and its row leaves it."""
        path = self.target_dir("aaa", folder) / name
        path.write_bytes(body)
        return path

    def aged(self):
        """`time.time` moved on, so every file renamed into place has settled."""
        return mock.patch("flnd.engine.store.time.time",
                          return_value=time.time() + 2 * _store.SETTLING)


class FindTest(CheckCase):
    def test_a_home_that_agrees_is_ok(self):
        self.saved_two()
        with self.aged():
            found = self.check()
        self.assertEqual((found.files, found.ok, found.fixed), (2, True, False))

    def test_missing_unknown_and_wrong_size_are_found(self):
        self.saved_two()
        self.path("a").unlink()
        self.path("b").write_bytes(b"cut")
        lost = self.orphan()
        with self.aged():
            found = self.check()
        self.assertEqual([record.key for record in found.missing], ["aaa:a.jpg"])
        self.assertEqual([record.key for record in found.resized], ["aaa:b.jpg"])
        self.assertEqual(found.unknown, (lost,))
        self.assertFalse(found.ok)

    def test_what_flnd_keeps_beside_its_files_is_never_unknown(self):
        self.config("[defaults]\nsidecars = true\n")
        self.engine().download(["aaa:a|all"])
        folder = self.target_dir("aaa", "a")
        (folder / ".DS_Store").write_bytes(b"finder")
        (folder / "x.jpg.part").write_bytes(b"half")
        (folder / "link.jpg").symlink_to("a.jpg")
        os.link(self.path("a"), folder / "hard.jpg")
        with self.aged():
            self.assertTrue(self.check().ok)

    def test_a_file_just_renamed_into_place_waits_for_its_row(self):
        self.saved_two()
        self.orphan()
        self.assertEqual(self.check().unknown, ())

    def test_an_archive_removed_once_extracted_is_not_missing_nor_its_contents_unknown(self):
        self.engine().download(["aaa:a|all"])
        folder = self.target_dir("aaa", "a")
        (folder / "set").mkdir()
        (folder / "set" / "in.txt").write_text("alpha")
        with _store.open(self.home) as db:
            db.update("aaa:a.jpg", unpacked="extracted", folder=str(folder / "set"))
        self.path("a").unlink()
        with self.aged():
            self.assertTrue(self.check().ok)


class FixTest(CheckCase):
    def test_fix_makes_the_history_match_the_disk_and_touches_no_file(self):
        self.saved_two()
        self.path("a").unlink()
        self.path("b").write_bytes(b"cut")
        before = sorted(path.name for path in self.home.rglob("*.jpg"))
        with self.aged():
            found = self.check(fix=True)
        self.assertTrue(found.fixed and found.ok)
        self.assertEqual(sorted(path.name for path in self.home.rglob("*.jpg")), before)
        rows = self.rows()
        self.assertNotIn("aaa:a.jpg", rows)
        self.assertEqual((rows["aaa:b.jpg"].size, rows["aaa:b.jpg"].hash),
                         (3, hashlib.sha256(b"cut").digest()))
        with self.aged():
            self.assertTrue(self.check().ok)

    def test_fix_refuses_when_media_is_missing(self):
        self.saved_two()
        shutil.rmtree(self.home / _store.MEDIA)
        with self.aged(), self.assertRaises(flnd.DiskError):
            self.check(fix=True)


class LibraryAndCliTest(CheckCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        # A terminal would be asked to repair; the test never asks.
        with redirect_stdout(out), redirect_stderr(io.StringIO()), self.aged(), \
                mock.patch("flnd.ui.forms.confirm", return_value=False):
            code = cli.main(["--home", str(self.home), *argv, "status"])
        return code, out.getvalue()

    def test_the_library_returns_a_checked(self):
        self.saved_two()
        with flnd.open(self.home) as fl, self.aged():
            found = fl.check()
        self.assertIsInstance(found, flnd.Checked)
        self.assertTrue(found.ok)

    def test_status_says_how_the_disk_compares_and_exits_1_until_it_agrees(self):
        self.saved_two()
        self.assertEqual(self.run_cli()[0], 0)
        self.path("a").unlink()
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("disk: 2 files: 1 missing", out)
        with flnd.open(self.home) as fl, self.aged():
            fl.check(fix=True)
        self.assertEqual(self.run_cli()[0], 0)

    def test_an_unknown_file_after_fix_is_not_ok(self):
        """E14: a repair never adopts an unknown file, so the home isn't ok until it goes."""
        self.saved_two()
        self.path("a").unlink()
        lost = self.orphan()
        with self.aged():
            found = self.check(fix=True)
        self.assertEqual((found.fixed, found.ok, found.unknown), (False, False, (lost,)))
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("1 unknown", out)
        lost.unlink()
        self.assertEqual(self.run_cli()[0], 0)

    def test_json_prints_one_status_object_with_the_disk_in_it(self):
        self.saved_two()
        self.path("a").unlink()
        code, out = self.run_cli("--json")
        [line] = out.splitlines()
        found = json.loads(line)
        self.assertEqual((code, found["type"], found["disk"]["files"]), (1, "Status", 2))
        self.assertEqual([record["key"] for record in found["disk"]["missing"]], ["aaa:a.jpg"])


if __name__ == "__main__":
    import unittest

    unittest.main()
