"""What flnd's processes use: its own and every one below it, and the browser's alone.

SPEC.md#commands-and-flags explains it.
"""

from __future__ import annotations

import os
import sys

from flnd.engine.events import Resources

__all__ = ["Meter"]

# Parts of a browser process's name (Chromium, its headless shell and helpers, Firefox, WebKit).
BROWSER = ("chrom", "headless", "firefox", "webkit", "msedge")


class Meter:
    """Reads flnd's process tree: itself and its children, whatever they run (the browser, an
    transform program). A process object lives across reads, since CPU % is the time used
    since the last read of the same process; the first read of a process says 0.

    Example:
        >>> Meter().read().procs >= 1
        True
    """

    def __init__(self) -> None:
        self._seen: dict[int, object] = {}

    def read(self) -> Resources:
        """The tree's process count, resident memory and CPU %, and the browser's share.

        Returns:
            The reading; processes that end while it is taken are left out.
        """
        import psutil

        root = self._seen.get(os.getpid()) or psutil.Process()
        try:
            found = [root, *root.children(recursive=True)]
        except psutil.Error:
            found = [root]
        live: dict[int, object] = {}
        total, browser = [0, 0, 0.0], [0, 0, 0.0]
        for proc in found:
            proc = self._seen.get(proc.pid, proc)
            try:
                with proc.oneshot():
                    cpu, name = proc.cpu_percent(None), proc.name().lower()
                    try:
                        win = sys.platform == "win32"
                        mem = getattr(proc.memory_info() if win else proc.memory_full_info(),
                                      "private" if win else "uss", None)
                        rss = mem if isinstance(mem, int) else proc.memory_info().rss
                    except Exception:
                        rss = getattr(proc.memory_info(), "rss", 0)
            except psutil.Error:
                continue
            live[proc.pid] = proc
            for row in ([total, browser] if any(b in name for b in BROWSER) else [total]):
                row[0], row[1], row[2] = row[0] + 1, row[1] + rss, row[2] + cpu
        self._seen = live
        return Resources(total[0], total[1], total[2], browser[0], browser[1], browser[2])
