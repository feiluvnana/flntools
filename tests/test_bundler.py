"""shared.bundler: the PyInstaller command line, a fake PyInstaller, and one real build when enabled.

The real build bundles Playwright's driver and takes a minute or more, so it runs only with
FLNTOOLS_BUILD_TESTS=1 and PyInstaller installed.
"""

import importlib.util
import logging
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import shared
from shared import bundler

ROOT = Path(__file__).resolve().parent.parent
HAS_PYINSTALLER = importlib.util.find_spec("PyInstaller") is not None


def values(argv, flag):
    return [argv[index + 1] for index, item in enumerate(argv) if item == flag]


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)

    def argv(self, **options):
        defaults = {"name": "tool", "hidden": (), "dest": Path(self.work.name) / "dist", "work": Path(self.work.name)}
        return bundler._arguments("tool.py", **{**defaults, **options})

    def test_one_folder_with_no_spec_file_left_behind(self):
        argv = self.argv()
        self.assertEqual(argv[0], str(Path("tool.py").resolve()))
        self.assertIn("--onedir", argv)
        self.assertIn("--noconfirm", argv)
        self.assertEqual(values(argv, "--name"), ["tool"])
        self.assertEqual(values(argv, "--distpath"), [str(Path(self.work.name) / "dist")])
        self.assertEqual(values(argv, "--specpath"), [self.work.name])
        self.assertEqual(values(argv, "--workpath"), [str(Path(self.work.name) / "build")])

    def test_shared_lazy_modules_and_extras_are_hidden_imports(self):
        hidden = values(self.argv(hidden=("mypkg.plugins", "mypkg.more")), "--hidden-import")
        for name in shared.LAZY_MODULES:
            if name != "bundler":
                self.assertIn(f"shared._{name}", hidden)
        self.assertIn("mypkg.plugins", hidden)
        self.assertIn("mypkg.more", hidden)

    def test_the_bundler_and_pyinstaller_stay_out_of_the_bundle(self):
        argv = self.argv()
        self.assertNotIn("shared._bundler", values(argv, "--hidden-import"))
        self.assertEqual(values(argv, "--exclude-module"), ["shared._bundler", "PyInstaller"])

    def test_packages_are_hidden_imports_only_when_installed(self):
        installed = {"playwright", "cryptography", "parsel"}
        with mock.patch.object(bundler, "_installed", lambda module: module in installed):
            hidden = values(self.argv(), "--hidden-import")
        self.assertLessEqual(installed, set(hidden))
        self.assertNotIn("telethon", hidden)
        self.assertNotIn("py7zr", hidden)

    def test_shared_is_on_the_search_path(self):
        self.assertIn(str(Path(shared.__file__).resolve().parent.parent), values(self.argv(), "--paths"))


