"""flnd/login.py: where a source's saved cookies are looked for. No browser, no network."""

import os
import tempfile
import unittest
from pathlib import Path

from flnd import login


class LoadTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home = self.root / ".flnd"

    def test_legacy_dirs_are_gone(self):
        """Fix #2: `E:/…` was read relative to the working directory on macOS and Linux."""
        self.assertFalse(hasattr(login, "LEGACY_DIRS"))
        legacy = self.root / "E:" / "flntools" / ".flnd" / "cookies"
        legacy.mkdir(parents=True)
        (legacy / "ins.txt").write_text("# Netscape HTTP Cookie File\n"
                                        ".instagram.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\tx\n")
        previous = Path.cwd()
        os.chdir(self.root)
        try:
            self.assertEqual(login.load(self.home, "ins"), [])
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
