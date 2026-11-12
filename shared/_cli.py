"""Command-line apps whose function signatures are the parser.

A handler is a plain function: each parameter becomes a positional, an option or a flag, read from
its annotation and default, and its docstring's first line and `Args:` section become the help.
`App.run` follows the command words down to a handler, parses the rest with argparse (wrapped, never
exposed: there is no `.raw`), calls the handler and returns its exit code. A bad command line raises
`UsageError`, drawn by `shared.theme` as `✗ message` and the usage, with exit code 2; help is drawn
by the same theme, to stdout.

    app = cli.App("notes", version="1.0")

    @app.command
    def add(text: str, *, tag: list[str] = []) -> int:
        \"\"\"Add a note.

        Args:
            text: the note.
            tag: a label for it; may be given more than once.
        \"\"\"
        return 0

    app()                                          # notes add "buy milk" --tag home
"""

from __future__ import annotations

import argparse
import inspect
import sys
import typing
from typing import Any, Callable

from shared import _theme
from shared._params import Error, UsageError, arg, opt, read_signature, spell_consts
from shared._style import strip
from shared._theme import HelpModel, detect_color

__all__ = ["App", "Error", "UsageError", "arg", "opt", "run"]


def run(fn: Callable) -> typing.NoReturn:
    """Build an App named after fn, with fn as its only handler, run it, and exit."""
    app = App(fn.__name__.replace("_", "-"))
    app.handler = Command(fn, app.name)
    app()


class App:
    """A program and its commands. Bare `app` with no command prints help; `cli.run(fn)` has one handler."""

    def __init__(self, name: str, *, version: str | None = None, description: str = "", epilog: str = ""):
        self.name, self.version, self.description, self.epilog = name, version, description, epilog
        self.parent: App | None = None
        self.children: dict[str, Command | App] = {}
        self.handler: Command | None = None

    def command(self, target: Callable | str | None = None, /) -> Any:
        """Register a command: `@app.command` or `@app.command("name")`."""
        def register(fn: Callable, name: str | None = None) -> Callable:
            command = Command(fn, name or fn.__name__.replace("_", "-"))
            self._add(command.name, command)
            return fn

        if callable(target):
            return register(target)
        return lambda fn: register(fn, target)

    def group(self, name: str, *, description: str = "") -> App:
        """Add a group of commands under `name`, and return it to register its commands on.

            auth = app.group("auth", description="Cookie files for a source.")

            @auth.command
            def show(source: str) -> int: ...            # app auth show <source>

        Args:
            name: the word that selects the group on the command line.
            description: the group's help text; its first line is its row in the parent's help.

        Raises:
            ValueError: this app already has a command or group called `name`, or has its own handler.
        """
        child = App(name, description=description)
        child.parent = self
        self._add(name, child)
        return child

    def __call__(self) -> typing.NoReturn:
        """Run with sys.argv and exit with the command's code."""
        sys.exit(self.run())

    def run(self, argv: list[str] | None = None) -> int:
        argv = sys.argv[1:] if argv is None else list(argv)
        try:
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
            usage = error.usage or self._help_model([self.name]).usage
            model = HelpModel(prog="", description="", usage=usage, error=str(error))
            text = _theme.theme.draw("help", model)
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
        return _theme.theme.draw("help", model)

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
        return (("-V, --version", "show the version"),) if self.version else ()

    def row_text(self) -> str:
        return _first_line(self.description) or self.name

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
        parser.add_argument("-h", "--help", action=_HelpAction)
        for param in self.params:
            param.add_to(parser)
        try:
            namespace = parser.parse_args(spell_consts(self.params, tokens))
            given = {param.name for param in self.params if param.is_given(getattr(namespace, param.name))}
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

    def help_model(self, path: list[str], app: App) -> HelpModel:
        root = _root(app)
        extra = root._root_options() if root.handler is self else ()
        positionals = [param for param in self.params if param.positional]
        options = [param for param in self.params if not param.positional]
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

class _HelpRequested(Exception):
    def __init__(self, model: HelpModel | None = None):
        super().__init__()
        self.model = model

    @property
    def text(self) -> str:
        # _HelpAction raises without a model; the command catching it re-raises with one.
        assert self.model is not None
        # Help goes to stdout, whose color depth can differ from stderr's.
        with _theme.theme.scope(color=detect_color(sys.stdout)) as stdout_theme:
            return stdout_theme.draw("help", self.model)


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
