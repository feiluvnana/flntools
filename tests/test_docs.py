"""Pins that a docs commit changes only comments and docstrings, by comparing hashes of the code without them.

`tests/data/code_baseline.json` holds, for every file of `flnd/` and `shared/`, the SHA-256 of
`ast.unparse` of the parsed module with its docstring nodes removed (comments never reach the AST).
A docs commit therefore leaves the baseline unchanged, which proves it changed no code. A commit
that changes code regenerates it with `uv run --no-project scripts/dev.py baseline`, and the diff
of the baseline file shows which files changed.

`ast.unparse` prints some code differently on different Python versions, so the test only compares
on the version that wrote the baseline and is skipped on the others. Nothing is faked.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_packaging import load_dev

dev = load_dev()


class CodeBaselineTest(unittest.TestCase):
    def test_code_without_docstrings_and_comments_is_the_baseline(self):
        baseline = json.loads(dev.BASELINE.read_text(encoding="utf-8"))
        if f"{sys.version_info.major}.{sys.version_info.minor}" != baseline["python"]:
            self.skipTest(f"the baseline was written on Python {baseline['python']}")
        hashes = dev.code_hashes()
        changed = sorted(path for path in hashes.keys() | baseline["files"].keys() if hashes.get(path) != baseline["files"].get(path))
        self.assertEqual(changed, [], "code changed; if on purpose, run `scripts/dev.py baseline`")


class DocstringCheckTest(unittest.TestCase):
    def breaches(self, source: str) -> list[str]:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(dev, "ROOT", Path(folder)):
            (Path(folder) / "m.py").write_text(source, encoding="utf-8")
            return dev.doc_breaches("m.py")

    def test_a_complete_file_passes(self):
        source = ('"""Module."""\n\n\ndef f(a, b):\n    """Does it.\n\n    Args:\n        a: one.\n        b: two.\n    """\n')
        self.assertEqual(self.breaches(source), [])

    def test_each_rule_is_reported(self):
        source = ('class C:\n    pass\n\n\ndef f(a, b):\n    """One line."""\n\n\n'
                  'def g(a, b):\n    """Does it.\n\n    Args:\n        a: one.\n        c: three.\n    """\n\n\n'
                  'def _h():\n' + "    pass\n" * 16)
        text = "\n".join(self.breaches(source))
        for expected in ("module has no docstring", "C has no docstring", "f takes 2 arguments", "g: Args lacks ['b'] and names ['c']",
                         "_h is private, over 15 lines"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
