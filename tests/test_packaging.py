import shutil
import subprocess
import sys
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

    def test_http_is_gone(self):
        self.assertNotIn("http", shared.__all__)
        with self.assertRaises(AttributeError):
            shared.http  # noqa: B018


def run_python(code: str) -> subprocess.CompletedProcess:
    """Run code in a fresh interpreter from the repository root, so imports start clean."""
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)


def missing_extra_message(module: str, package: str) -> str:
    """The ImportError text from `from shared import <module>` when `package` cannot be imported."""
    code = (f"import sys; sys.modules[{package!r}] = None\n"
            f"try:\n    from shared import {module}\nexcept ImportError as error:\n    print(error)")
    return run_python(code).stdout.strip()


class LazyImportTest(unittest.TestCase):
    def test_phase_1_imports_no_extra(self):
        code = ("import sys\nfrom shared import cli, fmt, log, prompt, style, theme\n"
                "print(sorted(n for n in ('httpx', 'playwright', 'cryptography', 'parsel') if n in sys.modules))")
        self.assertEqual(run_python(code).stdout.strip(), "[]")

    def test_modules_load_on_first_use(self):
        code = ("import sys, shared\nbefore = 'shared._kv' in sys.modules\n"
                "print(before, shared.kv is sys.modules['shared._kv'], 'kv' in dir(shared))")
        self.assertEqual(run_python(code).stdout.strip(), "False True True")

    def test_unknown_name(self):
        with self.assertRaises(AttributeError):
            shared.nothing  # noqa: B018

    def test_missing_extras(self):
        message = missing_extra_message("selector", "parsel")
        self.assertEqual(message, "shared.selector needs parsel: pip install 'flntools[selector]'")


if __name__ == "__main__":
    unittest.main()
