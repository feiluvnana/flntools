"""flnd.source: routes (the five pattern rules, their order, refusals, short links, the generated forms),
categories, `Post` and `File`, the formats rule, `day()`, `Range` and `pages`.

Also the checks a source's class gets when it's created, and the one test that runs every
source's `ROUTES` table (in tests/test_<code>.py) and checks each pattern has a row. Uses a
made-up source on example.com; the short-link HEAD request is a fake `follow`. No network.
"""

import dataclasses
import datetime
import importlib
import unittest
from unittest import mock

from example_source import FIXTURES, Example as RoutedExample
from fakes import collect

from flnd.engine import resolve
from flnd.source import File, Post, Range, Source, Target, TargetError, _parse, day, fill, pages, target
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
        self.assertEqual([route.kind for route in Example.routes], ["post", "video", "user"])

    def test_unsupported_beats_a_route(self):
        with self.assertRaises(TargetError) as caught:
            read("https://www.example.com/stories/example_user/1/")
        self.assertEqual(str(caught.exception), "exm: story links aren't supported; give the profile")
        self.assertIsNone(caught.exception.url)

    def test_a_more_specific_route_beats_a_refusal(self):
        class Narrow(Source):
            code, name, domains = "nrw", "Narrow", ("example.com",)
            unsupported = {"example.com/i": "that's not a profile"}

            @target("example.com/i/web/status/{id:\\d+}")
            def post(self, session, id): ...

            @target("example.com/*/status/{id:\\d+}")
            def other(self, session, id): ...

            @target("example.com/{user}")
            def user(self, session, user): ...

            def run(self, session): ...

        # Four literal segments (host, i, web, status) beat the refusal's two (host, i).
        self.assertEqual(Narrow().resolve("https://example.com/i/web/status/123"), Target("nrw", "post", "123"))
        for text in ("https://example.com/i", "https://example.com/i/flow/login"):
            with self.assertRaises(TargetError) as caught:
                Narrow().resolve(text)
            self.assertEqual(str(caught.exception), "nrw: that's not a profile")

    def test_an_equally_specific_route_does_not_beat_a_refusal(self):
        # `example.com/*/status/<id>` has two literals (host, status), the same as the refusal's.
        class Equal(Source):
            code, name, domains = "eql", "Equal", ("example.com",)
            unsupported = {"example.com/status": "no"}

            @target("example.com/*/status/{id:\\d+}")
            def post(self, session, id): ...

            def run(self, session): ...

        with self.assertRaises(TargetError) as caught:
            Equal().resolve("https://example.com/status/123")
        self.assertEqual(str(caught.exception), "eql: no")

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

        self.assertEqual((Chats.forms, [route.kind for route in Chats.routes]), (("cht:<chat name>",), ["chat"]))
        self.assertEqual(Chats().resolve("Lens Club"), Target("cht", "chat", "Lens Club"))


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

    def test_a_route_takes_self_site_and_value(self):
        self.assertRefused(r"take \(self, site, value\)", lambda: target("{user}")(lambda self, user: None))

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
    refusal = next((pattern for pattern, _ in source._refusals if pattern.match(text, url) is not None), None)
    routes = [pattern for route in source.routes for pattern in route._patterns]
    # A route pattern with more literal segments than the refusal wins over it, as in `resolve`.
    found = next((pattern for pattern in routes if pattern.match(text, url) is not None
                  and (refusal is None or pattern.literals > refusal.literals)), None)
    return found or refusal


