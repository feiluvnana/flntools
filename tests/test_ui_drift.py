"""Live progress drift on `flnd._ui`, reproduced through a pyte terminal (PLAN step 0).

Rich redraws transient bars when one of several concurrent rows finishes; the screen can
keep a stale or duplicate line. Step 4 replaces this UI; until then this test must fail.
"""

from __future__ import annotations

import io
import os
import stat
import unittest

import pyte

from flnd import _time
from flnd._ui import ui


class _TTY(io.StringIO):
    """A text buffer Rich treats as a terminal (`isatty` is True)."""

    def isatty(self) -> bool:  # noqa: D102 — test double
        return True


class DriftTest(unittest.TestCase):
    def test_finishing_one_live_row_leaves_one_bar_for_the_other(self) -> None:
        """After the first file's bar ends, exactly one live bar should remain for the second."""
        columns, lines = 80, 24
        screen = pyte.Screen(columns, lines)
        stream = pyte.Stream(screen)
        buf = _TTY()
        with _time.use(_time.Fake(start=100.0)):
            with ui.scope(color=True, dest=buf, width=columns, level="info"):
                with ui.progress("file-a.mp4", total=100) as row_a:
                    with ui.progress("file-b.mp4", total=100) as row_b:
                        row_a.advance(100)
                        row_a.finish()
                        row_b.advance(40)
                buf.flush()
        stream.feed(buf.getvalue())
        display = "\n".join("".join(line).rstrip() for line in screen.display)
        live = [
            line for line in display.splitlines()
            if "file-b.mp4" in line and any(char in line for char in "━─%⠋⠙⠹")
        ]
        self.assertEqual(
            len(live), 1,
            f"expected one live bar for file-b, found {len(live)}:\n{display}",
        )


class SecretModeTest(unittest.TestCase):
    def test_secret_db_is_not_world_readable(self) -> None:
        import tempfile
        from pathlib import Path

        from flnd import _home

        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        with _home.secrets(home, "tlg") as vault:
            vault["session"] = "x"
        db = home / _home.SECRETS
        self.assertTrue(db.is_file())
        if os.name != "nt":
            mode = stat.S_IMODE(db.stat().st_mode)
            self.assertEqual(mode, 0o600)
            meta = db.parent.stat().st_mode
            self.assertEqual(stat.S_IMODE(meta), 0o700)


if __name__ == "__main__":
    unittest.main()