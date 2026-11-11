"""Function parameters as command-line arguments: annotations, conversion, and stdin."""

from __future__ import annotations

import argparse
import inspect
import re
import sys
import types
import typing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from shared._screen import isatty

REQUIRED = inspect.Parameter.empty
MANY_NARGS = ("*", "+")
NARGS = MANY_NARGS + ("?",)
# The types read without a type=: each is its own conversion except the ISO dates.
PLAIN_TYPES = (str, int, float, bool, Path)
ISO_FORMATS = {date: "YYYY-MM-DD", datetime: "YYYY-MM-DDTHH:MM"}


class UsageError(Exception):
    """A command line the command cannot run with. Reported as `✗ message` and the usage, exit 2."""

    usage: str = ""


@dataclass(frozen=True)
class Opt:
    flags: tuple = ()
    help: str | None = None
    metavar: str | None = None
    choices: tuple | None = None
    type: Callable[[str], Any] | None = None
    stdin: bool = False


@dataclass(frozen=True)
class Arg:
    help: str | None = None
    metavar: str | None = None
    nargs: Any = None
    choices: tuple | None = None
    type: Callable[[str], Any] | None = None
    stdin: bool = True


def opt(*flags: str, help: str | None = None, metavar: str | None = None, choices: Any = None,
        type: Callable[[str], Any] | None = None, stdin: bool = False) -> Opt:
    """An option's spellings and settings. A long flag such as "--from" replaces the parameter's own name."""
    return Opt(flags, help, metavar, None if choices is None else tuple(choices), type, stdin)


def arg(help: str | None = None, metavar: str | None = None, nargs: str | None = None, choices: Any = None,
        type: Callable[[str], Any] | None = None, stdin: bool = True) -> Arg:
    """A positional's settings. nargs is "?", "*" or "+"."""
    if nargs is not None and nargs not in NARGS:
        raise TypeError(f"nargs must be one of {', '.join(NARGS)}, not {nargs!r}")
    return Arg(help, metavar, nargs, None if choices is None else tuple(choices), type, stdin)


def read_signature(fn: Callable) -> tuple[str, str, list[Param]]:
    """The docstring's summary and description, and one Param per parameter of fn."""
    summary, description, arg_help = _parse_docstring(fn.__doc__)
    return summary, description, _read_params(fn, inspect.signature(fn), arg_help)


@dataclass
class Param:
    """One function parameter as a command-line argument."""

    name: str
    kind: str  # "positional", "option", or "flag"
    convert: Callable[[str], Any]
    default: Any = REQUIRED
    nargs: Any = None
    flags: tuple = ()
    collect: type | None = None  # list when the parameter takes many values
    repeat: bool = False  # a list option given as --tags a,b or --tags a --tags b
    spread: bool = False  # *args
    keyword: bool = False  # keyword-only, so it is passed by name
    help: str = ""
    metavar: str = ""
    wants_stdin: bool = False
    stdin: bool = False
    choices: tuple = ()
    tokens: tuple = ()  # the command-line spelling of each choice

    @property
    def positional(self) -> bool:
        return self.kind == "positional"

    @property
    def many(self) -> bool:
        return self.collect is not None or self.nargs in MANY_NARGS

    @property
    def required(self) -> bool:
        if self.positional:
            return self.nargs not in ("*", "?") and not self.spread
        return self.default is REQUIRED and self.kind != "flag"

    @property
    def long_flag(self) -> str:
        return self.flags[-1]

    @property
    def parsed_default(self) -> Any:
        """The value argparse stores when the command line leaves this parameter out."""
        if self.kind == "option" or self.default is REQUIRED:
            return False if self.kind == "flag" else None
        return self.default

    def is_given(self, value: Any) -> bool:
        """Whether the command line set this parameter, judged by its parsed value."""
        # A flag always parses to a bool, so "not None" would count every flag as given.
        return value is not None and value != [] and value != self.parsed_default

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
        if self.nargs == "*" or self.spread:
            return f"[{name}...]"
        return f"{name}..." if self.nargs == "+" else name

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        if self.positional:
            # A positional that may read stdin accepts nothing here; finish() enforces "+".
            nargs = "*" if self.stdin and self.nargs == "+" else self.nargs
            parser.add_argument(
                self.name, nargs=nargs, type=self.convert, default=self.parsed_default, metavar=self.metavar,
            )
        elif self.kind == "flag":
            parser.add_argument(
                *self.flags, dest=self.name, action=argparse.BooleanOptionalAction, default=self.parsed_default,
            )
        else:
            parser.add_argument(
                *self.flags, dest=self.name, type=_pieces(self.convert) if self.repeat else self.convert,
                metavar=self.metavar, action="extend" if self.repeat else "store", required=self.required,
                default=None,
            )

    def finish(self, value: Any, *, others_given: bool = False) -> Any:
        """Turn a parsed value into the value the function gets: stdin, defaults, and collections."""
        if value is None or value == []:
            # An option reads stdin only as the fallback input: when the command line gave nothing
            # else. Otherwise `--path f` would block on a stdin pipe that never closes (cron, ssh).
            if self.stdin and (self.positional or not others_given):
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


