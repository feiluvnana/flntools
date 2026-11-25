"""Keeps the docs in step with the code: docstrings, `SPEC.md`, `COOKBOOK.md`, links, CLI, plugins.

AGENTS.md › Writing sets the rules this file checks. Each check is a test class below:

1. `DocstringTest` reads every module of `flnd` with `ast` (nothing is imported) and lists each
   docstring gap: a module without a docstring, one longer than `MODULE_LINES` lines, one that
   names no `SPEC.md#<section>` or a section the page lacks. Every docstring with `>>>` lines
   then runs as a doctest, and so does `tests/plugin_kit.py`.
2. `PageTest` runs every `python` block of `SPEC.md` and of `COOKBOOK.md`, in page order, one
   namespace for each page.
3. `LinkTest` checks every relative link of the three pages: the file exists, and a `#fragment`
   names one of its headings.
4. `CliTest` checks that `SPEC.md` names every command and every option the CLI parses, and
   that each `flnd …` command line of the three pages parses with the CLI's own parser.
5. `PluginTest` checks that each built-in plugin has a `SPEC.md` section headed by its code.
6. `SizeTest` keeps `README.md`, `COOKBOOK.md` and `SPEC.md` within their limits.

A gap fails `test_no_gap`, with one line per gap naming the module or the name and the rule it
breaks, so new code is documented as it is written.
"""

from __future__ import annotations

import ast
import contextlib
import doctest
import importlib
import io
import os
import re
import shlex
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

from test_packaging import ROOT

# The packages whose modules must document themselves. `tests/` is for contributors.
PACKAGES = ("src", "flnkit")
PAGES = README, COOKBOOK, SPEC = tuple(ROOT / n for n in ("README.md", "COOKBOOK.md", "SPEC.md"))
# The longest a module docstring may be, in lines; each page's longest, in lines.
MODULE_LINES, LINE_CHARS = 3, 100
LIMITS = dict(zip(PAGES, (40, 250, 1550)))
SECTION = re.compile(r"SPEC\.md#([\w-]+)")


def dotted(path: Path) -> str:
    """`src/engine/run.py` as `flnd.engine.run`; `flnkit/net.py` as `flnkit.net`."""
    parts = path.relative_to(ROOT).with_suffix("").parts
    if parts[0] == "src":
        parts = ("flnd", *parts[1:])
    return ".".join(parts).removesuffix(".__init__")


def modules() -> list[Path]:
    """Every module of `PACKAGES`, sorted, so the gaps come in one order on every OS."""
    return sorted(path for package in PACKAGES for path in (ROOT / package).rglob("*.py"))


def module_gaps(path: Path, sections: set[str]) -> list[str]:
    """Every docstring gap of one module, one line each.

    A module starts with a docstring of at most `MODULE_LINES` lines that names a real
    `SPEC.md#<section>`.

    Args:
        path: the module's file.
        sections: the `#fragment` of every heading of `SPEC.md`.

    Returns:
        `<path>: <gap>` for each gap of the module.
    """
    relative = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    doc = ast.get_docstring(tree)
    if not doc:
        return [f"{relative}: no module docstring"]
    gaps = []
    named = SECTION.findall(doc)
    if not named:
        gaps.append(f"{relative}: the module docstring names no SPEC.md section")
    gaps += [f"{relative}: SPEC.md has no section #{name}" for name in named
             if name not in sections]
    if len(doc.splitlines()) > MODULE_LINES:
        gaps.append(f"{relative}: the module docstring is over {MODULE_LINES} lines")

    return gaps


def gaps() -> list[str]:
    """Every gap the code has, sorted: `test_no_gap` wants none."""
    sections = anchors(SPEC)
    found = [gap for path in modules() for gap in module_gaps(path, sections)]
    # A property and its setter share a name, so the same gap can come twice; it is listed once.
    return sorted(set(found))


def rule(gap: str) -> str:
    """The rule of AGENTS.md › Writing that one gap breaks, for the failure message."""
    if gap.endswith("no module docstring"):
        return "a module starts with a docstring"
    if "SPEC.md" in gap:
        return "a module docstring names the SPEC.md#<section> that explains it"
    if gap.endswith(f"over {MODULE_LINES} lines"):
        return f"a module docstring is 1 to {MODULE_LINES} lines"
    return "a docstring is written for the module"


