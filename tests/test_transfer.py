"""flnd/_transfer.py: one post's file through the source's client, mirrors, DASH and its checks.

Every transfer runs through a real `flnd._net.Client` on `_net.fake`, on a loop of its own
(`asyncio.run`), with `clock.Fake`, so nothing opens a socket or sleeps.
"""

import asyncio
import contextlib
import fnmatch
import io
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from flnd import _net, _transfer

from flnd.source import SiteError, File
from flnd import _time as clock, _tools
from flnd._ui import ui
from flnd.errors import Error, NetworkError


def broken():
    """A body that sends one chunk, then drops the connection."""
    yield b"half"
    raise ConnectionResetError("connection reset")


class NetCase(unittest.TestCase):
    def setUp(self):
        self.dest = io.StringIO()
        scope = ui.scope(dest=self.dest, width=300, color=False)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.fake = self.enterContext(clock.use(clock.Fake()))
        # The backoff's jitter at its top, so a wait reads 1, 2, 4 s.
        self.enterContext(mock.patch("flnd._net.random.uniform", lambda low, high: high))
        self.enterContext(_net.fake(lambda request: self.serve(request)))
        self.stop = threading.Event()
        self.requests = []
        self.signatures = []
        self.answers = []

    @property
    def sleeps(self):
        return self.fake.slept

    def serve(self, request):
        self.requests.append(request)
        self.signatures.append(request.headers.get("x-signature"))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def client(self, *answers, prepare=None):
        """A downloading client like a source's, answered by `answers` in turn (the last one again
        and again); a context manager, for the tests' `with` blocks."""
        self.answers = list(answers)
        session = _net.Session(prepare=prepare)
        return contextlib.nullcontext(_net.Client(session, stop=self.stop, pace=False))

    def lines(self):
        return self.dest.getvalue().splitlines()


def save(client, file, dest, **options):
    """`_transfer.save` on a loop of its own."""
    return asyncio.run(_transfer.save(client, file, dest, **options))


def fetch(client, url):
    return asyncio.run(client.fetch(url))


class BackoffTest(NetCase):
    def test_429_is_waited_out_and_sent_again(self):
        with self.client(_net.reply(429), _net.reply(429), _net.reply(200)) as client:
            self.assertEqual(fetch(client, "https://a.example/").status, 200)
        self.assertEqual(self.sleeps, [1.0, 2.0])
        warnings = [line for line in self.lines() if line.startswith("!")]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all(line.startswith("! a.example: rate limited; waiting") for line in warnings))

    def test_retry_after_is_honored(self):
        limited = _net.reply(429, headers={"retry-after": "120"})
        with self.client(limited, _net.reply(200)) as client:
            fetch(client, "https://a.example/")
        self.assertEqual(self.sleeps, [120.0])
        self.assertIn("! a.example: rate limited; waiting 2m", self.lines())

    def test_a_rate_limit_reset_time_is_waited_for(self):
        reset = str(int(clock.wall()) + 600)
        with self.client(_net.reply(429, headers={"x-rate-limit-reset": reset}),
                         _net.reply(200)) as client:
            fetch(client, "https://a.example/")
        self.assertEqual(sum(self.sleeps), 601.0)
        self.assertIn("! a.example: rate limited; waiting 10m 1s", self.lines())

    def test_ctrl_c_ends_the_wait(self):
        self.stop.set()
        with self.client(_net.reply(429), _net.reply(200)) as client, \
                self.assertRaises(KeyboardInterrupt):
            save(client, File("https://cdn.example/a.jpg"),
                         self.tmp / "a.jpg", label="a.jpg")
        self.assertEqual(self.sleeps, [])


