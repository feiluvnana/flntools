"""The code rules of AGENTS.md, checked over flnd's own packages (src/ and flnkit/).

`DeadNameTest` checks that flnd holds no dead code: every name defined at the top of any module of
those packages, and every method of their classes, is used somewhere in them beyond its
definition, or is a hook Python or the framework calls (a dunder, a source's route or category).
It reads the modules with `ast` and searches by word, so a docstring, a comment or an `__all__`
entry isn't a use, and neither is a test: code only tests call is dead. The few names called from
outside flnd are in `CALLED_FROM_OUTSIDE`, each with who calls it; that list may only shrink.

`ReadableTest` checks the writing rules of AGENTS.md over the same packages: no plan or history
references in comments and docstrings, no import aliases of flnd's own modules, and lines of at
most 100 characters.
"""

import ast
import io
import re
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT_PACKAGES = ("flnd", "flnkit")
# Each package's folder: flnd is src/, flnkit sits at the repo root (AGENTS.md › Layout).
PACKAGE_DIRS = {"flnd": ROOT / "src", "flnkit": ROOT.parent / "flnkit" / "flnkit"}


HISTORY_REF = re.compile(
    r"plan/"
    r"|(?i:\bphase[- ]?\d+\b)"
    r"|(?i:\bstep\s+\d+\b(?!:))"
    r"|(?i:\bfixes\s+#\d+\b)"
    r"|(?i:\b(?:as|was|as it was)\s+before\b|\bas it always did\b)"
    r"|(?i:(?<!\w)(?!info\.md\b|SPEC\.md\b|COOKBOOK\.md\b)[A-Za-z0-9_-]+\.md\b)"
    r"|(?<!#)\b[A-F]\d{1,2}\b"
)


def history_breaches(source: str) -> list[str]:
    """Comments or docstrings that refer to plans, `.md` files, issues, phases or "as before"."""
    tree = ast.parse(source)
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                doc = node.body[0]
                doc_lines.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    found: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            if "# noqa" in token.string:
                continue
        elif not (token.type == tokenize.STRING and token.start[0] in doc_lines):
            continue
        for offset, line in enumerate(token.string.splitlines()):
            match = HISTORY_REF.search(line)
            if match:
                found.append(f"line {token.start[0] + offset}: {match.group(0)}")
    return found


