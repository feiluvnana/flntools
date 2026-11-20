"""config.toml: its structs, how a setting resolves, editing it, and the name templates.

COOKBOOK.md#config explains every key ("config.toml").
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import os
import re
import sys
import tomllib
import typing
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from flnd.errors import UsageError
from flnd.plugin import Range
from flnd.ui.log import ui
from flnkit import clock, files, text

FILE = "config.toml"
# While set (make test sets it), the real user file is refused, so a test never reads or writes
# the account's own settings.
NO_PROFILES = "FLND_TEST_NO_PROFILES"
# The fields the engine fills for every file, which a template may use beside a plugin's own.
FIELDS = ("plugin", "target", "category", "post", "num", "count", "index", "extension", "path",
          "date", "query")
# The longest file name most file systems take, in UTF-8 bytes.
NAME_MAX = 255
# A file name's extension, kept whole when a name is cut: a dot and up to 16 word characters.
EXTENSION = re.compile(r"\.\w{1,16}$")
PROXIES = ("http", "https", "socks5", "socks5h")
BROWSERS = ("chromium", "chrome")
# A speed: `"0"` for off, or a number and a unit per second, in units of 1024 (as `text.size`).
SPEED = re.compile(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB)/s", re.IGNORECASE)
UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}
WHEN_USAGE = ("when takes a year (2026), a month (2026-06), a day (2026-06-15), a range of "
              "those (2026-01..2026-03, 2026-01.., ..2026-03), or a span back from today "
              "(30d, 8w, 1y)")
PERIOD = re.compile(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?")
SPAN = re.compile(r"(\d+)([dwy])")
SPAN_DAYS = {"d": 1, "w": 7, "y": 365}
# The warnings about the files said so far (`_warn`).
WARNED: set[str] = set()



class Where(enum.StrEnum):
    """Where a setting's value was read from."""

    ARGUMENT = "argument"
    HOME = "home"
    USER = "user"
    DEFAULT = "default"


class Defaults(msgspec.Struct, frozen=True):
    """The `[defaults]` table (COOKBOOK.md#config)."""

    dedupe: bool = True
    sidecars: bool = False
    when: str = ""
    exec: tuple[str, ...] = ()
    rate: str = "0"
    slowest: str = "0"
    height: int = 0
    switch: bool = False


class PluginConfig(msgspec.Struct, frozen=True):
    """One `[plugins.<code>]` table; None leaves a key to the plugin's class."""

    pace: float | None = None
    limits: dict[str, int] = msgspec.field(default_factory=dict)
    proxy: str | None = None
    stop_after: int = 5
    browser: str | None = None
    switch: bool = False
    folder: str | None = None
    filename: str | None = None
    # The plugin's own `Settings` struct, or None for a plugin flnd wasn't given.
    settings: Any = None


class Config(msgspec.Struct, frozen=True):
    """Every setting, resolved, and where each value came from."""

    defaults: Defaults = Defaults()
    plugins: dict[str, PluginConfig] = msgspec.field(default_factory=dict)
    # The dotted name (`defaults.when`, `plugins.ins.pace`) of each value not left default.
    where: dict[str, Where] = msgspec.field(default_factory=dict)

    def plugin(self, code: str) -> PluginConfig:
        """One plugin's settings, or every default for a code no file names."""
        return self.plugins.get(code) or PluginConfig(switch=self.defaults.switch)

    def origin(self, key: str) -> Where:
        """Where a dotted name's value came from (`when` is `defaults.when`)."""
        return self.where.get(_dotted(key), Where.DEFAULT)


# ---------------------------------------------------------------------------------------------
# Checking one value
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    """One engine key: the type msgspec checks, the words for it, and a further check."""

    kind: Any
    allowed: str
    check: Callable[[Any], bool] = lambda value: True


def _speed_ok(value: str) -> bool:
    return value.strip() == "0" or SPEED.fullmatch(value.strip()) is not None


def _when_ok(value: str) -> bool:
    try:
        when(value)
    except UsageError:
        return False
    return True


def _proxy_ok(value: str) -> bool:
    if any(character.isspace() for character in value):
        return False
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port  # noqa: B018 - a bad port raises here
    except ValueError:  # a bad port or a broken [IPv6] host
        return False
    return parts.scheme in PROXIES and bool(parts.hostname)


