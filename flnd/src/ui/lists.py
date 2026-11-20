"""The interactive lists: targets, accounts and plugins, inline under the prompt (Textual).

COOKBOOK.md#commands-and-flags explains it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from flnd.errors import Error
from flnd.ui import forms, out, theme

__all__ = ["AccountRow", "Column", "Key", "PluginRow", "Row", "TargetRow", "accounts", "plugins",
           "show", "targets"]

Part = out.Part


@dataclass(frozen=True)
class Column:
    """A list's column: its title (the pipe's table header) and whether it is right-aligned."""

    title: str
    right: bool = False


@dataclass(frozen=True)
class Row:
    """One row of a list.

    Args:
        key: what the keys' callbacks get for it: a target, an account, a plugin.
        cells: one per column: text, or (text, style) pairs.
        fixed: `d` is shown dim and does nothing on it (a built-in plugin).
    """

    key: Any
    cells: tuple[Part, ...]
    fixed: bool = False


@dataclass(frozen=True)
class Key:
    """A list's own key.

    Args:
        char: the key pressed ("f", "*").
        label: what it does, in the bottom line.
        run: called with the key of the row under the cursor (None in an empty list); the rows
            load again afterwards. An `Error` it raises shows in the bottom line.
        leave: close the list while `run` runs (it asks or draws), then open it again.
    """

    char: str
    label: str
    run: Callable[[Any], object]
    leave: bool = False


def show(title: str, columns: Sequence[Column], load: Callable[[], Sequence[Row]], *,
         keys: Sequence[Key] = (), delete: Key | None = None) -> None:
    """Show a list until `q`: on a terminal, inline with its keys; on a pipe, a plain table.

    Args:
        title: the list's name, shown with its count ("Targets · 3").
        load: the rows, called at the start and after every key that changes them.
        keys: the list's own keys, beside ↑↓, /, enter, d and q.
        delete: what `d` does after a confirmation; None for no `d`.

    Raises:
        Stopped: Ctrl-C.
    """
    if not (theme.tty(sys.stdout) and forms.interactive()):
        out.table([row.cells for row in load()], [column.title for column in columns])
        return
    view = View(title, tuple(columns), load, tuple(keys), delete)
    while True:
        picked = forms.run(view, keep=False)
        if picked is None:
            return
        key, row = picked
        view.call(key, row)


class View(forms.Owner):
    """A list's state: its rows, cursor, filter, and whether a panel or a question is open."""

    def __init__(self, title: str, columns: tuple[Column, ...], load: Callable[[], Sequence[Row]],
                 keys: tuple[Key, ...], delete: Key | None) -> None:
        self.title, self.columns, self.load = title, columns, load
        self.keys = {key.char: key for key in keys}
        self.delete = delete
        self.rows: list[Row] = list(load())
        self.at = 0
        self.query = ""
        self.typing = False
        self.detail = False
        self.asking: forms.Field | None = None
        self.message = ""

    # -- what is shown ------------------------------------------------------------------------

    def shown(self) -> list[Row]:
        if not self.query:
            return self.rows
        words = self.query.lower()
        return [row for row in self.rows if words in " ".join(map(_words, row.cells)).lower()]

    def current(self) -> Row | None:
        rows = self.shown()
        return rows[min(self.at, len(rows) - 1)] if rows else None

    def view(self, fancy: bool) -> Any:
        from rich.console import Group
        from rich.text import Text

        rows = self.shown()
        count = f"{len(rows)} of {len(self.rows)}" if self.query else str(len(self.rows))
        head = Text(f"  {self.title}", style=theme.style("bold focus"))
        head.append(f" {theme.glyph('waiting', fancy)} {count}", style=theme.style("dim"))
        side = Text(f"/ {self.query}" if self.query or self.typing else "/ filter",
                    style=theme.style("focus" if self.typing else "dim"))
        top = _spread(head, side)
        body = self.panel(fancy) if self.detail and rows else self.table(rows, fancy)
        parts: list[Any] = [top, Text(""), body, Text("")]
        if self.asking is not None:
            parts.append(self.asking.view(fancy))
        else:
            if self.message:
                parts.append(Text(f"  {self.message}", style=theme.style("bad")))
            parts.append(self.hints(fancy))
        return Group(*parts)

    def table(self, rows: list[Row], fancy: bool) -> Any:
        from rich.padding import Padding
        from rich.table import Table
        from rich.text import Text

        if not rows:
            return Text("    nothing matches" if self.query else "    nothing here",
                        style=theme.style("dim"))
        grid = Table.grid(padding=(0, 3))
        # Widths from every row and title, so a filter doesn't move the columns.
        widths = [max(len(column.title), *(len(_words(row.cells[n])) for row in self.rows))
                  for n, column in enumerate(self.columns)]
        widths[0] += 2
        for column, width in zip(self.columns, widths):
            grid.add_column(justify="right" if column.right else "left", no_wrap=True,
                            min_width=width)
        # The titles, as the pipe's table prints them, over the cursor's two cells.
        dim = theme.style("dim")
        grid.add_row(Text.assemble("  ", Text(self.columns[0].title, style=dim)),
                     *(Text(column.title, style=dim) for column in self.columns[1:]))
        here = self.current()
        for row in rows:
            on = row is here
            cells = [_cell(cell, on) for cell in row.cells]
            mark = Text(f"{theme.glyph('cursor', fancy)} " if on else "  ",
                        style=theme.style("focus"))
            grid.add_row(Text.assemble(mark, cells[0]), *cells[1:])
        return Padding(grid, (0, 0, 0, 2))

    def panel(self, fancy: bool) -> Any:
        from rich.padding import Padding
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        row = self.current()
        assert row is not None
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=theme.style("dim"))
        grid.add_column()
        for column, cell in zip(self.columns, row.cells):
            grid.add_row(column.title, _cell(cell, False))
        title = Text(_words(row.cells[0]), style=theme.style("bold focus"))
        drawn = Panel(grid, box=theme.box(), padding=theme.PADDING, title=title,
                      title_align="left", border_style=theme.style("dim"), expand=False)
        return Padding(drawn, (0, 0, 0, 2))

    def hints(self, fancy: bool) -> Any:
        if self.typing:
            return forms.hints((("enter", "keep"), ("esc", "clear")), fancy)
        if self.detail:
            return forms.hints((("esc", "close"),), fancy)
        pairs = [("updown", "move"), ("enter", "details")]
        pairs += [(key.char, key.label) for key in self.keys.values()]
        off: set[str] = set()
        if self.delete is not None:
            pairs.append(("d", self.delete.label))
            here = self.current()
            if here is None or here.fixed:
                off.add("d")
        pairs += [("/", "filter"), ("q", "quit")]
        return forms.hints(pairs, fancy, off=frozenset(off))

    # -- keys ---------------------------------------------------------------------------------

    def press(self, key: str, char: str | None) -> Any:
        if self.asking is not None:
            return self.answer(key, char)
        if self.typing:
            return self.filter(key, char)
        self.message = ""
        if self.detail:
            if key == "q":
                return None
            if key in ("escape", "enter"):
                self.detail = False
            return forms.PENDING
        rows = self.shown()
        if key in ("up", "k"):
            self.at = max(min(self.at, len(rows) - 1) - 1, 0)
        elif key in ("down", "j"):
            self.at = min(self.at + 1, max(len(rows) - 1, 0))
        elif key == "enter":
            self.detail = bool(rows)
        elif char == "/":
            self.typing = True
        elif key == "q":
            return None
        elif key == "escape":
            self.query = ""
        elif char == "d" and self.delete is not None:
            here = self.current()
            if here is not None and not here.fixed:
                verb = self.delete.label
                self.asking = forms._Confirm(f"{verb[:1].upper()}{verb[1:]} "
                                             f"{_words(here.cells[0])}?", default=False)
        elif char in self.keys:
            return self.key(self.keys[char])
        return forms.PENDING

    def paste(self, text: str) -> None:
        # Only into the filter: pasted letters must never act as the list's keys.
        if self.typing and self.asking is None:
            self.query += " ".join(text.splitlines())
            self.at = 0

    def filter(self, key: str, char: str | None) -> Any:
        if key == "enter":
            self.typing = False
        elif key == "escape":
            self.typing, self.query = False, ""
        elif key == "backspace":
            self.query = self.query[:-1]
        elif char:
            self.query += char
        self.at = 0
        return forms.PENDING

    def answer(self, key: str, char: str | None) -> Any:
        asking = self.asking
        assert asking is not None
        said = asking.press(key, char)
        if said is forms.PENDING:
            return said
        self.asking = None
        if said and self.delete is not None:
            here = self.current()
            self.call(self.delete, here.key if here is not None else None)
        return forms.PENDING

    def key(self, key: Key) -> Any:
        here = self.current()
        row = here.key if here is not None else None
        if key.leave:
            return (key, row)
        self.call(key, row)
        return forms.PENDING

    def call(self, key: Key, row: Any) -> None:
        """Run `key` on `row`, then load the rows again; an `Error` shows as the message."""
        try:
            key.run(row)
        except Error as error:
            self.message = str(error)
        self.rows = list(self.load())


