"""Importing this makes a coroutine that is never awaited fail the test it was made in.

`make test` runs Python with `-W "error:coroutine:RuntimeWarning"`, so the warning Python gives
for such a coroutine becomes an error. But it is raised while the coroutine is finalized, where
nothing can catch it: Python only prints it, through `sys.unraisablehook`. The hook installed
here records it instead, and the test that was running fails with the coroutine's name. One
found after the last test (at exit) fails the run.

`test__unawaited.py`, the first module `unittest discover` imports, installs it for the suite;
`fakes.py` imports it too, so a test module run by hand is covered the same way.
"""

import atexit
import os
import re
import sys
import unittest

NAME = re.compile(r"coroutine '(.+?)' was never awaited")
# The coroutines finalized without being awaited, by name, in the order seen.
SEEN: list[str] = []
# How many of `SEEN` a test has already failed for.
_told = 0
_previous = sys.unraisablehook


def _hook(unraisable) -> None:
    error = unraisable.exc_value
    if isinstance(error, RuntimeWarning) and "was never awaited" in str(error):
        # The message reads "coroutine 'name' was never awaited".
        found = NAME.search(str(error))
        SEEN.append(found[1] if found else str(error))
        return
    _previous(unraisable)


def _run(self, result=None):
    global _told
    outcome = _plain_run(self, result)
    new, _told = SEEN[_told:], len(SEEN)
    if new and result is not None:
        try:
            raise AssertionError(f"a coroutine was never awaited: {', '.join(new)}")
        except AssertionError:
            result.addFailure(self, sys.exc_info())
    return outcome


def _at_exit() -> None:
    if len(SEEN) > _told:
        sys.stderr.write(f"a coroutine was never awaited: {', '.join(SEEN[_told:])}\n")
        sys.stderr.flush()
        os._exit(1)


if sys.unraisablehook is not _hook:
    _plain_run = unittest.TestCase.run
    sys.unraisablehook = _hook
    unittest.TestCase.run = _run
    atexit.register(_at_exit)
