"""`Run`: one download, follow or sync, started lazily and read as a stream of events.

`Flnd.download`, `follow` and `sync` return a `Run`. Nothing happens until someone reads it:

1. **Start.** The first `for event in run`, `run.wait()`, `async for` or `await run` starts a
   worker thread. The thread runs the engine command the `Run` was made with, in a copy of the
   caller's context (so a test's fake clock reaches it), with `_emit` as the engine's event sink
   and the run's own stop state.
2. **Events.** The engine (its listing thread and its download threads) calls `_emit` for each
   event. `_emit` counts the event into the report, then puts it on a bounded queue. When the
   queue is full, `_emit` waits: the listing waits while the script handles an event, so events
   never pile up in memory.
3. **Reading.** Iterating takes events off the queue as they come. `wait()` stops queueing
   (events are still counted), empties the queue and waits for the thread.
4. **End.** The thread puts an end marker on the queue. The reader then raises whatever ended
   the run: a `flnd.Error` that stopped it (a usage mistake, a full disk), or `flnd.Stopped` for
   Ctrl-C. A failed file or target is never raised: it is a `Failed` event and a line of
   `report.failed`. A run ended by `run.stop()` is not an error either: `report.stopped` says so.

Rules a change must keep:
- `_emit` is called from several threads, and may wait for the reader, so no caller holds a lock
  while calling it.
- Only the reader's thread installs the Ctrl-C handler, and only when it is the main thread: the
  first Ctrl-C sets `soon` (stop listing, finish what is in flight), the second `now`.
- A `Run` nobody starts warns with a `ResourceWarning` when it is dropped, the way a coroutine
  nobody awaited does: it is almost always a missing `.wait()`.
- The module imports only the stdlib and `flnd._events`, never the engine, so `flnd.Run` is
  cheap to reach.
"""

from __future__ import annotations

import contextlib
import contextvars
import queue
import signal
import threading
import warnings
from collections.abc import AsyncIterator, Callable, Generator, Iterator
from typing import TYPE_CHECKING, Any

from flnd._events import Failed, Planned, Report, Saved, Skipped

if TYPE_CHECKING:
    from flnd._events import Event

__all__ = ["Run", "Stop"]

# How many events wait for the reader before the run waits for it. Large enough that a reader
# printing a line per event never slows a download down; small enough to bound memory.
QUEUE = 256
# How often (in seconds) a waiting thread looks again at whether it should give up waiting.
WAKE = 0.1
# What the worker puts on the queue last, so the reader knows the run is over.
_END = object()
# The message of the `Stopped` a reader gets when Ctrl-C ended the run.
STOPPED = "stopped by Ctrl-C"


class Stop:
    """A run's two-stage stop state: `soon` stops listing, `now` cancels what is in flight.

    It has the shape of the engine's `Stopping` (two events) without importing the CLI's
    module, which imports a command-line framework.

    Example:
        >>> stop = Stop()
        >>> stop.soon.set()
        >>> stop.soon.is_set(), stop.now.is_set()
        (True, False)
    """

    def __init__(self) -> None:
        self.soon = threading.Event()
        self.now = threading.Event()


