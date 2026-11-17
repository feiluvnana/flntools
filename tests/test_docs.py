"""Keeps the docs in step with the code: docstrings, `flnddocs/` pages, links, CLI flags and sources.

AGENTS.md › Writing sets the rules this file checks. Each check is a test class below:

1. `DocstringTest` reads every module of `flnd` with `ast` (nothing is imported)
   and lists each docstring gap: a module, class or function without a docstring, a Google-style
   section it lacks (`Args:` naming each argument, `Returns:`, `Yields:`, `Raises:`), and a
   public name without a runnable `Example:` (one with `>>>` lines). Every docstring with `>>>`
   lines then runs as a doctest.
2. `PageTest` runs every `python` block of `flnddocs/**/*.md`, page by page, in a scratch folder.
3. `LinkTest` checks that every relative link in `flnddocs/` and in the root's markdown resolves:
   the file exists, and a `#fragment` names one of its headings.
4. `CliTest` checks that `flnddocs/cli.md` names every command and every option the CLI parses.
5. `SourcePageTest` checks that each built-in source has `flnddocs/sources/<code>.md`, and that no
   page is left for a source that is gone.
6. `ReadmeTest` runs the `python` blocks of `README.md` offline, and parses every `flnd …`
   command line of its `bash` blocks with the CLI's own argparse parser, so a command, a flag or
   an example the README shows can't drift from the code.

A gap fails `test_no_gap`, with one line per gap naming the module or the name and the rule it
breaks, so new code is documented as it is written. There is no allowlist: fix the gap, or
the docs page, in the same commit.

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
PACKAGES = ("flnd",)
DOCS = ROOT / "flnddocs"
# A private function this short (body lines, docstring included) needs no docstring: AGENTS.md
# exempts "a few obvious lines".
SHORT = 5
# The headings of a Google-style docstring this file reads. `Examples:` is accepted too.
SECTION = re.compile(r"^\s*(Args|Arguments|Returns|Yields|Raises|Examples?):\s*$")
# The marker that skips the `python` block right after it, with an optional reason:
# `<!-- docs-test: skip -->` or `<!-- docs-test: skip: it needs a real site -->`. A skipped block
# in `flnddocs/` is a gap; one in the README must give its reason, and is still compiled.
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


def modules() -> list[Path]:
    """Every module of `PACKAGES`, sorted, so the gaps are reported in one order on every OS."""
    return sorted(path for package in PACKAGES for path in (ROOT / package).rglob("*.py"))


def sections(doc: str) -> dict[str, list[str]]:
    """A docstring's Google-style sections: each heading, with the lines under it.

    Args:
        doc: the docstring, as `ast.get_docstring` cleans it.

    Returns:
        `{"Args": [...], "Returns": [...], ...}`; `Arguments` counts as `Args`, `Examples` as
        `Example`. The summary and the paragraphs before the first heading are left out.
    """
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in doc.splitlines():
        heading = SECTION.match(line)
        if heading:
            name = {"Arguments": "Args", "Examples": "Example"}.get(heading[1], heading[1])
            current = found.setdefault(name, [])
        elif current is not None:
            current.append(line)
    return found


def documented_args(lines: list[str]) -> set[str]:
    """The argument names an `Args:` section documents.

    An entry is a line at the section's first indent, written `name:`, `name (type):`,
    `*args:` or `a, b, c:` (several arguments sharing one meaning). Deeper lines continue the
    entry above them.

    Args:
        lines: the lines under the `Args:` heading.

    Returns:
        The names, without their `*` or `**`.
    """
    entries = [line for line in lines if line.strip()]
    if not entries:
        return set()
    indent = len(entries[0]) - len(entries[0].lstrip())
    names: set[str] = set()
    for line in entries:
        if len(line) - len(line.lstrip()) != indent:
            continue
        head = re.match(r"\s*([\w*][\w*, ]*?)\s*(\([^)]*\))?:", line)
        if head:
            names.update(part.strip().lstrip("*") for part in head[1].split(","))
    return names


def own_nodes(function: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a function's own body, without nested functions, lambdas or classes.

    A `return` or `raise` in a nested function is that function's, not this one's.
    """
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def caught_raises(function: ast.AST) -> set[ast.AST]:
    """The `raise` statements inside a `try` body that has handlers: those may never escape."""
    caught: set[ast.AST] = set()
    for node in own_nodes(function):
        if isinstance(node, ast.Try) and node.handlers:
            for statement in node.body:
                caught.update(n for n in ast.walk(statement) if isinstance(n, ast.Raise))
    return caught


