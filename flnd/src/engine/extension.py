"""The Chrome extension's side in flnd: its native-messaging host, the relay, the client, install.

src/docs/extension explains it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO

from flnd import errors
from flnd.errors import UsageError

# The host's name, as Chrome knows it from the host manifest `com.flnd.host.json`.
NAME = "com.flnd.host"
# The ops a run may send, as src/docs/extension lists them; `hello` comes first.
OPS = ("hello", "cookies", "tabs.open", "tabs.list", "tabs.close", "capture.start",
       "capture.read", "capture.stop", "document", "click", "scroll", "evaluate", "storage",
       "screenshot", "media")
# Chrome caps a message from a host at 1 MiB. A message over LIMIT bytes goes in parts of PART
# bytes each, whose base64 (4/3 larger) is LIMIT: under the cap with room for the wrapper.
LIMIT = 768 * 1024
PART = 576 * 1024
CAP = 1024 * 1024
# The largest frame read; Chrome sends a host at most 64 MiB in one message.
MOST = 64 * 1024 * 1024
# The largest frame read before `hello` has passed, so a stranger can't make the host read much,
# and the seconds a new run has to say it.
BEFORE_HELLO = 64 * 1024
HELLO = 5.0
# How long `install` waits for the extension's first `hello`, in seconds.
WAIT = 600.0
# The test suite's variable (`make test`, flnkit.cookies.NO_PROFILES): while set, the real
# folders are refused.
NO_PROFILES = "FLND_TEST_NO_PROFILES"
# The extension's files, shipped in the wheel as flnd/extension/.
SOURCE = Path(__file__).resolve().parent.parent / "extension"
FILES = ("manifest.json", "background.js", "page.js", "popup.html", "popup.js")
# Each Chromium browser's profile folder: (macOS, under Application Support; Linux, under
# ~/.config; Windows, its key under HKEY_CURRENT_USER).
BROWSERS = {
    "chrome": ("Google/Chrome", "google-chrome", r"Software\Google\Chrome"),
    "chromium": ("Chromium", "chromium", r"Software\Chromium"),
    "edge": ("Microsoft Edge", "microsoft-edge", r"Software\Microsoft\Edge"),
    "brave": ("BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser",
              r"Software\BraveSoftware\Brave-Browser"),
}
# The error classes an answer may name; any other name is raised as `flnd.Error`.
RAISED = ("UsageError", "LoginError", "NotFound", "Hidden", "Changed", "NetworkError", "Blocked")
WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------------------------

def folder() -> Path:
    """`~/.config/flnd/extension/`: the token, `host.json`, and the unpacked extension."""
    return Path.home() / ".config" / "flnd" / "extension"


def unpacked() -> Path:
    """The folder Load unpacked points at; the token stays outside it, so the extension's own
    origin can't fetch it."""
    return folder() / "unpacked"


def relay() -> str:
    """The relay's address: a socket in a 0700 folder, or a named pipe on Windows."""
    if WINDOWS:
        user = (os.environ.get("USERNAME") or "user").replace("\\", "-")
        return rf"\\.\pipe\flnd-host-{user}"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) / "flnd" if runtime else Path.home() / ".config" / "flnd" / "run"
    return str(base / "host.sock")


def browsers() -> list[Path]:
    """The profile folders of the Chromium browsers found on this computer (not Windows)."""
    if sys.platform == "darwin":
        root, column = Path.home() / "Library" / "Application Support", 0
    else:
        root, column = Path.home() / ".config", 1
    return [root / names[column] for names in BROWSERS.values() if (root / names[column]).is_dir()]


@lru_cache(maxsize=1)
def extension_id() -> str:
    """The extension's ID, made from the fixed `key` of its manifest as Chrome makes it."""
    key = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


def origin() -> str:
    """The one origin the host serves: `chrome-extension://<id>/`."""
    return f"chrome-extension://{extension_id()}/"


def patterns(domains: Sequence[str]) -> list[str]:
    """Chrome's match patterns for `domains` and their subdomains: `*://*.x.com/*`."""
    return [f"*://*.{domain}/*" for domain in dict.fromkeys(domains)]


def default_home() -> Path:
    """The home the popup acts on: `$FLND_HOME`, else `.flnd` in the current folder."""
    chosen = os.environ.get("FLND_HOME")
    return (Path(chosen) if chosen else Path.cwd() / ".flnd").absolute()


# ---------------------------------------------------------------------------------------------
# The guard: tests never touch the real folders
# ---------------------------------------------------------------------------------------------

