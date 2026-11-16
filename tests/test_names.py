"""The naming conventions of AGENTS.md › API and naming, checked over the API snapshot.

The names are the ones `make api` writes to tests/data/api.txt (test_api.py keeps that
file current), read from the same `api_names()` with their objects, so a check can look at a real
signature. Nothing is faked: the names come from importing `flnd`.

Today's code breaks some conventions. Every such breach is in `ALLOWED`, by module, and nowhere else:
a new breach fails `test_no_new_breach`, and a fixed one still listed fails `test_no_stale_allowance`,
so the list can only shrink.
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
# §2.2: option names the vocabulary has another word for.
RENAMED = {"out": "dest", "dest_dir": "dest", "preset": "level"}
POSITIONAL = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)

# Every breach today's code has, by the module it is in. Each module's
# step-4 commit renames its names and removes its own entries here, with the api.txt diff that shows
# the renames. Nothing else is exempt: a new name that breaks a convention fails.
ALLOWED: dict[str, tuple[str, ...]] = {}


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
    """S3 and §2.2 over one function, method or class: in its signature and in each of its overloads."""
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


PLAN_REF = re.compile(
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
            match = PLAN_REF.search(line)
            if match:
                found.append(f"line {token.start[0] + offset}: {match.group(0)}")
    return found


# The aliases the fold keeps (`from flnd import _http as http`): each old `shared` name, so that the
# code lines using it didn't change. Each pair is (the alias, the private module it names). A row
# that renames the uses removes its pairs here, so the list can only shrink. AGENTS.md says no
# aliases; this is the one exception, and it is temporary.
TRANSITIONAL_ALIASES = {
    ("browser", "_browser"), ("cli", "_cli"),
    ("clock", "_time"), ("cookies", "_cookies"), ("crypto", "_vault"), ("fs", "_files"),
    ("http", "_http"), ("prompt", "_prompt"), ("selector", "_html"), ("sh", "_tools"),
    ("store", "_db"), ("text", "_text"), ("worker", "_loop"),
}


def flnd_import_aliases(source: str) -> list[str]:
    """Import aliases (`as`) of `flnd` modules or symbols, except the transitional ones."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and (alias.name == "flnd" or alias.name.startswith("flnd.")):
                    found.append(f"line {node.lineno}: import {alias.name} as {alias.asname}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level > 0 or mod == "flnd" or mod.startswith("flnd."):
                for alias in node.names:
                    if alias.asname and (alias.asname, alias.name) not in TRANSITIONAL_ALIASES:
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


class NamesTest(unittest.TestCase):
    def test_no_new_breach(self):
        allowed = {breach for group in ALLOWED.values() for breach in group}
        new = sorted(breaches() - allowed)
        self.assertEqual(new, [], "these break AGENTS.md › API and naming; rename them, don't allow them")

    def test_no_stale_allowance(self):
        allowed = {breach for group in ALLOWED.values() for breach in group}
        stale = sorted(allowed - breaches())
        self.assertEqual(stale, [], "fixed: delete these from ALLOWED in tests/test_names.py")

    def test_allowances_are_grouped_by_their_module(self):
        for module, group in ALLOWED.items():
            for breach in group:
                found = module_of(breach.split(":", 1)[0])
                self.assertTrue(found == module or found.startswith(f"{module}."), breach)


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
        source = ("import flnd._http\nfrom flnd._browser import Page\n"
                  "from flnd import _loop, ui\n")
        self.assertEqual(private_imports(source), ["flnd._http", "flnd._browser", "flnd._loop"])
        self.assertEqual(private_imports("from flnd import browser\nimport flnd\n"), [])


def _source_files() -> list[str]:
    return sorted(
        path.relative_to(snapshot.ROOT).as_posix()
        for pkg in snapshot.PROJECT_PACKAGES
        for path in (snapshot.ROOT / pkg).rglob("*.py")
    )


class ReadableTest(unittest.TestCase):
    def test_no_plan_or_history_references_in_code(self):
        for path in _source_files():
            with self.subTest(path):
                self.assertEqual(history_breaches((snapshot.ROOT / path).read_text(encoding="utf-8")), [])

    def test_no_flnd_import_aliases(self):
        for path in sorted((snapshot.ROOT / "flnd").rglob("*.py")):
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
