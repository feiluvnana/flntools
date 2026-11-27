"""Reading the built-in plugins' targets from the manifest (`flnd.engine.resolve`): every
plugin's forms, bare URLs, short links and the refusals. No network."""

import unittest

from flnd.engine.plugins import PLUGINS
from flnd.engine.resolve import Manifest, resolve
from flnd.errors import UsageError
from flnd.plugin import Target

MANIFEST = Manifest(PLUGINS)


def read(text, follow=None):
    found = resolve(text, manifest=MANIFEST, known={}, follow=follow)
    return Target(found.key, kind=found.kind)


class AcceptedFormsTest(unittest.TestCase):
    def check(self, rows):
        for text, expected in rows:
            with self.subTest(text=text):
                self.assertEqual(read(text), expected)

    def test_ins(self):
        user = Target("ins:example_user", kind="user")
        self.check([
            ("ins:example_user", user), ("ins:@example_user", user),
            ("https://www.instagram.com/example_user/", user),
                    ("https://instagram.com/example_user/?hl=en", user),
            ("instagram.com/example_user", user), ("https://m.instagram.com/example_user/reels/",
                                                   user),
            ("ins:example_user", Target("ins:example_user", kind="user")),
            ("https://www.instagram.com/p/C1abcDEFghi/",
             Target("ins:post/C1abcDEFghi", kind="post")),
        ])

    def test_fbk(self):
        self.check([
            ("fbk:example.page", Target("fbk:example.page", kind="user")),
            ("fbk:100012345678901", Target("fbk:100012345678901", kind="user")),
            ("https://www.facebook.com/example.page", Target("fbk:example.page", kind="user")),
            ("https://www.facebook.com/profile.php?id=100012345678901",
             Target("fbk:100012345678901", kind="user")),
            ("https://fb.com/example.page", Target("fbk:example.page", kind="user")),
            ("https://m.facebook.com/example.page/photos", Target("fbk:example.page", kind="user")),
            ("https://mobile.facebook.com/example.page", Target("fbk:example.page", kind="user")),
            ("https://web.facebook.com/example.page", Target("fbk:example.page", kind="user")),
            ("https://mbasic.facebook.com/example.page", Target("fbk:example.page", kind="user")),
            ("https://touch.facebook.com/example.page", Target("fbk:example.page", kind="user")),
        ])

    def test_twt(self):
        user = Target("twt:example_handle", kind="user")
        self.check([
            ("twt:example_handle", user), ("twt:@example_handle", user),
                    ("https://x.com/example_handle", user),
            ("https://twitter.com/example_handle/media", user), ("x.com/example_handle", user),
            # A number is a user id, never a tweet.
            ("twt:44196397", Target("twt:44196397", kind="user")),
            ("twt:1234567890123456789", Target("twt:1234567890123456789", kind="user")),
            ("https://x.com/example_handle/status/1790000000000000000",
             Target("twt:post/1790000000000000000", kind="post")),
            ("https://twitter.com/example_handle/status/1790000000000000000",
             Target("twt:post/1790000000000000000", kind="post")),
        ])

    def test_tik(self):
        user = Target("tik:example_creator", kind="user")
        video = Target("tik:post/7301234567890123456", kind="post")
        self.check([
            ("tik:example_creator", user), ("tik:@example_creator", user),
            ("https://www.tiktok.com/@example_creator", user),
                    ("https://www.tiktok.com/@example_creator?lang=en", user),
            ("https://www.tiktok.com/@example_creator/video/7301234567890123456", video),
            ("https://www.tiktok.com/@example_creator/photo/7301234567890123456", video),
            # A number of 16 or more digits is a video id; a shorter one is a name.
            ("tik:7301234567890123456", video), ("tik:1234567890123456",
                                                 Target("tik:post/1234567890123456", kind="post")),
            ("tik:123456789012345", Target("tik:123456789012345", kind="user")),
        ])

    def test_loi(self):
        post = Target("loi:5930", kind="post")
        author = Target("loi:author/183", kind="author")
        self.check([("loi:5930", post), ("https://loix.cc/lois/5930", post),
                    ("loix.cc/lois/5930", post), ("loi:author/183", author),
                    ("https://loix.cc/library/loier/183", author)])

    def test_codes_are_case_insensitive_and_trimmed(self):
        self.assertEqual(read(" INS:example_user "), Target("ins:example_user", kind="user"))

    def test_percent_encoded_path_segments_are_decoded(self):
        self.assertEqual(
            read("https://mimihentai.moe/author/some%20author"),
            Target("mht:author/some author", kind="author"),
        )