def _real_home() -> Path:
    """The account's own home, which a test's `$HOME` does not move."""
    if WINDOWS:
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def guard(path: Path | str) -> None:
    """Refuse `path` while the test suite runs, unless it is inside the temp folder.

    Every write, socket and token read of this module goes through here first, so a test can't
    reach the real `~/.config/flnd/`, Chrome's folders or the real relay.

    Raises:
        RuntimeError: `FLND_TEST_NO_PROFILES` is set and `path` is outside the temp folder, in
            the real home's `.config` or `Library/Application Support`, or the real pipe.
    """
    if not os.environ.get(NO_PROFILES):
        return
    if WINDOWS and str(path).startswith("\\\\.\\pipe\\"):
        if str(path).startswith("\\\\.\\pipe\\flnd-host-"):
            raise RuntimeError(f"{NO_PROFILES} is set: a test may not use the real relay {path}")
        return
    resolved, real = Path(path).resolve(), _real_home().resolve()
    kept = [real / ".config", real / "Library" / "Application Support"]
    inside = resolved.is_relative_to(Path(tempfile.gettempdir()).resolve())
    if not inside or any(resolved.is_relative_to(place) for place in kept):
        raise RuntimeError(f"{NO_PROFILES} is set: a test may not touch {path}; give it a "
                           "temporary HOME instead")


def _mkdir(path: Path, *, private: bool = True) -> None:
    """Make `path` and its parents; a private one is the user's own, mode 0700.

    Its owner is checked before the mode is set, so a symlink there never changes another
    folder's mode.
    """
    guard(path)
    path.mkdir(parents=True, exist_ok=True)
    if private and not WINDOWS:
        _owned(path, mode=False)
        os.chmod(path, 0o700)
        _owned(path)


def _put(path: Path, data: bytes, mode: int) -> None:
    """Write `path` with `mode`, set before the first byte, never through a symlink.

    Raises:
        UsageError: `path` is a symlink.
    """
    guard(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | getattr(os, "O_BINARY", 0), mode)
    except OSError as error:
        if error.errno != errno.ELOOP:
            raise
        raise _linked(path) from None
    if not WINDOWS:
        os.fchmod(fd, mode)
    with os.fdopen(fd, "wb") as file:
        file.write(data)


