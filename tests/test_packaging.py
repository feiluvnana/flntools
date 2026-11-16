import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


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
                self.assertIn("flnd/py.typed", archive.namelist())


class PublicNamesTest(unittest.TestCase):
    def test_http_is_back(self):
        # Phase 4 removed `http`; Phase 6's D3 brought it back as `fetch` and `Response`.
        from flnd import _http

        self.assertTrue(callable(_http.fetch))


def run_python(code: str) -> subprocess.CompletedProcess:
    """Run code in a fresh interpreter from the repository root, so imports start clean."""
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)


def missing_dependency_message(module: str, package: str) -> str:
    """The ImportError text from `from flnd import <module>` when `package` cannot be imported."""
    code = (f"import sys; sys.modules[{package!r}] = None\n"
            f"try:\n    from flnd import {module}\nexcept ImportError as error:\n    print(error)")
    return run_python(code).stdout.strip()


class MissingDependencyTest(unittest.TestCase):
    # The lazy-loading guards (which modules a bare import loads) return with the lazy
    # `flnd/__init__` of the CLI rebuild: `import flnd` still loads the engine until then, and the
    # engine imports httpx itself, so a missing httpx is reported by the engine, not by `_http`.

    def test_missing_parsel_names_the_package_to_install(self):
        message = missing_dependency_message("_html", "parsel")
        self.assertEqual(message, "flnd._html needs parsel: pip install parsel")



if __name__ == "__main__":
    unittest.main()
