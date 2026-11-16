"""The dependency rules of AGENTS.md › Layout: which of flnd's modules may import which.

Read from each file's source with `ast`, every import counted: at the top, inside a function and
under `if TYPE_CHECKING:` alike, since each is a dependency. Only imports of flnd's own modules are
ruled on here; what a source may take from third-party libraries is tests/test_sources.py's guard.
Nothing is imported or run.

The rules:
1. a source (and `flnd.sources.common`) imports from flnd only `flnd.source`, the
   `flnd.sources.common` package, `flnd` itself (its version) and the toolkit: the private
   `flnd._x` modules that used to be the `shared` package;
2. `flnd.source` imports nothing from flnd but the toolkit: it is what sources see, not the engine;
3. `flnd/cli.py` imports from flnd only `flnd` (the library), `flnd.sources._manifest` (the
   sources as data, for help) and the toolkit (`flnd._prompt`, `flnd._ui`, `flnd._events` for
   JSON): every decision is the library's, so the CLI only parses, asks and prints;
4. nothing imports `flnd.cli` but `flnd/__main__.py`, which is how `python -m flnd` starts it;
5. a library module (`LIBRARIES` below: HTTP, browser, files, archives, tools, cookies, vault,
   store, HTML, text, time, prompts, progress, media) imports from flnd only other library modules
   and `flnd.errors`. flnd's own code uses the libraries; a library never reaches back, so it stays
   readable alone and could leave flnd as a package of its own.

Nothing is exempt: step 7 moved the CLI's decisions into the engine, which emptied the list of
what cli.py imported beyond rule 3.
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLND = ROOT / "flnd"

# Rule 3: everything cli.py may import from flnd, besides the toolkit.
CLI_ALLOWED = {"flnd", "flnd.sources._manifest"}


def toolkit(name: str) -> bool:
    """Whether a module is part of the toolkit: a private `flnd._x` module, which the sources may use.

    The toolkit was the `shared` package before the fold. Until the source API of the rebuild
    replaces it, sources and the CLI import these private modules directly; the rule is only that
    they stay private, so the engine (`flnd.job`, `flnd.site`, ...) is still off limits.
    `flnd.errors`, the error hierarchy, counts too: it imports nothing of flnd, and every layer
    raises its classes.
    """
    return name.startswith("flnd._") or name == "flnd.errors"


# Rule 5: the library modules. Each does a general job (HTTP, the browser, files, archives, the
# outside tools, cookies, the vault, the SQLite store, HTML, text, time, prompts, progress, media),
# so it stays its own module that another program could use, and could later leave flnd as a
# package of its own. A name may be listed before its row creates the file.
LIBRARIES = {
    "_net", "_browser", "_loop", "_files", "_unpack", "_tools", "_cookies", "_vault",
    "_db", "_html", "_text", "_time", "_prompt", "_ui", "_media", "_shrink",
}


def library(name: str) -> bool:
    """Whether a module is a library module (rule 5), or `flnd.errors`, which every layer raises.

    `from flnd import _files` is found as `flnd._files`; the package `flnd` itself is not a library,
    since importing from it reaches the public API and so the engine side.
    """
    return name == "flnd.errors" or (name.startswith("flnd.") and name[len("flnd."):] in LIBRARIES)


def flnd_imports(path: Path) -> set[str]:
    """Every flnd module a file imports, anywhere in it: `from flnd import job` counts as `flnd.job`."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names if alias.name.split(".")[0] == "flnd"}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "flnd":
            # `from flnd import job` and `from flnd.common import net` import modules by name. Names
            # are compared to the folder's listing, not looked up as paths: on macOS's and Windows'
            # case-insensitive disks, `from flnd import Engine` would find flnd/engine.py.
            package = FLND.parent / node.module.replace(".", "/")
            modules = {entry.name for entry in package.iterdir()} if package.is_dir() else set()
            # The package itself counts only when a name in the import isn't a module of it.
            if not all(f"{alias.name}.py" in modules for alias in node.names):
                found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names if f"{alias.name}.py" in modules}
    return found


def module_name(path: Path) -> str:
    """`flnd/sources/ins.py` as `flnd.sources.ins`; a package's `__init__.py` as the package."""
    parts = path.relative_to(ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def source_allowed(name: str) -> bool:
    """Rule 1: what a source may import from flnd."""
    return name in ("flnd", "flnd.source") or name == "flnd.sources.common" or name.startswith("flnd.sources.common.") \
        or toolkit(name)


class LayersTest(unittest.TestCase):
    def test_a_source_imports_only_flnd_source_and_the_common_package(self):
        files = sorted((FLND / "sources").rglob("*.py"))
        self.assertTrue(files)
        for path in files:
            name = module_name(path)
            if name == "flnd.sources":
                # The registry imports every source to list them; it isn't one.
                continue
            with self.subTest(module=name):
                self.assertEqual(sorted(found for found in flnd_imports(path) if not source_allowed(found)), [])

    def test_flnd_source_imports_nothing_from_the_engine_side(self):
        self.assertEqual(sorted(found for found in flnd_imports(FLND / "source.py") if not toolkit(found)), [])

    def test_the_cli_imports_the_library_the_toolkit_and_the_manifest(self):
        self.assertEqual(sorted(found for found in flnd_imports(FLND / "cli.py")
                                if found not in CLI_ALLOWED and not toolkit(found)), [])

    def test_nothing_imports_the_cli_but_the_entry_point(self):
        for path in sorted(FLND.rglob("*.py")):
            if path.name == "__main__.py":
                continue
            with self.subTest(module=module_name(path)):
                self.assertNotIn("flnd.cli", flnd_imports(path))

    def test_a_library_module_imports_only_libraries_and_the_errors(self):
        # Rule 5: a library module could be split out of flnd, so it never reaches flnd's own code.
        present = sorted(name for name in LIBRARIES if (FLND / f"{name}.py").exists())
        self.assertIn("_files", present)
        for name in present:
            with self.subTest(module=f"flnd.{name}"):
                self.assertEqual(sorted(found for found in flnd_imports(FLND / f"{name}.py")
                                        if not library(found)), [])

    def test_the_rules_see_imports_of_names_and_of_modules(self):
        # Guards the guard: `from flnd import Engine` is the package, `from flnd import job` a module.
        found = flnd_imports(FLND / "engine.py")
        self.assertIn("flnd.job", found)
        self.assertIn("flnd", flnd_imports(FLND / "cli.py"))


if __name__ == "__main__":
    unittest.main()
