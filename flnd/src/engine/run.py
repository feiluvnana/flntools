"""`Run`: one command on the run's loop, read as a stream of events; Ctrl-C's stages; `Tally`.

src/docs/library explains it, and src/docs/architecture its threads.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import os
import signal
import threading
import warnings
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import msgspec

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
)
from flnkit import clock

if TYPE_CHECKING:
    from flnd.engine.ctx import Listing
    from flnd.engine.events import Event
    from flnd.plugin import Target

__all__ = ["Counts", "Flight", "Run", "Stop", "Tally", "quit_now", "restoring", "sink", "stopping"]

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


def sink(emit: Callable[[Any], Any] | None) -> Callable[[Any], Awaitable[None]]:
    """`emit` as a coroutine function the loop awaits: `Run._emit` as it is, a plain function
    (a test's `list.append`) called at once, and None dropping every event."""

    async def put(event: Any) -> None:
        if emit is not None:
            done = emit(event)
            if inspect.isawaitable(done):
                await done

    return put


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
        if not self._holds:
            return step
        hold = self._holds[-1]
        held = f"held {max(0, round(hold.until - elapsed))} s: {hold.reason}"
        return f"{step} · {held}" if step else held
