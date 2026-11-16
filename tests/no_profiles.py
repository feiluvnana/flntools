"""Importing this keeps every test away from real browser profiles and the real plugin folder.

It sets `FLND_TEST_NO_PROFILES`, which `flnd._cookies.refuse_profile` reads: while it is set,
`flnd._cookies` (reading a browser's cookies) and `flnd._browser` (copying a Chrome profile, or
launching Chrome on one) refuse any profile inside the real home folder, before anything is read.
A test that needs a profile fakes the home folder in a temporary folder instead.

It also points `FLND_SOURCES` (the plugin folder, `flnd._plugins.folder`) at a folder that
doesn't exist, unless it is set already, so a plugin the person running the tests has installed
in `~/.config/flnd/sources/` never loads into a test. A test of plugins sets its own.

`make test` sets the first variable for the whole run. `fakes.py` and the test modules that deal
with browsers and cookies import this too, so running one of them by hand is guarded the same way;
in a `unittest discover` run every module is imported before any test runs, so one import covers
the whole suite.
"""

import os
from pathlib import Path

from flnd._cookies import NO_PROFILES
from flnd._plugins import VARIABLE

os.environ[NO_PROFILES] = "1"
os.environ.setdefault(VARIABLE, str(Path(__file__).resolve().parent / "data" / "no-plugins"))
