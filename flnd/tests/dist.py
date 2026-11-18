"""`make dist`: the one-file binary, built from a staged copy of the two packages.

PyInstaller can't follow the editable install, which maps the name flnd to the folder src/, so
the copy is staged as build/stage/flnd (from src/) and build/stage/flnkit, and the copy's
__main__.py is bundled."""

import shutil
from pathlib import Path

from PyInstaller.__main__ import run

ROOT = Path(__file__).resolve().parent.parent
STAGE = ROOT / "build" / "stage"


def main() -> None:
    shutil.rmtree(STAGE, ignore_errors=True)
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(ROOT / "src", STAGE / "flnd", ignore=ignore)
    shutil.copytree(ROOT / "flnkit", STAGE / "flnkit", ignore=ignore)
    run(["--onefile", "--noconfirm", "--name", "flnd",
         "--collect-submodules", "flnd", "--collect-submodules", "flnkit",
         "--specpath", str(ROOT / "build"), "--workpath", str(ROOT / "build" / "pyinstaller"),
         "--paths", str(STAGE), str(STAGE / "flnd" / "__main__.py")])


if __name__ == "__main__":
    main()
