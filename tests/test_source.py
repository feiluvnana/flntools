"""flnd.source, source API 1: routes (the pattern rules, their order, refusals, short links, the
generated forms), categories, `Target`, `Post` and `File`, `File.dash`, the templates rule,
`Range`, `dig`, logins, and `Site.pages` over a fake site.

Also every check a source's class gets when it is made (a mistake names the source, the
attribute and the fix), and the one test that runs every source's `ROUTES` table (in
tests/test_<code>.py) and checks each pattern has a row. Uses made-up sources on example.com;
the short-link HEAD request is a fake `follow`. No network.
"""

import dataclasses
import datetime
import importlib
import unittest

from example_source import FIXTURES
from example_source import Example as RoutedExample
from fakes import FakeSite, collect

from flnd import _text, _time
from flnd.engine import resolve
from flnd.site import Listing
from flnd.source import (
    API,
    Cookies,
    File,
    Login,
    NotFound,
    Post,
    Range,
    Request,
    Source,
    SourceError,
    Target,
    UsageError,
    _parse,
    cookies,
    dig,
    target,
)
from flnd.sources import SOURCES


class Example(Source):
    """A source with every kind of pattern, on example.com."""

    code, name, domains = "exm", "Example", ("example.com",)

    api = 1
    unsupported = {"example.com/stories": "story links aren't supported; give the profile",
                   "example.com/watch*": "videos aren't supported"}

    @target(r"example.com/p/{code:[\w-]+}", r"example.com/*/p/{code:[\w-]+}")
    def post(self, session, code): ...

    @target(r"exm:{id:\d{16,}}", r"example.com/video/{id:\d+}", shown="exm:<video id>")
    def video(self, session, id): ...

    @target(r"example.com/profile.php?id={user:\d+}", "exm:{user}", "example.com/{user}")
    def user(self, session, user): ...



def read(text):
    return Example().resolve(text)


class PatternTest(unittest.TestCase):
    def check(self, rows):
        for text, expected in rows:
            with self.subTest(text=text):
                self.assertEqual(read(text), Target(f"exm:{expected[1]}", kind=expected[0]))

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
            with self.subTest(text=text), self.assertRaisesRegex(UsageError, "^exm: can't read"):
                read(text)

    def test_a_regex_placeholder_and_a_star(self):
        self.check([("https://example.com/example_user/p/C1abc/", ("post", "C1abc")),
                    ("https://example.com/video/42", ("video", "42"))])
        # 15 digits aren't a video id, so they're a name.
        self.check([("730123456789012", ("user", "730123456789012"))])
        # A star inside a segment is any text there: /watch, /watchlist, /watch/?v=1.
        for text in ("https://example.com/watch", "https://example.com/watchlist", "https://example.com/watch/?v=1"):
            with self.subTest(text=text), self.assertRaisesRegex(UsageError, "^exm: videos aren't supported$"):
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
        self.assertEqual(read("https://example.com/p/C1abc"), Target("exm:C1abc", kind="post"))
        self.assertEqual([route.kind for route in Example.routes], ["post", "video", "user"])

    def test_unsupported_beats_a_route(self):
        with self.assertRaises(UsageError) as caught:
            read("https://www.example.com/stories/example_user/1/")
        self.assertEqual(str(caught.exception), "exm: story links aren't supported; give the profile")
        self.assertIsNone(getattr(caught.exception, "url", None))

    def test_a_more_specific_route_beats_a_refusal(self):
        class Narrow(Source):
            code, name, domains = "nrw", "Narrow", ("example.com",)
            api = 1
            unsupported = {"example.com/i": "that's not a profile"}

            @target("example.com/i/web/status/{id:\\d+}")
            def post(self, session, id): ...

            @target("example.com/*/status/{id:\\d+}")
            def other(self, session, id): ...

            @target("example.com/{user}")
            def user(self, session, user): ...


        # Four literal segments (host, i, web, status) beat the refusal's two (host, i).
        self.assertEqual(Narrow().resolve("https://example.com/i/web/status/123"), Target("nrw:123", kind="post"))
        for text in ("https://example.com/i", "https://example.com/i/flow/login"):
            with self.assertRaises(UsageError) as caught:
                Narrow().resolve(text)
            self.assertEqual(str(caught.exception), "nrw: that's not a profile")

    def test_an_equally_specific_route_does_not_beat_a_refusal(self):
        # `example.com/*/status/<id>` has two literals (host, status), the same as the refusal's.
        class Equal(Source):
            code, name, domains = "eql", "Equal", ("example.com",)
            api = 1
            unsupported = {"example.com/status": "no"}

            @target("example.com/*/status/{id:\\d+}")
            def post(self, session, id): ...


        with self.assertRaises(UsageError) as caught:
            Equal().resolve("https://example.com/status/123")
        self.assertEqual(str(caught.exception), "eql: no")

    def test_a_subclass_keeps_its_bases_routes_and_replaces_one_by_name(self):
        class Child(Example):
            @target(r"example.com/u/{user}")
            def user(self, session, user): ...

        self.assertEqual([route.kind for route in Child.routes], ["post", "video", "user"])
        self.assertEqual(Child().resolve("https://example.com/u/example_user"), Target("exm:example_user", kind="user"))
        with self.assertRaises(UsageError):
            Child().resolve("example_user")

    def test_a_route_is_still_a_method_on_an_instance(self):
        class Echo(Example):
            @target("exm:{user}")
            def user(self, session, user):
                return (self, session, user)

        source = Echo()
        self.assertEqual(source.user("session", "example_user"), (source, "session", "example_user"))


