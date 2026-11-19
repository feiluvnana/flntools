"""`Run`, one command on the run's loop read as a stream of events; Ctrl-C's stages; `Tally`;
and `Engine`, every command as a method. src/docs/architecture explains it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import os
import re
import signal
import threading
import warnings
from collections import ChainMap, deque
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Generator,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

import msgspec

import flnd.engine.resolve
from flnd.engine import auth, config, jobs, plugins, save, store
from flnd.engine.events import (
    NOTHING_NEW,
    RESUMING,
    Failed,
    FileState,
    HoldState,
    Phase,
    Planned,
    Report,
    Saved,
    Skipped,
    State,
    TargetState,
    sink,
)
from flnd.errors import DiskError, Error, LoginError, NetworkError, PluginError, Stopped, UsageError
from flnd.plugin import Account, Browser, Plugin, Range, Target
from flnd.ui.log import ui
from flnkit import clock, net, text

if TYPE_CHECKING:
    from flnd.engine import crawl
    from flnd.engine.crawl import Options, RunShared, TargetReport
    from flnd.engine.ctx import Ctx, Listing, PluginRun
    from flnd.engine.events import Event

T = TypeVar("T")

__all__ = ["Chosen", "Counts", "Engine", "Flight", "Results", "Run", "Stop", "Tally",
           "TargetInfo", "quit_now", "restoring", "stopping"]

# The events the channel holds before a producer on the loop waits for the reader.
QUEUE = 256
# How often (in seconds) a waiting reader looks again at whether it should give up waiting.
WAKE = 0.1
# What the loop's thread puts on the channel last, so the reader knows the run is over.
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
    and `suspended`, which holds new jobs until it is cleared.

    The engine, the crawl and the scheduler all watch the same `Stop`. Who sets it:
    `stopping`'s Ctrl-C handler, `Run.stop()`, `Run.suspend()`, or a test.

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


class _Done:
    """An awaitable that is done at once: what `Run._emit` gives for what it doesn't queue."""

    def __await__(self) -> Generator[Any, None, None]:
        return iter(())


_DONE = _Done()


class _Channel:
    """The run's events between the loop and the reader: at most `size` at once.

    Producers are on the loop only, and `put` waits while the channel is full. The reader takes
    events with a blocking `take`. `close` drops what is queued and lets every producer go on,
    for a reader that stopped listening (`Run.wait`).

    Args:
        size: the most events queued.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self._items: deque[Any] = deque()
        self._ready = threading.Condition()
        # The producers waiting for room, each a future of the loop that `take` resolves.
        self._waiting: list[asyncio.Future[None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._open = True

    def __len__(self) -> int:
        return len(self._items)

    async def put(self, item: Any) -> None:
        """Queue `item`, waiting on the loop while the channel is full; nothing once closed."""
        while True:
            with self._ready:
                if not self._open:
                    return
                if len(self._items) < self.size:
                    self._items.append(item)
                    self._ready.notify()
                    return
                self._loop = asyncio.get_running_loop()
                room = self._loop.create_future()
                self._waiting.append(room)
            await room

    def end(self) -> None:
        """Put the end marker after every event, room or not."""
        with self._ready:
            self._items.append(_END)
            self._ready.notify_all()

    def take(self, timeout: float) -> Any:
        """The next item, waiting at most `timeout`; `_IDLE` when none came."""
        with self._ready:
            if not self._items:
                self._ready.wait(timeout)
            if not self._items:
                return _IDLE
            item = self._items.popleft()
            self._wake()
            return item

    def close(self) -> None:
        """Stop listening: drop what is queued (the end marker kept) and free every producer."""
        with self._ready:
            self._open = False
            ended = any(item is _END for item in self._items)
            self._items.clear()
            if ended:
                self._items.append(_END)
            self._wake()

    def _wake(self) -> None:
        # Holding `_ready`: the producers look for room again on the loop.
        waiting, self._waiting = self._waiting, []
        if waiting and self._loop is not None:
            with contextlib.suppress(RuntimeError):  # the loop is closed: nobody waits
                self._loop.call_soon_threadsafe(_release, waiting)


def _release(waiting: Sequence[asyncio.Future[None]]) -> None:
    for room in waiting:
        if not room.done():
            room.set_result(None)


class Run:
    """One download, follow or sync: iterate it for its events, or `wait()` for its `Report`.

    A run starts when it is first iterated or waited on: one thread, `flnd-loop`, runs one
    asyncio loop for the whole command, and the caller's thread reads. Iterating yields
    `Saved`, `Skipped`, `Failed` and (for `dry=True`) `Planned` events as they happen; `state`
    says what it is doing now. Breaking out of the loop holds the run back: iterating again goes
    on where it was, `wait()` finishes it, and leaving the `Flnd`'s `with` stops it.

    Make one with `Flnd.download`, `follow` or `sync`, not directly.

    The sink must be awaited: at most `QUEUE` events wait for the reader, and past that the
    producer waits for room, so a slow reader paces the run. A producer never awaits the sink
    while it holds a place another file needs, so the transfers already started go on.

    Args:
        work: what the loop runs: called with the event sink and the stop state; what it
            returns is awaited when it is awaitable.
        name: what the run does, for its warning (`"download ins:example_user"`).
        closed: set when the run's `Flnd` is closed; a run may not start after that.

    Example:
        >>> run = Run(lambda emit, stop: None, name="nothing")
        >>> report = run.wait()
        >>> report.saved, report.failed, report.stopped
        (0, (), False)
    """

    def __init__(
        self, work: Callable[[Callable[[Any], Awaitable[None]], Stop], Any], *, name: str,
        closed: threading.Event | None = None,
    ) -> None:
        self.name = name
        self._work = work
        self._closed = closed or threading.Event()
        # The caller's context, so the loop sees what the caller set (a fake clock, a scope).
        self._context = contextvars.copy_context()
        self._stop = Stop()
        self._channel = _Channel(QUEUE)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Whether the end marker was read: iterating again yields nothing.
        self._over = False
        # What ended the command, if anything did.
        self._error: BaseException | None = None
        # Whether `stop()` asked the run to stop, which makes its end not an error.
        self._asked = False
        # The report's counts, kept under `_lock`.
        self._saved = self._skipped = self._bytes = self._planned = 0
        self._retried = self._resumed = self._unchanged = 0
        self._failed: list[Failed] = []
        # The newest snapshot the engine published: swapped whole, never changed, so any
        # thread reads it without a lock.
        self._state = State()

    # -----------------------------------------------------------------------------------------
    # Reading the run
    # -----------------------------------------------------------------------------------------

    def __iter__(self) -> Iterator[Event]:
        """Start the run if needed, and yield its events as they happen."""
        return self._events_now()

    def _events_now(self) -> Generator[Event, None, None]:
        """The generator behind `__iter__`.

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
        """The async generator behind `__aiter__`: each event is taken on a helper thread."""
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
        """Wait on a helper thread; a cancelled wait stops the run."""
        try:
            return await asyncio.to_thread(self.wait)
        except asyncio.CancelledError:
            self.stop()
            raise

    def wait(self) -> Report:
        """Run to the end, dropping the events, and return the report.

        Raises:
            Error: what stopped the whole run (a usage mistake, a full disk).
            Stopped: Ctrl-C ended it.

        Example:
            >>> Run(lambda emit, stop: None, name="nothing").wait().ok
            True
        """
        self._start()
        with self._interrupts():
            self._channel.close()
            thread = self._thread
            # A join with a timeout, in a loop, so Ctrl-C's handler runs while it waits.
            while thread is not None and thread.is_alive():
                thread.join(WAKE)
            self._over = True
        self._raise()
        return self.report

    @property
    def state(self) -> State:
        """What the run is doing now: its targets, the files in flight, the waits.

        A new `State` is swapped in every 100 ms while something changed (its `version` goes
        up); before the run starts it is an empty one, at version 0. Any thread may read it.
        """
        return self._state

    @property
    def report(self) -> Report:
        """The counts so far: at the end, the whole run's; also after a loop broke early."""
        with self._lock:
            return Report(saved=self._saved, skipped=self._skipped, bytes=self._bytes,
                          planned=self._planned, failed=tuple(self._failed),
                          stopped=self._asked, retried=self._retried, resumed=self._resumed,
                          unchanged=self._unchanged)

    def suspend(self) -> None:
        """Hold the run: no new job starts until `resume()`.

        The jobs already running go on. A stop still works while suspended, and ends the
        suspension. Suspending twice is the same as once.

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
    # The loop's side
    # -----------------------------------------------------------------------------------------

    def _emit(self, event: Any) -> Awaitable[None]:
        """Count an event, then hand it to the reader: await what it gives, on the loop.

        Two things that aren't events come here too, and are never queued: a `State`, which
        replaces `run.state` at once, and `RESUMING`, a listing that went on from an earlier
        run's cursor, which `report.resumed` counts. For those it gives an awaitable that is
        done at once, so a plain function may hand them over without awaiting.

        Args:
            event: a `Saved`, `Skipped`, `Failed` or `Planned` event, a `State`, or `RESUMING`.

        Returns:
            What puts the event on the channel, waiting while the channel is full.
        """
        if isinstance(event, State):
            self._state = event
            return _DONE
        with self._lock:
            if event is RESUMING:
                self._resumed += 1
                return _DONE
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
            elif isinstance(event, Failed):
                self._failed.append(event)
            elif isinstance(event, Planned):
                self._planned += 1
        return self._channel.put(event)

    def _start(self) -> None:
        """Start the loop's thread, once.

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
                                            name="flnd-loop", daemon=True)
        self._thread.start()

    def _main(self) -> None:
        """The loop's thread: run the command, end what it left, then mark the end."""
        loop = asyncio.new_event_loop()
        try:
            try:
                loop.run_until_complete(self._serve())
            except BaseException as error:  # a task's own Ctrl-C, which asyncio lets out
                self._error = self._error or error
            loop.run_until_complete(_shut_down())
        finally:
            loop.close()
            # Every producer is on the loop, which is closed: nothing can come after this.
            self._channel.end()

    async def _serve(self) -> None:
        """Run the command on the loop, keeping what ended it for the reader (`_raise`)."""
        try:
            done = self._work(self._emit, self._stop)
            if inspect.isawaitable(done):
                await done
        except BaseException as error:  # handed to the reader, who raises it
            self._error = error

    def _take(self) -> Any:
        """The next item of the channel, waiting at most `WAKE`, so Ctrl-C's handler can run
        and the reader can sample in between.

        Returns:
            An event; the end marker (the one the loop put, or one made here when the thread
            is gone and the channel is empty); or `_IDLE` when nothing came in time.
        """
        item = self._channel.take(WAKE)
        if item is _IDLE:
            thread = self._thread
            if thread is not None and not thread.is_alive() and not len(self._channel):
                return _END
        return item

    def _raise(self) -> None:
        """Raise what ended the run, once the loop's thread is done; nothing for a normal end.

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
        """While the reader waits on the main thread, Ctrl-C stops the run in stages."""
        return stopping(self._stop)


async def _shut_down() -> None:
    """Cancel every other task on the running loop and wait for them, then shut it down.

    Each task's error or cancel is dropped (`return_exceptions`), since nobody is left to read
    it. Then the loop's async generators are closed and its default executor's threads joined,
    so nothing outlives the run.
    """
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    loop = asyncio.get_running_loop()
    await loop.shutdown_asyncgens()
    await loop.shutdown_default_executor()


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
            # `ui`'s lock is reentrant, and its fold never waits on the thread holding it, so
            # this line can't deadlock on code this handler interrupted while it was logging.
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
    2. Show the cursor again on a terminal, which an inline live view may have hidden, and
       flush standard output and error (`out.close`); folded warnings are dropped, as draining
       them would take a lock another thread may hold.
    3. `os._exit(QUIT_STATUS)`. No `finally`, `atexit` or thread join runs, so nothing can
       hold the exit up; parts already on disk stay, and the next run resumes them, as after a
       second Ctrl-C.
    """
    from flnd.ui import out

    for undo in reversed(list(_RESTORERS)):
        try:
            undo()
        except BaseException:  # noqa: BLE001 - one failed undo must not keep the others back
            pass
    out.close(cursor=True, drain=False)
    os._exit(QUIT_STATUS)