class CategoriesTest(unittest.TestCase):
    """`@route.category`: the categories are methods, in declaration order, and `Source.categories` is derived."""

    def test_categories_keep_declaration_order_and_are_derived(self):
        self.assertEqual(RoutedExample.categories, ("posts", "albums", "profile"))
        self.assertEqual([category.name for category in RoutedExample.routes[1].categories],
                         ["posts", "albums", "profile"])
        self.assertEqual(RoutedExample.single_kinds, ("post",))

    def test_options_and_a_name_that_is_not_the_methods(self):
        class Named(Source):
            code, name, domains = "nmd", "Named", ("example.com",)

            @target("{user}")
            def user(self, site, user): ...

            @user.category(newest_first=True, name="reels")
            def reel_grid(self, site, profile): ...

        category, = Named.routes[0].categories
        self.assertEqual((category.name, category.attribute, category.newest_first), ("reels", "reel_grid", True))
        self.assertEqual(Named.categories, ("reels",))

    def test_the_decorator_returns_the_method_unchanged(self):
        # So a test calls a category as an ordinary method.
        posts = list(RoutedExample().profile(None, {"id": "42", "picture": "https://cdn.example/42.jpg"}))
        self.assertEqual([post.id for post in posts], ["42"])

    def test_without_from_every_category_is_called(self):
        saved = collect(RoutedExample, "exm:example_user", json=FIXTURES)
        self.assertEqual(list(dict.fromkeys(item.post.category for item in saved)), ["posts", "albums", "profile"])

    def test_a_field_and_methods_together_are_refused(self):
        with self.assertRaisesRegex(TypeError, "declare no categories"):
            class Both(Source):
                code, name, domains = "bth", "Both", ("example.com",)
                categories = ("posts",)

                @target("{user}")
                def user(self, site, user): ...

                @user.category
                def posts(self, site, profile): ...

    def test_a_category_declared_twice_or_with_the_wrong_parameters(self):
        route = target("{user}")(lambda self, site, user: None)
        route.category(lambda self, site, profile: None)
        with self.assertRaisesRegex(TypeError, "declared twice"):
            route.category(lambda self, site, profile: None)
        with self.assertRaisesRegex(TypeError, r"take \(self, site, context\)"):
            route.category(name="other")(lambda self, profile: None)


class PostTest(unittest.TestCase):
    def test_typed_keywords_and_fields(self):
        post = Post(7, [File("https://cdn.example/a.jpg")], date=1767312000, album="Summer")
        self.assertEqual((post.id, post.date, dict(post.fields), post.category), ("7", 1767312000, {"album": "Summer"}, ""))
        self.assertFalse(post.lazy)
        self.assertTrue(Post("7", lambda: []).lazy)

    def test_an_engine_field_is_a_type_error(self):
        for name in ("category", "num", "count", "index", "extension", "path"):
            with self.subTest(field=name), self.assertRaisesRegex(TypeError, f"{name} is a field the engine sets"):
                Post("7", **{name: "x"})
        with self.assertRaisesRegex(TypeError, "the engine sets"):
            Post("7").replace(category="posts")

    def test_frozen_and_replace(self):
        post = Post("7", headers={"Referer": "https://www.example.com/"}, album="a")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            post.id = "8"  # type: ignore[misc]
        changed = post.replace(album="b", date=5)
        self.assertEqual((dict(changed.fields), changed.date, dict(changed.headers or {})),
                         ({"album": "b"}, 5, {"Referer": "https://www.example.com/"}))
        self.assertEqual(dict(post.fields), {"album": "a"})

    def test_file_forms(self):
        self.assertEqual(File(["https://a.example/f.7z", "https://b.example/f.7z"]).url,
                         ("https://a.example/f.7z", "https://b.example/f.7z"))
        text = File.text("info.md", "# Title\n")
        self.assertEqual((text.url, text.name, text.content), (None, "info.md", "# Title\n"))
        with self.assertRaises(TypeError):
            File([])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            text.name = "other.md"  # type: ignore[misc]