def function_gaps(node: ast.FunctionDef | ast.AsyncFunctionDef, method: bool,
                  public: str) -> list[str]:
    """The docstring gaps of one function or method.

    The rules, in order:
    1. A private function whose body is at most `SHORT` lines needs nothing.
    2. Every other function needs a docstring.
    3. `Args:` names each argument but `self` and `cls`.
    4. `Returns:` when the body returns a value, `Yields:` when it yields.
    5. `Raises:` when the body raises outside a `try` that may catch it.
    6. A public function or method has an `Example:` with `>>>` lines. A property documents
       itself like an attribute, so it needs no example.
    Rules 3 to 5 don't apply to a property either.

    Args:
        node: the function's AST node.
        method: whether it is defined in a class body, so its first argument is bound.
        public: the API snapshot's kind for it; "" when it isn't public.

    Returns:
        One line per gap, as the failure message spells it after the name.
    """
    doc = ast.get_docstring(node)
    lines = node.end_lineno - node.body[0].lineno + 1
    if not public and lines <= SHORT:
        return []
    if doc is None:
        return ["no docstring"]
    names = {decorator.id if isinstance(decorator, ast.Name) else getattr(decorator, "attr", "")
             for decorator in node.decorator_list}
    found = sections(doc)
    gaps: list[str] = []
    if "property" not in names and "setter" not in names:
        params = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        if method and "staticmethod" not in names and params:
            params = params[1:]
        wanted = [arg.arg for arg in params]
        wanted += [arg.arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None]
        have = documented_args(found.get("Args", []))
        gaps += [f"Args: no {name}" for name in wanted if name not in have]
        body = list(own_nodes(node))
        yields = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in body)
        returns = any(isinstance(n, ast.Return) and n.value is not None
                      and not (isinstance(n.value, ast.Constant) and n.value.value is None)
                      for n in body)
        if yields and "Yields" not in found:
            gaps.append("no Yields:")
        elif returns and not yields and "Returns" not in found:
            gaps.append("no Returns:")
        caught = caught_raises(node)
        if "Raises" not in found and any(isinstance(n, ast.Raise) and n.exc is not None
                                         and n not in caught for n in body):
            gaps.append("no Raises:")
    if public in ("function", "method") and ">>>" not in "\n".join(found.get("Example", [])):
        gaps.append("no Example:")
    return gaps


def class_gaps(node: ast.ClassDef, public: str) -> list[str]:
    """The docstring gaps of a class itself; its methods are checked one by one.

    A class needs a docstring, and a public one an `Example:`; an error needs none, since its
    message shows how it reads. An explicit `__init__` takes arguments the class must document,
    in the class's `Args:` or in `__init__`'s own.

    Args:
        node: the class's AST node.
        public: the API snapshot's kind for it; "" when it isn't public.

    Returns:
        One line per gap, as the failure message spells it after the name.
    """
    doc = ast.get_docstring(node)
    if doc is None:
        return ["no docstring"]
    found = sections(doc)
    gaps: list[str] = []
    init = next((item for item in node.body if isinstance(item, ast.FunctionDef)
                 and item.name == "__init__"), None)
    if init is not None:
        have = documented_args(found.get("Args", []))
        have |= documented_args(sections(ast.get_docstring(init) or "").get("Args", []))
        params = (init.args.posonlyargs + init.args.args + init.args.kwonlyargs)[1:]
        params += [arg for arg in (init.args.vararg, init.args.kwarg) if arg is not None]
        gaps += [f"Args: no {arg.arg}" for arg in params if arg.arg not in have]
    if public == "class" and ">>>" not in "\n".join(found.get("Example", [])):
        gaps.append("no Example:")
    return gaps


def overload(node: ast.AST) -> bool:
    """Whether a function is an `@overload` stub, documented by the function it describes."""
    return any((isinstance(d, ast.Name) and d.id == "overload")
               or (isinstance(d, ast.Attribute) and d.attr == "overload")
               for d in getattr(node, "decorator_list", []))


