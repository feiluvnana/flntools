"""Target resolution: every row of engine.md's targets table, bare URLs, and the refusals."""

import unittest

from flnd.sources import SOURCES
from flnd.target import Target, TargetError, link, resolve


def read(text, follow=None):
    return resolve(text, SOURCES, follow=follow)


class AcceptedFormsTest(unittest.TestCase):
    def check(self, rows):
        for text, expected in rows:
            with self.subTest(text=text):
                self.assertEqual(read(text), expected)

    def test_ins(self):
        user = Target("ins", "user", "example_user")
        self.check([
            ("ins:example_user", user), ("ins:@example_user", user), ("ins:instagram.com/example_user", user),
            ("https://www.instagram.com/example_user/", user), ("https://instagram.com/example_user/?hl=en", user),
            ("instagram.com/example_user", user), ("https://m.instagram.com/example_user/reels/", user),
            ("ins:example.user", Target("ins", "user", "example.user")),
            ("https://www.instagram.com/p/C1abcDEFghi/", Target("ins", "post", "C1abcDEFghi")),
            ("ins:https://www.instagram.com/reel/C1abcDEFghi/", Target("ins", "post", "C1abcDEFghi")),
        ])

    def test_fbk(self):
        self.check([
            ("fbk:example.page", Target("fbk", "user", "example.page")),
            ("fbk:100012345678901", Target("fbk", "user", "100012345678901")),
            ("https://www.facebook.com/example.page", Target("fbk", "user", "example.page")),
            ("https://www.facebook.com/profile.php?id=100012345678901", Target("fbk", "user", "100012345678901")),
            ("https://fb.com/example.page", Target("fbk", "user", "example.page")),
            ("https://m.facebook.com/example.page/photos", Target("fbk", "user", "example.page")),
            ("https://mobile.facebook.com/example.page", Target("fbk", "user", "example.page")),
        ])

    def test_twt(self):
        user = Target("twt", "user", "example_handle")
        self.check([
            ("twt:example_handle", user), ("twt:@example_handle", user), ("https://x.com/example_handle", user),
            ("https://twitter.com/example_handle/media", user), ("x.com/example_handle", user),
            # A number is a user id, never a tweet.
            ("twt:44196397", Target("twt", "user", "44196397")),
            ("twt:1234567890123456789", Target("twt", "user", "1234567890123456789")),
            ("https://x.com/example_handle/status/1790000000000000000", Target("twt", "post", "1790000000000000000")),
            ("https://twitter.com/example_handle/status/1790000000000000000",
             Target("twt", "post", "1790000000000000000")),
        ])

    def test_tik(self):
        user = Target("tik", "user", "example_creator")
        video = Target("tik", "video", "7301234567890123456")
        self.check([
            ("tik:example_creator", user), ("tik:@example_creator", user),
            ("https://www.tiktok.com/@example_creator", user), ("https://www.tiktok.com/@example_creator?lang=en", user),
            ("https://www.tiktok.com/@example_creator/video/7301234567890123456", video),
            ("https://www.tiktok.com/@example_creator/photo/7301234567890123456", video),
            # A number of 16 or more digits is a video id; a shorter one is a name.
            ("tik:7301234567890123456", video), ("tik:1234567890123456", Target("tik", "video", "1234567890123456")),
            ("tik:123456789012345", Target("tik", "user", "123456789012345")),
        ])

    def test_loi(self):
        post = Target("loi", "post", "5930")
        self.check([("loi:5930", post), ("https://loix.cc/lois/5930", post), ("loix.cc/lois/5930", post),
                    ("loi:https://www.loix.cc/lois/5930?ref=1", post)])

    def test_tlg(self):
        self.check([("tlg:@examplechannel", Target("tlg", "chat", "examplechannel")),
                    ("https://t.me/examplechannel", Target("tlg", "chat", "examplechannel")),
                    ("tlg:Lens Club", Target("tlg", "chat", "Lens Club")),
                    ("tlg:-1001234567890", Target("tlg", "chat", "-1001234567890")),
                    ("https://t.me/c/1234567890/55", Target("tlg", "chat", "-1001234567890")),
                    ("https://t.me/+AbCdEf123", Target("tlg", "chat", "+AbCdEf123"))])

    def test_codes_are_case_insensitive_and_trimmed(self):
        self.assertEqual(read(" INS:someone "), Target("ins", "user", "someone"))


