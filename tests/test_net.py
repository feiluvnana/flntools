"""flnd/common/net.py: the source's httpx client, mirrors and atomic downloads, on MockTransport.

Downloads go through an `http.Client` labelled like a source's files client, on `clock.Fake`.
"""

import fnmatch
import io
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from media_samples import needs_ffmpeg, needs_pillow, sample

from flnd.common import files, net
from flnd.source import FetchError
from shared import clock, http, media, theme
from shared._theme import Screen


class Broken(httpx.SyncByteStream):
    """A body that sends one chunk, then drops the connection."""

    def __iter__(self):
        yield b"half"
        raise httpx.ReadError("connection reset")


class NetCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(dest=io.StringIO(), inp=io.StringIO(""), width=300, tty=False, interactive=False)
        scope = theme.scope(screen=self.screen, color="none", unicode=True)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.fake = self.enterContext(clock.use(clock.Fake()))
        # No jitter, so the waits are exact.
        self.enterContext(mock.patch.object(self.fake.random, "uniform", lambda low, high: 1.0))
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

    def raw(self, *answers, sign=lambda request: None, jar=(), user_agent=None):
        """A source's httpx client, answered by `answers` in turn (the last one again and again)."""
        self.answers = list(answers)
        with mock.patch.object(net, "_base_transport", lambda: httpx.MockTransport(self.serve)):
            raw = net.client(user_agent=user_agent, jar=list(jar), sign=sign)
        self.addCleanup(raw.close)
        return raw

    def client(self, *answers, **options):
        """A files client like a source's, over `raw`."""
        raw = self.raw(*answers, **options)
        return http.Client(raw, pace=http.Pace(label="ins"), stop=net.stopping)

    def lines(self):
        return self.screen.dest.getvalue().splitlines()


class ClientTest(NetCase):
    def test_cookies_and_httpx_user_agent(self):
        jar = [{"name": "sid", "value": "1", "domain": ".site.example", "path": "/"}]
        self.raw(httpx.Response(200), jar=jar).get("https://api.site.example/a")
        request = self.requests[0]
        self.assertEqual(request.headers["cookie"], "sid=1")
        self.assertTrue(request.headers["user-agent"].startswith("python-httpx/"))

    def test_source_user_agent(self):
        self.raw(httpx.Response(200), user_agent="Agent/1").get("https://a.example/")
        self.assertEqual(self.requests[0].headers["user-agent"], "Agent/1")

    def test_signing_runs_before_every_attempt(self):
        stamps = iter(range(100))

        def sign(request):
            request.headers["x-signature"] = str(next(stamps))

        with self.client(httpx.ConnectError("refused"), httpx.Response(500), httpx.Response(200),
                         sign=sign) as client:
            client.fetch("https://a.example/")
        self.assertEqual(self.signatures, ["0", "1", "2"])

    def test_a_5xx_waits_1_then_2_seconds_whatever_its_retry_after(self):
        busy = httpx.Response(503, headers={"retry-after": "900"})
        with self.client(busy, busy, httpx.Response(200, json={"ok": 1})) as client:
            self.assertEqual(client.fetch("https://a.example/").json, {"ok": 1})
        self.assertEqual(self.sleeps, [1.0, 2.0])


