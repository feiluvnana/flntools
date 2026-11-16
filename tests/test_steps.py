"""The steps after a file lands (`flnd._steps`): unpack and compress, end to end through the engine.

Every run here fakes the network (`flnd._net.fake`, from `test_engine`) and uses synthetic
photos and archives. The chain's own threads are real.
"""

from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path
from unittest import mock

import fakes
from flnd import _net

from flnd import _home, _steps, _tools
from flnd.engine import Engine
from flnd.source import File
from test_archive import CRYPTO_ZIP
from test_engine import CDN, EngineCase, make_source
from test_shrink import photo


def zipped(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return out.getvalue()


def jpeg(seed: int = 1) -> bytes:
    out = io.BytesIO()
    photo(320, 240, seed).save(out, "JPEG", quality=95)
    return out.getvalue()


class StepsCase(EngineCase):
    def serve_files(self, served: dict[str, bytes]) -> list[File]:
        """Serve each named body from the fake CDN, and return the files a source lists."""
        for name, body in served.items():
            self.routes[f"{CDN}/{name}"] = _net.reply(200, content=body)
        return [File(f"{CDN}/{name}", name=name) for name in served]

    def engine(self, files) -> Engine:
        source = make_source("aaa", files=files)
        return Engine(home=self.home, sources={"aaa": source})

    def history(self) -> dict:
        return fakes.history(self.home)

    @property
    def folder(self) -> Path:
        return self.target_dir("aaa", "a")


class UnpackTest(StepsCase):
    def test_an_archive_extracts_beside_itself_and_is_kept(self):
        files = self.serve_files({"set.zip": zipped({"in/a.txt": b"alpha"})})
        self.engine(files).download(["aaa:a"], categories="all")
        self.assertEqual((self.folder / "set" / "in" / "a.txt").read_bytes(), b"alpha")
        self.assertTrue((self.folder / "set.zip").is_file())
        row = self.history()["aaa:set.zip"]
        self.assertEqual((row["unpacked"], row["folder"]), ("extracted", str(self.folder / "set")))
        self.assertIn("Media: 1 archive extracted", self.dest.getvalue())

    def test_a_password_from_the_source_opens_the_archive(self):
        self.routes[f"{CDN}/locked.zip"] = _net.reply(200, content=CRYPTO_ZIP)
        files = [File(f"{CDN}/locked.zip", name="locked.zip", password="pw")]
        with _tools.fake({}):
            self.engine(files).download(["aaa:a"], categories="all")
        self.assertEqual((self.folder / "locked" / "a.txt").read_bytes(), b"secret")

    def test_a_wrong_password_keeps_the_archive_and_records_failed(self):
        self.routes[f"{CDN}/locked.zip"] = _net.reply(200, content=CRYPTO_ZIP)
        files = [File(f"{CDN}/locked.zip", name="locked.zip", password="nope")]
        with _tools.fake({}):
            report = self.engine(files).download(["aaa:a"], categories="all", compress=True)
        self.assertTrue(report.ok)
        self.assertTrue((self.folder / "locked.zip").is_file())
        self.assertFalse((self.folder / "locked").exists())
        self.assertEqual(self.history()["aaa:locked.zip"]["unpacked"], "failed")
        self.assertIn("locked.zip: not extracted: locked.zip: wrong or missing password",
                      self.dest.getvalue())

    def test_an_unsafe_archive_is_refused(self):
        files = self.serve_files({"evil.zip": zipped({"../../evil.txt": b"x"})})
        self.engine(files).download(["aaa:a"], categories="all")
        self.assertFalse((self.folder.parent / "evil.txt").exists())
        self.assertFalse((self.folder / "evil").exists())
        self.assertEqual(self.history()["aaa:evil.zip"]["unpacked"], "failed")

    def test_compress_removes_the_archive_after_a_verified_extract_and_never_fetches_it_again(self):
        files = self.serve_files({"set.zip": zipped({"a.txt": b"alpha"})})
        engine = self.engine(files)
        engine.download(["aaa:a"], categories="all", compress=True)
        self.assertFalse((self.folder / "set.zip").exists())
        self.assertEqual((self.folder / "set" / "a.txt").read_bytes(), b"alpha")
        self.requests.clear()
        report = engine.download(["aaa:a"], categories="all", compress=True)
        self.assertEqual((report.files, self.requests), (0, []))


class CompressTest(StepsCase):
    def test_without_compress_a_photo_is_left_as_it_came(self):
        body = jpeg()
        self.engine(self.serve_files({"a.jpg": body})).download(["aaa:a"], categories="all")
        self.assertEqual((self.folder / "a.jpg").read_bytes(), body)
        self.assertEqual(self.history()["aaa:a.jpg"]["shrink"], "")

    def test_compress_moves_the_file_and_its_row_together(self):
        engine = self.engine(self.serve_files({"a.jpg": jpeg()}))
        engine.download(["aaa:a"], categories="all", compress=True)
        row = self.history()["aaa:a.jpg"]
        self.assertFalse((self.folder / "a.jpg").exists())
        made = self.folder / "a.avif"
        self.assertEqual(row["path"], str(made))
        self.assertEqual((row["shrink"], row["size"]), ("compressed", made.stat().st_size))
        self.assertGreater(row["before"], row["size"])
        self.assertIn("pillow", row["tools"])
        self.assertIn("Media: 1 file compressed", self.dest.getvalue())
        # The row names the new file, so the next run finds it saved.
        self.requests.clear()
        self.assertEqual(engine.download(["aaa:a"], categories="all").files, 0)
        self.assertEqual(self.requests, [])

    def test_sync_compress_takes_files_saved_before_once(self):
        engine = self.engine(self.serve_files({"a.jpg": jpeg(1), "b.jpg": jpeg(2)}))
        engine.download(["aaa:a"], categories="all", sync=True)
        engine.sync(compress=True)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["a.avif", "b.avif"])
        rows = self.history()
        self.assertEqual({row["shrink"] for row in rows.values()}, {"compressed"})
        with mock.patch("flnd._shrink.shrink") as shrink:
            engine.sync(compress=True)
        shrink.assert_not_called()


