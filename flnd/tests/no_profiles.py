"""Importing this keeps every test away from real browser profiles and the real plugin folder.

It sets `FLND_TEST_NO_PROFILES`, which `flnkit.cookies.refuse_profile` reads: while it is set,
`flnkit.cookies` (reading a browser's cookies) and `flnkit.browser` (copying a Chrome profile, or
launching Chrome on one) refuse any profile inside the real home folder, before anything is read.
A test that needs a profile fakes the home folder in a temporary folder instead.

It also points `FLND_SOURCES` and `FLND_PLUGINS` (the plugin folders of
`flnd.engine.plugins.folder` and `flnd.engine.manager.folder`) at a folder that doesn't exist,
unless they are set already, so a plugin the person running the tests has installed in
`~/.config/flnd/` never loads into a test. A test of plugins sets its own.

`make test` sets the first variable for the whole run. `fakes.py` and the test modules that deal
with browsers and cookies import this too, so running one of them by hand is guarded the same way;
in a `unittest discover` run every module is imported before any test runs, so one import covers
the whole suite.
"""

import os
from pathlib import Path

from flnd.engine import manager, plugins
from flnkit.cookies import NO_PROFILES

os.environ[NO_PROFILES] = "1"
for variable in (plugins.VARIABLE, manager.VARIABLE):
    os.environ.setdefault(variable, str(Path(__file__).resolve().parent / "data" / "no-plugins"))