class MissTest(unittest.TestCase):
    def test_a_link_nothing_matches_carries_its_url(self):
        with self.assertRaises(UsageError) as caught:
            read("example.com/")
        self.assertEqual(caught.exception.url, "https://example.com/")
        with self.assertRaises(UsageError) as caught:
            read("https://short.example.com/AbC")
        self.assertEqual(caught.exception.url, "https://short.example.com/AbC")

    def test_text_that_is_no_link_carries_none(self):
        for text in ("not a name", "https://elsewhere.example/example_user", "elsewhere.example/example_user"):
            with self.subTest(text=text), self.assertRaises(UsageError) as caught:
                read(text)
            self.assertIsNone(getattr(caught.exception, "url", None))

    def test_the_refusal_lists_the_generated_forms(self):
        with self.assertRaises(UsageError) as caught:
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
                         Target("exm:C1abc", kind="post"))
        self.assertEqual(resolve("https://short.example.com/AbC", {"exm": Example}, follow=follow),
                         Target("exm:C1abc", kind="post"))
        self.assertEqual(followed, ["https://short.example.com/AbC"] * 2)

    def test_a_second_miss_lists_the_forms(self):
        follow, followed = self.follow_to("https://www.example.com/", "never asked")
        with self.assertRaisesRegex(UsageError, r"^exm: can't read 'https://www.example.com/'; write it like exm:"):
            resolve("https://short.example.com/AbC", {"exm": Example}, follow=follow)
        self.assertEqual(followed, ["https://short.example.com/AbC"])

    def test_a_short_link_can_lead_to_an_unsupported_one(self):
        follow, _ = self.follow_to("https://www.example.com/watch/?v=1")
        with self.assertRaisesRegex(UsageError, "^exm: videos aren't supported$"):
            resolve("https://short.example.com/AbC", {"exm": Example}, follow=follow)

    def test_refusals_and_matches_are_never_followed(self):
        def follow(url):
            raise AssertionError(f"followed {url}")

        self.assertEqual(resolve("https://example.com/example_user", {"exm": Example}, follow=follow),
                         Target("exm:example_user", kind="user"))
        for text in ("https://example.com/stories/example_user", "exm:not a name"):
            with self.subTest(text=text), self.assertRaises(UsageError):
                resolve(text, {"exm": Example}, follow=follow)

    def test_without_follow_the_miss_is_the_refusal(self):
        with self.assertRaisesRegex(UsageError, "^exm: can't read"):
            resolve("https://short.example.com/AbC", {"exm": Example})


