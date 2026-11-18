"""A coroutine never awaited fails its test (`tests/unawaited.py`), whatever the test asserts."""

import unittest
import warnings

import unawaited  # noqa: F401  (installs the hook for the whole run)


async def forgotten() -> None:
    """A coroutine the inner test makes and never awaits."""


class Inner(unittest.TestCase):
    """Run only by `UnawaitedTest`, never found by discovery (its name doesn't start `test`)."""

    def forgets(self) -> None:
        # As `make test` runs: the warning is an error, raised where the coroutine is finalized.
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            forgotten()

    def awaits(self) -> None:
        import asyncio

        asyncio.run(forgotten())


class UnawaitedTest(unittest.TestCase):
    def test_a_coroutine_never_awaited_fails_its_test(self):
        result = unittest.TestResult()
        Inner("forgets").run(result)
        [(_, said)] = result.failures
        self.assertIn("a coroutine was never awaited: forgotten", said)

    def test_an_awaited_coroutine_passes(self):
        result = unittest.TestResult()
        Inner("awaits").run(result)
        self.assertEqual((result.failures, result.errors), ([], []))


if __name__ == "__main__":
    unittest.main()
