import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_files() -> list[str]:
    """Every file of src/ that isn't Python, as the wheel names it: `flnd/extension/popup.js`."""
    found = [path for path in (ROOT / "src").rglob("*")
             if path.is_file() and path.suffix not in (".py", ".pyc")
             and "__pycache__" not in path.parts]
    return sorted(f"flnd/{path.relative_to(ROOT / 'src').as_posix()}" for path in found)


class WheelTest(unittest.TestCase):
    @unittest.skipIf(shutil.which("uv") is None, "uv is not installed")
    def test_wheel_ships_every_data_file(self):
        # A folder of data (the docs, the Chrome extension) ships only when pyproject.toml maps
        # it. The build runs on a clean copy, since setuptools reuses build/lib, where a file
        # from an older build would hide a missing mapping.
        with tempfile.TemporaryDirectory() as temp:
            copy, out_dir = Path(temp) / "project", Path(temp) / "dist"
            ignore = shutil.ignore_patterns("__pycache__")
            for name in ("src", "flnkit"):
                shutil.copytree(ROOT / name, copy / name, ignore=ignore)
            for name in ("pyproject.toml", "setup.py", "README.md"):
                shutil.copy2(ROOT / name, copy / name)
            subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", str(out_dir), str(copy)], check=True,
                capture_output=True,
            )
            # setup.py moves the egg-info into build/, out of the project folder.
            self.assertFalse((copy / "flntools.egg-info").exists())
            wheel = next(out_dir.glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())
        self.assertIn("flnd/extension/manifest.json", data_files())
        self.assertEqual([name for name in data_files() if name not in names], [],
                         "map these in [tool.setuptools.package-data] in pyproject.toml")
        self.assertTrue(any(name.startswith("flnkit/") for name in names))


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


class ListedPackagesTest(unittest.TestCase):
    def test_every_package_folder_is_listed_in_pyproject(self):
        # A new subpackage (a folder with an __init__.py) must be in the wheel: setuptools
        # ships only the packages it is given, so a missing one is silently left out.
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        listed = set(project["tool"]["setuptools"]["packages"])
        folders = {"flnd": ROOT / "src", "flnkit": ROOT / "flnkit"}
        expected = set()
        for name, folder in folders.items():
            for init in folder.rglob("__init__.py"):
                parts = init.parent.relative_to(folder).parts
                expected.add(".".join((name, *parts)))
        self.assertEqual(sorted(expected - listed), [],
                         "add these to [tool.setuptools] packages in pyproject.toml")


class EditableTest(unittest.TestCase):
    def test_both_packages_import_from_outside_the_folder(self):
        # `make setup` installs the project editable: flnd must come from src/ and flnkit from
        # flnkit/, whatever the working directory.
        code = "import flnd, flnkit; print(flnd.__file__); print(flnkit.__file__)"
        result = subprocess.run([sys.executable, "-c", code], cwd=tempfile.gettempdir(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        flnd_file, flnkit_file = (Path(line).resolve() for line in result.stdout.split("\n")[:2])
        self.assertTrue(flnd_file.is_relative_to(ROOT / "src"), flnd_file)
        self.assertTrue(flnkit_file.is_relative_to(ROOT / "flnkit"), flnkit_file)


# What only a command may load: the engine, the network, the browser and the screen. `import
# flnd` must load none of them, or every script and `flnd --help` pays.
HEAVY = {"flnd.engine.crawl", "flnd.engine.ctx", "playwright", "rich", "curl_cffi",
         "msgspec"}


class BundleTest(unittest.TestCase):
    def test_the_bundle_collects_the_modules_flnd_imports_by_name(self):
        # Plugins and libraries load lazily by name (`importlib.import_module`), which
        # PyInstaller's import scan can't follow: without this flag, `flnd sources` in the
        # bundle fails with "No module named 'flnd.plugins.tik'".
        script = (ROOT / "tests" / "dist.py").read_text(encoding="utf-8")
        self.assertIn('"--collect-submodules", "flnd"', script)
        self.assertIn('"--collect-submodules", "flnkit"', script)
        self.assertIn("flnd/extension", script)


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

    def test_missing_curl_cffi_names_the_package_to_install(self):
        # `flnkit.net` imports curl_cffi on its first request, so that is where it is missed.
        code = ("import sys, asyncio; sys.modules['curl_cffi'] = None\n"
                "from flnkit import net\n"
                "try:\n    net.Session()._session()\nexcept ImportError as error:\n    print(error)")
        self.assertEqual(run_python(code).stdout.strip(),
                         "flnkit.net needs curl_cffi: pip install curl_cffi")

    def test_a_name_loads_its_module_on_first_use(self):
        code = ("import sys, flnd\nbefore = 'flnd._library' in sys.modules\n"
                "print(before, flnd.open.__module__, 'open' in dir(flnd))")
        self.assertEqual(run_python(code).stdout.strip(), "False flnd._library True")

    def test_missing_selectolax_names_the_package_to_install(self):
        # `flnkit.html` imports selectolax on its first parse, so that is where it is missed.
        code = ("import sys; sys.modules['selectolax'] = None\n"
                "from flnkit import html\n"
                "try:\n    html.parse('<p>a</p>')\nexcept ImportError as error:\n    print(error)")
        self.assertEqual(run_python(code).stdout.strip(),
                         "flnkit.html needs selectolax: pip install selectolax")

    def test_importing_the_html_library_loads_no_parser(self):
        # `flnd.plugin` reads `dig` from the kit, so importing the HTML library must stay free.
        self.assertEqual(loaded_after("from flnkit import html") & {"selectolax", "msgspec"}, set())



if __name__ == "__main__":
    unittest.main()
