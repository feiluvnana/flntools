"""Benchmark group `net`: flnd's HTTP client downloading from a loopback server to disk.

Each file goes through the run's save (`flnd.engine.save.Saver`): its transfer a `FILE` job
of the run's scheduler (`flnd.engine.jobs`) under a fixed cap per host, by the client flnd
builds for a source's downloads (`flnkit.net.Client` over a `Session`), on a
`flnkit.loop.Loop` as in a run, so these numbers are what a real download pays, minus the
network and flnd.db (the save's rows go to a store that keeps nothing; `e2e` counts them). Loopback shows CPU and per-request
overhead and hides latency and bandwidth; the `e2e` group adds simulated latency.

Steps:
1. write a 1 GiB file, 2,000 files of 100 KiB and 4 of 16 MiB into a scratch folder (random
   bytes, so no layer can compress them);
2. serve the folder with `server.py` on 127.0.0.1;
3. download the big file (best of 3), then the 2,000 small files one at a time and 8 at once
   (the default cap per host);
4. download the four 16 MiB files at once onto a disk throttled to 20 MB/s (each write sleeps
   for its size at that rate), while a probe on the loop wakes every 5 ms: `stall_20mbs` is the
   longest the loop was held up, which must stay under 100 ms (src/docs/performance);
5. `two_disks`: sixteen 16 MiB downloads to a fast folder, alone and then while a second
   client (a second target) writes the four 16 MiB files to a folder throttled to 20 MB/s, as
   the percentage the slow disk adds to the fast target's time. Both folders sit in one
   scratch folder, so `os.stat` is wrapped meanwhile (`own_device`): a path under the slow
   folder reports a device of its own. A writer per device sees
   two disks only if it looks the device up through `os.stat`;
6. `per_conn_limit`: the 1 GiB file from `/capped/`, where the server sends each connection at
   20 MB/s, as flnd's throughput over that of one plain connection (`http.client`, 64 MiB);
7. `big_4_at_once`: four downloads of a 256 MiB file at once, each then hashed (SHA-256) as
   dedupe hashes a file of a size already saved, a `HASH` job, as their throughput over that
   of one alone;
8. `hls_200`: a playlist of 200 segments of 64 KiB, each answered 40 ms late (`/late/`), as
   the time of fetching them serially over one plain connection divided by flnd's time: the
   playlist read by `flnkit.stream.hls` and its segments fetched 8 at once by `fetch_all`
   into one part, written in order (`hls_at_once`).

Each run starts with an empty output folder, so every file is written fresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import io
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import kit
from kit import Loop

from flnd.engine import jobs, save
from flnd.plugin import File, Target
from flnd.ui.log import ui
from flnkit import files as _files
from flnkit import net as _net
from flnkit import stream as _stream

BIG = kit.size(1 << 30, 1 << 20)
# The big file is written in pieces of this size, so making it never holds 1 GiB in memory.
PIECE = min(64 << 20, BIG)
SMALL, COUNT = 100 * 1024, kit.size(2000, 20)
# Transfers at once to one host: where flnd's cap starts (`flnd.engine.jobs.CAP`), held there.
AT_ONCE = jobs.CAP
# The slow disk: four files of 16 MiB written at 20 MB/s.
MEDIUM, SLOW_FILES, DISK_RATE = kit.size(16 << 20, 1 << 20), 4, 20e6
PROBE = 0.005
# Step 5: the fast target's downloads, four of each 16 MiB file.
FAST_COPIES = 4
# Step 6: how much one plain connection reads to measure the server's cap.
REFERENCE = min(64 << 20, BIG)
# Steps 7 and 8: the file downloaded four times at once, and the playlist's segments.
QUARTER, AT_ONCE_BIG = kit.size(256 << 20, 1 << 20), 4
SEGMENT, SEGMENTS = 64 * 1024, kit.size(200, 4)


def make_data(folder: Path) -> None:
    """Write the files the steps download into `folder`.

    Args:
        folder: an empty scratch folder.
    """
    piece = os.urandom(PIECE)
    with open(folder / "big.bin", "wb") as big:
        for _ in range(BIG // PIECE):
            big.write(piece)
    small = os.urandom(SMALL)
    for n in range(COUNT):
        (folder / f"s{n}.bin").write_bytes(small)
    medium = os.urandom(MEDIUM)
    for n in range(SLOW_FILES):
        (folder / f"m{n}.bin").write_bytes(medium)
    (folder / "q.bin").write_bytes(os.urandom(QUARTER))
    segment = os.urandom(SEGMENT)
    for n in range(SEGMENTS):
        (folder / f"seg{n}.ts").write_bytes(segment)
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2"]
    for n in range(SEGMENTS):
        lines += ["#EXTINF:2.0,", f"seg{n}.ts"]
    (folder / "hls.m3u8").write_text("\n".join([*lines, "#EXT-X-ENDLIST", ""]))


class _Kept:
    """A store that keeps nothing: the save's rows are `e2e`'s to time, not this group's."""

    path = Path("flnd.db")
    due = False

    def twins(self, **asked: Any) -> list[Any]:
        return []

    def commit(self, *args: Any, **options: Any) -> None:
        pass

    def update(self, *args: Any, **fields: Any) -> None:
        pass

    def checkpoint(self, *, force: bool = False) -> None:
        pass


