"""The naming conventions of AGENTS.md › API and naming, checked over the API snapshot.

The names are the ones `make api` writes to tests/data/api.txt (test_api.py keeps that
file current), read from the same `api_names()` with their objects, so a check can look at a real
signature. Nothing is faked: the names come from importing `flnd`.

A breach the code may keep would be listed in `ALLOWED`, by module, and nowhere else; the list
is empty. A new breach fails `test_no_new_breach`, and a fixed one still listed fails
`test_no_stale_allowance`, so the list can only shrink.

`DeadNameTest` checks that flnd holds no dead code: every name defined at the top of any module of
flnd/, and every method of its classes, is used somewhere in flnd/ beyond its definition, or is
public (in the API snapshot), or is a hook Python or the framework calls (a dunder, a source's route
or category). It reads the modules with `ast` and searches by word, so a docstring, a comment or an
`__all__` entry isn't a use, and neither is a test: code only tests call is dead. The few names
called from outside flnd are in `CALLED_FROM_OUTSIDE`, each with who calls it; that list may only
shrink.
"""

import ast
import inspect
import io
import re
import sys
import tokenize
import typing
import unittest
from pathlib import Path
from typing import Any

import api_snapshot as snapshot

NAMES = snapshot.api_names()

# W7: prefixes that only say "this is a function".
PREFIXES = ("get_", "do_", "to_", "try_", "load_from_", "make_", "create_")
# Rule 6: at most this many options after the first parameter (AGENTS.md: "Options are
# keyword-only, at most 5").
MAX_OPTIONS = 5
# Option names the vocabulary has another word for (AGENTS.md: "One word, one meaning").
RENAMED = {"out": "dest", "dest_dir": "dest", "preset": "level"}
POSITIONAL = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)

# Every breach the code is allowed, by the module it is in: none. Nothing is exempt, so a new name
# that breaks a convention fails.
ALLOWED: dict[str, tuple[str, ...]] = {}
# The one exception to "no aliases" (PLAN.md 5.1): `flnd.errors` re-exports the classes flnkit
# defines and raises, and `disk`, as the same objects, since flnkit imports nothing of flnd.
REEXPORTS = ("Blocked", "Changed", "DiskError", "Error", "LoginError", "NetworkError", "NotFound",
             "SiteError", "Stopped", "ToolError", "UsageError", "disk")


def breaches() -> set[str]:
    """Every breach of the conventions in the snapshot, one text per breach, as `ALLOWED` spells it."""
    found: set[str] = set()
    for name in NAMES:
        found |= prefix_breaches(name) | option_breaches(name)
    return found | error_breaches()


def prefix_breaches(name: Any) -> set[str]:
    """W7: the name's last part starts with a prefix that only says "function"."""
    last = name.name.rsplit(".", 1)[-1]
    return {f"{name.name}: prefix {prefix}" for prefix in PREFIXES if last.startswith(prefix)}


def option_breaches(name: Any) -> set[str]:
    """The option rules (at most 5, the vocabulary's words) over one function, method or class.

    Each is checked in its signature and in each of its overloads.
    """
    if name.kind not in ("function", "method", "class"):
        return set()
    found: set[str] = set()
    overloads = typing.get_overloads(name.obj) if name.kind != "class" else []
    for function in [None, *overloads]:
        try:
            parameters = snapshot.parameters(name, function)
        except ValueError:  # no signature to read: a class whose constructor is a builtin's
            continue
        # S3, for functions and methods; a constructor's fields are its data, so they may be positional.
        if name.kind != "class" and not follows_protocol(name):
            found |= {f"{name.name}: positional option {parameter.name}" for parameter in parameters[1:]
                      if parameter.kind in POSITIONAL and parameter.default is not inspect.Parameter.empty}
        for parameter, scope in expanded(parameters, name.obj, function):
            field = name.kind == "class" and parameter.kind in POSITIONAL
            if parameter.name in RENAMED and not field:
                found.add(f"{name.name}: option {parameter.name}, not {RENAMED[parameter.name]}")
            if parameter.name == "progress" and progress_callback(parameter.annotation, scope):
                found.add(f"{name.name}: option progress, a callback, not a sink")
        options = [parameter for parameter in parameters[1:] if parameter.kind is not inspect.Parameter.VAR_KEYWORD
                   and parameter.kind is not inspect.Parameter.VAR_POSITIONAL]
        if name.kind != "class" and len(options) > MAX_OPTIONS:
            found.add(f"{name.name}: {len(options)} options, more than {MAX_OPTIONS}")
    return found


