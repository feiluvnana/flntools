"""Function parameters as command-line arguments: annotations, conversion, and stdin.

`_cli` reads each parameter of a handler into a `Param`, from its annotation, its default and any
`opt(...)` or `arg(...)` given in its `Annotated[...]`. `Param.add_to` gives argparse the argument,
`Param.finish` turns the parsed word into the value the handler gets (a stdin fallback, the default,
a list collection), and `spell_consts` rewrites the command line for options with an optional value.
argparse itself runs in `_cli`, and help text is drawn there too; this module only describes.

    def remove(names: Annotated[list[str], arg(nargs="+")], *, force: bool = False) -> int: ...

Here `names` is a positional taking one or more words, and `force` is the flag `--force`. Nothing
here touches the network; stdin is read only by `finish`, for a parameter that asked for it.
"""

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

import shared
from shared._screen import isatty

# What `_cli` builds on; `shared.cli` re-exports `Error`, `UsageError`, `opt` and `arg`.
__all__ = ["Arg", "Error", "Opt", "Param", "UsageError", "arg", "opt", "read_signature", "spell_consts"]

# The default of a parameter that has none; `inspect` marks it with its own empty sentinel.
REQUIRED = inspect.Parameter.empty
# argparse's nargs for "one or more" and "zero or more" words; both collect into a list.
MANY_NARGS = ("*", "+")
NARGS = MANY_NARGS + ("?",)
# The types read without a type=: each is its own conversion except the ISO dates.
PLAIN_TYPES = (str, int, float, bool, Path)
# Dates are read with fromisoformat, so the accepted spelling is shown in the error message.
ISO_FORMATS = {date: "YYYY-MM-DD", datetime: "YYYY-MM-DDTHH:MM"}


class Error(shared.Error):
    """The base of every error `shared.cli` raises. Here, beside `UsageError`, which `_cli` imports."""


class UsageError(Error):
    """A command line the command cannot run with. Reported as `✗ message` and the usage, exit 2."""

    usage: str = ""


@dataclass(frozen=True)
class Opt:
    """An option's settings, as `opt(...)` returns them inside a parameter's `Annotated[...]`."""

    # The spellings in the order given, such as ("-f", "--from"); empty keeps `--<name>`.
    flags: tuple = ()
    # The help row; None takes the parameter's entry from the handler docstring's `Args:`.
    help: str | None = None
    # The placeholder shown in usage and help; None is the parameter's name in capitals.
    metavar: str | None = None
    # The values accepted, as a tuple; None accepts any, or a Literal's values.
    choices: tuple | None = None
    # Turns the option's word into the value; None reads the type from the annotation.
    type: Callable[[str], Any] | None = None
    # Whether a piped stdin supplies the value when the command line gives no other.
    stdin: bool = False
    # The value an option given without its word gets; None means the word is required.
    const: Any = None


@dataclass(frozen=True)
class Arg:
    """A positional's settings, as `arg(...)` returns them inside a parameter's `Annotated[...]`."""

    # The help row; None takes the parameter's entry from the handler docstring's `Args:`.
    help: str | None = None
    # The placeholder shown in usage and help; None is the parameter's name.
    metavar: str | None = None
    # argparse's nargs: "?", "*" or "+"; None reads it from the annotation and the default.
    nargs: Any = None
    # The values accepted, as a tuple; None accepts any, or a Literal's values.
    choices: tuple | None = None
    # Turns one word of the command line into the value; None reads the type from the annotation.
    type: Callable[[str], Any] | None = None
    # Whether a "*" or "+" positional given no words reads them from a piped stdin.
    stdin: bool = True


