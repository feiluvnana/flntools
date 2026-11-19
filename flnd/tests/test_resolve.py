"""The resolver (`flnd.engine.resolve`): one table of texts and what they become."""

import subprocess
import sys
import unittest
from unittest import mock

from fakes import EngineCase, noise
from test_library import LibraryCase, example_source

from flnd.engine import crawl, store
from flnd.engine.resolve import Manifest, Resolved, resolve
from flnd.engine.run import Engine
from flnd.errors import PluginError, UsageError
from flnd.plugin import Target, route
from flnd.plugins import _manifest
from flnd.plugins.web import Web
from flnkit import net

BUILT_IN = {
    "exm": {
        "domains": ["example.com"],
        # The second rule never speaks for a link the first one also refuses.
        "unsupported": {"example.com/explore": "explore pages aren't targets",
                        "example.com/{x:stories}": "stories aren't targets",
                        "example.com/{x:st.*}": "never shown"},
        "routes": [
            {"name": "braced", "patterns": ["exm:b\\{{id:\\d+}\\}"]},
            {"name": "post", "single": True,
             "patterns": ["exm:p/{code:[\\w-]+}", "example.com/p/{code:[\\w-]+}"]},
            {"name": "album", "single": True, "patterns": ["exm:a/{id:\\d+}"],
             "categories": [{"name": "photo", "own": True}, {"name": "archive", "own": True}]},
            {"name": "permalink", "single": True, "patterns": [
                ["example.com/permalink.php?story_fbid={post:\\w+}&id={user:\\d+}",
                 "{user}_{post}"]]},
            {"name": "search", "patterns": ["exm?{words}"]},
            {"name": "list", "discovers": True, "patterns": ["exm:list/{list}"]},
            {"name": "user", "patterns": ["exm:{user}", "example.com/{user}"],
             "categories": [{"name": "posts", "own": True}, {"name": "reels", "own": True},
                            {"name": "tagged", "own": False}]},
        ],
    },
    "exc": {
        "domains": ["t.example"],
        "routes": [
            {"name": "private", "patterns": [["t.example/c/{chat:\\d+}", "-100{chat}"]]},
            {"name": "chat", "patterns": ["exc:{chat:.+}"], "shown": "exc:<title>",
             "categories": [{"name": "photo", "own": True}, {"name": "video", "own": True}]},
        ],
    },
    "exs": {"domains": ["s.example"], "routes": [{"name": "user", "patterns": ["exs:{user}"]}]},
    # Domains that start with a prefix word.
    "mde": {"domains": ["mobile.de", "m.example.org"],
            "routes": [{"name": "user", "patterns": ["mobile.de/{x}", "m.example.org/{x}"]}]},
}
INSTALLED = {"version": 1, "plugins": {
    "web": {"routes": [{"name": "page", "patterns": ["web:{url:.+}"]}]},
    "bbb": {"domains": ["dup.example"], "routes": [{"name": "page", "patterns": ["bbb:{x}"]}]},
    "aaa": {"domains": ["dup.example", "example.com"],
            "routes": [{"name": "page", "patterns": ["dup.example/{x}", "example.com/{x}"]}]},
    # A built-in's code is never replaced.
    "exm": {"routes": [{"name": "other", "patterns": ["exm:{x:.+}"]}]},
    # Malformed: disabled alone.
    "bad": {"domains": ["bad.example"], "routes": [{"name": "r", "patterns": ["bad:{a"]}]},
}}
MANIFEST = Manifest(BUILT_IN, INSTALLED)
KNOWN = {"exm:example_creator": ("reels",), "exm?dogs": ("posts",)}
SHORT = {"https://s.example/abc": "https://www.example.com/p/Abc",
         "https://s.example/loop": "https://s.example/elsewhere"}
LINK = "https://example.com/permalink.php?story_fbid=abc&id=4"

R = Resolved


def N(*args, **kwargs):
    """A target whose text names its categories."""
    return Resolved(*args, **kwargs, named=True)