def module_gaps(path: Path, public: dict[tuple[str, str], str]) -> list[str]:
    """Every docstring gap of one module, one line each.

    The walk goes into classes (and their nested classes), but not into functions: a closure
    is part of the function that holds it. Dunder methods other than `__init__` are skipped,
    since the language says what they do; `__init__` is checked as part of its class.

    Args:
        path: the module's file.
        public: the API snapshot's names, from `public_names`.

    Returns:
        `<path>: <gap>` for the module itself, `<path>:<qualname>: <gap>` for what is in it.
    """
    relative = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    gaps = [] if ast.get_docstring(tree) else [f"{relative}: no module docstring"]

    def walk(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                name = prefix + node.name
                gaps.extend(f"{relative}:{name}: {gap}"
                            for gap in class_gaps(node, public.get((relative, name), "")))
                walk(node.body, name + ".", True)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if overload(node) or (node.name.startswith("__") and node.name.endswith("__")):
                    continue
                name = prefix + node.name
                gaps.extend(f"{relative}:{name}: {gap}"
                            for gap in function_gaps(node, in_class,
                                                     public.get((relative, name), "")))

    walk(tree.body, "", False)
    return gaps


def page_gaps() -> list[str]:
    """One gap per `flnddocs/` page with skipped `python` blocks: `<page>: <n> skipped blocks`."""
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
    """The rule of AGENTS.md › Writing that one gap breaks, for the failure message.

    Args:
        gap: one line of `gaps()`.

    Returns:
        A short statement of the rule.
    """
    if gap.endswith("skipped python blocks"):
        return "every python block in flnddocs/ runs; drop its skip marker and make it run"
    if gap.endswith("no module docstring"):
        return "a module starts with a docstring"
    if gap.endswith("no docstring"):
        return "a class or function has a docstring (a private one of a few lines may skip it)"
    if ": Args: no " in gap:
        return "`Args:` names each argument"
    if gap.endswith("no Example:"):
        return "a public name has an `Example:` with `>>>` lines"
    return "a docstring has `Returns:`, `Yields:` or `Raises:` when the body does that"


class DocstringTest(unittest.TestCase):
    """The docstring rules of AGENTS.md › Writing: any gap fails, naming it and its rule."""

    def test_no_gap(self) -> None:
        found = gaps()
        self.assertFalse(found, "document these (AGENTS.md › Writing), or fix the docs page:\n"
                         + "\n".join(f"{gap}  [rule: {rule(gap)}]" for gap in found))

    def test_examples_run(self) -> None:
        """Every docstring with `>>>` lines runs as a doctest, in a scratch folder."""
        finder, runner = doctest.DocTestFinder(), doctest.DocTestRunner(verbose=False)
        for path in modules():
            if ">>>" not in path.read_text(encoding="utf-8"):
                continue
            name = ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
            module = importlib.import_module(name)
            with scratch():
                for test in finder.find(module, name):
                    runner.run(test)
        self.assertFalse(runner.failures, f"{runner.failures} docstring examples failed; "
                         "the output above shows each")


# ---------------------------------------------------------------------------------------------
# 2. Pages: the python blocks of flnddocs/
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
    """Every `python` block in `flnddocs/` runs, so an example can't rot."""

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
    """The pages whose links are checked: `flnddocs/` and the root's markdown files."""
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
        self.assertFalse(missing, f"flnddocs/README.md doesn't link: {', '.join(missing)}")

    def test_glossary_defines_the_terms(self) -> None:
        glossary = (DOCS / "glossary.md").read_text(encoding="utf-8").lower()
        missing = [term for term in TERMS if not re.search(rf"^#+\s+{term}\b", glossary, re.M)]
        self.assertFalse(missing, f"flnddocs/glossary.md has no heading for: {', '.join(missing)}")


# ---------------------------------------------------------------------------------------------
# 4. The CLI
# ---------------------------------------------------------------------------------------------

def cli_words() -> list[str]:
    """Every command the CLI has, as typed (`flnd download`), and every flag it parses.

    Read from the argparse parser itself (`flnd.cli.parser`): the top parser's flags, then each
    command's name and its own flags (`--only`, `-v`, `--verbose`). Positionals (`target`) and
    `--help`'s `-h` spelling are left out: the page shows them in each command's usage line.

    Returns:
        The words `flnddocs/cli.md` must contain, in command order.
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
    """`flnddocs/cli.md` names every command and every option, so the page can't fall behind."""

    def test_every_command_and_option_is_documented(self) -> None:
        page = (DOCS / "cli.md").read_text(encoding="utf-8")
        missing = [word for word in cli_words()
                   if not re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", page)]
        self.assertFalse(missing, f"flnddocs/cli.md doesn't mention: {', '.join(missing)}")


# ---------------------------------------------------------------------------------------------
# 5. One page per source
# ---------------------------------------------------------------------------------------------

class SourcePageTest(unittest.TestCase):
    """Each built-in source has `flnddocs/sources/<code>.md`, and each page a source."""

    def test_one_page_per_source(self) -> None:
        from flnd.sources import SOURCES

        pages = {page.stem for page in (DOCS / "sources").glob("*.md")}
        self.assertEqual(sorted(pages), sorted(SOURCES))


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