class DownloadTest(NetCase):
    def download(self, client, source, name="a.jpg", *, size=None, audio=None, **options):
        return save(client, File(source, size=size, audio=audio),
                            self.tmp / "posts" / name, label=f"posts/{name}", **options)

    def test_atomic_with_a_bar_and_mode_0644(self):
        with self.client(_net.reply(200, content=b"x" * 1536)) as client:
            self.assertTrue(self.download(client, "https://cdn.example/a.jpg", headers={"Referer": "https://site/"}))
        dest = self.tmp / "posts" / "a.jpg"
        self.assertEqual(dest.read_bytes(), b"x" * 1536)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode) & 0o644, 0o644)
        self.assertEqual(list(dest.parent.iterdir()), [dest])
        self.assertEqual(self.requests[0].headers["referer"], "https://site/")

    def test_a_streaming_error_is_retried_and_no_part_is_left(self):
        with self.client(_net.reply(200, chunks=broken()), _net.reply(200, content=b"whole")) as client:
            self.assertTrue(self.download(client, "https://cdn.example/a.jpg"))
        self.assertEqual((self.tmp / "posts" / "a.jpg").read_bytes(), b"whole")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([path.name for path in (self.tmp / "posts").iterdir()], ["a.jpg"])

    def test_a_dead_connection_costs_five_attempts_and_is_raised(self):
        with self.client(ConnectionRefusedError("refused")) as client, self.assertRaises(Error) as caught:
            self.download(client, "https://cdn.example/a.jpg")
        self.assertIsInstance(caught.exception, NetworkError)
        # The first try and 4 more (`flnd._net`'s policy).
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(list((self.tmp / "posts").iterdir()), [])

    def test_a_short_body_is_retried(self):
        short = _net.reply(200, headers={"content-length": "10"}, content=b"12345")
        with self.client(short, _net.reply(200, content=b"1234567890")) as client:
            self.assertTrue(self.download(client, "https://cdn.example/a.jpg"))
        self.assertEqual(len(self.requests), 2)

    def test_a_refusal_raises_with_its_status_and_is_not_retried(self):
        """The job reads the status: a 403 or 410 may be an expired link it can refresh (B6)."""
        with self.client(_net.reply(403)) as client, self.assertRaises(SiteError) as caught:
            self.download(client, "https://cdn.example/a.jpg?oe=signed")
        self.assertEqual(caught.exception.status, 403)
        # The signed query stays out of the message.
        self.assertEqual(str(caught.exception), "GET https://cdn.example/a.jpg: HTTP 403")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((self.tmp / "posts" / "a.jpg").exists())

    def test_mirrors_are_tried_in_order(self):
        with self.client(_net.reply(404), _net.reply(200, content=b"7z")) as client:
            self.assertTrue(self.download(client, ["https://a.example/f.7z", "https://b.example/f.7z"], "f.7z"))
        self.assertEqual([request.url.host for request in self.requests], ["a.example", "b.example"])
        self.assertIn("! mirror 1 failed: GET https://a.example/f.7z: HTTP 404; trying next", self.lines())

    def test_a_failed_mirror_logs_why(self):
        """Fix 9: the warning names the reason, not only the mirror's number."""
        refused = [ConnectionRefusedError("refused")] * 5
        with self.client(*refused, _net.reply(200, content=b"7z")) as client:
            self.assertTrue(self.download(client, ["https://a.example/f.7z", "https://b.example/f.7z"], "f.7z"))
        self.assertIn("! mirror 1 failed: GET https://a.example/f.7z: refused; trying next", self.lines())

    def test_an_incomplete_body_is_still_retried(self):
        """Fix 9: only a page that isn't media fails at once; a cut-off body costs every attempt."""
        short = _net.reply(200, headers={"content-length": "10"}, content=b"12345")
        with self.client(short) as client, self.assertRaises(NetworkError):
            self.download(client, "https://cdn.example/a.jpg")
        self.assertEqual(len(self.requests), 5)

    def test_download_preserves_binary_newlines(self):
        body = b"line\nnext\r\n\n\x00\x1aend\n"
        with self.client(_net.reply(200, content=body)) as client:
            self.assertTrue(self.download(client, "https://cdn.example/a.bin", "a.bin"))
        self.assertEqual((self.tmp / "posts" / "a.bin").read_bytes(), body)

    def test_a_fetch_function_with_progress(self):
        def fetch(dest, progress):
            dest.write_bytes(b"media")
            progress(5, 5)

        with self.client(_net.reply(200)) as client:
            self.assertTrue(self.download(client, fetch, "m.mp4"))
        self.assertEqual((self.tmp / "posts" / "m.mp4").read_bytes(), b"media")
        self.assertEqual(self.requests, [])

    def test_the_second_ctrl_c_stops_a_blocking_fetch_function_at_its_next_report(self):
        """D8: a source's fetch function (tlg's) gets a progress callback that a second Ctrl-C stops."""
        reported, gate, outcome, went_on = threading.Event(), threading.Event(), {}, []

        def fetch(dest, progress):
            dest.write_bytes(b"half")
            progress(4, 8)
            reported.set()
            gate.wait(5)  # blocked, as on a slow download, until the next piece arrives
            progress(8, 8)
            went_on.append(True)

        def run():
            try:
                self.download(client, fetch, "m.mp4")
            except BaseException as error:  # read below: the second Ctrl-C's KeyboardInterrupt
                outcome["error"] = error

        with self.client(_net.reply(200)) as client:
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(reported.wait(5))
            self.stop.set()             # the second Ctrl-C: it stops at its next report
            gate.set()
            thread.join(5)
        self.assertIsInstance(outcome.get("error"), KeyboardInterrupt)
        self.assertEqual(went_on, [])
        # The part stays for the next run to resume; nothing reaches m.mp4.
        self.assertEqual([path.name for path in (self.tmp / "posts").iterdir()], ["m.mp4.part"])

    def test_a_fetch_function_that_gets_a_web_page_is_called_once(self):
        """Fix 9: a fetch function retries every Exception, so `final` is what stops a login wall costing three tries."""
        error = SiteError("got a web page instead of a photo")
        fetch = mock.Mock(side_effect=error)
        with self.client(_net.reply(200)) as client, self.assertRaises(SiteError):
            self.download(client, fetch, "m.mp4")
        fetch.assert_called_once()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(list((self.tmp / "posts").iterdir()), [])

    def test_a_429_on_a_download_pauses_and_retries(self):
        with self.client(_net.reply(429), _net.reply(200, content=b"ok")) as client:
            self.assertTrue(self.download(client, "https://cdn.example/a.jpg"))
        self.assertEqual(sum(self.sleeps), 1.0)

    def test_a_429_during_a_mirror_is_waited_out_on_that_mirror(self):
        with self.client(_net.reply(429), _net.reply(429), _net.reply(200, content=b"ok")) as client:
            self.assertTrue(self.download(client, ["https://a.example/f", "https://b.example/f"]))
        self.assertEqual({request.url.host for request in self.requests}, {"a.example"})
        self.assertFalse(any("mirror 1 failed" in line for line in self.lines()))