class BackoffTest(NetCase):
    def test_429_is_waited_out_and_sent_again(self):
        with self.client(httpx.Response(429), httpx.Response(429), httpx.Response(200)) as client:
            self.assertEqual(client.fetch("https://a.example/").status, 200)
        self.assertEqual(sum(self.sleeps), 90.0)
        warnings = [line for line in self.lines() if line.startswith("!")]
        self.assertEqual(warnings, ["! ins: rate limited; waiting 30.0s (attempt 1)",
                                    "! ins: rate limited; waiting 1m (attempt 2)"])

    def test_a_rate_limit_never_stops_and_waits_double_up_to_15_minutes(self):
        answers = [httpx.Response(429)] * 7 + [httpx.Response(200)]
        with self.client(*answers) as client:
            self.assertEqual(client.fetch("https://a.example/").status, 200)
        # 429s are not attempts: eight requests, where three attempts are the limit for errors.
        self.assertEqual(len(self.requests), 8)
        self.assertEqual(sum(self.sleeps), 30 + 60 + 120 + 240 + 480 + 900 + 900)
        self.assertIn("! ins: rate limited; waiting 15m (attempt 7)", self.lines())

    def test_retry_after_is_honored_without_a_cap(self):
        limited = httpx.Response(429, headers={"retry-after": "3600"})
        with self.client(limited, httpx.Response(200)) as client:
            client.fetch("https://a.example/")
        self.assertEqual(sum(self.sleeps), 3600.0)
        self.assertIn("! ins: rate limited; waiting 1h (attempt 1)", self.lines())

    def test_a_rate_limit_reset_time_is_waited_for(self):
        reset = str(int(clock.wall()) + 600)
        with self.client(httpx.Response(429, headers={"x-rate-limit-reset": reset}),
                         httpx.Response(200)) as client:
            client.fetch("https://a.example/")
        self.assertEqual(sum(self.sleeps), 601.0)
        self.assertIn("! ins: rate limited; waiting 10m 1s (attempt 1)", self.lines())

    def test_the_wait_shows_a_countdown_row(self):
        with self.client(httpx.Response(429), httpx.Response(200)) as client:
            client.fetch("https://a.example/")
        self.assertEqual(self.sleeps, [1.0] * 30)
        self.assertTrue(any(line.startswith("✓ ") and line.endswith(" ins: waiting 30.0s") for line in self.lines()),
                        self.lines())

    def test_ctrl_c_ends_the_wait(self):
        net.stopping.set()
        self.addCleanup(net.stopping.clear)
        with self.client(httpx.Response(429), httpx.Response(200)) as client, \
                self.assertRaises(KeyboardInterrupt):
            net.download(client, "https://cdn.example/a.jpg", self.tmp / "a.jpg", label="a.jpg")
        self.assertEqual(self.sleeps, [])


