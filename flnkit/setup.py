"""Writes setuptools' egg-info to build/, out of the project folder."""

import os

from setuptools import setup
from setuptools.command.egg_info import egg_info


class EggInfo(egg_info):
    def finalize_options(self) -> None:
        if self.egg_base is None:
            os.makedirs("build", exist_ok=True)
            self.egg_base = "build"
        super().finalize_options()


setup(cmdclass={"egg_info": EggInfo})
