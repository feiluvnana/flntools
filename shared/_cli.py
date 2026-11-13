"""Command-line apps whose function signatures are the parser.

A handler is a plain function: each parameter becomes a positional, an option or a flag, read from
its annotation and default, and its docstring's first line and `Args:` section become the help.
`App.run` follows the command words down to a handler (`_route`), parses the rest with argparse
(the library this wraps; it is never exposed, so there is no `.raw`), calls the handler and returns
its exit code. The parameters are read by `shared._params`, and the drawing is `shared._theme`'s.
A bad command line raises `UsageError`, drawn by `shared.theme` as `✗ message` and the usage, with
exit code 2; help is drawn by the same theme, to stdout.

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
    """Build an App named after fn, with fn as its only handler, run it, and exit.

        cli.run(add)            # the program is named "add"; `add TEXT --tag home` calls add(...)

    Args:
        fn: the handler. Its name, with `_` turned into `-`, is the program's name.

    Raises:
        SystemExit: always, with the handler's exit code, 2 for a bad command line or 130 on Ctrl-C.
    """
    app = App(fn.__name__.replace("_", "-"))
    app.handler = Command(fn, app.name)
    app()


class App:
    """A program and its commands. Bare `app` with no command prints help; `cli.run(fn)` has one handler.

        app = cli.App("notes", version="1.0")

        @app.command
        def add(text: str) -> int: ...

        app()                                  # `notes add "buy milk"`

    Args:
        name: the program's name, as the help and usage lines show it.
        version: the text `-V` / `--version` prints after the name; None leaves the flag out.
        description: the help text under the usage line, its first line is the root's row in help.
        epilog: text printed after the commands in the root's help.
    """

    def __init__(self, name: str, *, version: str | None = None, description: str = "", epilog: str = ""):
        self.name, self.version, self.description, self.epilog = name, version, description, epilog
        self.parent: App | None = None
        self.children: dict[str, Command | App] = {}
        self.handler: Command | None = None
        # What a group runs when no command word follows its name; None shows its help.
        self.default: Command | None = None

    def command(self, target: Callable | str | None = None, /) -> Any:
        """Register a command: `@app.command` or `@app.command("name")`.

        Args:
            target: the handler when used bare as `@app.command`, or the command's name when given
                as `@app.command("name")`; None means the function's own name, with `_` as `-`.

        Returns:
            The handler itself, unchanged, when used bare; otherwise a decorator that returns it.

        Raises:
            ValueError: the name is already a command or group here, or this app has its own handler.
        """
        def register(fn: Callable, name: str | None = None) -> Callable:
            command = Command(fn, name or fn.__name__.replace("_", "-"))
            self._add(command.name, command)
            return fn

        if callable(target):
            return register(target)
        return lambda fn: register(fn, target)

    def group(self, name: str, *, description: str = "", default: Callable | None = None) -> App:
        """Add a group of commands under `name`, and return it to register its commands on.

            auth = app.group("auth", description="Cookie files for a source.")

            @auth.command
            def show(source: str) -> int: ...            # app auth show <source>

            sync = app.group("sync", default=sync_all)   # app sync [targets…] runs sync_all
            sync.command(ls)                             # app sync ls

        Args:
            name: the word that selects the group on the command line.
            description: the group's help text; its first line is its row in the parent's help.
            default: the handler run when the word after `name` isn't one of the group's commands,
                given every word that follows; None shows the group's help instead.

        Raises:
            ValueError: this app already has a command or group called `name`, or has its own handler.
        """
        child = App(name, description=description)
        child.parent = self
        if default is not None:
            child.default = Command(default, name)
        self._add(name, child)
        return child

    def __call__(self) -> typing.NoReturn:
        """Run with sys.argv and exit with the command's code."""
        sys.exit(self.run())

    def run(self, argv: list[str] | None = None) -> int:
        """Route argv to a command, run its handler, and return the exit code; help and errors are drawn here.

        Args:
            argv: the words after the program name; None reads `sys.argv[1:]`.

        Returns:
            The handler's exit code (0 when it returns no int), 0 after help or the version, 2 after a
            bad command line, or 130 on Ctrl-C.

        Raises:
            UsageError: never; every usage problem is drawn and turned into exit code 2.
        """
        argv = sys.argv[1:] if argv is None else list(argv)
        try:
            found = self._route(argv)
            # An int from _route is the version, which has already been written.
            if isinstance(found, int):
                return found
            command, path, tokens = found
            arguments = command.parse(tokens, path, self)
            try:
                return _exit_code(command.fn(*arguments.args, **arguments.kwargs))
            # A UsageError raised by the handler itself is a bad argument, so it gets the command's usage.
            except UsageError as error:
                raise _usage_error(str(error), command.help_model(path, self).usage) from None
        except _HelpRequested as request:
            _write_help(request.text)
            return 0
        # A usage error raised before any command ran has no command's usage, so the root's usage is shown.
        except UsageError as error:
            usage = error.usage or self._help_model([self.name]).usage
            model = HelpModel(prog="", description="", usage=usage, error=str(error))
            text = _theme.theme.draw("help", model)
            # When the theme's color is "none", the drawn text is stripped of its escape sequences.
            _theme.theme.screen.write(strip(text) if _theme.theme.color == "none" else text)
            return 2
        # 130 is the shell's code for SIGINT (128 + 2), the value a shell expects after Ctrl-C.
        except KeyboardInterrupt:
            return 130

    def parse(self, argv: list[str]) -> inspect.BoundArguments | None:
        """The bound arguments for argv, or None when argv asks for help or the version.

        Args:
            argv: the words after the program name, as `run` would receive them. Nothing is called.

        Raises:
            UsageError: argv is a bad command line for the command it routes to.
        """
        try:
            found = self._route(list(argv), print_version=False)
            if isinstance(found, int):
                return None
            command, path, tokens = found
            return command.parse(tokens, path, self)
        except _HelpRequested:
            return None

    def help(self, *path: str) -> str:
        """Help for the app, a group, or a command: `app.help("auth", "set-cookie")`.

        Args:
            path: the command words down to the group or command; none gives the app's own help.

        Returns:
            The help text, drawn by the theme with the same colors as `--help` would use.

        Raises:
            KeyError: a word in path is not a group or command of this app.
        """
        node: App | Command = self
        for name in path:
            if not isinstance(node, App) or name not in node.children:
                raise KeyError(f"no command {' '.join(path)!r}")
            node = node.children[name]
        names = [self.name, *path]
        model = node.help_model(names, self) if isinstance(node, Command) else node._help_model(names)
        return _theme.theme.draw("help", model)

    def _add(self, name: str, child: Command | App) -> None:
        """Register child under name, refusing a duplicate name or a group that already has a handler."""
        if name in self.children:
            raise ValueError(f"{self.name} already has a command or group named {name!r}")
        if self.handler is not None:
            raise ValueError(f"{self.name} has its own handler, so it cannot have commands")
        self.children[name] = child

    def _route(self, tokens: list[str], print_version: bool = True) -> Any:
        """Follow the command words in tokens down to a command: (command, path, its tokens)."""
        app, path = self, [self.name]
        while True:
            # Flags before a command word belong to the program or a group; a command's own flags are
            # left for its argparse parser, so `-h` after a command shows that command's help.
            while tokens and tokens[0].startswith("-"):
                flag = tokens[0]
                # A group's or program's -h is handled here, since it has no argparse parser of its own.
                if flag in ("-h", "--help") and app.handler is None:
                    raise _HelpRequested(app._help_model(path))
                # The version is only the program's own flag, so a group's -V is not treated as it.
                if app is self and self.version and flag in ("-V", "--version"):
                    if print_version:
                        sys.stdout.write(f"{self.name} {self.version}\n")
                    return 0
                break
            if app.handler is not None:
                return app.handler, path, tokens
            # A command word wins over the default: `sync ls` is the command, `sync x` the default's.
            if app.default is not None and (not tokens or tokens[0] not in app.children):
                return app.default, path, tokens
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
        """The help for this app or group, as the model the theme draws, for the command words in path."""
        if self.handler is not None:
            return self.handler.help_model(path, self)
        commands = tuple((name, child.row_text()) for name, child in self.children.items())
        if self.default is not None:
            # The default's own help, with the group's commands under it and either in the usage.
            model = self.default.help_model(path, self)
            return model._replace(usage=f"{model.usage} | {' '.join(path)} [command]", commands=commands)
        options = self._root_options() if self.parent is None else ()
        usage = " ".join(path) + (" [options]" if options else "") + " [command]"
        # Options are listed only when there is more than -h, the same rule as [options].
        rows = options + (("-h, --help", ""),) if options else ()
        return HelpModel(" ".join(path), self.description, usage, options=rows, commands=commands, epilog=self.epilog)

    def _root_options(self) -> tuple:
        return (("-V, --version", "show the version"),) if self.version else ()

    def row_text(self) -> str:
        """The row this group shows in its parent's command list: its description's first line, or its name."""
        if self.description or self.default is None:
            return _first_line(self.description) or self.name
        return self.default.row_text()