def follows_protocol(name: Any) -> bool:
    """Whether a method overrides one of a stdlib base's, as `Store.get` does `Mapping.get`.

    Such a method keeps the stdlib's signature (W4), positional default and all, since code written
    for the base calls it that way.
    """
    if name.kind != "method":
        return False
    member = name.name.rsplit(".", 1)[-1]
    return any(member in vars(base) for base in name.owner.__mro__[1:]
               if base.__module__.split(".")[0] not in snapshot.PROJECT_PACKAGES)


def expanded(parameters: list[inspect.Parameter], obj: Any, function: Any) -> list[tuple[inspect.Parameter, dict]]:
    """Each parameter with the globals its annotation is read in; `**options: Unpack[T]` becomes T's keys.

    `worker`'s functions take their options as a TypedDict, so its keys are the real option names.
    """
    scope = snapshot._scope(function or obj)
    found = []
    for parameter in parameters:
        unpacked = re.fullmatch(r"Unpack\[(\w+)\]", str(parameter.annotation))
        if parameter.kind is inspect.Parameter.VAR_KEYWORD and unpacked and unpacked.group(1) in scope:
            options = scope[unpacked.group(1)]
            options_scope = snapshot._scope(options)
            found += [(inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, annotation=annotation), options_scope)
                      for key, annotation in inspect.get_annotations(options).items()]
        else:
            found.append((parameter, scope))
    return found


def progress_callback(annotation: Any, scope: dict) -> bool:
    """Whether an annotation is a `progress(done, total)` callback, or None: `Callable[[A, B], R]` or `Callable[..., R]`.

    Args:
        annotation: the annotation as written, such as "Progress | None"; a bare alias is looked up.
        scope: the globals of the function's module, where an alias like `Progress` is defined.
    """
    kinds = [part for part in split_top_level(snapshot._annotation_text(annotation), "|") if part != "None"]
    if len(kinds) != 1:
        return False
    kind = kinds[0]
    if kind.isidentifier() and kind in scope:  # an alias: `Progress = Callable[..., Any]`
        kind = snapshot._annotation_text(scope[kind])
    kind = re.sub(r"^(typing\.|collections\.abc\.)", "", kind)
    if not kind.startswith("Callable[") or not kind.endswith("]"):
        return False
    arguments = split_top_level(kind[len("Callable["):-1], ",")[0]
    if arguments == "...":
        return True
    return arguments.startswith("[") and len([part for part in split_top_level(arguments[1:-1], ",") if part]) == 2


def split_top_level(text: str, separator: str) -> list[str]:
    """`text` split at `separator` outside brackets, each part stripped: "a[b, c], d" gives "a[b, c]" and "d"."""
    parts, depth, start = [], 0, 0
    for index, character in enumerate(text):
        depth += {"[": 1, "]": -1}.get(character, 0)
        if character == separator and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    return [*parts, text[start:].strip()]


def module_of(name: str, names: list[Any] = NAMES) -> str:
    """The module a name is in: the longest module among `names` that it starts with."""
    modules = {entry.name for entry in names if entry.kind == "module"}
    parts = name.split(".")
    return next(".".join(parts[:end]) for end in range(len(parts), 0, -1) if ".".join(parts[:end]) in modules)


