"""The web plugin (src/plugins/web.py): any link as a file, a stream, or a page's media.

Everything runs on `FakeCtx`: each link's HEAD and GET answers are `Reply`s. No network.
"""

import asyncio
import unittest
import warnings

import no_profiles  # noqa: F401

from flnd.plugin import NotFound, Target
from flnd.plugins.web import Web, name_of
from flnd.testing import FakeCtx, Reply, check, collect

SITE = "https://example.com"
CDN = "https://cdn.example.com"
PAGE = f"{SITE}/watch/42"
HTML = "text/html; charset=utf-8"
# A link's password, which nothing flnd shows may hold.
SECRET = "example_secret"
# A page with each kind of media: a structured og:video (its https copy taken), an embed
# player left out, a relative `<video src>`, an HLS `<source>`, two og:images (one twice),
# a `blob:` source, and a repeat of the og:video.
MEDIA_PAGE = f"""<html><head>
<meta property="og:video" content="http://cdn.example.com/v/clip.mp4">
<meta property="og:video:secure_url" content="{CDN}/v/clip.mp4">
<meta property="og:video:type" content="video/mp4">
<meta property="og:video" content="{SITE}/embed/42">
<meta property="og:video:type" content="text/html">
<meta property="og:image" content="{CDN}/i/poster.jpg">
<meta property="og:image:url" content="{CDN}/i/poster.jpg">
<meta name="og:image" content="/i/second.png">
</head><body>
<video src="/v/inline.webm"><source src="{CDN}/hls/master.m3u8" type="application/x-mpegURL">
</video><video src="blob:{SITE}/1"></video><video src="{CDN}/v/clip.mp4"></video>
</body></html>"""


def answers(url, *, head=None, get=None):
    """`url` answering HEAD with `head` and GET with `get`; a reply left None isn't routed."""
    def reply(request):
        given = head if request.method == "HEAD" else get
        if given is None:
            raise AssertionError(f"{request.method} {url} wasn't expected")
        return given
    return {url: reply}


def saved(text, **routes):
    """What `text` saves: (path, key) pairs, the files, and the requests made."""
    ctx = FakeCtx(Web, text, routes=routes.get("routes", {}))
    found = collect(Web, text, ctx=ctx)
    return ([(item.path, item.key) for item in found], [item.file for item in found],
            [(method, url) for method, url, _ in ctx.seen.requests])


class RoutesTest(unittest.TestCase):
    def test_every_form(self):
        for text, expected in [
            (f"{CDN}/v/clip.mp4", f"web:{CDN}/v/clip.mp4"),
            (f"web:{PAGE}?t=1#at", f"web:{PAGE}?t=1#at"),
            ("HTTP://Example.com/a/all", "web:HTTP://Example.com/a/all"),
            # Not a link: the resolver says which plugin to name instead.
            ("web:example_user", None), ("example.com/clip.mp4", None),
            (f"{SITE}/a b", None),
        ]:
            with self.subTest(text=text):
                found = Web.resolve(text)
                self.assertEqual(found and found.text, expected)
                if found:
                    self.assertEqual(found.kind, "page")

    def test_the_declarations(self):
        self.assertEqual(check(Web), [Target(f"web:{SITE}/clip.mp4", kind="page"),
                                      Target(f"web:{PAGE}", kind="page")])
        (page,) = Web.routes
        self.assertTrue(page.single)
        self.assertEqual((Web.domains, Web.login, Web.categories), ((), None, ()))

    def test_names(self):
        for link, expected in [(f"{CDN}/v/clip.mp4?x=1", "clip"), (f"{SITE}/watch/42/", "42"),
                               (f"{SITE}/a%20b.tar.gz", "a b.tar"), (f"{SITE}/", "example.com"),
                               # A blank or dot-only stem never names a file: the host does.
                               (f"{SITE}/.hidden", "example.com"),
                               (f"{SITE}/%2e%2e.mp4", "example.com"),
                               (f"{SITE}/a/%2E%2e", "example.com"),
                               (f"{SITE}/a/..", "example.com"), (f"{SITE}/.mp4", "example.com"),
                               (f"{SITE}/%20.mp4", "example.com"), ("https://[::1]/", "[--1]")]:
            with self.subTest(link=link):
                self.assertEqual(name_of(link), expected)