# ---------------------------------------------------------------------------------------------
@dataclass
class Counts:
    """One category's counts of files, as the run's tally keeps them.

    A file moves from `queued` to `downloading`, then to `saved`, `skipped` or `failed`.
    `linked` counts files linked from another category's folder, `later` the links held back
    until the file they point to lands, and `bytes` the bytes saved.
    """

    found: int = 0
    queued: int = 0
    downloading: int = 0
    saved: int = 0
    linked: int = 0
    later: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0

    @property
    def pending(self) -> int:
        """Return files and links not yet finished."""
        return self.queued + self.downloading + self.later


def _ignore(event: Any) -> None:
    """The sink of a run nobody listens to: the event is dropped."""


# ---------------------------------------------------------------------------------------------
# The tally: the run's live counts, and the `State` snapshots made from them
# ---------------------------------------------------------------------------------------------

# Seconds between two snapshots, and the window a speed is measured over.
TICK = 0.1
SPEED_WINDOW = 3.0
# The files in flight a snapshot names; `State.more` counts the rest.
FILES_SHOWN = 6


class _Rate:
    """Bytes a second over the last `SPEED_WINDOW` seconds, from samples taken at each tick."""

    def __init__(self) -> None:
        self._samples: deque[tuple[float, float]] = deque()

    def at(self, now: float, value: float) -> float:
        samples = self._samples
        samples.append((now, value))
        while len(samples) > 1 and now - samples[0][0] > SPEED_WINDOW:
            samples.popleft()
        then, before = samples[0]
        return (value - before) / (now - then) if now > then else 0.0


@dataclass
class _Row:
    """One target's row of the snapshot; changed under the tally's lock."""

    text: str
    planned: tuple[str, ...]
    phase: Phase = Phase.WAITING
    categories: list[str] = field(default_factory=list)
    counts: Counts = field(default_factory=Counts)
    received: int = 0
    rate: _Rate = field(default_factory=_Rate)


class Flight:
    """One file in flight: the bytes it received so far, and its size.

    Made by `Tally.fly`; the transfer counts on it from the loop, the snapshot reads it under
    the tally's lock.
    """

    def __init__(self, tally: Tally, key: str, row: _Row, path: str) -> None:
        self._tally, self.key, self._row, self.path = tally, key, row, path
        self.done = 0
        self.total: int | None = None
        self.rate = _Rate()

    def size(self, total: float | None) -> None:
        """Set the file's size; None when it isn't known."""
        self.total = int(total) if total is not None else None

    def advance(self, n: float) -> None:
        """Count `n` more bytes received."""
        # No lock: only the file's transfer writes it, and a snapshot reading it a chunk late
        # is still right at the next tick.
        self.done += int(n)

    def land(self) -> None:
        """Take the file off the snapshot: its transfer ended, however it ended."""
        tally = self._tally
        with tally._lock:
            if tally._flights.pop(self.key, None) is not None:
                self._row.received += self.done
                tally._received += self.done


@dataclass
class _Hold:
    """A wait going on, as the snapshot's `HoldState` shows it."""

    plugin: str
    account: str | None
    bucket: str
    until: float
    reason: str
    count: int


class Tally:
    """The run's live counts, and the `State` snapshots made from them (`run.state`).

    Every part of a run counts here: each target's listings (`start`, `downloading`, `end`),
    each file's steps (`count`), the files in flight (`fly`) and the waits (`hold`). Every
    `TICK` seconds, while something changed, `publish` makes a new `State` and sends it to the
    run's sink, which swaps it in as `run.state`; the screen reads only that.

    A target a search found counts on the search's row (`adopt`). Everything changes under one
    lock, which is never held while calling out.

    Args:
        emit: the run's event sink; None drops the snapshots.
    """

    def __init__(self, emit: Callable[[Any], None] | None = None) -> None:
        self.emit = emit or _ignore
        self._lock = threading.Lock()
        self._rows: dict[Target, _Row] = {}
        self._owners: dict[Target, Target] = {}
        # Counts by the target and category they belong to, a found target's on its own.
        self._lines: dict[tuple[Target, str], Counts] = {}
        self._flights: dict[str, Flight] = {}
        self._holds: list[_Hold] = []
        # The newest listing step: its row, its category, and its `Listing` (pages read).
        self._step: tuple[_Row, str, Listing | None] | None = None
        # The newest account switch since that step, as the `now` line shows it.
        self._switch = ""
        self._received = 0
        self._rate = _Rate()
        self._started = clock.now()
        self._version = 0
        self._last: State | None = None

    # -- what the run counts ------------------------------------------------------------------

    def add(self, target: Target, categories: Sequence[str] = ()) -> None:
        """Give `target` a row, waiting, unless it has one.

        Args:
            target: the target.
            categories: the categories it will take, shown until its listings start.
        """
        with self._lock:
            if target not in self._rows and target not in self._owners:
                self._rows[target] = _Row(str(target), tuple(categories))

    def adopt(self, child: Target, parent: Target) -> None:
        """Count a target a search found on the search's row."""
        with self._lock:
            self._owners[child] = parent

    def start(self, target: Target, category: str, listing: Listing | None = None) -> None:
        """A listing of `target` starts: its row is listing, and it is the newest step.

        Args:
            target: the target listed.
            category: its category; "" for a target without categories.
            listing: the category's `Listing`, whose pages read show on the `now` line.
        """
        with self._lock:
            row = self._row(target)
            row.phase = Phase.LISTING
            name = category or target.kind
            if name not in row.categories:
                row.categories.append(name)
            self._step = (row, name, listing)
            self._switch = ""

    def switched(self, line: str) -> None:
        """A job moved to another account (`flnd.engine.auth.Switcher`): the `now` line
        shows `line` until the next listing step."""
        with self._lock:
            self._switch = line

    def downloading(self, target: Target) -> None:
        """`target`'s listings ended; what is left is its files in flight."""
        with self._lock:
            row = self._row(target)
            if row.phase is Phase.LISTING:
                row.phase = Phase.DOWNLOADING

    def end(self, target: Target, *, failed: bool) -> None:
        """`target` is over: done, or failed (a stop included). A found target ends nothing."""
        with self._lock:
            if target in self._owners:
                return
            self._row(target).phase = Phase.FAILED if failed else Phase.DONE

    def count(self, target: Target, category: str, **steps: int) -> None:
        """Move the counts of `Counts` for one file or post of `target`'s category.

        Args:
            target: the target the files belong to.
            category: their category; "" for a target without categories.
            steps: how much each count moves, such as `saved=1, downloading=-1`.
        """
        with self._lock:
            line = self._lines.setdefault((target, category), Counts())
            row = self._row(target)
            for name, step in steps.items():
                setattr(line, name, getattr(line, name) + step)
                setattr(row.counts, name, getattr(row.counts, name) + step)

    @property
    def saved(self) -> int:
        """The files every target has saved so far."""
        with self._lock:
            return sum(line.saved for line in self._lines.values())

    def totals(self, target: Target) -> Counts:
        """The counts of every category of `target` itself, summed, as a new `Counts`."""
        total = Counts()
        with self._lock:
            for (owner, _), line in self._lines.items():
                if owner == target:
                    for name in vars(total):
                        setattr(total, name, getattr(total, name) + getattr(line, name))
        return total

    def fly(self, key: str, target: Target, path: str) -> Flight:
        """A file of `target` starts its transfer: it shows until it `land`s.

        Args:
            key: the file's key in the run.
            target: the target it belongs to.
            path: where it goes, under the target's folder.

        Returns:
            The file's `Flight`, which the transfer counts its bytes on.
        """
        with self._lock:
            flight = self._flights.get(key)
            if flight is None:
                flight = self._flights[key] = Flight(self, key, self._row(target), path)
            return flight

    @contextlib.contextmanager
    def hold(self, plugin: str, account: str | None, bucket: str, seconds: float, reason: str,
             *, count: int = 1) -> Iterator[None]:
        """Show a wait for the block: a site asked the run to come back later, or a source waits.

        Args:
            plugin: the source's code.
            account: its account's name; None when it has none.
            bucket: what waits: "pages" for a listing.
            seconds: how long the wait is.
            reason: why, in the site's or the source's words.
            count: the round of waits in a row this one is, from 1.
        """
        with self._lock:
            hold = _Hold(plugin, account, bucket, clock.now() - self._started + seconds, reason,
                         count)
            self._holds.append(hold)
        try:
            yield
        finally:
            with self._lock:
                self._holds.remove(hold)

    def _row(self, target: Target) -> _Row:
        """The row `target` counts on, made when it has none; the caller holds the lock."""
        owner = self._owners.get(target, target)
        row = self._rows.get(owner)
        if row is None:
            row = self._rows[owner] = _Row(str(owner), ())
        return row

    # -- the snapshots ------------------------------------------------------------------------

    def publish(self, *, force: bool = False) -> State | None:
        """Make a new `State` and send it to the sink, when it differs from the last one.

        The speeds move with the clock alone, so they are sampled at each call even when no
        count changed; the time the run has taken never makes a snapshot new by itself.

        Args:
            force: send one even when nothing changed (the run's last).

        Returns:
            The new snapshot; None when nothing changed.
        """
        with self._lock:
            state = self._state(clock.now())
            last = self._last
            same = last is not None and msgspec.structs.replace(
                last, version=state.version, elapsed=state.elapsed) == state
            if same and not force:
                return None
            self._version += 1
            state = msgspec.structs.replace(state, version=self._version)
            self._last = state
        self.emit(state)
        return state

    @contextlib.asynccontextmanager
    async def publishing(self) -> AsyncIterator[Tally]:
        """Publish a snapshot every `TICK` seconds from the loop, then a last one.

        Yields:
            This tally.
        """

        async def tick() -> None:
            while True:
                # Real seconds, as a screen redraws, even under a test's fake clock.
                await asyncio.sleep(TICK)
                self.publish()

        ticking = asyncio.get_running_loop().create_task(tick())
        try:
            yield self
        finally:
            ticking.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticking
            self.publish(force=True)

    def _state(self, now: float) -> State:
        """The snapshot of the counts as they are; the caller holds the lock."""
        elapsed = now - self._started
        flights = list(self._flights.values())
        flying: dict[int, int] = {}
        for flight in flights:
            flying[id(flight._row)] = flying.get(id(flight._row), 0) + flight.done
        targets = tuple(
            self._target(row, now, row.received + flying.get(id(row), 0))
            for row in self._rows.values()
        )
        files = tuple(
            FileState(f._row.text, f.path, f.done, f.total, f.rate.at(now, f.done))
            for f in flights[:FILES_SHOWN]
        )
        holds = tuple(HoldState(h.plugin, h.account, h.bucket, h.until, h.reason, h.count)
                      for h in self._holds)
        return State(
            version=self._version, elapsed=elapsed, targets=targets, files=files,
            more=max(0, len(flights) - FILES_SHOWN), holds=holds, now=self._now(elapsed),
            saved=sum(t.saved for t in targets), skipped=sum(t.skipped for t in targets),
            failed=sum(t.failed for t in targets), bytes=sum(t.bytes for t in targets),
            speed=self._rate.at(now, self._received + sum(flying.values())),
        )

    @staticmethod
    def _target(row: _Row, now: float, received: int) -> TargetState:
        c = row.counts
        return TargetState(
            row.text, tuple(row.categories) or row.planned, row.phase, found=c.found,
            saved=c.saved + c.linked, skipped=c.skipped, failed=c.failed, bytes=c.bytes,
            speed=row.rate.at(now, received),
        )

    def _now(self, elapsed: float) -> str:
        """The `now` line: the newest listing step, then the newest wait going on."""
        step = ""
        if self._step is not None:
            row, name, listing = self._step
            pages = listing.read if listing is not None else 0
            step = f"{row.text} · {name}" + (f", page {pages}" if pages else "")
        if self._switch:
            step = f"{step} · {self._switch}" if step else self._switch
        if not self._holds:
            return step
        hold = self._holds[-1]
        held = f"held {max(0, round(hold.until - elapsed))} s: {hold.reason}"
        return f"{step} · {held}" if step else held


