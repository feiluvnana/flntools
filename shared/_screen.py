"""The terminal: where `log` and `prompt` write, and where keys are read from."""

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
        self.lock = threading.RLock()

    @property
    def dest(self):
        """The text stream output goes to: the one given, else `sys.stderr` as it is now."""
        return self._dest if self._dest is not None else sys.stderr

    @property
    def inp(self):
        if self._inp is None and not self._terminal_failed:
            try:
                self._inp = open("CONIN$" if os.name == "nt" else "/dev/tty", "rb", buffering=0)
            except OSError:
                self._terminal_failed = True
        return self._inp

    @property
    def tty(self) -> bool:
        return self._tty if self._tty is not None else isatty(self.dest)

    @property
    def interactive(self) -> bool:
        if self._interactive is not None:
            return self._interactive
        return self.tty and self.inp is not None

    @property
    def width(self) -> int:
        if self._width:
            return self._width
        try:
            return os.get_terminal_size(self.dest.fileno()).columns
        except (AttributeError, OSError, ValueError):
            return 80

    @property
    def height(self) -> int:
        if self._height:
            return self._height
        try:
            return os.get_terminal_size(self.dest.fileno()).lines
        except (AttributeError, OSError, ValueError):
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
            fd = self.inp.fileno() if self.inp is not None and os.name != "nt" else None
        except (AttributeError, OSError, ValueError):
            fd = None
        if fd is not None:
            import termios
            import tty

            try:
                saved = termios.tcgetattr(fd)
                tty.setraw(fd)
                self._raw = True
                _keep_output_processing(fd)
            except termios.error:
                saved = None
        self.write(PASTE_ON)
        try:
            yield
        finally:
            self.write(PASTE_OFF)
            if saved is not None and fd is not None:
                import termios

                self._raw = False
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def readkey(self) -> str:
        """Read one key name, such as "a", "enter", "up", or "ctrl+c"."""
        inp = self.inp
        if inp is None:
            raise EOFError("no terminal to read keys from")
        try:
            fd = inp.fileno()
        except (AttributeError, OSError, ValueError):
            fd = None
        if fd is None:
            return _readkey_from_stream(inp)
        return _readkey_windows() if os.name == "nt" else _readkey_posix(fd, self._raw)

    def write(self, text: str) -> None:
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
        atexit.register(_show_all_cursors)
    _cursor_hidden.add(screen)
    screen.write(HIDE_CURSOR)


def show_cursor(screen: Screen) -> None:
    """Show the cursor again, if `hide_cursor` hid it on this screen."""
    if screen in _cursor_hidden:
        _cursor_hidden.discard(screen)
        screen.write(SHOW_CURSOR)


def _show_all_cursors() -> None:
    for screen in list(_cursor_hidden):
        try:
            show_cursor(screen)
        except (OSError, ValueError):
            pass


def isatty(stream) -> bool:
    try:
        return bool(stream and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


KEY_NAMES = {
    "\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace", "\t": "tab",
    " ": "space", "\x1b": "escape", "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right",
    "\x1b[D": "left", "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
    "\x1b[H": "home", "\x1b[F": "end", "\x1b[3~": "delete",
}
WINDOWS_KEY_NAMES = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "S": "delete"}


def key_name(sequence: str) -> str:
    if sequence in KEY_NAMES:
        return KEY_NAMES[sequence]
    if len(sequence) == 1 and ord(sequence) < 32:
        return "ctrl+" + chr(ord(sequence) + 96)
    return sequence


def _readkey_from_stream(stream) -> str:
    char = stream.read(1)
    if not char:
        raise EOFError("no more keys")
    return key_name(char.decode() if isinstance(char, bytes) else char)


def _keep_output_processing(fd: int) -> None:
    """Undo what `tty.setraw` does to output: with OPOST off a newline no longer returns to column 0."""
    import termios

    attrs = termios.tcgetattr(fd)
    attrs[1] |= termios.OPOST | termios.ONLCR
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def _readkey_posix(fd: int, raw: bool = False) -> str:
    """Read one key; `raw` says the terminal is already raw (held by `Screen.reading`)."""
    import select
    import termios
    import tty

    saved = None if raw else termios.tcgetattr(fd)
    try:
        if saved is not None:
            tty.setraw(fd)
            _keep_output_processing(fd)
        data = os.read(fd, 1)
        if data == b"\x1b":
            # An escape sequence arrives in one burst; a lone escape key does not.
            while len(data) < 8 and select.select([fd], [], [], 0.03)[0]:
                data += os.read(fd, 1)
                if data[-1:].isalpha() or data.endswith(b"~"):
                    break
            if data == PASTE_START:
                return _read_paste(fd)
        elif data and data[0] >= 0xC0:
            following = 1 if data[0] < 0xE0 else 2 if data[0] < 0xF0 else 3
            data += os.read(fd, following)
    finally:
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return key_name(data.decode("utf-8", "replace"))


def _read_paste(fd: int) -> Paste:
    """Read a bracketed paste up to its end marker, as text without newlines or control characters."""
    data = b""
    # One byte at a time, so the keys typed right after the paste are left for the next read.
    while not data.endswith(PASTE_END):
        byte = os.read(fd, 1)
        if not byte:
            break
        data += byte
    return paste_text(data.removesuffix(PASTE_END).decode("utf-8", "replace"))


def paste_text(text: str) -> Paste:
    """A pasted text for a one-line question: newlines and other control characters dropped."""
    return Paste("".join(char for char in text if char.isprintable()))


def _readkey_windows() -> str:
    assert sys.platform == "win32"
    import msvcrt

    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):
        return WINDOWS_KEY_NAMES.get(msvcrt.getwch(), "unknown")
    return key_name(char)


def _enable_windows_vt() -> None:
    assert sys.platform == "win32"
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except (AttributeError, OSError):
        pass
