"""Type-checked by pyright, never run. Each `pyright: ignore` line must stay an error.

`reportUnnecessaryTypeIgnoreComment` fails the build if one of those lines starts to type-check.
"""

from typing import ClassVar

from typing_extensions import assert_type

from flnd.session import Session, TargetSession
from flnd.sources.base import Source
from shared import prompt, theme, worker
from tests.fakes import FakeSession


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


class StringDomains(Source):
    code: ClassVar[str] = "abc"
    domains = "abc.example"  # pyright: ignore[reportAssignmentType]


class StringNeeds(Source):
    code: ClassVar[str] = "abc"
    needs = "sessionid"  # pyright: ignore[reportAssignmentType]


class StringCategories(Source):
    code: ClassVar[str] = "abc"
    categories = "posts"  # pyright: ignore[reportAssignmentType]


def sessions(engine: TargetSession, fake: FakeSession) -> None:
    # Both the engine's session and the tests' fake match the protocol sources are written against.
    checked: Session = engine
    checked = fake
    assert_type(checked.save("https://a.example/a.jpg", "a.jpg", category="posts"), bool)