class FakePyInstallerTest(unittest.TestCase):
    """`build` with a stand-in PyInstaller, so these run without it installed."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.calls = []
        self.should_fail = False
        main = types.ModuleType("PyInstaller.__main__")
        setattr(main, "run", self.run_pyinstaller)
        package = types.ModuleType("PyInstaller")
        setattr(package, "__main__", main)
        patcher = mock.patch.dict(sys.modules, {"PyInstaller": package, "PyInstaller.__main__": main})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_pyinstaller(self, argv):
        self.calls.append(argv)
        logger = logging.getLogger("PyInstaller")
        for number in range(30):
            logger.warning("step %d", number)
        if self.should_fail:
            logger.error("Unable to find module 'missing'")
            raise SystemExit(1)
        out, name = Path(values(argv, "--distpath")[0]), values(argv, "--name")[0]
        (out / name).mkdir(parents=True)
        (out / name / name).write_text("program")

    def test_returns_the_built_folder_with_data_folders_beside_the_program(self):
        assets = self.root / "assets"
        (assets / "inner").mkdir(parents=True)
        (assets / "inner" / "a.txt").write_text("a")
        folder = bundler.build("tool.py", name="tool", data=(assets,), dest=self.root / "dist")
        self.assertEqual(folder, (self.root / "dist" / "tool").resolve())
        self.assertEqual((folder / "assets" / "inner" / "a.txt").read_text(), "a")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((Path.cwd() / "tool.spec").exists())

    def test_a_failure_raises_with_pyinstallers_last_lines(self):
        self.should_fail = True
        with self.assertRaises(bundler.BuildError) as caught:
            bundler.build("tool.py", name="tool", dest=self.root / "dist")
        self.assertIsInstance(caught.exception, RuntimeError)
        message = str(caught.exception)
        self.assertIn("PyInstaller could not build tool", message)
        self.assertIn("Unable to find module 'missing'", message)
        self.assertIn("step 29", message)
        self.assertNotIn("step 0\n", message)

    def test_a_missing_data_folder_raises_before_building(self):
        with self.assertRaises(bundler.Error):
            bundler.build("tool.py", name="tool", data=(self.root / "nothing",), dest=self.root / "dist")
        self.assertEqual(self.calls, [])

    def test_build_refuses_single_str_for_hidden_or_data(self):
        with self.assertRaises(bundler.Error):
            bundler.build("tool.py", name="tool", hidden="mypkg")
        with self.assertRaises(bundler.Error):
            bundler.build("tool.py", name="tool", data=self.root)  # pyright: ignore[reportArgumentType]
        with self.assertRaises(bundler.Error):
            bundler.build("tool.py", name="tool", data="assets")
        self.assertEqual(self.calls, [])


class MissingExtraTest(unittest.TestCase):
    def test_without_pyinstaller(self):
        self.assertTrue(issubclass(bundler.MissingError, bundler.Error))
        self.assertTrue(issubclass(bundler.MissingError, shared.MissingError))
        with mock.patch.dict(sys.modules, {"PyInstaller": None, "PyInstaller.__main__": None}):
            with self.assertRaises(bundler.MissingError) as caught:
                bundler.build("tool.py", name="tool")
        self.assertEqual(str(caught.exception), "shared.bundler needs pyinstaller: uv sync --group dist")


@unittest.skipUnless(HAS_PYINSTALLER and os.environ.get("FLNTOOLS_BUILD_TESTS") == "1",
                     "set FLNTOOLS_BUILD_TESTS=1 with PyInstaller installed to run a real build")
class RealBuildTest(unittest.TestCase):
    def test_a_built_flnd_runs_sync(self):
        with tempfile.TemporaryDirectory() as folder:
            built = bundler.build(ROOT / "flnd" / "__main__.py", name="flnd", dest=Path(folder) / "dist")
            program = built / ("flnd.exe" if sys.platform == "win32" else "flnd")
            result = subprocess.run([str(program), "sync"], capture_output=True, text=True,
                                    timeout=120, cwd=folder)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("total", result.stdout + result.stderr)

    def test_a_built_script_finds_the_lazy_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "tiny.py"
            script.write_text(
                "from pathlib import Path\n"
                "from shared import archive, store, worker\n"
                "with store.open('state.db') as db:\n"
                "    db.set('n', 2)\n"
                "    factor = db.get('n')\n"
                "with worker.Pool() as pool:\n"
                "    for n in (1, 2, 3):\n"
                "        pool.submit(lambda x: x * factor, n)\n"
                "    print([r.value for r in pool.join()])\n"
                "Path('note.txt').write_text('hello')\n"
                "archive.pack('note.txt', 'note.7z', password='pw')\n"
                "print(Path(archive.unpack('note.7z', 'out', password='pw')[0]).read_text())\n")
            built = bundler.build(script, name="tiny", dest=Path(folder) / "dist")
            program = built / ("tiny.exe" if sys.platform == "win32" else "tiny")
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=120, cwd=folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        # 7z is looked up on PATH, so the bundled module packs and unpacks without a Python package.
        self.assertEqual(result.stdout.split(), ["[2,", "4,", "6]", "hello"])


if __name__ == "__main__":
    unittest.main()
