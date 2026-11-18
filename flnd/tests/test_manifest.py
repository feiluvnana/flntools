"""The source manifest (`src/sources/_manifest.py`) and the lazy registry built on it.

The manifest is generated (`make manifest`, tests/source_manifest.py); these tests fail when it
no longer matches the sources, and check that `--help` and target resolution import no more
sources than they need.
"""

import subprocess
import sys
import unittest
import unittest.mock

import source_manifest

from flnd import sources
from flnd.sources import _manifest


def loaded_after(code: str) -> list[str]:
    """The sources a fresh interpreter has imported after running `code`."""
    shown = (f"import sys\n{code}\n"
             "print(sorted(name for name in sys.modules if name.startswith('flnd.sources.')\n"
             "             and name.count('.') == 2\n"
             "             and not name.endswith(('_manifest', '.common'))))")
    result = subprocess.run([sys.executable, "-c", shown], capture_output=True, text=True,
                            check=True)
    return eval(result.stdout.strip().splitlines()[-1])  # a list of names, printed by us


class ManifestTest(unittest.TestCase):
    def test_the_manifest_matches_the_sources(self):
        text = source_manifest.MANIFEST.read_text(encoding="utf-8")
        self.assertEqual(text, source_manifest.render(),
                         "src/sources/_manifest.py is stale: run make manifest")

    def test_the_registry_lists_the_built_in_sources_in_order(self):
        codes = [getattr(__import__(module, fromlist=[name]), name).code
                 for module, name in sources.BUILT_IN]
        self.assertEqual(list(_manifest.SOURCES), codes)
        self.assertEqual(list(sources.SOURCES), codes)

    def test_a_source_is_imported_when_it_is_looked_up(self):
        self.assertEqual(loaded_after("from flnd.sources import SOURCES\nlist(SOURCES)"), [])
        self.assertEqual(loaded_after("from flnd.sources import SOURCES\nSOURCES['ins']"),
                         ["flnd.sources.ins"])

    def test_resolving_a_target_imports_only_its_source(self):
        code = ("import flnd\n"
                "with flnd.open('example-home') as fl:\n"
                "    fl._resolve(['ins:example_user', 'https://www.tiktok.com/@example_user'])")
        self.assertEqual(loaded_after(code), ["flnd.sources.ins", "flnd.sources.tik"])

    def test_a_registry_can_be_patched_like_a_dict(self):
        with unittest.mock.patch.dict(sources.SOURCES, {"exm": object}):
            self.assertIn("exm", sources.SOURCES)
            self.assertEqual(list(sources.SOURCES)[-1], "exm")
        self.assertNotIn("exm", sources.SOURCES)
        self.assertEqual(len(sources.SOURCES), len(_manifest.SOURCES))


if __name__ == "__main__":
    unittest.main()