DEFAULTS = {
    "dedupe": Key(bool, "true or false"),
    "sidecars": Key(bool, "true or false"),
    "when": Key(str, 'text such as "1y", "2026" or "2026-01..2026-03", or ""', _when_ok),
    "exec": Key(tuple[str, ...], 'a list: a program and its arguments, ["<program>", ...]',
                lambda value: all(value) and (not value or bool(value[0].strip()))),
    "rate": Key(str, 'a speed such as "20 MB/s" or "500 KB/s", or "0" for off', _speed_ok),
    "slowest": Key(str, 'a speed such as "50 KB/s", or "0" for off', _speed_ok),
    "height": Key(int, "a whole number of pixels, 0 for the best", lambda value: value >= 0),
    "switch": Key(bool, "true or false"),
}
PLUGINS = {
    "pace": Key(float, "a number of seconds, 0 or more", lambda value: value >= 0),
    "limits": Key(dict[str, int], 'a table of "<host>" = <files at once, at least 1>',
                  lambda value: all(host and n >= 1 for host, n in value.items())),
    "proxy": Key(str, " or ".join(f'"{scheme}://<host>:<port>"' for scheme in PROXIES),
                 _proxy_ok),
    "stop_after": Key(int, "a whole number of at least 1", lambda value: value >= 1),
    "browser": Key(str, '"chromium" or "chrome"', lambda value: value in BROWSERS),
    "switch": Key(bool, "true or false"),
    "folder": Key(str, "a template"),
    "filename": Key(str, "a template"),
}


def _checked(label: str, key: str, spec: Key, value: Any) -> Any:
    """`value` as `spec.kind`, or a `UsageError` naming the file, the key and what it takes."""
    try:
        found = msgspec.convert(value, spec.kind, strict=True)
    except msgspec.ValidationError:
        found = None
    if found is None or not spec.check(found):
        raise bad(label, key, spec.allowed, value)
    return found


def bad(label: str, key: str, allowed: str, value: Any) -> UsageError:
    """The error for a key with a value it doesn't take.

    No message here ever holds a value the user wrote, which may be a proxy's password: it
    names the key, what the key takes, and only the kind of value found.
    """
    return UsageError(f"{label}: {key} is {allowed}, not this {_kind(value)}",
                      hint=f"fix {key} in {label}, or remove it for the default")


def _kind(value: Any) -> str:
    """What sort of TOML value `value` is, in words, without its content."""
    for kinds, word in ((bool, "true/false value"), (int, "whole number"), (float, "number"),
                        (str, "text"), ((list, tuple), "list"), (dict, "table"),
                        ((datetime.date, datetime.time), "date or time")):
        if isinstance(value, kinds):
            return word
    return "value"


def _syntax(path: Path, error: Exception) -> UsageError:
    """A TOML syntax error naming only the line and column, never the text found there."""
    found = re.search(r"line (\d+)(?:, column| col) (\d+)", str(error))
    where = f" (line {found[1]}, column {found[2]})" if found else ""
    return UsageError(f"{path}: not valid TOML{where}", hint="fix the file, or move it away")


def when(value: str | None) -> Range:
    """Read `when`: a period, a range of periods, or a span back from today.

    Args:
        value: `"2026"`, `"2026-06"`, `"2026-06-15"`, `"2026-01..2026-03"` (either end may be
            left out), or `"30d"`, `"8w"`, `"1y"`; None or "" for every day.

    Raises:
        UsageError: it is none of those, or the range ends before it starts.
    """
    value = (value or "").strip()
    if not value:
        return Range()
    if span := SPAN.fullmatch(value):
        today = datetime.date.fromtimestamp(clock.wall())
        return Range(since=today - datetime.timedelta(days=int(span[1]) * SPAN_DAYS[span[2]]))
    first, dots, last = value.partition("..")
    if not dots:
        return Range(*_period(value))
    since = _period(first)[0] if first.strip() else None
    until = _period(last)[1] if last.strip() else None
    if since is not None and until is not None and since > until:
        raise UsageError(WHEN_USAGE)
    return Range(since, until)