class LinkTest(unittest.TestCase):
    def test_a_file_or_playlist_by_its_extension_asks_nothing(self):
        for link, path, playlist in [
            (f"{CDN}/v/clip.MP4?sig=1", "clip.mp4", None),
            (f"{CDN}/hls/master.m3u8?t=2", "master.mp4", f"{CDN}/hls/master.m3u8?t=2"),
            (f"{CDN}/dash/manifest.mpd", "manifest.mp4", f"{CDN}/dash/manifest.mpd"),
        ]:
            with self.subTest(link=link):
                placed, (file,), asked = saved(link)
                self.assertEqual(placed, [(path, f"{link}_0")])
                self.assertEqual(file.playlist, playlist)
                self.assertEqual(asked, [])

    def test_a_link_without_one_is_read_by_its_content_type(self):
        link = f"{SITE}/download?id=3"
        for head, path, playlist, size in [
            (Reply(headers={"Content-Type": "video/mp4", "Content-Length": "2048"}),
             "download.mp4", None, 2048),
            (Reply(headers={"Content-Type": "application/vnd.apple.mpegurl"}), "download.mp4",
             link, None),
            (Reply(headers={"Content-Type": "application/dash+xml; profiles=x"}),
             "download.mp4", link, None),
            # No type: a file, named once its bytes are seen.
            (Reply(), "download", None, None),
        ]:
            with self.subTest(head=head.headers):
                placed, (file,), asked = saved(link, routes=answers(link, head=head))
                self.assertEqual(placed, [(path, f"{link}_0")])
                self.assertEqual((file.playlist, file.size), (playlist, size))
                self.assertEqual(asked, [("HEAD", link)])

    def test_a_page_gives_its_videos_and_images_once(self):
        page = Reply(MEDIA_PAGE, url=f"{SITE}/watch/42/")
        placed, files, asked = saved(PAGE, routes=answers(
            PAGE, head=Reply(headers={"Content-Type": HTML}), get=page))
        self.assertEqual([url for url, _ in placed], [
            "42.mp4", "42.webm", "42_2.mp4", "42.jpg", "42.png"])
        self.assertEqual([file.url or file.playlist for file in files], [
            f"{CDN}/v/clip.mp4", f"{SITE}/v/inline.webm", f"{CDN}/hls/master.m3u8",
            f"{CDN}/i/poster.jpg", f"{SITE}/i/second.png"])
        # A playlist's segments are fetched with the page, where it landed, as the Referer.
        self.assertEqual(files[2].headers, {"Referer": f"{SITE}/watch/42/"})
        self.assertEqual(asked, [("HEAD", PAGE), ("GET", PAGE)])

    def test_the_post(self):
        ctx = FakeCtx(Web, f"{CDN}/v/clip.mp4#t=3")
        (item,) = collect(Web, f"{CDN}/v/clip.mp4#t=3", ctx=ctx)
        # The fragment is no part of the post: the server never sees it.
        self.assertEqual(item.post.id, f"{CDN}/v/clip.mp4")
        self.assertEqual(item.post.headers, {"Referer": f"{CDN}/v/clip.mp4"})
        self.assertEqual((item.post.fields["name"], item.date), ("clip", None))
        self.assertTrue(item.post.lazy)

    def test_the_post_holds_no_credential(self):
        """The post (and so the history key) and the Referer drop a link's `user:pw@`, and an
        empty path is `/`; the fetch still goes to the link as written."""
        for link, post in [("https://example_user:pw@example.com/v/clip.mp4#t=1",
                            f"{SITE}/v/clip.mp4"),
                           ("https://example.com", f"{SITE}/"),
                           ("https://example_user@example.com:8080?a=1", f"{SITE}:8080/?a=1")]:
            with self.subTest(link=link):
                (item,) = collect(Web, link, ctx=FakeCtx(Web, link, routes=answers(
                    link.split("#")[0], head=Reply(headers={"Content-Type": "video/mp4"}))))
                self.assertEqual((item.post.id, item.key), (post, f"{post}_0"))
                self.assertEqual(item.post.headers, {"Referer": post})
                self.assertEqual(item.file.url, link.split("#")[0])

    def test_a_dot_name_is_named_by_the_host(self):
        # `.mp4` is still an mp4, so nothing is asked; the kit, like the engine, reads no
        # extension in a name that is all one, so its bytes give it one when it is saved.
        for link, path in [(f"{SITE}/%2e%2e.mp4", "example.com.mp4"),
                           (f"{SITE}/.mp4", "example.com"), (f"{SITE}/%20.mp4", "example.com.mp4")]:
            with self.subTest(link=link):
                placed, _, asked = saved(link)
                self.assertEqual(([path for path, _ in placed], asked), ([path], []))

    def test_no_credential_in_any_error_log_or_warning(self):
        """A link's `user:pw@` is never in what flnd shows: an error, a log line, a warning."""
        link = f"https://example_user:{SECRET}@example.com/media"
        page = f"https://example_user:{SECRET}@example.com/page"
        html = Reply(headers={"Content-Type": HTML})
        for about, target, head, get in [
            ("HEAD 404", link, Reply(status=404), None),
            ("HEAD 410", link, Reply(status=410), None),
            *[(f"Range GET {status}", link, Reply(status=405), Reply(status=status))
              for status in (403, 404, 416, 500)],
            ("page GET 403", page, html, Reply(status=403)),
            ("page with no media", page, html, Reply("<p>words</p>", url=page)),
        ]:
            with self.subTest(about), warnings.catch_warnings(record=True) as warned:
                warnings.simplefilter("always")
                ctx = FakeCtx(Web, target, routes=answers(target, head=head, get=get))
                with self.assertRaises(Exception) as raised:
                    asyncio.run(files(ctx))
                shown = [str(raised.exception), getattr(raised.exception, "hint", ""),
                         *map(repr, ctx.seen.logs), *(str(item.message) for item in warned)]
                self.assertFalse([line for line in shown if SECRET in line], shown)

    def test_a_stream_on_a_page_has_the_landed_page_as_its_referer(self):
        page = f"https://example_user:{SECRET}@example.com/page"
        landed = f"https://example_user:{SECRET}@example.com/landed/"
        _, (stream,), _ = saved(page, routes=answers(
            page, head=Reply(headers={"Content-Type": HTML}),
            # The page without its `user:pw@` is still the page, so no media.
            get=Reply(f'<video src="m.m3u8"></video><video src="{SITE}/page"></video>',
                      url=landed)))
        self.assertEqual(stream.playlist, f"{landed}m.m3u8")
        self.assertEqual(stream.headers, {"Referer": f"{SITE}/landed/"})

    def test_an_html_206_is_a_page_read_whole(self):
        link = f"{SITE}/media"

        def reply(request):
            if request.method == "HEAD":
                return Reply(status=405)
            if (request.headers or {}).get("Range"):
                return Reply("<", status=206, headers={"Content-Type": HTML})
            return Reply('<video src="/a.mp4"></video>')

        _, found, asked = saved(link, routes={link: reply})
        self.assertEqual([file.url for file in found], [f"{SITE}/a.mp4"])
        self.assertEqual(asked, [("HEAD", link), ("GET", link), ("GET", link)])

    def test_a_server_refusing_head_gets_one_byte(self):
        """A refused HEAD (S3 and CloudFront presigned links answer 403) gets a GET of one byte,
        never the whole body: a type that isn't HTML is the file, HTML a page."""
        link = f"{SITE}/media"
        for status, get, expected, size in [
            (405, Reply(b"\x00", status=206, headers={"Content-Type": "video/mp4",
                                                       "Content-Range": "bytes 0-0/4096"}),
             [link], 4096),
            (403, Reply(b"\x00", status=206, headers={"Content-Range": "bytes 0-0/*"}),
             [link], None),
            (501, Reply(b"\x00\x00\x00\x18ftypmp42", headers={"Content-Type": "video/mp4"}),
             [link], None),
            (405, Reply(MEDIA_PAGE.split("<body>")[0] + "</html>"),
             [f"{CDN}/v/clip.mp4", f"{CDN}/i/poster.jpg", f"{SITE}/i/second.png"], None),
        ]:
            with self.subTest(status=status, get=get.status):
                ctx = FakeCtx(Web, link, routes=answers(link, head=Reply(status=status), get=get))
                found = collect(Web, link, ctx=ctx)
                self.assertEqual([item.file.url for item in found], expected)
                self.assertEqual(found[0].file.size, size)
                # A 5xx HEAD is tried again first, as the engine tries every 5xx.
                *heads, last = [(method, url, sent["headers"].get("Range"))
                                for method, url, sent in ctx.seen.requests]
                self.assertEqual(set(heads), {("HEAD", link, None)})
                self.assertEqual(last, ("GET", link, "bytes=0-0"))

    def test_what_a_page_gives(self):
        for about, body, landed, expected in [
            ("blank, stray and self links add nothing", """
             <meta property="og:video:type" content="video/mp4">
             <meta property="og:image:width" content="100">
             <meta property="og:video" content="  "><meta property="og:image" content="#">
             <video src=""></video><video src="#"></video><video src="  "></video>
             <video src="/watch/42#t=1"></video><video src="/landed/"></video>""",
             f"{SITE}/landed/", []),
            ("a player with a parameter is left out", """
             <meta property="og:video" content="/embed/42">
             <meta property="og:video:type" content="text/html; charset=utf-8">""", "", []),
            ("a fragment makes no second file", """
             <video src="/a.mp4"></video><video src="/a.mp4#t=1"></video>""", "",
             [f"{SITE}/a.mp4"]),
            ("relative links join where the page landed", '<video src="a.mp4"></video>',
             f"{SITE}/v/42/", [f"{SITE}/v/42/a.mp4"]),
            ("og:*:url starts an item", """
             <meta property="og:image" content="/1.jpg">
             <meta property="og:image:url" content="/2.jpg">""", "",
             [f"{SITE}/1.jpg", f"{SITE}/2.jpg"]),
            ("a secure_url before its og:video is one video", f"""
             <meta property="og:video:secure_url" content="{CDN}/v.mp4">
             <meta property="og:video" content="http://cdn.example.com/v.mp4">""", "",
             [f"{CDN}/v.mp4"]),
            ("a bad secure_url keeps the og:video", """
             <meta property="og:video" content="/v.mp4">
             <meta property="og:video:secure_url" content="javascript:alert(1)">""", "",
             [f"{SITE}/v.mp4"]),
            ("<base href> is honoured", f"""
             <base href="{CDN}/b/"><video src="c.mp4"></video>""", "", [f"{CDN}/b/c.mp4"]),
            ("a <base href> not on the web is not", """
             <base href="javascript:x/"><video src="c.mp4"></video>""", "",
             [f"{SITE}/watch/c.mp4"]),
        ]:
            with self.subTest(about):
                _, found, _ = saved(PAGE, routes=answers(
                    PAGE, head=Reply(headers={"Content-Type": HTML}),
                    get=Reply(f"<html><head>{body}</head></html>", url=landed)))
                self.assertEqual([file.url or file.playlist for file in found], expected)

    def test_nothing_to_save(self):
        """A gone link and a page with no media fail the post, never the run."""
        for link, routes in [
            (f"{SITE}/gone", answers(f"{SITE}/gone", head=Reply(status=404))),
            (f"{SITE}/went", answers(f"{SITE}/went", head=Reply(status=410))),
            (f"{SITE}/text", answers(f"{SITE}/text", head=Reply(headers={"Content-Type": HTML}),
                                     get=Reply("<p>words</p>"))),
        ]:
            with self.subTest(link=link):
                self.assertEqual(saved(link, routes=routes)[0], [])
                with self.assertRaises(NotFound):
                    asyncio.run(files(FakeCtx(Web, link, routes=routes)))


async def files(ctx):
    """The files of the one post `ctx`'s target lists."""
    async for post in Web().page(ctx, ctx.target.value):
        return await post.files()


if __name__ == "__main__":
    unittest.main()