class FormsTest(unittest.TestCase):
    def test_code_forms_come_first_and_shown_replaces_the_first_pattern(self):
        self.assertEqual(Example.forms[:2], ("exm:<video id>", "exm:<user>"))
        self.assertEqual(Example.routes[1].forms("exm"), ["exm:<video id>", "https://www.example.com/video/<id>/"])

    def test_a_route_without_patterns_shows_its_shown_form(self):
        class Chats(Source):
            code, name, domains = "cht", "Chats", ("example.com",)
            api = 1

            @target(shown="cht:<chat name>")
            def chat(self, session, chat): ...

            def resolve(self, text):
                return Target(f"{self.code}:{text}", kind="chat")


        self.assertEqual((Chats.forms, [route.kind for route in Chats.routes]), (("cht:<chat name>",), ["chat"]))
        self.assertEqual(Chats().resolve("Lens Club"), Target("cht:Lens Club", kind="chat"))


class ClassCreationTest(unittest.TestCase):
    """Each mistake in a source's routes raises `SourceError` when its class is created, so at import."""

    def declare(self, **attributes):
        body = {"code": "bad", "name": "Bad", "domains": ("example.com",), "api": 1, **attributes}
        if not any(isinstance(value, type(target("bad:{x}")(lambda self, site, x: None)))
                   for value in body.values()):
            body["user"] = target("bad:{user}")(lambda self, site, user: None)
        return type("Bad", (Source,), body)

    def assertRefused(self, message, make):
        with self.assertRaisesRegex(SourceError, message):
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

        self.assertRefused("given twice", lambda: self.declare(user=route("bad:{user}", "bad:{user}")))
        self.assertRefused("given twice", lambda: self.declare(post=route("example.com/{user}"),
                                                               user=route("www.example.com/{user}")))

    def test_patterns_that_do_not_compile(self):
        for pattern, message in [("example.com/{user", "no '}'"), ("example.com/user}", "no '{'"),
                                 ("example.com/{1x}", "isn't a placeholder name"),
                                 ("example.com/{user:[a-}", "pattern 'example.com/{user:\\[a-}'"),
                                 ("{user}?id={user}", "only a link pattern can name a query"),
                                 ("example.com/profile.php?id", "write a query as")]:
            with self.subTest(pattern=pattern):
                self.assertRefused(message, lambda pattern=pattern: target(pattern)(lambda self, session, user: None))

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
                used = {first_match(source, text.removeprefix(f"{code}:").removeprefix(code) if text.startswith(f"{code}?") else text.removeprefix(f"{code}:")) for text, _ in table}
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

    def test_options_and_the_name_from_the_method(self):
        class Named(Source):
            code, name, domains = "nmd", "Named", ("example.com",)
            api = 1

            @target("nmd:{user}")
            def user(self, site, user): ...

            @user.category(newest_first=True)
            def reels(self, site, profile): ...

        category, = Named.routes[0].categories
        self.assertEqual((category.name, category.newest_first), ("reels", True))
        self.assertEqual(Named.categories, ("reels",))

    def test_the_decorator_returns_the_method_unchanged(self):
        # So a test calls a category as an ordinary method.
        posts = list(RoutedExample().profile(None, {"id": "42", "picture": "https://cdn.example/42.jpg"}))
        self.assertEqual([post.id for post in posts], ["42"])

    def test_without_from_every_category_is_called(self):
        saved = collect(RoutedExample, "exm:example_user", json=FIXTURES)
        self.assertEqual(list(dict.fromkeys(item.post.category for item in saved)), ["posts", "albums", "profile"])

    def test_a_field_and_methods_together_are_refused(self):
        with self.assertRaisesRegex(SourceError, r"^Both.categories: made from the @target"):
            class Both(Source):
                code, name, domains = "bth", "Both", ("example.com",)
                api = 1
                categories = ("posts",)

                @target("bth:{user}")
                def user(self, site, user): ...

                @user.category
                def posts(self, site, profile): ...

    def test_a_category_declared_twice_or_with_the_wrong_parameters(self):
        route = target("exm:{user}")(lambda self, site, user: None)
        route.category(lambda self, site, profile: None)
        with self.assertRaisesRegex(SourceError, "declared twice"):
            route.category(lambda self, site, profile: None)
        with self.assertRaisesRegex(SourceError, r"take \(self, site, context\)"):
            route.category(key="")(lambda self, profile: None)