TABLE = [
    # The plugin form, and its categories.
    ("exm:example_user", R("exm", "example_user", "user", ("posts", "reels"))),
    ("  EXM :@example_user ", R("exm", "example_user", "user", ("posts", "reels"))),
    ("exm:example_user/tagged", N("exm", "example_user", "user", ("tagged",))),
    ("exm:example_user/reels, posts", N("exm", "example_user", "user", ("posts", "reels"))),
    ("exm:example_user/all", N("exm", "example_user", "user", ("posts", "reels", "tagged"))),
    ("exm:example_creator", R("exm", "example_creator", "user", ("reels",))),
    ("exm:example_creator/posts", N("exm", "example_creator", "user", ("posts",))),
    ("exm:p/Abc-1", R("exm", "Abc-1", "post", ())),
    ("exm:a/12", R("exm", "12", "album", ("photo", "archive"))),
    ("exm:a/12/archive", N("exm", "12", "album", ("archive",))),
    ("exm:list/example_handle/tagged", N("exm", "example_handle", "list", ("tagged",))),
    ("exm:list/example_handle", R("exm", "example_handle", "list", ())),
    # Another route reads the whole text: no split for user `list`.
    ("exm:list/posts", R("exm", "posts", "list", ())),
    # A discovering route reads the whole text before a split: `mht:author/7` is author `7`.
    ("exm:list/7", R("exm", "7", "list", ())),
    ("exm:b{12}", R("exm", "12", "braced", ())),
    # A trailing `/` or `,` names nothing: the defaults.
    ("exm:example_user/", R("exm", "example_user", "user", ("posts", "reels"))),
    ("exm:example_user/reels,", N("exm", "example_user", "user", ("reels",))),
    ("exm:example_creator/ , ", R("exm", "example_creator", "user", ("reels",))),
    ("exm:p/Abc-1/", R("exm", "Abc-1", "post", ())),
    ("exc:News/", R("exc", "News", "chat", ("photo", "video"))),
    # A catch-all splits only when every token is a category.
    ("exc:News / Updates", R("exc", "News / Updates", "chat", ("photo", "video"))),
    ("exc:News/video", N("exc", "News", "chat", ("video",))),
    ("exc:News/video, Updates", R("exc", "News/video, Updates", "chat", ("photo", "video"))),
    # Searches.
    ("exm?cats", R("exm", "cats", "search", (), query=True)),
    ("exm?cats and dogs/reels", N("exm", "cats and dogs", "search", ("reels",), query=True)),
    ("exm?dogs", R("exm", "dogs", "search", ("posts",), query=True)),
    ("exm?cats/all", N("exm", "cats", "search", ("photo", "archive", "posts", "reels", "tagged"),
                       query=True)),
    ("exm?cats/", R("exm", "cats", "search", (), query=True)),
    # A query is free text: it splits only before a list of categories.
    ("exm?AC/DC", R("exm", "AC/DC", "search", (), query=True)),
    ("exm?url:example.com/x", R("exm", "url:example.com/x", "search", (), query=True)),
    ("exm?cats/stories", R("exm", "cats/stories", "search", (), query=True)),
    # Links: hosts with prefixes dropped, value templates, built-ins before installed ones.
    ("https://m.example.com/example_user/", R("exm", "example_user", "user", ("posts", "reels"))),
    ("example.com/example_user", R("exm", "example_user", "user", ("posts", "reels"))),
    ("https://www.example.com/p/Abc-1?x=1", R("exm", "Abc-1", "post", ())),
    (LINK, R("exm", "4_abc", "permalink", ())),
    ("https://t.example/c/123", R("exc", "-100123", "private", ())),
    ("https://dup.example/x", R("aaa", "x", "page", ())),
    ("https://cdn.other.example/a/all", R("web", "https://cdn.other.example/a/all", "page", ())),
    ("https://s.example/abc", R("exm", "Abc", "post", ())),
    # Repeated prefixes, domains that start with one, and look-alike hosts.
    ("https://www.m.example.com/example_user", R("exm", "example_user", "user",
                                                 ("posts", "reels"))),
    ("https://mbasic.touch.example.com/example_user", R("exm", "example_user", "user",
                                                        ("posts", "reels"))),
    ("https://mobile.de/abc", R("mde", "abc", "user", ())),
    ("https://MOBILE.DE./abc", R("mde", "abc", "user", ())),
    ("https://www.mobile.de/abc", R("mde", "abc", "user", ())),
    ("mobile.de/abc", R("mde", "abc", "user", ())),
    ("https://m.example.org/abc", R("mde", "abc", "user", ())),
    ("https://notexample.com/example_user",
     R("web", "https://notexample.com/example_user", "page", ())),
    ("https://example.com.evil.example/x", R("web", "https://example.com.evil.example/x", "page",
                                             ())),
    # A link's `user:pw@` is never part of the value (so the key): the route reads `link`.
    ("https://example_user:pw@cdn.other.example/a.mp4?x=1",
     R("web", "https://cdn.other.example/a.mp4?x=1", "page", (),
       link="https://example_user:pw@cdn.other.example/a.mp4?x=1")),
    ("web:https://:pw@cdn.other.example/a", R("web", "https://cdn.other.example/a", "page", (),
                                             link="https://:pw@cdn.other.example/a")),
    ("https://example_user:pw@m.example.com/p/Abc-1", R("exm", "Abc-1", "post", ())),
    # Errors: the message, then the hint.
    ("", ("a target can't be empty", "")),
    ("example_user", ('which plugin is "example_user"? Write it as exm:example_user, '
                      "exc:example_user, exs:example_user, mde:example_user, aaa:example_user, "
                      "bbb:example_user, web:example_user", "")),
    # Text without a scheme is a link only on a plugin's host; it never goes to `web`.
    ("cdn.other.example/a", ('which plugin is "cdn.other.example/a"? Write it as '
                             "exm:cdn.other.example/a, exc:cdn.other.example/a, "
                             "exs:cdn.other.example/a, mde:cdn.other.example/a, "
                             "aaa:cdn.other.example/a, bbb:cdn.other.example/a, "
                             "web:cdn.other.example/a", "")),
    ("nop:example_user", ("no plugin named nop", "Plugins: exm, exc, exs, mde, aaa, bbb, web")),
    ("bad:example_user", ("no plugin named bad", None)),
    ("exm:example_user/stories", ("exm has no category stories",
                                  "Categories: posts, reels, tagged")),
    ("exm:list/example_handle/stories", ("exm has no category stories", None)),
    ("exm:p/Abc-1/posts", ("exm:Abc-1 is one post, with no categories", "")),
    ("exm?  ", ("exm: the query is empty", "")),
    ("exs?cats", ("exs has no search", "Write it like exs:<user>")),
    ("exm:", ("exm: write a value after exm:", None)),
    # Nor part of a message.
    ("exs:https://example_user:pw@s.example/x",
     ("exs: can't read 'https://s.example/x'", "Write it like exs:<user>")),
    ("https://example.com/explore", ("exm: explore pages aren't targets", "")),
    # A tie with a route (`example.com/{user}`) goes to the refusal; the first rule speaks.
    ("https://example.com/stories", ("exm: stories aren't targets", "")),
    ("https://example.com/p/Abc-1/stories", R("exm", "Abc-1", "post", ())),
    ("https://t.example/joinchat/x",
     ("exc: can't read https://t.example/joinchat/x",
      "Write it like https://www.t.example/c/<chat>/, exc:<title>")),
    ("https://s.example/loop",
     ("exs: can't read https://s.example/elsewhere (from https://s.example/loop)", None)),
]
MALFORMED = [
    (["bad:user}"], "plugin bad: pattern 'bad:user}': a '}' with no '{'"),
    (["bad:{user"], "plugin bad: pattern 'bad:{user': '{user' isn't a placeholder"),
    (["bad:{1x}"], "plugin bad: pattern 'bad:{1x}': '{1x}' isn't a placeholder"),
    (["bad:{a}-{b}"], "plugin bad: pattern 'bad:{a}-{b}': two placeholders need a value template"),
    ([["bad:{a}"]], "plugin bad: pattern ['bad:{a}']: write a text or a [pattern, template] pair"),
    ([["bad:{a}", "{b}"]], "plugin bad: pattern 'bad:{a}': '{b}' names a placeholder it lacks"),
    (None, "plugin bad: a route with the patterns ['bad:{a}'] has no name"),
]


