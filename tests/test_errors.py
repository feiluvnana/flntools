"""The error tree of AGENTS.md: `_base.Error`, its three kinds, and each module's `Error`.

It pins each error's place in the tree, and also the builtin base it had before the tree, such as
ValueError, so every `except` written before still catches what it caught. Nothing is faked.
"""

import unittest

from flnd import _base
from flnd import _unpack as archive, _browser as browser, _bundler as bundler, _cli as cli, _time as clock, _cookies as cookies, _vault as crypto, _files as fs, _http as http, _prompt as prompt, _html as selector, _tools as sh, _db as store, _text as text, _loop as worker
from flnd._ui import ui

MODULES = {"archive": archive, "browser": browser, "bundler": bundler, "cli": cli, "clock": clock,
           "cookies": cookies, "crypto": crypto, "fs": fs, "http": http,
           "prompt": prompt, "selector": selector, "sh": sh, "store": store, "text": text, "ui": ui,
           "worker": worker}

# Each error that existed before the tree: its module, the builtin base it had then, which it keeps,
# and the kind of problem it now also is (None for none of the three).
ERRORS = [
    (archive.UnsafeError, "archive", ValueError, _base.UnsafeError),
    (archive.BrokenError, "archive", ValueError, _base.BrokenError),
    (archive.PasswordError, "archive", ValueError, _base.RefusedError),
    (sh.UnsafeError, "sh", ValueError, _base.UnsafeError),
    (cookies.Error, "cookies", Exception, None),
    (cookies.UnsafeError, "cookies", ValueError, _base.UnsafeError),
    (crypto.Error, "crypto", ValueError, None),
    (store.ClosedError, "store", RuntimeError, None),
    (selector.BrokenError, "selector", ValueError, _base.BrokenError),
    (cli.UsageError, "cli", ValueError, None),
    (prompt.Error, "prompt", Exception, None),
    (worker.ClosedError, "worker", RuntimeError, None),
]
KINDS = (_base.UnsafeError, _base.BrokenError, _base.RefusedError)


class TreeTest(unittest.TestCase):
    def test_the_three_kinds(self):
        # UnsafeError and BrokenError are about bad input, so they are ValueErrors; a refusal isn't.
        for kind in KINDS:
            self.assertTrue(issubclass(kind, _base.Error))
        self.assertTrue(issubclass(_base.UnsafeError, ValueError))
        self.assertTrue(issubclass(_base.BrokenError, ValueError))
        self.assertFalse(issubclass(_base.RefusedError, ValueError))
        self.assertTrue(issubclass(_base.Error, Exception))

    def test_every_module_has_its_own_error(self):
        for name, module in MODULES.items():
            with self.subTest(name):
                self.assertTrue(issubclass(module.Error, _base.Error))
                self.assertIsNot(module.Error, _base.Error)
        self.assertEqual(len({module.Error for module in MODULES.values()}), len(MODULES))

    def test_each_error_keeps_its_old_base_and_joins_the_tree(self):
        for error, module, old_base, kind in ERRORS:
            with self.subTest(error.__qualname__, module=module):
                self.assertTrue(issubclass(error, old_base))
                self.assertTrue(issubclass(error, MODULES[module].Error))
                for other in KINDS:
                    self.assertEqual(issubclass(error, other), other is kind, other.__name__)

    def test_no_error_gained_a_builtin_base(self):
        # A new builtin base would make an old `except ValueError` or `except RuntimeError` catch more.
        for error, module, old_base, _ in ERRORS:
            with self.subTest(error.__qualname__, module=module):
                for builtin in (ValueError, RuntimeError, TypeError):
                    self.assertEqual(issubclass(error, builtin), old_base is builtin, builtin.__name__)

    def test_a_missing_tool_is_caught_by_name_and_by_its_old_builtin(self):
        for error, module in ((sh.MissingError, sh), (archive.MissingError, archive),
                              (browser.MissingError, browser), (cookies.MissingError, cookies)):
            with self.subTest(error.__qualname__):
                self.assertTrue(issubclass(error, _base.MissingError))
                self.assertTrue(issubclass(error, module.Error))
                self.assertTrue(issubclass(error, RuntimeError))
        self.assertTrue(issubclass(_base.MissingError, _base.Error))

    def test_siblings_stay_apart(self):
        # The tree adds bases, never links two module errors: a broken archive isn't an archive.UnsafeError.
        self.assertFalse(issubclass(archive.BrokenError, archive.UnsafeError))
        self.assertFalse(issubclass(sh.UnsafeError, archive.UnsafeError))
        self.assertFalse(issubclass(sh.RunError, sh.UnsafeError))

    def test_a_module_error_is_caught_at_every_level(self):
        for level in (archive.UnsafeError, archive.Error, _base.UnsafeError, _base.Error, ValueError):
            with self.subTest(level.__qualname__), self.assertRaises(level):
                raise archive.UnsafeError("unsafe path")


if __name__ == "__main__":
    unittest.main()