class PostTest(unittest.TestCase):
    def test_typed_keywords_and_fields(self):
        post = Post(7, [File("https://cdn.example/a.jpg")], date=1767312000, album="Summer")
        self.assertEqual((post.id, post.date, dict(post.fields), post.category), ("7", 1767312000, {"album": "Summer"}, ""))
        self.assertFalse(post.lazy)
        self.assertTrue(Post("7", lambda: []).lazy)

    def test_an_engine_field_is_a_type_error(self):
        for name in ("category", "num", "count", "index", "extension", "path", "post", "source"):
            kwargs: dict[str, object] = {name: "x"}
            with self.subTest(field=name), self.assertRaisesRegex(SourceError, f"{name} is a field the engine sets"):
                Post("7", **kwargs)  # type: ignore[arg-type]
        with self.assertRaisesRegex(SourceError, "the engine sets"):
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
        with self.assertRaises(SourceError):
            File([])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            text.name = "other.md"  # type: ignore[misc]


class FormatsTest(unittest.TestCase):
    """The templates rule: a missing or empty field is "", and the separator it leaves is dropped."""

    def test_an_empty_field_and_its_separator_are_dropped(self):
        rows = [
            ("{category}/{album}", {"category": "posts"}, "posts"),
            ("{category}/{album}", {"category": "highlights", "album": "Trip"}, "highlights/Trip"),
            ("{category}/{album}", {}, ""),
            ("{date}_{post}{index}", {"post": "C1abc", "date": None, "index": ""}, "C1abc"),
            ("{date}_{post}{index}", {"post": "C1abc", "date": datetime.date(2026, 1, 2), "index": "_1"},
             "2026-01-02_C1abc_1"),
            ("{post}_{idx}", {"post": "71"}, "71"),
            ("{a} - {b}", {"a": "x"}, "x"),
            ("{num:02d}", {"num": 0}, "00"),
            ("{date:%Y}/{post}", {"post": "9", "date": datetime.date(2026, 1, 2)}, "2026/9"),
        ]
        for template, fields, expected in rows:
            with self.subTest(template=template, fields=fields):
                self.assertEqual(_text.fill(template, fields), expected)

    def test_a_values_own_separators_are_kept(self):
        self.assertEqual(_text.fill("{title}{index}", {"title": "a_", "index": ""}, safe=False), "a_")

    def test_every_part_goes_through_a_file_name(self):
        self.assertEqual(_text.fill("{author}/{post} {title}", {"author": "../a/b", "post": 7, "title": "x:y"}),
                         "a_b/7_x_y")
        self.assertEqual(_text.fill("{chat}:{post}", {"chat": "-100", "post": "5"}, safe=False), "-100:5")
        self.assertEqual(_text.fill("{chat}:{post}", {"post": "5"}, safe=False), "5")


class DayTest(unittest.TestCase):
    """The one date rule, the engine's (`flnd._time.day`): any form a site gives, in UTC, 2004 to next year."""

    def test_seconds_milliseconds_and_digit_strings(self):
        for value in (1767312000, 1767312000.5, 1767312000000, "1767312000", "1767312000000", " 1767312000 "):
            with self.subTest(value=value):
                self.assertEqual(_time.day(value), datetime.date(2026, 1, 2))

    def test_dates_datetimes_and_iso_text(self):
        minus_five = datetime.timezone(datetime.timedelta(hours=-5))
        self.assertEqual(_time.day(datetime.datetime(2026, 1, 2, 23, tzinfo=minus_five)), datetime.date(2026, 1, 3))
        self.assertEqual(_time.day(datetime.datetime(2026, 1, 2, 23)), datetime.date(2026, 1, 2))
        self.assertEqual(_time.day(datetime.date(2026, 1, 2)), datetime.date(2026, 1, 2))
        self.assertEqual(_time.day("2026-11-10 01:58:16"), datetime.date(2026, 11, 10))
        self.assertEqual(_time.day("2026-01-02T23:00:00Z"), datetime.date(2026, 1, 2))

    def test_refused(self):
        next_next = datetime.date.today().year + 2
        for value in (None, True, "soon", 10 ** 30, -10 ** 30, 946684800, f"{next_next}-01-01",
                      datetime.date(2003, 12, 31)):
            with self.subTest(value=value):
                self.assertIsNone(_time.day(value))
        # 2004 and next year are the bounds, both kept.
        self.assertEqual(_time.day(datetime.date(2004, 1, 1)), datetime.date(2004, 1, 1))
        next_year = datetime.date(datetime.date.today().year + 1, 12, 31)
        self.assertEqual(_time.day(next_year), next_year)


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

    def test_a_raw_date_is_read_as_the_engine_reads_a_posts(self):
        time = Range(since=datetime.date(2026, 1, 1))
        self.assertIn(1767312000, time)
        self.assertIn("2026-01-02T01:00:00Z", time)
        self.assertNotIn("2025-12-31", time)
        # Text that isn't a date is an undated post: kept.
        self.assertIn("soon", time)