def _words(part: Part) -> str:
    return out.clean(part if isinstance(part, str) else part[0])


def _cell(part: Part, on: bool) -> Any:
    from rich.text import Text

    if isinstance(part, str):
        return Text(out.clean(part), style=theme.style("focus" if on else "text"))
    return Text(out.clean(part[0]), style=theme.style(part[1]))


def _spread(left: Any, right: Any) -> Any:
    from rich.table import Table

    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column(justify="right")
    grid.add_row(left, right)
    return grid


# ---------------------------------------------------------------------------------------------
# The three lists of the command line. Each takes its rows and what its keys do; no engine.
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TargetRow:
    """A row of `target list`.

    Args:
        ago: seconds since its last run; None if it never ran.
    """

    name: str
    followed: bool
    categories: tuple[str, ...]
    files: int
    size: int
    ago: float | None


@dataclass(frozen=True)
class AccountRow:
    """A row of `auth list`.

    Args:
        origin: how it logged in: browser, chrome or file.
        held: seconds left on a hold; None when it isn't held.
        expires: seconds until its login expires (0 or less: expired); None when unknown.
        ago: seconds since it last saved a file; None if it never did.
    """

    plugin: str
    name: str
    default: bool
    origin: str
    held: float | None
    expires: float | None
    ago: float | None


