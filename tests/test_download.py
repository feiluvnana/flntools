"""Tests for `http.download` and the downloader behind it, `shared._download`.

The CDN is faked with `httpx.MockTransport` (`Cdn` honours `Range` and `If-Range`, and can cut a
body short), the retry waits with a fake `clock`, and files go to a temporary folder. `small`
shrinks the piece sizes, so a 10 KB file splits as a big one would; the stall tests run on the
real clock, for a fraction of a second.
"""

import base64
import fnmatch
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

import httpx

import shared
from shared import _download as download
from shared import _http as http
from shared import clock, theme
from shared._theme import Screen

DATA = bytes(range(256)) * 40  # 10,240 bytes, every offset distinguishable
# The first request of a file `parts="auto"` may split: its first piece.
FIRST = f"bytes=0-{download.PIECE_MIN - 1}"


def client(handler: Callable[[httpx.Request], httpx.Response], **options) -> httpx.Client:
    """A client whose requests go to `handler`, never the network."""
    return httpx.Client(transport=httpx.MockTransport(handler), **options)


def small(test: unittest.TestCase) -> None:
    """Pieces of 1 KB and a 4 KB split threshold, for the rest of `test`."""
    for name, value in (("PIECE_MIN", 1024), ("PIECE_MAX", 1024), ("SPLIT_MIN", 4096)):
        test.enterContext(mock.patch.object(download, name, value))


class Body(httpx.SyncByteStream):
    """A body sent in `chunk`-byte pieces, which can drop the connection after `cut` bytes."""

    def __init__(self, data: bytes, *, chunk: int = 1024, cut: int | None = None,
                 on_chunk: Callable[[], None] | None = None,
                 closed: Callable[[], None] | None = None):
        self.data, self.chunk, self.cut = data, chunk, cut
        self.on_chunk, self.closed = on_chunk, closed

    def __iter__(self):
        sent = 0
        while sent < len(self.data):
            if self.cut is not None and sent >= self.cut:
                raise httpx.ReadError("connection reset")
            piece = self.data[sent:sent + self.chunk]
            if self.cut is not None:
                piece = piece[: self.cut - sent]
            if self.on_chunk is not None:
                self.on_chunk()
            yield piece
            sent += len(piece)

    def close(self) -> None:
        if self.closed is not None:
            self.closed()
            self.closed = None


class Cdn:
    """A file server: `handler` answers whole files and ranges of `data`, validated by `etag`.

    `script` holds callables taking the request and returning a response (or None for the
    normal answer), used one per request in order.
    """

    def __init__(self, data: bytes = DATA, *, etag: str | None = '"v1"', ranges: bool = True,
                 chunk: int = 1024):
        self.data, self.etag, self.ranges, self.chunk = data, etag, ranges, chunk
        self.requests: list[httpx.Request] = []
        self.threads: set[int] = set()
        self.script: list[Callable[[httpx.Request], httpx.Response | None]] = []
        self.lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            self.threads.add(threading.get_ident())
            step = self.script.pop(0) if self.script else None
        answer = step(request) if step is not None else None
        return answer if answer is not None else self.answer(request)

    def answer(self, request: httpx.Request, *, cut: int | None = None, data: bytes | None = None,
               etag: str | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
        data = self.data if data is None else data
        etag = self.etag if etag is None else etag
        sent = {"ETag": etag} if etag else {}
        if self.ranges:
            sent["Accept-Ranges"] = "bytes"
        sent.update(headers or {})
        wanted = request.headers.get("range")
        if wanted and self.ranges and request.headers.get("if-range") in (None, etag):
            first, _, last = wanted.removeprefix("bytes=").partition("-")
            start, end = int(first), min(int(last) + 1, len(data)) if last else len(data)
            sent.update({"Content-Range": f"bytes {start}-{end - 1}/{len(data)}",
                         "Content-Length": str(end - start)})
            return httpx.Response(206, headers=sent,
                                  stream=Body(data[start:end], chunk=self.chunk, cut=cut))
        sent["Content-Length"] = str(len(data))
        return httpx.Response(200, headers=sent, stream=Body(data, chunk=self.chunk, cut=cut))

    def cut_at(self, at: int) -> Callable[[httpx.Request], httpx.Response]:
        """A script step: the normal answer, dropped after `at` bytes."""
        return lambda request: self.answer(request, cut=at)

    def ranges_asked(self) -> list[str | None]:
        return [request.headers.get("range") for request in self.requests]


class StallingBody(httpx.SyncByteStream):
    """A body that sends one chunk, then hangs until the response is closed, as a stalled CDN."""

    def __init__(self, data: bytes):
        self.data, self.released = data, threading.Event()

    def __iter__(self):
        yield self.data[:1024]
        self.released.wait(10)
        raise httpx.ReadError("the connection was closed")

    def close(self) -> None:
        self.released.set()


class DownloadCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = self.enterContext(clock.use(clock.Fake()))
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cdn = Cdn()
        # Every test starts with fresh hosts: no connection counts or caps from another test.
        self.enterContext(mock.patch.object(download, "_gates", {}))

    def download(self, url: str | list[str] = "https://cdn.example/v.mp4",
                 dest: str | Path | None = None, **options) -> Path:
        with client(self.cdn.handler) as fake:
            dest = dest if dest is not None else self.tmp / "v.mp4"
            return http.download(url, dest, client=fake, **options).path

    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.tmp.iterdir() if path.name.startswith("."))


