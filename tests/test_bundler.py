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
        defaults = {"name": "tool", "hidden": (), "out": Path(self.work.name) / "dist", "work": Path(self.work.name)}
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

    def test_media_namespaces_and_engines_are_hidden_imports(self):
        # media loads its namespaces by name, and Pillow, pi-heif and imageio-ffmpeg inside functions.
        with mock.patch.object(bundler, "_installed", lambda module: True):
            hidden = values(self.argv(), "--hidden-import")
        for module in ("shared._media_image", "shared._media_video", "shared._media_audio", "PIL", "pi_heif",
                       "imageio_ffmpeg"):
            self.assertIn(module, hidden)

    def test_the_bundler_and_pyinstaller_stay_out_of_the_bundle(self):
        argv = self.argv()
        self.assertNotIn("shared._bundler", values(argv, "--hidden-import"))
        self.assertEqual(values(argv, "--exclude-module"), ["shared._bundler", "PyInstaller"])

    def test_packages_are_hidden_imports_only_when_installed(self):
        installed = {"playwright.sync_api", "cryptography", "parsel"}
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
        self.fail = False
        main = types.ModuleType("PyInstaller.__main__")
        main.run = self.run_pyinstaller
        package = types.ModuleType("PyInstaller")
        package.__main__ = main
        patcher = mock.patch.dict(sys.modules, {"PyInstaller": package, "PyInstaller.__main__": main})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_pyinstaller(self, argv):
        self.calls.append(argv)
        logger = logging.getLogger("PyInstaller")
        for number in range(30):
            logger.warning("step %d", number)
        if self.fail:
            logger.error("Unable to find module 'missing'")
            raise SystemExit(1)
        out, name = Path(values(argv, "--distpath")[0]), values(argv, "--name")[0]
        (out / name).mkdir(parents=True)
        (out / name / name).write_text("program")

    def test_returns_the_built_folder_with_data_folders_beside_the_program(self):
        assets = self.root / "assets"
        (assets / "inner").mkdir(parents=True)
        (assets / "inner" / "a.txt").write_text("a")
        folder = bundler.build("tool.py", name="tool", data=(assets,), out=self.root / "dist")
        self.assertEqual(folder, (self.root / "dist" / "tool").resolve())
        self.assertEqual((folder / "assets" / "inner" / "a.txt").read_text(), "a")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((Path.cwd() / "tool.spec").exists())

    def test_a_failure_raises_with_pyinstallers_last_lines(self):
        self.fail = True
        with self.assertRaises(RuntimeError) as caught:
            bundler.build("tool.py", name="tool", out=self.root / "dist")
        message = str(caught.exception)
        self.assertIn("PyInstaller could not build tool", message)
        self.assertIn("Unable to find module 'missing'", message)
        self.assertIn("step 29", message)
        self.assertNotIn("step 0\n", message)

    def test_a_missing_data_folder_raises_before_building(self):
        with self.assertRaises(FileNotFoundError):
            bundler.build("tool.py", name="tool", data=(self.root / "nothing",), out=self.root / "dist")
        self.assertEqual(self.calls, [])


class MissingExtraTest(unittest.TestCase):
    def test_without_pyinstaller(self):
        with mock.patch.dict(sys.modules, {"PyInstaller": None, "PyInstaller.__main__": None}):
            with self.assertRaises(ImportError) as caught:
                bundler.build("tool.py", name="tool")
        self.assertEqual(str(caught.exception), "shared.bundler needs pyinstaller: uv sync --group dist")


@unittest.skipUnless(HAS_PYINSTALLER and os.environ.get("FLNTOOLS_BUILD_TESTS") == "1",
                     "set FLNTOOLS_BUILD_TESTS=1 with PyInstaller installed to run a real build")
class RealBuildTest(unittest.TestCase):
    def test_a_built_program_probes_a_photo_and_a_video(self):
        # The ffmpeg binary is data to PyInstaller, so a lost executable bit would only show here.
        with tempfile.TemporaryDirectory() as folder:
            photo, video = Path(folder) / "a.png", Path(folder) / "v.mp4"
            from media_samples import sample

            photo.write_bytes(sample("small.png").read_bytes())
            video.write_bytes(sample("v.mp4").read_bytes())
            script = Path(folder) / "probe.py"
            script.write_text("import sys\nfrom shared import media\n"
                              "print(media.probe(sys.argv[1]).kind, media.probe(sys.argv[2]).video)\n")
            built = bundler.build(script, name="probe", out=Path(folder) / "dist")
            program = built / ("probe.exe" if sys.platform == "win32" else "probe")
            result = subprocess.run([str(program), str(photo), str(video)], capture_output=True, text=True,
                                    timeout=120, cwd=folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["image", "mpeg4"])

    def test_a_built_flnd_ships_ffmpeg_and_its_licences(self):
        # What `make dist` builds: flnd with licenses/, whose bundled ffmpeg runs from inside it.
        with tempfile.TemporaryDirectory() as folder:
            built = bundler.build(ROOT / "flnd" / "__main__.py", name="flnd", out=Path(folder) / "dist",
                                  data=(ROOT / "licenses",))
            program = built / ("flnd.exe" if sys.platform == "win32" else "flnd")
            result = subprocess.run([str(program), "sync"], capture_output=True, text=True,
                                    timeout=120, cwd=folder)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("total", result.stdout + result.stderr)
            self.assertTrue((built / "licenses" / "ffmpeg.txt").is_file())
            binaries = list(built.rglob("ffmpeg-*-v*"))
            self.assertTrue(binaries and os.access(binaries[0], os.X_OK), binaries)

    def test_a_built_script_finds_the_lazy_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "tiny.py"
            script.write_text(
                "from pathlib import Path\n"
                "from shared import archive, kv, worker\n"
                "with kv.open('state.db') as store:\n"
                "    store['n'] = 2\n"
                "    factor = store['n']\n"
                "print(worker.as_ordered(lambda n: n * factor, [1, 2, 3]))\n"
                "Path('note.txt').write_text('hello')\n"
                "archive.zip('note.txt', 'note.7z', password='pw')\n"
                "print(Path(archive.unzip('note.7z', 'out', password='pw')[0]).read_text())\n")
            built = bundler.build(script, name="tiny", out=Path(folder) / "dist")
            program = built / ("tiny.exe" if sys.platform == "win32" else "tiny")
            result = subprocess.run([str(program)], capture_output=True, text=True, timeout=120, cwd=folder)
        self.assertEqual(result.returncode, 0, result.stderr)
        # py7zr is imported by name inside archive, which PyInstaller can't see.
        self.assertEqual(result.stdout.split(), ["[2,", "4,", "6]", "hello"])


if __name__ == "__main__":
    unittest.main()