class ShortLinkTest(unittest.TestCase):
    def test_followed_with_the_given_function(self):
        followed = []

        def follow(url):
            followed.append(url)
            return "https://www.tiktok.com/@someone/video/7301234567890123456?is_from_webapp=1"

        self.assertEqual(read("https://vm.tiktok.com/ZMabc123/", follow), Target("tik", "video", "7301234567890123456"))
        self.assertEqual(read("tik:vm.tiktok.com/ZMabc123/", follow), Target("tik", "video", "7301234567890123456"))
        self.assertEqual(followed, ["https://vm.tiktok.com/ZMabc123/", "vm.tiktok.com/ZMabc123/"])

    def test_other_links_are_not_followed(self):
        def follow(url):
            raise AssertionError("followed a link that isn't short")

        self.assertEqual(read("https://www.tiktok.com/@someone", follow), Target("tik", "user", "someone"))

    def test_fb_watch_belongs_to_facebook(self):
        with self.assertRaisesRegex(TargetError, "^fbk: single posts and videos aren't supported yet"):
            read("https://fb.watch/abc/", lambda url: "https://www.facebook.com/watch/?v=123")


class RefusedTest(unittest.TestCase):
    def test_bare_value_lists_every_source(self):
        with self.assertRaises(TargetError) as caught:
            read("example_user")
        self.assertEqual(str(caught.exception), 'which source is "example_user"? Write it as tik:example_user, '
                                                "fbk:example_user, ins:example_user, twt:example_user, loi:example_user, "
                                                "tlg:example_user")

    def test_unknown_source(self):
        with self.assertRaisesRegex(TargetError, "^unknown source 'ytb'. Sources: tik, fbk, ins, twt, loi, tlg$"):
            read("ytb:nasa")

    def test_url_of_no_source(self):
        with self.assertRaisesRegex(TargetError, "^which source is"):
            read("https://example.com/someone")

    def test_refused_forms_have_a_hint(self):
        rows = [
            ("https://www.instagram.com/stories/example_user/123/", "ins: story links aren't supported; give the "
                                                                   "profile, for example ins:example_user"),
            ("https://www.facebook.com/example.page/posts/123", "fbk: single posts and videos aren't supported yet"),
        ]
        for text, message in rows:
            with self.subTest(text=text), self.assertRaisesRegex(TargetError, "^" + message):
                read(text)

    def test_unreadable_forms_list_the_examples(self):
        with self.assertRaises(TargetError) as caught:
            read("ins:not a name")
        self.assertEqual(str(caught.exception), "ins: can't read 'not a name'; write it like ins:example_user, "
                                                "ins:@example_user, https://www.instagram.com/example_user/, "
                                                "https://www.instagram.com/p/C1abcDEFghi/, "
                                                "https://www.instagram.com/reel/C1abcDEFghi/")
        for text in ("ins:", "tik:https://www.tiktok.com/", "twt:https://x.com/explore"):
            with self.subTest(text=text), self.assertRaisesRegex(TargetError, "can't read"):
                read(text)

    def test_empty(self):
        with self.assertRaises(TargetError):
            read("  ")


class LinkTest(unittest.TestCase):
    def test_domains_and_their_subdomains_only(self):
        self.assertEqual(link("https://www.instagram.com/a/?hl=en", ("instagram.com",)),
                         ("instagram.com", ["a"], {"hl": "en"}))
        self.assertIsNone(link("https://notinstagram.com/a", ("instagram.com",)))
        self.assertIsNone(link("https://instagram.com.evil.example/a", ("instagram.com",)))
        self.assertIsNone(link("example.user", ("instagram.com",)))

    def test_target_prints_as_code_and_value(self):
        self.assertEqual(str(Target("tik", "video", "1")), "tik:1")


if __name__ == "__main__":
    unittest.main()