def interrupt_at(count: int) -> Callable[[int, int | None], None]:
    """A progress callback that raises Ctrl-C once `count` bytes are done."""
    def progress(done: int, total: int | None) -> None:
        if done >= count:
            raise KeyboardInterrupt
    return progress


class DownloadTest(DownloadCase):
    def test_saves_the_file_atomically_and_reports_progress(self):
        seen: list[tuple[int, int | None]] = []
        saved = self.download(progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(saved, self.tmp / "v.mp4")
        self.assertEqual(saved.read_bytes(), DATA)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(seen[-1], (len(DATA), len(DATA)))
        self.assertEqual([done for done, _ in seen], sorted(done for done, _ in seen))
        request = self.cdn.requests[0]
        # Asked for as it is: a compressed body's bytes and ranges aren't the file's.
        self.assertEqual(request.headers["accept-encoding"], "identity")
        self.assertTrue(request.extensions.get(http.DOWNLOAD_EXTENSION))
        # The first piece only, so a big file's first stream ends cleanly where its piece does.
        self.assertEqual(self.cdn.ranges_asked(), [FIRST])

    def test_parts_1_sends_one_plain_request(self):
        self.assertEqual(self.download(parts=1).read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [None])

    def test_a_folder_dest_is_named_from_the_url_safely(self):
        for url, name in (("https://cdn.example/a/clip%20one.mp4?sig=1", "clip_one.mp4"),
                          ("https://cdn.example/a/..%2F..%2Fx:y.mp4", "x_y.mp4"),
                          ("https://cdn.example/CON", "CON_"),
                          ("https://cdn.example/", "download")):
            with self.subTest(url=url):
                saved = self.download(url, self.tmp)
                self.assertEqual(saved, self.tmp / name)
                saved.unlink()
        self.assertEqual(self.download("https://cdn.example/b.mp4", f"{self.tmp}/new/"),
                         self.tmp / "new" / "b.mp4")

    def test_mirrors_are_tried_in_order(self):
        self.cdn.script = [lambda request: httpx.Response(404)]
        saved = self.download(["https://a.example/v.mp4", "https://b.example/v.mp4"])
        self.assertEqual(saved.read_bytes(), DATA)
        self.assertEqual([request.url.host for request in self.cdn.requests],
                         ["a.example", "b.example"])

    def test_the_last_mirrors_refusal_is_raised(self):
        self.cdn.script = [lambda request: httpx.Response(404),
                           lambda request: httpx.Response(403)]
        with self.assertRaises(http.RefusedError) as raised:
            self.download(["https://a.example/v.mp4", "https://b.example/v.mp4?sig=secret"])
        self.assertEqual(raised.exception.status, 403)
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(self.leftovers(), [])

    def test_a_429_raises_without_sleeping_or_trying_a_mirror(self):
        self.cdn.script = [lambda request: httpx.Response(429, headers={"Retry-After": "7"})]
        with self.assertRaises(http.RateLimitedError) as raised:
            self.download(["https://a.example/v.mp4", "https://b.example/v.mp4"])
        self.assertEqual(raised.exception.retry_after, 7.0)
        self.assertEqual(self.clock.slept, [])
        self.assertEqual(len(self.cdn.requests), 1)

    def test_the_first_retry_is_at_once_then_1_s_doubling_to_30_s(self):
        self.cdn.script = [lambda request: httpx.Response(503)]
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.clock.slept, [])
        self.cdn.script = [lambda request: httpx.Response(503)] * 8
        with self.assertRaises(http.ServerError):
            self.download(attempts=8)
        self.assertEqual(self.clock.slept, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0])

    def test_the_size_check(self):
        """Without a length from the server, the caller's `size` is what's checked."""
        self.cdn.script = [lambda request: httpx.Response(200, content=iter([DATA[:100]]))] * 3
        with self.assertRaises(http.IncompleteError):
            self.download(size=len(DATA))
        self.assertEqual(len(self.cdn.requests), 3)
        self.assertEqual(self.leftovers(), [])

    def test_check_sees_the_whole_part_before_the_rename(self):
        seen: list[bytes] = []
        self.download(check=lambda path: seen.append(path.read_bytes()))
        self.assertEqual(seen, [DATA])

    def test_a_check_that_finds_the_file_broken_downloads_it_again(self):
        results = [shared.BrokenError("cut off"), None]

        def check(path: Path) -> None:
            result = results.pop(0)
            if result is not None:
                raise result

        self.assertEqual(self.download(check=check).read_bytes(), DATA)
        # From the start: the bytes that were there were all the body had.
        self.assertEqual(self.cdn.ranges_asked(), [FIRST, FIRST])

    def test_a_check_that_refuses_the_file_is_raised_at_once_and_leaves_nothing(self):
        def check(path: Path) -> None:
            raise ValueError("a web page")

        with self.assertRaises(ValueError):
            self.download(check=check)
        self.assertEqual(len(self.cdn.requests), 1)
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), [])

    def test_options_are_checked(self):
        for options in ({"attempts": 0}, {"parts": 0}, {"parts": "two"}, {"parts": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.download(**options)
        with self.assertRaises(ValueError):
            self.download([])

    def test_no_secret_in_messages(self):
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"refused with {request.headers['cookie']}")

        self.cdn.script = [refused] * 3
        with self.assertRaises(http.Error) as raised, client(self.cdn.handler) as fake:
            http.download("https://cdn.example/v.mp4?token=abcdef", self.tmp / "v.mp4",
                          client=fake, headers={"Cookie": "sid=supersecret"})
        self.assertNotIn("supersecret", str(raised.exception))
        self.assertNotIn("abcdef", str(raised.exception))

    def test_a_label_draws_a_row(self):
        screen = Screen(dest=io.StringIO(), inp=io.StringIO(""), width=200, tty=False,
                        interactive=False)
        with theme.scope(screen=screen, color="none", unicode=True):
            self.download(label="v.mp4")
        self.assertEqual(screen.dest.getvalue().splitlines(),
                         ["✓ " + "━" * 20 + " v.mp4 " + "10.0 KB"])

    def test_an_empty_file(self):
        def empty(request: httpx.Request) -> httpx.Response:
            if request.headers.get("range"):
                return httpx.Response(416, headers={"Content-Range": "bytes */0"})
            return httpx.Response(200, headers={"Content-Length": "0"}, content=b"")

        self.cdn.script = [empty, empty]
        self.assertEqual(self.download().read_bytes(), b"")
        self.assertEqual(self.cdn.ranges_asked(), [FIRST, None])


class ResumeTest(DownloadCase):
    def test_a_resume_after_a_cut_sends_range_and_if_range(self):
        self.cdn.script = [self.cdn.cut_at(3000)]
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [FIRST, "bytes=3000-"])
        self.assertEqual(self.cdn.requests[1].headers["if-range"], '"v1"')
        self.assertEqual(self.leftovers(), [])

    def test_last_modified_is_the_validator_without_a_strong_etag(self):
        modified = "Wed, 21 Oct 2026 07:28:00 GMT"
        for etag in (None, 'W/"weak"'):
            with self.subTest(etag=etag):
                self.cdn.requests.clear()

                def first(request: httpx.Request, etag=etag) -> httpx.Response:
                    response = self.cdn.answer(request, cut=3000, etag=etag or "")
                    response.headers["Last-Modified"] = modified
                    if etag:
                        response.headers["ETag"] = etag
                    return response

                def rest(request: httpx.Request) -> httpx.Response:
                    return self.cdn.answer(request, etag=request.headers.get("if-range"))

                self.cdn.script = [first, rest]
                self.assertEqual(self.download(parts=1).read_bytes(), DATA)
                self.assertEqual(self.cdn.requests[1].headers["if-range"], modified)

    def test_a_200_to_a_range_restarts_the_file(self):
        """The file changed on the server: `If-Range` no longer matches, so it comes whole."""
        changed = DATA[::-1]
        self.cdn.script = [self.cdn.cut_at(3000),
                           lambda request: self.cdn.answer(request, data=changed, etag='"v2"')]
        self.assertEqual(self.download().read_bytes(), changed)
        self.assertEqual(self.cdn.ranges_asked(), [FIRST, "bytes=3000-"])
        self.assertEqual(self.leftovers(), [])

    def test_a_changed_file_whose_restart_fails_raises_changed_error(self):
        changed = DATA[::-1]

        def cut_new(request: httpx.Request) -> httpx.Response:
            return self.cdn.answer(request, data=changed, etag='"v2"', cut=10)

        self.cdn.script = [self.cdn.cut_at(3000), cut_new, lambda request: httpx.Response(503)]
        with self.assertRaises(http.ChangedError) as raised:
            self.download()
        self.assertIsInstance(raised.exception, shared.BrokenError)
        self.assertIsInstance(raised.exception.__cause__, http.ServerError)

    def test_a_416_restarts_the_file_in_one_plain_request(self):
        self.cdn.script = [self.cdn.cut_at(3000), lambda request: httpx.Response(416)]
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [FIRST, "bytes=3000-", None])

    def test_a_part_json_from_an_earlier_run_resumes(self):
        """A Ctrl-C, here raised from `progress`, keeps the part and its `.part.json`."""
        with self.assertRaises(KeyboardInterrupt):
            self.download("https://cdn.example/v.mp4?sig=secret", progress=interrupt_at(4096))
        parts = self.leftovers()
        self.assertEqual(len(parts), 2)
        self.assertTrue(fnmatch.fnmatch(parts[0], ".v.mp4.*.part"), parts)
        self.assertEqual(parts[1], parts[0] + ".json")
        # The signed link's token stays off the disk: the sidecar has the URL without its query.
        self.assertNotIn("secret", (self.tmp / parts[1]).read_text())
        info = json.loads((self.tmp / parts[1]).read_text())
        self.assertEqual((info["url"], info["validator"], info["size"]),
                         ("https://cdn.example/v.mp4", '"v1"', len(DATA)))
        kept = (self.tmp / parts[0]).stat().st_size
        self.assertGreaterEqual(kept, 4096)

        # The next run, even through a fresh link (an expired one refreshed), asks for the rest.
        self.cdn.requests.clear()
        self.assertEqual(self.download("https://cdn.example/v.mp4?sig=fresh").read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [f"bytes={kept}-"])
        self.assertEqual(self.leftovers(), [])

    def test_a_file_never_adopts_a_siblings_part(self):
        """`a.rar` and `a.rar.001` in one folder each keep their own part."""
        with self.assertRaises(KeyboardInterrupt):
            self.download(dest=self.tmp / "a.rar.001", progress=interrupt_at(4096))
        sibling = self.leftovers()
        self.assertEqual(len(sibling), 2)
        self.cdn.requests.clear()
        self.assertEqual(self.download(dest=self.tmp / "a.rar").read_bytes(), DATA)
        # A fresh download, not a resume of the sibling's bytes, which are still there.
        self.assertEqual(self.cdn.ranges_asked(), [FIRST])
        self.assertEqual(self.leftovers(), sibling)

    def test_a_refusal_keeps_a_resumable_part(self):
        self.cdn.script = [self.cdn.cut_at(3000), lambda request: httpx.Response(403)]
        with self.assertRaises(http.RefusedError):
            self.download()
        self.assertEqual(len(self.leftovers()), 2)

    def test_a_part_from_a_server_without_validator_or_ranges_is_never_kept(self):
        self.cdn = Cdn(etag=None, ranges=False)
        self.cdn.script = [self.cdn.cut_at(3000)] * 3
        with self.assertRaises(http.Error):
            self.download()
        # Each attempt starts again: nothing could tell its bytes are still the server's.
        self.assertEqual(self.cdn.ranges_asked(), [FIRST, FIRST, FIRST])
        self.assertEqual(self.leftovers(), [])

    def test_resume_false_keeps_nothing(self):
        self.cdn.script = [self.cdn.cut_at(3000), lambda request: httpx.Response(403)]
        with self.assertRaises(http.RefusedError):
            self.download(resume=False)
        self.assertEqual(self.cdn.ranges_asked(), [FIRST, FIRST])
        self.assertEqual(self.leftovers(), [])

    def test_a_broken_sidecar_is_deleted_and_the_file_downloaded_whole(self):
        part = self.tmp / ".v.mp4.0badc0de.part"
        part.write_bytes(b"junk")
        (self.tmp / ".v.mp4.0badc0de.part.json").write_text("{not json")
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [FIRST])
        self.assertEqual(self.leftovers(), [])


