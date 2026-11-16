"""Benchmark group `net`: flnd's HTTP client downloading from a loopback server to disk.

The client is the one flnd builds for a source today (`flnd.common.net.client` wrapped by
`flnd.site._make_client`), so these numbers are what a real download pays, minus the network.
Loopback shows CPU and per-request overhead and hides latency and bandwidth; the `e2e` group
adds simulated latency.

Steps:
1. write a 1 GiB file and 2,000 files of 100 KiB into a scratch folder (random bytes, so no
   layer can compress them);
2. serve the folder with `server.py` on 127.0.0.1;
3. download the big file (best of 3), then the 2,000 small files on 1 thread and on 8 threads.

Each run starts with an empty output folder, so every file is written fresh.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import kit
from flnd.common import net
from flnd.site import _make_client

BIG = 1 << 30
# The big file is written in pieces of this size, so making it never holds 1 GiB in memory.
PIECE = 64 << 20
SMALL, COUNT = 100 * 1024, 2000
THREADS = 8


def make_data(folder: Path) -> None:
    """Write `big.bin` (1 GiB) and `s0.bin` … `s1999.bin` (100 KiB each) into `folder`.

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


def main() -> None:
    """Measure the three downloads and emit the report."""
    report = kit.Report("net")
    with tempfile.TemporaryDirectory(prefix="flnd-bench-") as scratch:
        data, out = Path(scratch, "data"), Path(scratch, "out")
        data.mkdir()
        make_data(data)

        def empty() -> None:
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir()

        raw = net.client(user_agent=None, jar=(), sign=lambda request: None)
        with kit.serve(data) as base, _make_client(raw, rate=None, stop=None) as client:
            took = kit.best(lambda: client.download(f"{base}/big.bin", out / "big"), before=empty)
            report.add("big_rate", BIG / took / (1 << 30), "GiB/s", better="higher")

            urls = [f"{base}/s{n}.bin" for n in range(COUNT)]

            def one_thread() -> None:
                for n, url in enumerate(urls):
                    client.download(url, out / str(n))

            report.add("small_1_thread", kit.best(one_thread, before=empty) * 1e3, "ms")

            with ThreadPoolExecutor(THREADS) as pool:
                def threads() -> None:
                    list(pool.map(lambda n: client.download(urls[n], out / str(n)), range(COUNT)))

                report.add("small_8_threads", kit.best(threads, before=empty) * 1e3, "ms")
    report.emit()


if __name__ == "__main__":
    main()