class TargetTest(unittest.TestCase):
    def test_text_as_typed(self):
        self.assertEqual((Target("ins:example_user").source, Target("ins:example_user").value),
                         ("ins", "example_user"))
        query = Target("mht?romance comedy")
        self.assertEqual((query.source, query.value, query.query, str(query)),
                         ("mht", "romance comedy", True, "mht?romance comedy"))
        link = Target("https://www.tiktok.com/@example_user")
        self.assertEqual((link.source, link.kind, str(link)), ("", "", "https://www.tiktok.com/@example_user"))
        # A value may hold a colon: only the first one ends the code.
        self.assertEqual(Target("tlg:a:b").value, "a:b")

    def test_resolved_targets_compare_by_source_kind_and_value(self):
        self.assertEqual(Target("ins:example_user", kind="user"), Target("ins:example_user", kind="user"))
        self.assertNotEqual(Target("ins:example_user", kind="user"), Target("ins:example_user"))
        self.assertEqual(str(Target("tlg:", kind="account")), "tlg")

    def test_events_carry_the_same_fields_as_before(self):
        import msgspec

        encoded = msgspec.json.encode(Target("ins:example_user", kind="user"))
        self.assertEqual(msgspec.json.decode(encoded),
                         {"source": "ins", "kind": "user", "value": "example_user", "query": False})
        self.assertEqual(msgspec.json.decode(encoded, type=Target), Target("ins:example_user", kind="user"))


class DigTest(unittest.TestCase):
    PAGE = {"data": {"user": {"edges": [{"node": {"id": 1, "media": [{"u": "a"}, {"u": "b"}]}},
                                        {"node": {"id": 2, "media": [{"u": "c"}]}},
                                        {"other": True}]}},
            "next": None, "alt": "z"}

    def test_keys_indexes_and_projections(self):
        self.assertEqual(dig(self.PAGE, "data.user.edges[0].node.id"), 1)
        self.assertEqual(dig(self.PAGE, "data.user.edges[-1].other"), True)
        self.assertEqual(dig(self.PAGE, "data.user.edges[].node.id"), [1, 2])
        self.assertEqual(dig(self.PAGE, "data.user.edges[].node.media[].u"), ["a", "b", "c"])

    def test_missing_is_none_or_empty(self):
        self.assertIsNone(dig(self.PAGE, "data.nobody.id"))
        self.assertIsNone(dig(self.PAGE, "data.user.edges[9]"))
        self.assertEqual(dig(self.PAGE, "data.nobody[].id"), [])
        self.assertIsNone(dig(["x"], "a.b"))

    def test_alternatives_take_the_first_that_is_not_empty(self):
        self.assertEqual(dig(self.PAGE, "next || alt"), "z")
        self.assertEqual(dig(self.PAGE, "data.user.edges[].node.id || alt"), [1, 2])

    def test_a_bad_bracket_is_a_source_error(self):
        with self.assertRaisesRegex(SourceError, r"can't read '\[x\]'"):
            dig(self.PAGE, "data[x]")