def opt(*flags: str, help: str | None = None, metavar: str | None = None, choices: Any = None,
        type: Callable[[str], Any] | None = None, stdin: bool = False, const: Any = None) -> Opt:
    """An option's spellings and settings. A long flag such as "--from" replaces the parameter's own name.

        def show(*, color: Annotated[Optional[str], cli.opt(choices=("always", "never"), const="always")] = None): ...

    Args:
        flags: its spellings, such as "-f" and "--from"; none keeps `--<name>`.
        help: the help row; None takes it from the handler docstring's `Args:` entry.
        metavar: the placeholder shown in the usage and help; None is the name in capitals.
        choices: the values accepted; None accepts any, or a `Literal`'s values.
        type: turns the option's word into the value; None reads it from the annotation.
        stdin: whether the option reads a piped stdin when the command line gives nothing else.
        const: makes the value optional, and needs `choices`: the option takes the next word
            only when that word is one of `choices` (`--color never`), and otherwise gets
            `const` and leaves the next word to what follows (`--color a.txt` is the option
            and then a positional). `--color=never` always takes its word. None means a word
            is required.

    Raises:
        TypeError: `const` without `choices`, which would leave no way to tell a value from a
            positional.
    """
    if const is not None and choices is None:
        raise TypeError("const= needs choices=, to tell the option's word from the next argument")
    return Opt(flags, help, metavar, None if choices is None else tuple(choices), type, stdin, const)


def arg(help: str | None = None, *, metavar: str | None = None, nargs: str | None = None, choices: Any = None,
        type: Callable[[str], Any] | None = None, stdin: bool = True) -> Arg:
    """A positional's settings, given in the parameter's `Annotated[...]`.

        def remove(names: Annotated[list[str], cli.arg(nargs="+", metavar="NAME")]) -> int: ...

    Args:
        help: the help row; None takes it from the handler docstring's `Args:` entry.
        metavar: the placeholder shown in the usage and help; None is the parameter's name.
        nargs: "?", "*" or "+"; None reads it from the annotation and the default.
        choices: the values accepted; None accepts any, or a `Literal`'s values.
        type: turns one word of the command line into the value; None reads it from the annotation.
        stdin: whether a "*" or "+" positional given no words reads them from a piped stdin, one per
            line, skipping blank lines and `#` comments. A terminal on stdin is never read.

    Raises:
        TypeError: `nargs` is none of "?", "*" and "+".
    """
    if nargs is not None and nargs not in NARGS:
        raise TypeError(f"nargs must be one of {', '.join(NARGS)}, not {nargs!r}")
    return Arg(help, metavar, nargs, None if choices is None else tuple(choices), type, stdin)


def read_signature(fn: Callable) -> tuple[str, str, list[Param]]:
    """Read a handler's docstring and signature into its help text and its command-line parameters.

        summary, description, params = read_signature(remove)   # params: one Param per parameter

    Args:
        fn: the handler function; its docstring gives the help, its signature the parameters.

    Returns:
        The summary (the first docstring line), the description up to `Args:`, and the Params.

    Raises:
        TypeError: a parameter is `**kwargs`, or its type can't be read from the command line.
        ValueError: more than one option is marked to read stdin.
    """
    summary, description, arg_help = _parse_docstring(fn.__doc__)
    return summary, description, _read_params(fn, inspect.signature(fn), arg_help)


