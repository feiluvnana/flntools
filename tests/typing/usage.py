"""Type-checked by pyright, never run. Each `pyright: ignore` line must stay an error.

`reportUnnecessaryTypeIgnoreComment` fails the build if one of those lines starts to type-check.
"""

from pathlib import Path
from typing import Any, ClassVar, Iterator, Optional

from typing_extensions import assert_type

from flnd.site import LoginSite, TargetSite
from flnd.source import File, Login, Post, Site, Source
from shared import cookies, fs, http, prompt, sh, ui, worker
from tests.fakes import FakeSite


def double(number: int) -> int:
    return number * 2


def label(name: str, count: int) -> str:
    return f"{name}: {count}"


with worker.Pool(concurrency=2, progress=lambda done, total: None) as pool:
    pool.submit(double, 1)
    pool.submit(label, "a", 2)
    assert_type(pool.as_completed(), Iterator[worker.Result[Any]])
    assert_type(pool.join(), list[worker.Result[Any]])

assert_type(prompt.choose("Source", {"Instagram": "ins", "TikTok": "tik"}), str)
assert_type(prompt.choose("Count", [1, 2, 3]), int)
assert_type(prompt.choose("Tabs", ["posts", "reels"], many=False), str)
assert_type(prompt.choose("Tabs", ["posts", "reels"], many=True), list[str])
assert_type(prompt.choose("Source", {"Instagram": "ins"}, many=True, default=["ins"]), list[str])
assert_type(prompt.ask("Token", secret=True), str)
prompt.choose("Tabs", ["posts"], min=1)  # pyright: ignore[reportCallIssue]
prompt.input("Name")  # pyright: ignore[reportAttributeAccessIssue]

ui.level = "debug"
ui.level = "loud"  # pyright: ignore[reportAttributeAccessIssue]

worker.Pool(concurrency="2")  # pyright: ignore[reportArgumentType]
worker.Pool(progress="numbers")  # pyright: ignore[reportArgumentType]

with ui.progress("photos", total=2) as bar:
    assert_type(bar.count, float)
    bar.total = 3
    bar.count = 1
    bar.advance()
    bar.finish()
    bar.update(1, 2)  # pyright: ignore[reportAttributeAccessIssue]
with ui.group("download") as rows:
    assert_type(rows.add("ins:example", total=12).total, Optional[float])
ui.multi("download")  # pyright: ignore[reportAttributeAccessIssue]

tool = sh.tool("7zz", platforms={("linux", "amd64"): sh.Spec("https://example.com/7zz", "0" * 64)})
assert_type(tool, sh.Tool)
assert_type(tool.path, Path)
assert_type(tool.run("--help"), sh.Result)
assert_type(sh.run(["echo", "hi"]), sh.Result)
sh.tool("7zz", checksum="0" * 64)  # pyright: ignore[reportCallIssue]

jar = cookies.load("cookies.txt").filter(domains="example.com")
assert_type(jar, cookies.Jar)
assert_type(jar.merge(cookies.load("firefox")), cookies.Jar)
assert_type(jar.header, str)
assert_type(jar["sessionid"], str)
assert_type(jar.save("out.txt"), Path)
cookies.load("cookies.txt", domain="example.com")  # pyright: ignore[reportCallIssue]

assert_type(fs.write("out.txt", data="hi"), Path)
with fs.write("out.txt") as temp:
    assert_type(temp, Path)


class StringDomains(Source):
    code: ClassVar[str] = "abc"
    domains = "abc.example"  # pyright: ignore[reportAssignmentType]


class StringNeeds(Source):
    code: ClassVar[str] = "abc"
    needs = "sessionid"  # pyright: ignore[reportAssignmentType]


class StringCategories(Source):
    code: ClassVar[str] = "abc"
    categories = "posts"  # pyright: ignore[reportAssignmentType]


def sites(engine: TargetSite, fake: FakeSite, login: LoginSite) -> None:
    # The engine's site and the tests' fake both match the 13-member protocol sources are written against.
    checked: Site = engine
    checked = fake
    assert_type(checked.fetch("https://a.example/"), http.Response)
    assert_type(checked.try_fetch("https://a.example/"), Optional[http.Response])
    assert_type(checked.goto("https://a.example/"), bool)
    asking: Login = login
    assert_type(asking.ask("Code", secret=True), str)
    # A download's site never asks: misuse is a type error, not a run-time one.
    checked.ask("Code")  # pyright: ignore[reportAttributeAccessIssue]
    checked.fetch_json("https://a.example/")  # pyright: ignore[reportAttributeAccessIssue]
    assert_type(Post("1", [File("https://a.example/a.jpg")], date=1767312000).replace(album="a"), Post)
    File(1)  # pyright: ignore[reportArgumentType]
