"""Benchmark group `net`: flnd's HTTP client downloading from a loopback server to disk.

The client is the one flnd builds for a source's downloads (`flnd._net.Client` over a
`Session`, with the per-host cap of `flnd.site`), on a `flnd._loop.Loop` as in a run, so these
numbers are what a real download pays, minus the network. Loopback shows CPU and per-request
overhead and hides latency and bandwidth; the `e2e` group adds simulated latency.

Steps:
1. write a 1 GiB file, 2,000 files of 100 KiB and 4 of 16 MiB into a scratch folder (random
   bytes, so no layer can compress them);
2. serve the folder with `server.py` on 127.0.0.1;
3. download the big file (best of 3), then the 2,000 small files one at a time and 8 at once
   (the default cap per host);
4. download the four 16 MiB files at once onto a disk throttled to 20 MB/s (each write sleeps
   for its size at that rate), while a probe on the loop wakes every 5 ms: `stall_20mbs` is the
   longest the loop was held up, which must stay under 100 ms (docs/performance).

Each run starts with an empty output folder, so every file is written fresh.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock

import kit
from flnd import _files, _net, _transfer
from flnd._loop import Loop
from flnd._ui import ui

BIG = 1 << 30
# The big file is written in pieces of this size, so making it never holds 1 GiB in memory.
PIECE = 64 << 20
SMALL, COUNT = 100 * 1024, 2000
# Transfers at once to one host: flnd's default cap (`flnd.site.MEDIA_CAP`).
AT_ONCE = 8
# The slow disk: four files of 16 MiB written at 20 MB/s.
MEDIUM, SLOW_FILES, DISK_RATE = 16 << 20, 4, 20e6
PROBE = 0.005


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


async def fill(client: _net.Client, url: str, dest: Path) -> None:
    """Download `url` into `dest` as a source's file is: its part opened and landed off the loop
    (`flnd._transfer`), filled by the client. No progress row: that is the UI's cost (`cpu`)."""
    async with _transfer._held(dest) as held:
        await client.fill(url, held)


async def many(client: _net.Client, urls: list[str], out: Path) -> None:
    """Download every URL, each a task; the client's cap per host bounds how many at once."""
    await asyncio.gather(*(fill(client, url, out / str(n)) for n, url in enumerate(urls)))


async def stalled(client: _net.Client, urls: list[str], out: Path) -> float:
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
            session = _net.Session(handles=_net.PAGES + AT_ONCE)
            one = _net.Client(session, limits=_net.Limits(cap=1), pace=False)
            capped = _net.Client(session, limits=_net.Limits(cap=AT_ONCE), pace=False)

            took = kit.best(lambda: loop.run(fill(capped, f"{base}/big.bin", out / "big")),
                            before=empty)
            report.add("big_rate", BIG / took / (1 << 30), "GiB/s", better="higher")

            urls = [f"{base}/s{n}.bin" for n in range(COUNT)]
            report.add("small_1_at_once", kit.best(lambda: loop.run(many(one, urls, out)),
                                                   before=empty) * 1e3, "ms")
            report.add("small_8_at_once", kit.best(lambda: loop.run(many(capped, urls, out)),
                                                   before=empty) * 1e3, "ms")

            real = _files.Part.write

            def slow(held: _files.Part, chunk: bytes) -> None:
                time.sleep(len(chunk) / DISK_RATE)
                real(held, chunk)

            empty()
            medium = [f"{base}/m{n}.bin" for n in range(SLOW_FILES)]
            with mock.patch.object(_files.Part, "write", slow):
                worst = loop.run(stalled(capped, medium, out))
            report.add("stall_20mbs", worst * 1e3, "ms")
            loop.run(session.close())
    report.emit()


if __name__ == "__main__":
    main()