class Capped:
    """A target's saves on a scheduler of its own, held to `at_once` transfers per host (a
    limit at or under the cap's start fixes it there)."""

    def __init__(self, session: _net.Session, at_once: int, *, name: str = "example_user",
                 hashed: bool = False) -> None:
        # Whether each file (all `QUARTER` bytes) is hashed once it lands, as dedupe hashes one
        # of a size saved before.
        self.hashed = hashed
        self.client = _net.Client(session)
        self.scheduler = jobs.Scheduler(limits={"127.0.0.1": at_once, "localhost": at_once})
        self.saver = save.Saver(_Kept(), self.scheduler, lambda event: None,  # type: ignore[arg-type]
                                dedupe=False, sidecars=False)
        self.target = Target(f"bnc:{name}", kind="user")


async def fill(client: Capped, url: str, dest: Path) -> None:
    """Download `url` into `dest` as a source's file is, through the save: its part opened and
    landed off the loop, filled by the client. No progress row: that is the UI's cost (`cpu`)."""
    item = save.Item(f"bnc:{dest.name}", client.target, dest.name, "", File(url), dest)
    await client.saver.post([item], client=client.client, plugin="bnc")
    if client.hashed:
        await client.saver._hash(dest, QUARTER, "bnc")


async def many(client: Capped, urls: list[str], out: Path) -> None:
    """Download every URL, each a task; the scheduler's cap per host bounds how many at once."""
    await asyncio.gather(*(fill(client, url, out / str(n)) for n, url in enumerate(urls)))


async def stalled(client: Capped, urls: list[str], out: Path) -> float:
    """Download `urls` at once while a probe measures how late the loop wakes it.

    Returns:
        The longest stall seen, in seconds.
    """
    lags: list[float] = []
    done = asyncio.Event()

    async def probe() -> None:
        loop = asyncio.get_running_loop()
        while not done.is_set():
            at = loop.time()
            await asyncio.sleep(PROBE)
            lags.append(loop.time() - at - PROBE)

    probing = asyncio.ensure_future(probe())
    await many(client, urls, out)
    done.set()
    await probing
    return max(lags)


def plain(base: str, paths: list[str], *, span: str | None = None) -> float:
    """Fetch `paths` in order over one plain keep-alive connection, and return the wall time.

    Args:
        base: the server's base URL.
        paths: what to fetch, each read whole and dropped.
        span: a `Range` header's value, sent with each request; None for the whole file.
    """
    port = int(base.rsplit(":", 1)[1])
    connection = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Range": span} if span else {}
    start = time.perf_counter()
    try:
        for path in paths:
            connection.request("GET", path, headers=headers)
            answer = connection.getresponse()
            while answer.read(1 << 20):
                pass
    finally:
        connection.close()
    return time.perf_counter() - start