class OverlapTest(DownloadCase):
    """A part whose server sent no validator resumes once its last 64 bytes match."""

    def setUp(self) -> None:
        super().setUp()
        self.cdn = Cdn(etag=None)

    def test_a_cut_resumes_from_64_bytes_back(self):
        self.cdn.script = [self.cdn.cut_at(3000)]
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [FIRST, f"bytes={3000 - download.OVERLAP}-"])
        self.assertNotIn("if-range", self.cdn.requests[1].headers)

    def test_overlap_bytes_that_differ_restart_the_file(self):
        changed = DATA[::-1]
        self.cdn.script = [self.cdn.cut_at(3000),
                           lambda request: self.cdn.answer(request, data=changed),
                           lambda request: self.cdn.answer(request, data=changed)]
        self.assertEqual(self.download().read_bytes(), changed)
        self.assertEqual(self.cdn.ranges_asked(), [FIRST, "bytes=2936-", None])

    def test_a_part_without_a_validator_resumes_in_a_later_call(self):
        with self.assertRaises(KeyboardInterrupt):
            self.download(progress=interrupt_at(4096))
        info = json.loads(next(self.tmp.glob(".v.mp4.*.part.json")).read_text())
        self.assertEqual((info["validator"], info["ranges"]), (None, True))
        kept = next(path for path in self.tmp.glob(".v.mp4.*.part")).stat().st_size
        self.cdn.requests.clear()
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [f"bytes={kept - download.OVERLAP}-"])

    def test_a_file_without_a_validator_is_never_split(self):
        """Without `If-Range`, pieces fetched at once could come from two versions of the file."""
        small(self)
        self.assertEqual(self.download(parts=4).read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), ["bytes=0-1023", f"bytes={1024 - 64}-"])