# ---------------------------------------------------------------------------------------------
# The engine: every command as a method
# ---------------------------------------------------------------------------------------------

# How many links deep the targets that listings yield are followed (`Engine._found`).
FOLLOW_DEPTH = 2
# Targets a search's sync downloads at most per run, among those it hadn't seen: a search that
# suddenly finds hundreds (a popular tag) is taken a slice per sync, never all at once.
MAX_SYNC_NEW = 25
# Targets active at once in a run (listing, or with more files queued than their places), and
# targets listing at once per plugin and account: one for a plugin that reads in the browser,
# whose site sees one person, and `HTTP_AT_ONCE` for one that only sends requests.
ACTIVE = jobs.LISTINGS
HTTP_AT_ONCE = 4


class Chosen(NamedTuple):
    """A target as a run takes it: resolved, with the categories it runs."""

    target: Target
    # The categories named in its text (`/reels`), else the stored ones; None takes the
    # route's own.
    categories: tuple[str, ...] | None = None
    # The value as typed, with a link's `user:pw@`, which the target leaves out (`Options.link`);
    # "" when they are the same.
    link: str = ""


@dataclass(frozen=True)
class TargetInfo:
    """A known target's state and its saved files, as `Engine.target_ls` reads them."""

    target: Target
    sync: bool
    at: str
    files: int
    size: int
    seen: int
    categories: dict[str, str]
    query: bool


@dataclass(frozen=True)
class Results:
    """Summary of a `download` or `sync` run, one `TargetReport` per target."""

    targets: tuple[TargetReport, ...]

    @property
    def files(self) -> int:
        """Total files written across all targets."""
        return sum(len(target.written) for target in self.targets)

    @property
    def failed(self) -> tuple[Target, ...]:
        """Targets that failed with an error."""
        return tuple(target.target for target in self.targets if target.error is not None)

    @property
    def ok(self) -> bool:
        """Whether every target succeeded without error."""
        return not self.failed


