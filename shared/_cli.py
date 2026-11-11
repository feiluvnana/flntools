"""Command-line apps whose function signatures are the parser."""

from __future__ import annotations

import argparse
import inspect
import shlex
import sys
import typing
from typing import Any, Callable

from shared import _prompt as prompt
from shared import _theme
from shared._log import log
from shared._params import UsageError, arg, command_line, opt, read_signature
from shared._screen import isatty
from shared._style import strip
from shared._theme import HelpModel, detect_color

__all__ = ["App", "UsageError", "arg", "opt", "run"]

def run(fn: Callable) -> None:
    """Build an App named after fn, register fn as its handler, run it, and exit."""
    app = App(fn.__name__.replace("_", "-"))
    app(fn)
    app()


class App:
    def __init__(self, name: str, *, version: str | None = None, description: str = "", epilog: str = "",
                 verbosity: bool = False, promptmode: bool = False):
        self.name, self.version, self.description, self.epilog = name, version, description, epilog
        self.verbosity, self.promptmode = verbosity, promptmode
        self.parent: App | None = None
        self.children: dict[str, Command | App] = {}
        self.default_command: Command | None = None
        self.handler: Command | None = None

    def command(self, target: Callable | str | None = None, /, *, default: bool = False) -> Any:
        """Register a command: `@app.command`, `@app.command("name")`, or `@app.command(default=True)`."""
        def register(fn: Callable, name: str | None = None) -> Callable:
            command = Command(fn, name or fn.__name__.replace("_", "-"))
            self._add(command.name, command)
            if default:
                self.default_command = command
            return fn

        if callable(target):
            return register(target)
        return lambda fn: register(fn, target)

    def group(self, name: str, description: str = "") -> App:
        child = App(name, description=description)
        child.parent = self
        self._add(name, child)
        return child

    def prepare(self, parser: argparse.ArgumentParser) -> None:
        """Called for each parser before arguments are added. Override it to change argparse."""

    def __call__(self, fn: Callable | None = None) -> Any:
        """`@app` registers the program's only handler; `app()` runs it and exits."""
        if fn is None:
            sys.exit(self.run())
        if self.children:
            raise ValueError(f"{self.name} has commands, so it cannot also have its own handler")
        self.handler = Command(fn, self.name)
        return fn

    def run(self, argv: list[str] | None = None) -> int:
        argv = sys.argv[1:] if argv is None else list(argv)
        try:
            if not argv and self.promptmode and _stdin_is_terminal() and _theme.theme.screen.interactive:
                return self._run_promptmode()
            found = self._route(argv)
            if isinstance(found, int):
                return found
            command, path, tokens = found
            arguments = command.parse(tokens, path, self)
            try:
                return _exit_code(command.fn(*arguments.args, **arguments.kwargs))
            except UsageError as error:
                raise _usage_error(str(error), command.help_model(path, self).usage) from None
        except _HelpRequested as request:
            _write_help(request.text)
            return 0
        except UsageError as error:
            usage = getattr(error, "usage", None) or self._help_model([self.name]).usage
            model = HelpModel(prog="", description="", usage=usage, error=str(error))
            text = _theme.theme._format_help(model)
            _theme.theme.screen.write(strip(text) if _theme.theme.color == "none" else text)
            return 2
        except KeyboardInterrupt:
            return 130

    def parse(self, argv: list[str]) -> inspect.BoundArguments | None:
        """The bound arguments for argv, or None when argv asks for help or the version."""
        try:
            found = self._route(list(argv), print_version=False)
            if isinstance(found, int):
                return None
            command, path, tokens = found
            return command.parse(tokens, path, self)
        except _HelpRequested:
            return None

    def help(self, *path: str) -> str:
        """Help for the app, a group, or a command: `app.help("auth", "set-cookie")`."""
        node: App | Command = self
        for name in path:
            if not isinstance(node, App) or name not in node.children:
                raise KeyError(f"no command {' '.join(path)!r}")
            node = node.children[name]
        names = [self.name, *path]
        model = node.help_model(names, self) if isinstance(node, Command) else node._help_model(names)
        return _theme.theme._format_help(model)

    def _add(self, name: str, child: Command | App) -> None:
        if name in self.children:
            raise ValueError(f"{self.name} already has a command or group named {name!r}")
        if self.handler is not None:
            raise ValueError(f"{self.name} has its own handler, so it cannot have commands")
        self.children[name] = child

    def _route(self, tokens: list[str], print_version: bool = True) -> Any:
        """Follow the command words in tokens down to a command: (command, path, its tokens)."""
        app, path = self, [self.name]
        while True:
            while tokens and tokens[0].startswith("-"):
                flag = tokens[0]
                if flag in ("-h", "--help") and app.handler is None:
                    raise _HelpRequested(app._help_model(path))
                if app is self and self.version and flag in ("-V", "--version"):
                    if print_version:
                        sys.stdout.write(f"{self.name} {self.version}\n")
                    return 0
                if app is self and self.verbosity and flag in ("-v", "-q"):
                    log.level = "debug" if flag == "-v" else "quiet"
                    tokens = tokens[1:]
                    continue
                break
            if app.handler is not None:
                return app.handler, path, tokens
            if not tokens:
                raise _HelpRequested(app._help_model(path))
            child = app.children.get(tokens[0])
            if isinstance(child, App):
                app, path, tokens = child, path + [tokens[0]], tokens[1:]
            elif child is not None:
                return child, path + [tokens[0]], tokens[1:]
            elif app.default_command is not None:
                return app.default_command, path + [app.default_command.name], tokens
            else:
                message = (f"unrecognized arguments: {' '.join(tokens)}" if tokens[0].startswith("-")
                           else f"unknown command '{tokens[0]}'")
                raise _usage_error(message, app._help_model(path).usage)

    def _help_model(self, path: list[str]) -> HelpModel:
        if self.handler is not None:
            return self.handler.help_model(path, self)
        commands = tuple((name, child.row_text()) for name, child in self.children.items())
        options = self._root_options() if self.parent is None else ()
        usage = " ".join(path) + (" [options]" if options else "") + " [command]"
        # Options are listed only when there is more than -h, the same rule as [options].
        rows = options + (("-h, --help", ""),) if options else ()
        return HelpModel(" ".join(path), self.description, usage, options=rows, commands=commands, epilog=self.epilog)

    def _root_options(self) -> tuple:
        rows = (("-V, --version", "show the version"),) if self.version else ()
        if self.verbosity:
            rows += (("-v", "show debug lines"), ("-q", "show only errors"))
        return rows

    def row_text(self) -> str:
        return _first_line(self.description) or self.name

    def _run_promptmode(self) -> int:
        app, path = self, [self.name]
        command = self.handler
        while command is None:
            labels: dict[str, str] = {}
            for name, child in app.children.items():
                label = child.row_text()
                labels[label if label not in labels else f"{label} ({name})"] = name
            name = prompt.choice("What do you want to do?", labels)
            child, path = app.children[name], path + [name]
            if isinstance(child, App):
                app = child
            else:
                command = child
        values = command.ask_values()
        log.info(shlex.join(path + command_line(command.params, values)))
        return _exit_code(command.call(values))


