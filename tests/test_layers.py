"""The dependency rules of plan/phase-6/redesign.md, B4: which of flnd's modules may import which.

Read from each file's source with `ast`, every import counted: at the top, inside a function and
under `if TYPE_CHECKING:` alike, since each is a dependency. Only imports of flnd's own modules are
ruled on here; what a source may take from third-party libraries is tests/test_sources.py's guard.
Nothing is imported or run.

The rules:
1. a source (and `flnd.sources.common`) imports from flnd only `flnd.source`, the
   `flnd.sources.common` package and `flnd` itself (its version);
2. `flnd.source` imports nothing from the engine side: from flnd, only `flnd.common.pace`, whose
   `Pace` it re-exports;
3. `flnd/cli.py` imports from flnd only `flnd` (the engine API) and `flnd.sources` (for help and
   login's list): every decision is the engine's, so the CLI only parses, asks and prints;
4. nothing imports `flnd.cli` but `flnd/__main__.py`, which is how `python -m flnd` starts it.

Nothing is exempt: step 7 moved the CLI's decisions into the engine, which emptied the list of
what cli.py imported beyond rule 3.
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLND = ROOT / "flnd"

# Rule 3: everything cli.py may import from flnd.
CLI_ALLOWED = {"flnd", "flnd.sources"}


def flnd_imports(path: Path) -> set[str]:
    """Every flnd module a file imports, anywhere in it: `from flnd import job` counts as `flnd.job`."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names if alias.name.split(".")[0] == "flnd"}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "flnd":
            found.add(node.module)
            # `from flnd import job` and `from flnd.common import net` import modules by name. Names
            # are compared to the folder's listing, not looked up as paths: on macOS's and Windows'
            # case-insensitive disks, `from flnd import Engine` would find flnd/engine.py.
            package = FLND.parent / node.module.replace(".", "/")
            modules = {entry.name for entry in package.iterdir()} if package.is_dir() else set()
            found |= {f"{node.module}.{alias.name}" for alias in node.names if f"{alias.name}.py" in modules}
    return found


def module_name(path: Path) -> str:
    """`flnd/sources/ins.py` as `flnd.sources.ins`; a package's `__init__.py` as the package."""
    parts = path.relative_to(ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def source_allowed(name: str) -> bool:
    """Rule 1: what a source may import from flnd."""
    return name in ("flnd", "flnd.source") or name == "flnd.sources.common" or name.startswith("flnd.sources.common.")


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
        self.assertEqual(sorted(flnd_imports(FLND / "source.py") - {"flnd.common.pace", "flnd.common"}), [])

    def test_the_cli_imports_the_engine_api_shared_and_the_sources(self):
        self.assertEqual(sorted(flnd_imports(FLND / "cli.py") - CLI_ALLOWED), [])

    def test_nothing_imports_the_cli_but_the_entry_point(self):
        for path in sorted(FLND.rglob("*.py")):
            if path.name == "__main__.py":
                continue
            with self.subTest(module=module_name(path)):
                self.assertNotIn("flnd.cli", flnd_imports(path))

    def test_the_rules_see_imports_of_names_and_of_modules(self):
        # Guards the guard: `from flnd import Engine` is the package, `from flnd import job` a module.
        found = flnd_imports(FLND / "engine.py")
        self.assertIn("flnd.job", found)
        self.assertIn("flnd", flnd_imports(FLND / "cli.py"))


if __name__ == "__main__":
    unittest.main()