def photo(format, size=(32, 24)):
    if format.upper() == "WEBP":
        return b"RIFF\x24\x00\x00\x00WEBPVP8 \x18\x00\x00\x00" + bytes(24)
    if format.upper() == "PNG":
        return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(32)
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + bytes(32)


def chunked(data):
    """A body sent without Content-Length, as a chunked response is."""
    return _net.reply(200, chunks=iter([data]))


class MediaDownloadTest(NetCase):
    def download(self, client, source, name="a.jpg", *, size=None, audio=None, **options):
        return save(client, File(source, size=size, audio=audio),
                            self.tmp / "posts" / name, label=f"posts/{name}", **options)

    def names(self):
        return sorted(path.name for path in (self.tmp / "posts").iterdir())

    def test_a_file_named_by_its_content_is_renamed_onto_that_name(self):
        with self.client(_net.reply(200, content=photo("WEBP"))) as client:
            self.assertEqual(self.download(client, "https://cdn.example/a.jpg", kind="image").path.name, "a.webp")
        self.assertEqual(self.names(), ["a.webp"])

    def test_content_decides_the_extension(self):
        with self.client(_net.reply(200, content=photo("WEBP"))) as client:
            self.assertEqual(self.download(client, "https://cdn.example/a.jpg", kind="image").path, self.tmp / "posts" / "a.webp")
        self.assertEqual(self.names(), ["a.webp"])
        with self.client(_net.reply(200, content=photo("JPEG"))) as client:
            self.assertEqual(self.download(client, "https://cdn.example/b.jpeg", "b.jpeg", kind="image").path.name, "b.jpeg")

    def test_an_mp4_keeps_its_name(self):
        with self.client(_net.reply(200, content=b"\0\0\0\x18ftypisom" + bytes(32))) as client:
            self.assertEqual(self.download(client, "https://cdn.example/v.mp4", "v.mp4", kind="video").path.name, "v.mp4")

    def test_a_web_page_is_requested_once(self):
        """Fix 9: a login wall is the same page on every attempt, so it fails at once instead of three times."""
        page = _net.reply(200, content=b"<!doctype html><title>Log in</title>")
        with self.client(page) as client, self.assertRaises(SiteError) as caught:
            self.download(client, "https://cdn.example/a.jpg", kind="image")
        self.assertEqual(str(caught.exception), "got a web page instead of a photo")
        self.assertEqual(len(self.requests), 1)

    def test_a_web_page_on_a_mirror_is_logged_with_why(self):
        page = _net.reply(200, content=b"<!doctype html><title>Log in</title>")
        with self.client(page, _net.reply(200, content=photo("JPEG"))) as client:
            self.assertTrue(self.download(client, ["https://a.example/a.jpg", "https://b.example/a.jpg"], kind="image"))
        self.assertEqual(len(self.requests), 2)
        self.assertIn("! mirror 1 failed: got a web page instead of a photo; trying next", self.lines())

    def test_files_and_archives_are_never_renamed(self):
        with self.client(_net.reply(200, content=photo("WEBP"))) as client:
            self.assertEqual(self.download(client, "https://cdn.example/f.zip", "f.zip").path.name, "f.zip")


