"""flnd.source's routes: the five pattern rules, the order they're tried in, refusals, short links and the generated forms.

Also the checks a source's class gets when it's created, and the one test that runs every
source's `ROUTES` table (in tests/test_<code>.py) and checks each pattern has a row. Uses a
made-up source on example.com; the short-link HEAD request is a fake `follow`. No network.
"""

import importlib
import unittest

from flnd.engine import resolve
from flnd.source import Source, Target, TargetError, _parse, target
from flnd.sources import SOURCES


class Example(Source):
    """A source with every kind of pattern, on example.com."""

    code, name, domains = "exm", "Example", ("example.com",)
    unsupported = {"example.com/stories": "story links aren't supported; give the profile",
                   "example.com/watch*": "videos aren't supported"}

    @target(r"example.com/p/{code:[\w-]+}", r"example.com/*/p/{code:[\w-]+}")
    def post(self, session, code): ...

    @target(r"{id:\d{16,}}", r"example.com/video/{id:\d+}", shown="exm:<video id>")
    def video(self, session, id): ...

    @target(r"example.com/profile.php?id={user:\d+}", "{user}", "example.com/{user}")
    def user(self, session, user): ...

    def run(self, session): ...


def read(text):
    return Example().resolve(text)


class PatternTest(unittest.TestCase):
    def check(self, rows):
        for text, expected in rows:
            with self.subTest(text=text):
                self.assertEqual(read(text), Target("exm", *expected))

    def test_a_domain_pattern_matches_its_urls_however_they_are_written(self):
        self.check([
            ("https://example.com/example_user", ("user", "example_user")),
            ("http://example.com/example_user", ("user", "example_user")),
            ("example.com/example_user", ("user", "example_user")),
            ("https://www.example.com/example_user/", ("user", "example_user")),
            ("https://m.example.com/example_user", ("user", "example_user")),
            ("https://mobile.example.com/example_user", ("user", "example_user")),
            ("HTTPS://WWW.EXAMPLE.COM/example_user", ("user", "example_user")),
        ])

    def test_extra_segments_an_unnamed_query_and_the_fragment_are_ignored(self):
        self.check([
            ("https://example.com/example_user/photos/2026?hl=en#top", ("user", "example_user")),
            ("https://example.com/p/C1a-bc_D/?igsh=1", ("post", "C1a-bc_D")),
        ])

    def test_a_pattern_without_a_domain_matches_the_text_after_the_code(self):
        self.check([("example_user", ("user", "example_user")), ("example.user", ("user", "example.user")),
                    ("7301234567890123456", ("video", "7301234567890123456"))])
        # Without a slash or a scheme, text is a name, even with a dot; with one, it's a link.
        self.check([("example.com", ("user", "example.com")), ("example.com/example_user", ("user", "example_user"))])

    def test_a_name_placeholder_drops_one_at_sign(self):
        self.check([("@example_user", ("user", "example_user")),
                    ("https://example.com/@example_user", ("user", "example_user"))])
        for text in ("@@example_user", "example-user", "not a name", ""):
            with self.subTest(text=text), self.assertRaisesRegex(TargetError, "^exm: can't read"):
                read(text)

    def test_a_regex_placeholder_and_a_star(self):
        self.check([("https://example.com/example_user/p/C1abc/", ("post", "C1abc")),
                    ("https://example.com/video/42", ("video", "42"))])
        # 15 digits aren't a video id, so they're a name.
        self.check([("730123456789012", ("user", "730123456789012"))])
        # A star inside a segment is any text there: /watch, /watchlist, /watch/?v=1.
        for text in ("https://example.com/watch", "https://example.com/watchlist", "https://example.com/watch/?v=1"):
            with self.subTest(text=text), self.assertRaisesRegex(TargetError, "^exm: videos aren't supported$"):
                read(text)

    def test_a_query_placeholder(self):
        self.check([("https://www.example.com/profile.php?id=100012345678901&sk=photos",
                     ("user", "100012345678901"))])
        # Without its key, or with a value its regex refuses, the query pattern doesn't match.
        self.check([("https://example.com/profile.php", ("user", "profile.php")),
                    ("https://example.com/profile.php?id=example", ("user", "profile.php"))])


class OrderTest(unittest.TestCase):
    def test_routes_are_tried_in_declaration_order(self):
        # `post` comes first, so /p/<code> is never the user "p".
        self.assertEqual(read("https://example.com/p/C1abc"), Target("exm", "post", "C1abc"))
        self.assertEqual(Example.kinds_of_target, ("post", "video", "user"))
        self.assertEqual([route.kind for route in Example.routes], ["post", "video", "user"])

    def test_unsupported_beats_a_route(self):
        with self.assertRaises(TargetError) as caught:
            read("https://www.example.com/stories/example_user/1/")
        self.assertEqual(str(caught.exception), "exm: story links aren't supported; give the profile")
        self.assertIsNone(caught.exception.url)

    def test_a_subclass_keeps_its_bases_routes_and_replaces_one_by_name(self):
        class Child(Example):
            @target(r"example.com/u/{user}")
            def user(self, session, user): ...

        self.assertEqual([route.kind for route in Child.routes], ["post", "video", "user"])
        self.assertEqual(Child().resolve("https://example.com/u/example_user"), Target("exm", "user", "example_user"))
        with self.assertRaises(TargetError):
            Child().resolve("example_user")

    def test_a_route_is_still_a_method_on_an_instance(self):
        class Echo(Example):
            @target("{user}")
            def user(self, session, user):
                return (self, session, user)

        source = Echo()
        self.assertEqual(source.user("session", "example_user"), (source, "session", "example_user"))