@dataclass
class Param:
    """One function parameter as a command-line argument."""

    # The parameter's name, which is the keyword `bind` passes it under.
    name: str
    # "positional", "option", or "flag"; a bool parameter is a flag unless a type= says otherwise.
    kind: str
    # Turns the word into the value: the annotation's type, a choice lookup, or a date parser.
    convert: Callable[[str], Any]
    # The parameter's default, or REQUIRED when it has none.
    default: Any = REQUIRED
    # argparse's nargs for a positional: "?", "*" or "+"; None for exactly one word.
    nargs: Any = None
    # The spellings, shortest first, so the longest (the long flag) is last.
    flags: tuple = ()
    # list when the parameter takes many values; None for a single value.
    collect: type | None = None
    # A list option given as --tags a,b or --tags a --tags b.
    repeat: bool = False
    # *args, which is passed as positional values spread into the call.
    spread: bool = False
    # keyword-only, so it is passed by name.
    keyword: bool = False
    # The help row, from the Opt or Arg, or from the handler docstring's `Args:`.
    help: str = ""
    # The placeholder shown in usage and help.
    metavar: str = ""
    # Whether the parameter asked for a piped stdin, before one is chosen as the reader.
    wants_stdin: bool = False
    # Whether this is the one parameter that reads stdin when the command line gives nothing.
    stdin: bool = False
    # The values accepted, from a Literal or choices=; empty when any value is accepted.
    choices: tuple = ()
    # The command-line spelling of each choice, in the same order as `choices`.
    tokens: tuple = ()
    # What an option given without its word gets; None means the word is required.
    const: Any = None

    @property
    def positional(self) -> bool:
        """Whether the parameter is a word on the command line rather than an option or a flag."""
        return self.kind == "positional"

    @property
    def many(self) -> bool:
        """Whether the parameter takes a list of values: a list annotation, or a "*" or "+" positional."""
        return self.collect is not None or self.nargs in MANY_NARGS

    @property
    def required(self) -> bool:
        """Whether the command line must give the parameter.

        A positional is required unless its nargs is "?" or "*", or it is *args. An option is required
        when it has no default. A flag is never required, since it is simply off when absent.
        """
        if self.positional:
            return self.nargs not in ("*", "?") and not self.spread
        return self.default is REQUIRED and self.kind != "flag"

    @property
    def long_flag(self) -> str:
        """The longest spelling, which `label` builds the `--no-` form of a flag from.

        Raises IndexError for a positional, which has no spelling at all.
        """
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
        """The usage and help label: `-f, --from METAVAR`, or `--[no-]name` for a flag."""
        if self.kind == "flag":
            return ", ".join(self.flags[:-1] + ("--[no-]" + self.long_flag[2:],))
        return f"{', '.join(self.flags)} {f'[{self.metavar}]' if self.const is not None else self.metavar}"

    def help_row(self) -> str:
        """The help line, with the values listed for a choice."""
        return f"{self.help} ({', '.join(self.tokens)})".strip() if self.tokens else self.help

    def usage(self) -> str:
        """The positional's form in the usage line: `[NAME]`, `[NAME...]`, `NAME...` or `NAME`."""
        name = self.metavar
        # Brackets mark what may be left off, so the usage shows the same optionality argparse enforces.
        if self.nargs == "?":
            return f"[{name}]"
        if self.nargs == "*" or self.spread:
            return f"[{name}...]"
        return f"{name}..." if self.nargs == "+" else name

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        """Register this parameter on an argparse parser, as a positional, a flag or an option.

        Args:
            parser: the parser the command's arguments are added to; `_cli` builds one per command.
        """
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
            # An option with `const` always gets a word here: `spell_consts` has already written
            # `--flag=<const>` for the flag given alone, so argparse never guesses.
            parser.add_argument(
                *self.flags, dest=self.name, type=_pieces(self.convert) if self.repeat else self.convert,
                metavar=self.metavar, action="extend" if self.repeat else "store", required=self.required,
                default=None,
            )

    def finish(self, value: Any, *, others_given: bool = False) -> Any:
        """Turn a parsed value into the value the function gets: stdin, defaults, and collections.

        Args:
            value: what argparse stored for this parameter, or None or [] when the line left it out.
            others_given: whether another parameter of the same command was given on the command
                line, which stops an option from falling back to a piped stdin.

        Returns:
            The value to pass to the handler: a tuple for *args, a collection for a list, or the
            default when nothing was given.

        Raises:
            UsageError: a "+" positional got no words, from the command line or from stdin, or a
                stdin line doesn't convert.
        """
        # Step 1: a stdin fallback, when the line gave nothing for this parameter.
        if value is None or value == []:
            # An option reads stdin only as the fallback input: when the command line gave nothing
            # else. Otherwise `--path f` would block on a stdin pipe that never closes (cron, ssh).
            if self.stdin and (self.positional or not others_given):
                value = _read_stdin(self)
            if (value is None or value == []) and self.positional and self.nargs == "+":
                raise UsageError(f"the following arguments are required: {self.metavar}")
        # Step 2: the default, copied so a handler that mutates a list default can't change the next call.
        if value is None or (value == [] and self.default is not REQUIRED and not self.positional):
            value = None if self.default is REQUIRED else self.default
            if isinstance(value, (list, set, dict)):
                value = type(value)(value)
        # Step 3: shape the value for its parameter kind: *args become a tuple, lists are collected.
        if self.spread:
            return tuple(value or ())
        if self.collect is not None and value is not None:
            return self.collect(value)
        return value


