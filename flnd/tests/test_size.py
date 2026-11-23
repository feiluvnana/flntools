"""The size limits of flnd's code and tests (AGENTS.md › Size): they only go down.

The counts are the lines of `src/` and `flnkit/`, and of `tests/`. A count above its limit fails
its test. Trim the code, or raise the limit in the same commit, with the reason in the body.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The limits: today's counts, rounded up to the next 100.
CODE_LINES = 41400
TEST_LINES = 33000


def count(*patterns: str) -> int:
    """The lines of every file the glob patterns match under flnd/, each file once."""
    files = {path for pattern in patterns for path in ROOT.glob(pattern)}
    return sum(len(path.read_text(encoding="utf-8").splitlines()) for path in files)


def over(what: str, lines: int, limit: int) -> str:
    """The failure message: the count, the limit, and the two ways out."""
    return (f"{what} has {lines} lines, over the limit of {limit}. Trim it, or raise the limit "
            "in tests/test_size.py in the same commit, with the reason in the commit body.")


class SizeTest(unittest.TestCase):
    def test_code_size(self):
        lines = count("src/*.py", "src/*/*.py", "flnkit/*.py")
        self.assertLessEqual(lines, CODE_LINES, over("src/ and flnkit/", lines, CODE_LINES))

    def test_test_size(self):
        lines = count("tests/*.py")
        self.assertLessEqual(lines, TEST_LINES, over("tests/", lines, TEST_LINES))


if __name__ == "__main__":
    unittest.main()
