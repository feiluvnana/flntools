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


def loaded_after(code: str) -> set[str]:
    """The top-level packages and flnd's modules a fresh interpreter has loaded after `code`."""
    shown = ("import sys\n"
             f"{code}\n"
             "print(' '.join(sorted(name for name in sys.modules\n"
             "                      if name.startswith('flnd') or '.' not in name)))")
    result = run_python(shown)
    if result.returncode:
        raise AssertionError(result.stderr)
    return set(result.stdout.split())


# What only a command may load: the engine, the network, the browser and the screen. `import
# flnd` must load none of them, or every script and `flnd --help` pays.
HEAVY = {"flnd.engine", "flnd.job", "flnd.site", "httpx", "playwright", "rich", "curl_cffi",
         "msgspec"}


class LazyImportTest(unittest.TestCase):
    def test_import_flnd_loads_only_its_own_init(self):
        loaded = loaded_after("import flnd")
        self.assertEqual({name for name in loaded if name.startswith("flnd")}, {"flnd"})
        self.assertEqual(loaded & HEAVY, set())

    def test_the_library_names_load_no_engine_network_or_browser(self):
        code = ("import flnd\n"
                "flnd.open, flnd.Run, flnd.Saved, flnd.Report, flnd.Error, flnd.Target")
        loaded = loaded_after(code)
        self.assertEqual(loaded & (HEAVY - {"rich", "msgspec"}), set())

    def test_every_public_name_resolves_and_is_listed(self):
        import flnd

        homes = set(vars(flnd)["_HOMES"])
        self.assertEqual(set(flnd.__all__), homes | {"__version__"})
        for name in flnd.__all__:
            with self.subTest(name=name):
                self.assertIsNotNone(getattr(flnd, name))
        with self.assertRaises(AttributeError):
            flnd.Engine  # noqa: B018 - the engine is no public name


class MissingDependencyTest(unittest.TestCase):

    def test_missing_httpx_names_the_package_to_install(self):
        # `import flnd` loads nothing, so `_http` is the first to meet a missing httpx.
        self.assertEqual(missing_dependency_message("_http", "httpx"),
                         "flnd._http needs httpx: pip install httpx")

    def test_a_name_loads_its_module_on_first_use(self):
        code = ("import sys, flnd\nbefore = 'flnd._library' in sys.modules\n"
                "print(before, flnd.open.__module__, 'open' in dir(flnd))")
        self.assertEqual(run_python(code).stdout.strip(), "False flnd._library True")

    def test_missing_parsel_names_the_package_to_install(self):
        message = missing_dependency_message("_html", "parsel")
        self.assertEqual(message, "flnd._html needs parsel: pip install parsel")



if __name__ == "__main__":
    unittest.main()