class Engine:
    """flnd's commands as methods: `resolve`, `download`, `sync`, `retry`, and the reads."""

    def __init__(
        self,
        *,
        home: Path = Path(".flnd"),
        sources: Mapping[str, type[Plugin]] = plugins.PLUGINS,
        emit: Callable[[Any], Any] | None = None,
        stop: Stop | None = None,
    ) -> None:
        """Make the engine of one home.

        Args:
            home: the home folder.
            sources: the sources it knows, by code.
            emit: where its runs send their events (`flnd.engine.run.Run._emit`, whose answer
                is awaited; a plain function is called); None drops them.
            stop: the stop state its runs watch, when the caller owns it (a library run, whose
                `stop()` sets it); None makes one per command, set by Ctrl-C.
        """
        self.home, self.sources = home, sources
        self.emit = sink(emit)
        # Where the snapshots go (`run.state`), at once and never awaited.
        self._shown: Callable[[Any], Any] = emit or (lambda state: None)
        self.stop = stop
        # The `runs` row of the command going on, which its failures name; None between them.
        self.run: int | None = None
        # The live counts of the command going on, which `run.state` is made from.
        self.tally = Tally(self._shown)
        # What resolving reads of each plugin known so far, by code (`manifest`).
        self._entries: dict[str, Mapping[str, Any]] | None = None
        self._installed: dict[str, Mapping[str, Any]] = {}
        self._manifest: flnd.engine.resolve.Manifest | None = None

    def resolve(self, texts: Sequence[str]) -> list[Target]:
        """Resolve every target's text (`chosen`), leaving out the categories it names.

        Raises:
            UsageError: a text names no known plugin, or no route reads it.
            NetworkError: a short link couldn't be followed.
        """
        return [found.target for found in self.chosen(texts)]

    def chosen(self, targets: Sequence[Target | str | Chosen]) -> list[Chosen]:
        """Each target resolved, with its categories: those its text names (`/reels,posts`,
        `/all`), else the stored ones of a known target, else None for the route's own.

        A `Target` takes its stored categories, and a `Chosen` is taken as it is.

        Raises:
            UsageError: a text names no known plugin, no route reads it, or it names a
                category its route lacks.
            NetworkError: a short link couldn't be followed.
        """
        # Only the rows that keep categories: each imports its plugin, to write its key.
        rows = {item: row for item, row in self._rows().items() if row.categories}
        stored = {str(target): rows[store.key(target)].categories
                  for target in self._recorded(rows)}
        found: list[Chosen] = []
        for given in targets:
            if isinstance(given, Chosen):
                found.append(given)
            elif isinstance(given, Target):
                found.append(Chosen(given, stored.get(str(given))))
            else:
                chosen = self._read(given, stored)
                # The target alone: the text as typed may hold a link's `user:pw@`.
                ui.debug("resolved {target} ({kind}), categories {categories}",
                         target=chosen.target, kind=chosen.target.kind,
                         categories=chosen.categories)
                found.append(chosen)
        return found

    @property
    def manifest(self) -> flnd.engine.resolve.Manifest:
        """What resolving reads of the plugins known so far: each built-in's manifest entry,
        which imports none of them, then each other plugin's, from `plugins.json` or its class."""
        if self._entries is None:
            self._entries = {}
            maps = self.sources.maps if isinstance(self.sources, ChainMap) else [self.sources]
            for found in reversed(maps):
                if isinstance(found, plugins.Registry):
                    self._entries |= found.manifest()
                else:
                    self._entries |= {code: flnd.engine.resolve.entry(cls)
                                      for code, cls in found.items()}
        if self._manifest is None:
            installed = {"plugins": self._installed} if self._installed else None
            self._manifest = flnd.engine.resolve.Manifest(self._entries, installed)
        return self._manifest

    def _widen(self, code: str | None = None) -> bool:
        """Add one installed plugin, or every installed plugin, to `manifest`, without importing it.

        Args:
            code: the plugin's code; None for every installed plugin from `plugins.json`.
        """
        known = self.manifest.plugins
        if code is not None and code in known:
            return False
        maps = self.sources.maps if isinstance(self.sources, ChainMap) else [self.sources]
        added_entries: dict[str, Mapping[str, Any]] = {}
        added_installed: dict[str, Mapping[str, Any]] = {}
        for found in reversed(maps):
            if isinstance(found, plugins.Registry):
                for name, entry in found.installed(code).items():
                    if name not in known and name not in self._installed:
                        added_installed[name] = entry
            else:
                codes = [code] if code is not None else list(found)
                for name in codes:
                    if name not in known and name in found:
                        added_entries[name] = flnd.engine.resolve.entry(found[name])
        if added_entries or added_installed:
            self._entries = {**(self._entries or {}), **added_entries}
            self._installed.update(added_installed)
            self._manifest = None
        return bool(added_entries or added_installed)

    def _read(self, text: str, stored: Mapping[str, Sequence[str]]) -> Chosen:
        """One target's text, resolved (`flnd.engine.resolve`), with its categories.

        A code looks for its plugin alone; a link only the plugins not built in read looks
        for every plugin, as today.
        """
        code = re.match(r"\s*([A-Za-z][A-Za-z0-9]*)\s*[:?]", text)
        linked = text.strip().lower().startswith(("http://", "https://"))
        if code is not None and not linked:
            self._widen(code[1].lower())
        try:
            found = flnd.engine.resolve.resolve(text, manifest=self.manifest, known=stored,
                                                follow=_follow)
        except UsageError:
            if (code is not None and not linked) or not self._widen():
                raise
            found = flnd.engine.resolve.resolve(text, manifest=self.manifest, known=stored,
                                                follow=_follow)
        if found.code == flnd.engine.resolve.WEB and not text.strip().lower().startswith("web:"):
            if self._widen():
                found = flnd.engine.resolve.resolve(text, manifest=self.manifest, known=stored,
                                                    follow=_follow)

        kept = found.named or found.key in stored
        return Chosen(Target(found.key, kind=found.kind),
                      found.categories if kept else None, found.link)

    def download(self, targets: Sequence[Target | str], **options: Any) -> Results:
        """`run_download`, on a loop of its own on this thread, for a caller outside a run."""
        return _drive(self.run_download(targets, **options))

    def sync(self, targets: Sequence[Target | str] = (), *, time: Range | None = None) -> Results:
        """`run_sync`, on a loop of its own on this thread, for a caller outside a run."""
        return _drive(self.run_sync(targets, time=time))

    def retry(self) -> Results:
        """`run_retry`, on a loop of its own on this thread, for a caller outside a run."""
        return _drive(self.run_retry())

    async def run_download(
        self,
        targets: Sequence[Target | str | Chosen],
        *,
        time: Range | None = None,
        limit: int | None = None,
        dry: bool = False,
        sync: bool = False,
        again: bool = False,
        keep: bool = False,
    ) -> Results:
        """Download each target with its saved or given options and return a `Results`.

        Args:
            targets: the targets, as text (`ins:example_user/reels`) or resolved.
            time: `--when`, as a `Range`; None for no limit.
            limit: at most this many posts per target, or targets per search; None for all.
            dry: plan what would be saved (`Planned` events), and write nothing.
            sync: follow the targets, so `sync` takes them later.
            again: ignore saved cursors and the stop at saved posts.
            keep: whether to save each target's categories, for later syncs (`follow`).

        Returns:
            One `TargetReport` per target, in order.

        Raises:
            UsageError: a bad target, category or limit, before anything is fetched.
            DiskError: the home can't be written; the run stopped.
            KeyboardInterrupt: Ctrl-C (or the run's stop) ended it.
        """
        from flnd.engine.crawl import Options

        if limit is not None and limit < 1:
            raise UsageError("--limit must be at least 1")
        span = time or Range()
        # On a thread: a short link is followed with a request of its own.
        resolved = await asyncio.to_thread(self.chosen, targets)
        base = Options(
            since=span.since,
            until=span.until,
            limit=limit,
            again=again,
            stop_at_saved=sync and not again,
        )
        chosen = [(found.target, replace(base, categories=found.categories,
                                         link=found.link or None))
                  for found in resolved]
        with stopping(self.stop) as stop:
            async with self._live():
                if dry:
                    return self._finish_report(await self._dry_run(chosen, span, stop), stop)
                ui.info("Storage: {path}", path=await _off((self.home / store.MEDIA).resolve))
                async with self._recording("download", [t for t, _ in chosen]):
                    async with self._running(stop) as runner:
                        return await self._download(runner, chosen, span=span, sync=sync,
                                                    stop=stop, keep=keep)

    async def run_sync(self, targets: Sequence[Target | str] = (), *,
                       time: Range | None = None) -> Results:
        """Bring synced targets up to date: fetch new posts and sum up.

        Args:
            targets: only these targets; every synced target when empty.
            time: the days every target takes posts from (`[defaults] when` in config.toml`);
                None for every day.

        Returns:
            One `TargetReport` per target.
        """
        # On a thread: a short link is followed with a request of its own.
        resolved = await asyncio.to_thread(self.chosen, targets)
        span = time or Range()
        chosen = [(target, replace(options, since=span.since, until=span.until))
                  for target, options in await _off(self._plan, resolved)]
        if not chosen:
            ui.info("nothing to sync")
            return Results(())
        with stopping(self.stop) as stop:
            async with self._recording("sync", [t for t, _ in chosen]), \
                    self._live(), self._running(stop) as runner:
                results: dict[int, TargetReport] = {}
                await self._sync_normal(runner, chosen, results, stop)
                await self._sync_queries(runner, chosen, results, stop)
                report = Results(tuple(results[i] for i in range(len(chosen))))
            return self._finish_report(report, stop)

    async def run_retry(self) -> Results:
        """Retry every failure the home keeps, however many runs failed it (`flnd retry`).

        Each target with failed files lists each failed post again, from the cursor of the page
        it was on, and downloads the files that failed, from `(target, post, file)`: never a
        stored URL (src/docs/errors). A target whose listing failed whole is downloaded again, its
        failed files first. A gone file (`NotFound`) isn't retried; `--again` asks for those.

        Returns:
            One `TargetReport` per target retried; none when nothing failed.

        Raises:
            KeyboardInterrupt: Ctrl-C (or the run's stop) ended it.
        """
        from flnd.engine.crawl import Options, Retry

        failed, rows = await _off(self._failures)
        chosen: list[tuple[Target, Options]] = []
        for item, whole in failed.items():
            row = rows.get(item)
            # A target a search found has no row of its own: its key resolves it.
            recorded = self._recorded({item: row}) if row is not None else []
            try:
                target = recorded[0] if recorded else await asyncio.to_thread(self._one, item)
            except Error as error:
                ui.warn("{target}: can't retry: {error}", target=item, error=error)
                continue
            if target.plugin not in self.sources or self._is_query(target):
                continue
            # A failed file may be in any category, so a retry lists the ones it failed in; a
            # target that failed whole is downloaded as its last run would have been.
            options = (Options(categories=(row.categories or None) if row else None,
                               stop_at_saved=True)
                       if whole else Options(retry=Retry.ONLY))
            chosen.append((target, options))
        if not chosen:
            ui.info("nothing to retry")
            return Results(())
        with stopping(self.stop) as stop:
            async with self._recording("retry", [target for target, _ in chosen]), \
                    self._live(), self._running(stop) as runner:
                report = Results(tuple(await runner.run(chosen)))
            return self._finish_report(report, stop)

    def _failures(self) -> tuple[dict[str, bool], dict[str, store.TargetRow]]:
        """The targets with failures `flnd retry` takes, and every target's row."""
        with store.open(self.home) as db:
            return db.failed(), db.targets()

    def target_ls(self) -> list[TargetInfo]:
        """Every known target with its state and saved files, all read from `flnd.db`.

        Returns:
            The recorded targets, then the folders in `media/` no row names.
        """
        with store.open(self.home) as db:
            rows = db.targets()
            recorded = self._recorded(rows)
            all_targets = recorded + list(self._unrecorded(recorded, rows))
            return [self._target_info(target, rows, db) for target in all_targets]

    def target_sync(self, targets: Sequence[Target | str], *, enabled: bool) -> list[Target]:
        """Turn `sync` on or off for each known target (`fl.unfollow` turns it off).

        A known target is one with a row in `flnd.db`, or a folder under `media/` that no row
        names (a target downloaded before targets were recorded), which gets a row here. Every
        target is checked before any row changes, so one unknown target changes nothing.

        Args:
            targets: the targets, as text or resolved.
            enabled: True to sync them from now on, False to stop.

        Returns:
            The resolved targets, in order.

        Raises:
            UsageError: a target isn't known; download it first.
        """
        resolved = self.resolve(targets)
        rows = self._rows()
        recorded = self._recorded(rows)
        unrecorded = set(self._unrecorded(recorded, rows))
        for target in resolved:
            if store.key(target) not in rows and target not in unrecorded:
                raise UsageError(f"{target}: not a known target; download it first")
        with store.open(self.home) as db:
            for target in resolved:
                db.follow(target, enabled)
        return resolved

    async def _download(
        self,
        runner: _Runner,
        chosen: Sequence[tuple[Target, Options]],
        *,
        span: Range,
        sync: bool,
        stop: Stop,
        keep: bool = False,
    ) -> Results:
        """Download the targets, recording each in flnd.db first.

        Plain targets run at once; with a search among them, each runs in the order given,
        and a search downloads each target it finds (`_run_query`).

        Args:
            runner: the command's run.
            chosen: the targets, in order, with their options.
            span: `--when`, which a search filters what it finds by.
            sync: whether to follow the targets (a search is followed, not what it finds).
            stop: the run's stop state.
            keep: whether to save each target's categories for later syncs.

        Returns:
            One `TargetReport` per plain target, in order, and for a search one per target it
            found (and one for the search itself when it failed or was stopped).

        Raises:
            KeyboardInterrupt: the run was stopped.
        """
        rows = await _off(self._rows)
        kept = []
        for target, options in chosen:
            # A followed target stops at its saved posts, unless told to list them again.
            row = rows.get(store.key(target))
            followed = row is not None and row.follow and not options.again
            kept.append((target, replace(
                options, stop_at_saved=options.stop_at_saved or followed)))
        await _off(self._record, kept, follow=sync, keep=keep)
        if not any(self._is_query(target) for target, _ in kept):
            reports = await runner.run(kept)
        else:
            reports = []
            for target, options in kept:
                if self._is_query(target):
                    reports.extend(await self._run_query(runner, target, options, span=span,
                                                         stop=stop))
                else:
                    reports.extend(await runner.run([(target, options)]))
        return await self._complete_download([target for target, _ in kept], reports,
                                             sync=sync, stop=stop, rows=rows)

    async def _complete_download(
        self,
        resolved: Sequence[Target],
        reports: Sequence[TargetReport],
        *,
        sync: bool,
        stop: Stop,
        rows: Mapping[str, store.TargetRow] | None = None,
    ) -> Results:
        """End a download: raise for Ctrl-C, log the totals, and say when a target isn't synced.

        Args:
            resolved: the command's targets.
            reports: one report per target.
            sync: whether the command followed them, so no reminder is needed.
            stop: the run's stop state.
            rows: the target rows read before the run; None reads them again.

        Returns:
            The command's `Results`.

        Raises:
            KeyboardInterrupt: Ctrl-C ended the run.
        """
        report = self._finish_report(Results(tuple(reports)), stop)
        if not sync:
            await _off(self._warn_unsynced, resolved, rows=rows)
        return report

    def _finish_report(self, report: Results, stop: Stop) -> Results:
        """Raise `KeyboardInterrupt` when the run was stopped, else log its totals and return it.

        A stopped run raises only here, once every target has its report, so the `runs` row
        still ends with the files saved before the stop.

        Args:
            report: the command's report.
            stop: the run's stop state.

        Returns:
            `report`, unchanged.

        Raises:
            KeyboardInterrupt: Ctrl-C (or the run's stop) ended the run.
        """
        if stop.soon.is_set():
            raise KeyboardInterrupt
        _log_totals(report)
        return report

    async def _sync_normal(
        self,
        runner: _Runner,
        chosen: Sequence[tuple[Target, Options]],
        results: dict[int, TargetReport],
        stop: Stop,
    ) -> None:
        """Sync the plain targets of `chosen` (not searches), each stopping at saved posts.

        Args:
            runner: the sync's run, which every target shares.
            chosen: every target of the sync, with its options; searches are left out here.
            results: filled with each plain target's report, by its index in `chosen`.
            stop: the run's stop state.

        Raises:
            KeyboardInterrupt: Ctrl-C stopped the run; the searches aren't synced then.
        """
        indices = [i for i, (target, _) in enumerate(chosen) if not self._is_query(target)]
        if not indices:
            return
        normal = [chosen[i] for i in indices]
        await _off(self._record, normal)
        ui.info("Storage: {path}", path=await _off((self.home / store.MEDIA).resolve))
        reports = await runner.run([(t, replace(opts, stop_at_saved=True)) for t, opts in normal])
        if stop.soon.is_set():
            raise KeyboardInterrupt
        for pos, rep in zip(indices, reports):
            results[pos] = rep

    async def _sync_queries(
        self,
        runner: _Runner,
        chosen: Sequence[tuple[Target, Options]],
        results: dict[int, TargetReport],
        stop: Stop,
    ) -> None:
        """Sync the searches of `chosen`, one after another, after the plain targets.

        Args:
            runner: the sync's run, which every target shares.
            chosen: every target of the sync, with its options; plain targets are left out.
            results: filled with each search's report, by its index in `chosen`.
            stop: the run's stop state.

        Raises:
            KeyboardInterrupt: Ctrl-C stopped the run after a search's report was kept.
        """
        indices = [i for i, (target, _) in enumerate(chosen) if self._is_query(target)]
        if not indices:
            return
        query_chosen = [chosen[i] for i in indices]
        await _off(self._record, query_chosen)
        for pos, (target, options) in zip(indices, query_chosen):
            results[pos] = await self._sync_query(runner, target, options, stop=stop)
            if stop.soon.is_set():
                raise KeyboardInterrupt

    def _target_info(
        self, target: Target, rows: Mapping[str, store.TargetRow], db: store.Store,
    ) -> TargetInfo:
        """A target's state and its saved files' count and bytes, from `flnd.db` alone.

        A target with a row has its counts there, kept by each run; a folder with no row (a
        target downloaded before targets were recorded) is counted in one query, never by
        walking its folder.

        Args:
            target: the target.
            rows: every target row, by key.
            db: the home's database, for a target with no row.

        Returns:
            What `flnd ls` shows of it.
        """
        row = rows.get(store.key(target))
        is_query = self._is_query(target)
        if is_query:
            files, size = 0, 0
        elif row is not None:
            files, size = row.files, row.bytes
        else:
            files, size = db.count(store.key(target))
        complete = row.complete if row is not None else {}
        cursors = row.cursors if row is not None else {}
        categories = (
            {}
            if is_query
            else {
                cat: (
                    "complete" if cat in complete
                    else ("interrupted" if cat in cursors else "incomplete")
                )
                for cat in self.sources[target.plugin].categories
            }
        )
        return TargetInfo(
            target=target,
            # A folder no row names was downloaded before following existed: it syncs.
            sync=row.follow if row is not None else True,
            at=row.last_sync if row is not None else "",
            files=files,
            size=size,
            seen=len(row.seen) if is_query and row is not None else 0,
            categories=categories,
            query=is_query,
        )

    def _is_query(self, target: Target) -> bool:
        """Whether `target`'s route discovers other targets (a search) rather than posts."""
        plugin = self.manifest.plugins.get(target.plugin)
        if plugin is None and self._widen(target.plugin):
            plugin = self.manifest.plugins.get(target.plugin)
        if plugin is not None:
            route = next((r for r in plugin.routes if r.name == target.kind), None)
            return route is not None and route.discovers
        try:
            source = self.sources[target.plugin]
        except PluginError:
            return False
        route = next((r for r in source.routes if r.kind == target.kind), None)
        return route is not None and route.discovers

    async def _dry_run(
        self,
        chosen: Sequence[tuple[Target, Options]],
        span: Range,
        stop: Stop,
    ) -> Results:
        """Plan every target without saving anything (`--dry`), one `Planned` event per file.

        Args:
            chosen: the targets, in order, with their options.
            span: `--when`, which a search filters what it finds by.
            stop: the run's stop state.

        Returns:
            One report per target, with no files written; a failed plan carries its error.
        """
        # A dry run reads `flnd.db` to tell "saved" from "new", but never writes it: a home
        # without one stays untouched.
        async with self._running(stop, dry=True) as runner:
            return Results(tuple([await self._dry_one(runner, target, options, span, stop)
                                 for target, options in chosen]))

    async def _dry_one(
        self,
        runner: _Runner,
        target: Target,
        options: Options,
        span: Range,
        stop: Stop,
    ) -> TargetReport:
        """Plan one target of a dry run, on its plugin's instance.

        A failure ends this target's plan alone, as a failed report and a `Failed` event; the
        first Ctrl-C ends it as stopped, and the second goes on up.

        Args:
            runner: the dry run's run (`dry` on, the database read-only).
            target: the target to plan.
            options: its options.
            span: `--when`, for a search.
            stop: the run's stop state.

        Returns:
            Its report: no files, and the error that ended the plan, if any.

        Raises:
            KeyboardInterrupt: the second Ctrl-C, or a stop that isn't the run's.
        """
        from flnd.engine import crawl
        from flnd.engine.crawl import TargetReport

        entry, failed = await runner.ready(target)
        if entry is None or failed is not None:
            assert failed is not None
            return failed
        try:
            await _log_dry(entry.run, target, options, span, is_query=self._is_query(target))
        except KeyboardInterrupt:
            if not stop.soon.is_set() or stop.now.is_set():
                raise
            return TargetReport(target, (), 0, error=Stopped(crawl.STOPPED))
        except Exception as error:
            failure = crawl.failure(error, target, entry.run.source)
            await crawl.tell(target, failure, self.emit)
            return TargetReport(target, (), 0, error=failure)
        return TargetReport(target, (), 0)

    async def _run_query(
        self,
        runner: _Runner,
        target: Target,
        options: Options,
        *,
        span: Range,
        stop: Stop,
    ) -> list[TargetReport]:
        """Download a search, its row recorded already: list the targets it finds, then
        download each one.

        Its found targets count on the search's row of the run's snapshot (`Tally.adopt`).

        Args:
            runner: the command's run.
            target: the search.
            options: its options; `limit` bounds how many targets it takes.
            span: `--when`, which the search filters what it finds by.
            stop: the run's stop state.

        Returns:
            One report per target found; only the search's own when it needs a login it lacks.
        """
        self.tally.add(target, _planned(options))
        entry, failed = await runner.ready(target)
        if entry is None or failed is not None:
            assert failed is not None
            return [failed]
        reports: list[TargetReport] = []
        await self._discover_and_run(runner, entry.run, target, options, reports, span=span,
                                     stop=stop)
        return reports

    async def _discover_and_run(
        self,
        runner: _Runner,
        run: PluginRun,
        target: Target,
        options: Options,
        reports: list[TargetReport],
        *,
        span: Range,
        stop: Stop,
    ) -> None:
        """List a search's targets and download each as it comes, then keep what it saw.

        Each found target that downloads without error is added to the search's `seen` keys
        in `flnd.db`, so a later sync of the search skips it. A failure of the listing itself
        is one more report, for the search; the first Ctrl-C stops it after the target going
        on, and the second goes on up.

        Args:
            runner: the command's run, which runs each found target on its own plugin.
            run: the search's source run.
            target: the search.
            options: its options; the found targets get them with no limit, and the search's
                text as their `query` (for the `{query}` folder field).
            reports: filled with a report per target downloaded, and one for a failure.
            span: `--when`, which the search filters what it finds by.
            stop: the run's stop state.

        Raises:
            KeyboardInterrupt: the second Ctrl-C, or a stop that isn't the run's.
        """
        from flnd.engine import crawl
        from flnd.engine.crawl import TargetReport

        seen_keys: list[str] = []
        try:
            async with contextlib.aclosing(crawl.discover(run, target, limit=options.limit,
                                                          time_range=span)) as found:
                async for child in found:
                    if stop.soon.is_set():
                        break
                    child_opts = replace(options, limit=None, query=target.value, link=None)
                    rep = await runner.target(child, child_opts, parent=target)
                    if rep.error is None:
                        seen_keys.append(store.key(child))
                    reports.append(rep)
        except KeyboardInterrupt:
            if not stop.soon.is_set() or stop.now.is_set():
                raise
            reports.append(TargetReport(target, (), 0, error=Stopped(crawl.STOPPED)))
        except Exception as error:
            failed = crawl.failure(error, target, run.source)
            await crawl.tell(target, failed, self.emit)
            reports.append(TargetReport(target, (), 0, error=failed))
        finally:
            self.tally.end(target, failed=any(r.error is not None for r in reports))
            if seen_keys and run.settings.db is not None:
                await run.off(run.settings.db.seen, target, seen_keys)

    async def _sync_query(
        self,
        runner: _Runner,
        target: Target,
        options: Options,
        *,
        stop: Stop,
    ) -> TargetReport:
        """Sync one search: download the targets it finds that it hadn't seen before.

        A found target is new when it is neither in the search's `seen` keys nor a target the
        home knows already (followed or downloaded on its own).

        Args:
            runner: the sync's run.
            target: the search.
            options: its saved options.
            stop: the run's stop state.

        Returns:
            The search's report: the files its new targets saved, the targets skipped, and the
            first error, if any.
        """
        rows = await _off(self._rows)
        row = rows.get(store.key(target))
        seen_keys = set(row.seen if row is not None else ())
        known_keys = set(rows)
        self.tally.add(target, _planned(options))
        entry, failed = await runner.ready(target)
        if entry is None or failed is not None:
            assert failed is not None
            return failed
        return await self._sync_query_run(runner, entry.run, target, options, seen_keys,
                                          known_keys, stop)

    async def _sync_query_run(
        self,
        runner: _Runner,
        run: PluginRun,
        target: Target,
        options: Options,
        seen_keys: set[str],
        known_keys: set[str],
        stop: Stop,
    ) -> TargetReport:
        """Run a search's sync, and keep the keys it saw, however it ends.

        Args:
            runner: the sync's run.
            run: the search's source run.
            target: the search.
            options: its saved options.
            seen_keys: the keys of the targets earlier syncs saw.
            known_keys: the keys of every target the home has a row for.
            stop: the run's stop state.

        Returns:
            The search's report; a failed listing or a stop is its `error`, and else the first
            found target's error.

        Raises:
            KeyboardInterrupt: the second Ctrl-C, or a stop that isn't the run's.
        """
        from flnd.engine import crawl
        from flnd.engine.crawl import TargetReport

        written: list[Path] = []
        skipped, child_failed, seen_to_mark, failed = 0, None, [], False
        try:
            skipped, child_failed = await self._sync_query_items(
                runner, run, target, options, seen_keys, known_keys, stop, written,
                seen_to_mark)
        except KeyboardInterrupt:
            if not stop.soon.is_set() or stop.now.is_set():
                raise
            failed = True
            return TargetReport(target, tuple(written), skipped, error=Stopped(crawl.STOPPED))
        except Exception as error:
            failed = True
            ended = crawl.failure(error, target, run.source)
            await crawl.tell(target, ended, self.emit)
            return TargetReport(target, tuple(written), skipped, error=ended)
        finally:
            self.tally.end(target, failed=failed or child_failed is not None)
            if seen_to_mark and run.settings.db is not None:
                await run.off(run.settings.db.seen, target, seen_to_mark)
        return TargetReport(target, tuple(written), skipped, error=child_failed)

    async def _sync_query_items(
        self,
        runner: _Runner,
        run: PluginRun,
        target: Target,
        options: Options,
        seen_keys: set[str],
        known_keys: set[str],
        stop: Stop,
        written: list[Path],
        seen_to_mark: list[str],
    ) -> tuple[int, Error | Stopped | None]:
        """List a search's targets and download the new ones, at most `MAX_SYNC_NEW` of them.

        The listing walks past the targets seen before (or known to the home), which are
        marked seen, so a sync that took its slice of new ones leaves the rest for the next. A
        new one is downloaded, and marked seen only when it downloaded without
        error, so a failed one is tried again on the next sync.

        Args:
            runner: the sync's run.
            run: the search's source run.
            target: the search.
            options: its saved options.
            seen_keys: the keys of the targets earlier syncs saw.
            known_keys: the keys of every target the home has a row for.
            stop: the run's stop state.
            written: filled with the paths the new targets saved.
            seen_to_mark: filled with the keys to add to the search's `seen`.

        Returns:
            The files skipped, and the first found target's error (None when
            none failed).
        """
        from flnd.engine import crawl

        skipped, new_count, child_failed = 0, 0, None
        async with contextlib.aclosing(crawl.discover(run, target, time_range=Range())) as found:
            async for child in found:
                if stop.soon.is_set():
                    break
                child_key = store.key(child)
                if child_key in seen_keys or child_key in known_keys:
                    # A target seen before is passed over: `skipped` counts files alone.
                    seen_to_mark.append(child_key)
                    continue
                if new_count >= MAX_SYNC_NEW:
                    break
                new_count += 1
                child_opts = replace(options, limit=None, query=target.value, link=None)
                rep = await runner.target(child, child_opts, parent=target)
                written.extend(rep.written)
                skipped += rep.skipped
                if rep.error is None:
                    seen_to_mark.append(child_key)
                else:
                    child_failed = rep.error
        return skipped, child_failed

    @contextlib.asynccontextmanager
    async def _live(self) -> AsyncIterator[None]:
        """Count one command afresh, publishing its snapshots while it runs (`run.state`)."""
        self.tally = Tally(self._shown)
        async with self.tally.publishing():
            yield

    @contextlib.asynccontextmanager
    async def _running(self, stop: Stop, *, dry: bool = False) -> AsyncIterator[_Runner]:
        """A command's run: the home's database, the browser, the save and the plugin instances.

        Args:
            stop: the run's stop state.
            dry: a dry run, which reads the database and writes nothing.

        Yields:
            The run, whose plugin instances are closed when the block ends.
        """
        from flnd.engine import crawl, ctx
        from flnd.engine.crawl import RunShared
        from flnd.engine.ctx import shared_browser

        # The home's settings are read once, off the loop: its browser, its save and each
        # plugin's run take theirs from them, and a file's warnings are said once.
        found = await _off(config.settings, self.home, plugins=self.sources)
        db = await _off(store.open, self.home)
        try:
            settings = RunShared(home=self.home, db=db, run=None if dry else self.run,
                                 stop=stop, dry=dry, emit=self.emit, resolve=self._one,
                                 tally=self.tally, config=found)
            settings.claims = crawl.Claims()
            runner = _Runner(self, settings)
            runner.worked = await _off(_worked, db)
            settings.follow, settings.peer = runner.follow, runner.peer
            async with shared_browser(settings), save.attached(settings):
                scheduler = ctx.scheduler(settings)
                assert scheduler is not None
                runner.switcher = scheduler.switch = auth.Switcher(
                    scheduler, lambda code: found.plugin(code).switch, runner.listed,
                    shown=self.tally.switched)
                try:
                    yield runner
                finally:
                    await runner.close()
        finally:
            await _off(db.close)

    def _warn_unsynced(
        self, resolved: Sequence[Target], *, rows: Mapping[str, store.TargetRow] | None = None,
    ) -> None:
        """Remind the user how to keep a target up to date, when one of them isn't followed."""
        saved = self._rows() if rows is None else rows
        if any((row := saved.get(store.key(t))) is None or not row.follow for t in resolved):
            ui.info("not followed; flnd follow keeps it up to date")

    def _recorded(self, rows: Mapping[str, store.TargetRow]) -> list[Target]:
        """The targets of `flnd.db`'s rows, rebuilt from each row's source, kind and value.

        A row of a source that is gone (an uninstalled plugin), or with no kind (a row an old
        home migrated without one), is left out. No source's `resolve` runs, so this costs no
        import of a source and no request.

        Args:
            rows: target rows, by key.

        Returns:
            The targets, in the rows' order; a search is written `<code>?<query>`.
        """
        return [
            Target(f"{row.plugin}{'?' if self._is_query_kind(row.plugin, row.kind) else ':'}"
                   f"{row.value}", kind=row.kind)
            for row in rows.values()
            if row.kind and row.plugin in self.sources
        ]

    def _rows(self) -> dict[str, store.TargetRow]:
        """Every target `flnd.db` knows, by key; none for a home that has no database yet."""
        with store.open(self.home) as db:
            return db.targets()

    def _record(self, chosen: Sequence[tuple[Target, Options]], *, follow: bool | None = None,
                keep: bool = False) -> None:
        """Remember the targets of a run in `flnd.db`, in one transaction.

        Args:
            chosen: each target, with its options.
            follow: as `store.Store.record` takes it.
            keep: whether to keep each target's categories for later syncs; otherwise the
                stored ones stay.
        """
        if not chosen:
            return
        with store.open(self.home) as db, db.transaction():
            for target, options in chosen:
                db.record(target, follow=follow, query=options.query,
                          categories=(options.categories or ()) if keep else None)

    @contextlib.asynccontextmanager
    async def _recording(self, command: str, targets: Sequence[Target]) -> AsyncIterator[None]:
        """A `runs` row for a command that downloads, ended with the files it saved, however
        it ends (a stopped run's row keeps its count too); its reads and writes are off the
        loop.

        Args:
            command: the command's name.
            targets: its targets; the first three are named in the row.
        """
        named = " ".join([command, *map(str, targets[:3])])
        self.run = await _off(self._started, named if len(targets) <= 3 else f"{named} …")
        try:
            yield
        finally:
            run, self.run = self.run, None
            # The command's tally: `_live` made it before, or inside, this block.
            await _off(self._ended, run, self.tally.saved)

    def _started(self, named: str) -> int:
        """Open a command's `runs` row, after sweeping the month-old logs (src/docs/storage)."""
        store.sweep(self.home)
        with store.open(self.home) as db:
            return db.start(named)

    def _ended(self, run: int | None, saved: int) -> None:
        with store.open(self.home) as db:
            db.end(run, saved=saved)

    def _is_query_kind(self, code: str, kind: str) -> bool:
        """Whether the route of `kind` in source `code` takes a query (`<code>?<query>`)."""
        route = next((r for r in self.sources[code].routes if r.kind == kind), None)
        return route is not None and route.is_query

    def _plan(self, targets: Sequence[Chosen]) -> list[tuple[Target, Options]]:
        """Which targets a sync takes, each with the options its row keeps.

        With no targets named, a sync takes every followed row, then every folder under
        `media/` no row names (a target downloaded before following existed, which syncs).

        Args:
            targets: the targets named, with their categories; empty for every synced target.

        Returns:
            Each target with its categories and search text.
        """
        from flnd.engine.crawl import Options

        rows = self._rows()
        if not targets:
            followed = {item: row for item, row in rows.items() if row.follow}
            recorded = self._recorded(rows)
            targets = [Chosen(target, rows[store.key(target)].categories or None)
                       for target in self._recorded(followed)]
            targets += [Chosen(target) for target in self._unrecorded(recorded, rows)]
        planned = []
        for target, categories, link in targets:
            row = rows.get(store.key(target))
            planned.append((target, Options(categories=categories, link=link or None,
                                            query=row.query if row else None)))
        return planned

    def _unrecorded(
        self, known: Sequence[Target], rows: Mapping[str, store.TargetRow] | None = None,
    ) -> Iterator[Target]:
        """The targets whose folders are under `media/` but which no row of `known` names.

        A folder a search's found target saved into belongs to the search (its `seen` keys),
        so it isn't yielded on its own. A folder no source can read as a target is logged at
        -vv and passed over. It lists one folder per source, never a target's files.

        Args:
            known: the targets the home's rows name.
            rows: those rows, by key, for the searches' `seen` keys; None for none.

        Yields:
            Each unrecorded target, resolved by its source, in folder-name order per source.
        """
        seen = {store.target_folder(self.home, t) for t in known if not self._is_query(t)}
        media = self.home / store.MEDIA
        saved_rows = rows or {}
        for target in known:
            row = saved_rows.get(store.key(target))
            if self._is_query(target) and row is not None:
                for child_key in row.seen:
                    if ":" in child_key:
                        seen.add(store.target_folder(self.home, Target(child_key)))
        for code in self.sources:
            for folder in sorted((media / code).iterdir()) if (media / code).is_dir() else ():
                if folder.is_dir() and folder not in seen:
                    try:
                        yield self._one(f"{code}:{folder.name}")
                    except UsageError as error:
                        ui.debug(
                            "{folder}: not synced: {error}",
                            folder=f"{code}/{folder.name}",
                            error=error,
                        )

    def _one(self, text: str) -> Target:
        """Resolve one target's text on any plugin, following a short link once; the
        categories it names are left out."""
        return self._read(text, {}).target