def _read_params(fn: Callable, signature: inspect.Signature, arg_help: dict) -> list[Param]:
    hints = typing.get_type_hints(fn, include_extras=True)
    parameters = list(signature.parameters.values())
    params = []
    for parameter in parameters:
        if parameter.kind is parameter.VAR_KEYWORD:
            raise TypeError(f"{fn.__name__}: **{parameter.name} cannot be a command-line argument")
        params.append(_read_param(fn, parameter, hints.get(parameter.name, str), arg_help))
    stdin_options = [param for param in params if param.kind == "option" and param.wants_stdin]
    if len(stdin_options) > 1:
        raise ValueError(f"{fn.__name__}: only one parameter can read stdin")
    variadic = next((param for param in params if param.positional and param.nargs in MANY_NARGS), None)
    reader = stdin_options[0] if stdin_options else variadic if variadic and variadic.wants_stdin else None
    if reader is not None:
        reader.stdin = True
    return params


def _read_param(fn: Callable, parameter: inspect.Parameter, annotation: Any, arg_help: dict) -> Param:
    name = parameter.name
    # A parameter that defaults to None may be Optional outside its Annotated, or inside it.
    annotation = _without_none(annotation)
    annotation, extras = (typing.get_args(annotation)[0], typing.get_args(annotation)[1:]) \
        if typing.get_origin(annotation) is typing.Annotated else (annotation, ())
    meta = next((extra for extra in extras if isinstance(extra, (Opt, Arg))), None)
    flags = [extra for extra in extras if isinstance(extra, str) and extra.startswith("-")]
    if any(type(extra) is int for extra in extras):
        raise TypeError(f"{fn.__name__}: {name!r} has a count of values; use list[...] with \"+\" or \"*\"")
    nargs = next((extra for extra in extras if extra in NARGS), None)
    if isinstance(meta, Arg) and meta.nargs is not None:
        nargs = meta.nargs
    annotation = _without_none(annotation)

    collect = None
    if typing.get_origin(annotation) is list:
        item_types = typing.get_args(annotation)
        collect, annotation = list, (item_types[0] if item_types else str)
    choices: tuple = ()
    if typing.get_origin(annotation) is typing.Literal:
        choices = typing.get_args(annotation)
    if meta is not None and meta.choices is not None:
        choices = meta.choices
    custom_type = meta.type if meta is not None else None
    if not (custom_type or choices or annotation in PLAIN_TYPES or annotation in ISO_FORMATS):
        raise TypeError(f"{fn.__name__}: cannot read parameter {name!r} of type {annotation!r} from the command line")
    tokens = tuple(str(choice) for choice in choices)
    convert = _converter(custom_type or annotation, choices, tokens)

    has_default = parameter.default is not REQUIRED
    spread = parameter.kind is parameter.VAR_POSITIONAL
    keyword = parameter.kind is parameter.KEYWORD_ONLY
    if spread:
        nargs = "*"
    positional = (spread or isinstance(meta, Arg) or nargs is not None
                  or (not has_default and not keyword and not isinstance(meta, Opt) and annotation is not bool))
    if positional and nargs is None:
        nargs = "+" if collect else "?" if has_default else None
    kind = "positional" if positional else "flag" if annotation is bool and not custom_type else "option"

    # A long flag given in the annotation replaces the parameter's own name, which is
    # often a word the command line shouldn't show (`categories` read as --from).
    spellings = list(dict.fromkeys(flags + list(meta.flags if isinstance(meta, Opt) else ())))
    if not any(flag.startswith("--") for flag in spellings):
        spellings.append("--" + name.replace("_", "-"))
    help_text = (meta.help if meta is not None and meta.help is not None else arg_help.get(name, ""))
    default_metavar = (name if positional else "N" if annotation in (int, float)
                       else annotation.__name__.upper() if annotation in ISO_FORMATS else name.upper())
    return Param(
        name=name, kind=kind, convert=convert, default=parameter.default if has_default else REQUIRED,
        nargs=nargs if positional else None, flags=tuple(sorted(spellings, key=len)),
        collect=None if spread else collect, repeat=bool(collect) and not positional, spread=spread,
        keyword=keyword, help=help_text, metavar=(meta.metavar if meta and meta.metavar else default_metavar),
        wants_stdin=meta.stdin if meta is not None else positional, choices=choices, tokens=tokens,
    )


def _converter(base: Any, choices: tuple, tokens: tuple) -> Callable[[str], Any]:
    if base in ISO_FORMATS:
        return _iso(base)
    if not choices:
        return base
    by_token = dict(zip(tokens, choices))

    def to_choice(text: str) -> Any:
        if text not in by_token:
            raise argparse.ArgumentTypeError(f"invalid choice: {text!r} (choose from {', '.join(tokens)})")
        return by_token[text]

    return to_choice


def _iso(kind: type[date]) -> Callable[[str], Any]:
    def from_iso(text: str) -> Any:
        try:
            return kind.fromisoformat(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid {kind.__name__}: {text!r} (use {ISO_FORMATS[kind]})") from None

    return from_iso


def _pieces(convert: Callable[[str], Any]) -> Callable[[str], list]:
    """Read "a,b" as the values a and b, checking each on its own."""
    def split(text: str) -> list:
        values = []
        for piece in filter(None, (piece.strip() for piece in text.split(","))):
            try:
                values.append(convert(piece))
            except (TypeError, ValueError):
                # argparse would name the whole "a,b" token; this names the piece.
                name = getattr(convert, "__name__", "")
                raise argparse.ArgumentTypeError(f"invalid {name} value: {piece!r}") from None
        return values

    return split


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
