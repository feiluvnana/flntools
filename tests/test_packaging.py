import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from importlib import metadata
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

    def test_http_is_back(self):
        # Phase 4 removed `http`; Phase 6's D3 brought it back as `fetch` and `Response`.
        self.assertIn("http", shared.LAZY_MODULES)
        self.assertTrue(callable(shared.http.fetch))


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

    def test_missing_httpx_names_the_http_extra(self):
        self.assertEqual(missing_extra_message("http", "httpx"), "shared.http needs httpx: pip install 'flntools[http]'")

    def test_http_loads_on_first_use(self):
        code = "import sys, shared\nprint('httpx' in sys.modules, shared.http is sys.modules['shared._http'])"
        self.assertEqual(run_python(code).stdout.strip(), "False True")

    def test_media_loads_on_first_use(self):
        code = "import sys, shared\nprint('shared._media' in sys.modules, shared.media is sys.modules['shared._media'])"
        self.assertEqual(run_python(code).stdout.strip(), "False True")


def load_dev() -> types.ModuleType:
    """scripts/dev.py as a module; scripts/ is not a package, so it is loaded from its path."""
    spec = importlib.util.spec_from_file_location("dev", ROOT / "scripts" / "dev.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_distributions(name: str, extras: tuple[str, ...] = ()) -> dict[str, metadata.Distribution]:
    """Every installed distribution `name` with `extras` needs at run time on this machine, by normalized name."""
    from packaging.requirements import Requirement

    found: dict[str, metadata.Distribution] = {}
    queue = [(name, extras)]
    while queue:
        current, extras = queue.pop()
        key = re.sub(r"[-_.]+", "-", current).lower()
        if key in found:
            continue
        found[key] = distribution = metadata.distribution(current)
        for line in distribution.requires or []:
            requirement = Requirement(line)
            wanted = ("", *extras)
            if requirement.marker is None or any(requirement.marker.evaluate({"extra": extra}) for extra in wanted):
                queue.append((requirement.name, tuple(requirement.extras)))
    del found[re.sub(r"[-_.]+", "-", name).lower()]
    return found


class LicensesTest(unittest.TestCase):
    """licenses/ ships in the bundle: one <distribution>.txt for each third-party package in it."""

    def test_every_file_names_its_package_and_licence(self):
        for path in sorted((ROOT / "licenses").glob("*.txt")):
            first, second = path.read_text(encoding="utf-8").splitlines()[:2]
            self.assertEqual(first, path.stem, path.name)
            self.assertTrue(second.startswith("License: "), path.name)

    def test_ffmpeg_ships_its_gpl_and_a_source_offer(self):
        # imageio-ffmpeg's wheel carries a GPL ffmpeg; the `dist` task adds ffmpeg-source.txt with its version.
        text = (ROOT / "licenses" / "ffmpeg.txt").read_text(encoding="utf-8")
        self.assertIn("License: GPL-2.0-or-later", text)
        self.assertIn("GNU GENERAL PUBLIC LICENSE", text)
        self.assertIn("Written offer: for three years", text)
        self.assertIn("ffmpeg-source.txt", (ROOT / "scripts" / "dev.py").read_text(encoding="utf-8"))

    @unittest.skipIf(shutil.which("uv") is None, "uv is not installed")
    def test_ffmpeg_source_names_the_bundled_binary(self):
        # dev.ffmpeg_source() finds imageio-ffmpeg's binary through `uv run`, as the `dist` task does.
        try:
            import imageio_ffmpeg
        except ImportError:
            self.skipTest("imageio-ffmpeg is not installed")
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        text = load_dev().ffmpeg_source(python)
        version = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-version"], capture_output=True,
                                 text=True).stdout.splitlines()[0]
        self.assertEqual(text.splitlines()[:3], [
            "ffmpeg-source",
            "The ffmpeg program in this bundle, as it reports itself. The source offer is in ffmpeg.txt.",
            "",
        ])
        self.assertIn(version, text)
        self.assertIn("configuration:", text)

    def test_every_bundled_package_has_its_licence(self):
        try:
            # The bundle carries the `classify` extra, as it can't install it into itself.
            needed = runtime_distributions("flntools", ("classify",))
        except (ImportError, metadata.PackageNotFoundError):
            self.skipTest("flntools isn't installed with packaging, so its dependencies can't be listed")
        missing = [f"{key} (its licence is in its .dist-info folder)"
                   for key in needed if not (ROOT / "licenses" / f"{key}.txt").is_file()]
        self.assertEqual(missing, [], "add licenses/<name>.txt for each")


if __name__ == "__main__":
    unittest.main()
