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
3. `flnd/cli.py` imports from flnd only `flnd`, `flnd.engine` and `flnd.sources`;
4. nothing imports `flnd.cli` but `flnd/__main__.py`, which is how `python -m flnd` starts it.

Two lists exempt today's code, and may only shrink, as test_names.py's `ALLOWED` does:
`BRIDGED`, the sources still on `run(session)` (step-6 bridge, each removed by its port), and
`CLI_UNTIL_STEP_7`, what cli.py imports until step 7 moves those decisions into the engine.
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLND = ROOT / "flnd"

# step-6 bridge: the sources that still import flnd.session; each port removes its own entry.
BRIDGED = {"fbk", "tik", "tlg"}
# What a bridged source may import besides rule 1's.
BRIDGE_IMPORTS = {"flnd.session"}
# What cli.py imports today that rule 3 doesn't allow; step 7 (the engine API) empties it.
CLI_UNTIL_STEP_7 = {"flnd.common", "flnd.common.auth", "flnd.common.compress", "flnd.session", "flnd.source",
                    "flnd.sync"}


def flnd_imports(path: Path) -> set[str]:
    """Every flnd module a file imports, anywhere in it: `from flnd import sync` counts as `flnd.sync`."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names if alias.name.split(".")[0] == "flnd"}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "flnd":
            found.add(node.module)
            # `from flnd import sync` and `from flnd.common import net` import modules by name.
            found |= {f"{node.module}.{alias.name}" for alias in node.names
                      if (FLND.parent / node.module.replace(".", "/") / f"{alias.name}.py").is_file()}
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
            allowed = BRIDGE_IMPORTS if path.stem in BRIDGED else set()
            with self.subTest(module=name):
                self.assertEqual(sorted(found for found in flnd_imports(path) - allowed if not source_allowed(found)), [])

    def test_flnd_source_imports_nothing_from_the_engine_side(self):
        self.assertEqual(sorted(flnd_imports(FLND / "source.py") - {"flnd.common.pace", "flnd.common"}), [])

    def test_the_cli_imports_the_engine_api_shared_and_the_sources(self):
        found = flnd_imports(FLND / "cli.py") - {"flnd", "flnd.engine", "flnd.sources"}
        self.assertEqual(sorted(found - CLI_UNTIL_STEP_7), [])

    def test_nothing_imports_the_cli_but_the_entry_point(self):
        for path in sorted(FLND.rglob("*.py")):
            if path.name == "__main__.py":
                continue
            with self.subTest(module=module_name(path)):
                self.assertNotIn("flnd.cli", flnd_imports(path))

    def test_the_exemptions_only_shrink(self):
        # A ported source drops out of BRIDGED: it then imports flnd.session no more.
        for code in sorted(BRIDGED):
            with self.subTest(source=code):
                self.assertIn("flnd.session", flnd_imports(FLND / "sources" / f"{code}.py"))
        found = flnd_imports(FLND / "cli.py")
        self.assertEqual(sorted(CLI_UNTIL_STEP_7 - found), [])


if __name__ == "__main__":
    unittest.main()
