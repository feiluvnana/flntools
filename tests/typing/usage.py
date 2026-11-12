"""Type-checked by pyright, never run. Each `pyright: ignore` line must stay an error.

`reportUnnecessaryTypeIgnoreComment` fails the build if one of those lines starts to type-check.
"""

from pathlib import Path
from typing import ClassVar, Iterator, Optional

from typing_extensions import assert_type

from flnd.session import Session, TargetSession
from flnd.site import LoginSite, TargetSite
from flnd.source import File, Login, Post, Site, Source
from shared import cookies, http, log, media, prompt, theme, worker
from tests.fakes import FakeSession, FakeSite


def double(number: int) -> int:
    return number * 2


def label(name: str, count: int) -> str:
    return f"{name}: {count}"


assert_type(worker.as_ordered(double, [1, 2]), list[int])
assert_type(worker.as_ordered(label, ["a", "b"], [1, 2]), list[str])
assert_type(worker.as_ordered(len, ["ab"]), list[int])
assert_type(worker.as_completed(double, [1, 2]), Iterator[worker.Result[int]])
worker.as_ordered(double, [1], label="numbers", progress=lambda done, total: None)

assert_type(prompt.choose("Source", {"Instagram": "ins", "TikTok": "tik"}), str)
assert_type(prompt.choose("Count", [1, 2, 3]), int)
assert_type(prompt.choose("Tabs", ["posts", "reels"], many=False), str)
assert_type(prompt.choose("Tabs", ["posts", "reels"], many=True), list[str])
assert_type(prompt.choose("Source", {"Instagram": "ins"}, many=True, min=1, max=1), list[str])
assert_type(prompt.ask("Token", secret=True), str)
prompt.choose("Tabs", ["posts"], min=1)  # pyright: ignore[reportCallIssue]
prompt.input("Name")  # pyright: ignore[reportAttributeAccessIssue]

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

clip = media.open("a.mp4")
assert_type(clip, media.Media)
assert_type(clip.info, media.Info)
assert_type(clip.duration, Optional[float])
assert_type(clip.trim(end=10).compress("low", format="mp4"), media.Media)
assert_type(clip.compress("low").save(), Path)
assert_type(clip.frames(dest="frames"), list[Path])
assert_type(media.open("a.jpg").resize(width=100).crop((0, 0, 50, 50)), media.Media)
assert_type(media.kind("a.jpg"), Optional[str])
with media.open("a.mov") as opened:
    assert_type(opened, media.Media)
clip.trim(0, 10)  # pyright: ignore[reportCallIssue]
clip.frames(dest_dir="frames")  # pyright: ignore[reportCallIssue]
media.probe("a.jpg")  # pyright: ignore[reportAttributeAccessIssue]

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



def sites(engine: TargetSite, fake: FakeSite, login: LoginSite) -> None:
    # The engine's site and the tests' fake both match the 13-member protocol sources are written against.
    checked: Site = engine
    checked = fake
    assert_type(checked.fetch("https://a.example/"), http.Response)
    assert_type(checked.fetch("https://a.example/", soft=True), Optional[http.Response])
    assert_type(checked.goto("https://a.example/"), bool)
    asking: Login = login
    assert_type(asking.ask("Code", secret=True), str)
    # A download's site never asks: misuse is a type error, not a run-time one.
    checked.ask("Code")  # pyright: ignore[reportAttributeAccessIssue]
    checked.fetch_json("https://a.example/")  # pyright: ignore[reportAttributeAccessIssue]
    assert_type(Post("1", [File("https://a.example/a.jpg")], date=1767312000).replace(album="a"), Post)
    File(1)  # pyright: ignore[reportArgumentType]