def _remove(path: Path) -> None:
    guard(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _owned(path: Path, *, mode: bool = True) -> None:
    """Raise unless `path` is a real folder of this user (and, with `mode`, no one else's to
    open)."""
    found = os.lstat(path)
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid() \
            or (mode and found.st_mode & 0o077):
        raise UsageError(f"{path} isn't a private folder of yours",
                         hint="remove it; flnd makes it again, mode 0700")


def _linked(path: Path) -> UsageError:
    return UsageError(f"{path} is a symlink, which flnd won't follow",
                      hint="remove it, then run flnd extension install")


# ---------------------------------------------------------------------------------------------
# Messages: a 4-byte length in native byte order, as Chrome writes it, then JSON
# ---------------------------------------------------------------------------------------------

def encode(message: Mapping[str, Any]) -> bytes:
    """One message as JSON bytes, without its length."""
    return json.dumps(message, separators=(",", ":")).encode()


def frame(message: Mapping[str, Any]) -> bytes:
    """One message with its length in front."""
    data = encode(message)
    return struct.pack("=I", len(data)) + data


def read(stream: Any, most: int = MOST) -> dict[str, Any] | None:
    """Read one frame from `stream` (anything with `read(n)`); None at its end or past `most`.

    Raises:
        ValueError: the frame isn't a JSON object.
    """
    head = _exact(stream, 4)
    if head is None:
        return None
    (size,) = struct.unpack("=I", head)
    body = _exact(stream, size) if size <= most else None
    if body is None:
        return None
    message = json.loads(body)
    if not isinstance(message, dict):
        raise ValueError("a frame that isn't a JSON object")
    return message


def _exact(stream: Any, size: int) -> bytes | None:
    pieces, left = [], size
    while left:
        piece = stream.read(left)
        if not piece:
            return None
        pieces.append(piece)
        left -= len(piece)
    return b"".join(pieces)


def parts(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """`message` as it is when it fits in LIMIT bytes, else its JSON in base64 parts."""
    data = encode(message)
    if len(data) <= LIMIT:
        return [dict(message)]
    pieces = [data[start:start + PART] for start in range(0, len(data), PART)]
    return [{"id": message["id"], "part": index, "of": len(pieces),
             "chunk": base64.b64encode(piece).decode()} for index, piece in enumerate(pieces)]


class Joiner:
    """Puts the parts of messages back together, by their id."""

    def __init__(self) -> None:
        self._parts: dict[Any, list[bytes | None]] = {}

    def add(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """The whole message once its last part came; None while parts are missing.

        Raises:
            ValueError: a part is malformed.
        """
        if "part" not in message:
            return message
        ident, index, count = message.get("id"), message.get("part"), message.get("of")
        chunk = message.get("chunk")
        if not (isinstance(ident, int) and isinstance(index, int) and isinstance(count, int)
                and isinstance(chunk, str) and 0 <= index < count <= MOST // PART + 1):
            raise ValueError("a malformed part")
        held = self._parts.setdefault(ident, [None] * count)
        try:
            if len(held) != count:
                raise ValueError("a part of another message")
            held[index] = base64.b64decode(chunk, validate=True)
            if any(piece is None for piece in held):
                return None
            whole = json.loads(b"".join(held))  # type: ignore[arg-type]
        except (ValueError, RecursionError):
            self._parts.pop(ident, None)
            raise ValueError("a malformed part") from None
        del self._parts[ident]
        if not isinstance(whole, dict):
            raise ValueError("a message that isn't an object")
        return whole


def _refusal(ident: Any, message: str, *, cls: str = "UsageError",
             hint: str = "flnd extension install") -> dict[str, Any]:
    return {"id": ident, "ok": False, "error": {"class": cls, "message": message, "hint": hint}}


def _raised(error: Any) -> errors.Error:
    """The flnd error an error answer names."""
    error = error if isinstance(error, dict) else {}
    name = error.get("class")
    cls = getattr(errors, name) if name in RAISED else errors.Error
    return cls(str(error.get("message") or "the extension failed"), hint=str(error.get("hint", "")))


def _offline() -> UsageError:
    return UsageError("the Chrome extension isn't connected", hint="flnd extension install")


# ---------------------------------------------------------------------------------------------
# The relay, on the host's side: a Unix socket, or a named pipe on Windows
# ---------------------------------------------------------------------------------------------

class _Conn:
    """One run's connection to the host, as frames; `send` may be called from any thread.

    Args:
        stream: has `read(n)`, returning b"" at the end.
        write: sends bytes, whole.
        close: closes the connection, waking a `read` blocked in another thread.
    """

    def __init__(self, stream: Any, write: Callable[[bytes], Any],
                 close: Callable[[], None]) -> None:
        self._stream, self._write, self._close = stream, write, close
        self._lock = threading.Lock()
        # Its own lock, so a close never waits behind a send that is blocked.
        self._once = threading.Lock()
        self._closed = False

    def send(self, message: Mapping[str, Any]) -> None:
        with self._lock:
            self._write(frame(message))

    def receive(self, most: int = MOST) -> dict[str, Any] | None:
        try:
            return read(self._stream, most)
        except (OSError, ValueError, RecursionError):
            return None

    def close(self) -> None:
        with self._once:
            if self._closed:
                return
            self._closed = True
        with contextlib.suppress(OSError):
            self._close()


def _socket_conn(sock: socket.socket) -> _Conn:
    stream = sock.makefile("rb", buffering=0)

    def close() -> None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        # The stream holds the socket open until it is closed too.
        stream.close()
        sock.close()

    return _Conn(stream, sock.sendall, close)


class _SocketListener:
    """The relay on Unix: a 0600 socket in a 0700 folder."""

    def __init__(self, path: str) -> None:
        guard(path)
        where = Path(path)
        _mkdir(where.parent)
        if where.exists() or where.is_symlink():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(path)
            except OSError:
                where.unlink()  # a socket a host that ended left behind
            else:
                raise UsageError("another flnd host is already serving a browser",
                                 hint="close the other browser, then reload the extension")
            finally:
                probe.close()
        self.path, self.closed = path, False
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        os.chmod(path, 0o600)
        self._inode = os.stat(path).st_ino
        self._sock.listen(128)

    def accept(self) -> _Conn | None:
        try:
            sock, _ = self._sock.accept()
        except OSError:
            return None
        if self.closed:
            sock.close()
            return None
        return _socket_conn(sock)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # A close alone doesn't wake an accept() blocked in another thread; a connection does.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
            with contextlib.suppress(OSError):
                wake.connect(self.path)
        self._sock.close()
        with contextlib.suppress(OSError):
            if os.stat(self.path).st_ino == self._inode:
                os.unlink(self.path)


class _PipeListener:
    """The relay on Windows: a byte-mode named pipe whose DACL lets in only the current user."""

    def __init__(self, name: str) -> None:
        guard(name)
        self.path, self.closed = name, False
        self._security = _Security()
        self._next = self._instance(first=True)

    def _instance(self, *, first: bool) -> int:
        import _winapi

        mode = _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED
        if first:
            mode |= _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE
        kind = _winapi.PIPE_WAIT | 0x8  # a byte pipe; 0x8 is PIPE_REJECT_REMOTE_CLIENTS
        return _winapi.CreateNamedPipe(self.path, mode, kind, _winapi.PIPE_UNLIMITED_INSTANCES,
                                       CAP, CAP, _winapi.NMPWAIT_WAIT_FOREVER,
                                       self._security.address)

    def accept(self) -> _Conn | None:
        import _winapi

        handle = self._next
        try:
            waiting = _winapi.ConnectNamedPipe(handle, overlapped=True)
            _winapi.WaitForMultipleObjects([waiting.event], False, _winapi.INFINITE)
            waiting.GetOverlappedResult(True)
        except OSError:
            _winapi.CloseHandle(handle)
            return None
        if self.closed:
            _winapi.CloseHandle(handle)
            return None
        self._next = self._instance(first=False)
        pipe = _Handle(handle)
        return _Conn(pipe, pipe.write, pipe.close)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        import _winapi

        # Connecting wakes the accept() that waits; its handle is then closed there.
        with contextlib.suppress(OSError):
            _winapi.CloseHandle(_winapi.CreateFile(self.path, _winapi.GENERIC_READ, 0, 0,
                                                   _winapi.OPEN_EXISTING, 0, 0))


class _Handle:
    """A connected pipe instance, read and written as a byte stream (overlapped, so closing
    it from another thread ends a read)."""

    def __init__(self, handle: int) -> None:
        self._handle = handle

    def read(self, size: int) -> bytes:
        import _winapi

        try:
            waiting, _ = _winapi.ReadFile(self._handle, size, overlapped=True)
            done, _ = waiting.GetOverlappedResult(True)
        except OSError:
            return b""
        return bytes(waiting.getbuffer())[:done]

    def write(self, data: bytes) -> None:
        import _winapi

        waiting, _ = _winapi.WriteFile(self._handle, data, overlapped=True)
        waiting.GetOverlappedResult(True)

    def close(self) -> None:
        import _winapi

        _winapi.CloseHandle(self._handle)


def _windows() -> Any:
    """ctypes' advapi32 and kernel32 with the signatures used here, and a checker (Windows)."""
    import ctypes
    from ctypes import POINTER, c_int, c_void_p, wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, c_int, c_void_p, wintypes.DWORD,
                                           POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [c_void_p, POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, POINTER(c_void_p), c_void_p]
    advapi.GetSecurityInfo.argtypes = [wintypes.HANDLE, c_int, wintypes.DWORD, POINTER(c_void_p),
                                       c_void_p, c_void_p, c_void_p, POINTER(c_void_p)]

    def check(done: Any) -> None:
        if not done:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]

    return ctypes, advapi, kernel, check


def _text(sid: Any) -> str:
    """A SID as text (`S-1-5-21-…`) (Windows)."""
    ctypes, advapi, kernel, check = _windows()
    text = ctypes.wintypes.LPWSTR()
    check(advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)))
    found = text.value or ""
    kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    return found


def _sid() -> str:
    """The current user's SID, as text (Windows)."""
    ctypes, advapi, kernel, check = _windows()
    token, size = ctypes.wintypes.HANDLE(), ctypes.wintypes.DWORD()
    check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)))  # QUERY
    try:
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # 1 is TokenUser
        buffer = ctypes.create_string_buffer(size.value)
        check(advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)))
    finally:
        kernel.CloseHandle(token)
    # TOKEN_USER starts with SID_AND_ATTRIBUTES, which starts with the SID's pointer.
    return _text(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])