def flnd_import_aliases(source: str) -> list[str]:
    """Import aliases (`as`) of `flnd` or `flnkit` modules or symbols. Every one is a breach.

    AGENTS.md says import a module by its own name. The transitional aliases of an earlier
    move (`from flnd import _text as text`) are gone, so any `as` on a `flnd` or `flnkit`
    import, or a relative one, fails here.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name.split(".")[0] in ("flnd", "flnkit"):
                    found.append(f"line {node.lineno}: import {alias.name} as {alias.asname}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level > 0 or mod.split(".")[0] in ("flnd", "flnkit"):
                for alias in node.names:
                    if alias.asname:
                        found.append(
                            f"line {node.lineno}: from {mod} import {alias.name} as {alias.asname}"
                        )
    return found


def line_length_breaches(source: str, limit: int = 100) -> list[str]:
    """Lines longer than `limit` characters."""
    return [f"line {i}: {len(line)} chars" for i, line in enumerate(source.splitlines(), 1)
            if len(line) > limit]


FLND = PACKAGE_DIRS["flnd"]
FLNKIT = PACKAGE_DIRS["flnkit"]
# The modules whose names must each be used: all of src/ and flnkit/.
PRIVATE = ("**/*.py",)


# A string constant that reads as a name or a dotted path (`"chapter"`, `"flnd.plugins.twt"`)
# counts as a use of its words: routes, categories and lazy imports name code in strings.
NAMEISH = re.compile(r"[\w.:]+")


WORD = re.compile(r"[A-Za-z_]\w*")


# Names something outside flnd calls by name, each with who calls it. A name is listed here only
# when nothing in flnd/ could use it instead. The list may only shrink: a listed name that
# becomes used, or goes, fails `test_no_stale_allowance`.
CALLED_FROM_OUTSIDE: dict[str, str] = {
    "src/engine/run.py:restoring": "registers an undo function to run on hard exit (Ctrl-C); "
                              "flnd's own tests use it",
    "flnkit/net.py:fake": "answers every request in a block from routes, never the network; "
                         "the tests (tests/plugin_kit.py and the test modules) use it",
    "flnkit/files.py:fake": "answers every file read in a block from handlers; the tests use it",
    "flnkit/clock.py:Fake": "a clock that moves only when slept on; tests/plugin_kit.py's "
                            "fake_clock is its only caller",
    "src/plugin.py:headline": "plugins (naming plan)",
    "flnkit/clock.py:use": "makes a clock the active one inside a block; the tests use it",
}


# Kit names built early, before the code that calls them: each counts as unused, whatever the
# word search finds, until a module of src/ or flnkit/ calls it as `<module>.<name>`. Then
# `test_built_early_is_not_called_yet` fails, so the entry goes when its caller lands.
BUILT_EARLY: dict[str, str] = {
    name: "built early; the engine's save calls it for File.stream"
    for name in ("flnkit/stream.py:dash", "flnkit/stream.py:fetch_all", "flnkit/stream.py:hls",
                 "flnkit/stream.py:pick")
}


def _rel(path: Path) -> str:
    if path.is_relative_to(FLND):
        return f"src/{path.relative_to(FLND).as_posix()}"
    return f"flnkit/{path.relative_to(FLNKIT).as_posix()}"


def _resolve(entry_path: str) -> Path:
    if entry_path.startswith("flnkit/"):
        return FLNKIT / entry_path.removeprefix("flnkit/")
    return ROOT / entry_path


def early_calls(entry: str) -> list[str]:
    """The files of src/ and flnkit/, beside the name's own, that call it as `<module>.<name>`."""
    path, name = entry.split(":", 1)
    call = re.compile(rf"\b{re.escape(Path(path).stem)}\.{re.escape(name)}\b")
    return sorted(_rel(other) for root in (FLND, FLNKIT)
                  for other in root.rglob("*.py")
                  if other != _resolve(path) and call.search(other.read_text("utf-8")))


def registered(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a method is a source's route or category, which the framework finds by decorator."""
    for decorator in function.decorator_list:
        decorator = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(decorator, ast.Name) and decorator.id in ("target", "route"):
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr == "category":
            return True
    return False


def definitions(source: str) -> list[tuple[str, str]]:
    """The names a module defines at its top level, and its classes' methods.

    Args:
        source: the module's text.

    Returns:
        `(name, qualname)` per definition, in file order: a function, class or assigned name
        at the top, and `Class.method` for a method. Dunders and a source's routes and
        categories are left out, since Python and the framework call them.
    """
    found: list[tuple[str, str]] = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.name, node.name))
            if isinstance(node, ast.ClassDef):
                found += [(sub.name, f"{node.name}.{sub.name}") for sub in node.body
                          if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                          and not registered(sub)]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                names = target.elts if isinstance(target, ast.Tuple) else [target]
                found += [(name.id, name.id) for name in names if isinstance(name, ast.Name)]
    return [(name, qual) for name, qual in found
            if not (name.startswith("__") and name.endswith("__"))]


def references(source: str) -> set[str]:
    """Every name a module uses: read names, attributes, imports, keywords and name-like strings.

    A definition itself isn't a use, nor is a docstring or an `__all__` entry: a name only
    exported or only described is still dead.

    Args:
        source: the module's text.

    Returns:
        The words used.
    """
    tree = ast.parse(source)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                                                and target.id == "__all__"
                                                for target in node.targets):
            skip |= {id(sub) for sub in ast.walk(node.value)}
    used: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.alias):
            used.add(node.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.keyword) and node.arg:
            used.add(node.arg)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and NAMEISH.fullmatch(node.value)):
            used |= set(WORD.findall(node.value))
    return used