class ResolveTest(unittest.TestCase):
    def test_table(self):
        followed = []

        def follow(url):
            followed.append(url)
            return SHORT.get(url, url)

        for text, expected in TABLE:
            with self.subTest(text=text):
                if isinstance(expected, Resolved):
                    found = resolve(text, manifest=MANIFEST, known=KNOWN, follow=follow)
                    self.assertEqual(found, expected)
                    continue
                with self.assertRaises(UsageError) as caught:
                    resolve(text, manifest=MANIFEST, known=KNOWN, follow=follow)
                message, hint = expected
                self.assertEqual(str(caught.exception), message)
                if hint is not None:
                    self.assertEqual(caught.exception.hint, hint)
        # A short link is followed once, and what it leads to is never followed again.
        self.assertEqual(followed, ["https://s.example/abc", "https://t.example/joinchat/x",
                                    "https://s.example/loop"])

    def test_the_key_leaves_categories_out(self):
        self.assertEqual(resolve("exm:example_user/tagged", manifest=MANIFEST, known={}).key,
                         "exm:example_user")
        self.assertEqual(resolve("exm?cats/reels", manifest=MANIFEST, known={}).key, "exm?cats")

    def test_a_malformed_entry_names_its_plugin_and_pattern(self):
        for patterns, message in MALFORMED:
            route = {"patterns": ["bad:{a}"]} if patterns is None else {"name": "r",
                                                                         "patterns": patterns}
            entry = {"bad": {"routes": [route]}}
            with self.subTest(patterns=patterns):
                with self.assertRaises(PluginError) as caught:
                    Manifest(entry)
                self.assertEqual(str(caught.exception), message)
                # Installed, it is disabled alone and the rest still loads.
                manifest = Manifest(BUILT_IN, {"plugins": entry})
                self.assertEqual(str(manifest.broken["bad"]), message)
                self.assertEqual(list(manifest.plugins), list(BUILT_IN))
        self.assertEqual(list(MANIFEST.broken), ["bad"])

    def test_the_built_ins_resolve_from_their_manifest(self):
        manifest = Manifest(_manifest.PLUGINS)
        for text, expected in [
            ("ins:example_user/tagged", N("ins", "example_user", "user", ("tagged",))),
            ("loi:12", R("loi", "12", "post", ("archive", "photo"))),
            ("mht?cats/cover", N("mht", "cats", "query", ("cover",), query=True)),
            ("https://www.instagram.com/example_user/",
             R("ins", "example_user", "user",
               ("posts", "reels", "stories", "highlights", "profile"))),
        ]:
            with self.subTest(text=text):
                self.assertEqual(resolve(text, manifest=manifest, known={}), expected)

    def test_resolving_imports_no_plugin(self):
        code = ("import sys\n"
                "from flnd.engine.resolve import Manifest, resolve\n"
                "from flnd.plugins import _manifest\n"
                "manifest = Manifest(_manifest.PLUGINS)\n"
                "for text in ('ins:example_user/reels', 'twt?cats', "
                "'https://www.instagram.com/example_user', 'https://nowhere.example/x'):\n"
                "    try:\n"
                "        resolve(text, manifest=manifest, known={})\n"
                "    except Exception as error:\n"
                "        print(type(error).__name__)\n"
                "print(sorted(n for n in sys.modules if n.startswith('flnd.plugins.') "
                "or n == 'flnd.plugin'))\n")
        found = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               check=True)
        self.assertEqual(found.stdout.split(), ["['flnd.plugins._manifest']"])