@dataclass(frozen=True)
class PluginRow:
    """A row of `plugin list`: `targets` counts the targets that use it; `disabled` is why a
    plugin that didn't load is off (the row says "disabled"; the command prints the reason)."""

    code: str
    name: str
    builtin: bool
    api: int
    domains: tuple[str, ...]
    targets: int
    disabled: str = ""


TARGET_COLUMNS = (Column("target"), Column("followed"), Column("categories"),
                  Column("files", right=True), Column("size", right=True),
                  Column("last run", right=True))
ACCOUNT_COLUMNS = (Column("plugin"), Column("name"), Column("default"), Column("origin"),
                   Column("status"), Column("saved", right=True))
PLUGIN_COLUMNS = (Column("code"), Column("name"), Column("kind"), Column("api", right=True),
                  Column("domains"), Column("targets", right=True))
# An account whose login expires sooner than this shows the days left, in `warn`.
SOON = 7 * 86400


def targets(load: Callable[[], Sequence[TargetRow]], *, follow: Callable[[TargetRow], object],
            retry: Callable[[TargetRow], object], add: Callable[[str], object],
            delete: Callable[[TargetRow], object]) -> None:
    """The targets flnd knows.

    Args:
        follow: follow the target, or unfollow it when it is followed (`f`).
        retry: mark its exhausted failures retryable, for the next `get` (`r`).
        add: run `get` on a target typed into the form (`a`).
        delete: forget the target, after a confirmation (`d`).
    """
    fancy = theme.unicode(sys.stdout)

    def rows() -> list[Row]:
        return [Row(target, _target(target, fancy)) for target in load()]

    def ask(_: Any) -> None:
        typed = forms.text("Target to get")
        if typed:
            add(typed.strip())

    keys = (Key("f", "follow", _given(follow)), Key("r", "retry", _given(retry)),
            Key("a", "add", ask, leave=True))
    show("Targets", TARGET_COLUMNS, rows, keys=keys, delete=Key("d", "delete", _given(delete)))