def _owner(handle: int) -> str:
    """The owner SID of a kernel object, such as the pipe a client connected to (Windows)."""
    ctypes, advapi, kernel, _ = _windows()
    owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    # 6 is SE_KERNEL_OBJECT, 1 is OWNER_SECURITY_INFORMATION.
    failed = advapi.GetSecurityInfo(handle, 6, 1, ctypes.byref(owner), None, None, None,
                                    ctypes.byref(descriptor))
    if failed:
        raise ctypes.WinError(failed)  # type: ignore[attr-defined]
    try:
        return _text(owner)
    finally:
        kernel.LocalFree(descriptor)


class _Security:
    """A SECURITY_ATTRIBUTES owned by the current user, whose DACL lets in only that user's SID,
    kept alive (Windows)."""

    def __init__(self) -> None:
        ctypes, advapi, _, check = _windows()
        wintypes = ctypes.wintypes

        class Attributes(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p),
                        ("inherit", wintypes.BOOL)]

        self.sid = _sid()
        # O: sets the owner, so a client can check it; D:P is a protected DACL, and
        # (A;;GA;;;<sid>) gives that SID, and no one else, every right.
        self._descriptor = ctypes.c_void_p()
        check(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"O:{self.sid}D:P(A;;GA;;;{self.sid})", 1, ctypes.byref(self._descriptor), None))
        self._attributes = Attributes(ctypes.sizeof(Attributes), self._descriptor, False)
        self.address = ctypes.addressof(self._attributes)


def _listen(path: str) -> Any:
    return _PipeListener(path) if WINDOWS else _SocketListener(path)


# ---------------------------------------------------------------------------------------------
# The host
# ---------------------------------------------------------------------------------------------

class _Peer:
    """A run connected to the relay, and the captures it has open."""

    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.captures: set[Any] = set()


