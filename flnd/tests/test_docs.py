"""Keeps the docs in step with the code: docstrings, `src/docs/` pages, links, CLI flags and sources.

PLAN.md section 13 › Writing sets the rules this file checks. Each check is a test class below:

1. `DocstringTest` reads every module of `flnd` with `ast` (nothing is imported) and lists each
   docstring gap: a module without a docstring, one longer than `MODULE_LINES` lines (but the
   modules of `LONG`), one that names no `src/docs` page, and a public name without a summary.
   Every docstring with `>>>` lines then runs as a doctest.
2. `PageTest` runs every `python` block of `src/docs/**/*.md`, page by page, in a scratch folder.
3. `LinkTest` checks that every relative link in `src/docs/` and in the root's markdown resolves:
   the file exists, and a `#fragment` names one of its headings.
4. `CliTest` checks that `src/docs/cli.md` names every command and every option the CLI parses.
5. `SourcePageTest` checks that each built-in source has `src/docs/sources/<code>.md`, and that no
   page is left for a source that is gone.
6. `ReadmeTest` runs the `python` blocks of `README.md` offline, and parses every `flnd …`
   command line of its `bash` blocks with the CLI's own argparse parser, so a command, a flag or
   an example the README shows can't drift from the code.

A gap fails `test_no_gap`, with one line per gap naming the module or the name and the rule it
breaks, so new code is documented as it is written. The one allowlist, `LONG`, may only shrink:
fix the gap, or the docs page, in the same commit.

The whole file runs in well under a second on a laptop, since it parses and never imports the
modules it checks; only the API snapshot's names are imported, as `test_names.py` does.
"""

from __future__ import annotations

import ast
import contextlib
import doctest
import importlib
import inspect
import io
import os
import re
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

import api_snapshot
from test_packaging import ROOT

# ---------------------------------------------------------------------------------------------
# What is checked, and where
# ---------------------------------------------------------------------------------------------

# The packages whose modules must document themselves. `tests/` is for contributors, not the
# product, so it is left out.
PACKAGES = ("src", "flnkit")
DOCS = ROOT / "src" / "docs"
# The longest a module docstring may be, in lines: what the module holds, and its src/docs page.
MODULE_LINES = 3
# The modules that keep a longer docstring for now (25 of them): their flows and rules aren't
# in a src/docs page yet. Step 16 (PLAN.md section 14) empties this list by moving each into
# its page, or the module goes when a step replaces it. The list may only shrink:
# `test_long_docstrings_are_still_long`.
LONG = frozenset({
    "src/__init__.py", "src/_library.py", "src/cli.py", "src/errors.py", "src/source.py",
    "src/testing.py",
    "src/engine/events.py", "src/engine/home.py", "src/engine/options.py",
    "src/engine/plugins.py",
    "flnkit/browser.py", "flnkit/cookies.py", "flnkit/db.py", "flnkit/html.py",
    "flnkit/net.py", "flnkit/text.py",
    "src/sources/__init__.py", "src/sources/_manifest.py", "src/sources/fbk.py",
    "src/sources/ins.py", "src/sources/loi.py", "src/sources/mht.py", "src/sources/tik.py",
    "src/sources/tlg.py", "src/sources/twt.py",
})
# The marker that skips the `python` block right after it, with an optional reason:
# `<!-- docs-test: skip -->` or `<!-- docs-test: skip: it needs a real site -->`. A skipped block
# in `src/docs/` is a gap; one in the README must give its reason, and is still compiled.
SKIP = re.compile(r"^<!-- docs-test: skip(?:: (?P<reason>.+?))? -->$")
README = ROOT / "README.md"
# The terms the glossary must define: the words every page uses.
TERMS = ("target", "source", "route", "category", "post", "file", "event", "home")


# ---------------------------------------------------------------------------------------------
# 1. Docstrings
# ---------------------------------------------------------------------------------------------