class DownloadTest(NetCase):
    def download(self, client, source, name="a.jpg", **options):
        return net.download(client, source, self.tmp / "posts" / name, label=f"posts/{name}", **options)

    def test_atomic_with_a_bar_and_mode_0644(self):
        with self.client(httpx.Response(200, content=b"x" * 1536)) as client:
            self.assertTrue(self.download(client, "https://cdn.example/a.jpg", headers={"Referer": "https://site/"}))
        dest = self.tmp / "posts" / "a.jpg"
        self.assertEqual(dest.read_bytes(), b"x" * 1536)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode) & 0o644, 0o644)
        self.assertEqual(list(dest.parent.iterdir()), [dest])
        self.assertEqual(self.requests[0].headers["referer"], "https://site/")
        self.assertEqual(self.lines(), ["✓ " + "━" * 20 + " posts/a.jpg " + "1.5 KB"])

    def test_a_streaming_error_is_retried_and_no_part_is_left(self):
        with self.client(httpx.Response(200, stream=Broken()), httpx.Response(200, content=b"whole")) as client:
            self.assertTrue(self.download(client, "https://cdn.example/a.jpg"))
        self.assertEqual((self.tmp / "posts" / "a.jpg").read_bytes(), b"whole")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([path.name for path in (self.tmp / "posts").iterdir()], ["a.jpg"])

    def test_a_dead_connection_costs_three_attempts_and_is_raised(self):
        with self.client(httpx.ConnectError("refused")) as client, self.assertRaises(http.Error) as caught:
            self.download(client, "https://cdn.example/a.jpg")
        self.assertIsInstance(caught.exception.__cause__, httpx.ConnectError)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(list((self.tmp / "posts").iterdir()), [])

    def test_a_short_body_is_retried(self):
        short = httpx.Response(200, headers={"content-length": "10"}, content=b"12345")
        with self.client(short, httpx.Response(200, content=b"1234567890")) as client:
            self.assertTrue(self.download(client, "https://cdn.example/a.jpg"))
        self.assertEqual(len(self.requests), 2)

    def test_a_refusal_raises_with_its_status_and_is_not_retried(self):
        """The job reads the status: a 403 or 410 may be an expired link it can refresh (B6)."""
        with self.client(httpx.Response(403)) as client, self.assertRaises(FetchError) as caught:
            self.download(client, "https://cdn.example/a.jpg?oe=signed")
        self.assertEqual(caught.exception.status, 403)
        # The signed query stays out of the message.
        self.assertEqual(str(caught.exception), "HTTP 403 for https://cdn.example/a.jpg")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((self.tmp / "posts" / "a.jpg").exists())

    def test_mirrors_are_tried_in_order(self):
        with self.client(httpx.Response(404), httpx.Response(200, content=b"7z")) as client:
            self.assertTrue(self.download(client, ["https://a.example/f.7z", "https://b.example/f.7z"], "f.7z"))
        self.assertEqual([request.url.host for request in self.requests], ["a.example", "b.example"])
        self.assertIn("! mirror 1 failed: HTTP 404 for https://a.example/f.7z; trying next", self.lines())

    def test_a_failed_mirror_logs_why(self):
        """Fix 9: the warning names the reason, not only the mirror's number."""
        with self.client(httpx.ConnectError("refused"), httpx.ConnectError("refused"), httpx.ConnectError("refused"),
                         httpx.Response(200, content=b"7z")) as client:
            self.assertTrue(self.download(client, ["https://a.example/f.7z", "https://b.example/f.7z"], "f.7z"))
        self.assertIn("! mirror 1 failed: GET https://a.example/f.7z: ConnectError: refused; trying next", self.lines())

    def test_an_incomplete_body_is_still_retried(self):
        """Fix 9: only a page that isn't media fails at once; a cut-off body costs three attempts as before."""
        short = httpx.Response(200, headers={"content-length": "10"}, content=b"12345")
        with self.client(short) as client, self.assertRaises(http.IncompleteError):
            self.download(client, "https://cdn.example/a.jpg")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(list((self.tmp / "posts").iterdir()), [])

    def test_open_part_passes_o_binary(self):
        """Fix 1: without O_BINARY, Windows opens the part in text mode and writes every \\n byte as \\r\\n.

        `os.O_BINARY` exists only on Windows, so it is patched in here, and the spy strips the bit
        before the real `os.open` sees it, so the test runs on every platform.
        """
        body = b"line\nnext\r\n\n\x00\x1aend\n"
        binary, real_open, created = 0x8000, os.open, []

        def spy(path, flags, mode=0o777, *args, **kwargs):
            if flags & os.O_CREAT:
                created.append(flags)
            return real_open(path, flags & ~binary, mode, *args, **kwargs)

        with self.client(httpx.Response(200, content=body)) as client, \
                mock.patch.object(os, "O_BINARY", binary, create=True), mock.patch.object(os, "open", spy):
            self.assertTrue(self.download(client, "https://cdn.example/a.bin", "a.bin"))
        self.assertEqual((self.tmp / "posts" / "a.bin").read_bytes(), body)
        self.assertTrue(created)
        self.assertTrue(all(flags & binary for flags in created), created)

    def test_a_fetch_function_with_progress(self):
        def fetch(dest, progress):
            dest.write_bytes(b"media")
            progress(5, 5)

        with self.client(httpx.Response(200)) as client:
            self.assertTrue(self.download(client, fetch, "m.mp4"))
        self.assertEqual((self.tmp / "posts" / "m.mp4").read_bytes(), b"media")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.lines(), ["✓ " + "━" * 20 + " posts/m.mp4 " + "5.0 B"])

    def test_the_second_ctrl_c_stops_a_blocking_fetch_function_at_its_next_report(self):
        """D8: a source's fetch function (tlg's) gets a progress callback that a second Ctrl-C stops."""
        for event in (net.stopping, net.stopping_now):
            self.addCleanup(event.clear)
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

        with self.client(httpx.Response(200)) as client:
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(reported.wait(5))
            net.stopping.set()          # the first Ctrl-C: a running download goes on
            net.stopping_now.set()      # the second: it stops at its next report
            gate.set()
            thread.join(5)
        self.assertIsInstance(outcome.get("error"), KeyboardInterrupt)
        self.assertEqual(went_on, [])
        self.assertEqual(list((self.tmp / "posts").iterdir()), [])

    def test_a_fetch_function_that_gets_a_web_page_is_called_once(self):
        """Fix 9: a fetch function retries every Exception, so `final` is what stops a login wall costing three tries."""
        for error in (files.NotMediaError("got a web page instead of a photo"), media.UnsafeError("too many pixels")):
            with self.subTest(error=type(error).__name__):
                fetch = mock.Mock(side_effect=error)
                with self.client(httpx.Response(200)) as client, self.assertRaises(type(error)):
                    self.download(client, fetch, "m.mp4")
                fetch.assert_called_once()
                self.assertEqual(self.sleeps, [])
                self.assertEqual(list((self.tmp / "posts").iterdir()), [])

    def test_a_429_on_a_download_pauses_and_retries(self):
        with self.client(httpx.Response(429), httpx.Response(200, content=b"ok")) as client:
            self.assertTrue(self.download(client, "https://cdn.example/a.jpg"))
        self.assertEqual(sum(self.sleeps), 30.0)

    def test_a_429_during_a_mirror_is_waited_out_on_that_mirror(self):
        with self.client(httpx.Response(429), httpx.Response(429), httpx.Response(200, content=b"ok")) as client:
            self.assertTrue(self.download(client, ["https://a.example/f", "https://b.example/f"]))
        self.assertEqual({request.url.host for request in self.requests}, {"a.example"})
        self.assertFalse(any("mirror 1 failed" in line for line in self.lines()))