class Host:
    """The native-messaging host: the extension on one side, the runs on the relay on the other.

    Each run's ops get the host's own ids, so several runs share the extension; each answer,
    its parts put back together, goes back whole to the run that asked, under its id.
    """

    def __init__(self, incoming: BinaryIO, outgoing: BinaryIO, *, token: str, path: str,
                 popup: Callable[[str, Mapping[str, Any]], Any] | None = None) -> None:
        """Args:
            incoming: the extension's messages (Chrome's pipe on standard input).
            outgoing: where the host writes to the extension (standard output).
            token: the shared secret a run's `hello` must carry.
            path: the relay's address (`relay()`).
            popup: answers the popup's questions (`Popup`); None refuses them.
        """
        self._in, self._out, self._token, self.path = incoming, outgoing, token, path
        self._popup = popup
        self._write = threading.Lock()
        self._lock = threading.Lock()
        self._routes: dict[int, tuple[_Peer | None, Any, str]] = {}
        self._peers: dict[_Peer, threading.Thread] = {}
        self._next = 0
        self._listener: Any = None
        self._joiner = Joiner()
        # Set once the relay listens, so a run may connect.
        self.ready = threading.Event()

    def serve(self) -> None:
        """Relay until the extension closes its end; every thread has stopped on return."""
        self._listener = _listen(self.path)
        self.ready.set()
        accept = threading.Thread(target=self._accept, name="flnd-host-accept", daemon=True)
        accept.start()
        try:
            while (message := self._read()) is not None:
                if "op" in message:
                    self._ask(message)
                else:
                    self._answer(message)
        finally:
            self._listener.close()
            accept.join(5)
            with self._lock:
                peers = list(self._peers.items())
            for peer, thread in peers:
                peer.conn.close()
                thread.join(5)

    def _read(self) -> dict[str, Any] | None:
        """The extension's next whole message, its parts put together: a question or an answer.

        A malformed one is dropped, with a line in the journal; None at the end of the pipe.
        """
        while True:
            try:
                message = read(self._in)
            except OSError:
                return None
            except (ValueError, RecursionError):
                _dropped("a message that isn't a JSON object")
                continue
            if message is None:
                return None
            try:
                whole = self._joiner.add(message)
            except ValueError as error:
                _dropped(str(error))
                continue
            if whole is not None and not isinstance(whole.get("id"), int):
                _dropped("a message without a whole-number id")
            elif whole is not None:
                return whole

    def _send(self, message: Mapping[str, Any]) -> None:
        """Write one message to the extension, in parts when it is over LIMIT bytes."""
        with self._write:
            for piece in parts(message):
                self._out.write(frame(piece))
            self._out.flush()

    def _accept(self) -> None:
        while (conn := self._listener.accept()) is not None:
            peer = _Peer(conn)
            thread = threading.Thread(target=self._serve, args=(peer,), name="flnd-host-run",
                                      daemon=True)
            with self._lock:
                self._peers[peer] = thread
            thread.start()

    def _serve(self, peer: _Peer) -> None:
        # A run that doesn't say hello in time is closed, which ends the read below.
        late = threading.Timer(HELLO, peer.conn.close)
        late.daemon = True
        late.start()
        try:
            first = peer.conn.receive(BEFORE_HELLO)
            late.cancel()
            if not self._hello(peer, first):
                return
            while (message := peer.conn.receive()) is not None:
                self._forward(peer, message)
        except OSError:
            pass  # the run went away while being answered
        finally:
            late.cancel()
            self._drop(peer)

    def _hello(self, peer: _Peer, message: dict[str, Any] | None) -> bool:
        """Check a run's first message: `hello` with the shared secret, or the run is refused."""
        message = message if isinstance(message, dict) else {}
        args = message.get("args")
        given = args.get("token") if isinstance(args, dict) else None
        if message.get("op") != "hello":
            refused = "the host refused this run: hello must come first"
        elif not isinstance(given, str) or not hmac.compare_digest(given.encode(),
                                                                   self._token.encode()):
            refused = "the host refused this run: its token is wrong"
        else:
            self._forward(peer, {**message, "args": {}})  # the token stays in the host
            return True
        with contextlib.suppress(OSError):
            peer.conn.send(_refusal(message.get("id"), refused))
        return False

    def _forward(self, peer: _Peer, message: dict[str, Any]) -> None:
        """Send a run's op to the extension, under a new id."""
        ident, op, args = message.get("id"), message.get("op"), message.get("args", {})
        if op not in OPS or not isinstance(args, dict):
            peer.conn.send(_refusal(ident, f"the extension has no op {op!r}", hint=""))
            return
        if op.startswith("capture.") and op != "capture.start" \
                and not isinstance(args.get("capture"), int):
            peer.conn.send(_refusal(ident, f"{op} needs the number capture.start gave", hint=""))
            return
        if op == "capture.stop":
            with self._lock:
                peer.captures.discard(args.get("capture"))
        out = {"id": 0, "op": op, "args": args}
        if len(encode(out)) > CAP - 64:
            peer.conn.send(_refusal(ident, f"{op} is too large for Chrome", hint=""))
            return
        with self._lock:
            self._next += 1
            out["id"] = self._next
            self._routes[self._next] = (peer, ident, op)
        try:
            self._send(out)
        except (OSError, ValueError):
            with self._lock:
                self._routes.pop(out["id"], None)
            peer.conn.send(_refusal(ident, "the Chrome extension went away"))

    def _answer(self, message: dict[str, Any]) -> None:
        """Pass an answer back to the run that asked."""
        with self._lock:
            route = self._routes.pop(message.get("id"), None)
            if route is None or route[0] is None:
                return
            peer, theirs, op = route
            value = message.get("value") if message.get("ok") else None
            if op == "capture.start" and isinstance(value, dict) \
                    and isinstance(value.get("capture"), int):
                peer.captures.add(value["capture"])
        try:
            peer.conn.send({**message, "id": theirs})
        except OSError:
            peer.conn.close()

    def _drop(self, peer: _Peer) -> None:
        """Forget a run that left, and stop the captures it left open."""
        with self._lock:
            self._peers.pop(peer, None)
            for ident in [ident for ident, route in self._routes.items() if route[0] is peer]:
                self._routes[ident] = (None, None, "")
            left = list(peer.captures)
            peer.captures.clear()
        for capture in left:
            with self._lock:
                self._next += 1
                mine = self._next
                self._routes[mine] = (None, None, "capture.stop")
            try:
                self._send({"id": mine, "op": "capture.stop", "args": {"capture": capture}})
            except (OSError, ValueError):
                break
        peer.conn.close()

    def _ask(self, message: dict[str, Any]) -> None:
        """Answer one of the popup's questions."""
        ident, op = message.get("id"), str(message.get("op"))
        args = message.get("args") if isinstance(message.get("args"), dict) else {}
        if self._popup is None:
            answer = _refusal(ident, "flnd has no home for the popup to use")
        else:
            try:
                answer = {"id": ident, "ok": True, "value": self._popup(op, args or {})}
            except errors.Error as error:
                answer = _refusal(ident, str(error), cls=type(error).__name__, hint=error.hint)
            except Exception as error:
                answer = _refusal(ident, f"flnd failed: {error}", cls="Error", hint="")
        with contextlib.suppress(OSError, ValueError):
            self._send(answer)


