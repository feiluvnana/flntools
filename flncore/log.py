"""Terminal output: log lines and progress items, safe for site text.

The single renderer, stable keys, and the run journal.
"""

from __future__ import annotations

import json
import os
import re
import string
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import IO, Any, Literal
from urllib.parse import urlsplit

from rich.console import Console as RichConsole
from rich.control import strip_control_codes
from rich.text import Text
from rich.theme import Theme

from flncore import clock as _time

Level = Literal["debug", "info", "ok", "warn", "error", "quiet"]
LEVELS: tuple[Level, ...] = ("debug", "info", "ok", "warn", "error", "quiet")
LEVEL_RANKS: dict[str, int] = {
    "debug": 0, "info": 1, "ok": 2, "warn": 3, "error": 4, "quiet": 4,
}

PALETTE: dict[str, str] = {
    "info": "color(243)",
    "ok": "#02BF87",
    "warn": "yellow",
    "error": "#ED567A",
    "debug": "color(242)",
    "text": "color(252)",
    "faint": "color(238)",
    "accent": "#7D84D6",
    "size": "color(245)",
}

GLYPHS: dict[str, tuple[str, str]] = {
    "info": ("•", "*"),
    "ok": ("✓", "+"),
    "warn": ("!", "!"),
    "error": ("✗", "x"),
    "debug": ("·", "."),
    "bar_full": ("━", "="),
    "bar_empty": ("─", "-"),
}

LOG_GLYPHS: dict[str, str] = {
    "info": "info", "ok": "ok", "warn": "warn", "error": "error", "debug": "debug",
}

SPIN_FRAMES: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"),
    ("|", "/", "-", "\\"),
)
BAR_CELLS = 20

_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]"
)


class _SafeFormatter(string.Formatter):
    """`str.format` that strips terminal escape and control codes from each field it formats."""

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, str):
            value = _sanitize(value)
        return _sanitize(super().format_field(value, format_spec))


_FORMATTER = _SafeFormatter()


def _sanitize(value: str) -> str:
    return strip_control_codes(_ESCAPE.sub("", value))


@lru_cache(maxsize=256)
def _field_names(message: str) -> frozenset[str]:
    names: set[str] = set()
    for _, field_name, format_spec, _ in string.Formatter().parse(message):
        if field_name:
            names.add(field_name.split(".")[0].split("[")[0])
        if format_spec and "{" in format_spec:
            names.update(_field_names(format_spec))
    return frozenset(names)


def _color_settings(
    color: str | bool | None, base: RichConsole | None,
) -> tuple[Any, bool | None, bool | None]:
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


def _detect_unicode(stream: Any) -> bool:
    encoding = getattr(stream, "encoding", None) or ""
    if not encoding and getattr(stream, "isatty", lambda: False)():
        encoding = "utf-8"
    if encoding.lower().replace("-", "").replace("_", "") != "utf8":
        return False
    locale = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if locale.lower().startswith(("ja", "ko", "zh")):
        return False
    modern = ("WT_SESSION", "TERM", "ANSICON", "TERM_PROGRAM")
    if os.name == "nt" and not any(os.environ.get(k) for k in modern):
        return os.environ.get("ConEmuANSI") == "ON"
    return True