class DocstringTest(unittest.TestCase):
    """The docstring rules of AGENTS.md › Writing: any gap fails, naming it and its rule."""

    def test_no_gap(self) -> None:
        found = gaps()
        self.assertFalse(found, "document these (AGENTS.md › Writing):\n"
                         + "\n".join(f"{gap}  [rule: {rule(gap)}]" for gap in found))

    def test_examples_run(self) -> None:
        """Every docstring with `>>>` lines runs as a doctest, in a scratch folder."""
        finder, runner = doctest.DocTestFinder(), doctest.DocTestRunner(verbose=False)
        kit = ROOT / "tests" / "plugin_kit.py"
        for path in [*modules(), kit]:
            if ">>>" not in path.read_text(encoding="utf-8"):
                continue
            name = path.stem if path == kit else dotted(path)
            module = importlib.import_module(name)
            with scratch():
                for test in finder.find(module, name):
                    runner.run(test)
        self.assertFalse(runner.failures, f"{runner.failures} docstring examples failed")


def fenced_blocks(page: Path) -> list[tuple[int, str, str]]:
    """Each fenced block of a page: its first code line's number, language and code."""
    lines = page.read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[int, str, str]] = []
    start: int | None = None
    fence = language = ""
    for number, line in enumerate(lines):
        stripped = line.strip()
        if start is None and stripped.startswith("```"):
            fence = stripped[: len(stripped) - len(stripped.lstrip("`"))]
            language = stripped[len(fence):].strip()
            start = number + 1
        elif start is not None and stripped == fence:
            blocks.append((start + 1, language, "\n".join(lines[start:number])))
            start = None
    return blocks


@contextlib.contextmanager
def scratch() -> Iterator[Path]:
    """Run in a new empty folder, so an example never touches the real `.flnd`."""
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as folder:
        os.chdir(folder)
        try:
            yield Path(folder)
        finally:
            os.chdir(previous)


class PageTest(unittest.TestCase):
    """Every `python` block of `SPEC.md` and `COOKBOOK.md` runs, so an example can't rot."""

    def test_python_blocks_run(self) -> None:
        # A page's blocks share one namespace, like a doctest session: a recipe builds a plugin
        # in one block and uses it in the next. `tests/` is on the path for the fakes.
        for page in (COOKBOOK, SPEC):
            namespace: dict[str, object] = {"__name__": "docs_page"}
            for line, language, code in fenced_blocks(page):
                if language != "python":
                    continue
                with (self.subTest(page=page.name, line=line), scratch(),
                      contextlib.redirect_stdout(io.StringIO())):
                    exec(compile(code, f"{page.name}:{line}", "exec"), namespace)


FENCED = re.compile(r"^\s*(```+|~~~+).*?^\s*\1\s*$", re.M | re.S)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def prose(text: str) -> str:
    """A page without its fenced blocks and inline code, where a `[x](y)` isn't a link."""
    return re.sub(r"`[^`\n]*`", "", FENCED.sub("", text))


def anchors(page: Path) -> set[str]:
    """The `#fragment` each heading of a page gets, the way GitHub makes them.

    A heading is lowercased, loses every character but letters, digits, `-`, `_` and spaces,
    and its spaces become `-`. A repeated heading gets `-1`, `-2`, … in order.
    """
    seen: dict[str, int] = {}
    found: set[str] = set()
    text = FENCED.sub("", page.read_text("utf-8"))
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*$", text, re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


class LinkTest(unittest.TestCase):
    """Every relative link resolves, to a file and, with a `#fragment`, to one of its headings."""

    def test_links_resolve(self) -> None:
        broken: list[str] = []
        for page in PAGES:
            for link in LINK.findall(prose(page.read_text(encoding="utf-8"))):
                if re.match(r"[a-z][a-z0-9+.-]*:", link):  # https:, mailto: and the like
                    continue
                path, _, fragment = link.partition("#")
                target = (page.parent / path).resolve() if path else page
                where = f"{page.name}: {link}"
                if not target.exists():
                    broken.append(f"{where} (no such file)")
                elif fragment and target.suffix == ".md" and fragment not in anchors(target):
                    broken.append(f"{where} (no such heading)")
        self.assertFalse(broken, "broken links:\n" + "\n".join(broken))


