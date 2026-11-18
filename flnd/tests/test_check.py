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
from flnd.engine import check as _check
from flnd.engine import home as _home


class CheckCase(ChainCase):
    def check(self, *, fix=False):
        with _home.open(self.home) as db:
            return _check.check(self.home, db, fix=fix)

    def saved_two(self):
        self.config('[defaults]\ndedupe = "off"\n')
        self.engine().download(["aaa:a", "aaa:b"], categories="all")

    def orphan(self, name="lost.jpg", body=b"orphan", folder="a"):
        """A whole file with no row, as a crash between its rename and its row leaves it."""
        path = self.target_dir("aaa", folder) / name
        path.write_bytes(body)
        return path

    def aged(self):
        """`time.time` moved on, so every file renamed into place has settled."""
        return mock.patch("flnd.engine.check.time.time", return_value=time.time() + 2 * _check.SETTLING)


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
        self.engine().download(["aaa:a"], categories="all")
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
        self.engine().download(["aaa:a"], categories="all")
        folder = self.target_dir("aaa", "a")
        (folder / "set").mkdir()
        (folder / "set" / "in.txt").write_text("alpha")
        with _home.open(self.home) as db:
            db.update("aaa:a.jpg", unpacked="extracted", folder=str(folder / "set"))
        self.path("a").unlink()
        with self.aged():
            self.assertTrue(self.check().ok)


class FixTest(CheckCase):
    def test_fix_makes_the_history_match_the_disk_and_touches_no_file(self):
        self.saved_two()
        self.path("a").unlink()
        self.path("b").write_bytes(b"cut")
        lost = self.orphan()
        before = sorted(path.name for path in self.home.rglob("*.jpg"))
        with self.aged():
            found = self.check(fix=True)
        self.assertTrue(found.fixed and found.ok)
        self.assertEqual(sorted(path.name for path in self.home.rglob("*.jpg")), before)
        rows = self.rows()
        self.assertNotIn("aaa:a.jpg", rows)
        self.assertEqual((rows["aaa:b.jpg"].size, rows["aaa:b.jpg"].hash),
                         (3, hashlib.sha256(b"cut").digest()))
        self.assertNotIn("aaa:a/lost.jpg", rows)
        with self.aged():
            again = self.check()
        self.assertEqual(again.unknown, (lost,))
        self.assertFalse(again.ok and not again.fixed)

    def test_fix_refuses_when_media_is_missing(self):
        self.saved_two()
        shutil.rmtree(self.home / _home.MEDIA)
        with self.aged(), self.assertRaises(flnd.DiskError):
            self.check(fix=True)


class LibraryAndCliTest(CheckCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), self.aged():
            code = cli.main(["--home", str(self.home), "check", *argv])
        return code, out.getvalue()

    def test_the_library_returns_a_checked(self):
        self.saved_two()
        with flnd.open(self.home) as fl, self.aged():
            found = fl.check()
        self.assertIsInstance(found, flnd.Checked)
        self.assertTrue(found.ok)

    def test_the_cli_lists_each_problem_and_exits_1_until_fixed(self):
        self.saved_two()
        self.path("a").unlink()
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("missing", out)
        self.assertEqual(self.run_cli("--fix")[0], 0)
        self.assertEqual(self.run_cli(), (0, "checked 1 file: history and disk agree\n"))

    def test_json_prints_one_checked_object(self):
        self.saved_two()
        self.path("a").unlink()
        code, out = self.run_cli("--json")
        [line] = out.splitlines()
        found = json.loads(line)
        self.assertEqual((code, found["type"], found["files"]), (1, "Checked", 2))
        self.assertEqual([record["key"] for record in found["missing"]], ["aaa:a.jpg"])


if __name__ == "__main__":
    import unittest

    unittest.main()
