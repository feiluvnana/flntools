"""The terminal: where `log` and `prompt` write, and where keys are read from."""

from __future__ import annotations

import os
import sys
import threading

from shared._style import PAINT


class Screen:
    """The terminal that `log` and `prompt` write to and read keys from."""

    def __init__(self, out=None, inp=None, *, width: int | None = None, tty: bool | None = None,
                 interactive: bool | None = None):
        self._out, self._inp, self._width = out, inp, width
        self._tty, self._interactive = tty, interactive
        self._terminal_failed = False
        self._vt_enabled = False
        self.lock = threading.RLock()

    @property
    def out(self):
        return self._out if self._out is not None else sys.stderr

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
        return self._tty if self._tty is not None else isatty(self.out)

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
            return os.get_terminal_size(self.out.fileno()).columns
        except (AttributeError, OSError, ValueError):
            return 80

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
        return _readkey_windows() if os.name == "nt" else _readkey_posix(fd)

    def write(self, text: str) -> None:
        # theme imports this module, so the process theme is looked up at write time.
        from shared._theme import theme

        if theme.color == "none":
            text = PAINT.sub("", text)
        with self.lock:
            if os.name == "nt" and not self._vt_enabled:
                self._vt_enabled = True
                _enable_windows_vt()
            self.out.write(text)
            self.out.flush()


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


def _readkey_posix(fd: int) -> str:
    import select
    import termios
    import tty

    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        data = os.read(fd, 1)
        if data == b"\x1b":
            # An escape sequence arrives in one burst; a lone escape key does not.
            while len(data) < 8 and select.select([fd], [], [], 0.03)[0]:
                data += os.read(fd, 1)
                if data[-1:].isalpha() or data.endswith(b"~"):
                    break
        elif data and data[0] >= 0xC0:
            following = 1 if data[0] < 0xE0 else 2 if data[0] < 0xF0 else 3
            data += os.read(fd, following)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return key_name(data.decode("utf-8", "replace"))


def _readkey_windows() -> str:
    import msvcrt

    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):
        return WINDOWS_KEY_NAMES.get(msvcrt.getwch(), "unknown")
    return key_name(char)


def _enable_windows_vt() -> None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except (AttributeError, OSError):
        pass
