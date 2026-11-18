"""The hand check behind the media cap's ceiling: throughput from one real CDN host per cap.

    make cdn-real ARGS="--urls ~/flnd-cdn.txt"                       # caps 8, 16, 24, 32
    make cdn-real ARGS="--urls ~/flnd-cdn.txt --caps 4,8,16 --rounds 3"
    make cdn-real ARGS="--urls ~/flnd-cdn.txt --header 'Referer: https://www.example.com/'"

Loopback can't show where a real CDN stops giving more for more transfers at once, so the
ceiling a host's adaptive cap grows to (`flnd.engine.site.MEDIA_MOST`) is set by hand: the URLs of
about 200 files on one CDN host, taken from the authorized test accounts (from the browser's
network panel, say), one per line in a file kept outside the repo, or given as arguments. This
script is never part of `make check` or `make bench`. It prints no URL, file name or host, only
numbers, so its output can be pasted into flnd/docs/performance.md as it is.

How it goes:
1. Each round runs every cap in turn (rounds alternate, so a change in the link's load spreads
   over every cap). A cap downloads every URL through flnd's own client (`flnd.kit.net`, curl_cffi
   on a `flnd.kit.loop.Loop`, the part and rename of `flnd.engine.transfer`) with a fixed cap: `Limits`
   with `most` equal to `cap`, so it never grows, though an overload still halves it.
2. Then, once per round, the default adaptive cap (8, growing to 32) runs the same list.
3. Every file goes into a scratch folder, removed at the end; each run starts empty.
4. One table is printed: for each cap, the median seconds and MiB/s over the rounds, the files
   that failed, and the cap a run ended at (lower than asked when the host overloaded).
Pick the smallest cap past which MiB/s stops rising by more than a few per cent.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from flnd.engine import transfer as _transfer
from flnd.kit import net as _net
from flnd.kit.loop import Loop
from flnd.ui.log import ui


def arguments() -> argparse.Namespace:
    """The command line: the URLs, the caps, the rounds and any headers."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("url", nargs="*", help="a file's URL; more may come from --urls")
    parser.add_argument("--urls", type=Path, help="a file of URLs, one per line")
    parser.add_argument("--caps", default="8,16,24,32", help="fixed caps to try, by commas")
    parser.add_argument("--rounds", type=int, default=1, help="runs of every cap (median)")
    parser.add_argument("--header", action="append", default=[],
                        help="'Name: value' sent with every request (a Referer the CDN needs)")
    parser.add_argument("--no-adaptive", action="store_true",
                        help="skip the run with the default adaptive cap")
    return parser.parse_args()


def urls_from(args: argparse.Namespace) -> list[str]:
    """Every URL given, in order, with blank lines and `#` comments dropped."""
    urls = list(args.url)
    if args.urls is not None:
        lines = args.urls.expanduser().read_text().splitlines()
        urls += [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    return urls


async def fetch_all(limits: _net.Limits, urls: list[str], headers: dict[str, str],
                    out: Path) -> tuple[float, int, int, int]:
    """Download every URL into `out` under `limits`, one task each.

    Returns:
        The seconds it took, the bytes and the failures, and the host's cap at the end.
    """
    session = _net.Session(handles=limits.total)
    client = _net.Client(session, limits=limits, pace=False)
    failed = 0

    async def one(n: int, url: str) -> int:
        nonlocal failed
        try:
            async with _transfer._held(out / str(n)) as held:
                await client.fill(url, held, headers=headers, resume=False)
                return held.size
        except Exception:
            failed += 1
            return 0

    start = time.perf_counter()
    try:
        sizes = await asyncio.gather(*(one(n, url) for n, url in enumerate(urls)))
    finally:
        await session.close()
    took = time.perf_counter() - start
    loads = list(limits.load.values())
    return took, sum(sizes), failed, loads[0].cap if loads else 0


def main() -> None:
    """Run every cap the given number of rounds, and print the table."""
    args = arguments()
    urls = urls_from(args)
    if not urls:
        raise SystemExit("give URLs, or --urls FILE")
    if len({urlsplit(url).hostname for url in urls}) != 1:
        raise SystemExit("every URL must be on one host: the cap is per host")
    headers = dict(tuple(part.strip() for part in h.split(":", 1)) for h in args.header)
    caps = [int(cap) for cap in args.caps.split(",")]
    runs: list[tuple[str, int, int | None]] = [(str(cap), cap, cap) for cap in caps]
    if not args.no_adaptive:
        runs.append((f"{_net.CAP} to {_net.MOST}", _net.CAP, None))
    seen: dict[str, list[tuple[float, int, int, int]]] = {name: [] for name, _, _ in runs}
    with tempfile.TemporaryDirectory(prefix="flnd-cdn-") as scratch, Loop() as loop, \
            ui.scope(dest=io.StringIO(), color=False):
        out = Path(scratch)
        for round in range(args.rounds):
            for name, start, most in runs:
                shutil.rmtree(out, ignore_errors=True)
                out.mkdir()
                limits = _net.Limits(cap=start, most=most if most is not None else _net.MOST)
                result = loop.run(fetch_all(limits, urls, headers, out))
                seen[name].append(result)
                print(f"round {round + 1}, cap {name}: {result[0]:.2f} s", file=sys.stderr)
    print(f"{len(urls)} files, {args.rounds} round(s)")
    print(f"{'cap':>10} {'seconds':>9} {'MiB/s':>8} {'failed':>7} {'ended at':>9}")
    for name, results in seen.items():
        took = statistics.median(r[0] for r in results)
        rate = statistics.median(r[1] / r[0] / (1 << 20) for r in results)
        failed = sum(r[2] for r in results)
        ended = min(r[3] for r in results)
        print(f"{name:>10} {took:>9.2f} {rate:>8.1f} {failed:>7} {ended:>9}")


if __name__ == "__main__":
    main()
