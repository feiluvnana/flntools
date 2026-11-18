"""The file lifecycle state machine (`flnd.engine.track`): every phase and Ctrl-C stops."""

from __future__ import annotations

import io
import unittest
from pathlib import Path

from flnd.engine.events import Saved
from flnd.engine.track import TERMINAL, FileTrack, Phase, Tracks
from flnd.source import Target
from flnd.ui.log import ui


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class TrackTransitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[object] = []
        self.target = Target("aaa:example_user")
        self.track = FileTrack(
            "aaa:1_0", "posts/1.jpg", self.target, "1", "posts", Path("posts/1.jpg"),
            self.events.append,
        )

    def test_every_terminal_phase(self) -> None:
        paths = {
            Phase.SKIPPED: lambda t: t.skip(),
            Phase.FAILED: lambda t: t.fail(RuntimeError("net")),
            Phase.DROPPED: lambda t: t.dropped(),
        }
        for phase, end in paths.items():
            with self.subTest(phase=phase):
                t = FileTrack(
                    f"k-{phase}", "f", self.target, "1", "posts", Path("f"),
                    lambda e: None,
                )
                t.active()
                end(t)
                self.assertEqual(t.phase, phase)

        saved = Saved(self.target, "posts", "1", Path("f.jpg"), None, 9)
        t = FileTrack("k-s", "f", self.target, "1", "posts", Path("f.jpg"), lambda e: None)
        t.unpacking()
        t.saved(saved)
        self.assertEqual(t.phase, Phase.SAVED)

    def test_forward_only(self) -> None:
        self.track.active()
        self.track.merging()
        with self.assertRaises(ValueError):
            self.track.active()

    def test_saved_emits_once(self) -> None:
        event = Saved(self.target, "posts", "1", Path("posts/1.jpg"), None, 100)
        self.track.unpacking()
        self.track.saved(event)
        self.assertEqual(len(self.events), 1)
        self.assertIs(self.events[0], event)

    def test_ctrl_c_at_each_nonterminal_phase(self) -> None:
        phases = [
            (Phase.QUEUED, lambda t: None),
            (Phase.ACTIVE, lambda t: t.active()),
            (Phase.MERGING, lambda t: (t.active(), t.merging())),
            (Phase.UNPACKING, lambda t: (t.active(), t.unpacking())),
        ]
        for expected, setup in phases:
            with self.subTest(before=expected):
                t = FileTrack("k", "f", self.target, "1", "posts", Path("f"), lambda e: None)
                setup(t)
                self.assertNotIn(t.phase, TERMINAL)
                t.dropped()
                self.assertEqual(t.phase, Phase.DROPPED)

    def test_tracks_registry(self) -> None:
        tracks = Tracks(self.target, self.events.append)
        t = tracks.add("aaa:1_0", "f", "1", "posts", Path("f"))
        self.assertIs(tracks.get("aaa:1_0"), t)


class TrackQuietTest(unittest.TestCase):
    def test_a_file_draws_nothing_under_a_live_ui(self) -> None:
        """A file's lifecycle is events only: the run's summary rows are the board's."""
        buf = _TTY()
        with ui.scope(dest=buf, color=False, level="info"):
            track = FileTrack(
                "aaa:f", "posts/f.mp4", Target("aaa:a"), "1", "posts", Path("f.mp4"),
                lambda e: None,
            )
            track.active()
            track.merging()
            track.saved(Saved(Target("aaa:a"), "posts", "1", Path("f.mp4"), None, 10))
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()