class Run:
    """One download, follow or sync: iterate it for its events, or `wait()` for its `Report`.

    A run starts when it is first iterated or waited on. Iterating yields `Saved`, `Skipped`,
    `Failed`, `Progress` and (for `dry=True`) `Planned` events as they happen. Breaking out of
    the loop pauses the run: iterating again goes on where it was, `wait()` finishes it, and
    leaving the `Flnd`'s `with` stops it.

    Make one with `Flnd.download`, `follow` or `sync`, not directly.

    Args:
        work: what the worker thread runs: called with the event sink and the stop state.
        name: what the run does, for its thread's name and its warning
            (`"download ins:example_user"`).
        closed: set when the run's `Flnd` is closed; a run may not start after that.

    Example:
        >>> run = Run(lambda emit, stop: None, name="nothing")
        >>> run.wait()
        Report(saved=0, skipped=0, bytes=0, planned=0, failed=(), stopped=False)
    """

    def __init__(
        self, work: Callable[[Callable[[Any], None], Stop], Any], *, name: str,
        closed: threading.Event | None = None,
    ) -> None:
        self.name = name
        self._work = work
        self._closed = closed or threading.Event()
        # The caller's context, so the worker sees what the caller set (a fake clock, a scope).
        self._context = contextvars.copy_context()
        self._stop = Stop()
        self._events: queue.Queue[Any] = queue.Queue(maxsize=QUEUE)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Whether events go on the queue; `wait()` turns it off, since nobody will read them.
        self._listening = True
        # Whether the end marker was read: iterating again yields nothing.
        self._over = False
        # What ended the worker, if anything did.
        self._error: BaseException | None = None
        # Whether `stop()` asked the run to stop, which makes its end not an error.
        self._asked = False
        # The report's counts, kept under `_lock`.
        self._saved = self._skipped = self._bytes = self._planned = 0
        self._failed: list[Failed] = []

    # -----------------------------------------------------------------------------------------
    # Reading the run
    # -----------------------------------------------------------------------------------------

    def __iter__(self) -> Iterator[Event]:
        """Start the run if needed, and yield its events as they happen."""
        return self._events_now()

    def _events_now(self) -> Generator[Event, None, None]:
        """The generator behind `__iter__`.

        Yields:
            Each event, in the order the engine made it.

        Raises:
            Error: what stopped the whole run (a usage mistake, a full disk).
            Stopped: Ctrl-C ended it.
        """
        self._start()
        with self._interrupts():
            while not self._over:
                item = self._take()
                if item is _END:
                    self._over = True
                    break
                yield item
        self._raise()

    def __aiter__(self) -> AsyncIterator[Event]:
        """Start the run if needed, and yield its events without blocking the caller's loop."""
        return self._events_async()

    async def _events_async(self) -> AsyncIterator[Event]:
        """The async generator behind `__aiter__`: each event is taken on a helper thread.

        Yields:
            Each event, in the order the engine made it.
        """
        import asyncio

        self._start()
        try:
            while not self._over:
                item = await asyncio.to_thread(self._take)
                if item is _END:
                    self._over = True
                    break
                yield item
        except asyncio.CancelledError:
            self.stop()
            raise
        self._raise()

    def __await__(self) -> Generator[Any, None, Report]:
        """`await run` is `run.wait()` on a helper thread, so the caller's loop keeps going."""
        return self._waited().__await__()

    async def _waited(self) -> Report:
        """Wait on a helper thread; a cancelled wait stops the run.

        Returns:
            The run's report.
        """
        import asyncio

        try:
            return await asyncio.to_thread(self.wait)
        except asyncio.CancelledError:
            self.stop()
            raise

    def wait(self) -> Report:
        """Run to the end, dropping the events, and return the report.

        Returns:
            The `Report`: counts, sizes and failures.

        Raises:
            Error: what stopped the whole run (a usage mistake, a full disk).
            Stopped: Ctrl-C ended it.

        Example:
            >>> Run(lambda emit, stop: None, name="nothing").wait().ok
            True
        """
        self._start()
        with self._interrupts():
            self._listening = False
            self._empty()
            thread = self._thread
            # A join with a timeout, in a loop, so Ctrl-C's handler runs while it waits.
            while thread is not None and thread.is_alive():
                thread.join(WAKE)
            self._empty()
            self._over = True
        self._raise()
        return self.report

    @property
    def report(self) -> Report:
        """The counts so far: at the end, the whole run's; also after a loop broke early."""
        with self._lock:
            return Report(saved=self._saved, skipped=self._skipped, bytes=self._bytes,
                          planned=self._planned, failed=tuple(self._failed),
                          stopped=self._asked)

    def stop(self) -> None:
        """Ask the run to stop gracefully, as a first Ctrl-C does.

        Listing stops, and the files already downloading finish. The run then ends normally,
        with `report.stopped` set: a stop asked for isn't an error. A run not started yet
        never starts.

        Example:
            >>> run = Run(lambda emit, stop: None, name="nothing")
            >>> run.stop()
            >>> run.wait().stopped
            True
        """
        with self._lock:
            self._asked = True
            if self._thread is None:
                # Nothing to stop: the run is over before it began, and won't warn when dropped.
                self._over = True
        self._stop.soon.set()

    def __repr__(self) -> str:
        if self._thread is None:
            state = "stopped" if self._over else "not started"
        else:
            state = "running" if self._thread.is_alive() else "done"
        return f"<flnd.Run {self.name!r} {state}>"

    def __del__(self) -> None:
        # A run nobody started did nothing, which is almost always a missing `.wait()`.
        if getattr(self, "_thread", 1) is None and not getattr(self, "_over", True):
            warnings.warn(f"run {self.name!r} was never started: iterate it, or call wait()",
                          ResourceWarning, stacklevel=1, source=self)

    # -----------------------------------------------------------------------------------------
    # The worker's side
    # -----------------------------------------------------------------------------------------

    def _emit(self, event: Any) -> None:
        """Count an event, then hand it to the reader, waiting while the queue is full.

        Called by the engine on its listing and download threads. It returns at once when
        nobody will read the event (`wait()` is draining the run).

        Args:
            event: a `Saved`, `Skipped`, `Failed`, `Progress` or `Planned` event.
        """
        with self._lock:
            if isinstance(event, Saved):
                self._saved += 1
                self._bytes += event.size
            elif isinstance(event, Skipped):
                self._skipped += 1
            elif isinstance(event, Failed):
                self._failed.append(event)
            elif isinstance(event, Planned):
                self._planned += 1
        self._put(event)

    def _put(self, item: Any) -> None:
        """Put one item on the queue, giving up as soon as nobody listens.

        Args:
            item: an event, or the end marker.
        """
        while self._listening:
            try:
                self._events.put(item, timeout=WAKE)
                return
            except queue.Full:
                continue

    def _start(self) -> None:
        """Start the worker thread, once.

        Raises:
            UsageError: the run's `Flnd` was closed before the run started.
        """
        with self._lock:
            if self._thread is not None or self._over:
                return
            if self._closed.is_set():
                # Marked over, so the error doesn't come with a warning when the run is dropped.
                self._over = True
                from flnd.errors import UsageError

                raise UsageError(f"can't start {self.name}: its Flnd is closed",
                                 hint="start runs inside `with flnd.open(home) as fl:`")
            self._thread = threading.Thread(target=self._context.run, args=(self._main,),
                                            name=f"flnd {self.name}", daemon=True)
        self._thread.start()

    def _main(self) -> None:
        """The worker thread: run the command, keep what ended it, and mark the end."""
        try:
            self._work(self._emit, self._stop)
        except BaseException as error:  # handed to the reader, who raises it (`_raise`)
            self._error = error
        finally:
            self._put(_END)

    def _take(self) -> Any:
        """The next item of the queue, waiting in short steps so Ctrl-C's handler can run.

        Returns:
            An event, or the end marker: the one the worker put, or one made here when the
            worker is gone and the queue is empty.
        """
        while True:
            try:
                return self._events.get(timeout=WAKE)
            except queue.Empty:
                thread = self._thread
                if thread is not None and not thread.is_alive() and self._events.empty():
                    return _END

    def _empty(self) -> None:
        """Drop every item on the queue, so a worker waiting to put one goes on."""
        with contextlib.suppress(queue.Empty):
            while True:
                self._events.get_nowait()

    def _raise(self) -> None:
        """Raise what ended the run, once the worker is done; nothing for a normal end.

        Raises:
            Stopped: Ctrl-C ended the run (not `run.stop()`, which ends it normally).
            BaseException: the error that stopped the whole run, as it was raised.
        """
        if self._thread is not None:
            self._thread.join()
        error = self._error
        if error is None:
            return
        if isinstance(error, KeyboardInterrupt):
            if self._asked:
                return
            from flnd.errors import Stopped

            if isinstance(error, Stopped):
                raise error
            raise Stopped(STOPPED) from None
        raise error

    @contextlib.contextmanager
    def _interrupts(self) -> Iterator[None]:
        """While the reader waits on the main thread, Ctrl-C stops the run in two stages.

        On any other thread nothing is installed: Python runs signal handlers on the main
        thread alone, and there Ctrl-C is the caller's to handle.

        Yields:
            Nothing; the handler is in place for the block, and the old one comes back after.
        """
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        stop = self._stop

        def handle(_signum: int, _frame: Any) -> None:
            (stop.now if stop.soon.is_set() else stop.soon).set()

        previous = signal.signal(signal.SIGINT, handle)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous)