class PagesTest(unittest.TestCase):
    """`Site.pages`: cursors read and sent, the end of a listing, and the engine's resume."""

    API = "https://api.example.com/feed"

    def site(self, book, seen=None):
        def answer(url, request):
            params = request.get("params") or {}
            return book[params.get("cursor") or params.get("after")]

        return FakeSite(Target("exm:example_user", kind="user"), json={self.API: answer}, seen=seen)

    def test_follows_a_dig_path_and_stops_on_a_repeat(self):
        book = {None: {"n": 1, "next": "b"}, "b": {"n": 2, "next": "c"}, "c": {"n": 3, "next": "b"}}
        self.assertEqual([page["n"] for page in self.site(book).pages(self.API, cursor="next")], [1, 2, 3])

    def test_stops_on_an_empty_cursor_or_an_empty_page(self):
        self.assertEqual(len(list(self.site({None: {"n": 1, "next": ""}}).pages(self.API, cursor="next"))), 1)
        book = {None: {"n": 1, "next": "b"}, "b": {}}
        self.assertEqual(len(list(self.site(book).pages(self.API, cursor="next"))), 1)

    def test_params_a_cursor_function_and_a_send_function(self):
        book = {None: {"n": 1, "info": {"more": True, "end": "x"}},
                "x": {"n": 2, "info": {"more": False, "end": "y"}}}

        def cursor(page):
            return page["info"]["end"] if page["info"]["more"] else None

        site = self.site(book)
        pages = list(site.pages(self.API, params={"count": 2}, cursor=cursor,
                                send=lambda at: {"after": at}))
        self.assertEqual([page["n"] for page in pages], [1, 2])
        sent = [request[2]["params"] for request in site._seen.requests]
        self.assertEqual(sent, [{"count": 2}, {"count": 2, "after": "x"}])

    def test_resumes_from_the_listings_cursor_and_reports_the_page_it_reads(self):
        book = {None: {"n": 1, "next": "b"}, "b": {"n": 2, "next": "c"}, "c": {"n": 3, "next": None}}
        site = self.site(book)
        site.listing = Listing("b")
        pages = site.pages(self.API, cursor="next")
        self.assertEqual(next(pages)["n"], 2)
        self.assertEqual(site.listing.cursor, "b")
        self.assertEqual(next(pages)["n"], 3)
        self.assertEqual(site.listing.cursor, "c")
        # Only the first `pages` of a category resumes.
        self.assertEqual([page["n"] for page in site.pages(self.API, cursor="next")], [1, 2, 3])

    def test_a_refused_cursor_starts_from_the_newest(self):
        book = {None: {"n": 1, "next": None}, "stale": NotFound("HTTP 404", status=404)}

        def answer(url, request):
            found = book[(request.get("params") or {}).get("cursor")]
            return found

        site = FakeSite(Target("exm:example_user", kind="user"), json={self.API: answer})
        site.listing = Listing("stale", label="exm:example_user: posts")
        self.assertEqual([page["n"] for page in site.pages(self.API, cursor="next")], [1])
        self.assertTrue(site.listing.refused)


MANIFEST_HEAD = '<?xml version="1.0"?>\n<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>'


def representation(mime, url, *, height=None, bandwidth=1000):
    size = f' height="{height}"' if height else ""
    return (f'<Representation mimeType="{mime}"{size} bandwidth="{bandwidth}">'
            f"<BaseURL>{url}</BaseURL></Representation>")


def manifest(*videos, audios=()):
    sets = f'<AdaptationSet>{"".join(videos)}</AdaptationSet>'
    if audios:
        sets += f'<AdaptationSet>{"".join(audios)}</AdaptationSet>'
    return MANIFEST_HEAD + sets + "</Period></MPD>"


# Meta serves each quality as one representation; some list the same height twice, told apart only
# by bandwidth.
INSTAGRAM = manifest(
    representation("video/mp4", "https://cdn.example/360.mp4", height=640, bandwidth=168170),
    representation("video/mp4", "https://cdn.example/1080.mp4", height=1920, bandwidth=1160975),
    representation("video/mp4", "https://cdn.example/720.mp4", height=1280, bandwidth=825291),
    audios=[representation("audio/mp4", "https://cdn.example/a64.mp4", bandwidth=64353),
            representation("audio/mp4", "https://cdn.example/a95.mp4", bandwidth=95480)])