class MissTest(unittest.TestCase):
    def test_a_link_nothing_matches_carries_its_url(self):
        with self.assertRaises(TargetError) as caught:
            read("example.com/")
        self.assertEqual(caught.exception.url, "https://example.com/")
        with self.assertRaises(TargetError) as caught:
            read("https://short.example.com/AbC")
        self.assertEqual(caught.exception.url, "https://short.example.com/AbC")

    def test_text_that_is_no_link_carries_none(self):
        for text in ("not a name", "https://elsewhere.example/example_user", "elsewhere.example/example_user"):
            with self.subTest(text=text), self.assertRaises(TargetError) as caught:
                read(text)
            self.assertIsNone(caught.exception.url)

    def test_the_refusal_lists_the_generated_forms(self):
        with self.assertRaises(TargetError) as caught:
            read("not a name")
        self.assertEqual(str(caught.exception),
                         "exm: can't read 'not a name'; write it like exm:<video id>, exm:<user>, "
                         "https://www.example.com/p/<code>/, https://www.example.com/…/p/<code>/, "
                         "https://www.example.com/video/<id>/, https://www.example.com/profile.php?id=<user>, "
                         "https://www.example.com/<user>/")


class ShortLinkTest(unittest.TestCase):
    """The engine follows a link no route matched once, with `follow` standing in for the HEAD request."""

    def follow_to(self, *places):
        followed = []

        def follow(url):
            followed.append(url)
            return places[len(followed) - 1]

        return follow, followed

    def test_followed_once_and_read_again(self):
        follow, followed = self.follow_to(*["https://www.example.com/p/C1abc/?utm_source=share"] * 2)
        self.assertEqual(resolve("exm:short.example.com/AbC", {"exm": Example}, follow=follow),
                         Target("exm", "post", "C1abc"))
        self.assertEqual(resolve("https://short.example.com/AbC", {"exm": Example}, follow=follow),
                         Target("exm", "post", "C1abc"))
        self.assertEqual(followed, ["https://short.example.com/AbC"] * 2)

    def test_a_second_miss_lists_the_forms(self):
        follow, followed = self.follow_to("https://www.example.com/", "never asked")
        with self.assertRaisesRegex(TargetError, r"^exm: can't read 'https://www.example.com/'; write it like exm:"):
            resolve("https://short.example.com/AbC", {"exm": Example}, follow=follow)
        self.assertEqual(followed, ["https://short.example.com/AbC"])

    def test_a_short_link_can_lead_to_an_unsupported_one(self):
        follow, _ = self.follow_to("https://www.example.com/watch/?v=1")
        with self.assertRaisesRegex(TargetError, "^exm: videos aren't supported$"):
            resolve("https://short.example.com/AbC", {"exm": Example}, follow=follow)

    def test_refusals_and_matches_are_never_followed(self):
        def follow(url):
            raise AssertionError(f"followed {url}")

        self.assertEqual(resolve("https://example.com/example_user", {"exm": Example}, follow=follow),
                         Target("exm", "user", "example_user"))
        for text in ("https://example.com/stories/example_user", "exm:not a name"):
            with self.subTest(text=text), self.assertRaises(TargetError):
                resolve(text, {"exm": Example}, follow=follow)

    def test_without_follow_the_miss_is_the_refusal(self):
        with self.assertRaisesRegex(TargetError, "^exm: can't read"):
            resolve("https://short.example.com/AbC", {"exm": Example})