def cli_words() -> list[str]:
    """Every command the CLI has, as typed (`flnd get`), and every flag it parses.

    Read from the argparse parser itself (`flnd.cli.parser`). Positionals and `-h` are left out.
    """
    import argparse

    from flnd.cli import parser

    top = parser()
    words: list[str] = []

    def flags(command: argparse.ArgumentParser) -> None:
        for action in command._actions:
            words.extend(flag for flag in action.option_strings if flag != "-h")

    def walk(command: argparse.ArgumentParser, prefix: str) -> None:
        for action in command._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    words.append(f"{prefix} {name}")
                    flags(sub)
                    walk(sub, f"{prefix} {name}")

    flags(top)
    walk(top, "flnd")
    return list(dict.fromkeys(words))


JOINERS = {"|", "||", "&&", ";"}


def commands(code: str) -> list[list[str]]:
    """The arguments of every `flnd` command in a block of shell lines, without `flnd` itself.

    Each line is split like a POSIX shell, comments dropped, and cut at `|`, `&&`, `||`, `;`. A
    part runs flnd when one of its words is a program named `flnd`. A line ending in `\\` goes on.
    """
    found: list[list[str]] = []
    for line in code.replace("\\\n", " ").splitlines():
        part: list[str] = []
        for word in [*shlex.split(line, comments=True), ";"]:
            if word not in JOINERS:
                part.append(word)
                continue
            at = next((i for i, w in enumerate(part) if Path(w).name == "flnd"), None)
            if at is not None:
                found.append(part[at + 1:])
            part = []
    return found


def parse_error(args: list[str]) -> str:
    """What the CLI's parser says about `flnd <args>`: "" when it parses."""
    from flnd.cli import parser

    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            parser().parse_args(args)
    except SystemExit as stop:
        if stop.code:
            return stderr.getvalue().strip().splitlines()[-1]
    return ""


class CliTest(unittest.TestCase):
    """SPEC.md names every command and option, and every page's command lines are true."""

    def test_every_command_and_option_is_documented(self) -> None:
        page = SPEC.read_text(encoding="utf-8")
        missing = [word for word in cli_words()
                   if not re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", page)]
        self.assertFalse(missing, f"SPEC.md doesn't mention: {', '.join(missing)}")

    def test_commands_parse(self) -> None:
        checked = 0
        for page in PAGES:
            for line, language, code in fenced_blocks(page):
                if language not in ("sh", "bash", "shell"):
                    continue
                for args in commands(code):
                    checked += 1
                    with self.subTest(page=page.name, line=line, command=" ".join(args)):
                        self.assertEqual(parse_error(args), "")
        self.assertGreater(checked, 10, "the pages show too few flnd command lines")


class PluginTest(unittest.TestCase):
    """Each built-in plugin has a `SPEC.md` section headed by its code."""

    def test_one_section_per_plugin(self) -> None:
        from flnd.engine.plugins import PLUGINS

        text = prose(SPEC.read_text(encoding="utf-8"))
        sections = re.findall(r"^#{3}\s+([a-z]{3})\b", text, re.M)
        self.assertEqual(sorted(sections), sorted(PLUGINS))


class SizeTest(unittest.TestCase):
    """The pages stay within their line limits, and no line is wider than `LINE_CHARS`."""

    def test_limits(self) -> None:
        for page, limit in LIMITS.items():
            lines = page.read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(len(lines), limit, f"{page.name} has {len(lines)} lines")
            wide = [number for number, line in enumerate(lines, 1) if len(line) > LINE_CHARS]
            self.assertEqual(wide, [], f"{page.name}: lines over {LINE_CHARS} characters")


if __name__ == "__main__":
    unittest.main()