async def hls_at_once(client: Capped, url: str, dest: Path) -> None:
    """Download a playlist with `flnkit.stream`: its segments `AT_ONCE` at a time, in order."""
    playlist = _stream.hls((await client.client.fetch(url)).text, url)

    async def get(segment: str, span: tuple[int, int] | None) -> bytes:
        headers = {"Range": f"bytes={span[0]}-{span[1] - 1}"} if span else None
        return (await client.client.fetch(segment, headers=headers)).content

    with _files.part(dest, resume=True) as held:
        async def write(data: bytes) -> None:
            held.write(data)

        await _stream.fetch_all(playlist.segments, get, write, at_once=AT_ONCE)


async def raced(fast: Capped, slow: Capped, fast_urls: list[str],
                slow_urls: list[str], out: Path, slow_out: Path) -> float:
    """Download `fast_urls` while `slow_urls` go to the throttled folder; return the fast time.

    The slow downloads are cancelled once the fast ones are done: only the fast target's time
    is measured.
    """
    behind = asyncio.ensure_future(many(slow, slow_urls, slow_out))
    start = time.perf_counter()
    await many(fast, fast_urls, out)
    took = time.perf_counter() - start
    behind.cancel()
    await asyncio.gather(behind, return_exceptions=True)
    return took


class _OtherDevice:
    """A stat result that says `st_dev` is another device; every other field is the real one."""

    def __init__(self, real: os.stat_result, device: int) -> None:
        self._real, self.st_dev = real, device

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __getitem__(self, index: int) -> Any:
        return self.st_dev if index == 2 else self._real[index]

    def __len__(self) -> int:
        return len(self._real)


@contextlib.contextmanager
def own_device(folder: Path) -> Iterator[None]:
    """Inside the block, `os.stat` (and so `Path.stat`) of a path under `folder` reports a
    device of its own, so the two folders of `two_disks` look like two disks."""
    real = os.stat
    inside = str(folder)
    device = real(folder).st_dev + 1

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        found = real(path, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)) and os.fspath(path).startswith(inside):
            return _OtherDevice(found, device)
        return found

    with mock.patch.object(os, "stat", stat):
        yield


def two_disks(report: kit.Report, loop: Loop, clients: tuple[Capped, Capped],
              base: str, folders: tuple[Path, Path], empty: Any) -> None:
    """Time step 5: the fast target alone, then beside the slow one.

    Args:
        clients: the fast target's client and the slow one's.
        folders: the fast folder and the throttled one.
    """
    (fast, slow), (out, slow_out) = clients, folders
    fast_urls = [f"{base}/m{n % SLOW_FILES}.bin?{n}" for n in range(SLOW_FILES * FAST_COPIES)]
    slow_urls = [f"{base}/m{n}.bin?slow" for n in range(SLOW_FILES)]
    alone = kit.best(lambda: loop.run(many(fast, fast_urls, out)), before=empty)
    real = _files.Part.write
    throttled = str(slow_out)

    def slow_disk(held: _files.Part, chunk: bytes, **how: bool) -> None:
        if str(held.path).startswith(throttled):
            time.sleep(len(chunk) / DISK_RATE)
        real(held, chunk, **how)

    def empty_both() -> None:
        empty()
        shutil.rmtree(slow_out, ignore_errors=True)
        slow_out.mkdir()

    both = float("inf")
    with mock.patch.object(_files.Part, "write", slow_disk):
        for _ in range(1 if kit.SMOKE else 3):
            empty_both()
            both = min(both, loop.run(raced(fast, slow, fast_urls, slow_urls, out, slow_out)))
    report.add("two_disks", (both / alone - 1) * 100, "%")