class FormsTest(unittest.TestCase):
    def test_code_forms_come_first_and_shown_replaces_the_first_pattern(self):
        self.assertEqual(Example.forms[:2], ("exm:<video id>", "exm:<user>"))
        self.assertEqual(Example.routes[1].forms("exm"), ["exm:<video id>", "https://www.example.com/video/<id>/"])

    def test_a_route_without_patterns_shows_its_shown_form(self):
        class Chats(Source):
            code, name, domains = "cht", "Chats", ("example.com",)

            @target(shown="cht:<chat name>")
            def chat(self, session, chat): ...

            def resolve(self, text):
                return Target(self.code, "chat", text)

            def run(self, session): ...

        self.assertEqual((Chats.forms, Chats.kinds_of_target), (("cht:<chat name>",), ("chat",)))
        self.assertEqual(Chats().resolve("Lens Club"), Target("cht", "chat", "Lens Club"))

    def test_a_source_without_routes_keeps_its_resolve_and_examples(self):
        class Old(Source):
            code, name, domains = "old", "Old", ("example.com",)
            examples = ("old:example_user",)
            short_links = ("short.example.com",)

            def resolve(self, text):
                return Target(self.code, "user", text)

            def run(self, session): ...

        self.assertEqual((Old.forms, Old.kinds_of_target, Old.routes), (("old:example_user",), ("user",), ()))
        self.assertEqual(Old().resolve("example_user"), Target("old", "user", "example_user"))
        followed = []
        self.assertEqual(resolve("https://short.example.com/x", {"old": Old},
                                 follow=lambda url: followed.append(url) or "example_user"),
                         Target("old", "user", "example_user"))
        self.assertEqual(followed, ["https://short.example.com/x"])


class ClassCreationTest(unittest.TestCase):
    """Each mistake in a source's routes raises `TypeError` when its class is created, so at import."""

    def declare(self, **attributes):
        body = {"code": "bad", "name": "Bad", "domains": ("example.com",), "run": lambda self, session: None,
                **attributes}
        return type("Bad", (Source,), body)

    def assertRefused(self, message, make):
        with self.assertRaisesRegex(TypeError, message):
            make()

    def test_a_placeholder_must_name_the_value_parameter(self):
        self.assertRefused("captures 'name', but the method's value parameter is 'user'",
                           lambda: target("{name}")(lambda self, session, user: None))

    def test_a_route_takes_self_session_and_value(self):
        self.assertRefused(r"take \(self, session, value\)", lambda: target("{user}")(lambda self, user: None))

    def test_exactly_one_placeholder(self):
        self.assertRefused("needs exactly one placeholder, not 2",
                           lambda: target("example.com/{user}/{id:\\d+}")(lambda self, session, user: None))
        self.assertRefused("needs exactly one placeholder, not 0",
                           lambda: target("example.com/home")(lambda self, session, user: None))

    def test_duplicate_patterns(self):
        def route(*patterns):
            return target(*patterns)(lambda self, session, user: None)

        self.assertRefused("given twice", lambda: self.declare(user=route("{user}", "{user}")))
        self.assertRefused("given twice", lambda: self.declare(post=route("example.com/{user}"),
                                                               user=route("www.example.com/{user}")))

    def test_patterns_that_do_not_compile(self):
        for pattern, message in [("example.com/{user", "no '}'"), ("example.com/user}", "no '{'"),
                                 ("example.com/{1x}", "isn't a placeholder name"),
                                 ("example.com/{user:[a-}", "pattern 'example.com/{user:\\[a-}'"),
                                 ("{user}?id={user}", "only a URL pattern can name a query"),
                                 ("example.com/profile.php?id", "write a query as")]:
            with self.subTest(pattern=pattern):
                self.assertRefused(message, lambda: target(pattern)(lambda self, session, user: None))

    def test_a_pattern_on_another_site(self):
        self.assertRefused("isn't on its domains",
                           lambda: self.declare(user=target("elsewhere.example/{user}")(lambda self, s, user: None)))
        self.assertRefused("isn't on its domains",
                           lambda: self.declare(unsupported={"elsewhere.example/stories": "no"}))

    def test_unsupported_captures_nothing(self):
        self.assertRefused("captures nothing", lambda: self.declare(unsupported={"example.com/{user}": "no"}))

    def test_a_source_with_routes_drops_what_they_replace(self):
        user = target("{user}")(lambda self, session, user: None)
        for field, value in [("examples", ("bad:a",)), ("short_links", ("example.com",)),
                             ("kinds_of_target", ("user",))]:
            with self.subTest(field=field):
                self.assertRefused(f"drop {field}", lambda: self.declare(user=user, **{field: value}))


class RouteTablesTest(unittest.TestCase):
    """Every source with routes has a `ROUTES` table in tests/test_<code>.py, and each of its patterns has a row."""

    def test_every_pattern_has_a_row(self):
        for code, source in SOURCES.items():
            if not source.routes:
                continue
            with self.subTest(source=code):
                table = importlib.import_module(f"test_{code}").ROUTES
                patterns = [pattern for pattern, _ in source._refusals] + \
                    [pattern for route in source.routes for pattern in route._patterns]
                used = {first_match(source, text.removeprefix(f"{code}:")) for text, _ in table}
                self.assertEqual([pattern.text for pattern in patterns if pattern not in used], [])


def first_match(source, text):
    """The pattern that reads `text` for `source`, as `Source.resolve` tries them, or None."""
    url = _parse(text, source.domains)
    patterns = [pattern for pattern, _ in source._refusals] + \
        [pattern for route in source.routes for pattern in route._patterns]
    return next((pattern for pattern in patterns if pattern.match(text, url) is not None), None)


if __name__ == "__main__":
    unittest.main()