class Command:
    """One handler function and the parameters read from its signature."""

    def __init__(self, fn: Callable, name: str):
        self.fn, self.name = fn, name
        self.summary, self.description, self.params = read_signature(fn)
        self.signature = inspect.signature(fn)

    def row_text(self) -> str:
        return self.summary.rstrip(".") or self.name

    def parse(self, tokens: list[str], path: list[str], app: App) -> inspect.BoundArguments:
        prog = " ".join(path)
        parser = _Parser(prog=prog, add_help=False)
        _root(app).prepare(parser)
        parser.add_argument("-h", "--help", action=_HelpAction)
        for param in self.params:
            param.add_to(parser)
        try:
            namespace = parser.parse_args(tokens)
            given = {param.name for param in self.params if getattr(namespace, param.name) not in (None, [])}
            values = {
                param.name: param.finish(getattr(namespace, param.name), others_given=bool(given - {param.name}))
                for param in self.params
            }
        except _HelpRequested as request:
            raise _HelpRequested(self.help_model(path, app)) from request
        except UsageError as error:
            raise _usage_error(str(error), self.help_model(path, app).usage) from None
        return self.bind(values)

    def bind(self, values: dict) -> inspect.BoundArguments:
        args, kwargs = [], {}
        for param in self.params:
            value = values[param.name]
            if param.spread:
                args.extend(value)
            elif param.keyword:
                kwargs[param.name] = value
            else:
                args.append(value)
        return self.signature.bind(*args, **kwargs)

    def call(self, values: dict) -> Any:
        arguments = self.bind(values)
        return self.fn(*arguments.args, **arguments.kwargs)

    def help_model(self, path: list[str], app: App) -> HelpModel:
        root = _root(app)
        extra = root._root_options() if root.handler is self else ()
        shown = [param for param in self.params if not param.hidden]
        positionals = [param for param in shown if param.positional]
        options = [param for param in shown if not param.positional]
        usage = [" ".join(path)]
        if extra or any(not param.required for param in options):
            usage.append("[options]")
        usage += [f"{param.long_flag} {param.metavar}" for param in options if param.required]
        usage += [param.usage() for param in positionals]
        description = self.summary + (f"\n\n{self.description}" if self.description else "")
        return HelpModel(
            " ".join(path), description, " ".join(usage),
            arguments=tuple((param.metavar, param.help_row()) for param in positionals),
            options=tuple((param.label(), param.help_row()) for param in options) + extra + (("-h, --help", ""),),
            epilog=root.epilog if root.handler is self else "",
        )

    def ask_values(self) -> dict:
        """Prompt mode: ask the required parameters, keep the defaults of the rest."""
        values = {}
        for param in self.params:
            if param.ask is not None:
                values[param.name] = param.ask()
            elif param.required and not param.hidden:
                values[param.name] = param.ask_generated()
            else:
                values[param.name] = param.finish(None)
        return values



class _HelpRequested(Exception):
    def __init__(self, model: HelpModel | None = None):
        super().__init__()
        self.model = model

    @property
    def text(self) -> str:
        stdout_theme = _theme.theme.branch(color=detect_color(sys.stdout))
        return stdout_theme._format_help(self.model)


class _HelpAction(argparse.Action):
    def __init__(self, option_strings: list[str], dest: str, **kwargs: Any):
        super().__init__(option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, nargs=0)

    def __call__(self, *args: Any) -> None:
        raise _HelpRequested()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> typing.NoReturn:
        raise UsageError(message)


def _usage_error(message: str, usage: str) -> UsageError:
    error = UsageError(message)
    error.usage = usage
    return error


def _write_help(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _exit_code(result: Any) -> int:
    return result if isinstance(result, int) else 0


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0].rstrip(".") if text.strip() else ""


def _root(app: App) -> App:
    while app.parent is not None:
        app = app.parent
    return app


def _stdin_is_terminal() -> bool:
    return isatty(sys.stdin)
