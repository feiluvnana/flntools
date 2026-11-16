"""Importing this turns on the guard that keeps every test away from real browser profiles.

It sets `FLND_TEST_NO_PROFILES`, which `flnd._cookies.refuse_profile` reads: while it is set,
`flnd._cookies` (reading a browser's cookies) and `flnd._browser` (copying a Chrome profile, or
launching Chrome on one) refuse any profile inside the real home folder, before anything is read.
A test that needs a profile fakes the home folder in a temporary folder instead.

`make test` sets the variable for the whole run. `fakes.py` and the test modules that deal with
browsers and cookies import this too, so running one of them by hand is guarded the same way;
in a `unittest discover` run every module is imported before any test runs, so one import covers
the whole suite.
"""

import os

from flnd._cookies import NO_PROFILES

os.environ[NO_PROFILES] = "1"