class FormatsTest(unittest.TestCase):
    """The formats rule: a missing or empty field is "", and the separator it leaves is dropped."""

    def test_an_empty_field_and_its_separator_are_dropped(self):
        rows = [
            ("{category}/{album}", {"category": "posts"}, "posts"),
            ("{category}/{album}", {"category": "highlights", "album": "Trip"}, "highlights/Trip"),
            ("{category}/{album}", {}, ""),
            ("{date}_{id}{index}", {"id": "C1abc", "date": None, "index": ""}, "C1abc"),
            ("{date}_{id}{index}", {"id": "C1abc", "date": datetime.date(2026, 1, 2), "index": "_1"},
             "2026-01-02_C1abc_1"),
            ("{id}_{idx}", {"id": "71"}, "71"),
            ("{a} - {b}", {"a": "x"}, "x"),
            ("{num:02d}", {"num": 0}, "00"),
            ("{date:%Y}/{id}", {"id": "9", "date": datetime.date(2026, 1, 2)}, "2026/9"),
        ]
        for template, fields, expected in rows:
            with self.subTest(template=template, fields=fields):
                self.assertEqual(fill(template, fields), expected)

    def test_a_values_own_separators_are_kept(self):
        self.assertEqual(fill("{title}{index}", {"title": "a_", "index": ""}, safe=False), "a_")

    def test_every_part_goes_through_fmt_filename(self):
        self.assertEqual(fill("{author}/{id} {title}", {"author": "../a/b", "id": 7, "title": "x:y"}), "a_b/7_x_y")
        self.assertEqual(fill("{chat}:{id}", {"chat": "-100", "id": "5"}, safe=False), "-100:5")
        self.assertEqual(fill("{chat}:{id}", {"id": "5"}, safe=False), "5")


class DayTest(unittest.TestCase):
    """The one date rule: any form a site gives, in UTC, from 2004 to next year."""

    def test_seconds_milliseconds_and_digit_strings(self):
        for value in (1767312000, 1767312000.5, 1767312000000, "1767312000", "1767312000000", " 1767312000 "):
            with self.subTest(value=value):
                self.assertEqual(day(value), datetime.date(2026, 1, 2))

    def test_dates_datetimes_and_iso_text(self):
        minus_five = datetime.timezone(datetime.timedelta(hours=-5))
        self.assertEqual(day(datetime.datetime(2026, 1, 2, 23, tzinfo=minus_five)), datetime.date(2026, 1, 3))
        self.assertEqual(day(datetime.datetime(2026, 1, 2, 23)), datetime.date(2026, 1, 2))
        self.assertEqual(day(datetime.date(2026, 1, 2)), datetime.date(2026, 1, 2))
        self.assertEqual(day("2026-11-10 01:58:16"), datetime.date(2026, 11, 10))
        self.assertEqual(day("2026-01-02T23:00:00Z"), datetime.date(2026, 1, 2))

    def test_refused(self):
        next_next = datetime.date.today().year + 2
        for value in (None, True, "soon", 10 ** 30, -10 ** 30, 946684800, f"{next_next}-01-01",
                      datetime.date(2003, 12, 31)):
            with self.subTest(value=value):
                self.assertIsNone(day(value))
        # 2004 and next year are the bounds, both kept.
        self.assertEqual(day(datetime.date(2004, 1, 1)), datetime.date(2004, 1, 1))
        next_year = datetime.date(datetime.date.today().year + 1, 12, 31)
        self.assertEqual(day(next_year), next_year)


class RangeTest(unittest.TestCase):
    def test_in(self):
        time = Range(since=datetime.date(2026, 1, 1), until=datetime.date(2026, 1, 31))
        self.assertIn(datetime.date(2026, 1, 1), time)
        self.assertIn(datetime.datetime(2026, 1, 31, 23, 59), time)
        self.assertNotIn(datetime.date(2025, 12, 31), time)
        self.assertNotIn(datetime.date(2026, 2, 1), time)
        # An undated post is always kept.
        self.assertIn(None, time)
        self.assertIn(datetime.date(1999, 1, 1), Range())


class PagesTest(unittest.TestCase):
    def test_follows_cursors_and_stops_on_a_repeat_or_an_empty_one(self):
        book = {None: {"n": 1, "next": "b"}, "b": {"n": 2, "next": "c"}, "c": {"n": 3, "next": "b"}}
        fetch = mock.Mock(side_effect=lambda cursor: book[cursor])
        self.assertEqual([page["n"] for page in pages(fetch, lambda page: page["next"])], [1, 2, 3])
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([page["n"] for page in pages(lambda cursor: {"n": 1, "next": ""}, lambda page: page["next"])],
                         [1])


if __name__ == "__main__":
    unittest.main()
