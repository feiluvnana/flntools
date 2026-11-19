"""The log lines' levels and scope, the folding of repeated warnings, and the run journal.

src/docs/cli explains it.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import msgspec
from rich.console import Console as RichConsole
from rich.text import Text

from flnd.ui import out, theme
from flnkit import clock, text

_SECRET_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization", "set-cookie"})


def _request_url(url: str) -> str:
    """`url` with userinfo and fragment removed, keeping the query string for signed CDNs."""
    try:
        parts = urlsplit(url)
        if not parts.netloc:
            return urlunsplit((parts.scheme, "", parts.path, parts.query, ""))
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = f"{host}:{parts.port}" if parts.port is not None else host
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    except ValueError:
        return url.split("#", 1)[0]

Level = Literal["debug", "info", "ok", "warn", "error", "quiet"]
LEVELS: tuple[Level, ...] = ("debug", "info", "ok", "warn", "error", "quiet")
LEVEL_RANKS: dict[str, int] = {
    "debug": 0, "info": 1, "ok": 2, "warn": 3, "error": 4, "quiet": 4,
}

def _color_settings(
    color: str | bool | None, base: RichConsole | None,
) -> tuple[Any, bool | None, bool | None]:
    """The Rich settings for a `color` choice: color system, no-color and force-terminal.

    Args:
        color: None copies `base`, or auto-detects when there is no base. False or "none"
            turns color off. True, "16m" or "truecolor", "256", and "8" or "standard" pick the
            color depth.
        base: the console to copy from when `color` is None; None when there is none.

    Returns:
        The three values `RichConsole` takes for those settings.

    Raises:
        ValueError: `color` is not one of the choices above.
    """
    if color is None and base is not None:
        return base.color_system, base.no_color, base._force_terminal
    table: dict[Any, tuple[Any, bool | None, bool | None]] = {
        None: ("auto", None, None),
        False: (None, True, False),
        "none": (None, True, False),
        True: ("truecolor", False, True),
        "16m": ("truecolor", False, True),
        "truecolor": ("truecolor", False, True),
        "256": ("256", False, True),
        "8": ("standard", False, True),
        "standard": ("standard", False, True),
    }
    if color not in table:
        raise ValueError(f"unknown color setting {color!r}")
    return table[color]


class Journal:
    """A JSON Lines run journal file recording outcome records and log lines.

    Args:
        path: the file to append to; its folder is made when it is missing.
        owner: the `UI` whose log lines are journalled while this is open.
    """

    def __init__(self, path: str | os.PathLike[str], owner: UI) -> None:
        self.path = Path(path)
        self.owner = owner
        self._file: IO[str] | None = None
        # Held for each whole line and for the close. Reentrant: a Ctrl-C handler logs on the
        # main thread, which may be inside `write` already.
        self._lock = threading.RLock()

    def __enter__(self) -> Journal:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8")
        self.owner._journal = self
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.owner._journal = None
        with self._lock:
            if self._file is not None:
                try:
                    self._file.flush()
                    self._file.close()
                finally:
                    self._file = None

    def write(self, record: Any) -> None:
        """Write one JSON record object, adding `at` and `type` if omitted.

        Does nothing when the journal is not open.

        Args:
            record: a dict, an object with attributes, or a value msgspec can encode.
        """
        if self._file is None:
            return

        event_type = type(record).__name__
        if isinstance(record, dict):
            event_type = str(record.get("type", "Event"))
        elif hasattr(record, "__dict__"):
            record = record.__dict__
        data = msgspec.to_builtins(record, enc_hook=_encode)
        if not isinstance(data, dict):
            data = {"value": data}

        if "type" not in data:
            data["type"] = event_type
        if "at" not in data:
            data["at"] = datetime.fromtimestamp(clock.wall(), tz=UTC).isoformat()

        line = json.dumps(data, ensure_ascii=False) + "\n"
        with self._lock:
            if self._file is not None:
                self._file.write(line)
                self._file.flush()


def _encode(value: Any) -> Any:
    """Write a value msgspec has no form for: a path as a string, an error as its parts.

    Args:
        value: the value msgspec could not encode.

    Returns:
        A string, or for an error a dict of its class name, message and hint.
    """
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, BaseException):
        hint = getattr(value, "hint", None)
        return {"class": type(value).__name__, "message": str(value), "hint": hint}
    return str(value)


class UI:
    """flnd's log lines: their level, where they go, and the journal they are written to."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._level: Level = "info"
        self._console = self._make_console()
        self._journal: Journal | None = None
        self._folds = out.Folds()

    @property
    def level(self) -> Level:
        """Current minimum log level."""
        return self._level

    @level.setter
    def level(self, value: Level) -> None:
        if value not in LEVELS:
            raise ValueError(f"unknown ui level {value!r}; choose from {', '.join(LEVELS)}")
        self._level = value

    @property
    def raw(self) -> RichConsole:
        """Underlying Rich Console."""
        return self._console

    def _allows(self, kind: Level) -> bool:
        return LEVEL_RANKS[kind] >= LEVEL_RANKS[self._level]

    def _use_unicode(self) -> bool:
        return theme.unicode(self._console.file)

    def _flush(self) -> None:
        if hasattr(self._console.file, "flush"):
            try:
                self._console.file.flush()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _make_console(
        *,
        color: str | bool | None = None,
        dest: IO[str] | None = None,
        width: int | None = None,
        base: RichConsole | None = None,
    ) -> RichConsole:
        """A Rich console for the given settings; `base` supplies any setting left as None.

        Args:
            color: the color choice, as `_color_settings` takes it; None copies `base`.
            dest: the file to write to; None writes to stderr, or to `base`'s file.
            width: the width in columns; None copies `base`, or lets Rich decide.
            base: the console to copy from; None when there is none.

        Returns:
            The new console. It has no highlighting and soft-wraps long lines.
        """
        color_system, no_color, force_terminal = _color_settings(color, base)
        file = dest if dest is not None else (base.file if base is not None else None)
        return RichConsole(
            file=file,
            stderr=file is None,
            width=width if width is not None else (base.width if base is not None else None),
            color_system=color_system,
            no_color=no_color,
            force_terminal=force_terminal,
            highlight=False,
            soft_wrap=True,
        )

    def _log(self, _kind: Level, message: str, *args: Any, **fields: Any) -> None:
        """Write one log line of `_kind` if its level is on, and journal it when a journal is open.

        A warning that repeats within `out.FOLD` seconds folds into one line (`out.Folds`).

        Args:
            _kind: the level: debug, info, ok, warn or error.
            message: the message template; `{name}` marks a field.
            *args: positional values for the template.
            **fields: values for the template's named fields. Each must be named in it.

        Raises:
            TypeError: `fields` holds a name the message does not use.
        """
        unused = set(fields) - out.names(message) if fields else set()
        if unused:
            raise TypeError(
                f"ui message {message!r} has no field {', '.join(sorted(unused))}"
            )
        if self._journal is None and not self._allows(_kind):
            return
        text = out.fill(message, *args, **fields)
        if self._journal is not None:
            self._journal.write({
                "type": "Log",
                "level": _kind,
                "message": text,
                "fields": fields,
            })
        if not self._allows(_kind):
            return
        with self._lock:
            shown = self._folds.see(text) if _kind == "warn" else [(text, 1)]
            for line, times in shown:
                self._show(out.styled(_kind, line, fancy=self._use_unicode(), times=times))

    def note(self, record: dict[str, Any]) -> None:
        """Write a record to the journal only, such as an account switch; nothing without one.

        Args:
            record: its fields, with its `type`.
        """
        if self._journal is not None:
            self._journal.write(record)

    def record(self, kind: Level, message: str) -> None:
        """Write a line to the journal only, never to the terminal: the screen's own failures.

        Args:
            kind: the line's level.
            message: the line, as it is.
        """
        if self._journal is not None:
            self._journal.write({"type": "Log", "level": kind, "message": message, "fields": {}})

    def request(
        self,
        client: Any,
        url: str,
        file: str,
        *,
        headers: Mapping[str, str] | None = None,
        offset: int = 0,
        etag: str | None = None,
    ) -> None:
        """Journal a file GET request with its query string and scrubbed request headers.

        Args:
            client: the `flnkit.net.Client` sending the request.
            url: the file URL, kept with its query string so signed CDN links work.
            file: the target file path label.
            headers: per-post request headers (`Referer`, etc.).
            offset: resume byte offset for `Range`.
            etag: validator for `If-Range` when resuming.
        """
        clean_url = _request_url(url)
        message = f"GET {clean_url} -> {file}"
        if self._journal is not None:
            session = getattr(client, "session", None)
            merged: dict[str, Any] = dict(getattr(session, "headers", None) or {})
            if headers:
                merged.update(headers)
            merged["Range"] = f"bytes={offset}-"
            if offset and etag:
                merged["If-Range"] = etag
            safe_headers = {
                str(k): (
                    "…"
                    if text.SECRET_NAMES.search(str(k)) or str(k).lower() in _SECRET_HEADERS
                    else v
                )
                for k, v in merged.items()
            }
            fields: dict[str, Any] = {
                "method": "GET",
                "url": clean_url,
                "headers": safe_headers,
                "impersonate": "chrome",
                "file": file,
            }
            proxy = getattr(session, "proxy", None)
            if isinstance(proxy, str) and proxy:
                fields["proxy"] = text.redact(proxy)
            self._journal.write({
                "type": "Log",
                "level": "debug",
                "message": message,
                "fields": fields,
            })
        if self._allows("debug"):
            with self._lock:
                screen = f"GET {text.redact(clean_url)} -> {file}"
                self._show(out.styled("debug", screen, fancy=self._use_unicode()))

    def drain(self) -> None:
        """Print the warnings still folded, each as "text (×n)", where this `UI` prints now."""
        with self._lock:
            for text, times in self._folds.drain():
                self._show(out.styled("warn", text, fancy=self._use_unicode(), times=times))

    def _show(self, line: Text) -> None:
        """Print a line: above the run's screen while one is drawn there (`out.above`)."""
        sink = out._above
        if sink is not None and self._console.file is sys.stderr:
            sink(line)
            return
        self._console.print(line)
        self._flush()

    def debug(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a debug message."""
        self._log("debug", message, *args, **fields)

    def info(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log an informational message."""
        self._log("info", message, *args, **fields)

    def ok(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a success message."""
        self._log("ok", message, *args, **fields)

    def warn(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log a warning message."""
        self._log("warn", message, *args, **fields)

    def error(self, message: str = "", /, *args: Any, **fields: Any) -> None:
        """Log an error message."""
        self._log("error", message, *args, **fields)

    def file(self, path: str | os.PathLike[str]) -> Journal:
        """Context manager opening a JSON Lines journal file."""
        return Journal(path, self)

    @contextmanager
    def scope(
        self,
        *,
        color: str | bool | None = None,
        dest: IO[str] | None = None,
        width: int | None = None,
        level: Level | None = None,
    ) -> Iterator[UI]:
        """Inside the block, write to another destination, or with other colors, width or level.

        Args:
            color: the color choice, as `_color_settings` takes it; None keeps the current one.
            dest: the file to write to; None keeps the current destination.
            width: the width in columns; None keeps the current width.
            level: the level for the block; None keeps the current level.

        Yields:
            This `UI`, with the new settings in place.
        """
        old_console, old_level = self._console, self._level
        old_folds = self._folds
        try:
            self._console = self._make_console(
                color=color, dest=dest, width=width, base=self._console,
            )
            if level is not None:
                self._level = level
            self._folds = out.Folds()
            yield self
        finally:
            # The warnings still folded are printed where the block printed them.
            self.drain()
            self._console, self._level = old_console, old_level
            self._folds = old_folds

ui = UI()
log = ui
# A script that warns outside any scope still sees each warning's count, when Python exits.
atexit.register(ui.drain)


class _KitLines(logging.Handler):
    """Routes the kit's `logging` records (`flnkit.*`) to `ui`'s lines and the journal.

    A kit module logs `logger.warning(template, extra={"fields": {...}})`; the record becomes
    `ui.warn(template, **fields)`, so it reads and is journalled as flnd's own lines are.
    """

    def handle(self, record: logging.LogRecord) -> bool:
        # No handler lock: `ui` has its own, and a record is passed on whole.
        self.emit(record)
        return True

    def emit(self, record: logging.LogRecord) -> None:
        """Pass one `logging` record to `ui` as a line of the matching level.

        Args:
            record: the record a kit module logged.
        """
        kind: Level = "debug"
        if record.levelno >= logging.ERROR:
            kind = "error"
        elif record.levelno >= logging.WARNING:
            kind = "warn"
        elif record.levelno >= logging.INFO:
            kind = "info"
        ui._log(kind, str(record.msg), **getattr(record, "fields", {}))


_kit = logging.getLogger("flnkit")
_kit.setLevel(logging.DEBUG)
_kit.addHandler(_KitLines())
# The kit's lines are flnd's own: a script's root handler doesn't print them a second time.
_kit.propagate = False


__all__ = ["Journal", "UI", "log", "ui"]