def _period(value: str) -> tuple[datetime.date, datetime.date]:
    """The first and last day of a year, a month or a day."""
    found = PERIOD.fullmatch(value.strip())
    if found is None:
        raise UsageError(WHEN_USAGE)
    year, month, day = (int(part) if part else 0 for part in found.groups())
    try:
        if day:
            return datetime.date(year, month, day), datetime.date(year, month, day)
        if month:
            following = datetime.date(year + (month == 12), month % 12 + 1, 1)
            return datetime.date(year, month, 1), following - datetime.timedelta(days=1)
        return datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    except ValueError:
        raise UsageError(WHEN_USAGE) from None


def speed(value: str) -> int:
    """Bytes per second of a `rate` or `slowest` value; 0 for off."""
    found = SPEED.fullmatch(value.strip())
    return 0 if found is None else int(float(found[1]) * UNITS[found[2].lower()])


# ---------------------------------------------------------------------------------------------
# Templates and names
# ---------------------------------------------------------------------------------------------


def template(label: str, key: str, value: str, fields: tuple[str, ...] | None) -> None:
    """Check a folder or filename template before any request.

    Args:
        label: the file or call it came from, for the message.
        key: its dotted name.
        value: the template.
        fields: the fields it may use besides the engine's; None skips the field check (a
            plugin flnd wasn't given).

    Raises:
        UsageError: it leaves the target's folder, can't be parsed, or names an unknown field.
    """
    if (not isinstance(value, str)
            or "\\" in value
            or value.startswith("/")
            or value.endswith("/")
            or re.search(r"[\x00-\x1f\x7f-\x9f]", value)
            or any(re.match(r"[A-Za-z]:", part) or part.strip() in (".", "..")
                   or (value and not part.strip())
                   for part in value.split("/"))):
        raise bad(label, key, "a template inside the target's folder", value)
    try:
        used = text.fields(value)
    except ValueError:
        raise bad(label, key, "a template whose braces each hold one field name", value) from None
    unknown = sorted(used - {*FIELDS, *fields}) if fields is not None else []
    if unknown:
        known = ", ".join(sorted({*FIELDS, *fields}))
        raise bad(label, key, f"a template of the fields {known}", value)


def check_folder(value: str, sources: Mapping[str, type],
                 targets: Sequence[Any] = ()) -> None:
    """Validate a `--folder` / `folder=` template against path safety and each target's plugin.

    Args:
        value: the folder template.
        sources: plugin classes by code, consulted only when plugin-specific fields are named.
        targets: resolved targets (`Chosen` or `Target`); empty checks syntax and path safety.

    Raises:
        UsageError: `value` escapes `media/<plugin>/`, has bad braces, or names an unknown field.
    """
    template("the call", "folder", value, None)
    used = text.fields(value)
    if not (used - set(FIELDS)):
        return
    codes = dict.fromkeys(
        item.target.plugin if hasattr(item, "target") else item.plugin for item in targets
    )
    for code in codes:
        cls = sources.get(code)
        fields = tuple(getattr(cls, "fields", ())) if cls is not None else None
        template("the call", "folder", value, fields)


def cut(name: str, limit: int = NAME_MAX) -> str:
    """`name` cut to `limit` UTF-8 bytes, keeping its extension, never inside a character."""
    if len(name.encode()) <= limit:
        return name
    found = EXTENSION.search(name)
    extension = found[0] if found else ""
    stem = name[: len(name) - len(extension)]
    room = limit - len(extension.encode())
    return stem.encode()[:room].decode("utf-8", "ignore") + extension


# ---------------------------------------------------------------------------------------------
# Reading and resolving
# ---------------------------------------------------------------------------------------------


def user_file() -> Path:
    """The user file: `~/.config/flnd/config.toml`, or `%APPDATA%\\flnd\\config.toml`."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "flnd" / FILE
    return Path.home() / ".config" / "flnd" / FILE


def _guard(path: Path) -> None:
    """Refuse the account's real user file while the tests run."""
    if not os.environ.get(NO_PROFILES):
        return
    if sys.platform == "win32":
        real = Path(os.environ.get("APPDATA") or Path.home()) / "flnd"
    else:
        import pwd

        # The account's own home, not $HOME, which a test may point elsewhere.
        real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "flnd"
    if path.resolve().is_relative_to(real.resolve()):
        raise RuntimeError(f"{NO_PROFILES} is set: refusing the real user config {path}")