SPLIT_CHECKED = frozenset({"fbcdn.net", "pbs.twimg.com", "video.twimg.com", "loicloud.com"})


class SplitHostsTest(unittest.TestCase):
    def test_split_hosts_are_the_cdns_that_passed_a_live_check_and_match_subdomains(self):
        """D6: only CDNs that passed a live check split; every other host is one connection."""
        self.assertEqual(net.SPLIT_HOSTS, SPLIT_CHECKED)
        self.assertEqual(net.split_parts("https://scontent.fhan1-1.fna.fbcdn.net/v.mp4"), "auto")
        self.assertEqual(net.split_parts("https://video.twimg.com/v.mp4"), "auto")
        self.assertEqual(net.split_parts("https://abs.twimg.com/v.mp4"), 1)
        self.assertEqual(net.split_parts("https://scontent-a.cdninstagram.com/v.mp4"), 1)
        with mock.patch.object(net, "SPLIT_HOSTS", frozenset({"cdninstagram.com"})):
            self.assertEqual(net.split_parts("https://scontent-a.cdninstagram.com/v.mp4"), "auto")
            self.assertEqual(net.split_parts("https://cdninstagram.com/v.mp4"), "auto")
            self.assertEqual(net.split_parts("https://notcdninstagram.com/v.mp4"), 1)


def photo(format, size=(32, 24)):
    from PIL import Image

    out = io.BytesIO()
    Image.effect_noise(size, 40).convert("RGB").save(out, format)
    return out.getvalue()


def chunked(data):
    """A body sent without Content-Length, as a chunked response is."""
    return httpx.Response(200, content=iter([data]))