def public_names() -> dict[tuple[str, str], str]:
    """The public functions, methods, classes, errors and properties, by where they are defined.

    "Public" means listed in the API snapshot (`make api`), which is what a script or
    a source author can reach. A name the snapshot reaches through a re-export points at the
    file that defines it, so the AST walk below can match it.

    Returns:
        `{(path, qualname): kind}`: the file's path relative to the root, with `/`; the
        `__qualname__`; and the snapshot's kind (`function`, `method`, `class`, `error` or
        `property`).
    """
    found: dict[tuple[str, str], str] = {}
    for name in api_snapshot.api_names():
        if name.kind not in ("function", "method", "class", "error", "property"):
            continue
        obj = name.obj.fget if isinstance(name.obj, property) else name.obj
        obj = inspect.unwrap(obj) if callable(obj) else obj
        try:
            path = Path(inspect.getsourcefile(obj) or "").resolve()
        except TypeError:  # a builtin, which has no source
            continue
        if path.is_relative_to(ROOT) and hasattr(obj, "__qualname__"):
            found[path.relative_to(ROOT).as_posix(), obj.__qualname__] = name.kind
    return found


def dotted(path: Path) -> str:
    """`src/engine/run.py` as `flnd.engine.run`; `flnkit/net.py` as `flnkit.net`."""
    parts = path.relative_to(ROOT).with_suffix("").parts
    if parts[0] == "src":
        parts = ("flnd", *parts[1:])
    return ".".join(parts).removesuffix(".__init__")


def modules() -> list[Path]:
    """Every module of `PACKAGES`, sorted, so the gaps are reported in one order on every OS."""
    return sorted(path for package in PACKAGES for path in (ROOT / package).rglob("*.py"))


def module_gaps(path: Path, public: dict[tuple[str, str], str]) -> list[str]:
    """Every docstring gap of one module, one line each.

    The rules (PLAN.md section 13): a module starts with a docstring of at most `MODULE_LINES`
    lines that names its `src/docs` page; a public name (in the API snapshot) has a docstring
    whose first line sums it up. `Args:`, `Returns:` and `Raises:` are written only when they
    add something, so nothing asks for them, and an `Example:` is never required.

    Args:
        path: the module's file.
        public: the API snapshot's names, from `public_names`.

    Returns:
        `<path>: <gap>` for the module itself, `<path>:<qualname>: <gap>` for a public name.
    """
    relative = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    doc = ast.get_docstring(tree)
    if not doc:
        return [f"{relative}: no module docstring"]
    gaps = []
    if "src/docs" not in doc:
        gaps.append(f"{relative}: the module docstring names no src/docs page")
    if len(doc.splitlines()) > MODULE_LINES and relative not in LONG:
        gaps.append(f"{relative}: the module docstring is over {MODULE_LINES} lines")

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = prefix + node.name
            summary = (ast.get_docstring(node) or "").strip()
            if (relative, name) in public and not summary:
                gaps.append(f"{relative}:{name}: no summary")
            if isinstance(node, ast.ClassDef):
                walk(node.body, name + ".")

    walk(tree.body, "")
    return gaps


def page_gaps() -> list[str]:
    """One gap per `src/docs/` page with skipped `python` blocks: `<page>: <n> skipped blocks`."""
    gaps = []
    for page in sorted(DOCS.rglob("*.md")):
        skipped = sum(1 for _, _, skip in python_blocks(page) if skip)
        if skipped:
            gaps.append(f"{page.relative_to(ROOT).as_posix()}: {skipped} skipped python blocks")
    return gaps


def gaps() -> list[str]:
    """Every gap the code and the docs have, sorted: `test_no_gap` wants none."""
    public = public_names()
    found = [gap for path in modules() for gap in module_gaps(path, public)]
    # A property and its setter share a name, so the same gap can come twice; it is listed once.
    return sorted(set(found + page_gaps()))


