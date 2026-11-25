"""The import rules of AGENTS.md › Layout: which of flnd's modules may import which.

Every file under `src/` (the package flnd) and `flnkit/` is read with `ast`, and every import
counts: at the top, inside a function and under `if TYPE_CHECKING:` alike, since each is a
dependency. Nothing is imported or run. Third-party packages are ruled on only where a rule names
them (`engine/events.py`, the plugins).

The rules:
1. `flnkit/*` imports only `flnkit/*`, the standard library and third-party packages. Nothing
   under `flnkit/` imports `flnd`. `src/errors.py` imports only `flnkit.errors`.
2. `src/ui/*` imports only `src/ui/*`, `flnkit`, `src/engine/events.py` and `src/errors.py`.
3. `src/engine/*` imports `src/engine/*`, `flnkit`, `src/ui/*`, `src/errors.py` and `src/plugin.py`;
   `src/engine/events.py` only `src/errors.py`, `src/plugin.py`, `flnkit` and `msgspec`.
4. `src/plugin.py` imports only `flnkit`, `src/errors.py` and the standard library;
   `src/testing.py` imports `flnd`, `src/plugin.py`, `src/engine/*` and `flnkit`;
   `src/_library.py` imports `src/engine/*`, `flnkit`, `src/errors.py` and `src/plugin.py`.
5. `src/plugins/*` imports from flnd only `flnd.plugin`, and a third-party SDK only inside a
   function; the table, `src/plugins/__init__.py`, imports none.
6. `src/cli.py` imports only `flnd`, `src/ui/*` and the table, `flnd.plugins`.
7. `src/__init__.py` imports nothing at import time beyond the standard library.
And nothing imports `flnd.cli` but `src/__main__.py`, which is how `python -m flnd` starts it.
What can't follow a rule yet is in `EXEMPT`, each entry with the step that removes it.
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The package flnd lives in src/; the standalone package flnkit beside it (AGENTS.md › Layout).
FLND = ROOT / "src"
FLNKIT = ROOT / "flnkit"

# Imports a rule forbids that stay for now: (importer, imported) -> the step that removes them.
# An importer or an import ending in ".*" covers every module under it.
EXEMPT: dict[tuple[str, str], int] = {}


# The two packages this project ships: flnd (the folder src/) and flnkit.
OWN = ("flnd", "flnkit")


def module_name(path: Path) -> str:
    """`src/plugins/ins.py` as `flnd.plugins.ins`; a package's `__init__.py` as the package."""
    parts = path.relative_to(ROOT).with_suffix("").parts
    if parts[0] == "src":
        parts = ("flnd", *parts[1:])
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def folder(dotted: str) -> Path:
    """The folder of a dotted module: `flnkit.net` is `flnkit/net`, `flnd.engine` `src/engine`."""
    head, *rest = dotted.split(".")
    return (FLND if head == "flnd" else FLNKIT).joinpath(*rest)


def _flnd_names(node: ast.AST) -> set[str]:
    """The flnd or flnkit modules an import node names: `from flnkit import net` is `flnkit.net`."""
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names if alias.name.split(".")[0] in OWN}
    if not (isinstance(node, ast.ImportFrom) and node.module and node.level == 0
            and node.module.split(".")[0] in OWN):
        return set()
    # `from flnd import errors` imports a module by name. Names are compared to the folder's
    # listing, not looked up as paths: on a case-insensitive disk, `from flnd import Engine`
    # would find src/engine/.
    package = folder(node.module)
    entries = {entry.name for entry in package.iterdir()} if package.is_dir() else set()

    def is_module(name: str) -> bool:
        return f"{name}.py" in entries or (name in entries and (package / name).is_dir())

    found = {f"{node.module}.{alias.name}" for alias in node.names if is_module(alias.name)}
    # The package itself counts only when a name in the import isn't a module of it.
    if not all(is_module(alias.name) for alias in node.names):
        found.add(node.module)
    return found


def flnd_imports(path: Path) -> set[str]:
    """Every flnd module a file imports, anywhere in it, as written."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        found |= _flnd_names(node)
    return found


def _top_level(tree: ast.Module) -> list[ast.stmt]:
    """The statements that run at import time: the module's own, without `if TYPE_CHECKING:`."""
    run: list[ast.stmt] = []
    pending = list(tree.body)
    while pending:
        node = pending.pop(0)
        if isinstance(node, ast.If):
            test = node.test
            checking = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
            pending[:0] = node.orelse if checking else [*node.body, *node.orelse]
        elif isinstance(node, (ast.Try, ast.With)):
            pending[:0] = list(ast.iter_child_nodes(node))
        else:
            run.append(node)
    return run