class PiecesTest(DownloadCase):
    def setUp(self) -> None:
        super().setUp()
        small(self)

    def test_a_big_file_is_fetched_in_bounded_pieces_in_order(self):
        self.assertEqual(self.download().read_bytes(), DATA)
        asked = self.cdn.ranges_asked()
        pieces = [f"bytes={at}-{at + 1023}" for at in range(1024, 9216, 1024)] + ["bytes=9216-"]
        self.assertEqual(asked[0], "bytes=0-1023")
        self.assertEqual(sorted(asked[1:]), sorted(pieces))
        self.assertTrue(all(request.headers["if-range"] == '"v1"'
                            for request in self.cdn.requests[1:]))
        # The first request may be a photo's whole file; only the split file's pieces are pieces.
        self.assertFalse(self.cdn.requests[0].extensions.get(http.PIECE_EXTENSION))
        self.assertTrue(all(request.extensions.get(http.PIECE_EXTENSION)
                            for request in self.cdn.requests[1:]))
        self.assertEqual(self.leftovers(), [])

    def test_a_connection_that_finishes_a_piece_takes_the_next(self):
        self.download()
        # 10 pieces on at most the host's 4 starting connections, each a thread that took
        # piece after piece, with the calling thread's first request.
        self.assertEqual(len(self.cdn.requests), 10)
        self.assertLessEqual(len(self.cdn.threads), download.START_CONNECTIONS + 1)

    def test_a_cut_piece_resumes_on_its_own_and_keeps_its_bytes(self):
        cuts = ["bytes=5120-6143"]

        def maybe_cut(request: httpx.Request) -> httpx.Response | None:
            if request.headers.get("range") in cuts:
                cuts.clear()
                return self.cdn.answer(request, cut=100)
            return None

        self.cdn.script = [maybe_cut] * 20
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertIn("bytes=5220-6143", self.cdn.ranges_asked())

    def test_a_server_without_ranges_is_never_split(self):
        self.cdn = Cdn(ranges=False)
        self.download()
        self.assertEqual(self.cdn.ranges_asked(), ["bytes=0-1023"])

    def test_a_split_part_json_resumes_each_piece(self):
        with self.assertRaises(KeyboardInterrupt):
            self.download(progress=interrupt_at(6000))
        info = json.loads(next(self.tmp.glob(".v.mp4.*.part.json")).read_text())
        self.assertEqual(len(info["segments"]), 10)
        done = sum(segment[2] for segment in info["segments"])
        self.cdn.requests.clear()
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertTrue(all(asked is not None for asked in self.cdn.ranges_asked()))
        self.assertLessEqual(len(self.cdn.requests), 10 - done // 1024)
        self.assertEqual(self.leftovers(), [])

    def test_a_429_on_every_try_of_a_piece_keeps_the_part_and_the_next_call_resumes(self):
        def limited(request: httpx.Request) -> httpx.Response | None:
            if request.headers.get("range") == "bytes=5120-6143":
                return httpx.Response(429)
            return None

        self.cdn.script = [limited] * 40
        with self.assertRaises(http.RateLimitedError):
            self.download()
        self.assertEqual(self.clock.slept, [])
        self.assertEqual(len(self.leftovers()), 2)
        self.cdn.script.clear()
        self.cdn.requests.clear()
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertTrue(all(asked is not None for asked in self.cdn.ranges_asked()))

    def test_a_sidecar_never_claims_bytes_not_on_disk_and_a_crash_resumes_whole(self):
        """Every save of a split's `.part.json`, read as kill -9 would leave it, holds its bytes."""
        real_save = download._State.save
        snapshots: list[tuple[bytes, str, str]] = []

        def save(state) -> None:
            real_save(state)
            if len(state.segments) > 1 and state.part.exists():
                snapshots.append((state.part.read_bytes(), state.info.read_text(), state.part.name))

        def slow(request: httpx.Request) -> httpx.Response:
            response = self.cdn.answer(request)
            stream = response.stream
            assert isinstance(stream, Body)
            stream.chunk = 256
            stream.on_chunk = lambda: time.sleep(0.001)
            return response

        self.cdn.script = [slow] * 10
        with mock.patch.object(download._State, "save", save):
            self.assertEqual(self.download().read_bytes(), DATA)
        self.assertGreater(len(snapshots), 1)
        for data, info, _ in snapshots:
            for start, end, done in json.loads(info)["segments"]:
                self.assertEqual(data[start:start + done], DATA[start:start + done])

        # Resumed from the earliest crash point, the file comes out whole.
        data, info, name = snapshots[0]
        crashed = self.tmp / "crashed"
        crashed.mkdir()
        (crashed / name).write_bytes(data)
        (crashed / (name + ".json")).write_text(info)
        self.assertEqual(self.download(dest=crashed / "v.mp4").read_bytes(), DATA)

    def test_never_more_than_the_hosts_connections_across_threads(self):
        """Four split downloads at once from one host: 4 open at most, and every one given back."""
        lock = threading.Lock()
        state = {"open": 0, "peak": 0}

        def opened() -> None:
            with lock:
                state["open"] += 1
                state["peak"] = max(state["peak"], state["open"])

        def closed() -> None:
            with lock:
                state["open"] -= 1

        def handler(request: httpx.Request) -> httpx.Response:
            response = self.cdn.handler(request)
            opened()
            stream = response.stream
            assert isinstance(stream, Body)
            stream.closed = closed
            stream.on_chunk = lambda: time.sleep(0.002)
            return response

        url = "https://limits.cdn.example/v.mp4"
        results: dict[int, bytes] = {}
        with client(handler) as fake:
            def work(number: int) -> None:
                saved = http.download(url, self.tmp / f"{number}.mp4", client=fake)
                results[number] = saved.path.read_bytes()

            threads = [threading.Thread(target=work, args=(number,)) for number in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results, {number: DATA for number in range(4)})
        self.assertLessEqual(state["peak"], download.START_CONNECTIONS)
        self.assertGreater(state["peak"], 1)
        self.assertEqual(state["open"], 0)
        self.assertEqual(download._gate(url).active, 0)

    def test_progress_raising_before_the_body_gives_its_connection_back(self):
        url = "https://slots.cdn.example/v.mp4"
        with self.assertRaises(KeyboardInterrupt):
            self.download(url, progress=interrupt_at(0))
        self.assertEqual(download._gate(url).active, 0)


class ThresholdTest(DownloadCase):
    def test_a_4_mb_file_splits_and_a_3_mb_one_does_not(self):
        mb = 1024 * 1024
        for size, pieces in ((4 * mb, True), (3 * mb, False)):
            with self.subTest(size=size):
                self.cdn = Cdn(bytes(range(256)) * (size // 256), chunk=256 * 1024)
                saved = self.download(dest=self.tmp / f"{size}.mp4")
                self.assertEqual(saved.stat().st_size, size)
                asked = self.cdn.ranges_asked()
                self.assertEqual(asked[0], FIRST)
                # The rest of a 3 MB file is one request to its end, on the same connection.
                self.assertEqual(asked[1:], ["bytes=2097152-"])
                marked = [bool(request.extensions.get(http.PIECE_EXTENSION))
                          for request in self.cdn.requests]
                self.assertEqual(marked, [False, pieces])

    def test_pieces_are_a_32nd_of_the_file_within_2_to_4_mb(self):
        mb = 1024 * 1024
        sizes = {total: {segment.end - segment.start for segment in download._plan(0, total)}
                 for total in (8 * mb, 96 * mb, 1024 * mb)}
        self.assertEqual(sizes[8 * mb], {2 * mb})
        self.assertEqual(sizes[96 * mb], {3 * mb})
        self.assertEqual(sizes[1024 * mb], {4 * mb})


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = self.enterContext(clock.use(clock.Fake()))
        self.gate = download._Gate()

    def fill(self) -> None:
        while self.gate.take():
            pass

    def test_a_host_grows_while_its_throughput_rises(self):
        self.fill()
        self.assertEqual(self.gate.active, download.START_CONNECTIONS)
        self.gate.received(1000)
        self.clock.advance(1.0)
        self.gate.received(1000)          # 2,000 B/s, the first window: one more connection
        self.assertEqual(self.gate.allowed(), 5)
        self.fill()
        self.clock.advance(1.0)
        self.gate.received(2100)          # barely more: the fifth didn't help, so no sixth
        self.assertEqual(self.gate.allowed(), 5)
        self.fill()
        self.clock.advance(1.0)
        self.gate.received(4000)          # much more: a sixth
        self.assertEqual(self.gate.allowed(), 6)

    def test_a_host_never_grows_without_a_connection_wanted_or_past_8(self):
        self.gate.received(1000)
        self.clock.advance(1.0)
        self.gate.received(1000)
        self.assertEqual(self.gate.allowed(), download.START_CONNECTIONS)
        for step in range(10):
            self.fill()
            self.clock.advance(1.0)
            self.gate.received(10_000 * 2 ** step)
        self.assertEqual(self.gate.allowed(), download.MAX_CONNECTIONS)

    def test_two_refusals_while_others_receive_cap_the_host_for_10_minutes(self):
        self.fill()
        for _ in range(3):
            self.gate.started()
        self.assertTrue(self.gate.refused())
        self.assertEqual(self.gate.allowed(), 4)     # one refusal could be a one-off
        self.assertTrue(self.gate.refused())
        self.assertEqual(self.gate.allowed(), 3)     # the most seen working
        self.clock.advance(download.FORGET)
        self.assertEqual(self.gate.allowed(), 3)     # the limit stays; the cap is gone
        self.assertIsNone(self.gate.cap)

    def test_a_refusal_with_nothing_receiving_is_the_files_own_failure(self):
        self.assertFalse(self.gate.refused())
        self.assertEqual(self.gate.refusals, 0)

    def test_a_429_on_an_extra_connection_shrinks_the_host_and_the_file_still_completes(self):
        small(self)
        self.enterContext(mock.patch.object(download, "_gates", {}))
        cdn = Cdn()
        refusals = ["bytes=3072-4095", "bytes=4096-5119"]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers.get("range") in refusals:
                refusals.remove(request.headers["range"])
                # Once the others are receiving, as a host at its limit refuses.
                time.sleep(0.03)
                return httpx.Response(429)
            response = cdn.answer(request)
            stream = response.stream
            assert isinstance(stream, Body)
            stream.chunk = 128
            stream.on_chunk = lambda: time.sleep(0.002)
            return response

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        url = "https://busy.cdn.example/v.mp4"
        with client(handler) as fake:
            saved = http.download(url, tmp / "v.mp4", client=fake)
        self.assertEqual(saved.path.read_bytes(), DATA)
        gate = download._gate(url)
        self.assertIsNotNone(gate.cap)
        self.assertLess(gate.allowed(), download.START_CONNECTIONS)


class StallTest(DownloadCase):
    def setUp(self) -> None:
        super().setUp()
        small(self)
        # The stall rules measure real time: a fraction of a second here.
        self.enterContext(clock.use(clock.Clock()))

    def test_a_piece_without_a_first_byte_goes_to_another_connection(self):
        stalled = ["bytes=2048-3071"]
        release = threading.Event()
        self.addCleanup(release.set)

        def maybe_stall(request: httpx.Request) -> httpx.Response | None:
            if request.headers.get("range") in stalled:
                stalled.clear()
                release.wait(5)
            return None

        self.cdn.script = [maybe_stall] * 20
        started = time.monotonic()
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(self.cdn.ranges_asked().count("bytes=2048-3071"), 2)

    def test_the_rules(self):
        worker = mock.Mock(first=None, sent=0.0, since=None, segment=None)
        rules = {"ttfb": 0.1, "best": 0.0, "disk": False}
        self.assertIsNone(download._stall(worker, 0.2, others=True, **rules))
        self.assertEqual(download._stall(worker, 0.21, others=True, **rules), "stalled")
        self.assertIsNone(download._stall(worker, 0.21, others=False, **rules))
        # A 20 ms floor, when the first answer came at once.
        self.assertEqual(download._stall(worker, 0.03, others=True,
                                         **{**rules, "ttfb": 0.0}), "stalled")

        segment = download._Segment(0, 10_000_000, done=100_000)
        slow = download._Worker.__new__(download._Worker)
        slow.first, slow.sent, slow.since = 0.0, 0.0, 0.0
        slow.received, slow.segment = 100_000, segment
        rules = {"ttfb": 0.1, "best": 1_000_000.0, "disk": False, "others": True}
        self.assertIsNone(download._stall(slow, 1.9, **rules))          # under 2 s: not judged
        self.assertEqual(download._stall(slow, 2.0, **rules), "slow")    # 50 KB/s < 250 KB/s
        self.assertIsNone(download._stall(slow, 2.0, **{**rules, "disk": True}))
        self.assertIsNone(download._stall(slow, 2.0, **{**rules, "best": 150_000.0}))
        segment.done = segment.end - 50_000                              # 1 s left at its pace
        self.assertIsNone(download._stall(slow, 2.0, **rules))


class DigestTest(DownloadCase):
    def answer(self, headers: dict[str, str]) -> Callable[[httpx.Request], httpx.Response]:
        return lambda request: self.cdn.answer(request, headers=headers)

    def test_a_matching_digest_passes_in_every_spelling(self):
        sha = base64.b64encode(hashlib.sha256(DATA).digest()).decode()
        md5 = base64.b64encode(hashlib.md5(DATA).digest()).decode()
        for headers in ({"Repr-Digest": f"sha-256=:{sha}:"}, {"Digest": f"SHA-256={sha}"},
                        {"x-goog-hash": f"crc32c=AAAAAA==, md5={md5}"}, {"Content-MD5": md5}):
            with self.subTest(headers=headers):
                self.cdn.script = [self.answer(headers)]
                self.assertEqual(self.download(parts=1).read_bytes(), DATA)

    def test_a_digest_that_differs_downloads_the_file_again_then_raises(self):
        wrong = base64.b64encode(hashlib.sha256(b"other").digest()).decode()
        self.cdn.script = [self.answer({"Repr-Digest": f"sha-256=:{wrong}:"})]
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertEqual(self.cdn.ranges_asked(), [FIRST, FIRST])
        self.cdn.script = [self.answer({"Repr-Digest": f"sha-256=:{wrong}:"})] * 3
        with self.assertRaises(http.CorruptError) as raised:
            self.download()
        self.assertIsInstance(raised.exception, shared.BrokenError)
        self.assertEqual(self.leftovers(), [])

    def test_a_206s_content_md5_is_its_pieces_not_the_files(self):
        piece = base64.b64encode(hashlib.md5(b"a piece").digest()).decode()
        self.cdn.script = [self.answer({"Content-MD5": piece})]
        self.assertEqual(self.download().read_bytes(), DATA)
        self.cdn.script = [self.answer({"Content-MD5": piece})]
        with self.assertRaises(http.CorruptError):
            self.download(parts=1, attempts=1)


class SpaceTest(DownloadCase):
    def test_a_big_file_needs_its_size_plus_128_mb_free(self):
        self.enterContext(mock.patch.object(download, "SPLIT_MIN", 4096))
        usage = mock.Mock(free=len(DATA) + download.SPACE_MARGIN - 1)
        with mock.patch.object(download.shutil, "disk_usage", return_value=usage), \
                self.assertRaises(http.SpaceError):
            self.download(["https://a.example/v.mp4", "https://b.example/v.mp4"])
        self.assertEqual(len(self.cdn.requests), 1)
        self.assertEqual(self.leftovers(), [])
        usage.free += 1
        with mock.patch.object(download.shutil, "disk_usage", return_value=usage):
            self.assertEqual(self.download().read_bytes(), DATA)

    def test_fat32_refuses_a_file_of_4_gib(self):
        usage = mock.Mock(free=10 * download.FAT_LIMIT)
        with mock.patch.object(download, "_filesystem", return_value="msdos"), \
                mock.patch.object(download.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(http.SpaceError) as raised:
                download._room(self.tmp, download.FAT_LIMIT)
            self.assertIn("FAT32", str(raised.exception))
            download._room(self.tmp, download.FAT_LIMIT - 1)

    @unittest.skipIf(os.name == "nt", "a temporary folder's volume may be anything on Windows")
    def test_this_machines_file_system_has_a_name(self):
        self.assertNotEqual(download._filesystem(self.tmp), "")


class DownloadRecordTest(DownloadCase):
    def test_a_download_gives_its_path_url_and_headers(self):
        with client(self.cdn.handler) as fake:
            saved = http.download("https://cdn.example/v.mp4?sig=x", self.tmp, client=fake,
                                  parts=1)
        self.assertEqual(saved.path, self.tmp / "v.mp4")
        self.assertEqual(saved.url, "https://cdn.example/v.mp4?sig=x")
        self.assertEqual(saved.headers["ETag"], '"v1"')
        self.assertEqual(saved.headers["content-length"], str(len(DATA)))

    def test_a_resumed_download_gives_the_resuming_responses_headers(self):
        self.cdn.script = [self.cdn.cut_at(3000)]
        with client(self.cdn.handler) as fake:
            saved = http.download("https://cdn.example/v.mp4", self.tmp / "v.mp4", client=fake)
        self.assertEqual(saved.headers["content-range"], f"bytes 3000-{len(DATA) - 1}/{len(DATA)}")


class StopTest(DownloadCase):
    def test_a_stop_between_chunks_raises_and_keeps_the_part_to_resume(self):
        stop = threading.Event()

        def progress(done: int, total: int | None) -> None:
            if done >= 4096:
                stop.set()

        with self.assertRaises(http.StoppedError):
            self.download(progress=progress, stop=stop)
        self.assertEqual(len(self.leftovers()), 2)
        self.cdn.requests.clear()
        self.assertEqual(self.download().read_bytes(), DATA)
        self.assertTrue(self.cdn.ranges_asked()[0].startswith("bytes=4096"))

    def test_a_stalled_read_is_stopped_within_a_second(self):
        stop = threading.Event()
        headers = {"ETag": '"v1"', "Content-Length": str(len(DATA))}
        self.cdn.script = [lambda request: httpx.Response(200, headers=headers,
                                                          stream=StallingBody(DATA))]
        threading.Timer(0.2, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(http.StoppedError):
            self.download(stop=stop)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(self.leftovers()), 2)

    def test_a_wait_for_a_full_host_is_stopped(self):
        stop = threading.Event()
        url = "https://full.cdn.example/v.mp4"
        gate = download._gate(url)
        while gate.take():
            pass
        threading.Timer(0.2, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(http.StoppedError):
            self.download(url, stop=stop)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_retry_is_stopped(self):
        stop = threading.Event()

        def down(request: httpx.Request) -> httpx.Response:
            stop.set()
            return httpx.Response(503)

        self.cdn.script = [down]
        with self.assertRaises(http.StoppedError):
            self.download(stop=stop)
        self.assertEqual(len(self.cdn.requests), 1)


if __name__ == "__main__":
    unittest.main()