@needs_pillow
class MediaDownloadTest(NetCase):
    def download(self, client, source, name="a.jpg", **options):
        return net.download(client, source, self.tmp / "posts" / name, label=f"posts/{name}", **options)

    def names(self):
        return sorted(path.name for path in (self.tmp / "posts").iterdir())

    def test_a_file_named_by_its_content_is_renamed_once_onto_that_name(self):
        """A crash can never leave `a.jpg` holding a WebP, which the job would take as saved."""
        real, moved = os.replace, []

        def spy(src, dst):
            moved.append(Path(dst).name)
            return real(src, dst)

        with self.client(httpx.Response(200, content=photo("WEBP"))) as client, mock.patch.object(os, "replace", spy):
            self.assertEqual(self.download(client, "https://cdn.example/a.jpg", kind="image").name, "a.webp")
        self.assertNotIn("a.jpg", moved)
        self.assertEqual(self.names(), ["a.webp"])

    def test_content_decides_the_extension(self):
        with self.client(httpx.Response(200, content=photo("WEBP"))) as client:
            self.assertEqual(self.download(client, "https://cdn.example/a.jpg", kind="image"), self.tmp / "posts" / "a.webp")
        self.assertEqual(self.names(), ["a.webp"])
        with self.client(httpx.Response(200, content=photo("JPEG"))) as client:
            self.assertEqual(self.download(client, "https://cdn.example/b.jpeg", "b.jpeg", kind="image").name, "b.jpeg")

    @needs_ffmpeg()
    def test_an_mp4_keeps_its_name(self):
        with self.client(httpx.Response(200, content=sample("v.mp4").read_bytes())) as client:
            self.assertEqual(self.download(client, "https://cdn.example/v.mp4", "v.mp4", kind="video").name, "v.mp4")

    def test_a_web_page_is_requested_once_and_nothing_is_saved(self):
        """Fix 9: a login wall is the same page on every attempt, so it fails at once instead of three times."""
        page = httpx.Response(200, content=b"<!doctype html><title>Log in</title>")
        with self.client(page) as client, self.assertRaises(net.NotMediaError) as caught:
            self.download(client, "https://cdn.example/a.jpg", kind="image")
        self.assertEqual(str(caught.exception), "got a web page instead of a photo")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.names(), [])

    def test_an_unsafe_file_is_requested_once(self):
        """Fix 9: a file media refuses (a decompression bomb) is refused again on every attempt."""
        body = photo("JPEG")
        with self.client(*(chunked(body) for _ in range(3))) as client, self.assertRaises(media.UnsafeError), \
                mock.patch("shared.media.open", side_effect=media.UnsafeError("too many pixels")):
            self.download(client, "https://cdn.example/a.jpg", kind="image")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.names(), [])

    def test_a_web_page_on_a_mirror_is_logged_with_why(self):
        page = httpx.Response(200, content=b"<!doctype html><title>Log in</title>")
        with self.client(page, httpx.Response(200, content=photo("JPEG"))) as client:
            self.assertTrue(self.download(client, ["https://a.example/a.jpg", "https://b.example/a.jpg"], kind="image"))
        self.assertEqual(len(self.requests), 2)
        self.assertIn("! mirror 1 failed: got a web page instead of a photo; trying next", self.lines())

    def test_files_and_archives_are_never_renamed(self):
        with self.client(httpx.Response(200, content=photo("WEBP"))) as client:
            self.assertEqual(self.download(client, "https://cdn.example/f.zip", "f.zip").name, "f.zip")

    def test_a_truncated_photo_without_a_size_is_retried(self):
        whole = photo("JPEG", (400, 300))
        with self.client(chunked(whole[: len(whole) // 2]), chunked(whole)) as client:
            self.assertEqual(self.download(client, "https://cdn.example/a.jpg", kind="image").read_bytes(), whole)
        self.assertEqual(len(self.requests), 2)

    def test_a_fetch_without_a_size_is_checked(self):
        whole = photo("JPEG", (400, 300))
        bodies = [whole[:300], whole]

        def fetch(dest, progress):
            dest.write_bytes(bodies.pop(0))

        with self.client(httpx.Response(200)) as client:
            self.assertEqual(self.download(client, fetch, kind="image").read_bytes(), whole)
        self.assertEqual(bodies, [])

    def test_a_download_with_a_known_size_is_not_decoded(self):
        with self.client(httpx.Response(200, content=photo("JPEG"))) as client, \
                mock.patch("shared.media.open", side_effect=AssertionError("decoded")):
            self.assertTrue(self.download(client, "https://cdn.example/a.jpg", kind="image"))

    def test_without_ffmpeg_video_checks_are_skipped(self):
        with self.client(chunked(b"\0\0\0\x18ftypisom" + bytes(100))) as client, \
                mock.patch("shared.media.open", side_effect=RuntimeError("shared.media needs ffmpeg")):
            self.assertEqual(self.download(client, "https://cdn.example/v.mp4", "v.mp4", kind="video").name, "v.mp4")


@needs_ffmpeg()
class DashDownloadTest(NetCase):
    VIDEO, AUDIO = "https://cdn.example/v.mp4", "https://cdn.example/a.mp4"

    def serve(self, request):
        self.requests.append(request)
        return self.bodies[str(request.url)].pop(0)

    def client(self, **bodies):
        self.bodies = {self.VIDEO: [httpx.Response(200, content=sample("v.mp4").read_bytes())],
                       self.AUDIO: [httpx.Response(200, content=sample("a.m4a").read_bytes())], **bodies}
        return super().client(httpx.Response(500))

    def merged(self, client, name="r.mp4"):
        return net.download(client, self.VIDEO, self.tmp / "reels" / name, label=f"reels/{name}",
                            kind="video", audio=self.AUDIO)

    def test_video_and_audio_are_merged_and_both_parts_deleted(self):
        with self.client() as client:
            saved = self.merged(client)
        info = media.open(saved).info
        self.assertEqual((info.video, info.audio is not None), ("mpeg4", True))
        self.assertEqual([path.name for path in saved.parent.iterdir()], ["r.mp4"])
        self.assertEqual(len(self.lines()), 1)

    def test_a_truncated_audio_part_is_retried(self):
        sound = sample("a.m4a").read_bytes()
        with self.client(**{self.AUDIO: [chunked(sound[: len(sound) * 7 // 10]), chunked(sound)],
                            self.VIDEO: [httpx.Response(200, content=sample("v.mp4").read_bytes())] * 2}) as client:
            saved = self.merged(client)
        self.assertIsNotNone(media.open(saved).info.audio)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual([path.name for path in saved.parent.iterdir()], ["r.mp4"])

    def test_merge_temp_files_are_named_part(self):
        """Fix 13: every file a DASH download writes before its rename matches what `flnd sync` repair removes."""
        real_replace, moved = os.replace, []

        def spy(src, dst):
            moved.append(Path(src).name)
            return real_replace(src, dst)

        with self.client() as client, mock.patch.object(os, "replace", spy):
            self.merged(client)
        self.assertTrue(moved)
        self.assertEqual([name for name in moved if not fnmatch.fnmatch(name, files.PART_GLOB)], [])

    def test_a_second_ctrl_c_before_the_merge_stops_it(self):
        """The merge is ffmpeg's and can't see `stopping_now`, so a second Ctrl-C after the streams stops before it."""
        self.addCleanup(net.stopping_now.clear)
        real = http.Client.download

        def transfer(client, url, dest, **options):
            saved = real(client, url, dest, **options)
            if url == self.AUDIO:
                net.stopping_now.set()
            return saved

        with self.client() as client, mock.patch.object(http.Client, "download", transfer), \
                mock.patch("shared.media.open", side_effect=AssertionError("merged")), \
                self.assertRaises(KeyboardInterrupt):
            self.merged(client)
        self.assertEqual(list((self.tmp / "reels").iterdir()), [])

    def test_a_container_that_cant_hold_the_streams_fails_at_once(self):
        with self.client() as client, self.assertRaises(RuntimeError):
            self.merged(client, "r.webm")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(list((self.tmp / "reels").iterdir()), [])


class FollowTest(NetCase):
    def test_follows_redirects_with_one_head(self):
        def serve(request):
            self.requests.append(request)
            if request.url.host == "vm.tiktok.com":
                return httpx.Response(301, headers={"location": "https://www.tiktok.com/@a/video/7301234567890123456"})
            return httpx.Response(200)

        with mock.patch.object(net, "_base_transport", lambda: httpx.MockTransport(serve)):
            self.assertEqual(net.follow("https://vm.tiktok.com/ZM1/"), "https://www.tiktok.com/@a/video/7301234567890123456")
        self.assertEqual({request.method for request in self.requests}, {"HEAD"})

    def test_fetch_error_keeps_the_status(self):
        self.assertEqual(FetchError("x", status=403).status, 403)


if __name__ == "__main__":
    unittest.main()
