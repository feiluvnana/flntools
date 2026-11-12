"""The terminal: where `log` and `prompt` write, and where keys are read from.

`Screen` wraps an output stream (`dest`, stderr by default) and an input stream (`inp`, the
terminal itself by default). `write` drops colour codes when the theme has colour off, takes
`Screen.lock` so that output from different threads never interleaves, and flushes. `readkey`
returns a key name such as "enter" or "ctrl+c", reading with termios on POSIX and msvcrt on
Windows; `reading()` holds the terminal in raw mode while a prompt waits for keys. Cursor hiding
and the synchronized-output markers also live here, because `shared._log` and `shared._prompt`
both draw with them. The process theme is looked up from `shared._theme` at write time, since
that module imports this one.

Example:

    screen = Screen()                       # stderr for output, the terminal for keys
    screen.write("\\x1b[1mhello\\x1b[0m\\n")
    with screen.reading():
        key = screen.readkey()              # "a", "enter", "up", or a Paste for a pasted text
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
from contextlib import contextmanager
from typing import Iterator

from shared._style import PAINT

# Private: `theme.screen` is a Screen, and `log`, `prompt` and `cli` use the other functions.
__all__ = ["Paste", "Screen", "hide_cursor", "isatty", "key_name", "show_cursor"]


# Bracketed paste (DEC private mode 2004): the terminal wraps pasted text in the start and end
# markers below, so a paste can be told apart from the same keys typed one by one.
PASTE_ON, PASTE_OFF = "\x1b[?2004h", "\x1b[?2004l"
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"


class Paste(str):
    """A key that is really a pasted text: what `readkey` returns for a bracketed paste.

    Newlines are already removed from it, so a paste never submits a question.
    """


class Screen:
    """The terminal that `log` and `prompt` write to and read keys from.

        screen = Screen(dest=io.StringIO(), width=80, tty=False, interactive=False)  # a fake, for tests

    Args:
        dest: the text stream output goes to; None is `sys.stderr`, looked up at each write.
        inp: the binary stream keys are read from; None opens the terminal itself (`/dev/tty`, or
            `CONIN$` on Windows) on first use, so a piped stdin never answers a prompt.
        width: the width in columns; None asks the terminal, and 80 when it can't say.
        height: the height in lines; None asks the terminal, and 24 when it can't say.
        tty: whether `dest` is a terminal, so lines may be redrawn; None asks `dest`.
        interactive: whether a person can answer prompts; None is `tty` with a terminal to read.
    """

    def __init__(self, dest=None, inp=None, *, width: int | None = None, height: int | None = None,
                 tty: bool | None = None, interactive: bool | None = None):
        self._dest, self._inp, self._width, self._height = dest, inp, width, height
        self._tty, self._interactive = tty, interactive
        self._terminal_failed = False
        self._vt_enabled = False
        self._raw = False  # set while `reading()` holds the terminal in raw mode
        # Re-entrant: `shared._log` holds the lock across a whole frame and then writes inside it,
        # and every write takes the lock again. A plain Lock would deadlock on that second take.
        self.lock = threading.RLock()

    @property
    def dest(self):
        """The text stream output goes to: the one given, else `sys.stderr` as it is now.

        `sys.stderr` is looked up on each access, so a redirected stderr is followed.
        """
        return self._dest if self._dest is not None else sys.stderr

    @property
    def inp(self):
        """The binary stream keys are read from, or None when there is no terminal to read.

        The terminal is opened on first access. A failed open is remembered, so it is not retried
        on every key read.
        """
        if self._inp is None and not self._terminal_failed:
            try:
                # Unbuffered, so that `select` on the descriptor sees a key as soon as it arrives.
                # A buffered reader would pull the bytes into Python and hide them from `select`.
                self._inp = open("CONIN$" if os.name == "nt" else "/dev/tty", "rb", buffering=0)
            except OSError:
                self._terminal_failed = True
        return self._inp

    @property
    def tty(self) -> bool:
        """Whether output is going to a terminal, so that lines may be redrawn in place."""
        return self._tty if self._tty is not None else isatty(self.dest)

    @property
    def interactive(self) -> bool:
        """Whether a person can answer prompts on this screen.

        The first access opens the input terminal when no value was given, so it can be slower
        than the other properties.
        """
        if self._interactive is not None:
            return self._interactive
        return self.tty and self.inp is not None

    @property
    def width(self) -> int:
        """The width in columns: the one given, else the terminal's, else 80.

        A given 0 counts as not given.
        """
        if self._width:
            return self._width
        try:
            return os.get_terminal_size(self.dest.fileno()).columns
        except (AttributeError, OSError, ValueError):
            # No descriptor (a StringIO, say), or not a terminal: fall back to the classic 80.
            return 80

    @property
    def height(self) -> int:
        """The height in lines: the one given, else the terminal's, else 24.

        A given 0 counts as not given.
        """
        if self._height:
            return self._height
        try:
            return os.get_terminal_size(self.dest.fileno()).lines
        except (AttributeError, OSError, ValueError):
            # Same fallback as `width`, for the same reasons.
            return 24

    @contextmanager
    def reading(self) -> Iterator[None]:
        """Hold the terminal for reading keys: raw mode, and bracketed paste on, until the block ends.

        Raw mode is held across keys, so characters that arrive together (a paste, or fast typing)
        stay in the input queue instead of being dropped between two reads. Both are restored on
        any exit. On a screen that isn't a terminal this does nothing.
        """
        if not self.tty:
            yield
            return
        saved = None
        fd = None
        try:
            # Windows reads keys through msvcrt, not a descriptor, so there is no fd to set raw.
            fd = self.inp.fileno() if self.inp is not None and os.name != "nt" else None
        except (AttributeError, OSError, ValueError):
            # A stream without a real descriptor can't go raw; its keys are read as a plain stream.
            fd = None
        if fd is not None:
            import termios
            import tty

            try:
                # Raw mode: keys arrive one at a time, without waiting for Enter and without echo.
                saved = termios.tcgetattr(fd)
                tty.setraw(fd)
                self._raw = True
                _keep_output_processing(fd)
            except termios.error:
                # tcgetattr fails when the input is not a terminal; keys are then read unchanged.
                saved = None
        self.write(PASTE_ON)
        try:
            yield
        finally:
            self.write(PASTE_OFF)
            if saved is not None and fd is not None:
                import termios

                self._raw = False
                # TCSADRAIN waits for output already written to go out before the mode changes,
                # so restoring the terminal never cuts off the last frame.
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def readkey(self) -> str:
        """Read one key name, such as "a", "enter", "up", or "ctrl+c".

        Blocks until a key arrives. Raises EOFError when there is no terminal to read from, or when
        `inp` is a stream without a descriptor that has run out of characters.
        """
        inp = self.inp
        if inp is None:
            raise EOFError("no terminal to read keys from")
        try:
            fd = inp.fileno()
        except (AttributeError, OSError, ValueError):
            # Not a descriptor: read the keys from the stream itself (a test's StringIO, say).
            fd = None
        if fd is None:
            return _readkey_from_stream(inp)
        return _readkey_windows() if os.name == "nt" else _readkey_posix(fd, self._raw)

    def write(self, text: str) -> None:
        """Write `text` to `dest` under `lock` and flush it, dropping colour codes when colour is off.

        Safe to call from any thread, and from inside `with screen.lock:`. The first write on
        Windows also turns on virtual-terminal output, so ANSI escapes are understood there.
        """
        # theme imports this module, so the process theme is looked up at write time.
        from shared._theme import theme

        if theme.color == "none":
            text = PAINT.sub("", text)
        with self.lock:
            if os.name == "nt" and not self._vt_enabled:
                self._vt_enabled = True
                _enable_windows_vt()
            self.dest.write(text)
            self.dest.flush()


# Synchronized output (DEC private mode 2026, from the "synchronized updates" spec of kitty and
# iTerm2): the terminal holds what is written between the two and shows it as one frame. Terminals
# that don't know the mode ignore it.
SYNC_BEGIN, SYNC_END = "\x1b[?2026h", "\x1b[?2026l"
# DEC private mode 25 (DECTCEM): shows or hides the text cursor.
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"

# Screens whose cursor is hidden now; one `atexit` hook, registered with the first, shows them all.
_cursor_hidden: set[Screen] = set()
_exit_hook_registered = False


def hide_cursor(screen: Screen) -> None:
    """Hide the terminal's cursor until `show_cursor(screen)`, or until the process exits.

    Does nothing on a screen that isn't a terminal. A frame or live area that draws its own
    cursor calls this when it opens and `show_cursor` when it closes.
    """
    global _exit_hook_registered
    if not screen.tty or screen in _cursor_hidden:
        return
    if not _exit_hook_registered:
        _exit_hook_registered = True
        # A process that exits with the cursor hidden would leave the user's terminal without one,
        # so the hook shows every hidden cursor at exit.
        atexit.register(_show_all_cursors)
    _cursor_hidden.add(screen)
    screen.write(HIDE_CURSOR)


def show_cursor(screen: Screen) -> None:
    """Show the cursor again, if `hide_cursor` hid it on this screen."""
    if screen in _cursor_hidden:
        _cursor_hidden.discard(screen)
        screen.write(SHOW_CURSOR)


def _show_all_cursors() -> None:
    """Show the cursor on every screen that still has it hidden. Runs at exit."""
    for screen in list(_cursor_hidden):
        try:
            show_cursor(screen)
        except (OSError, ValueError):
            # At exit a stream may already be closed; skip it so the other screens still get theirs.
            pass


def isatty(stream) -> bool:
    """Whether `stream` is a terminal; False for None, and for any stream that cannot say.

    Args:
        stream: any file-like object, or None.
    """
    try:
        return bool(stream and stream.isatty())
    except (AttributeError, ValueError, OSError):
        # A closed stream raises ValueError and some wrappers lack isatty; neither is a terminal.
        return False


# Keys by the bytes a terminal sends for them. Each arrow key appears twice: "\x1b[" is the normal
# form, and "\x1bO" is the form the terminal sends in application cursor-key mode, which some
# programs switch on. Backspace appears as DEL ("\x7f") and as BS ("\x08"), because terminals
# differ on which one the Backspace key sends.
KEY_NAMES = {
    "\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace", "\t": "tab",
    " ": "space", "\x1b": "escape", "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right",
    "\x1b[D": "left", "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
    "\x1b[H": "home", "\x1b[F": "end", "\x1b[3~": "delete",
}
# On Windows, `msvcrt.getwch` sends an arrow or edit key as a \x00 or \xe0 prefix followed by one
# of these letters (the console's scan codes), so the letter alone names the key.
WINDOWS_KEY_NAMES = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "S": "delete"}


def key_name(sequence: str) -> str:
    """Name the key that `sequence` stands for, such as "up" for "\\x1b[A".

    Args:
        sequence: the characters one read produced: a single key, or a whole escape sequence.
            Unknown sequences are returned unchanged, so nothing typed is lost.

    Returns:
        The name in `KEY_NAMES`; a control character becomes "ctrl+" and its letter, so "\\x03"
        is "ctrl+c".
    """
    if sequence in KEY_NAMES:
        return KEY_NAMES[sequence]
    # Control characters are 1 to 31, and adding 96 turns 1 into "a", 2 into "b", and so on.
    if len(sequence) == 1 and ord(sequence) < 32:
        return "ctrl+" + chr(ord(sequence) + 96)
    return sequence


def _readkey_from_stream(stream) -> str:
    """Read one character from a stream that has no descriptor, and name it. Raises EOFError at its end."""
    char = stream.read(1)
    if not char:
        raise EOFError("no more keys")
    # A binary stream gives bytes; a text stream gives str. Both name the same way.
    return key_name(char.decode() if isinstance(char, bytes) else char)


def _keep_output_processing(fd: int) -> None:
    """Undo what `tty.setraw` does to output: with OPOST off a newline no longer returns to column 0."""
    import termios

    attrs = termios.tcgetattr(fd)
    attrs[1] |= termios.OPOST | termios.ONLCR
    # TCSANOW, not TCSADRAIN: this runs right after raw mode is set, and nothing is waiting to drain.
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def _readkey_posix(fd: int, raw: bool = False) -> str:
    """Read one key; `raw` says the terminal is already raw (held by `Screen.reading`)."""
    import select
    import termios
    import tty

    # Outside `reading()` each key switches to raw mode and back, so it is read without waiting
    # for Enter. Inside `reading()` the mode is already raw and is left alone.
    saved = None if raw else termios.tcgetattr(fd)
    try:
        if saved is not None:
            tty.setraw(fd)
            _keep_output_processing(fd)
        data = os.read(fd, 1)
        if data == b"\x1b":
            # An escape sequence arrives in one burst; a lone escape key does not.
            # The wait is 30 ms: long enough for the rest of a burst, short enough that Esc feels
            # immediate. The 8-byte cap bounds the read; key sequences are shorter than that.
            while len(data) < 8 and select.select([fd], [], [], 0.03)[0]:
                data += os.read(fd, 1)
                # A CSI sequence ends at its first letter or "~". This also stops after the "O" of
                # an SS3 arrow ("\x1bO" then a letter), so those arrows are not read whole yet.
                if data[-1:].isalpha() or data.endswith(b"~"):
                    break
            if data == PASTE_START:
                return _read_paste(fd)
        elif data and data[0] >= 0xC0:
            # A multi-byte UTF-8 character: the lead byte says how many continuation bytes follow.
            following = 1 if data[0] < 0xE0 else 2 if data[0] < 0xF0 else 3
            data += os.read(fd, following)
    finally:
        if saved is not None:
            # TCSADRAIN, as in `Screen.reading`: let pending output go before the mode changes.
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return key_name(data.decode("utf-8", "replace"))


def _read_paste(fd: int) -> Paste:
    """Read a bracketed paste up to its end marker, as text without newlines or control characters."""
    data = b""
    # One byte at a time, so the keys typed right after the paste are left for the next read.
    while not data.endswith(PASTE_END):
        byte = os.read(fd, 1)
        if not byte:
            # The input ended inside the paste; keep what arrived.
            break
        data += byte
    return paste_text(data.removesuffix(PASTE_END).decode("utf-8", "replace"))


def paste_text(text: str) -> Paste:
    """A pasted text for a one-line question: newlines and other control characters dropped.

    Args:
        text: the text the terminal pasted, without its bracketed-paste markers.

    Returns:
        A `Paste`, so a caller can tell it from a key that was typed.
    """
    return Paste("".join(char for char in text if char.isprintable()))


def _readkey_windows() -> str:
    """Read one key with msvcrt on Windows, where the POSIX terminal calls don't exist."""
    assert sys.platform == "win32"
    import msvcrt

    char = msvcrt.getwch()
    # A function or arrow key arrives as a \x00 or \xe0 prefix, then a second call gives the key.
    if char in ("\x00", "\xe0"):
        return WINDOWS_KEY_NAMES.get(msvcrt.getwch(), "unknown")
    return key_name(char)


def _enable_windows_vt() -> None:
    """Turn on ANSI escape handling for the Windows console, so colour and cursor codes work."""
    assert sys.platform == "win32"
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -12 is STD_ERROR_HANDLE, the default `dest`. A Screen on another stream is not switched here.
        handle = kernel32.GetStdHandle(-12)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # 0x0004 is ENABLE_VIRTUAL_TERMINAL_PROCESSING; the mode is kept and this bit added.
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except (AttributeError, OSError):
        # No ctypes console API (not really a console, or an old Windows): the escapes print as text.
        pass
