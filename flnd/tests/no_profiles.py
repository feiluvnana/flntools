"""Importing this keeps every test away from real browser profiles and the real plugin folder.

It sets `FLND_TEST_NO_PROFILES`, which `flnkit.cookies.refuse_profile` reads: while it is set,
`flnkit.cookies` (reading a browser's cookies) and `flnkit.browser` (copying a Chrome profile, or
launching Chrome on one) refuse any profile inside the real home folder, before anything is read.
A test that needs a profile fakes the home folder in a temporary folder instead.

It also points `FLND_PLUGINS` (`flnd.engine.plugins.folder`) at a folder that doesn't exist,
unless it is set already, so a plugin the person running the tests has installed in
`~/.config/flnd/` never loads into a test. A test of plugins sets its own.

`make test` sets the first variable for the whole run. `fakes.py` and the test modules that deal
with browsers and cookies import this too, so running one of them by hand is guarded the same way;
in a `unittest discover` run every module is imported before any test runs, so one import covers
the whole suite.
"""

import os
import tempfile
from pathlib import Path

from flnd.engine import auth, config, plugins
from flnkit.cookies import NO_PROFILES

os.environ[NO_PROFILES] = "1"
os.environ.setdefault(
    plugins.VARIABLE, str(Path(__file__).resolve().parent / "data" / "no-plugins"))
# The vault's key, in a folder of the run's own, never `~/.config/flnd/key`.
auth.KEYFILE = Path(tempfile.mkdtemp(prefix="flnd-key-")) / "key"


class _NoCooldown(config.Defaults, frozen=True):
    fresh: str = "0"


# A run straight after a complete one still lists, as most tests expect; a test of the `fresh`
# cooldown writes `fresh = "1d"` to its config.toml.
config.Defaults = _NoCooldown
