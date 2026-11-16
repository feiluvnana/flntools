"""The run's asyncio `Loop`: one event loop on a thread of its own, for any caller.

A run makes one (`flnd.site.shared_browser`), and everything async of that run happens on it:
the browser (`flnd._browser`), every HTTP transfer (`flnd._net`), a target's downloads, and an
async source's coroutines (Telegram's Telethon). Other threads hand it work with `Loop.run`,
which waits for the answer, or `Loop.submit`, which doesn't; that hop costs about 40 µs, small
against one browser round trip (0.4 ms).

    with Loop() as loop:
        title = loop.run(page.evaluate("document.title"))
        running = loop.submit(download(url))       # a concurrent.futures.Future

Rules a change must keep:
- `Loop.run` is never called on the loop's own thread: it would wait for itself forever, so it
  raises instead. Code already on the loop awaits.
- A coroutine handed over runs in a copy of its caller's context (`contextvars`), so the clock a
  test installed (`flnd._time.use`) and any other context variable reach it.
- Closing cancels whatever still runs on the loop, then stops its thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import threading
from collections.abc import Awaitable
from typing import Any, TypeVar

__all__ = ["Loop"]

R = TypeVar("R")

# How long closing waits for the event loop's cancelled tasks before leaving them.
LOOP_GRACE = 10.0


class Loop:
    """One asyncio event loop on its own thread, started on first use; `run` hands it a coroutine.

        with Loop() as loop:
            loop.run(asyncio.sleep(0.1))

    Any thread may call `run`, except the loop's own (see `run`). `close` (or leaving `with`)
    cancels what still runs and ends the thread; a closed loop refuses more work.
    """

    def __init__(self) -> None:
        # Guards the start and the close, which two listing threads may reach at once.
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def __enter__(self) -> Loop:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @property
    def here(self) -> bool:
        """Whether the calling code runs on this loop's thread, where it must await, not `run`."""
        return self._thread is not None and threading.get_ident() == self._thread.ident

    def run(self, awaitable: Awaitable[R]) -> R:
        """Run `awaitable` on the loop, wait for it, and return its value or raise its error.

        Args:
            awaitable: a coroutine (or any awaitable) made for this loop.

        Returns:
            What the awaitable returned.

        Raises:
            RuntimeError: the loop is closed, or this is the loop's own thread, which would wait
                for itself forever.

        A Ctrl-C while waiting cancels the coroutine on the loop before it goes on up.
        """
        if self.here:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise RuntimeError("Loop.run was called on the loop's own thread; await it instead")
        future = self.submit(_carried(awaitable))
        try:
            ok, value = future.result()
        except BaseException:
            future.cancel()
            raise
        if not ok:
            raise value
        return value

    def submit(self, awaitable: Awaitable[R]) -> concurrent.futures.Future[R]:
        """Start `awaitable` on the loop, in a copy of the caller's context, without waiting.

        Args:
            awaitable: a coroutine (or any awaitable) made for this loop.

        Returns:
            Its future: `result()` waits for it, `cancel()` cancels it on the loop. A
            `KeyboardInterrupt` it raises would end the loop's thread, so a caller that may
            meet one wraps the coroutine to return it instead (as `run` does).

        Raises:
            RuntimeError: the loop is closed.
        """
        try:
            loop = self._started()
        except RuntimeError:
            # Closed, so nothing will ever await it, and Python would warn of that.
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise
        context = contextvars.copy_context()
        future: concurrent.futures.Future[R] = concurrent.futures.Future()

        def start() -> None:
            # Runs on the loop: the task takes the caller's context, and its outcome (or a
            # cancel from the caller's side) is passed across. The future stays pending until
            # the task ends, so the caller can still cancel it while it runs.
            if future.cancelled():
                if inspect.iscoroutine(awaitable):
                    awaitable.close()
                return
            task = loop.create_task(_awaited(awaitable), context=context)
            task.add_done_callback(lambda done: _settled(done, future))
            future.add_done_callback(
                lambda done: done.cancelled() and loop.call_soon_threadsafe(task.cancel))

        loop.call_soon_threadsafe(start)
        return future

    def close(self) -> None:
        """Cancel what still runs on the loop, stop it and end its thread; once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        cancelled = asyncio.run_coroutine_threadsafe(_cancel_pending(), loop)
        concurrent.futures.wait([cancelled], timeout=LOOP_GRACE)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(LOOP_GRACE)
        if not thread.is_alive():
            loop.close()

    def _started(self) -> asyncio.AbstractEventLoop:
        """The event loop, with its thread started on the first call.

        Returns:
            The running event loop.

        Raises:
            RuntimeError: the loop is closed.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("the loop is closed")
            if self._loop is None:
                # On Windows the default loop is the proactor, which Playwright's driver needs
                # for its subprocess pipes; elsewhere the selector loop serves both.
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=_serve_loop, args=(self._loop,), name="flnd-loop", daemon=True,
                )
                self._thread.start()
            return self._loop


def _settled(task: asyncio.Task[Any], future: concurrent.futures.Future[Any]) -> None:
    """Pass a finished task's outcome to the future its caller holds.

    Args:
        task: the task, done.
        future: the caller's future; one the caller cancelled is left as it is.
    """
    if task.cancelled():
        future.cancel()
        return
    if not future.set_running_or_notify_cancel():
        return
    if (error := task.exception()) is not None:
        future.set_exception(error)
    else:
        future.set_result(task.result())


async def _awaited(awaitable: Awaitable[R]) -> R:
    return await awaitable


async def _carried(awaitable: Awaitable[R]) -> tuple[bool, Any]:
    """`(True, value)`, or `(False, error)` for any error but a cancel, for `Loop.run` to raise.

    asyncio lets a `KeyboardInterrupt` (and so `flnd.Stopped`) raised in a task out of the loop,
    which would end the loop's thread and leave its caller waiting forever; carried back as a
    value, it is raised on the caller's thread instead, where it belongs.

    Args:
        awaitable: what `Loop.run` was given.

    Returns:
        Whether it returned, and its value or its error.
    """
    try:
        return True, await awaitable
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        return False, error


def _serve_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


async def _cancel_pending() -> None:
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    loop = asyncio.get_running_loop()
    await loop.shutdown_asyncgens()
    await loop.shutdown_default_executor()