def error_breaches(names: list[Any] = NAMES) -> set[str]:
    """S6: each module's errors share the module's own `Error`.

    Args:
        names: the snapshot's names; a test passes made-up ones.

    An error re-exported under another module's name (`flnd.source.LoginError` is
    `flnd.LoginError`) belongs to the module that defines it: it is a breach only when no
    module listing it has an `Error` it is under. A library's own error passed through by name
    (`browser.RawError` is Playwright's) is the library's, not the module's. `flnd.Stopped` is
    a `KeyboardInterrupt` on purpose (AGENTS.md: a Ctrl-C is never an `Error`), so a
    `KeyboardInterrupt` is no breach.
    """
    found: set[str] = set()
    by_name = {entry.name: entry for entry in names}
    errors: dict[str, list[Any]] = {}
    homes: dict[int, set[str]] = {}
    for entry in names:
        if (entry.kind == "error" and not from_a_library(entry.obj)
                and not issubclass(entry.obj, KeyboardInterrupt)):
            errors.setdefault(module_of(entry.name, names), []).append(entry)
            homes.setdefault(id(entry.obj), set()).add(module_of(entry.name, names))

    def under_error(error: Any, module: str) -> bool:
        base = by_name.get(f"{module}.Error")
        return base is not None and base.kind == "error" and issubclass(error, base.obj)

    for module in {entry.name for entry in names if entry.kind == "module"}:
        stray = sorted(entry.name for entry in errors.get(module, [])
                       if not any(under_error(entry.obj, home) for home in homes[id(entry.obj)]))
        if stray:
            found.add(f"{module}: errors outside {module}.Error: {', '.join(stray)}")
    return found


def private_imports(source: str) -> list[str]:
    """Each `flnd._x` module a file imports, in any of the three spellings of an import."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name.startswith("flnd._")]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.startswith("flnd._"):
                found.append(node.module)
            elif node.module == "flnd":
                found += [f"flnd.{alias.name}" for alias in node.names if alias.name.startswith("_")]
    return found


HISTORY_REF = re.compile(
    r"plan/"
    r"|(?i:\bphase[- ]?\d+\b)"
    r"|(?i:\bstep\s+\d+\b(?!:))"
    r"|(?i:\bfixes\s+#\d+\b)"
    r"|(?i:\b(?:as|was|as it was)\s+before\b|\bas it always did\b)"
    r"|(?i:(?<!\w)(?!info\.md\b)[A-Za-z0-9_-]+\.md\b)"
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
                        found.append(f"line {node.lineno}: from {mod} import {alias.name} as {alias.asname}")
    return found


def line_length_breaches(source: str, limit: int = 100) -> list[str]:
    """Lines longer than `limit` characters."""
    return [f"line {i}: {len(line)} chars" for i, line in enumerate(source.splitlines(), 1)
            if len(line) > limit]


def from_a_library(error: type) -> bool:
    """Whether an error class is an installed library's, not this project's or a test's."""
    file = getattr(sys.modules.get(error.__module__), "__file__", None) or ""
    return "site-packages" in Path(file).parts


# ---------------------------------------------------------------------------------------------
# Dead names: code nothing in flnd calls
# ---------------------------------------------------------------------------------------------

