"""The public test kit, `flnd.testing`: what a plugin's own tests are written with.

Its docstrings' examples run in tests/test_docs.py; these cover what they don't: `check` over
every built-in source and over a wrong example, `collect`'s `only`, and the fake clock under a
source's pacing.
"""

import unittest

import no_profiles  # noqa: F401

from flnd import _time
from flnd.source import File, Post, Source, SourceError, UsageError, target
from flnd.sources import SOURCES
from flnd.testing import FakeSite, check, collect, fake_clock


class Example(Source):
    code, domains, api = "exm", ("example.com",), 1

    @target("exm:{user}", "example.com/{user}")
    def user(self, site, user):
        """A profile: `exm:example_user`."""
        return site.fetch(f"https://api.example.com/{user}").json

    @user.category
    def posts(self, site, profile):
        for item in profile["items"]:
            yield Post(item, [File(f"https://cdn.example.com/{item}.jpg")])

    @user.category
    def avatar(self, site, profile):
        yield Post("avatar", [File("https://cdn.example.com/avatar.jpg")])


class CheckTest(unittest.TestCase):
    def test_every_built_in_source_checks(self):
        for code, source in SOURCES.items():
            with self.subTest(source=code):
                check(source)

    def test_an_example_read_by_another_route_fails(self):
        class Wrong(Source):
            code, domains, api = "wrg", ("example.com",), 1

            @target("example.com/{user}")
            def user(self, site, user):
                return []

            @target(r"example.com/p/{id:\w+}")
            def post(self, site, id):
                """One post: `https://www.example.com/p/p1`."""
                return []

        with self.assertRaises(SourceError) as caught:
            check(Wrong)
        self.assertEqual(str(caught.exception), "wrg.post: the example "
                         "'https://www.example.com/p/p1' is read by the route user")

    def test_check_takes_a_source_class(self):
        with self.assertRaises(SourceError):
            check(object)  # type: ignore[arg-type]


class CollectTest(unittest.TestCase):
    JSON = {"https://api.example.com/example_user": {"items": ["p1"]}}

    def test_only_lists_the_categories_named(self):
        saved = collect(Example, "exm:example_user", json=self.JSON, only=["posts"])
        self.assertEqual([found.path for found in saved], ["posts/p1_0.jpg"])
        both = collect(Example, "exm:example_user", json=self.JSON)
        self.assertEqual([found.key for found in both], ["p1_0", "avatar_0"])

    def test_an_unknown_category_is_a_usage_error(self):
        with self.assertRaises(UsageError):
            collect(Example, "exm:example_user", json=self.JSON, only=["reels"])

    def test_a_site_given_is_the_one_run_on(self):
        site = FakeSite(json=self.JSON)
        collect(Example, "exm:example_user", site=site)
        self.assertEqual(str(site.target), "exm:example_user")
        self.assertEqual(len(site.seen.requests), 1)


class ClockTest(unittest.TestCase):
    def test_the_fake_clock_moves_only_when_slept_on(self):
        with fake_clock(100.0) as clock:
            self.assertEqual(_time.now(), 100.0)
            _time.sleep(2.5)
            self.assertEqual((clock.now, clock.slept), (102.5, [2.5]))
        self.assertNotEqual(_time.now(), 102.5)


if __name__ == "__main__":
    unittest.main()