class RoundTripTest(unittest.TestCase):
    def test_every_single_post_key_resolves_when_typed_back(self):
        for link in (
            "https://www.instagram.com/p/C1abcDEFghi/", "https://x.com/example_handle/status/1",
            "https://www.tiktok.com/@example_creator/video/7301234567890123456",
            "https://vm.tiktok.com/ZMabc123/", "https://www.facebook.com/watch/?v=123",
            "https://fb.watch/abc/", "https://www.facebook.com/photo.php?fbid=123",
            "https://www.facebook.com/permalink.php?story_fbid=pfbid1&id=2",
            "https://loix.cc/lois/5930", "https://loix.cc/library/loier/183",
        ):
            with self.subTest(link=link):
                key = read(link)
                self.assertEqual(read(key.text), key)
        self.assertEqual(read("tik:short/ZMabc"), Target("tik:short/ZMabc", kind="short"))
        self.assertEqual(read("fbk:watch/abc"), Target("fbk:watch/abc", kind="watch"))
        with self.assertRaisesRegex(UsageError, r"reel isn't a post kind of ins; write "
                                    r"ins:post/<id>, or categories after \|"):
            read("ins:reel/C1")


class ShortLinkTest(unittest.TestCase):
    def test_followed_with_the_given_function(self):
        followed = []

        def follow(url):
            followed.append(url)
            return (
                "https://www.tiktok.com/@example_creator/video/"
                "7301234567890123456?is_from_webapp=1"
            )

        self.assertEqual(read("https://www.tiktok.com/example_creator", follow),
                         Target("tik:post/7301234567890123456", kind="post"))
        self.assertEqual(followed, ["https://www.tiktok.com/example_creator"])

    def test_other_links_are_not_followed(self):
        def follow(url):
            raise AssertionError("followed a link that isn't short")

        self.assertEqual(read("https://www.tiktok.com/@example_creator", follow),
                         Target("tik:example_creator", kind="user"))

    def test_fb_watch_belongs_to_facebook(self):
        def follow(url):
            raise AssertionError("an fb.watch link is read by fbk's own route, never followed here")

        self.assertEqual(read("https://fb.watch/abc/", follow),
                         Target("fbk:watch/abc", kind="watch"))

    def test_share_links_belong_to_facebook(self):
        def follow(url):
            raise AssertionError("a share link is read by fbk's own route, never followed here")

        self.assertEqual(read("https://www.facebook.com/share/r/abc/", follow),
                         Target("fbk:share/r/abc", kind="share"))

    def test_cross_source_redirect_and_unchanged_follow(self):
        self.assertEqual(
            read("https://www.tiktok.com/example_creator",
                 lambda url: "https://www.instagram.com/p/C1abcDEFghi/"),
            Target("ins:post/C1abcDEFghi", kind="post"),
        )
        with self.assertRaisesRegex(UsageError, "can't read"):
            read("https://www.tiktok.com/example_creator", lambda url: url)


class RefusedTest(unittest.TestCase):
    def test_refused_forms_have_a_hint(self):
        rows = [
            ("https://www.instagram.com/stories/example_user/123/",
             "ins: story links aren't supported; give the "
             "profile, and its stories come with it"),
            ("https://www.instagram.com/explore/", "ins: that's not a profile"),
            ("https://www.instagram.com/accounts/login/", "ins: that's not a profile"),
            ("https://www.facebook.com/groups/123/posts/1", "fbk: groups aren't supported"),
            ("https://x.com/explore", "twt: that's not a profile"),
            ("https://x.com/intent/tweet", "twt: that's not a profile"),
        ]
        for text, message in rows:
            with self.subTest(text=text), self.assertRaisesRegex(UsageError, "^" + message):
                read(text)

    def test_unreadable_forms(self):
        for text, said in (("ins:", "write a value"), ("ins:not a name", "can't read"),
                           ("tik:https://www.tiktok.com/", "can't read")):
            with self.subTest(text=text), self.assertRaisesRegex(UsageError, said):
                read(text)

    def test_empty(self):
        with self.assertRaises(UsageError):
            read("  ")


class TargetTest(unittest.TestCase):
    def test_target_prints_as_code_and_value(self):
        self.assertEqual(str(Target("tik:1", kind="video")), "tik:1")


if __name__ == "__main__":
    unittest.main()
