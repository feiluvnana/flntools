"""`Run`: one download, follow or sync, started lazily and read as a stream of events.

`Flnd.download`, `follow` and `sync` return a `Run`. Nothing happens until someone reads it:

1. **Start.** The first `for event in run`, `run.wait()`, `async for` or `await run` starts a
   worker thread. The thread runs the engine command the `Run` was made with, in a copy of the
   caller's context (so a test's fake clock reaches it), with `_emit` as the engine's event sink
   and the run's own stop state.
2. **Events.** The engine (its listing thread, and the download tasks on the run's loop) calls
   `_emit` for each event. `_emit` counts the event into the report, then puts it on a bounded
   queue. When the queue is full, `_emit` waits: the listing waits while the script handles an
   event, so events never pile up in memory.
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
  first Ctrl-C sets `soon` (stop listing, finish what is in flight), the second `now` (cancel
  what is in flight, keeping the parts) and says a third quits, and the third exits at once
  (`quit_now`): it runs the terminal's undo steps (`restoring`), then `os._exit(130)`, which
  runs no `finally` and joins no thread. Whatever changes the terminal (the full screen, the
  key reader) registers its undo with `restoring`, so the shell gets its terminal back.
- A `Run` nobody starts warns with a `ResourceWarning` when it is dropped, the way a coroutine
  nobody awaited does: it is almost always a missing `.wait()`.
- `suspend()` holds new requests and files (`Stop.suspended`, which each source's limits
  watch); a stop always wins over it, so a suspended run still stops.
- The module imports only the stdlib, `flnd._events` and `flnd.ui.log` on a second Ctrl-C, never
  the engine, so `flnd.Run` is cheap to reach.
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import os
import queue
import signal
import sys
import threading
import warnings
from collections.abc import AsyncIterator, Callable, Generator, Iterator
from typing import TYPE_CHECKING, Any

from flnd._events import NOTHING_NEW, RESUMING, Failed, Planned, Progress, Report, Saved, Skipped

if TYPE_CHECKING:
    from flnd._events import Event

__all__ = ["Run", "Stop", "quit_now", "restoring", "stopping"]

# How many events wait for the reader before the run waits for it. Large enough that a reader
# printing a line per event never slows a download down; small enough to bound memory.
QUEUE = 256
# How often (in seconds) a waiting thread looks again at whether it should give up waiting.
WAKE = 0.1
# What the worker puts on the queue last, so the reader knows the run is over.
_END = object()
# What `_take` gives when nothing came within `WAKE`, so the reader can sample meanwhile.
_IDLE = object()
# The message of the `Stopped` a reader gets when Ctrl-C ended the run.
STOPPED = "stopped by Ctrl-C"
# What the second Ctrl-C says, so a person knows a third one quits without waiting.
AGAIN = "stopping now: the files in flight are cancelled; Ctrl-C again to quit now"
# The exit status of a third Ctrl-C: 128 + SIGINT (2), what a shell reports for a Ctrl-C.
QUIT_STATUS = 130
# The undo steps a hard exit runs, in the order they were registered (`restoring`): each puts
# back something on the terminal. A list, not a lock-guarded structure: it is changed and read
# on the main thread only, where the signal handler runs too.
_RESTORERS: list[Callable[[], None]] = []


class Stop:
    """A run's two-stage stop state: `soon` stops listing, `now` cancels what is in flight;
    and `suspended`, which holds new requests until it is cleared.

    The engine, the job and the sites all watch the same `Stop`: a listing checks `soon`
    between posts, and a transfer checks `now` between chunks; each source's limits wait while
    `suspended` is set. Who sets it: `stopping`'s Ctrl-C handler, `Run.stop()`,
    `Run.suspend()`, or a test.

    Example:
        >>> stop = Stop()
        >>> stop.soon.set()
        >>> stop.soon.is_set(), stop.now.is_set()
        (True, False)
    """

    def __init__(self) -> None:
        self.soon = threading.Event()
        self.now = threading.Event()
        self.suspended = threading.Event()

    @property
    def stage(self) -> str:
        """"" while nothing asked to stop, "soon" after a first stop, "now" after a second."""
        if self.now.is_set():
            return "now"
        return "soon" if self.soon.is_set() else ""

    def __repr__(self) -> str:
        return f"Stop(stage={self.stage!r})"


class Run:
    """One download, follow or sync: iterate it for its events, or `wait()` for its `Report`.

    A run starts when it is first iterated or waited on. Iterating yields `Saved`, `Skipped`,
    `Failed`, `Progress` and (for `dry=True`) `Planned` events as they happen. Breaking out of the
    loop holds the run back: iterating again goes on where it was, `wait()` finishes it, and
    leaving the `Flnd`'s `with` stops it.

    Make one with `Flnd.download`, `follow` or `sync`, not directly.

    Args:
        work: what the worker thread runs: called with the event sink and the stop state.
        name: what the run does, for its thread's name and its warning
            (`"download ins:example_user"`).
        closed: set when the run's `Flnd` is closed; a run may not start after that.

    Example:
        >>> run = Run(lambda emit, stop: None, name="nothing")
        >>> report = run.wait()
        >>> report.saved, report.failed, report.stopped
        (0, (), False)
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
        # Events from the loop when the queue is full; the listing thread may still wait.
        self._overflow: collections.deque[Any] = collections.deque()
        self._overflow_lock = threading.Lock()
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
        self._retried = self._resumed = self._unchanged = 0
        self._failed: list[Failed] = []
        # The live stats: what the run's parts track themselves in (through the context, as
        # the clock reaches them), and the latest sample.

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
                if item is not _IDLE:
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
                if item is not _IDLE:
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
                          stopped=self._asked, retried=self._retried, resumed=self._resumed,
                          unchanged=self._unchanged)

    def suspend(self) -> None:
        """Hold the run: no new request or file starts until `resume()`.

        The files already downloading finish. A stop still works while suspended, and ends
        the suspension. Suspending twice is the same as once.

        Example:
            >>> run = Run(lambda emit, stop: None, name="nothing")
            >>> run.suspend()
            >>> run.resume()
        """
        self._stop.suspended.set()

    def resume(self) -> None:
        """Let a suspended run go on where it was; nothing for a run that isn't suspended.

        Example:
            >>> run = Run(lambda emit, stop: None, name="nothing")
            >>> run.resume()
            >>> run.wait().ok
            True
        """
        self._stop.suspended.clear()

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
        # A suspended run would never get to its stop.
        self._stop.suspended.clear()

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

        Called by the engine on its listing thread and from download tasks on the run's loop.
        It returns at once when nobody will read the event (`wait()` is draining the run).

        Args:
            event: a `Saved`, `Skipped`, `Failed`, `Progress` or `Planned` event.
        """
        with self._lock:
            if isinstance(event, Saved):
                self._saved += 1
                self._bytes += event.size
                self._retried += event.retried
            elif isinstance(event, Skipped):
                # A listing with nothing new is no file skipped.
                if event.reason == NOTHING_NEW:
                    self._unchanged += 1
                else:
                    self._skipped += 1
            elif isinstance(event, Progress):
                self._resumed += event.note == RESUMING
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
        worker = self._thread
        while self._listening:
            try:
                self._events.put_nowait(item)
                self._drain_overflow()
                return
            except queue.Full:
                if worker is not None and threading.current_thread() is worker:
                    try:
                        self._events.put(item, timeout=WAKE)
                        self._drain_overflow()
                        return
                    except queue.Full:
                        continue
                with self._overflow_lock:
                    self._overflow.append(item)
                return

    def _drain_overflow(self) -> None:
        """Move buffered events onto the queue while there is room."""
        while self._listening:
            with self._overflow_lock:
                if not self._overflow:
                    return
                item = self._overflow[0]
            try:
                self._events.put_nowait(item)
            except queue.Full:
                return
            with self._overflow_lock:
                if self._overflow and self._overflow[0] is item:
                    self._overflow.popleft()

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
        """The next item of the queue, waiting at most `WAKE`, so Ctrl-C's handler can run and
        the reader can sample in between.

        Returns:
            An event; the end marker (the one the worker put, or one made here when the worker
            is gone and the queue is empty); or `_IDLE` when nothing came in time.
        """
        try:
            item = self._events.get(timeout=WAKE)
            self._drain_overflow()
            return item
        except queue.Empty:
            thread = self._thread
            if thread is not None and not thread.is_alive() and self._events.empty():
                return _END
            return _IDLE

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

    def _interrupts(self) -> contextlib.AbstractContextManager[Stop]:
        """While the reader waits on the main thread, Ctrl-C stops the run in two stages."""
        return stopping(self._stop)


@contextlib.contextmanager
def stopping(state: Stop | None = None) -> Iterator[Stop]:
    """Make Ctrl-C stop in three stages for the block: `soon`, then `now`, then exit at once.

    1. The first Ctrl-C sets `soon`: listing stops, the files in flight finish.
    2. The second sets `now`: the files in flight are cancelled, their parts kept, and a line
       says a third Ctrl-C quits now (`AGAIN`).
    3. The third quits at once (`quit_now`), with the terminal put back.

    Only the main thread can install a signal handler: Python runs handlers there alone. On
    any other thread (a run's worker) the state is yielded as it is, and whoever owns it sets
    it: the reader of the run, which installs the handler on its own thread, or `Run.stop()`.

    Args:
        state: the stop state to set; None makes a new one.

    Yields:
        The state. The old handler comes back when the block ends.

    Example:
        >>> with stopping() as stop:
        ...     stop.soon.is_set()
        False
    """
    state = state or Stop()
    if threading.current_thread() is not threading.main_thread():
        yield state
        return

    def handle(_signum: int, _frame: Any) -> None:
        # A suspended run would never get to its stop: Ctrl-C ends the suspension too.
        state.suspended.clear()
        if state.now.is_set():
            quit_now()
        elif state.soon.is_set():
            state.now.set()
            # `ui`'s lock is reentrant, so this line can't deadlock on code this handler
            # interrupted while it was logging.
            from flnd.ui.log import ui

            ui.warn(AGAIN)
        else:
            state.soon.set()

    previous = signal.signal(signal.SIGINT, handle)
    try:
        yield state
    finally:
        signal.signal(signal.SIGINT, previous)


@contextlib.contextmanager
def restoring(undo: Callable[[], None]) -> Iterator[None]:
    """Register `undo` for the block: what a third Ctrl-C runs before it exits.

    `undo` must be quick and take no lock another thread may hold, since it runs inside the
    signal handler: put a terminal mode back, write an escape sequence. Undo steps run newest
    first, the way `with` blocks unwind.

    Args:
        undo: what puts the terminal back.

    Yields:
        Nothing; leaving the block forgets `undo`.

    Example:
        >>> with restoring(lambda: None):
        ...     pass
    """
    _RESTORERS.append(undo)
    try:
        yield
    finally:
        _RESTORERS.remove(undo)


def quit_now() -> None:
    """Exit the process at once, as a third Ctrl-C does, with the terminal put back.

    1. Run each registered undo step (`restoring`), newest first; one that fails is skipped,
       so the rest still run.
    2. Show the cursor again on a terminal, which an inline live view may have hidden.
    3. Flush standard output and error, then `os._exit(QUIT_STATUS)`. No `finally`, `atexit`
       or thread join runs, so nothing can hold the exit up; parts already on disk stay, and
       the next run resumes them, as after a second Ctrl-C.
    """
    for undo in reversed(list(_RESTORERS)):
        try:
            undo()
        except BaseException:  # noqa: BLE001 - one failed undo must not keep the others back
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.isatty():
                stream.write("\x1b[?25h")
            if stream is not None:
                stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(QUIT_STATUS)