def rule(gap: str) -> str:
    """The rule of PLAN.md section 13 › Writing that one gap breaks, for the failure message.

    Args:
        gap: one line of `gaps()`.

    Returns:
        A short statement of the rule.
    """
    if gap.endswith("skipped python blocks"):
        return "every python block in src/docs/ runs; drop its skip marker and make it run"
    if gap.endswith("no module docstring"):
        return "a module starts with a docstring"
    if gap.endswith("names no src/docs page"):
        return "a module docstring names the src/docs page that explains it"
    if gap.endswith(f"over {MODULE_LINES} lines"):
        return (f"a module docstring is 1 to {MODULE_LINES} lines; the explanation lives in "
                "src/docs")
    return "a public name has a docstring that sums it up"


class DocstringTest(unittest.TestCase):
    """The docstring rules of PLAN.md section 13: any gap fails, naming it and its rule."""

    def test_no_gap(self) -> None:
        found = gaps()
        self.assertFalse(found, "document these (AGENTS.md › Writing), or fix the docs page:\n"
                         + "\n".join(f"{gap}  [rule: {rule(gap)}]" for gap in found))

    def test_long_docstrings_are_still_long(self) -> None:
        # A module of `LONG` trimmed to `MODULE_LINES` lines leaves the list.
        short = sorted(name for name in LONG
                       if len((ast.get_docstring(ast.parse((ROOT / name).read_text(
                           encoding="utf-8"))) or "").splitlines()) <= MODULE_LINES)
        self.assertEqual(short, [], "these are short now: delete them from LONG")

    def test_examples_run(self) -> None:
        """Every docstring with `>>>` lines runs as a doctest, in a scratch folder."""
        finder, runner = doctest.DocTestFinder(), doctest.DocTestRunner(verbose=False)
        for path in modules():
            if ">>>" not in path.read_text(encoding="utf-8"):
                continue
            name = dotted(path)
            module = importlib.import_module(name)
            with scratch():
                for test in finder.find(module, name):
                    runner.run(test)
        self.assertFalse(runner.failures, f"{runner.failures} docstring examples failed; "
                         "the output above shows each")


# ---------------------------------------------------------------------------------------------
# 2. Pages: the python blocks of src/docs/
# ---------------------------------------------------------------------------------------------

def fenced_blocks(page: Path) -> list[tuple[int, str, str, str]]:
    """Each fenced block of a page: its first line number, language, code, and the line before.

    Args:
        page: a markdown file.

    Returns:
        `(line, language, code, before)` per block, in page order: `line` is the number of the
        block's first code line, `language` the word after the fence ("" for none), and
        `before` the stripped line above the fence, where a `SKIP` marker sits.
    """
    lines = page.read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[int, str, str, str]] = []
    start: int | None = None
    fence = language = ""
    for number, line in enumerate(lines):
        stripped = line.strip()
        if start is None and stripped.startswith("```"):
            fence = stripped[: len(stripped) - len(stripped.lstrip("`"))]
            language = stripped[len(fence):].strip()
            start = number + 1
        elif start is not None and stripped == fence:
            before = lines[start - 2].strip() if start >= 2 else ""
            blocks.append((start + 1, language, "\n".join(lines[start:number]), before))
            start = None
    return blocks


def python_blocks(page: Path) -> list[tuple[int, str, bool]]:
    """Each fenced `python` block of a page: its first line number, its code, and whether the
    `SKIP` marker sits on the line before its fence.

    Args:
        page: a markdown file.

    Returns:
        `(line, code, skipped)` per block, in page order.
    """
    return [(line, code, bool(SKIP.match(before)))
            for line, language, code, before in fenced_blocks(page) if language == "python"]


@contextlib.contextmanager
def scratch() -> Iterator[Path]:
    """Run the block in a new empty folder, so an example never touches the real `.flnd`."""
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as folder:
        os.chdir(folder)
        try:
            yield Path(folder)
        finally:
            os.chdir(previous)


