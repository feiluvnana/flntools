"""The error tree of AGENTS.md: `shared.Error`, its three kinds, and each module's `Error`.

It pins each error's place in the tree, and also the builtin base it had before the tree, such as
ValueError, so every `except` written before still catches what it caught. Nothing is faked.
"""

import unittest

import shared
from shared import archive, browser, bundler, cli, clock, cookies, crypto, fs, http, prompt, selector, sh, store, text, ui, worker

MODULES = {"archive": archive, "browser": browser, "bundler": bundler, "cli": cli, "clock": clock,
           "cookies": cookies, "crypto": crypto, "fs": fs, "http": http,
           "prompt": prompt, "selector": selector, "sh": sh, "store": store, "text": text, "ui": ui,
           "worker": worker}

# Each error that existed before the tree: its module, the builtin base it had then, which it keeps,
# and the kind of problem it now also is (None for none of the three).
ERRORS = [
    (archive.UnsafeError, "archive", ValueError, shared.UnsafeError),
    (archive.BrokenError, "archive", ValueError, shared.BrokenError),
    (archive.PasswordError, "archive", ValueError, shared.RefusedError),
    (sh.UnsafeError, "sh", ValueError, shared.UnsafeError),
    (cookies.Error, "cookies", Exception, None),
    (cookies.UnsafeError, "cookies", ValueError, shared.UnsafeError),
    (crypto.Error, "crypto", ValueError, None),
    (store.ClosedError, "store", RuntimeError, None),
    (selector.BrokenError, "selector", ValueError, shared.BrokenError),
    (cli.UsageError, "cli", ValueError, None),
    (prompt.Error, "prompt", Exception, None),
    (worker.ClosedError, "worker", RuntimeError, None),
]
KINDS = (shared.UnsafeError, shared.BrokenError, shared.RefusedError)


class TreeTest(unittest.TestCase):
    def test_the_three_kinds(self):
        # UnsafeError and BrokenError are about bad input, so they are ValueErrors; a refusal isn't.
        for kind in KINDS:
            self.assertTrue(issubclass(kind, shared.Error))
        self.assertTrue(issubclass(shared.UnsafeError, ValueError))
        self.assertTrue(issubclass(shared.BrokenError, ValueError))
        self.assertFalse(issubclass(shared.RefusedError, ValueError))
        self.assertTrue(issubclass(shared.Error, Exception))

    def test_every_module_has_its_own_error(self):
        for name, module in MODULES.items():
            with self.subTest(name):
                self.assertTrue(issubclass(module.Error, shared.Error))
                self.assertIsNot(module.Error, shared.Error)
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
                self.assertTrue(issubclass(error, shared.MissingError))
                self.assertTrue(issubclass(error, module.Error))
                self.assertTrue(issubclass(error, RuntimeError))
        self.assertTrue(issubclass(shared.MissingError, shared.Error))

    def test_siblings_stay_apart(self):
        # The tree adds bases, never links two module errors: a broken archive isn't an archive.UnsafeError.
        self.assertFalse(issubclass(archive.BrokenError, archive.UnsafeError))
        self.assertFalse(issubclass(sh.UnsafeError, archive.UnsafeError))
        self.assertFalse(issubclass(sh.RunError, sh.UnsafeError))

    def test_a_module_error_is_caught_at_every_level(self):
        for level in (archive.UnsafeError, archive.Error, shared.UnsafeError, shared.Error, ValueError):
            with self.subTest(level.__qualname__), self.assertRaises(level):
                raise archive.UnsafeError("unsafe path")


if __name__ == "__main__":
    unittest.main()
