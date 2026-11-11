"""Function parameters as command-line arguments: annotations, conversion, and stdin."""

from __future__ import annotations

import argparse
import enum
import inspect
import re
import shlex
import sys
import types
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shared import _prompt as prompt
from shared._screen import isatty

REQUIRED = inspect.Parameter.empty
MANY_NARGS = ("*", "+")


class UsageError(Exception):
    """A command line the command cannot run with. Reported as `✗ message` and the usage, exit 2."""


@dataclass(frozen=True)
class Opt:
    flags: tuple = ()
    help: str | None = None
    metavar: str | None = None
    choices: tuple | None = None
    hidden: bool = False
    type: Callable[[str], Any] | None = None
    ask: Callable[[], Any] | None = None
    stdin: bool = False


@dataclass(frozen=True)
class Arg:
    help: str | None = None
    metavar: str | None = None
    nargs: Any = None
    choices: tuple | None = None
    type: Callable[[str], Any] | None = None
    ask: Callable[[], Any] | None = None
    stdin: bool = True


def opt(*flags: str, help: str | None = None, metavar: str | None = None, choices: Any = None, hidden: bool = False,
        type: Callable[[str], Any] | None = None, ask: Callable[[], Any] | None = None, stdin: bool = False) -> Opt:
    return Opt(flags, help, metavar, None if choices is None else tuple(choices), hidden, type, ask, stdin)


def arg(help: str | None = None, metavar: str | None = None, nargs: Any = None, choices: Any = None,
        type: Callable[[str], Any] | None = None, ask: Callable[[], Any] | None = None, stdin: bool = True) -> Arg:
    return Arg(help, metavar, nargs, None if choices is None else tuple(choices), type, ask, stdin)


def read_signature(fn: Callable) -> tuple[str, str, list[Param]]:
    """The docstring's summary and description, and one Param per parameter of fn."""
    summary, description, arg_help = _parse_docstring(fn.__doc__)
    return summary, description, _read_params(fn, inspect.signature(fn), arg_help)


def command_line(params: list[Param], values: dict) -> list[str]:
    """The cli-mode arguments that give these values."""
    options, positionals = [], []
    for param in params:
        value = values[param.name]
        if not param.required and _same(value, param.default):
            continue
        if param.positional:
            positionals += [token(item) for item in value] if param.many else [token(value)]
        elif param.kind == "flag":
            options.append(param.long_flag if value else "--no-" + param.long_flag[2:])
        elif param.repeat:
            for item in value:
                options += [param.long_flag, token(item)]
        else:
            options += [param.long_flag, token(value)]
    if any(word.startswith("-") for word in positionals):
        positionals.insert(0, "--")
    return options + positionals


def token(value: Any) -> str:
    """The command-line spelling of a value."""
    return str(value.value) if isinstance(value, enum.Enum) else str(value)


