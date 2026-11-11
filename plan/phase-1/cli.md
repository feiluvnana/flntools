# cli

Phase 1. Depends on [theme](theme.md), [style](style.md), [log](log.md), and [prompt](prompt.md). Build it last.

[`shared/_cli.py`](../../shared/_cli.py) holds apps, groups, dispatch, help, and the two modes. [`shared/_params.py`](../../shared/_params.py) turns a signature into argparse arguments and converts values. `cli.opt`, `cli.arg`, and `cli.UsageError` are imported from `cli` as before. The signature is the parser.

- The docstring's first line is the command summary.
- The rest of the docstring, up to `Args:`, is the description.
- Each `name: text` line under `Args:` is that parameter's help.
- `theme._format_help` draws the help text.

## Parameters

| Signature | Command line |
| --- | --- |
| `name: str` with no default | a required positional |
| `*names: str` | zero or more positionals |
| `count: int = 1` | `--count N` |
| `*, path: Path` (keyword-only, no default) | a required `--path PATH` |
| `headless: bool = False` | `--headless` / `--no-headless` |
| `limit: int \| None = None` | `--limit N`; `None` when it is left out |
| `output: Annotated[Path, "-o"] = Path("out")` | `-o` / `--output` |
| `source: Literal["tik", "ins"] = "ins"`, or an `Enum` | a choice; help lists the values |
| `tags: list[str] = []` | repeatable: `--tags a --tags b`; each run gets a new list |
| `Annotated[list[str], "*"]`, `"+"`, `"?"`, or an int | a positional with that `nargs`, even when there is a default |
| `rest: tuple[str, ...] = ()` as the last parameter | the rest of the line |

`Annotated` metadata can be a short string or a `cli.opt` / `cli.arg` call:

- `"-o"` or `"--output"` adds flag spellings. The long name from the parameter is always kept.
- `"*"`, `"+"`, `"?"`, or an int sets `nargs`.
- `cli.opt(*flags, help=None, metavar=None, choices=None, hidden=False, type=None, ask=None, stdin=False)` is for options.
- `cli.arg(help=None, metavar=None, nargs=None, choices=None, type=None, ask=None, stdin=True)` is for positionals.
- `ask` is a function with no arguments that returns the value. Prompt mode calls it instead of asking its own generated question (see [Prompt mode](#prompt-mode)).

Conversions:

- Built in: `str`, `int`, `float`, and `Path`. `Path` does not check that the path exists. `T | None` is unwrapped to `T`.
- `type` is a function from `str` to the value. A `ValueError` or `ArgumentTypeError` from it becomes a usage error.
- `help=` replaces the docstring line, and `hidden=True` leaves the option out of help while it still parses.
- An annotation `cli` cannot handle raises `TypeError` when the function is decorated.

## Values from stdin

A command's first positional that takes many values (`*args`, a list, or `nargs` of `*` or `+`) also reads stdin. This happens when both of these are true:

- the command line gave that positional no values;
- stdin is not a terminal.

In that case each line of stdin becomes one value. Surrounding whitespace is stripped, and blank lines and lines starting with `#` are skipped. The values then go through the usual type conversion. Empty stdin counts as no values, so a `"+"` positional still reports its usage error. Values given on the command line always win, and stdin is never read in addition to them. `cli.arg(stdin=False)` turns this off for one parameter.

An option can take stdin as one block of text instead. With `cli.opt(stdin=True)`, stdin is the command's fallback input. If the command line gave no value to any of the command's parameters, and stdin is not a terminal, the option's value is the whole of stdin, read as UTF-8, with nothing stripped or split. This is for raw content such as a cookie file. Because any other value on the command line skips the read, `set-cookie --path f` never waits on a stdin that is open but never written to, as under cron or ssh.

At most one parameter per command reads stdin. An option marked `stdin=True` takes stdin from the command's variadic positional. Marking two parameters `stdin=True` raises `ValueError` when the function is decorated.

```sh
flnd download ins:example_user twt:example_handle   # from the command line
cat list.txt | flnd download                   # one target per line
flnd download < list.txt                       # same
```

```text
# list.txt
ins:example_user
tik:example_creator

twt:example_handle
```

## Apps, commands, and groups

```python
app = cli.App("flnd", version="0.1.0", description="...", epilog="...", verbosity=True, promptmode=True)

@app.command
def download(targets: Annotated[list[str], "+"]) -> int: ...

auth = app.group("auth", description="Cookie files for a source.")

@auth.command("set-cookie")
def set_cookie(path: Path | None = None, content: Annotated[str | None, cli.opt(stdin=True)] = None) -> int: ...
```

- `@app.command` names the command after the function, with `_` turned into `-`. `@app.command("import")` sets the name.
- `@app.command(default=True)` runs when the first word is not a command or group name.
- `app.group(name, description="")` returns a child app with `command`, `group`, and `help`. A group has no handler of its own, and groups can nest.
- A group run with no command prints its help and exits `0`.
- Parent help lists commands and groups together. A group row uses its `description`, and a command row uses the docstring's first line.
- Registering a name twice on the same parent raises `ValueError` at decoration time.
- `@app` registers the program's only handler, for a program with no subcommand word. `cli.run(fn)` builds an `App` from the function name, registers `fn`, and exits.

`cli` picks the command itself by matching the first word against the command and group names. Each command has its own `ArgumentParser`, which parses only that command's arguments. This keeps errors short: `flnd no-such` reports `unknown command 'no-such'`.

`verbosity=True` adds `-v` and `-q` before the command word. They set `log.level` to `debug` and `quiet`. A command's own `verbose` parameter is unaffected.

To change argparse, subclass `App` and override `prepare(parser)`. It is called for each parser before arguments are added.

```python
class Tool(cli.App):
    def prepare(self, parser):
        parser.allow_abbrev = False
```

## Two modes

An app with `promptmode=True` can be run in two ways.

| Mode | Starts when | Behavior |
| --- | --- | --- |
| **cli mode** | anything is on the command line: a command, an option, or `-h` | parses argv as described above. It never asks a question; a missing value is a usage error |
| **prompt mode** | the command line is empty, and stdin and stderr are both terminals | guides the user through questions, then runs the chosen command |

A bare `flnd` with stdin piped in, or under cron or CI, prints the top-level help, as it would without `promptmode`. The two modes never mix. cli mode stays predictable for scripts, and prompt mode is the one place that asks.

Prompts called from a handler, such as flnd's cookie question, are separate from the modes. They ask whenever `theme.screen.interactive` is `True`, in either mode.

## Prompt mode

1. A `prompt.choice` titled `What do you want to do?` lists the commands and groups. Each row's label is the command's summary, or the group's description. Picking a group opens the same kind of list for that group.
2. The command's parameters are asked in signature order. A parameter with `ask=` gets its value from that function. A required parameter with no `ask=` gets a question generated from its type. An optional parameter with no `ask=`, or one with `hidden=True`, is not asked and keeps its default. This keeps a command with many options from turning into a long form. The generated questions are:

| Parameter | Question |
| --- | --- |
| `str`, `int`, `float`, `Path`, or a custom `type` | `prompt.input`; a conversion error is shown as the validate error |
| `bool` | `prompt.confirm` |
| `Literal`, `Enum`, or `choices=` | `prompt.choice` |
| a list, a tuple, or `nargs` of `*` or `+` | `prompt.input`, split with `shlex.split`; `+` must not be empty |

   A generated question uses the parameter's help line as its text, or the parameter name when there is no help. It starts from the parameter's default, so pressing enter keeps that default.
3. The app logs the cli-mode command line that does the same work (`• flnd download ins:example_user`). This teaches the user the command for next time.
4. The app runs the handler and exits with its code.

Ctrl-C at any question exits `130`.

## Running

- `app()` calls `sys.exit`.
- `app.run(argv) -> int` does the same work without exiting.
- `app.parse(argv)` returns the bound arguments without calling the function.
- `app.help(*path) -> str` returns help: `app.help()`, `app.help("download")`, or `app.help("auth", "set-cookie")`.
- `raise cli.UsageError(message)` inside a handler is reported the same way as a parse error: `✗ message`, a blank line, the command's usage, and exit `2`. Use it for rules that one signature cannot express, such as "give a path or `--content`, not both".

| Outcome | Exit code |
| --- | --- |
| the handler returns `None` | `0` |
| the handler returns an int | that int |
| `-h` / `--help` | `0` |
| `-V` / `--version` (top level, when `version` is set) | `0` |
| usage error | `2` |
| `KeyboardInterrupt`, including Ctrl-C at a prompt | `130`, no traceback |
| any other exception | propagates with its traceback |

Help is written to stdout, in a color depth worked out from stdout the same way `theme.color` is worked out from stderr. That keeps `flnd --help | less` plain. A usage error goes to stderr as `help.error`, a blank line, and the usage.

## Help screens

Section titles, usage, rows, and the epilog use `templates.help`. Labels are padded to 20 columns, and `-h, --help` is the last option row. A row drops a trailing period from its summary. The usage line is the program, then `[options]`, then the required options, then the positionals. `[options]` and the `Options:` section appear only when there are flags other than `-h`, so `-V` / `--version`, `-v`, and `-q` count. A bool flag's label is `--[no-]name`, and a choice's values are added to its help as `(tik, ins)`.

`flnd --help`. flnd's `App` sets `version`, so `-V` is listed:

```text
Dead simple media downloader for TikTok, Facebook, Instagram, and Twitter/X.

Usage:
  flnd [options] [command]

Options:
  -V, --version       show the version
  -h, --help

Commands:
  download            Download each target
  auth                Cookie files for a source

Target format:
  <source>:<want to download>
  Sources: tik, fbk, ins, twt

Examples:
  flnd download ins:example_user
  flnd download tik:example_creator twt:example_handle
  cat list.txt | flnd download
  flnd auth set-cookie --path ~/Downloads/cookies.txt
```

`~/Downloads/cookies.txt` is cyan, underlined, and a link. Every other word in the epilog is dim. `list.txt` stays dim because it has no `/`.

`flnd download --help`:

```text
Download each target.

Usage:
  flnd download targets...

Arguments:
  targets             source:value pairs

Options:
  -h, --help
```

`flnd auth set-cookie --help`:

```text
Set a cookie file.

Each cookie is stored under the source its domain belongs to.

Usage:
  flnd auth set-cookie [options]

Options:
  --path PATH         Netscape cookies.txt or .json file
  --content CONTENT   the cookie text itself; read from stdin when piped
  -h, --help
```

Usage errors in cli mode. argparse cannot know whether an unknown option takes a value, so in `--limit 5` the `5` is read as a target and only `--limit` is reported. Plain argparse does the same.

```text
$ flnd download --limit 5
✗ unrecognized arguments: --limit

Usage:
  flnd download targets...

$ flnd auth set-cookie
✗ give --path, --content, or pipe the cookie text in

Usage:
  flnd auth set-cookie [options]

$ flnd no-such
✗ unknown command 'no-such'

Usage:
  flnd [options] [command]
```

## Tests

- every row of the parameter table, and `TypeError`;
- duplicate names;
- docstring parsing;
- each help screen compared after `style.strip`;
- each usage error and exit code;
- `--version`, `-v`, and `-q`;
- stdin values: piped lines, blank and `#` lines skipped, command-line values winning over stdin, `stdin=False`, and empty stdin with `"+"`;
- `cli.opt(stdin=True)`: the whole of stdin as one string, the command line winning over stdin, and `ValueError` when two parameters claim stdin;
- `cli.UsageError` raised from a handler;
- which mode starts: an empty argv on a terminal, an empty argv with stdin piped, and any argv;
- prompt mode driven by `readkey`: the menu, groups, each generated question, `ask=`, and the logged command line;
- `KeyboardInterrupt` exiting `130`.
