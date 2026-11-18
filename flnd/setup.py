"""Writes setuptools' egg-info to build/, out of the project folder; pyproject.toml holds the
rest of the project (PLAN.md 4.1, Packaging)."""

import os

from setuptools import setup
from setuptools.command.egg_info import egg_info


class EggInfo(egg_info):
    def finalize_options(self) -> None:
        # pyproject's `[tool.distutils.egg_info] egg-base` needs the folder to exist already, and
        # build/ is absent in a fresh clone. dist_info and editable_wheel set their own egg_base.
        if self.egg_base is None:
            os.makedirs("build", exist_ok=True)
            self.egg_base = "build"
        super().finalize_options()


setup(cmdclass={"egg_info": EggInfo})