def segments_and_disks(report: kit.Report, loop: Loop, base: str, scratch: Path,
                       empty: Any) -> None:
    """Time steps 5 to 8: two disks, the per-connection cap, four big files, the playlist."""
    out, slow_out = scratch / "out", scratch / "slow"
    session = _net.Session(handles=jobs.PAGES + AT_ONCE)
    second = _net.Session(handles=jobs.PAGES + AT_ONCE)
    capped, other = Capped(session, AT_ONCE), Capped(second, AT_ONCE, name="example_creator")

    slow_out.mkdir()
    with own_device(slow_out):
        two_disks(report, loop, (capped, other), base, (out, slow_out), empty)

    one = plain(base, ["/capped/big.bin"], span=f"bytes=0-{REFERENCE - 1}")
    empty()
    took = kit.best(lambda: loop.run(fill(capped, f"{base}/capped/big.bin", out / "big")),
                    runs=1)
    report.add("per_conn_limit", (BIG / took) / (REFERENCE / one), "x", better="higher")

    hashed = Capped(session, AT_ONCE, hashed=True)
    single = kit.best(lambda: loop.run(fill(hashed, f"{base}/q.bin", out / "q")), before=empty)
    quads = [f"{base}/q.bin?{n}" for n in range(AT_ONCE_BIG)]
    four = kit.best(lambda: loop.run(many(hashed, quads, out)), before=empty)
    report.add("big_4_at_once", (AT_ONCE_BIG * QUARTER / four) / (QUARTER / single), "x",
               better="higher")

    paths = [f"/late/seg{n}.ts" for n in range(SEGMENTS)]
    serial = plain(base, paths)
    empty()
    took = kit.best(lambda: loop.run(hls_at_once(capped, f"{base}/late/hls.m3u8", out / "v.ts")),
                    runs=1)
    report.add("hls_200", serial / took, "x", better="higher")
    loop.run(session.close())
    loop.run(second.close())


def main() -> None:
    """Measure the downloads and emit the report."""
    report = kit.Report("net")
    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        data, out = Path(scratch, "data"), Path(scratch, "out")
        data.mkdir()
        make_data(data)

        def empty() -> None:
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir()

        # Any line the client logs goes to a buffer: the terminal's speed is no part of this.
        with kit.serve(data) as base, Loop() as loop, ui.scope(dest=io.StringIO(), color=False):
            session = _net.Session(handles=jobs.PAGES + AT_ONCE)
            # Fixed caps, so the numbers don't depend on how a cap adapts.
            one, capped = Capped(session, 1), Capped(session, AT_ONCE)

            took = kit.best(lambda: loop.run(fill(capped, f"{base}/big.bin", out / "big")),
                            before=empty)
            report.add("big_rate", BIG / took / (1 << 30), "GiB/s", better="higher")

            urls = [f"{base}/s{n}.bin" for n in range(COUNT)]
            report.add("small_1_at_once", kit.best(lambda: loop.run(many(one, urls, out)),
                                                   before=empty) * 1e3, "ms")
            report.add("small_8_at_once", kit.best(lambda: loop.run(many(capped, urls, out)),
                                                   before=empty) * 1e3, "ms")

            real = _files.Part.write

            def slow(held: _files.Part, chunk: bytes, **how: bool) -> None:
                time.sleep(len(chunk) / DISK_RATE)
                real(held, chunk, **how)

            empty()
            medium = [f"{base}/m{n}.bin" for n in range(SLOW_FILES)]
            with mock.patch.object(_files.Part, "write", slow):
                worst = loop.run(stalled(capped, medium, out))
            report.add("stall_20mbs", worst * 1e3, "ms")
            loop.run(session.close())
            segments_and_disks(report, loop, base, Path(scratch), empty)
    report.emit()


if __name__ == "__main__":
    main()