def settings(home: Path, *, plugins: Mapping[str, type] | None = None,
             given: Mapping[str, Any] | None = None) -> Config:
    """Every setting of a home, with the user's own file (`user_file`): what a run reads.

    Under `FLND_TEST_NO_PROFILES` the account's real user file is never read, so it adds none.

    Args:
        plugins: the plugin classes by code, as `read` takes them; a lazy mapping imports only
            the plugins a file names.

    Raises:
        UsageError: a file isn't TOML, or a value is one its key doesn't take.
    """
    user: Path | None = user_file()
    try:
        _guard(user)
    except RuntimeError:
        user = None
    return read(home, user=user, plugins=plugins, given=given)


def read(home: Path, *, user: Path | None, plugins: Mapping[str, type] | None = None,
         given: Mapping[str, Any] | None = None) -> Config:
    """Read and resolve every setting: `given`, then the home's file, the user file, the default.

    Args:
        home: the home folder; its `config.toml` may be missing.
        user: the user file (`user_file()` outside tests); it may be missing; None reads none.
        plugins: the plugin classes by code, whose `fields`, `Settings` and `browser` check
            their tables; a code not here has only the engine's keys. Only a plain `dict`'s
            codes all resolve; another mapping is asked only for the codes a file names.
        given: values for one call, by dotted name (`"when"`, `"plugins.ins.pace"`).

    Raises:
        UsageError: a file isn't TOML, or a value is one its key doesn't take.
    """
    plugins = {} if plugins is None else plugins
    called = _from_dotted(given or {})
    if "exec" in called.get("defaults", {}):
        raise UsageError("defaults.exec is read only from the user file, not from a call",
                         hint=f"set it in {user or user_file()}")
    layers = [
        (Where.ARGUMENT, "the call", called),
        (Where.HOME, str(home / FILE), _load(home / FILE, Where.HOME)),
        *([(Where.USER, str(user), _load(user, Where.USER))] if user is not None else []),
    ]
    values: dict[str, Any] = {}
    where: dict[str, Where] = {}
    # A plain mapping's codes all resolve; a lazy one (the registry) only those a layer names,
    # so no plugin is imported for nothing.
    codes = set(plugins) if isinstance(plugins, dict) else set()
    for origin, label, data in reversed(layers):
        for key, value in _checked_layer(label, data, plugins).items():
            values[key], where[key] = value, origin
        codes |= set(data.get("plugins", {}))
    defaults = Defaults(**{key.removeprefix("defaults."): value for key, value in values.items()
                           if key.startswith("defaults.")})
    resolved = {code: _plugin(code, values, plugins.get(code), defaults) for code in codes}
    for code in codes:
        if f"plugins.{code}.switch" not in where and "defaults.switch" in where:
            where[f"plugins.{code}.switch"] = where["defaults.switch"]
    return Config(defaults=defaults, plugins=resolved, where=where)