class DashDownloadTest(NetCase):
    VIDEO, AUDIO = "https://cdn.example/v.mp4", "https://cdn.example/a.mp4"

    def serve(self, request):
        self.requests.append(request)
        items = self.bodies[str(request.url)]
        return items.pop(0) if len(items) > 1 else items[0]

    def client(self, **bodies):
        self.bodies = {self.VIDEO: [_net.reply(200, content=b"\0\0\0\x18ftypisomvideo")],
                       self.AUDIO: [_net.reply(200, content=b"\0\0\0\x18ftypisomaudio")], **bodies}
        return super().client(_net.reply(500))

    def fake_ffmpeg(self, *, fail=False):
        calls = []

        def ffmpeg(args):
            calls.append(args[1:])
            if fail:
                return 1, "", "bad container"
            Path(args[-1]).write_bytes(b"\0\0\0\x18ftypisommerged")
            return 0, "", ""

        return _tools.fake({"ffmpeg": ffmpeg}), calls

    def merged(self, client, name="r.mp4"):
        return save(client, File(self.VIDEO, audio=self.AUDIO),
                            self.tmp / "reels" / name, label=f"reels/{name}", kind="video")

    def test_video_and_audio_are_merged_and_both_parts_deleted(self):
        patcher, calls = self.fake_ffmpeg()
        with self.client() as client, patcher:
            saved = self.merged(client)
        self.assertEqual(len(calls), 1)
        self.assertEqual([path.name for path in saved.path.parent.iterdir()], ["r.mp4"])

    def test_merge_temp_files_are_named_part(self):
        """Every file a DASH download writes before its rename matches the part names `flnd._files.sweep` removes."""
        real_replace, moved = os.replace, []

        def spy(src, dst):
            moved.append(Path(src).name)
            return real_replace(src, dst)

        patcher, _ = self.fake_ffmpeg()
        with self.client() as client, patcher, mock.patch.object(os, "replace", spy):
            self.merged(client)
        self.assertTrue(moved)
        self.assertEqual([name for name in moved if not fnmatch.fnmatch(name, "*.part")], [])

    def test_a_second_ctrl_c_before_the_merge_stops_it(self):
        """The merge is ffmpeg's and can't see `stop`, so a second Ctrl-C after the streams stops before it."""
        real = _net.Client.fill

        async def fill(client, url, held, **kwargs):
            await real(client, url, held, **kwargs)
            if url == self.AUDIO:
                self.stop.set()

        patcher, calls = self.fake_ffmpeg()
        with self.client() as client, patcher, \
                mock.patch.object(_net.Client, "fill", fill), \
                self.assertRaises(KeyboardInterrupt):
            self.merged(client)
        self.assertEqual(calls, [])
        # The streams came whole, so their parts stay for the next run; the empty file's goes.
        self.assertEqual(sorted(path.name for path in (self.tmp / "reels").iterdir()),
                         ["r.mp4.audio.part", "r.mp4.video.part"])

    def test_a_container_that_cant_hold_the_streams_fails(self):
        patcher, _ = self.fake_ffmpeg(fail=True)
        with self.client() as client, patcher, self.assertRaises(_tools.RunError):
            self.merged(client, "r.webm")
        self.assertEqual(list((self.tmp / "reels").iterdir()), [])


class FollowTest(NetCase):
    def test_follows_redirects_with_one_head(self):
        def serve(request):
            self.requests.append(request)
            if request.url.host == "vm.tiktok.com":
                return _net.reply(301, headers={"location": "https://www.tiktok.com/@a/video/7301234567890123456"})
            return _net.reply(200)

        with _net.fake(serve):
            self.assertEqual(_net.follow("https://vm.tiktok.com/ZM1/"),
                             "https://www.tiktok.com/@a/video/7301234567890123456")
        self.assertEqual({request.method for request in self.requests}, {"HEAD"})

    def test_fetch_error_keeps_the_status(self):
        self.assertEqual(SiteError("x", status=403).status, 403)


if __name__ == "__main__":
    unittest.main()
