"""The error tree of plan/phase-6/redesign.md C4: `shared.Error`, its three kinds, and each module's `Error`.

It pins each error's place in the tree, and also the builtin base it had before the tree, such as
ValueError, so every `except` written before still catches what it caught. Nothing is faked.
"""

import unittest

import shared
from shared import archive, browser, bundler, cli, cookies, crypto, fmt, kv, log, media, prompt, selector, style, theme, worker

MODULES = {"archive": archive, "browser": browser, "bundler": bundler, "cli": cli, "cookies": cookies,
           "crypto": crypto, "fmt": fmt, "kv": kv, "log": log, "media": media, "prompt": prompt,
           "selector": selector, "style": style, "theme": theme, "worker": worker}

# Each error that existed before the tree: its module, the builtin base it had then, which it keeps,
# and the kind of problem it now also is (None for none of the three).
ERRORS = [
    (archive.UnsafeError, "archive", ValueError, shared.UnsafeError),
    (archive.BombError, "archive", ValueError, shared.UnsafeError),
    (archive.ConflictError, "archive", ValueError, None),
    (archive.PasswordError, "archive", ValueError, shared.RefusedError),
    (media.BrokenError, "media", ValueError, shared.BrokenError),
    (media.UnsafeError, "media", ValueError, shared.UnsafeError),
    (cookies.Error, "cookies", ValueError, None),       # CookieError, renamed to its base in step 4
    (cookies.UnsafeError, "cookies", ValueError, shared.UnsafeError),  # new in step 4: a file over 16 MB
    (crypto.Error, "crypto", ValueError, None),       # crypto's one error, renamed to its base in step 4
    (kv.ClosedError, "kv", RuntimeError, None),      # kv raised these builtins until step 4
    (kv.UnstorableError, "kv", TypeError, None),
    (selector.BrokenError, "selector", ValueError, shared.BrokenError),  # json's ValueError until step 4
    (selector.UnsafeError, "selector", ValueError, shared.UnsafeError),  # new in step 4; a ValueError as its kind is
    (cli.UsageError, "cli", Exception, None),
    (prompt.PromptError, "prompt", Exception, None),
    (worker.TaskError, "worker", Exception, None),
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

    def test_siblings_stay_apart(self):
        # The tree adds bases, never links two module errors: a bomb isn't an archive.UnsafeError, as before.
        self.assertFalse(issubclass(archive.BombError, archive.UnsafeError))
        self.assertFalse(issubclass(media.UnsafeError, archive.UnsafeError))
        self.assertFalse(issubclass(media.BrokenError, media.UnsafeError))

    def test_a_module_error_is_caught_at_every_level(self):
        for level in (archive.BombError, archive.Error, shared.UnsafeError, shared.Error, ValueError):
            with self.subTest(level.__qualname__), self.assertRaises(level):
                raise archive.BombError("too big")


if __name__ == "__main__":
    unittest.main()