class BacklogTest(StepsCase):
    def chain(self):
        settings = mock.Mock(db=None, stop=None, home=self.home)
        return _steps.attached(settings)

    def rows(self, **found):
        """The file rows `Home.unfinished` would give, from `key=(name, fields)` pairs."""
        folder = self.home / "media" / "aaa" / "a"
        return [_home.FileRow(item, "aaa:a", f"{folder}/{name}", 10, **fields)
                for item, (name, fields) in found.items()]

    def test_a_done_file_is_skipped_without_a_stat(self):
        from flnd import _shrink

        tools = _shrink.tools(_shrink.Kind.JPEG)
        rows = self.rows(**{f"n{n}": (f"{n}.jpg", {"shrink": "compressed"}) for n in range(50)},
                         kept=("kept.jpg", {"shrink": "kept", "tools": tools}))
        with mock.patch("pathlib.Path.stat", side_effect=AssertionError("stat")), \
                self.chain() as chain:
            self.assertEqual(chain.backlog(rows), 0)

    def test_a_tool_upgrade_retries_kept_and_failed_files(self):
        rows = self.rows(**{"aaa:kept": ("kept.jpg", {"shrink": "kept", "tools": "old"}),
                            "aaa:failed": ("f.mp4", {"shrink": "failed", "tools": "old"}),
                            "aaa:new": ("new.png", {}), "aaa:zip": ("a.zip", {}),
                            "aaa:zipped": ("b.zip", {"unpacked": "extracted"})})
        with mock.patch.object(_steps.Chain, "landed") as landed, self.chain() as chain:
            self.assertEqual(chain.backlog(rows), 4)
        self.assertEqual(sorted(call.args[0].key for call in landed.call_args_list),
                         ["aaa:failed", "aaa:kept", "aaa:new", "aaa:zip"])

    def test_after_ctrl_c_queued_files_are_dropped(self):
        stop = mock.Mock(soon=threading.Event(), now=threading.Event())
        stop.soon.set()
        chain = _steps.Chain(None, stop)
        with mock.patch.object(_steps.Chain, "_run") as run:
            chain.landed(_steps.Landed("aaa:a", Path("a.jpg"), compress=True))
            chain.close()
        run.assert_not_called()


if __name__ == "__main__":
    import unittest

    unittest.main()
