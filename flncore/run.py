"""Run work on its own thread, read as events; 2-stage Ctrl-C handling."""

from __future__ import annotations

import contextlib
import contextvars
import queue
import signal
import sys
import threading
import warnings
from collections.abc import Callable, Iterator
from typing import Any

__all__ = ["Run", "Stop", "stopping"]

QUEUE = 256
WAKE = 0.1
_END = object()
STOPPED = "stopped by Ctrl-C"
AGAIN = "stopping now: the files in flight are cancelled; Ctrl-C again to quit now"
QUIT_STATUS = 130


class Stop:
    """A run's two-stage stop state: `soon` stops listing, `now` cancels in-flight work."""

    def __init__(self) -> None:
        self.soon = threading.Event()
        self.now = threading.Event()
        self.suspended = threading.Event()

    @property
    def stage(self) -> str:
        if self.now.is_set():
            return "now"
        if self.soon.is_set():
            return "soon"
        return "running"

    def stop(self, *, now: bool = False) -> None:
        """Ask the run to stop."""
        self.soon.set()
        if now:
            self.now.set()


class Run:
    """Work on its own thread, read as an event stream."""

    def __init__(
        self,
        work: Callable[[Callable[[Any], None], Stop], Any],
        *,
        name: str = "run",
    ) -> None:
        self._work = work
        self.name = name
        self.stop = Stop()
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=QUEUE)
        self._thread: threading.Thread | None = None
        self._result: Any = None
        self._error: BaseException | None = None
        self._started = False
        self._ended = threading.Event()

    def __iter__(self) -> Iterator[Any]:
        self._start()
        with stopping(self.stop):
            while True:
                try:
                    item = self._queue.get(timeout=WAKE)
                except queue.Empty:
                    if self._ended.is_set():
                        break
                    continue
                if item is _END:
                    break
                yield item
        if self._error is not None:
            raise self._error

    def wait(self) -> Any:
        """Wait for the work to end and return its result."""
        self._start()
        with stopping(self.stop):
            while not self._ended.wait(timeout=WAKE):
                pass
        if self._error is not None:
            raise self._error
        return self._result

    def _start(self) -> None:
        if self._started:
            return
        self._started = True
        context = contextvars.copy_context()

        def runner() -> None:
            try:
                self._result = self._work(self._emit, self.stop)
            except BaseException as e:
                self._error = e
            finally:
                self._ended.set()
                self._queue.put(_END)

        thread = threading.Thread(
            target=context.run, args=(runner,), name=f"flncore.{self.name}", daemon=True
        )
        thread.start()
        self._thread = thread

    def _emit(self, event: Any) -> None:
        """Called by work to put an event on the queue."""
        if self.stop.now.is_set():
            return
        while not self.stop.now.is_set():
            try:
                self._queue.put(event, timeout=WAKE)
                return
            except queue.Full:
                continue

    def __del__(self) -> None:
        if not self._started:
            warnings.warn(
                f"run {self.name!r} was never started: iterate it, or call wait()",
                ResourceWarning,
                stacklevel=2,
            )


@contextlib.contextmanager
def stopping(stop: Stop) -> Iterator[None]:
    """Context manager installing Ctrl-C handler for `stop`."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    old_handler = signal.getsignal(signal.SIGINT)

    def on_sigint(sig: int, frame: Any) -> None:
        if not stop.soon.is_set():
            stop.soon.set()
            sys.stderr.write("! stopping soon: finishing in-flight tasks...\n")
            sys.stderr.flush()
        elif not stop.now.is_set():
            stop.now.set()
            sys.stderr.write("! stopping now: cancelling tasks...\n")
            sys.stderr.flush()
        else:
            sys.exit(QUIT_STATUS)

    try:
        signal.signal(signal.SIGINT, on_sigint)
        yield
    except (ValueError, AttributeError):
        yield
    finally:
        try:
            signal.signal(signal.SIGINT, old_handler)
        except (ValueError, AttributeError):
            pass