@dataclass(eq=False)
class _Entry:
    """One plugin's instance in a run: its source run, its login, and its `connect` hook."""

    run: PluginRun
    stack: contextlib.AsyncExitStack
    # Whether its login is kept (a source without a login always is).
    logged_in: bool = True
    # What broke it as it started (its `connect`, its login check); its targets fail with it.
    error: BaseException | None = None
    # A login the site refused: its later targets would hit the same wall, so they fail with it.
    refused: LoginError | None = None
    # Set once its login is checked and its `connect` entered: what its other targets wait for.
    started: asyncio.Event = field(default_factory=asyncio.Event)


class _Runner:
    """A command's run: what its targets share, one instance per plugin, and the targets.

    Each plugin gets one instance for the whole run (`entry`), made the first time one of its
    targets runs, discovered and linked ones included, so a cache it keeps serves them all.
    Its login is checked and its `connect` hook entered then, once; `close` leaves them.

    Args:
        engine: the engine running the command.
        settings: what every target of the run shares.
    """

    def __init__(self, engine: Engine, settings: RunShared) -> None:
        self.engine, self.settings = engine, settings
        # Each (plugin, account)'s instance.
        self._entries: dict[tuple[str, str | None], _Entry] = {}
        # Each plugin's accounts, read once; and the account that worked for each target, by
        # key, read as the run starts (`Engine._running`).
        self._accounts: dict[str, asyncio.Future[list[Account]]] = {}
        self.worked: dict[str, str] = {}
        # The run's switcher, whose holds and expiries `close` writes; None until it runs.
        self.switcher: auth.Switcher | None = None
        self._active = asyncio.Semaphore(ACTIVE)
        # Each plugin's and account's listings at once.
        self._listing: dict[tuple[str, str | None], asyncio.Semaphore] = {}
        # The targets the run has started, each run once however often it is named; set
        # once each ends, with its report.
        self._once: dict[Target, asyncio.Event] = {}
        self._reports: dict[Target, TargetReport] = {}
        # Targets a listing of another plugin discovered, handed to the run (`follow`).
        self._handed: list[asyncio.Future[tuple[bool, Any]]] = []
        # Each target's turn at its first step, in the order they started (`crawl.Turn`).
        self._turns: list[asyncio.Event] = []

    async def entry(self, code: str, account: Account | None) -> _Entry:
        """The run's instance of the plugin `code` for `account`, started on first use.

        Raises:
            UsageError: the home's config.toml has a value the plugin doesn't take.
            LoginError: the vault's key is gone.
        """
        from flnd.engine import crawl, ctx

        key = (code, account.name if account is not None else None)
        entry = self._entries.get(key)
        if entry is not None:
            await entry.started.wait()
            if entry.run is None:
                # Its plugin's run didn't open: each of its targets fails with that.
                assert entry.error is not None
                raise entry.error
            return entry
        source = self.engine.sources[code]()
        stack = contextlib.AsyncExitStack()
        # Kept before the first wait, so a target that comes meanwhile waits for this one.
        entry = self._entries[key] = _Entry(None, stack)  # type: ignore[arg-type]
        try:
            run = entry.run = await stack.enter_async_context(
                ctx.opened(self.settings, source, account))
        except BaseException as error:
            entry.error = error
            entry.started.set()
            raise
        try:
            # The plugin's state, read once, off the loop, and written back as the run ends.
            await run.off(run.load_state)
            stack.push_async_callback(run.off, run.save_state)
            entry.logged_in = await _logged_in(run)
            if not entry.logged_in and _required(source):
                return entry
            if not entry.logged_in:
                ui.warn("{code}: not logged in, using public access; run flnd login {code}",
                        code=code)
            if account is not None:
                stack.push_async_callback(run.off, auth.write_back, run.settings.home, account,
                                          run.session.jar)
            await stack.enter_async_context(_connected(run, _account(run)))
            # One cache of the posts the `date` hook had no day for, shared by its targets.
            await run.off(crawl.asked, run)
        except Exception as error:
            entry.error = error
            ui.debug("{source}: {error}", source=code, error=error)
        finally:
            entry.started.set()
        return entry

    async def ready(self, target: Target) -> tuple[_Entry | None, TargetReport | None]:
        """The instance `target` runs on, or the failed report of a target that can't run.

        A target fails here, with a `Failed` event, when its plugin didn't start, needs a
        login it lacks, or had its login refused by an earlier target of the run.
        """
        from flnd.engine import crawl
        from flnd.engine.crawl import TargetReport

        settings, tally = self.settings, self.settings.tally
        try:
            entry = await self.entry(target.plugin, await self._signed(target))
        except Exception as error:
            failed = crawl.failure(error, target)
            tally.end(target, failed=True)
            await crawl.tell(target, failed, settings.emit)
            return None, TargetReport(target, (), 0, error=failed)
        failed = None
        if entry.error is not None:
            failed = crawl.failure(entry.error, target, entry.run.source)
        elif not entry.logged_in and _required(entry.run.source):
            failed = crawl.logged_out(target)
        elif entry.refused is not None:
            failed = entry.refused
        if failed is None:
            return entry, None
        if failed is entry.refused:
            return entry, await self._refused(target, failed)
        tally.end(target, failed=True)
        await crawl.tell(target, failed, settings.emit)
        return entry, TargetReport(target, (), 0, error=failed)

    async def _signed(self, target: Target) -> Account | None:
        """The account `target` runs as: the one that worked for it last, else the plugin's
        first usable one (`auth.pick`)."""
        db = self.settings.db
        if db is None:
            return None
        found = self._accounts.get(target.plugin)
        if found is None:
            found = self._accounts[target.plugin] = asyncio.ensure_future(
                _off(_accounts, db, target.plugin))
        return auth.pick(await found, preferred=self.worked.get(store.key(target)))

    def listed(self, code: str) -> list[Account]:
        """A plugin's accounts, once read for one of its targets; none before."""
        found = self._accounts.get(code)
        return found.result() if found is not None and found.done() else []

    async def peer(self, code: str, name: str) -> PluginRun:
        """The plugin's instance for its account `name`, which a job moved to it runs on.

        Raises:
            Error: what broke that instance as it started.
        """
        account = next(found for found in self.listed(code) if found.name == name)
        entry = await self.entry(code, account)
        if entry.error is not None:
            raise entry.error
        return entry.run

    async def _refused(self, target: Target, login: LoginError) -> TargetReport:
        """A target whose plugin's login an earlier target had refused: it fails, unasked."""
        from flnd.engine.crawl import TargetReport

        self.settings.tally.end(target, failed=True)
        await self.settings.emit(Failed(target, login))
        return TargetReport(target, (), 0, error=login)

    async def follow(self, target: Target, options: Options, *, inside: str) -> None:
        """Run a target a listing of the plugin `inside` discovered.

        One of the same plugin runs inside that listing, which holds its places. One of
        another plugin is handed to the run as a target of its own, which the listing doesn't
        wait for: waiting for that plugin's listing place while holding its own could wait
        forever, for a listing of that plugin waiting the other way.
        """
        if inside == target.plugin:
            await self.target(target, options, inside=inside)
            return
        self.settings.tally.add(target, _planned(options))
        # The target's coroutine is made once its task starts: one cancelled before (a full
        # disk) leaves no coroutine never awaited.
        self._handed.append(asyncio.ensure_future(_carried_call(self.target, target, options)))

    async def target(self, target: Target, options: Options, *,
                     parent: Target | None = None, inside: str | None = None,
                     turn: crawl.Turn | None = None) -> TargetReport:
        """Run one target on its plugin's instance, a discovered or linked one included; a
        target named twice in the run runs once, and the second gets the first's report.

        It waits for a place among its plugin's listings, then among the run's active
        targets (`_held`). A target a listing of its own plugin discovered runs inside that
        listing (`inside`), and takes no place of its own (`follow`).

        Args:
            target: the target, resolved.
            options: its options.
            parent: the search that found it; a target of the search's own plugin counts on
                the search's row of the run's snapshot.
            inside: the plugin of the listing it runs inside; None for a target of its own.
            turn: its turn at its route's call (`crawl.Turn`); None takes the next.

        Returns:
            Its report.

        Raises:
            DiskError: its files can't be written; the whole run stops.
        """
        from flnd.engine.crawl import TargetReport

        began = turn or self._turn()
        started = self._once.get(target)
        if started is not None:
            began.release()
            await started.wait()
            return self._reports.get(target) or TargetReport(target, (), 0)
        self._once[target] = asyncio.Event()
        try:
            report = self._reports[target] = await self._run(target, options, parent, inside,
                                                             began)
        except BaseException as error:
            # What stops the run (a full disk) keeps the later targets' routes from running.
            if not jobs.stops(error):
                began.release()
            raise
        finally:
            self._once[target].set()
        began.release()
        return report

    def _turn(self) -> crawl.Turn:
        """The next target's turn at its first step, after every turn given out so far."""
        from flnd.engine import crawl

        turn = crawl.Turn(self._turns)
        self._turns.append(turn.passed)
        return turn

    async def _run(self, target: Target, options: Options, parent: Target | None,
                   inside: str | None, began: crawl.Turn) -> TargetReport:
        from flnd.engine import crawl

        if parent is not None and parent.plugin == target.plugin:
            self.settings.tally.adopt(target, parent)
        entry, failed = await self.ready(target)
        if entry is None or failed is not None:
            assert failed is not None
            return failed
        refused = None
        if inside == target.plugin:
            report = await crawl.download(entry.run, target, self.settings.tally, options,
                                          turn=began)
        else:
            async with self._held(entry, began) as listed:
                # Refused while this target waited for its place: it fails, unasked, once
                # it has given its places back.
                refused = entry.refused
                if refused is None:
                    report = await crawl.download(entry.run, target, self.settings.tally,
                                                  options, listed=listed, turn=began)
        if refused is not None:
            return await self._refused(target, refused)
        if isinstance(report.error, LoginError):
            entry.refused = report.error
        # A full or refused disk stops the whole run: every later file would fail too.
        elif isinstance(report.error, DiskError):
            raise report.error
        return report

    @contextlib.asynccontextmanager
    async def _held(self, entry: _Entry,
                    turn: crawl.Turn) -> AsyncIterator[Callable[[Any], Awaitable[None]]]:
        """A target's places: one of its plugin's listings, then one of the run's active ones;
        a target that waits for one lets the next targets have their turns.

        Yields:
            What the crawl awaits once the target's listings ended: it gives the listing's
            place back at once, and the active one when the target has no more files queued
            than its places, so the next target lists while these download.
        """
        source, account = entry.run.source, entry.run.account
        key = (source.code, account.name if account is not None else None)
        listing = self._listing.get(key)
        if listing is None:
            browser = type(source).browser is not Browser.NONE
            listing = self._listing[key] = asyncio.Semaphore(1 if browser else HTTP_AT_ONCE)
        held = {"listing": False, "active": False}
        if listing.locked():
            turn.release()
        await listing.acquire()
        held["listing"] = True
        try:
            if self._active.locked():
                turn.release()
            await self._active.acquire()
            held["active"] = True

            async def listed(downloads: Any) -> None:
                if held["listing"]:
                    held["listing"] = False
                    listing.release()
                await downloads.below(entry.run.jobs)
                if held["active"]:
                    held["active"] = False
                    self._active.release()

            yield listed
        finally:
            if held["active"]:
                self._active.release()
            if held["listing"]:
                listing.release()

    async def run(self, chosen: Sequence[tuple[Target, Options]], *,
                  depth: int = 0) -> list[TargetReport]:
        """Run the targets at once, as their places allow; then the targets their listings
        found (`found`).

        They start in order: each once the one before it has called its route, so what stops
        the run at a target's first step (a full disk) stops it before the next starts.

        Args:
            chosen: the targets, each with its options.
            depth: how many links deep these targets are; 0 for the run's own.

        Returns:
            One report per target of `chosen`, in order, then one per target found.

        Raises:
            DiskError: a target's files can't be written; the whole run stops.
            KeyboardInterrupt: the second Ctrl-C.
        """
        # A target named twice runs once, with one report.
        once: dict[Target, tuple[Target, Options]] = {}
        for target, options in chosen:
            once.setdefault(target, (target, options))
        chosen = list(once.values())
        for target, options in chosen:
            self.settings.tally.add(target, _planned(options))
        tasks: list[asyncio.Future[tuple[bool, Any]]] = []
        try:
            tasks = [asyncio.ensure_future(_carried(self.target(target, options,
                                                                turn=self._turn())))
                     for target, options in chosen]
            pending = list(tasks)
            while pending:
                for ended in asyncio.as_completed(pending):
                    ok, value = await ended
                    if not ok:
                        # A full disk or a second Ctrl-C stops the run: the targets still
                        # listing or waiting for a place end at once.
                        raise value
                # The targets the listings handed over meanwhile run too, as the run's own.
                pending, self._handed = self._handed, []
                tasks.extend(pending)
        except BaseException:
            # The targets handed over by a listing end with the rest.
            tasks.extend(self._handed)
            self._handed = []
            raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        reports: list[TargetReport] = []
        for task in tasks:
            report = task.result()[1]
            # A handed target that was one of the run's own already has its report.
            if all(report.target != kept.target for kept in reports):
                reports.append(report)
        return reports + await self.found(chosen, reports, depth=depth)

    async def found(self, chosen: Sequence[tuple[Target, Options]],
                    reports: Sequence[TargetReport], *, depth: int) -> list[TargetReport]:
        """Download the targets the run's listings yielded (`TargetReport.found`), once each.

        A post that links to another site hands the link on this way. Each found target is
        resolved by its own source and downloaded with every category, after the run's own
        targets; what those find in turn is followed up to `FOLLOW_DEPTH` links deep, so two
        posts linking to each other can't loop.

        Args:
            chosen: the targets that ran, with their options.
            reports: their reports, in order.
            depth: how many links deep these reports are.

        Returns:
            The found targets' reports, in the order they were found.
        """
        from flnd.engine.crawl import Options

        if depth >= FOLLOW_DEPTH or self.settings.stop.soon.is_set():
            return []
        ran = {target for target, _ in chosen}
        found: dict[Target, Options] = {}
        for (target, options), report in zip(chosen, reports):
            for item in report.found:
                try:
                    child = item if item.kind else await asyncio.to_thread(self.engine._one,
                                                                           str(item))
                except Error as error:
                    ui.warn("{target}: can't follow {link}: {error}", target=str(target),
                            link=str(item), error=error)
                    continue
                if child in ran or child in found:
                    continue
                found[child] = Options(since=options.since, until=options.until)
        if not found:
            return []
        ui.info("{targets} linked from the listings", targets=text.count(len(found), "target"))
        await _off(self.engine._record, list(found.items()), follow=False)
        return await self.run(list(found.items()), depth=depth + 1)

    async def close(self) -> None:
        """End the targets handed to the run that no `run` waited for (a search's found
        targets', say), then leave each plugin's `connect` hook, write back its cookies and
        close its run.

        A hook whose exit fails is reported, as a `Failed` event for the plugin's account:
        the files are saved, but the run isn't all right.
        """
        from flnd.engine import crawl, ctx

        scheduler = ctx.scheduler(self.settings)
        stopped = scheduler is not None and scheduler.stopped is not None
        while self._handed:
            handed, self._handed = self._handed, []
            if stopped or self.settings.stop.now.is_set():
                # What stopped the run ends them, as it ends the run's own targets.
                for task in handed:
                    task.cancel()
            await asyncio.gather(*handed, return_exceptions=True)
        entries, self._entries = list(self._entries.values()), {}
        for entry in entries:
            try:
                await entry.stack.aclose()
            except Exception as error:
                account = Target(f"{entry.run.source.code}:", kind=auth.ACCOUNT)
                await crawl.tell(account, crawl.failure(error, account, entry.run.source),
                                 self.settings.emit)
        db = self.settings.db
        if self.switcher is not None and scheduler is not None and db is not None \
                and not self.settings.dry:
            await _off(auth.settle, db, self.switcher, scheduler.expired)