def _elide_middle(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return "…"[:max_len]
    keep = max_len - 1
    left = keep // 2
    right = keep - left
    return f"{text[:left]}…{text[-right:]}"


_URL = re.compile(r"https?://[^\s:]+(?::\d+)?[^\s]*")


def _brief(error: BaseException, *, limit: int = 100) -> str:
    """One line naming `error`, with each URL cut to its host."""
    first = (str(error).strip().splitlines() or [""])[0]
    first = _URL.sub(lambda match: urlsplit(match.group()).hostname or match.group(), first)
    message = f"{type(error).__name__}: {first}" if first else type(error).__name__
    return message if len(message) <= limit else message[: limit - 1] + "…"


class State(StrEnum):
    """The lifecycle state of a progress item."""

    QUEUED = "queued"
    ACTIVE = "active"
    MERGING = "merging"
    UNPACKING = "unpacking"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class Item:
    """One progress item: a stable key, a label, count/total, state, and an optional note."""

    def __init__(
        self,
        key: str,
        label: str = "",
        *,
        total: float | None = None,
        unit: str | None = None,
        drawn: bool = True,
        owner: UI,
    ) -> None:
        self.key = key
        self.label = label or key
        self.total = total
        self.count = 0.0
        self.unit = unit
        self.drawn = drawn
        self.owner = owner
        self.state = State.ACTIVE
        self._note = ""
        self._started = _time.now()
        self._ended: float | None = None
        self._outcome: Level | None = None

    @property
    def raw(self) -> Any:
        """Raw item identifier for compatibility."""
        return self.key

    def advance(self, n: float = 1) -> None:
        """Increment progress by `n`."""
        self.count += n
        if self.drawn and self.owner._live:
            self.owner._item_updated(self)

    def note(self, text: str) -> None:
        """Set the text after the bar."""
        self._note = text
        if self.drawn and self.owner._live:
            self.owner._item_updated(self)

    def ok(self, note: str = "") -> None:
        """Mark the item as successfully finished."""
        if self._ended is not None:
            return
        self.state = State.DONE
        if note:
            self._note = note
        self._ended = _time.now()
        self._outcome = "ok"
        self.owner._item_finished(self)

    def skip(self, note: str = "") -> None:
        """Mark the item as skipped."""
        if self._ended is not None:
            return
        self.state = State.SKIPPED
        if note:
            self._note = note
        self._ended = _time.now()
        self._outcome = "warn"
        self.owner._item_finished(self)

    def fail(self, note: str = "") -> None:
        """Mark the item as failed."""
        if self._ended is not None:
            return
        self.state = State.FAILED
        if note:
            self._note = note
        self._ended = _time.now()
        self._outcome = "error"
        self.owner._item_finished(self)

    def finish(self) -> None:
        """Alias for `ok()`."""
        self.ok()

    def part(self) -> Callable[[int, int | None], None]:
        """A callback feeding `(fed, total)` chunks from a stream."""
        def callback(fed: int, total: int | None = None) -> None:
            self.count = float(fed)
            if total is not None:
                self.total = float(total)
            if self.drawn and self.owner._live:
                self.owner._item_updated(self)
        return callback

    def __enter__(self) -> Item:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is None:
            if self.state in (State.ACTIVE, State.QUEUED, State.MERGING, State.UNPACKING):
                self.ok()
        else:
            if isinstance(exc_val, KeyboardInterrupt):
                self.fail("stopped")
            else:
                self.fail(_brief(exc_val) if exc_val else "failed")


# Backwards compatibility alias for flnd
Row = Item


class Journal:
    """A JSON Lines run journal file recording outcome records and log lines."""

    def __init__(self, path: str | os.PathLike[str], owner: UI) -> None:
        self.path = Path(path)
        self.owner = owner
        self._file: IO[str] | None = None

    def __enter__(self) -> Journal:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8")
        self.owner._journal = self
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.owner._journal = None
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            finally:
                self._file = None

    def write(self, record: Any) -> None:
        """Write one JSON record object, adding `at` and `type` if omitted."""
        if self._file is None:
            return
        if isinstance(record, dict):
            data = dict(record)
            event_type = str(data.get("type", "Event"))
        elif hasattr(record, "__dict__"):
            data = dict(record.__dict__)
            event_type = type(record).__name__
        else:
            try:
                import msgspec
                data = msgspec.to_builtins(record)
            except Exception:
                data = {"value": str(record)}
            event_type = type(record).__name__

        if event_type == "Progress" or data.get("type") == "Progress":
            return

        if "type" not in data:
            data["type"] = event_type
        if "at" not in data:
            now = _time.now()
            data["at"] = datetime.fromtimestamp(now, tz=UTC).isoformat()

        line = json.dumps(data, ensure_ascii=False, default=str)
        self._file.write(line + "\n")
        self._file.flush()


class Group:
    """A collection of progress items belonging to one logical task."""

    def __init__(self, owner: UI, title: str = "", *, drawn: bool = True) -> None:
        self.owner = owner
        self.title = title
        self.drawn = drawn
        self._items: list[Item] = []

    def add(
        self, label: str, *, total: float | None = None, unit: str | None = None,
    ) -> Item:
        item = self.owner.item(label, label, total=total, unit=unit, drawn=self.drawn)
        self._items.append(item)
        return item

    def __enter__(self) -> Group:
        if self.title:
            self.owner.info(self.title)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        for item in self._items:
            if item.state in (State.ACTIVE, State.QUEUED, State.MERGING, State.UNPACKING):
                if exc_type is not None:
                    item.fail(_brief(exc_val) if exc_val else "failed")
                else:
                    item.ok()


class UI:
    """The terminal output renderer, log dispatcher, and progress tracker."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._level: Level = "info"
        self._console = self._make_console()
        self._items: dict[str, Item] = {}
        self._active_keys: list[str] = []
        self._live_lines = 0
        self._pending_lines: list[Text | str] = []
        self._last_draw = 0.0
        self._journal: Journal | None = None
        self._live_tasks: dict[Any, Item] = {}
        self._live_count = 0
        self._live = self._compute_live()

    def _compute_live(self) -> bool:
        if self._level == "quiet":
            return False
        file = self._console.file
        is_tty = getattr(file, "isatty", lambda: False)()
        return is_tty or bool(getattr(self._console, "_force_terminal", False))

    @property
    def live(self) -> bool:
        """Whether the screen is interactive and live bars should be drawn."""
        return self._live

    def _progress_enabled(self) -> bool:
        return self.live

    @property
    def _live_progress(self) -> Any:
        return self if self.live and self._live_count > 0 else None

    @property
    def level(self) -> Level:
        """Current minimum log level."""
        return self._level

    @level.setter
    def level(self, value: Level) -> None:
        if value not in LEVELS:
            raise ValueError(f"unknown ui level {value!r}; choose from {', '.join(LEVELS)}")
        self._level = value
        self._live = self._compute_live()

    @property
    def raw(self) -> RichConsole:
        """Underlying Rich Console."""
        return self._console

    def _allows(self, kind: Level) -> bool:
        return LEVEL_RANKS[kind] >= LEVEL_RANKS[self._level]

    def _use_unicode(self) -> bool:
        return _detect_unicode(self._console.file)

    def _glyph(self, name: str) -> str:
        unicode, ascii_ = GLYPHS[name]
        return unicode if self._use_unicode() else ascii_

    def _spinner(self) -> str:
        frames = SPIN_FRAMES[0] if self._use_unicode() else SPIN_FRAMES[1]
        return frames[int(_time.now() * 10) % len(frames)]

    def _get_width(self) -> int:
        if self._console.width is not None:
            return self._console.width
        try:
            return os.get_terminal_size().columns
        except (OSError, ValueError):
            return 80

    def _get_height(self) -> int:
        if self._console.height is not None:
            return self._console.height
        try:
            return os.get_terminal_size().lines
        except (OSError, ValueError):
            return 24

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
        color_system, no_color, force_terminal = _color_settings(color, base)
        file = dest if dest is not None else (base.file if base is not None else None)
        return RichConsole(
            file=file,
            stderr=file is None,
            width=width if width is not None else (base.width if base is not None else None),
            color_system=color_system,
            no_color=no_color,
            force_terminal=force_terminal,
            theme=Theme(PALETTE),
            highlight=False,
            soft_wrap=True,
        )

    def _render_item(self, it: Item, width: int, unicode: bool) -> Text:
        if it.state == State.ACTIVE:
            spinner = self._spinner()
            glyph_style = "accent"
        elif it.state in (State.MERGING, State.UNPACKING):
            spinner = self._spinner()
            glyph_style = "warn"
        elif it.state == State.DONE:
            spinner = self._glyph("ok")
            glyph_style = "ok"
        elif it.state == State.SKIPPED:
            spinner = self._glyph("warn")
            glyph_style = "warn"
        else:
            spinner = self._glyph("error")
            glyph_style = "error"

        if it.unit == "bytes":
            if it.total is not None and it.total > 0:
                stats = f"{size(it.count)}/{size(it.total)}"
            else:
                stats = size(it.count)
        else:
            if it.total is not None and it.total > 0:
                stats = f"{it.count:g}/{it.total:g}"
            else:
                stats = f"{it.count:g}"

        note_str = f"  ({it._note})" if it._note else ""
        bar_len = 16
        full_c = GLYPHS["bar_full"][0 if unicode else 1]
        empty_c = GLYPHS["bar_empty"][0 if unicode else 1]

        if it.total is not None and it.total > 0:
            pct = max(0.0, min(1.0, it.count / it.total))
            filled = int(round(pct * bar_len))
            bar_full = full_c * filled
            bar_empty = empty_c * (bar_len - filled)
        else:
            bar_full = ""
            bar_empty = empty_c * bar_len

        fixed_len = len(spinner) + 3 + bar_len + 2 + len(stats) + len(note_str)
        avail_for_label = max(8, width - fixed_len)
        label = _elide_middle(it.label, avail_for_label)

        line = Text()
        line.append(spinner, style=glyph_style)
        line.append(" ")
        line.append(label, style="text")
        line.append("  ")
        if bar_full:
            line.append(bar_full, style="accent")
        if bar_empty:
            line.append(bar_empty, style="faint")
        line.append("  ")
        line.append(stats, style="size")
        if note_str:
            line.append(note_str, style="faint")

        line.truncate(width)
        return line

    def _redraw(self, force: bool = False) -> None:
        now = _time.now()
        if not force and (now - self._last_draw < 0.1):
            return
        self._last_draw = now
        file = self._console.file

        if self._live_lines > 0:
            if hasattr(file, "write"):
                file.write(f"\r\x1b[{self._live_lines}A\x1b[J")
            self._live_lines = 0

        while self._pending_lines:
            pending = self._pending_lines.pop(0)
            self._console.print(pending)

        if not self._active_keys:
            self._flush()
            return

        w = self._get_width()
        h = self._get_height()
        unicode = self._use_unicode()
        cap = max(1, h - 2)

        active = [self._items[k] for k in self._active_keys if k in self._items]
        if len(active) <= cap:
            visible = active
            folded = 0
        else:
            visible = active[:cap - 1]
            folded = len(active) - (cap - 1)

        drawn_count = 0
        for it in visible:
            rendered = self._render_item(it, width=w, unicode=unicode)
            self._console.print(rendered)
            drawn_count += 1

        if folded > 0:
            fold_glyph = "…" if unicode else "..."
            fold_line = f"  {fold_glyph} and {folded} more"
            self._console.print(Text(fold_line[:w], style="faint"))
            drawn_count += 1

        self._live_lines = drawn_count
        self._flush()

    def item(
        self,
        key: str,
        label: str = "",
        *,
        total: float | None = None,
        unit: str | None = None,
        drawn: bool = True,
    ) -> Item:
        """Create and register a progress item under a stable key."""
        with self._lock:
            it = Item(key, label or key, total=total, unit=unit, drawn=drawn, owner=self)
            self._items[key] = it
            self._live_count += 1
            self._live_tasks[it.raw] = it
            if drawn and self.live:
                if key not in self._active_keys:
                    self._active_keys.append(key)
                self._redraw(force=True)
            return it

    def _item_updated(self, item: Item) -> None:
        if not item.drawn or not self.live:
            return
        now = _time.now()
        if now - self._last_draw >= 0.1:
            with self._lock:
                self._redraw()

    def _item_finished(self, item: Item) -> None:
        with self._lock:
            if item.key in self._active_keys:
                self._active_keys.remove(item.key)
            if item.raw in self._live_tasks:
                del self._live_tasks[item.raw]
            self._live_count = max(0, self._live_count - 1)

            if item.drawn:
                rendered = self._render_item(item, self._get_width(), self._use_unicode())
                if not self.live:
                    self._console.print(rendered)
                    self._flush()
                else:
                    self._pending_lines.append(rendered)
                    self._redraw(force=True)

    def _log(self, _kind: Level, message: str, *args: Any, **fields: Any) -> None:
        unused = set(fields) - _field_names(message) if fields else set()
        if unused:
            raise TypeError(
                f"ui message {message!r} has no field {', '.join(sorted(unused))}"
            )
        if not self._allows(_kind):
            return
        text = (
            _FORMATTER.format(message, *args, **fields)
            if (args or fields) else _sanitize(message)
        )
        if self._journal is not None:
            if _kind != "debug" or self._allows("debug"):
                self._journal.write({
                    "type": "Log",
                    "level": _kind,
                    "message": text,
                    "fields": fields,
                })

        mark = self._glyph(LOG_GLYPHS.get(_kind, "info"))
        with self._lock:
            if not text or _kind == "debug":
                line = Text(f"{mark} {text}" if text else mark, style=_kind)
            elif _kind == "info":
                head, colon, detail = text.partition(": ")
                line = Text.assemble(
                    (mark, "info"), " ", (head, "text"), (colon + detail, "info"),
                )
            else:
                head, colon, detail = text.partition(": ")
                line = Text.assemble(
                    (f"{mark} {head}", _kind),
                    (colon + detail, f"dim {_kind}") if colon else ("", ""),
                )

            if self.live and self._active_keys:
                self._pending_lines.append(line)
                self._redraw(force=True)
            else:
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
        """Inside the block, write to another destination, or with other colors, width or level."""
        old_console, old_level, old_live = self._console, self._level, self._live
        try:
            self._console = self._make_console(
                color=color, dest=dest, width=width, base=self._console,
            )
            if level is not None:
                self._level = level
            self._live = self._compute_live()
            yield self
        finally:
            self._console, self._level, self._live = old_console, old_level, old_live

    @contextmanager
    def group(self, title: str = "", *, drawn: bool = True) -> Iterator[Group]:
        """Context manager yielding a `Group` of progress items."""
        grp = Group(self, title, drawn=drawn)
        with grp:
            yield grp

    @contextmanager
    def progress(
        self,
        label: str,
        *,
        total: float | None = None,
        unit: str | None = None,
        drawn: bool = True,
    ) -> Iterator[Item]:
        """Context manager yielding a single `Item` progress bar."""
        it = self.item(label, label, total=total, unit=unit, drawn=drawn)
        with it:
            yield it


ui = UI()
log = ui

BYTE_UNITS = ("B", "KB", "MB", "GB", "TB")


def size(n: float) -> str:
    amount = float(n)
    for u in BYTE_UNITS[:-1]:
        if abs(round(amount, 1)) < 1024:
            return f"{amount:.1f} {u}"
        amount /= 1024
    return f"{amount:.1f} {BYTE_UNITS[-1]}"


def duration(seconds: float) -> str:
    if round(seconds * 1000) < 1000:
        return f"{round(seconds * 1000)}ms"
    if round(seconds, 1) < 60:
        return f"{seconds:.1f}s"
    whole = round(seconds)
    units = ((whole // 3600, "h"), (whole % 3600 // 60, "m"), (whole % 60, "s"))
    nonzero = [f"{amount}{u}" for amount, u in units if amount]
    return " ".join(nonzero[:2])


def count(n: float, unit: str, *, plural: str | None = None) -> str:
    if n == 1:
        return f"{n:,} {unit}"
    return f"{n:,} {plural if plural is not None else unit + 's'}"


def redact(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    try:
        parts = urlsplit(url)
        if not parts.netloc:
            return urlunsplit((parts.scheme, "", parts.path, "", ""))
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = f"{host}:{parts.port}" if parts.port is not None else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        head = re.split(r"[?#]", url, maxsplit=1)[0]
        scheme, sep, rest = head.partition("://")
        if not sep:
            return head
        authority, slash, path = rest.partition("/")
        return f"{scheme}{sep}{authority.rpartition('@')[2]}{slash}{path}"


__all__ = [
    "Item",
    "Journal",
    "State",
    "UI",
    "count",
    "duration",
    "log",
    "redact",
    "size",
    "ui",
]