class MixedRunTest(LibraryCase):
    """E4: a run of a search and a user keeps the categories each names, and the followed
    user's stop at saved posts."""

    def setUp(self):
        super().setUp()

        async def search(self, ctx, words):
            yield Target("exq:example_creator")

        self.fl.add(example_source(self.listing, code="exq", search=route("exq?{words}")(search)))

    def test_a_search_and_a_user_with_categories_are_both_stored(self):
        self.fl.follow(["exq?words/reels", "exq:example_user/posts"]).wait()
        with store.open(self.home) as db:
            rows = db.targets()
        self.assertEqual((rows["exq:words"].categories, rows["exq:example_user"].categories),
                         (("reels",), ("posts",)))

    def test_a_followed_user_stops_at_saved_posts_in_a_mixed_run(self):
        self.fl.follow("exq:example_user").wait()
        seen = []
        real = crawl.download

        async def download(run, target, tally, options, **kwargs):
            seen.append((str(target), options.stop_at_saved))
            return await real(run, target, tally, options, **kwargs)

        with mock.patch.object(crawl, "download", download):
            self.fl.download(["exq?words", "exq:example_user"]).wait()
        self.assertIn(("exq:example_user", True), seen)


class LinkCredentialTest(EngineCase):
    """PRIVACY: a typed link's `user:pw@` is never stored, logged or shown; the fetch has it."""

    def test_the_fetch_uses_the_link_as_typed_and_nothing_keeps_it(self):
        self.routes["https://cdn.example/a.jpg"] = net.reply(200, content=noise("JPEG"))
        events = []
        engine = Engine(home=self.home, sources={"web": Web}, emit=events.append)
        with self.assertRaises(UsageError) as caught:
            engine.download(["web:https://example_user:SECRET-PW@cdn.example/a b.jpg"])
        report = engine.download(["https://example_user:SECRET-PW@cdn.example/a.jpg"])
        self.assertEqual(str(report.targets[0].target), "web:https://cdn.example/a.jpg")
        self.assertEqual([str(request.url) for request in self.requests],
                         ["https://example_user:SECRET-PW@cdn.example/a.jpg"])
        kept = [str(caught.exception), caught.exception.hint, *self.lines(), *map(str, events),
                *(path.read_bytes().decode("latin-1") for path in self.home.rglob("*")
                  if path.is_file())]
        self.assertFalse([line for line in kept if "SECRET-PW" in line])


if __name__ == "__main__":
    unittest.main()