def _dropped(why: str) -> None:
    """Note in the journal a message from the extension that the host dropped."""
    from flnd.ui.log import ui

    ui.debug(f"the host dropped {why} from the extension")


class Popup:
    """The popup's questions about a tab, answered for the home recorded at install."""

    def __init__(self, home: Path) -> None:
        self.home = home

    def __call__(self, op: str, args: Mapping[str, Any]) -> Any:
        """`resolve {url}`, `get {target}` or `follow {target}`.

        Raises:
            UsageError: an unknown question, or a target no plugin reads.
        """
        if op == "resolve":
            return self.resolve(str(args.get("url") or ""))
        if op in ("get", "follow"):
            return self.start(op, str(args.get("target") or ""))
        raise UsageError(f"the popup has no question {op!r}")

    def resolve(self, url: str) -> dict[str, Any]:
        """The target a link is, and whether this home follows it."""
        from flnd.engine import home
        from flnd.engine.core import Engine

        target = Engine(home=self.home).resolve([url])[0]
        with home.open(self.home) as db:
            row = db.target(target)
        return {"target": str(target), "followed": bool(row and row.follow)}

    def start(self, op: str, target: str) -> dict[str, Any]:
        """Start `flnd download` or `flnd follow` on `target`, in a process of its own.

        The target is resolved first, so one no plugin reads fails here, in the popup.
        """
        if not target:
            raise UsageError("the popup sent no target")
        resolved = self.resolve(target)["target"]
        # The popup's Get is `flnd download` on today's command line.
        command = {"get": "download", "follow": "follow"}[op]
        argv = [*_flnd(), "--home", str(self.home), command, "--", resolved]
        detach: dict[str, Any] = ({"creationflags": subprocess.DETACHED_PROCESS} if WINDOWS
                                  else {"start_new_session": True})
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **detach)
        return {"started": resolved}


def _flnd() -> list[str]:
    """The command that runs flnd: the bundle itself, or this Python with `-m flnd`."""
    return [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "flnd"]


