"""Type-checked by pyright, never run. Each `pyright: ignore` line must stay an error.

`reportUnnecessaryTypeIgnoreComment` fails the build if one of those lines starts to type-check.
"""

from typing import ClassVar

from typing_extensions import assert_type

from flnd.sources.base import BaseSource
from shared import prompt, theme, worker


def double(number: int) -> int:
    return number * 2


def label(name: str, count: int) -> str:
    return f"{name}: {count}"


assert_type(worker.map(double, [1, 2]), list[int])
assert_type(worker.map(label, ["a", "b"], [1, 2]), list[str])
assert_type(worker.map(len, ["ab"]), list[int])

assert_type(prompt.choice("Source", {"Instagram": "ins", "TikTok": "tik"}), str)
assert_type(prompt.choice("Count", [1, 2, 3]), int)

theme.color = "256"
theme.color = "265"  # pyright: ignore[reportAttributeAccessIssue]

worker.map(len, [1, 2])  # pyright: ignore[reportArgumentType]


class StringDomains(BaseSource):
    code: ClassVar[str] = "abc"
    domains = "abc.example"  # pyright: ignore[reportAssignmentType]