async def _off(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """`fn(*args, **kwargs)` on a thread: the engine's own blocking step (a read or write of
    `flnd.db`, a stat), so the loop never waits for it. The thread is the loop's default
    executor's, which the run shuts down at its end."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _drive(command: Coroutine[Any, Any, T]) -> T:
    """Run an engine command on a loop of its own, on this thread, and return what it gives.

    Raises:
        BaseException: what the command raised, `KeyboardInterrupt` included.
    """
    loop = asyncio.new_event_loop()
    try:
        ok, value = loop.run_until_complete(_carried(command))
        loop.run_until_complete(_shut_down())
    finally:
        loop.close()
    if not ok:
        raise value
    return value


async def _carried(awaitable: Awaitable[T]) -> tuple[bool, Any]:
    """`(True, value)`, or `(False, error)`, so a `KeyboardInterrupt` never leaves the loop."""
    try:
        return True, await awaitable
    except BaseException as error:
        return False, error


async def _carried_call(call: Callable[..., Awaitable[T]], *args: Any) -> tuple[bool, Any]:
    """`_carried(call(*args))`, the coroutine made only as this one runs."""
    return await _carried(call(*args))


def _accounts(db: store.Store, plugin: str) -> list[Account]:
    return [auth.account(row) for row in db.accounts(plugin)]


def _worked(db: store.Store) -> dict[str, str]:
    """The account that worked for each target, by key."""
    return {item: row.account for item, row in db.targets().items() if row.account}


def _planned(options: Options) -> tuple[str, ...]:
    """The categories a target's row shows before its listings start: those it names."""
    from flnd.engine import crawl

    names = options.categories or ()
    return () if crawl.ALL in names else tuple(names)


def _account(run: PluginRun) -> Ctx:
    """The `Ctx` of a plugin's account, not of a target: what `connect` and `check` see."""
    from flnd.engine.ctx import Ctx

    return Ctx(run, Target(f"{run.source.code}:", kind=auth.ACCOUNT))


async def _logged_in(run: PluginRun) -> bool:
    """Whether the plugin's login is kept; a plugin without a login always is."""
    login = run.source.login
    if login is None:
        return True
    return bool(await login.check(_account(run)))


def _required(source: Plugin) -> bool:
    """Whether the source lists nothing without a login."""
    return source.login is not None and source.login.required


async def _log_dry(
    run: PluginRun, target: Target, options: Options, span: Range, *, is_query: bool,
) -> None:
    """List what a download of `target` would save: a log line and a `Planned` event each.

    A file is "saved" when its row or its file is already there, else "new". A
    search plans each target it finds, with no post and no path.

    Args:
        run: the source's run, whose settings hold the database and the event sink.
        target: the target to plan.
        options: its options (categories, limit, time).
        span: the time range a search filters by.
        is_query: whether the target is a search.
    """
    from flnd.engine import crawl

    emit = run.settings.emit
    if is_query:
        async with contextlib.aclosing(crawl.discover(run, target, limit=options.limit,
                                                      time_range=span)) as found:
            async for item in found:
                ui.info("{target}", target=str(item))
                await emit(Planned(item, "", None, "new"))
        return
    folder = store.target_folder(run.settings.home, target)
    db = run.settings.db
    async with contextlib.aclosing(crawl.plan(run, target, options)) as planned:
        async for placed in planned:
            ui.info("{target}: {path}", target=str(target), path=placed.path)
            dest = folder.joinpath(*placed.path.split("/"))
            saved = await run.off(_planned_saved, db, f"{run.source.code}:{placed.key}", dest)
            await emit(Planned(target, placed.post.id, dest, "saved" if saved else "new"))

def _planned_saved(db: store.Store | None, key: str, dest: Path) -> bool:
    """Whether a planned file is saved: its row or its file is there (off the loop)."""
    return (db is not None and db.known(key) is not None) or dest.is_file()



def _log_totals(report: Results) -> None:
    """Log a command's totals and its exit status at -v, for a log read after the run.

    Args:
        report: the command's report.
    """
    ui.debug(
        "totals: {files} written, {skipped} skipped, {failed} of {targets} failed; exit {status}",
        files=report.files,
        skipped=sum(target.skipped for target in report.targets),
        failed=len(report.failed),
        targets=len(report.targets),
        status=0 if report.ok else 1,
    )


def _follow(url: str) -> str:
    """Where a short link leads, from one HEAD request (`net.follow`).

    Args:
        url: the short link.

    Returns:
        The URL its redirects end at.

    Raises:
        NetworkError: no answer came; the message names the link redacted, since a link's
            query may carry a signature.
    """
    try:
        return net.follow(url)
    except NetworkError as error:
        raise NetworkError(f"can't follow {text.redact(url)}: {error}",
                           retryable=error.retryable) from None


@contextlib.asynccontextmanager
async def _connected(run: PluginRun, account: Ctx) -> AsyncIterator[None]:
    """Run the plugin's `connect` hook, an async context manager, around a block, on the run's
    loop. An error in the block is handed to its exit, which may swallow it, as `async with`
    would; an error of the exit itself goes on up, for the run to report."""
    async with run.source.connect(account):
        yield
