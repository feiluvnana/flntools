"""Part names and sidecars (`flnkit.files`)."""

import fnmatch
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flnd.ui.log import ui
from flnkit import files as _files


class FilesCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(ui.scope(dest=io.StringIO(), width=300, color=False))
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def part(self, name, **options):
        return _files.part(self.tmp / name, resume=True, **options)


class FilesTest(FilesCase):
    def test_part_names_match_what_sweep_removes(self):
        with _files.part(self.tmp / "a.jpg") as held:
            self.assertEqual(held.path, self.tmp / "a.jpg.part")
            self.assertTrue(fnmatch.fnmatch(held.path.name, "*.part"))

    def test_a_sidecar_is_never_torn_by_two_threads_or_a_failed_write(self):
        # A part written in segments keeps its sidecar from several threads at once.
        metas = [{"size": n, "segments": [[0, n, n]] * (n % 9 + 1)} for n in range(1, 300)]
        with self.assertRaises(RuntimeError), self.part("a.bin") as held:
            held.write(b"x")
            held.keep(metas[0])
            seen, done = [], threading.Event()

            def read():
                while not done.is_set():
                    seen.append(_files._read_side(held.side_path))

            reader = threading.Thread(target=read)
            reader.start()
            with ThreadPoolExecutor(4) as pool:
                list(pool.map(held.keep, metas))
            done.set()
            reader.join()
            self.assertEqual([one for one in seen if one not in metas][:3], [])
            # A write that fails part-way leaves the sidecar before it.
            with self.assertRaises(TypeError):
                held.keep({"size": 1, "segments": [[0, 1, object()]]})
            self.assertIn(_files._read_side(held.side_path), metas)
            raise RuntimeError("the transfer failed")


if __name__ == "__main__":
    unittest.main()