class PageTest(unittest.TestCase):
    """Every `python` block in `src/docs/` runs, so an example can't rot."""

    def test_python_blocks_run(self) -> None:
        # The blocks of one page share a namespace, like a doctest session: a tutorial builds a
        # class in one block and tests it in the next. `tests/` is on the path for the fakes.
        for page in sorted(DOCS.rglob("*.md")):
            namespace: dict[str, object] = {"__name__": f"docs_{page.stem.replace('-', '_')}"}
            for line, code, skipped in python_blocks(page):
                if skipped:
                    continue
                with (self.subTest(page=page.relative_to(ROOT).as_posix(), line=line), scratch(),
                      contextlib.redirect_stdout(io.StringIO())):
                    exec(compile(code, f"{page.name}:{line}", "exec"), namespace)


# ---------------------------------------------------------------------------------------------
# 3. Links
# ---------------------------------------------------------------------------------------------

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def prose(text: str) -> str:
    """A page without its fenced blocks and inline code, where a `[x](y)` isn't a link."""
    text = re.sub(r"^\s*(```+|~~~+).*?^\s*\1\s*$", "", text, flags=re.M | re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def anchors(page: Path) -> set[str]:
    """The `#fragment` each heading of a page gets, the way GitHub makes them.

    A heading is lowercased, loses every character but letters, digits, `-`, `_` and spaces,
    and its spaces become `-`. A repeated heading gets `-1`, `-2`, … in order.
    """
    seen: dict[str, int] = {}
    found: set[str] = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*$", prose(page.read_text("utf-8")), re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def markdown_files() -> list[Path]:
    """The pages whose links are checked: `src/docs/` and the root's markdown files."""
    return sorted([*DOCS.rglob("*.md"), *ROOT.glob("*.md")])


class LinkTest(unittest.TestCase):
    """Every relative link resolves, to a file and, with a `#fragment`, to one of its headings."""

    def test_links_resolve(self) -> None:
        broken: list[str] = []
        for page in markdown_files():
            for link in LINK.findall(prose(page.read_text(encoding="utf-8"))):
                if re.match(r"[a-z][a-z0-9+.-]*:", link):  # https:, mailto: and the like
                    continue
                path, _, fragment = link.partition("#")
                target = (page.parent / path).resolve() if path else page
                where = f"{page.relative_to(ROOT).as_posix()}: {link}"
                if not target.exists():
                    broken.append(f"{where} (no such file)")
                elif fragment and target.suffix == ".md" and fragment not in anchors(target):
                    broken.append(f"{where} (no such heading)")
        self.assertFalse(broken, "broken links:\n" + "\n".join(broken))

    def test_docs_index_links_every_page(self) -> None:
        index = (DOCS / "README.md").read_text(encoding="utf-8")
        linked = {(DOCS / link.partition("#")[0]).resolve() for link in LINK.findall(index)}
        pages = {page.resolve() for page in DOCS.rglob("*.md")} - {(DOCS / "README.md").resolve()}
        missing = sorted(page.relative_to(DOCS).as_posix() for page in pages - linked)
        self.assertFalse(missing, f"src/docs/README.md doesn't link: {', '.join(missing)}")

    def test_glossary_defines_the_terms(self) -> None:
        glossary = (DOCS / "glossary.md").read_text(encoding="utf-8").lower()
        missing = [term for term in TERMS if not re.search(rf"^#+\s+{term}\b", glossary, re.M)]
        self.assertFalse(missing, f"src/docs/glossary.md has no heading for: {', '.join(missing)}")


# ---------------------------------------------------------------------------------------------
# 4. The CLI
# ---------------------------------------------------------------------------------------------

def cli_words() -> list[str]:
    """Every command the CLI has, as typed (`flnd download`), and every flag it parses.

    Read from the argparse parser itself (`flnd.cli.parser`): the top parser's flags, then each
    command's name and its own flags (`--only`, `-v`, `--verbose`). Positionals (`target`) and
    `--help`'s `-h` spelling are left out: the page shows them in each command's usage line.

    Returns:
        The words `src/docs/cli.md` must contain, in command order.
    """
    import argparse

    from flnd.cli import parser

    top = parser()
    words: list[str] = []

    def flags(command: argparse.ArgumentParser) -> None:
        for action in command._actions:
            words.extend(flag for flag in action.option_strings if flag != "-h")

    flags(top)
    for action in top._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, command in action.choices.items():
                words.append(f"flnd {name}")
                flags(command)
    return list(dict.fromkeys(words))


class CliTest(unittest.TestCase):
    """`src/docs/cli.md` names every command and every option, so the page can't fall behind."""

    def test_every_command_and_option_is_documented(self) -> None:
        page = (DOCS / "cli.md").read_text(encoding="utf-8")
        missing = [word for word in cli_words()
                   if not re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", page)]
        self.assertFalse(missing, f"src/docs/cli.md doesn't mention: {', '.join(missing)}")


# ---------------------------------------------------------------------------------------------
# 5. One page per source
# ---------------------------------------------------------------------------------------------

class SourcePageTest(unittest.TestCase):
    """Each built-in source has `src/docs/sources/<code>.md`, and each page a source."""

    def test_one_page_per_source(self) -> None:
        from flnd.sources import SOURCES

        pages = {page.stem for page in (DOCS / "sources").glob("*.md")}
        self.assertEqual(sorted(pages), sorted([*SOURCES, "web"]))


# ---------------------------------------------------------------------------------------------
# 6. The README
# ---------------------------------------------------------------------------------------------

# The words a shell joins commands with; each part is checked as a command of its own.
JOINERS = {"|", "||", "&&", ";"}


def commands(code: str) -> list[list[str]]:
    """The arguments of every `flnd` command in a block of shell lines.

    Each line is split the way a POSIX shell splits words (`shlex`), comments dropped, and cut at
    `|`, `&&`, `||` and `;`. A part runs flnd when one of its words is a program named `flnd`
    (`flnd`, `/path/to/flnd`, `uv run flnd`, or the command of a cron line); what follows that
    word is flnd's arguments. A line ending in `\\` goes on on the next.

    Args:
        code: the block's lines.

    Returns:
        One argument list per `flnd` command, without the `flnd` itself.
    """
    import shlex

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
    """What the CLI's parser says about `flnd <args>`: "" when it parses.

    Args:
        args: flnd's arguments, as a shell would pass them.

    Returns:
        argparse's message (an unknown command or flag, a missing value), or "".
    """
    from flnd.cli import parser

    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            parser().parse_args(args)
    except SystemExit as stop:
        if stop.code:
            return stderr.getvalue().strip().splitlines()[-1]
    return ""


class ReadmeTest(unittest.TestCase):
    """The README's examples are true: its Python runs, and its `flnd` command lines parse."""

    def test_python_blocks_run(self) -> None:
        # The blocks share one namespace, in page order: "Add a site" defines a source in one
        # block and tests it with `flnd.testing` in the next. A block marked to skip (one that
        # needs a real site) must say why, and is compiled, so at least its syntax is checked.
        namespace: dict[str, object] = {"__name__": "docs_readme"}
        for line, language, code, before in fenced_blocks(README):
            if language != "python":
                continue
            with self.subTest(line=line):
                compiled = compile(code, f"README.md:{line}", "exec")
                skip = SKIP.match(before)
                if skip:
                    self.assertTrue(skip["reason"], f"README.md:{line}: say why it is skipped")
                    continue
                with scratch(), contextlib.redirect_stdout(io.StringIO()):
                    exec(compiled, namespace)

    def test_commands_parse(self) -> None:
        checked = 0
        for line, language, code, _ in fenced_blocks(README):
            if language not in ("bash", "sh", "shell"):
                continue
            for args in commands(code):
                checked += 1
                with self.subTest(line=line, command=" ".join(["flnd", *args])):
                    self.assertEqual(parse_error(args), "")
        self.assertGreater(checked, 5, "README.md shows no flnd command lines")


if __name__ == "__main__":
    unittest.main()