def host(caller: str | None) -> int:
    """Serve the extension on standard input and output: Chrome starts this, as `flnd host`.

    Args:
        caller: the origin Chrome names as the host's first argument.

    Raises:
        UsageError: Chrome named another extension, or install hasn't written the token.
    """
    if caller != origin():
        raise UsageError(f"the host serves only flnd's extension, not {caller or 'none'}",
                         hint="Chrome starts it; flnd extension install sets it up")
    secret = token()
    try:
        recorded = json.loads((folder() / "host.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        recorded = {}
    home = recorded.get("home") if isinstance(recorded, dict) else None
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    if WINDOWS:
        import msvcrt

        for stream in (incoming, outgoing):
            msvcrt.setmode(stream.fileno(), os.O_BINARY)  # type: ignore[attr-defined]
    # Standard output is the extension's channel: anything else printed goes to standard error.
    sys.stdout = sys.stderr
    try:
        Host(incoming, outgoing, token=secret, path=relay(),
             popup=Popup(Path(home)) if home else None).serve()
    finally:
        sys.stdout = sys.__stdout__
    return 0


# ---------------------------------------------------------------------------------------------
# The client a run uses, on the run's loop
# ---------------------------------------------------------------------------------------------

class Client:
    """A run's connection to the host: ops from any task of the loop, answers routed by one
    reader task. Made by `connect`; a resource (`async with`)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 timeout: float = 120.0) -> None:
        self._reader, self._writer, self.timeout = reader, writer, timeout
        self.version: str | None = None
        self._waiting: dict[int, asyncio.Future[dict[str, Any] | None]] = {}
        self._next = 0
        self._task = asyncio.get_running_loop().create_task(self._read())

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def call(self, op: str, **args: Any) -> Any:
        """Run one op of the extension and return its value.

        Raises:
            UsageError: the extension isn't connected, or refused the op.
            NetworkError: no answer in `timeout` seconds.
            Error: the op failed; the class is the one the extension named.
        """
        if self._task.done():
            raise _offline()
        self._next += 1
        ident = self._next
        waiting = self._waiting[ident] = asyncio.get_running_loop().create_future()
        try:
            self._writer.write(frame({"id": ident, "op": op, "args": args}))
            await self._writer.drain()
            answer = await asyncio.wait_for(waiting, self.timeout)
        except OSError as error:
            raise _offline() from error
        except TimeoutError:
            raise errors.NetworkError(f"the extension didn't answer {op} in {self.timeout:g} s",
                                      retryable=True) from None
        finally:
            self._waiting.pop(ident, None)
        if answer is None:
            raise _offline()
        if answer.get("ok"):
            return answer.get("value")
        raise _raised(answer.get("error"))

    async def close(self) -> None:
        """Leave the relay; the reader task has ended on return."""
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _read(self) -> None:
        try:
            while True:
                (size,) = struct.unpack("=I", await self._reader.readexactly(4))
                message = json.loads(await self._reader.readexactly(size))
                ident = message.get("id") if isinstance(message, dict) else None
                waiting = self._waiting.get(ident)  # type: ignore[arg-type]
                if waiting is not None and not waiting.done():
                    waiting.set_result(message)
        except (asyncio.IncompleteReadError, OSError, ValueError):
            pass
        finally:
            for waiting in self._waiting.values():
                if not waiting.done():
                    waiting.set_result(None)


def token() -> str:
    """The shared secret from `token.json`, which must be the user's own, mode 0600.

    Raises:
        UsageError: the extension isn't installed, the file is a symlink, or it may be read
            by others.
    """
    path = folder() / "token.json"
    guard(path)
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise _linked(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _linked(path) from None
        raise UsageError("flnd's Chrome extension isn't installed",
                         hint="flnd extension install") from None
    with os.fdopen(fd, "rb") as file:
        found, data = os.fstat(file.fileno()), file.read()
    try:
        value = json.loads(data).get("token")
    except (ValueError, AttributeError):
        value = None
    if not WINDOWS and (found.st_mode & 0o077 or found.st_uid != os.getuid()):
        raise UsageError(f"{path} may be read by others", hint="flnd extension install")
    if not isinstance(value, str) or len(value) < 32:
        raise UsageError(f"{path} holds no token", hint="flnd extension install")
    return value


async def connect() -> Client:
    """Connect a run to the host and say `hello`; the client's `version` is the extension's.

    Raises:
        UsageError: the extension isn't installed or connected, or the host refused the token.
    """
    secret = token()
    reader, writer = await _dial(relay())
    client = Client(reader, writer)
    try:
        client.version = (await client.call("hello", token=secret) or {}).get("version")
    except BaseException:
        await client.close()
        raise
    return client


async def _dial(path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the relay at `path`, after checking that it is the user's own.

    Raises:
        UsageError: nothing listens there, or it isn't the user's.
    """
    guard(path)
    if not WINDOWS:
        try:
            _owned(Path(path).parent)
            return await asyncio.open_unix_connection(path)
        except (OSError, UsageError) as error:
            raise _offline() from error
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        transport, _ = await loop.create_pipe_connection(  # type: ignore[attr-defined]
            lambda: protocol, path)
    except OSError as error:
        raise _offline() from error
    # Another user may have made a pipe of this name first: the token goes only to ours.
    pipe = transport.get_extra_info("pipe")
    if _owner(getattr(pipe, "handle", pipe)) != _sid():
        transport.close()
        raise UsageError(f"the relay {path} isn't yours", hint="close the program that made it")
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


# ---------------------------------------------------------------------------------------------
# Install and uninstall
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Setup:
    """What `install` did: the folder Load unpacked points at, the host manifests, and whether
    the extension said hello."""

    folder: Path
    manifests: tuple[Path, ...]
    connected: bool


def install(*, home: Path | None = None, domains: Sequence[str] = (),
            profiles: Sequence[Path] | None = None, wait: float = WAIT) -> Setup:
    """Copy the extension, make its token, write the host manifests, and wait for `hello`.

    The extension goes to `unpacked()`; the token and `host.json` stay in `folder()`, outside
    it. A token already there is kept, so a host Chrome runs keeps accepting runs.

    Args:
        home: the home the popup acts on; None for `default_home()`.
        domains: the plugins' domains, which the extension may ask for (optional permissions).
        profiles: the browsers' profile folders; None for every one `browsers()` finds.
        wait: seconds to wait for the extension's first `hello`; 0 tries once.
    """
    where = folder()
    found = browsers() if profiles is None else list(profiles)
    manifests = [browser / "NativeMessagingHosts" / f"{NAME}.json" for browser in found]
    for path in (where, *manifests):
        guard(path)
    if WINDOWS:
        _registry(None)  # refused under the test suite before anything is written
    _mkdir(where)
    _mkdir(unpacked())
    for name in FILES:
        data = (SOURCE / name).read_bytes()
        if name == "manifest.json" and domains:
            manifest = json.loads(data)
            manifest["optional_host_permissions"] = sorted(
                {*manifest.get("optional_host_permissions", ()), *patterns(domains)})
            data = (json.dumps(manifest, indent=2) + "\n").encode()
        _put(unpacked() / name, data, 0o644)
    try:
        secret = token()
    except UsageError:
        secret = secrets.token_hex(32)
    _put(where / "token.json", encode({"token": secret}), 0o600)
    _put(where / "host.json", encode({"home": str(home or default_home()), "id": extension_id()}),
         0o600)
    described = encode({"name": NAME, "description": "flnd's host for its Chrome extension",
                        "path": str(_launcher(where)), "type": "stdio",
                        "allowed_origins": [origin()]})
    if WINDOWS:
        manifests = [where / f"{NAME}.json"]
        _put(manifests[0], described, 0o644)
        _registry(manifests[0])
    for path in [] if WINDOWS else manifests:
        _mkdir(path.parent, private=False)
        _put(path, described, 0o644)
    _steps(unpacked())
    return Setup(unpacked(), tuple(manifests), _wait(wait))


def uninstall(*, profiles: Sequence[Path] | None = None) -> list[Path]:
    """Remove the extension's folder (the unpacked extension, the token and `host.json`) and
    the host manifests; return what went.

    Args:
        profiles: the browsers' profile folders; None for every one `browsers()` finds.
    """
    where = folder()
    found = browsers() if profiles is None else list(profiles)
    manifests = [browser / "NativeMessagingHosts" / f"{NAME}.json" for browser in found]
    for path in (where, *manifests):
        guard(path)
    removed = [path for path in manifests if path.exists()]
    for path in removed:
        _remove(path)
    if WINDOWS:
        _registry(None, remove=True)
    if where.exists():
        _remove(where)
        removed.append(where)
    return removed


def _launcher(where: Path) -> Path:
    """The program Chrome starts: a script that runs `flnd host` with this flnd."""
    import shlex

    command = _flnd()
    if WINDOWS:
        path = where / "flnd-host.bat"
        line = " ".join(f'"{part}"' for part in command)
        _put(path, f"@echo off\r\n{line} host %*\r\n".encode(), 0o700)
    else:
        path = where / "flnd-host"
        line = " ".join(shlex.quote(part) for part in command)
        _put(path, f'#!/bin/sh\nexec {line} host "$@"\n'.encode(), 0o700)
    return path


def _registry(manifest: Path | None, *, remove: bool = False) -> None:
    """On Windows, point each browser found at `manifest` (None only checks the guard)."""
    if os.environ.get(NO_PROFILES):
        raise RuntimeError(f"{NO_PROFILES} is set: a test may not write the Windows registry")
    if manifest is None and not remove:
        return
    import winreg  # type: ignore[import-not-found]

    for _, _, vendor in BROWSERS.values():
        key = rf"{vendor}\NativeMessagingHosts\{NAME}"
        try:
            if remove:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
                continue
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, vendor).Close()
        except OSError:
            continue
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as opened:
            winreg.SetValue(opened, "", winreg.REG_SZ, str(manifest))


def _steps(where: Path) -> None:
    from flnd.ui import out

    out.line(("Load flnd's extension in Chrome:", "bold"))
    out.line(("  1. ", "dim"), "open ", ("chrome://extensions", "focus"))
    out.line(("  2. ", "dim"), "turn on ", ("Developer mode", "focus"))
    out.line(("  3. ", "dim"), "click ", ("Load unpacked", "focus"), " and choose ",
             (str(where), "focus"))


def _wait(seconds: float) -> bool:
    """Wait up to `seconds` for the extension to answer `hello`, with a spinner."""
    from flnd.ui import out

    async def hello() -> str | None:
        async with asyncio.timeout(10), await connect() as client:
            return client.version

    start = time.monotonic()
    while True:
        try:
            version = asyncio.run(hello())
        except (errors.Error, TimeoutError):
            pass
        else:
            out.spin("", at=None)
            out.line(("connected", "good"), (f": flnd's extension {version}", "dim"))
            return True
        waited = time.monotonic() - start
        if waited >= seconds:
            out.spin("", at=None)
            out.line(("the extension hasn't said hello yet", "warn"),
                     (": it connects once Chrome loads it", "dim"))
            return False
        out.spin("waiting for the extension", at=waited)
        time.sleep(0.25)