@dataclass
class Param:
    """One function parameter as a command-line argument."""

    name: str
    kind: str  # "positional", "option", or "flag"
    convert: Callable[[str], Any]
    default: Any = REQUIRED
    nargs: Any = None
    flags: tuple = ()
    collect: type | None = None  # list or tuple when the parameter takes many values
    repeat: bool = False  # a list option given as --tags a --tags b
    spread: bool = False  # *args
    keyword: bool = False  # keyword-only, so it is passed by name
    help: str = ""
    metavar: str = ""
    hidden: bool = False
    ask: Callable[[], Any] | None = None
    wants_stdin: bool = False
    stdin: bool = False
    choices: tuple = ()
    tokens: tuple = ()  # the command-line spelling of each choice

    @property
    def positional(self) -> bool:
        return self.kind == "positional"

    @property
    def many(self) -> bool:
        return self.collect is not None or self.nargs in MANY_NARGS or self.nargs is argparse.REMAINDER

    @property
    def required(self) -> bool:
        if self.positional:
            return self.nargs not in ("*", "?", argparse.REMAINDER) and not self.spread
        return self.default is REQUIRED and self.kind != "flag"

    @property
    def long_flag(self) -> str:
        return self.flags[-1]

    def label(self) -> str:
        if self.kind == "flag":
            return ", ".join(self.flags[:-1] + ("--[no-]" + self.long_flag[2:],))
        return f"{', '.join(self.flags)} {self.metavar}"

    def help_row(self) -> str:
        """The help line, with the values listed for a choice."""
        return f"{self.help} ({', '.join(self.tokens)})".strip() if self.tokens else self.help

    def usage(self) -> str:
        name = self.metavar
        if self.nargs == "?":
            return f"[{name}]"
        if self.nargs == "*" or self.nargs is argparse.REMAINDER or self.spread:
            return f"[{name}...]"
        if self.nargs == "+":
            return f"{name}..."
        return " ".join([name] * self.nargs) if isinstance(self.nargs, int) else name

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        if self.positional:
            # A positional that may read stdin accepts nothing here; finish() enforces "+".
            nargs = "*" if self.stdin and self.nargs == "+" else self.nargs
            if nargs is argparse.REMAINDER:
                parser.add_argument(self.name, nargs=nargs, metavar=self.metavar)
            else:
                default = None if self.default is REQUIRED else self.default
                parser.add_argument(self.name, nargs=nargs, type=self.convert, default=default, metavar=self.metavar)
        elif self.kind == "flag":
            default = False if self.default is REQUIRED else self.default
            parser.add_argument(*self.flags, dest=self.name, action=argparse.BooleanOptionalAction, default=default)
        else:
            parser.add_argument(
                *self.flags, dest=self.name, type=self.convert, metavar=self.metavar,
                action="append" if self.repeat else "store", required=self.required, default=None,
            )

    def finish(self, value: Any) -> Any:
        """Turn a parsed value into the value the function gets: stdin, defaults, and collections."""
        if value is None or value == []:
            if self.stdin:
                value = _read_stdin(self)
            if (value is None or value == []) and self.positional and self.nargs == "+":
                raise UsageError(f"the following arguments are required: {self.metavar}")
        if value is None or (value == [] and self.default is not REQUIRED and not self.positional):
            value = None if self.default is REQUIRED else self.default
            if isinstance(value, (list, set, dict)):
                value = type(value)(value)
        if self.spread:
            return tuple(value or ())
        if self.collect is not None and value is not None:
            return self.collect(value)
        return value

    def ask_generated(self) -> Any:
        """Ask for this parameter with a question chosen from its type."""
        text = self.help or self.name
        has_default = self.default is not REQUIRED and self.default is not None
        if self.kind == "flag":
            return prompt.confirm(text, default=bool(self.default) if has_default else False)
        if self.tokens:
            options = dict(zip(self.tokens, self.choices))
            return prompt.choice(text, options, default=self.default if has_default else None)
        if self.many:
            start = " ".join(shlex.quote(token(item)) for item in self.default) if has_default else None
            raw = prompt.input(text, default=start, validate=lambda raw: _problem(self._split, raw))
            return self.finish(self._split(raw))
        start = token(self.default) if has_default else None
        raw = prompt.input(text, default=start, validate=lambda raw: _problem(self.convert, raw))
        return self.convert(raw)

    def _split(self, raw: str) -> list:
        items = shlex.split(raw)
        if not items and self.nargs == "+":
            raise ValueError("enter at least one value")
        return [self.convert(item) for item in items]


def _read_params(fn: Callable, signature: inspect.Signature, arg_help: dict) -> list[Param]:
    hints = typing.get_type_hints(fn, include_extras=True)
    parameters = list(signature.parameters.values())
    params = []
    for position, parameter in enumerate(parameters):
        if parameter.kind is parameter.VAR_KEYWORD:
            raise TypeError(f"{fn.__name__}: **{parameter.name} cannot be a command-line argument")
        is_last = position == len(parameters) - 1
        params.append(_read_param(fn, parameter, hints.get(parameter.name, str), arg_help, is_last))
    stdin_options = [param for param in params if param.kind == "option" and param.wants_stdin]
    if len(stdin_options) > 1:
        raise ValueError(f"{fn.__name__}: only one parameter can read stdin")
    variadic = next((param for param in params if param.positional and param.nargs in MANY_NARGS), None)
    reader = stdin_options[0] if stdin_options else variadic if variadic and variadic.wants_stdin else None
    if reader is not None:
        reader.stdin = True
    return params