class DashTest(unittest.TestCase):
    """`File.dash`: a DASH manifest's best video and audio, as one file the engine joins."""

    def test_the_highest_video_and_audio_are_chosen(self):
        file = File.dash(INSTAGRAM)
        self.assertEqual((file.url, file.audio), ("https://cdn.example/1080.mp4", "https://cdn.example/a95.mp4"))

    def test_equal_heights_are_told_apart_by_bandwidth(self):
        text = manifest(representation("video/mp4", "https://cdn.example/low.mp4", height=1280, bandwidth=688558),
                        representation("video/mp4", "https://cdn.example/high.mp4", height=1280, bandwidth=2633777),
                        audios=[representation("audio/mp4", "https://cdn.example/a.mp4")])
        self.assertEqual(File.dash(text).url, "https://cdn.example/high.mp4")

    def test_the_joined_file_is_webm_only_when_both_tracks_are(self):
        from flnd.job import _extension

        webm = manifest(representation("video/webm", "https://cdn.example/v.webm", height=720),
                        audios=[representation("audio/webm", "https://cdn.example/a.webm")])
        mixed = manifest(representation("video/webm", "https://cdn.example/v.webm", height=720),
                         audios=[representation("audio/mp4", "https://cdn.example/a.mp4")])
        self.assertEqual(_extension(File.dash(webm))[0], ".webm")
        self.assertEqual(_extension(File.dash(mixed))[0], ".mp4")

    def test_segments_a_missing_track_relative_urls_or_junk_give_none(self):
        for element in ('<SegmentTemplate media="seg-$Number$.m4s"/>', "<SegmentList/>", "<SegmentTimeline/>"):
            self.assertIsNone(File.dash(INSTAGRAM.replace("</AdaptationSet>", element + "</AdaptationSet>", 1)))
        self.assertIsNone(File.dash(manifest(representation("video/mp4", "https://cdn.example/v.mp4", height=720))))
        # BaseURL ending in / or containing $ is unusable as a progressive file.
        dir_url = manifest(representation("video/mp4", "https://cdn.example/vid/", height=720),
                           audios=[representation("audio/mp4", "https://cdn.example/a.mp4")])
        self.assertIsNone(File.dash(dir_url))
        for text in (None, "", "not xml", "<MPD/>", 42):
            self.assertIsNone(File.dash(text), text)
        relative = INSTAGRAM.replace("https://cdn.example/1080.mp4", "1080.mp4")
        self.assertEqual(File.dash(relative).url, "https://cdn.example/720.mp4")

    def test_an_entity_bomb_and_an_external_entity_are_never_expanded(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
                + "".join(f'<!ENTITY lol{n} "{("&lol" + (str(n - 1) if n > 1 else "") + ";") * 10}">'
                          for n in range(1, 10))
                + ']><MPD xmlns="urn:mpeg:dash:schema:mpd:2011">&lol9;</MPD>')
        self.assertIsNone(File.dash(bomb))
        text = ('<?xml version="1.0"?><!DOCTYPE m [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'
                + INSTAGRAM.split("\n", 1)[1].replace("https://cdn.example/1080.mp4", "&secret;"))
        self.assertNotIn("root", repr(File.dash(text)))


class LoginTest(unittest.TestCase):
    def test_cookies_is_logged_in_when_one_of_its_cookies_is_there(self):
        login = cookies("sessionid", "sid_tt")
        target_ = Target("exm:", kind="account")
        self.assertTrue(login.check(FakeSite(target_, cookies={"sid_tt": "x"})))
        self.assertFalse(login.check(FakeSite(target_, cookies={"other": "x"})))
        self.assertEqual((login.methods, login.required), (("browser", "file"), False))
        self.assertTrue(cookies("token", required=True).required)

    def test_cookies_needs_a_name(self):
        with self.assertRaisesRegex(SourceError, "cookie names"):
            Cookies()

    def test_a_custom_login_is_required_by_default(self):
        class Code(Login):
            methods = ("phone",)

            def start(self, site, how):
                site.secrets["session"] = site.ask("Code")

            def check(self, site):
                return "session" in site.secrets

        self.assertTrue(Code().required)
        self.assertEqual(Code.methods, ("phone",))


class SignTest(unittest.TestCase):
    def test_the_example_signs_its_own_requests_from_the_login(self):
        request = Request("GET", "https://api.example.com/users/example_user", {}, None, {"token": "t"})
        RoutedExample().sign(request)
        self.assertEqual(request.headers, {"Authorization": "Bearer t"})