FLND = snapshot.PACKAGE_DIRS["flnd"]
FLNKIT = snapshot.PACKAGE_DIRS["flnkit"]
# The modules whose names must each be used: all of flnd/. A public name (in the API snapshot)
# is a script's to call, so it never counts as dead.
PRIVATE = ("**/*.py",)
# A string constant that reads as a name or a dotted path (`"chapter"`, `"flnd.sources.twt"`)
# counts as a use of its words: routes, categories and lazy imports name code in strings.
NAMEISH = re.compile(r"[\w.:]+")
WORD = re.compile(r"[A-Za-z_]\w*")
# Names something outside flnd calls by name, each with who calls it. A name is listed here only
# when nothing in flnd/ could use it instead. The list may only shrink: a listed name that
# becomes used, or goes, fails `test_no_stale_allowance`.
CALLED_FROM_OUTSIDE: dict[str, str] = {
    "flnkit/vault.py:Keychain": "tests/test_keychain.py exercises the OS keychain path",
    # An early part (PLAN.md 15): step 10's `feat:let config override a plugin's templates`
    # wires config.py in and removes these three.
    "src/engine/config.py:speed": "early part; the transfer reads rate and slowest from step 10",
    "src/engine/config.py:unset": "early part; `flnd config` and `Flnd.config` call it",
    "src/engine/config.py:user_file": "early part; the engine passes it to read and write",
    "src/engine/run.py:restoring": "registers an undo function to run on hard exit (Ctrl-C); "
                              "flnd's own tests use it",
    # Plugin API 2 is declared ahead of its wiring: the plugins call `numbered`, and the engine
    # and the test kit `_wall`, once `flnd.plugin` is public. Until then only its tests do.
    "src/plugin.py:numbered": "tests/test_plugin.py, until the plugins on API 2 use it",
    "src/plugin.py:_wall": "tests/test_plugin.py, until the engine and FakeCtx match walls",
    # The scheduler is built ahead of the code that calls it: the save (a transfer's backlog,
    # `slowest`, a segment's start) and the screen (the holds) take these when they are wired.
    "src/engine/jobs.py:Backlog": "the save's transfers, once wired; tests/test_jobs.py",
    "src/engine/jobs.py:Scheduler.holds": "the screen's State, once wired; tests/test_jobs.py",
    "src/engine/jobs.py:Segments.splittable": "the save, once wired; tests/test_jobs.py",
    "src/engine/jobs.py:Speed": "the save's `slowest`, once wired; tests/test_jobs.py",
    "src/engine/jobs.py:Speed.below": "the save's `slowest`, once wired; tests/test_jobs.py",
    "src/engine/save.py:Saver": "the save pipeline, built ahead of the scheduler that runs it; "
                                "tests/test_save.py drives it until `feat:save files as jobs`",
}