class Command:
    """One handler function and the parameters read from its signature."""

    def __init__(self, fn: Callable, name: str):
        self.fn, self.name = fn, name
        self.summary, self.description, self.params = read_signature(fn)
        self.signature = inspect.signature(fn)

    def row_text(self) -> str:
        """The row this command shows in its parent's list: its summary without the final period, or its name."""
        return self.summary.rstrip(".") or self.name

    def parse(self, tokens: list[str], path: list[str], app: App) -> inspect.BoundArguments:
        """Parse this command's words with argparse, then bind them to the handler's signature.

        Args:
            tokens: the words after the command name, up to the end of the command line.
            path: the program and group names down to this command, for the usage line.
            app: the root app, which decides whether the usage shows its root options.

        Returns:
            The arguments bound to the handler's signature, ready to call it with.

        Raises:
            UsageError: a word is missing, unknown or invalid; its `usage` is this command's usage line.
        """
        prog = " ".join(path)
        # add_help=False, because -h is added as its own action so the help can be drawn by the theme.
        parser = _Parser(prog=prog, add_help=False)
        parser.add_argument("-h", "--help", action=_HelpAction)
        for param in self.params:
            param.add_to(parser)
        try:
            namespace = parser.parse_args(spell_consts(self.params, tokens))
            # "given" is what the command line set, used to stop an option from reading stdin when another was given.
            given = {param.name for param in self.params if param.is_given(getattr(namespace, param.name))}
            values = {
                param.name: param.finish(getattr(namespace, param.name), others_given=bool(given - {param.name}))
                for param in self.params
            }
        # -h from argparse carries no model, so the help is built here and re-raised with the command's own help.
        except _HelpRequested as request:
            raise _HelpRequested(self.help_model(path, app)) from request
        except UsageError as error:
            raise _usage_error(str(error), self.help_model(path, app).usage) from None
        return self.bind(values)

    def bind(self, values: dict) -> inspect.BoundArguments:
        """Arrange parsed values into the positional and keyword arguments the handler's signature takes.

        Args:
            values: each parameter's finished value, keyed by the parameter's name.

        Returns:
            The bound arguments, whose `args` and `kwargs` are what the handler is called with.

        Raises:
            TypeError: the values don't fit the signature, such as a missing argument.
        """
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
        """The help this command draws: its usage line, its arguments, options and epilog.

        Args:
            path: the program and group names down to this command, shown as the usage's start.
            app: the app this command is registered on, whose root options and epilog are shown when
                this command is the program's only handler.

        Returns:
            The model the theme draws as help text.
        """
        root = _root(app)
        extra = root._root_options() if root.handler is self else ()
        positionals = [param for param in self.params if param.positional]
        options = [param for param in self.params if not param.positional]
        usage = [" ".join(path)]
        # [options] stands for the optional flags and options, so the usage line stays short.
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
        """The help text drawn for the model, in the color depth of stdout.

        Raises AssertionError when no model is attached yet; `Command.parse` attaches one before it re-raises.
        """
        # _HelpAction raises without a model; the command catching it re-raises with one.
        assert self.model is not None
        # Help goes to stdout, whose color depth can differ from stderr's.
        with _theme.theme.scope(color=detect_color(sys.stdout)) as stdout_theme:
            return stdout_theme.draw("help", self.model)


class _HelpAction(argparse.Action):
    """The argparse action behind -h: it raises `_HelpRequested` so the help is drawn outside argparse."""

    def __init__(self, option_strings: list[str], dest: str, **kwargs: Any):
        # dest and default are suppressed, so -h stores nothing on the namespace and needs no value.
        super().__init__(option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, nargs=0)

    def __call__(self, *args: Any) -> None:
        raise _HelpRequested()


class _Parser(argparse.ArgumentParser):
    """An argparse parser that raises `UsageError` on a bad command line, instead of printing and exiting."""

    def error(self, message: str) -> typing.NoReturn:
        raise UsageError(message)


def _usage_error(message: str, usage: str) -> UsageError:
    """A UsageError carrying the usage line to show under its message."""
    error = UsageError(message)
    error.usage = usage
    return error


def _write_help(text: str) -> None:
    """Write help to stdout and flush it."""
    sys.stdout.write(text)
    sys.stdout.flush()


def _exit_code(result: Any) -> int:
    """The handler's return value as an exit code; anything that isn't an int (such as None) exits 0."""
    return result if isinstance(result, int) else 0


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0].rstrip(".") if text.strip() else ""


def _root(app: App) -> App:
    while app.parent is not None:
        app = app.parent
    return app
