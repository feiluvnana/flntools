"""Importing this keeps every test away from the real logins and the real cache.

It sets `FLND_TEST_NO_PROFILES`, which `flnkit.browser` and `flnd.engine.config` read: while it
is set, the browser's cache goes to a temporary folder and nothing reads or writes the real
`~/.config/flnd/`.

`make test` sets the first variable for the whole run. `fakes.py` and the test modules that deal
with browsers and cookies import this too, so running one of them by hand is guarded the same way;
in a `unittest discover` run every module is imported before any test runs, so one import covers
the whole suite.
"""

import os
import tempfile
from pathlib import Path

from flnd.engine import auth
from flnkit.browser import NO_PROFILES

os.environ[NO_PROFILES] = "1"
# The logins folder, in a folder of the run's own, never `~/.config/flnd/logins`.
auth.LOGINS = Path(tempfile.mkdtemp(prefix="flnd-logins-"))
