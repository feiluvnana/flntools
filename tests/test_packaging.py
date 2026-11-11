import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

import shared

ROOT = Path(__file__).resolve().parent.parent


class WheelTest(unittest.TestCase):
    @unittest.skipIf(shutil.which("uv") is None, "uv is not installed")
    def test_wheel_ships_py_typed(self):
        with tempfile.TemporaryDirectory() as out_dir:
            subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", out_dir, str(ROOT)], check=True, capture_output=True,
            )
            wheel = next(Path(out_dir).glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                self.assertIn("shared/py.typed", archive.namelist())


class PublicNamesTest(unittest.TestCase):
    def test_all_lists_every_lazy_module(self):
        self.assertLessEqual(set(shared.LAZY_MODULES), set(shared.__all__))


if __name__ == "__main__":
    unittest.main()