def _read_param(fn: Callable, parameter: inspect.Parameter, annotation: Any, arg_help: dict,
                is_last: bool) -> Param:
    name = parameter.name
    # Python 3.10 wraps a parameter that defaults to None in Optional, outside its Annotated.
    annotation = _without_none(annotation)
    annotation, extras = (typing.get_args(annotation)[0], typing.get_args(annotation)[1:]) \
        if typing.get_origin(annotation) is typing.Annotated else (annotation, ())
    meta = next((extra for extra in extras if isinstance(extra, (Opt, Arg))), None)
    flags = [extra for extra in extras if isinstance(extra, str) and extra.startswith("-")]
    nargs = next((extra for extra in extras if extra in MANY_NARGS + ("?",) or type(extra) is int), None)
    if isinstance(meta, Arg) and meta.nargs is not None:
        nargs = meta.nargs
    annotation = _without_none(annotation)

    collect = None
    origin = typing.get_origin(annotation)
    if origin in (list, tuple):
        item_types = [item for item in typing.get_args(annotation) if item is not Ellipsis]
        collect, annotation = origin, (item_types[0] if item_types else str)
    choices: tuple = ()
    if typing.get_origin(annotation) is typing.Literal:
        choices = typing.get_args(annotation)
    elif isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        choices = tuple(annotation)
    if meta is not None and meta.choices is not None:
        choices = meta.choices
    custom_type = meta.type if meta is not None else None
    if not (custom_type or choices or annotation in (str, int, float, bool, Path)):
        raise TypeError(f"{fn.__name__}: cannot read parameter {name!r} of type {annotation!r} from the command line")
    tokens = tuple(token(choice) for choice in choices)
    convert = _converter(custom_type or annotation, choices, tokens)

    has_default = parameter.default is not REQUIRED
    spread = parameter.kind is parameter.VAR_POSITIONAL
    keyword = parameter.kind is parameter.KEYWORD_ONLY
    rest = collect is tuple and is_last and not keyword and has_default and nargs is None
    if spread:
        nargs = "*"
    elif rest:
        nargs = argparse.REMAINDER
    positional = (spread or rest or isinstance(meta, Arg) or nargs is not None
                  or (not has_default and not keyword and not isinstance(meta, Opt) and annotation is not bool))
    if positional and nargs is None:
        nargs = "+" if collect else "?" if has_default else None
    kind = "positional" if positional else "flag" if annotation is bool and not custom_type else "option"

    long_flag = "--" + name.replace("_", "-")
    spellings = flags + list(meta.flags if isinstance(meta, Opt) else ())
    shorts = [flag for flag in dict.fromkeys(spellings) if flag != long_flag]
    help_text = (meta.help if meta is not None and meta.help is not None else arg_help.get(name, ""))
    default_metavar = name if positional else "N" if annotation in (int, float) else name.upper()
    return Param(
        name=name, kind=kind, convert=convert, default=parameter.default if has_default else REQUIRED,
        nargs=nargs if positional else None, flags=tuple(sorted(shorts, key=len) + [long_flag]),
        collect=None if spread else collect, repeat=bool(collect) and not positional, spread=spread,
        keyword=keyword, help=help_text, metavar=(meta.metavar if meta and meta.metavar else default_metavar),
        hidden=isinstance(meta, Opt) and meta.hidden, ask=meta.ask if meta is not None else None,
        wants_stdin=meta.stdin if meta is not None else positional, choices=choices, tokens=tokens,
    )


def _converter(base: Any, choices: tuple, tokens: tuple) -> Callable[[str], Any]:
    if not choices:
        return base
    by_token = dict(zip(tokens, choices))

    def to_choice(text: str) -> Any:
        if text not in by_token:
            raise argparse.ArgumentTypeError(f"invalid choice: {text!r} (choose from {', '.join(tokens)})")
        return by_token[text]

    return to_choice


def _parse_docstring(doc: str | None) -> tuple[str, str, dict]:
    """The summary line, the description up to `Args:`, and each `name: text` under it."""
    lines = inspect.cleandoc(doc or "").splitlines()
    summary = lines[0].strip() if lines else ""
    body, arg_help, current, in_args = [], {}, None, False
    for line in lines[1:]:
        if line.strip() == "Args:":
            in_args = True
        elif in_args and line and not line[0].isspace():
            in_args = False
            body.append(line)
        elif in_args:
            match = re.match(r"\s+\**(\w+)(?:\s*\([^)]*\))?:\s*(.*)", line)
            if match:
                current = match[1]
                arg_help[current] = match[2].strip()
            elif current and line.strip():
                arg_help[current] += " " + line.strip()
        else:
            body.append(line)
    return summary, "\n".join(body).strip(), arg_help


def _without_none(annotation: Any) -> Any:
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        real = [arg for arg in args if arg is not type(None)]
        if len(real) == 1:
            return real[0]
    return annotation


def _read_stdin(param: Param) -> Any:
    stream = sys.stdin
    if stream is None or isatty(stream):
        return None
    if not param.positional:
        raw = stream.buffer.read().decode("utf-8") if hasattr(stream, "buffer") else stream.read()
        return param.convert(raw) if raw else None
    lines = [line.strip() for line in stream.read().splitlines()]
    values = []
    for line in lines:
        if line and not line.startswith("#"):
            try:
                values.append(param.convert(line))
            except (ValueError, TypeError, argparse.ArgumentTypeError) as error:
                raise UsageError(f"argument {param.metavar}: invalid value {line!r}: {error}") from None
    return values


def _problem(convert: Callable[[str], Any], raw: str) -> str | None:
    try:
        convert(raw)
    except (ValueError, TypeError, argparse.ArgumentTypeError) as error:
        return str(error) or "invalid value"
    return None


def _same(value: Any, default: Any) -> bool:
    if default is REQUIRED:
        return False
    if isinstance(value, (list, tuple)) and isinstance(default, (list, tuple)):
        return list(value) == list(default)
    return value == default
