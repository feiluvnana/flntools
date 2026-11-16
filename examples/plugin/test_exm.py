"""The example plugin's tests, written with flnd's test kit: no network, no browser, no home.

    python -m unittest discover -s examples/plugin
"""

import unittest

from exm import API, Example

from flnd.testing import FakeSite, check, collect

FIXTURES = {
    f"{API}/users/example_user": {"id": "42"},
    f"{API}/users/42/posts": lambda url, request: PAGES[request["params"] is not None],
    f"{API}/posts/p1": {"time": "2026-01-02", "urls": ["https://cdn.example.com/p1.jpg"]},
}
# The posts, in two pages: the first (asked without a cursor) names the second.
PAGES = {
    False: {"items": [{"id": "p2", "time": "2026-02-01",
                       "urls": ["https://cdn.example.com/p2.jpg",
                                "https://cdn.example.com/p2.mp4"]}],
            "next": "c2"},
    True: {"items": [{"id": "p1", "time": "2026-01-02",
                      "urls": ["https://cdn.example.com/p1.jpg"]}],
           "next": None},
}


class ExampleTest(unittest.TestCase):
    def test_the_class_and_its_examples_check(self):
        kinds = [found.kind for found in check(Example)]
        self.assertEqual(kinds, ["post", "user", "user"])

    def test_a_profile_lists_its_posts_newest_first(self):
        saved = collect(Example, "exm:example_user", json=FIXTURES)
        self.assertEqual([found.path for found in saved], [
            "posts/2026-02-01_p2_0.jpg", "posts/2026-02-01_p2_1.mp4", "posts/2026-01-02_p1.jpg",
        ])
        self.assertEqual([found.key for found in saved], ["p2_0", "p2_1", "p1_0"])
        self.assertEqual(str(saved[-1].date), "2026-01-02")

    def test_a_post_by_its_link(self):
        site = FakeSite(json=FIXTURES)
        [found] = collect(Example, "https://www.example.com/p/p1", site=site)
        self.assertEqual((found.path, found.url), ("2026-01-02_p1.jpg",
                                                   "https://cdn.example.com/p1.jpg"))
        self.assertEqual([url for _, url, _ in site.seen.requests], [f"{API}/posts/p1"])


if __name__ == "__main__":
    unittest.main()
