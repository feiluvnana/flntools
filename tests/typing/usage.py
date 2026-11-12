"""Type-checked by pyright, never run. Each `pyright: ignore` line must stay an error.

`reportUnnecessaryTypeIgnoreComment` fails the build if one of those lines starts to type-check.
"""

from pathlib import Path
from typing import ClassVar, Iterator, Optional

from typing_extensions import assert_type

from flnd.session import Session, TargetSession
from flnd.sources.base import Source
from shared import cookies, log, media, prompt, theme, worker
from tests.fakes import FakeSession


def double(number: int) -> int:
    return number * 2


def label(name: str, count: int) -> str:
    return f"{name}: {count}"


assert_type(worker.as_ordered(double, [1, 2]), list[int])
assert_type(worker.as_ordered(label, ["a", "b"], [1, 2]), list[str])
assert_type(worker.as_ordered(len, ["ab"]), list[int])
assert_type(worker.as_completed(double, [1, 2]), Iterator[worker.Result[int]])
worker.as_ordered(double, [1], label="numbers", progress=lambda done, total: None)

assert_type(prompt.choice("Source", {"Instagram": "ins", "TikTok": "tik"}), str)
assert_type(prompt.choice("Count", [1, 2, 3]), int)

theme.color = "256"
theme.color = "265"  # pyright: ignore[reportAttributeAccessIssue]

worker.as_ordered(len, [1, 2])  # pyright: ignore[reportArgumentType]
worker.as_ordered(double, [1], progress="numbers")  # pyright: ignore[reportArgumentType]

with log.progress("photos", total=2) as bar:
    assert_type(bar.count, float)
    bar.total = 3
    bar.count = 1
    bar.caption = "part 2"
    bar.advance()
    assert_type(bar.finished, bool)
    bar.update(1, 2)  # pyright: ignore[reportAttributeAccessIssue]
with log.group("download") as rows:
    assert_type(rows.add("ins:example", total=12).total, Optional[float])
log.multi("download")  # pyright: ignore[reportAttributeAccessIssue]

assert_type(media.probe("a.jpg"), media.Info)
assert_type(media.compress("a.jpg", preset="low"), Optional[Path])
assert_type(media.video.frames("a.mp4", dest_dir="frames"), list[Path])
assert_type(media.image.resize("a.jpg", width=100), Path)
media.compress("a.jpg", "low")  # pyright: ignore[reportCallIssue]

jar = cookies.load("cookies.txt").filter(domain="example.com")
assert_type(jar, cookies.Jar)
assert_type(jar.merge(cookies.from_browser("firefox")), cookies.Jar)
assert_type(jar.header, str)
assert_type(jar["sessionid"], str)
assert_type(jar.save("out.txt"), Path)
cookies.load("cookies.txt", domain="example.com")  # pyright: ignore[reportCallIssue]


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

