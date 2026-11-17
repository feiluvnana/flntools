"""The steps after a file lands (`flnd._steps`): unpack, end to end through the engine.

Every run here fakes the network (`flnd._net.fake`, from `test_engine`) and uses synthetic
archives. The chain's own threads are real.
"""

from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path
from unittest import mock

from test_archive import CRYPTO_ZIP
from test_engine import CDN, EngineCase, make_source

from flnd import _net, _steps, _tools
from flnd.engine import Engine
from flnd.source import File


def zipped(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
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
        import fakes

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
            report = self.engine(files).download(["aaa:a"], categories="all")
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

    def test_after_ctrl_c_queued_files_are_dropped(self):
        stop = mock.Mock(soon=threading.Event(), now=threading.Event())
        stop.soon.set()
        chain = _steps.Chain(None, stop)
        with mock.patch.object(_steps.Chain, "_run") as run:
            chain.landed(_steps.Landed("aaa:a", Path("a.zip")))
            chain.close()
        run.assert_not_called()


if __name__ == "__main__":
    import unittest

    unittest.main()