def _load(path: Path, origin: Where) -> dict[str, Any]:
    """One file's tables, `[sources.*]` read as `[plugins.*]`; `{}` when it is missing."""
    _guard(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError:
        raise UsageError(f"{path}: not UTF-8 text", hint="save it as UTF-8") from None
    except OSError as error:
        raise UsageError(f"{path}: {error.strerror}",
                         hint="fix the file, or move it away") from None
    except tomllib.TOMLDecodeError as error:
        raise _syntax(path, error) from None
    old = data.pop("sources", None)
    if old is not None and not isinstance(old, dict):
        _warn("{path}: sources is not a table; it is ignored", path=str(path))
    if isinstance(old, dict):
        _warn("{path}: [sources.<code>] is now [plugins.<code>]; rename it", path=str(path))
        plugins = data.setdefault("plugins", {})
        for code, table in old.items():
            own = plugins.get(code, {}) if isinstance(plugins, dict) else None
            if not isinstance(table, dict):
                _warn("{path}: [sources] {code} is not a table; it is ignored",
                        path=str(path), code=code)
            elif isinstance(own, dict):
                plugins[code] = {**table, **own}
    defaults = data.get("defaults")
    if origin is Where.HOME and isinstance(defaults, dict) and "exec" in defaults:
        _warn("{path}: [defaults] exec is read only from the user file; it is ignored",
                path=str(path))
        del defaults["exec"]
    return data


def _from_dotted(given: Mapping[str, Any]) -> dict[str, Any]:
    """Dotted names and their values as the nested tables a file would hold."""
    data: dict[str, Any] = {}
    for key, value in given.items():
        *tables, name = _dotted(key).split(".")
        place = data
        for i, table in enumerate(tables):
            place = place.setdefault(table, {})
            _table("the call", ".".join(tables[: i + 1]), place)
        place[name] = value
    return data


def _dotted(key: str) -> str:
    return key if "." in key else f"defaults.{key}"


def _checked_layer(label: str, data: Mapping[str, Any],
                   plugins: Mapping[str, type]) -> dict[str, Any]:
    """Check one file's values, by dotted name, warning about each key flnd doesn't take."""
    found: dict[str, Any] = {}
    for name, table in data.items():
        if name not in ("defaults", "plugins"):
            _unknown(label, f"[{name}]")
            continue
        _table(label, name, table)
        if name == "defaults":
            found |= _keys(label, "defaults", table, DEFAULTS)
            continue
        for code, own in table.items():
            _table(label, f"plugins.{code}", own)
            found |= _plugin_keys(label, code, own, plugins.get(code))
    return found


def _plugin_keys(label: str, code: str, table: Mapping[str, Any],
                 cls: type | None) -> dict[str, Any]:
    where = f"plugins.{code}"
    settings = _settings_fields(cls)
    keys = {**PLUGINS, **{name: Key(kind, f"of type {_type_name(kind)}")
                          for name, kind in settings.items()}}
    found = _keys(label, where, table, keys)
    fields = tuple(getattr(cls, "fields", ())) if cls is not None else None
    for name in ("folder", "filename"):
        if f"{where}.{name}" in found:
            template(label, f"{where}.{name}", found[f"{where}.{name}"], fields)
    kind = getattr(getattr(cls, "browser", None), "name", None)
    if f"{where}.browser" in found and kind is not None and kind != "ANY":
        raise bad(label, f"{where}.browser", "set only for a plugin whose browser is ANY",
                  found[f"{where}.browser"])
    return found


def _keys(label: str, where: str, table: Mapping[str, Any],
          keys: Mapping[str, Key]) -> dict[str, Any]:
    found = {}
    for name, value in table.items():
        if name in keys:
            found[f"{where}.{name}"] = _value(label, f"{where}.{name}", keys[name], value)
        else:
            _unknown(label, f"[{where}] {name}")
    return found


def _value(label: str, key: str, spec: Key, value: Any) -> Any:
    if key == "defaults.dedupe" and value in ("link", "off"):
        _warn('{label}: dedupe = "{value}" is now dedupe = {flag}', label=label, value=value,
                flag="true" if value == "link" else "false")
        return value == "link"
    return _checked(label, key, spec, value)


def _table(label: str, name: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise UsageError(f"{label}: {name} is a table ([{name}]), not this {_kind(value)}",
                         hint="fix the file, or move it away")


def _unknown(label: str, setting: str) -> None:
    _warn("{label}: {setting} is not a setting flnd knows; it is ignored", label=label,
            setting=setting)


def _warn(message: str, **fields: str) -> None:
    """A warning about a file, said once per process: the library reads a home's settings,
    then its run reads them again."""
    said = message.format(**fields)
    if said not in WARNED:
        WARNED.add(said)
        ui.warn(message, **fields)


def _settings_fields(cls: type | None) -> dict[str, Any]:
    """A plugin's own keys and their types: its `Settings`, a dataclass (`flnd.plugin`) or a
    msgspec struct."""
    settings = getattr(cls, "Settings", None)
    if settings is None:
        return {}
    if dataclasses.is_dataclass(settings):
        hints = typing.get_type_hints(settings)
        return {field.name: hints.get(field.name, Any) for field in dataclasses.fields(settings)}
    return {field.name: field.type for field in msgspec.structs.fields(settings)}


def _type_name(kind: Any) -> str:
    return getattr(kind, "__name__", None) or str(kind)


def _plugin(code: str, values: Mapping[str, Any], cls: type | None,
            defaults: Defaults) -> PluginConfig:
    prefix = f"plugins.{code}."
    own = {key.removeprefix(prefix): value for key, value in values.items()
           if key.startswith(prefix)}
    engine = {name: own.pop(name) for name in list(own) if name in PLUGINS}
    engine.setdefault("switch", defaults.switch)
    settings_class = getattr(cls, "Settings", None)
    settings = msgspec.convert(own, settings_class) if settings_class is not None else None
    return PluginConfig(**engine, settings=settings)


# ---------------------------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------------------------


def write(key: str, value: Any, *, home: Path, user: Path,
          plugins: Mapping[str, type] | None = None) -> Path:
    """Set one key: `exec` in the user file, any other in the home's; comments are kept.

    Args:
        key: a dotted name (`"defaults.when"`, `"plugins.ins.pace"`); a bare name is in
            `[defaults]`.
        value: the TOML value.
        home: the home folder.
        user: the user file.
        plugins: the plugin classes by code, as `read` takes them.

    Returns:
        The file written.

    Raises:
        UsageError: the key isn't one flnd takes, or the value is one it doesn't take; the file
            is left as it was.
    """
    return _edit(key, value, home=home, user=user, plugins=plugins or {})


def unset(key: str, *, home: Path, user: Path,
          plugins: Mapping[str, type] | None = None) -> Path:
    """Remove one key from its file, so it takes the next file's value or the default.

    Args:
        plugins: the plugin classes by code, as `read` takes them, so a plugin's own key is
            known.
    """
    return _edit(key, None, home=home, user=user, plugins=plugins or {})


def _edit(key: str, value: Any, *, home: Path, user: Path,
          plugins: Mapping[str, type]) -> Path:
    import tomlkit

    tables, name = _known(key, plugins)
    path = user if tables == ["defaults"] and name == "exec" else home / FILE
    _guard(path)
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = tomlkit.document()
    except UnicodeDecodeError:
        raise UsageError(f"{path}: not UTF-8 text", hint="save it as UTF-8") from None
    except OSError as error:
        raise UsageError(f"{path}: {error.strerror}",
                         hint="fix the file, or move it away") from None
    except Exception as error:  # tomlkit's parse errors share no base but Exception
        raise _syntax(path, error) from None
    if value is None:
        _remove(path, document, tables, name)
    else:
        place = document
        for i, table in enumerate(tables):
            if table not in place:
                place[table] = tomlkit.table(is_super_table=i < len(tables) - 1)
            place = place[table]
            _table(str(path), ".".join(tables[: i + 1]), place)
        try:
            place[name] = value
        except (TypeError, ValueError):  # tomlkit's ConvertError is a TypeError and ValueError
            raise UsageError(f"{key}: a {type(value).__name__} isn't a TOML value",
                             hint="give text, a number, true or false, a list or a table") \
                from None
    content = tomlkit.dumps(document)
    origin = Where.USER if path == user else Where.HOME
    _checked_layer(str(path), _without_exec(tomllib.loads(content), origin), plugins)
    files.write(path, content)
    return path


def _known(key: str, plugins: Mapping[str, type]) -> tuple[list[str], str]:
    """A dotted name's tables and key, refused when flnd doesn't take it."""
    parts = _dotted(key).split(".")
    if parts[0] == "defaults" and len(parts) == 2 and parts[1] in DEFAULTS:
        return parts[:1], parts[1]
    if parts[0] == "plugins" and len(parts) == 3:
        code, name = parts[1], parts[2]
        if name in PLUGINS or name in _settings_fields(plugins.get(code)):
            return parts[:2], name
    raise UsageError(f"{key} is not a setting flnd knows",
                     hint="see COOKBOOK.md#config for every key of config.toml")


def _without_exec(data: dict[str, Any], origin: Where) -> dict[str, Any]:
    defaults = data.get("defaults")
    if origin is Where.HOME and isinstance(defaults, dict):
        defaults.pop("exec", None)
    return data


def _remove(path: Path, document: Any, tables: list[str], name: str) -> None:
    places = [document]
    for table in tables:
        if table not in places[-1]:
            return
        places.append(places[-1][table])
        _table(str(path), ".".join(tables[: len(places) - 1]), places[-1])
    places[-1].pop(name, None)
    for parent, table in zip(reversed(places[:-1]), reversed(tables), strict=True):
        if len(parent[table]) == 0:
            del parent[table]