def spell_consts(params: list[Param], tokens: list[str]) -> list[str]:
    """The command line with each optional-value option given alone written as `--flag=<const>`.

        spell_consts(params, ["--color", "a.txt"])            # ["--color=always", "a.txt"]
        spell_consts(params, ["--color", "never", "a.txt"])   # unchanged

    argparse's `nargs="?"` takes whatever word follows, so `--color a.txt` would read the
    positional as the option's value. Here the next word is the value only when it is one of the
    option's choices, spelled as the command line writes them; anything after `--` is left alone.

    Args:
        params: the command's parameters; only those with a `const` are looked at.
        tokens: the command line's words after the command name, as argparse would receive them.

    Returns:
        The same words, with each bare optional-value flag given its `const` in the `=` form.
    """
    flags = {flag: param for param in params if param.const is not None for flag in param.flags}
    out: list[str] = []
    for index, token in enumerate(tokens):
        # "--" ends the options, so the positionals after it are passed through untouched.
        if token == "--":
            return out + tokens[index:]
        param = flags.get(token)
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        # A word that is a choice is the value; anything else (or the end of the line) means the option was given alone.
        if param is not None and following not in param.tokens:
            token = f"{token}={param.const}"
        out.append(token)
    return out


def _read_params(fn: Callable, signature: inspect.Signature, arg_help: dict) -> list[Param]:
    """Read every parameter of fn into a Param, then pick the one that reads a piped stdin."""
    # Type hints are resolved here, once, so string annotations (from __future__) become real types.
    hints = typing.get_type_hints(fn, include_extras=True)
    parameters = list(signature.parameters.values())
    params = []
    for parameter in parameters:
        # **kwargs has no name a command line could give, so it can't be a parameter.
        if parameter.kind is parameter.VAR_KEYWORD:
            raise TypeError(f"{fn.__name__}: **{parameter.name} cannot be a command-line argument")
        params.append(_read_param(fn, parameter, hints.get(parameter.name, str), arg_help))
    # Step: at most one reader of stdin. An option that asked for it wins; else a variadic positional.
    stdin_options = [param for param in params if param.kind == "option" and param.wants_stdin]
    if len(stdin_options) > 1:
        raise ValueError(f"{fn.__name__}: only one parameter can read stdin")
    variadic = next((param for param in params if param.positional and param.nargs in MANY_NARGS), None)
    reader = stdin_options[0] if stdin_options else variadic if variadic and variadic.wants_stdin else None
    if reader is not None:
        reader.stdin = True
    return params