def import_time_packages(path: Path) -> set[str]:
    """The top-level package of every import that runs when `path` is imported."""
    found: set[str] = set()
    for node in _top_level(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found - {"__future__"}


def all_packages(path: Path) -> set[str]:
    """The top-level package of every import in `path`, lazy ones inside a function included."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found - {"__future__"}


def third_party(name: str) -> bool:
    return name.split(".")[0] not in sys.stdlib_module_names and name.split(".")[0] not in OWN


def under(name: str, *prefixes: str) -> bool:
    """Whether `name` is one of `prefixes` or a module under one."""
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


def allowed(importer: str, imported: str) -> bool:
    """Whether rules 1 to 6 let `importer` import `imported` (both read as API 2)."""
    if under(importer, "flnkit"):
        return under(imported, "flnkit")
    if importer == "flnd.errors":
        return imported == "flnkit.errors"
    if imported == "flnd.errors" and not under(importer, "flnd.plugins"):
        return importer != "flnd.cli"
    if under(importer, "flnd.ui"):
        return under(imported, "flnd.ui", "flnkit") or imported == "flnd.engine.events"
    if importer == "flnd.engine.events":
        return imported == "flnd.plugin" or under(imported, "flnkit")
    if importer == "flnd.engine.plugins":
        # The loader reads the table of the built-ins, without importing them.
        return (under(imported, "flnd.engine", "flnkit", "flnd.ui")
                or imported in ("flnd.plugin", "flnd.plugins"))
    if under(importer, "flnd.engine"):
        return under(imported, "flnd.engine", "flnkit", "flnd.ui") or imported == "flnd.plugin"
    if importer == "flnd.plugin":
        return under(imported, "flnkit")
    if importer == "flnd.testing":
        return imported in ("flnd", "flnd.plugin") or under(imported, "flnd.engine", "flnkit")
    if importer == "flnd._library":
        return under(imported, "flnd.engine", "flnkit") or imported == "flnd.plugin"
    if importer == "flnd.plugins":
        # The table of the built-ins is plain data.
        return False
    if under(importer, "flnd.plugins"):
        return imported == "flnd.plugin"
    if importer == "flnd.cli":
        return imported in ("flnd", "flnd.plugins") or under(imported, "flnd.ui")
    if importer == "flnd.__main__":
        return imported == "flnd.cli"
    raise AssertionError(f"{importer} is in no layer of AGENTS.md › Layout")


def covers(pattern: str, name: str) -> bool:
    """Whether an `EXEMPT` side names `name`: itself, or a module under a `.*` pattern."""
    return name == pattern or (pattern.endswith(".*") and under(name, pattern[:-2]))


def exempt(importer: str, imported: str) -> bool:
    """Whether `EXEMPT` keeps this import for now."""
    return any(covers(who, importer) and covers(what, imported) for who, what in EXEMPT)


def breaches(path: Path) -> list[str]:
    """The imports of `path` that rules 1 to 6 forbid and `EXEMPT` doesn't keep."""
    importer = module_name(path)
    found = []
    for name in sorted(flnd_imports(path)):
        imported = name
        if imported != importer and not allowed(importer, imported) \
                and not exempt(importer, imported):
            found.append(name)
    return found


def _files() -> list[Path]:
    """Every module of the package but `src/__init__.py`, whose rule is rule 7."""
    return [path for path in sorted([*FLND.rglob("*.py"), *FLNKIT.rglob("*.py")])
            if path != FLND / "__init__.py"]


class LayersTest(unittest.TestCase):
    def test_every_module_imports_only_what_its_layer_may(self):
        for path in _files():
            with self.subTest(module=module_name(path)):
                self.assertEqual(breaches(path), [])

    def test_engine_events_uses_only_msgspec_from_outside(self):
        # Lazy imports inside a function count too: they run later, but they are still deps.
        found = {name for name in all_packages(FLND / "engine" / "events.py")
                 if third_party(name)}
        self.assertLessEqual(found, {"msgspec"})

    def test_the_plugin_api_imports_only_the_stdlib_kit_and_errors(self):
        # Rule 4 in full: `src/plugin.py` takes no third-party package, at import time or
        # inside a function.
        self.assertEqual(sorted(name for name in all_packages(FLND / "plugin.py")
                                if third_party(name)), [])

    def test_a_plugin_imports_its_sdk_only_inside_a_function(self):
        for path in sorted((FLND / "plugins").glob("*.py")):
            with self.subTest(module=module_name(path)):
                self.assertEqual(sorted(name for name in import_time_packages(path)
                                        if third_party(name)), [])

    def test_the_package_imports_only_the_standard_library_at_import_time(self):
        found = import_time_packages(FLND / "__init__.py")
        self.assertEqual(sorted(name for name in found if name not in sys.stdlib_module_names),
                         [])

    def test_nothing_imports_the_cli_but_the_entry_point(self):
        for path in sorted([*FLND.rglob("*.py"), *FLNKIT.rglob("*.py")]):
            if path.name == "__main__.py":
                continue
            with self.subTest(module=module_name(path)):
                self.assertNotIn("flnd.cli", flnd_imports(path))

    def test_nothing_under_flnkit_imports_flnd(self):
        # Rule 1 in full: at import time, inside a function and under TYPE_CHECKING alike.
        for path in sorted(FLNKIT.rglob("*.py")):
            with self.subTest(module=module_name(path)):
                self.assertNotIn("flnd", all_packages(path))

    def test_every_exemption_is_still_needed(self):
        # An exemption whose import is gone is dropped, so the list only shrinks.
        used = set()
        for path in _files():
            importer = module_name(path)
            for name in flnd_imports(path):
                imported = name
                if imported != importer and not allowed(importer, imported):
                    used |= {(who, what) for who, what in EXEMPT
                             if covers(who, importer) and covers(what, imported)}
        self.assertEqual(sorted(set(EXEMPT) - used), [])

    def test_the_rules_see_imports_of_names_and_of_modules(self):
        # Guards the guard: `from flnd import Engine` is the package, `from flnd import errors`
        # a module, and `from flnkit import net` the module flnkit.net.
        self.assertIn("flnd.engine.crawl", flnd_imports(FLND / "engine" / "_engine.py"))
        self.assertIn("flnkit.net", flnd_imports(FLND / "engine" / "ctx.py"))
        self.assertIn("flnd", flnd_imports(FLND / "cli.py"))
        self.assertFalse(allowed("flnkit.net", "flnd.ui.log"))
        self.assertFalse(allowed("flnkit.net", "flnd.errors"))
        self.assertFalse(allowed("flnd.errors", "flnkit.net"))
        self.assertFalse(allowed("flnd.ui.log", "flnd.engine.run"))
        self.assertFalse(allowed("flnd.plugins.ins", "flnkit.net"))


if __name__ == "__main__":
    unittest.main()