def registered(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a method is a source's route or category, which the framework finds by decorator."""
    for decorator in function.decorator_list:
        decorator = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(decorator, ast.Name) and decorator.id == "target":
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
    """Every name of flnd's private modules that nothing in flnd/ uses and that isn't public.

    The search is by word over all of flnd/, so a name is used when any module uses that word;
    tests don't count, since code only tests call is dead. A public name (in the API snapshot)
    is a script's to call, so it is never dead here.

    Returns:
        `<path>:<qualname>` per dead name, as `CALLED_FROM_OUTSIDE` spells it.
    """
    from test_docs import public_names

    public = public_names()
    used: set[str] = set()
    for path in [*FLND.rglob("*.py"), *FLNKIT.rglob("*.py")]:
        used |= references(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for path in sorted({path for pattern in PRIVATE for root in (FLND, FLNKIT)
                        for path in root.glob(pattern)}):
        relative = path.relative_to(snapshot.ROOT).as_posix()
        for name, qual in definitions(path.read_text(encoding="utf-8")):
            if name not in used and (relative, qual) not in public:
                found.add(f"{relative}:{qual}")
    return found


class NamesTest(unittest.TestCase):
    def test_no_new_breach(self):
        allowed = {breach for group in ALLOWED.values() for breach in group}
        new = sorted(breaches() - allowed)
        self.assertEqual(new, [], "these break AGENTS.md › API and naming; rename them, don't allow them")

    def test_no_stale_allowance(self):
        allowed = {breach for group in ALLOWED.values() for breach in group}
        stale = sorted(allowed - breaches())
        self.assertEqual(stale, [], "fixed: delete these from ALLOWED in tests/test_names.py")

    def test_flnd_errors_re_exports_exactly_flnkits_errors(self):
        from flnd import errors

        found = sorted(name for name in errors.__all__
                       if getattr(getattr(errors, name), "__module__", "") == "flnkit.errors")
        self.assertEqual(found, sorted(REEXPORTS))

    def test_allowances_are_grouped_by_their_module(self):
        for module, group in ALLOWED.items():
            for breach in group:
                found = module_of(breach.split(":", 1)[0])
                self.assertTrue(found == module or found.startswith(f"{module}."), breach)


class DeadNameTest(unittest.TestCase):
    """Every private name is used somewhere in flnd/, or is listed with who calls it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dead = dead_names()

    def test_no_dead_name(self):
        new = sorted(self.dead - set(CALLED_FROM_OUTSIDE))
        self.assertEqual(new, [], "nothing in flnd/ uses these: delete them, and the tests that "
                         "only exercise them (AGENTS.md › Writing: plain over clever)")

    def test_no_stale_allowance(self):
        stale = sorted(set(CALLED_FROM_OUTSIDE) - self.dead)
        self.assertEqual(stale, [], "used now, or gone: delete these from CALLED_FROM_OUTSIDE")

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


class LibraryErrorTest(unittest.TestCase):
    def test_a_librarys_error_is_not_a_modules_own(self):
        from playwright.sync_api import Error

        self.assertTrue(from_a_library(Error))
        self.assertFalse(from_a_library(snapshot.ApiName))
        self.assertFalse(from_a_library(LibraryErrorTest))


class PrivateImportTest(unittest.TestCase):
    def test_the_snapshot_imports_no_private_module_of_flnd(self):
        # flnd's own modules may import each other's private modules; the snapshot reads only
        # what a script can reach, so it may not.
        path = snapshot.ROOT / "tests" / "api_snapshot.py"
        self.assertEqual(private_imports(path.read_text(encoding="utf-8")), [])

    def test_every_spelling_of_a_private_import_is_found(self):
        source = ("import flnd._library\nfrom flnd._library import Flnd\n"
                  "from flnd import _library, ui\n")
        self.assertEqual(private_imports(source), ["flnd._library", "flnd._library", "flnd._library"])
        self.assertEqual(private_imports("from flnkit import net\nimport flnd\n"), [])


def _source_files() -> list[str]:
    return sorted(
        path.relative_to(snapshot.ROOT).as_posix()
        for pkg in snapshot.PROJECT_PACKAGES
        for path in snapshot.PACKAGE_DIRS[pkg].rglob("*.py")
    )


class ReadableTest(unittest.TestCase):
    def test_no_plan_or_history_references_in_code(self):
        for path in _source_files():
            with self.subTest(path):
                self.assertEqual(history_breaches((snapshot.ROOT / path).read_text(encoding="utf-8")), [])

    def test_no_flnd_import_aliases(self):
        for path in sorted([*FLND.rglob("*.py"), *FLNKIT.rglob("*.py")]):
            rel = path.relative_to(snapshot.ROOT).as_posix()
            with self.subTest(rel):
                self.assertEqual(flnd_import_aliases(path.read_text(encoding="utf-8")), [])

    def test_lines_are_at_most_100_characters(self):
        for path in _source_files():
            with self.subTest(path):
                self.assertEqual(line_length_breaches((snapshot.ROOT / path).read_text(encoding="utf-8")), [])

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


class ChecksTest(unittest.TestCase):
    """The checks themselves, on made-up names, so a check that silently passes everything fails here."""

    def name(self, kind: str, obj: Any, *, owner: Any = None, bound: bool = False, name: str = "flnd.x.f") -> Any:
        return snapshot.ApiName(name, kind, obj, owner, bound)

    def test_prefix(self):
        self.assertEqual(prefix_breaches(self.name("function", None, name="flnd.x.get_keys")),
                         {"flnd.x.get_keys: prefix get_"})
        self.assertEqual(prefix_breaches(self.name("function", None, name="flnd.x.target")), set())

    def test_positional_option(self):
        def fetch(url, timeout=30.0, *, attempts=3): ...

        self.assertEqual(option_breaches(self.name("function", fetch)), {"flnd.x.f: positional option timeout"})

    def test_first_parameter_may_have_a_default(self):
        def save(dest=None, *, mode=0o666): ...

        self.assertEqual(option_breaches(self.name("function", save)), set())

    def test_method_leaves_out_self(self):
        class Store:
            def keys(self, prefix=""): ...

        self.assertEqual(option_breaches(self.name("method", Store.keys, owner=Store, bound=True)), set())

    def test_a_stdlib_protocol_keeps_its_signature(self):
        class Store(dict):
            def get(self, key, default=None): ...      # dict.get's own signature

            def find(self, key, default=None): ...

        found = set()
        for member in (Store.get, Store.find):
            found |= option_breaches(self.name("method", member, owner=Store, bound=True,
                                               name=f"flnd.x.Store.{member.__name__}"))
        self.assertEqual(found, {"flnd.x.Store.find: positional option default"})

    def test_renamed_options(self):
        def build(entry, *, out="dist", dest_dir=None, preset="mid"): ...

        self.assertEqual(option_breaches(self.name("function", build)),
                         {"flnd.x.f: option out, not dest", "flnd.x.f: option dest_dir, not dest",
                          "flnd.x.f: option preset, not level"})

    def test_a_record_field_is_data_not_an_option(self):
        class Result:
            def __init__(self, code, out, *, dest_dir=None): ...

        self.assertEqual(option_breaches(self.name("class", Result)),
                         {"flnd.x.f: option dest_dir, not dest"})

    def test_progress_callback_is_recognised(self):
        scope = {"Progress": typing.Callable[..., Any]}
        self.assertTrue(progress_callback("Callable[[float, float | None], Any] | None", scope))
        self.assertTrue(progress_callback("Progress | None", scope))
        self.assertFalse(progress_callback("str | None", scope))
        self.assertFalse(progress_callback("Callable[[], int] | None", scope))

    def test_errors_share_their_modules_error(self):
        class Error(Exception): ...

        class Module(Error): ...       # flnd.x.Error

        class Kept(Module): ...

        class Stray(ValueError): ...

        names = [self.name("module", None, name="flnd.x"), self.name("error", Module, name="flnd.x.Error"),
                 self.name("error", Kept, name="flnd.x.KeptError"),
                 self.name("error", Stray, name="flnd.x.Inner.StrayError")]
        self.assertEqual(error_breaches(names), {"flnd.x: errors outside flnd.x.Error: flnd.x.Inner.StrayError"})

    def test_a_keyboard_interrupt_is_no_stray_error(self):
        # `flnd.Stopped` derives from KeyboardInterrupt so `except flnd.Error` never eats a Ctrl-C.
        class Error(Exception): ...

        class Stopped(KeyboardInterrupt): ...

        names = [self.name("module", None, name="flnd"), self.name("error", Error, name="flnd.Error"),
                 self.name("error", Stopped, name="flnd.Stopped")]
        self.assertEqual(error_breaches(names), set())

    def test_a_reexported_error_belongs_to_the_module_that_defines_it(self):
        # `flnd.source.LoginError` is `flnd.LoginError`: it belongs to the module defining it.
        class SourceError(Exception): ...

        class Kept(SourceError): ...

        class Stray(ValueError): ...

        names = [self.name("module", None, name="flnd"),
                 self.name("module", None, name="flnd.source"),
                 self.name("error", SourceError, name="flnd.source.Error"),
                 self.name("error", Kept, name="flnd.source.TargetError"),
                 self.name("error", Kept, name="flnd.TargetError"), self.name("error", Stray, name="flnd.StrayError")]
        self.assertEqual(error_breaches(names), {"flnd: errors outside flnd.Error: flnd.StrayError"})


if __name__ == "__main__":
    unittest.main()