def accounts(load: Callable[[], Sequence[AccountRow]], *, default: Callable[[AccountRow], object],
             add: Callable[[AccountRow | None], object], logout: Callable[[AccountRow], object]
             ) -> None:
    """Every account of every plugin.

    Args:
        default: make the account its plugin's default (`*`).
        add: open the login form, for the plugin of the account under the cursor (`a`).
        logout: log the account out, after a confirmation (`d`).
    """
    fancy = theme.unicode(sys.stdout)

    def rows() -> list[Row]:
        return [Row(account, _account(account, fancy)) for account in load()]

    keys = (Key("*", "default", _given(default)), Key("a", "add", add, leave=True))
    show("Accounts", ACCOUNT_COLUMNS, rows, keys=keys, delete=Key("d", "log out",
                                                                  _given(logout)))


def plugins(load: Callable[[], Sequence[PluginRow]], *, install: Callable[[str], object],
            uninstall: Callable[[PluginRow], object]) -> None:
    """The plugins: built in, then installed.

    Args:
        install: install the plugin at a path typed into the form (`a`).
        uninstall: uninstall an installed plugin, after a confirmation (`d`); a built-in has none.
    """
    def rows() -> list[Row]:
        return [Row(plugin, _plugin(plugin), fixed=plugin.builtin) for plugin in load()]

    def ask(_: Any) -> None:
        typed = forms.text("Path of the plugin")
        if typed:
            install(typed.strip())

    show("Plugins", PLUGIN_COLUMNS, rows, keys=(Key("a", "add", ask, leave=True),),
         delete=Key("d", "uninstall", _given(uninstall)))


def _given(run: Callable[[Any], object]) -> Callable[[Any], object]:
    """`run`, skipped in an empty list, where there is no row to give it."""
    return lambda row: None if row is None else run(row)


def _target(target: TargetRow, fancy: bool) -> tuple[Part, ...]:
    if target.followed:
        followed: Part = (f"{theme.glyph('followed', fancy)} followed", "good")
    else:
        followed = (theme.glyph("unfollowed", fancy), "dim")
    size: Part = _size(target.size) if target.size else ("–", "dim")
    return (target.name, followed, " ".join(target.categories), f"{target.files:,} files",
            size, _ago(target.ago, "never"))


def _account(account: AccountRow, fancy: bool) -> tuple[Part, ...]:
    star: Part = (f"{theme.glyph('default', fancy)} default", "focus") if account.default else ""
    saved = _ago(account.ago, "never saved")
    return (account.plugin, account.name, star, account.origin, _status(account),
            saved if isinstance(saved, tuple) else f"saved {saved}")


def _status(account: AccountRow) -> Part:
    if account.held:
        return (f"held {max(1, round(account.held / 60))} min", "warn")
    if account.expires is None:
        return ("ok", "good")
    if account.expires <= 0:
        return ("expired", "bad")
    if account.expires < SOON:
        days = max(1, round(account.expires / 86400))
        return (f"expires in {days} day{'s' if days != 1 else ''}", "warn")
    return ("ok", "good")


def _plugin(plugin: PluginRow) -> tuple[Part, ...]:
    kind: Part = ("built-in", "dim") if plugin.builtin else "installed"
    if plugin.disabled:
        kind = ("disabled", "bad")
    return (plugin.code, plugin.name, kind, str(plugin.api), " ".join(plugin.domains),
            f"{plugin.targets:,}")


def _size(n: int) -> str:
    from flnkit.text import size

    return size(n)


def _ago(seconds: float | None, never: str) -> Part:
    """`2h ago`: the largest whole unit of `seconds`, or `never` (dim) when None."""
    if seconds is None:
        return (never, "dim")
    for unit, length in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= length:
            return f"{int(seconds // length)}{unit} ago"
    return "just now"