class CheckTest(unittest.TestCase):
    """What a source's class is checked for when it is made: each mistake names the attribute."""

    def make(self, **attributes):
        body = {"code": "exm", "domains": ("example.com",), "api": 1,
                "user": target("exm:{user}")(lambda self, site, user: None), **attributes}
        return type("Made", (Source,), {name: value for name, value in body.items() if value is not ...})

    def refused(self, message, **attributes):
        with self.assertRaisesRegex(SourceError, message):
            self.make(**attributes)

    def test_a_good_source_and_its_defaults(self):
        made = self.make()
        self.assertEqual((made.name, made.login, made.pace, made.folder, made.filename, made.key),
                         ("Made", None, 0.5, "{category}", "{post}_{num}", "{post}_{num}"))
        self.assertEqual(API, 1)

    def test_code(self):
        self.refused(r"^Made.code: missing; set code", code=...)
        self.refused(r"^Made.code: 'Ex M'", code="Ex M")

    def test_domains(self):
        self.refused(r"^exm.domains: set domains", domains="example.com")
        self.refused(r"^exm.domains: set domains", domains=("https://example.com/",))

    def test_api(self):
        self.refused(r"^exm.api: missing; set api = 1", api=...)
        with self.assertRaises(SourceError) as caught:
            self.make(api=2)
        self.assertEqual((str(caught.exception), caught.exception.hint),
                         ("exm needs source API 2; this flnd has 1", "update flnd"))
        self.refused(r"^exm.api: 0; set api = 1", api=0)

    def test_a_route(self):
        self.refused(r"^exm: no route; mark a method with @target", user=None)

    def test_templates_name_known_fields(self):
        with self.assertRaises(SourceError) as caught:
            self.make(folder="{target}/{usr}", fields=("user_name",))
        self.assertEqual(str(caught.exception), "exm.folder: unknown field {usr}; fields are source, target, "
                         "category, post, num, count, index, extension, path, date, query, user_name")
        self.assertEqual(caught.exception.hint, "list a post field in exm.fields to use it")
        self.refused(r"^exm.filename: unknown field \{id\}", filename="{id}")
        self.refused(r"^exm.folder: '../x' leaves the target's folder", folder="../x")
        self.refused(r"^exm.folder: '/x' leaves", folder="/x")
        self.make(folder="{category}/{album}", fields=("album",))

    def test_a_category_or_route_template(self):
        route = target("exm:{user}", folder="{nope}")(lambda self, site, user: None)
        self.refused(r"^exm.user.folder: unknown field \{nope\}", user=route)

    def test_a_field_may_not_be_the_engines(self):
        self.refused(r"^exm.fields: post is the engine's", fields=("post",))

    def test_login_pace_limits_and_scroll(self):
        self.refused(r"^exm.login: set login = cookies", login=("sessionid",))
        self.refused(r"^exm.pace: -1", pace=-1)
        self.refused(r"^exm.limits: ", limits={"*": 0})
        self.refused(r"^exm.scroll: 'slow'", scroll="slow")
        self.assertEqual(self.make(login=cookies("sid"), limits={"*": 3}).limits, {"*": 3})

    def test_old_api_names_say_the_new_spelling(self):
        self.refused(r"^exm.needs: source API 1 spells it login = cookies", needs=("sid",))
        self.refused(r"^exm.concurrency: source API 1 spells it limits", concurrency=3)
        self.refused(r"^exm.prepare: source API 1 spells it sign", prepare=lambda self, r, c: r)

    def test_a_route_with_categories_returns_its_context(self):
        def user(self, site, user):
            yield user

        route = target("exm:{user}")(user)
        route.category(lambda self, site, profile: None)
        self.refused(r"^exm.user: a route with categories returns what they receive, but it yields", user=route)

    def test_a_code_pattern_starts_with_the_sources_code(self):
        self.refused(r"pattern '\{user\}' must start with the source's code; write it exm:\{user\}",
                     user=target("{user}")(lambda self, site, user: None))
        self.refused(r"pattern 'ins:\{user\}' must start with the source's code; write it exm:\{user\}",
                     user=target("ins:{user}")(lambda self, site, user: None))
        self.refused(r"write it exm\?\{words\}", user=target("?{words}")(lambda self, site, words: None))


if __name__ == "__main__":
    unittest.main()