def _read_param(fn: Callable, parameter: inspect.Parameter, annotation: Any, arg_help: dict) -> Param:
    """Build the Param for one parameter from its annotation, its default and its Annotated metadata."""
    name = parameter.name
    # Step 1: unwrap the annotation. A parameter that defaults to None may be Optional outside its
    # Annotated, or inside it, so Optional is stripped before and after the Annotated unwrap.
    annotation = _without_none(annotation)
    annotation, extras = (typing.get_args(annotation)[0], typing.get_args(annotation)[1:]) \
        if typing.get_origin(annotation) is typing.Annotated else (annotation, ())
    meta = next((extra for extra in extras if isinstance(extra, (Opt, Arg))), None)
    flags = [extra for extra in extras if isinstance(extra, str) and extra.startswith("-")]
    # A bare int in Annotated is a count of values, which argparse can't take; a list says it better.
    if any(type(extra) is int for extra in extras):
        raise TypeError(f"{fn.__name__}: {name!r} has a count of values; use list[...] with \"+\" or \"*\"")
    nargs = next((extra for extra in extras if extra in NARGS), None)
    if isinstance(meta, Arg) and meta.nargs is not None:
        nargs = meta.nargs
    annotation = _without_none(annotation)

    # Step 2: a list is one value collected from many words, so the item type is what converts.
    collect = None
    if typing.get_origin(annotation) is list:
        item_types = typing.get_args(annotation)
        collect, annotation = list, (item_types[0] if item_types else str)
    # Step 3: choices come from a Literal, or from choices= which overrides it.
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

    # Step 4: kind. *args is always a "*" positional; a bare parameter with no default is one too,
    # except keyword-only ones and bools, which are flags.
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

    # Step 5: spellings. A long flag given in the annotation replaces the parameter's own name, which is
    # often a word the command line shouldn't show (`categories` read as --from).
    spellings = list(dict.fromkeys(flags + list(meta.flags if isinstance(meta, Opt) else ())))
    if not any(flag.startswith("--") for flag in spellings):
        spellings.append("--" + name.replace("_", "-"))
    help_text = (meta.help if meta is not None and meta.help is not None else arg_help.get(name, ""))
    # The metavar shown for a value: a positional uses its name; numbers show N; dates show their type name.
    default_metavar = (name if positional else "N" if annotation in (int, float)
                       else annotation.__name__.upper() if annotation in ISO_FORMATS else name.upper())
    return Param(
        name=name, kind=kind, convert=convert, default=parameter.default if has_default else REQUIRED,
        nargs=nargs if positional else None, flags=tuple(sorted(spellings, key=len)),
        collect=None if spread else collect, repeat=bool(collect) and not positional, spread=spread,
        keyword=keyword, help=help_text, metavar=(meta.metavar if meta and meta.metavar else default_metavar),
        wants_stdin=meta.stdin if meta is not None else positional, choices=choices, tokens=tokens,
        const=meta.const if isinstance(meta, Opt) else None,
    )


def _converter(base: Any, choices: tuple, tokens: tuple) -> Callable[[str], Any]:
    """The function that turns one word into the value: a date parser, a choice lookup, or the type itself."""
    # A date keeps its own parser, since int-style construction would not accept "YYYY-MM-DD".
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
    """A converter that reads a word as a date or datetime in ISO form, and names the expected form on error."""
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
    """The annotation with `None` removed from an Optional, when only one real type is left."""
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        real = [arg for arg in args if arg is not type(None)]
        if len(real) == 1:
            return real[0]
    return annotation


def _read_stdin(param: Param) -> Any:
    """Read the piped stdin for a parameter: one value for an option, one per line for a positional."""
    stream = sys.stdin
    # A terminal would block until Ctrl-D, so it is never read; no stdin means no value.
    if stream is None or isatty(stream):
        return None
    if not param.positional:
        # The bytes are read and decoded as UTF-8 directly, when the stream has a binary buffer.
        raw = stream.buffer.read().decode("utf-8") if hasattr(stream, "buffer") else stream.read()
        return param.convert(raw) if raw else None
    lines = [line.strip() for line in stream.read().splitlines()]
    values = []
    for line in lines:
        # Blank lines and `#` comment lines are skipped, so a list file can be annotated.
        if line and not line.startswith("#"):
            try:
                values.append(param.convert(line))
            # A bad stdin line is the caller's input, so it is a usage error naming the parameter, not a crash.
            except (ValueError, TypeError, argparse.ArgumentTypeError) as error:
                raise UsageError(f"argument {param.metavar}: invalid value {line!r}: {error}") from None
    return values