def dead_names() -> set[str]:
    """Every name of src/ and flnkit/ that nothing in those packages uses.

    The search is by word over both packages, so a name is used when any module uses that
    word; tests don't count, since code only tests call is dead.

    Returns:
        `<path>:<qualname>` per dead name, as `CALLED_FROM_OUTSIDE` spells it.
    """
    used: set[str] = set()
    for path in [*FLND.rglob("*.py"), *FLNKIT.rglob("*.py")]:
        used |= references(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for path in sorted({path for pattern in PRIVATE for root in (FLND, FLNKIT)
                        for path in root.glob(pattern)}):
        relative = _rel(path)
        for name, qual in definitions(path.read_text(encoding="utf-8")):
            if name not in used:
                found.add(f"{relative}:{qual}")
    return found


class DeadNameTest(unittest.TestCase):
    """Every private name is used somewhere in flnd/, or is listed with who calls it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dead = dead_names()

    def test_no_dead_name(self):
        new = sorted(self.dead - set(CALLED_FROM_OUTSIDE) - set(BUILT_EARLY))
        self.assertEqual(new, [], "nothing in flnd/ uses these: delete them, and the tests that "
                         "only exercise them (AGENTS.md › Writing: plain over clever)")

    def test_no_stale_allowance(self):
        stale = sorted(set(CALLED_FROM_OUTSIDE) - self.dead)
        self.assertEqual(stale, [], "used now, or gone: delete these from CALLED_FROM_OUTSIDE")

    def test_built_early_is_not_called_yet(self):
        for entry in BUILT_EARLY:
            with self.subTest(entry):
                path, name = entry.split(":", 1)
                defined = {qual for _, qual in definitions(_resolve(path).read_text())}
                self.assertIn(name, defined, "gone: delete it from BUILT_EARLY")
                self.assertEqual(early_calls(entry), [], "called now: delete it from BUILT_EARLY")

    def test_the_search_sees_uses_and_ignores_mentions(self):
        source = ('"""Says `told` in the docstring."""\n'
                  '__all__ = ["exported"]\n'
                  "def used(): ...\n"
                  "def exported(): ...\n"
                  "def told(): ...\n"
                  "class Box:\n"
                  "    def __init__(self): ...\n"
                  "    def get(self): return used()\n"
                  "    @target(\"x/{id}\")\n"
                  "    def route(self): ...\n"
                  'LAZY = "pkg.mod:Box"\n')
        self.assertEqual(definitions(source),
                         [("used", "used"), ("exported", "exported"), ("told", "told"),
                          ("Box", "Box"), ("get", "Box.get"), ("LAZY", "LAZY")])
        used = references(source)
        self.assertTrue({"used", "Box", "target"} <= used)
        self.assertFalse({"exported", "told", "get", "LAZY"} & used)


def _source_files() -> list[str]:
    return sorted(
        _rel(path)
        for pkg in PROJECT_PACKAGES
        for path in PACKAGE_DIRS[pkg].rglob("*.py")
    )


class ReadableTest(unittest.TestCase):
    def test_no_plan_or_history_references_in_code(self):
        for path in _source_files():
            with self.subTest(path):
                text = _resolve(path).read_text(encoding="utf-8")
                self.assertEqual(history_breaches(text), [])

    def test_no_flnd_import_aliases(self):
        for path in sorted([*FLND.rglob("*.py"), *FLNKIT.rglob("*.py")]):
            rel = _rel(path)
            with self.subTest(rel):
                self.assertEqual(flnd_import_aliases(path.read_text(encoding="utf-8")), [])

    def test_lines_are_at_most_100_characters(self):
        for path in _source_files():
            with self.subTest(path):
                text = _resolve(path).read_text(encoding="utf-8")
                self.assertEqual(line_length_breaches(text), [])

    def test_history_and_alias_checks_catch_breaches(self):
        bad = ('"""See plan/phase-6/redesign.md (B3)."""\n'
               '# fixes #8, Phase 5, as before, step 4\n'
               'from flnd import source as _source\n'
               f'x = "{"a" * 100}"\n')
        self.assertEqual(len(history_breaches(bad)), 2)
        self.assertEqual(len(flnd_import_aliases(bad)), 1)
        self.assertEqual(len(line_length_breaches(bad)), 1)
        good = ('"""Writes info.md."""\n'
                '# Step 1: parse.\n'
                'A1 = TypeVar("A1")\n'
                'color = "#f00"\n'
                'from flnd import source\n')
        self.assertEqual(history_breaches(good), [])
        self.assertEqual(flnd_import_aliases(good), [])
        self.assertEqual(line_length_breaches(good), [])


if __name__ == "__main__":
    unittest.main()
