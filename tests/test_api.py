"""The API snapshot, tests/data/api.txt: it lists exactly the public names the code has today.

The snapshot is what `make api` (`tests/api_snapshot.py --write`) writes, so a rename shows in review
as a one-line diff of that file. Nothing is faked: the names come from importing `flnd`.
"""

import difflib
import unittest

import api_snapshot as snapshot
from test_packaging import ROOT


class SnapshotTest(unittest.TestCase):
    def test_snapshot_matches_the_code(self):
        written = (ROOT / "tests" / "data" / "api.txt").read_text(encoding="utf-8")
        current = snapshot.api_text()
        if current != written:
            diff = "".join(difflib.unified_diff(written.splitlines(keepends=True), current.splitlines(keepends=True),
                                                "tests/data/api.txt", "the code"))
            self.fail("the public API differs from tests/data/api.txt; if the change is meant, run "
                      f"`make api` and commit the file with it:\n{diff}")

    def test_one_sorted_line_per_name(self):
        names = [name.name for name in snapshot.api_names()]
        self.assertEqual(names, sorted(set(names)))

    def test_every_covered_module_is_listed(self):
        listed = {name.name for name in snapshot.api_names() if name.kind == "module"}
        self.assertLessEqual(set(snapshot.API_MODULES), listed)


class RenderTest(unittest.TestCase):
    """How a line is drawn, where the output must not depend on the OS or the run."""

    def test_signature_marks_positional_only_and_keyword_only(self):
        def debug(message: "str" = "", /, *args: "int", level: "str" = "info", **fields: "object") -> "None": ...

        def fetch(url, *, timeout=30.0): ...

        self.assertEqual(snapshot._signature(debug),
                         "(message: str = '', /, *args: int, level: str = 'info', **fields: object) -> None")
        self.assertEqual(snapshot._signature(fetch), "(url, *, timeout=30.0)")

    def test_defaults_are_the_same_on_every_os(self):
        from pathlib import Path

        def build(*, home=Path(".flnd"), show=str, rows=()): ...

        self.assertEqual(snapshot._signature(build), "(*, home=Path('.flnd'), show=str, rows=())")

    def test_a_default_with_a_memory_address_shows_its_type(self):
        class Marker:
            pass

        self.assertEqual(snapshot._default_text(Marker(), {}), "Marker(...)")


if __name__ == "__main__":
    unittest.main()
