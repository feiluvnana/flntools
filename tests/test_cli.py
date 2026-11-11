import argparse
import enum
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal, Optional
from unittest import mock

from shared import cli, log, style, theme
from shared._theme import Screen


class Stdin(io.StringIO):
    def __init__(self, text="", tty=False):
        super().__init__(text)
        self.tty = tty
        self.buffer = io.BytesIO(text.encode())

    def isatty(self):
        return self.tty


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class CliCase(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(out=io.StringIO(), inp=io.StringIO(""), width=80, tty=False, interactive=True)
        self.scope = theme.scope(screen=self.screen, color="none", unicode=True)
        self.scope.__enter__()
        self.previous_level = log.level
        self.stdin = mock.patch("sys.stdin", Stdin(tty=True))
        self.stdin.start()

    def tearDown(self):
        self.stdin.stop()
        log.level = self.previous_level
        self.scope.__exit__(None, None, None)

    def pipe(self, text):
        self.stdin.stop()
        self.stdin = mock.patch("sys.stdin", Stdin(text))
        self.stdin.start()

    def run_app(self, app, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = app.run(list(argv))
        return code, out.getvalue(), self.screen.out.getvalue()

    def parsed(self, fn, *argv):
        return single(fn).parse(list(argv)).arguments


def single(fn):
    """An app whose only handler is fn, as `cli.run(fn)` builds it."""
    app = cli.App("tool")
    app.handler = cli.Command(fn, "tool")
    return app


def flnd_app():
    app = cli.App(
        "flnd", description="Dead simple media downloader for TikTok, Facebook, Instagram, and Twitter/X.",
        epilog=("Target format:\n  <source>:<want to download>\n  Sources: tik, fbk, ins, twt\n\nExamples:\n"
                "  flnd download ins:example_user\n  flnd download tik:example_creator twt:example_handle\n"
                "  cat list.txt | flnd download\n  flnd auth set-cookie --path ~/Downloads/cookies.txt"),
    )
    calls = []

    @app.command
    def download(targets: Annotated[list[str], "+"]) -> int:
        """Download each target.

        Args:
            targets: source:value pairs
        """
        calls.append(("download", targets))
        return 0

    auth = app.group("auth", description="Cookie files for a source.")

    @auth.command("set-cookie")
    def set_cookie(path: Optional[Path] = None, content: Annotated[Optional[str], cli.opt(stdin=True)] = None) -> int:
        """Set a cookie file.

        Each cookie is stored under the source its domain belongs to.

        Args:
            path: Netscape cookies.txt or .json file
            content: the cookie text itself; read from stdin when piped
        """
        if (path is None) == (content is None):
            raise cli.UsageError("give --path, --content, or pipe the cookie text in")
        calls.append(("set-cookie", path, content))

    app.calls = calls
    return app


class ParameterTest(CliCase):
    def test_required_positional(self):
        def fn(name: str): ...
        self.assertEqual(self.parsed(fn, "ada"), {"name": "ada"})

    def test_star_args(self):
        def fn(*names: str): ...
        self.assertEqual(self.parsed(fn, "a", "b"), {"names": ("a", "b")})
        self.assertEqual(self.parsed(fn), {})

    def test_int_option(self):
        def fn(count: int = 1): ...
        self.assertEqual(self.parsed(fn, "--count", "3"), {"count": 3})
        self.assertEqual(self.parsed(fn), {"count": 1})

    def test_keyword_only_required_option(self):
        def fn(*, path: Path): ...
        self.assertEqual(self.parsed(fn, "--path", "a/b"), {"path": Path("a/b")})
        app = single(fn)
        self.assertEqual(self.run_app(app)[0], 2)

    def test_bool_flag(self):
        def fn(headless: bool = False): ...
        self.assertEqual(self.parsed(fn, "--headless"), {"headless": True})
        self.assertEqual(self.parsed(fn, "--no-headless"), {"headless": False})
        self.assertEqual(self.parsed(fn), {"headless": False})

    def test_optional_value(self):
        def fn(limit: int | None = None): ...
        self.assertEqual(self.parsed(fn), {"limit": None})
        self.assertEqual(self.parsed(fn, "--limit", "5"), {"limit": 5})

    def test_short_flag(self):
        def fn(output: Annotated[Path, "-o"] = Path("out")): ...
        self.assertEqual(self.parsed(fn, "-o", "x"), {"output": Path("x")})
        self.assertEqual(self.parsed(fn, "--output", "y"), {"output": Path("y")})

    def test_literal_choices(self):
        def fn(source: Literal["tik", "ins"] = "ins"): ...
        self.assertEqual(self.parsed(fn, "--source", "tik"), {"source": "tik"})
        app = single(fn)
        code, _, err = self.run_app(app, "--source", "fbk")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice: 'fbk' (choose from tik, ins)", err)
        self.assertIn("--source SOURCE     (tik, ins)", app.help())

    def test_repeatable_list_gets_a_new_list_each_run(self):
        def fn(tags: list[str] = []): ...
        first = self.parsed(fn)["tags"]
        first.append("x")
        self.assertEqual(self.parsed(fn), {"tags": []})
        self.assertEqual(self.parsed(fn, "--tags", "a", "--tags", "b"), {"tags": ["a", "b"]})

    def test_annotated_nargs(self):
        def star(items: Annotated[list[str], "*"] = ["d"]): ...
        def plus(items: Annotated[list[int], "+"]): ...
        def maybe(item: Annotated[str, "?"] = "d"): ...
        self.assertEqual(self.parsed(star), {"items": ["d"]})
        self.assertEqual(self.parsed(star, "a"), {"items": ["a"]})
        self.assertEqual(self.parsed(plus, "1", "2"), {"items": [1, 2]})
        self.assertEqual(self.parsed(maybe), {"item": "d"})

    def test_date_and_datetime(self):
        def fn(*, since: date | None = None, at: datetime | None = None): ...
        self.assertEqual(self.parsed(fn, "--since", "2026-01-31", "--at", "2026-01-31T08:30"),
                         {"since": date(2026, 1, 31), "at": datetime(2026, 1, 31, 8, 30)})
        app = single(fn)
        self.assertIn("--since DATE", app.help())
        self.assertIn("--at DATETIME", app.help())
        code, _, err = self.run_app(app, "--since", "yesterday")
        self.assertEqual(code, 2)
        self.assertIn("✗ argument --since: invalid date: 'yesterday' (use YYYY-MM-DD)", err)
        self.assertIn("invalid datetime: '2026-13-01' (use YYYY-MM-DDTHH:MM)", self.run_app(app, "--at", "2026-13-01")[2])

    def test_comma_lists(self):
        def fn(*, kind: list[Literal["photo", "video"]] = [], n: list[int] = []): ...
        self.assertEqual(self.parsed(fn, "--kind", "photo,video"), {"kind": ["photo", "video"], "n": []})
        self.assertEqual(self.parsed(fn, "--kind", "photo", "--kind", "video,", "--n", "1, 2"),
                         {"kind": ["photo", "video"], "n": [1, 2]})
        app = single(fn)
        code, _, err = self.run_app(app, "--kind", "photo,gif")
        self.assertEqual(code, 2)
        self.assertIn("✗ argument --kind: invalid choice: 'gif' (choose from photo, video)", err)
        self.assertIn("✗ argument --n: invalid int value: 'x'", self.run_app(app, "--n", "1,x")[2])

    def test_opt_and_arg(self):
        def fn(name: Annotated[str, cli.arg(metavar="WHO", help="who")],
               level: Annotated[int, cli.opt("-l", "--lvl", metavar="L", help="how loud")] = 1): ...
        self.assertEqual(self.parsed(fn, "x", "-l", "2"), {"name": "x", "level": 2})
        self.assertEqual(self.parsed(fn, "x", "--lvl", "3")["level"], 3)
        app = single(fn)
        text = app.help()
        self.assertIn("  WHO                 who", text)
        self.assertIn("  -l, --lvl L         how loud", text)

    def test_a_given_long_flag_replaces_the_name(self):
        def fn(*, categories: Annotated[list[str] | None, cli.opt("--from", metavar="CATEGORIES")] = None,
               fast: Annotated[bool, cli.opt("--quick")] = False): ...
        self.assertEqual(self.parsed(fn, "--from", "a,b", "--quick"), {"categories": ["a", "b"], "fast": True})
        app = single(fn)
        text = app.help()
        self.assertIn("  --from CATEGORIES\n", text)
        self.assertIn("  --[no-]quick\n", text)
        self.assertNotIn("--categories", text)
        self.assertEqual(self.run_app(app, "--categories", "a")[0], 2)

    def test_custom_type(self):
        def even(text):
            if int(text) % 2:
                raise argparse.ArgumentTypeError("not even")
            return int(text)

        def fn(n: Annotated[int, cli.opt(type=even)] = 0): ...
        self.assertEqual(self.parsed(fn, "--n", "4"), {"n": 4})
        app = single(fn)
        self.assertIn("✗ argument --n: not even", self.run_app(app, "--n", "3")[2])

    def test_unsupported_annotations_raise_type_error(self):
        app = cli.App("tool")
        with self.assertRaises(TypeError):
            @app.command
            def bad(mapping: dict): ...
        with self.assertRaises(TypeError):
            @app.command
            def kwargs(**rest: str): ...

    def test_removed_forms_raise_type_error(self):
        app = cli.App("tool")
        removed = {
            "integer nargs": lambda: app.command(_fn(Annotated[list[str], 2])),
            "integer nargs in arg": lambda: cli.arg(nargs=2),
            "fixed tuple": lambda: app.command(_fn(tuple[str, str])),
            "rest of the line": lambda: app.command(_fn(tuple[str, ...], ())),
            "enum": lambda: app.command(_fn(Color, Color.RED)),
            "hidden": lambda: cli.opt(hidden=True),
            "ask on opt": lambda: cli.opt(ask=lambda: 1),
            "ask on arg": lambda: cli.arg(ask=lambda: 1),
            "verbosity": lambda: cli.App("tool", verbosity=True),
            "prompt mode": lambda: cli.App("tool", promptmode=True),
            "default command": lambda: app.command(default=True),
            "single handler": lambda: cli.App("tool")(lambda: None),
        }
        for name, build in removed.items():
            with self.subTest(name), self.assertRaises(TypeError):
                build()
        self.assertFalse(hasattr(cli.App, "prepare"))


def _fn(annotation, *default):
    """A handler with one parameter of this annotation, and its default when given."""
    def handler(value=None): ...
    handler.__annotations__ = {"value": annotation}
    handler.__defaults__ = default or None
    return handler


class StructureTest(CliCase):
    def test_duplicate_names(self):
        app = cli.App("tool")

        @app.command
        def go(): ...
        with self.assertRaises(ValueError):
            @app.command("go")
            def again(): ...
        with self.assertRaises(ValueError):
            app.group("go")

    def test_docstring(self):
        app = cli.App("tool")

        @app.command
        def go(name: str, fast: bool = False):
            """Go somewhere.

            A longer description
            over two lines.

            Args:
                name: where to go,
                    across lines
                fast (bool): hurry
            """

        text = app.help("go")
        self.assertTrue(text.startswith("Go somewhere.\n\nA longer description\nover two lines.\n\nUsage:"))
        self.assertIn("  name                where to go, across lines", text)
        self.assertIn("  --[no-]fast         hurry", text)

    def test_command_names(self):
        app = cli.App("tool")
        seen = []

        @app.command
        def fetch_all(url: str):
            seen.append(url)

        @app.command("import")
        def import_(): ...
        self.assertEqual(list(app.children), ["fetch-all", "import"])
        self.assertEqual(self.run_app(app, "fetch-all", "https://x")[0], 0)
        self.assertEqual(seen, ["https://x"])
        self.assertEqual(self.run_app(app, "https://x")[0], 2)

    def test_nested_groups_and_group_help(self):
        app = cli.App("tool")
        inner = app.group("a").group("b", description="Inner things.")

        @inner.command
        def c() -> int:
            return 7

        self.assertEqual(self.run_app(app, "a", "b", "c")[0], 7)
        code, out, _ = self.run_app(app, "a", "b")
        self.assertEqual(code, 0)
        self.assertEqual(out, "Inner things.\n\nUsage:\n  tool a b [command]\n\nCommands:\n  c                   c\n")

    def test_cli_run(self):
        def greet(name: str = "you"):
            log.ok("hi {name}", name=name)

        with mock.patch("sys.argv", ["greet", "--name", "ada"]), self.assertRaises(SystemExit) as exit:
            cli.run(greet)
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(self.screen.out.getvalue(), "✓ hi ada\n")


class HelpScreenTest(CliCase):
    def test_top_level(self):
        code, out, _ = self.run_app(flnd_app(), "--help")
        self.assertEqual(code, 0)
        self.assertEqual(style.strip(out), (
            "Dead simple media downloader for TikTok, Facebook, Instagram, and Twitter/X.\n\n"
            "Usage:\n  flnd [command]\n\n"
            "Commands:\n  download            Download each target\n  auth                Cookie files for a source\n\n"
            "Target format:\n  <source>:<want to download>\n  Sources: tik, fbk, ins, twt\n\n"
            "Examples:\n  flnd download ins:example_user\n  flnd download tik:example_creator twt:example_handle\n"
            "  cat list.txt | flnd download\n  flnd auth set-cookie --path ~/Downloads/cookies.txt\n"
        ))
        self.assertEqual(self.run_app(flnd_app())[1], out)

    def test_download(self):
        self.assertEqual(style.strip(self.run_app(flnd_app(), "download", "--help")[1]), (
            "Download each target.\n\nUsage:\n  flnd download targets...\n\n"
            "Arguments:\n  targets             source:value pairs\n\nOptions:\n  -h, --help\n"
        ))

    def test_set_cookie(self):
        self.assertEqual(style.strip(flnd_app().help("auth", "set-cookie")), (
            "Set a cookie file.\n\nEach cookie is stored under the source its domain belongs to.\n\n"
            "Usage:\n  flnd auth set-cookie [options]\n\n"
            "Options:\n  --path PATH         Netscape cookies.txt or .json file\n"
            "  --content CONTENT   the cookie text itself; read from stdin when piped\n  -h, --help\n"
        ))

    def test_help_is_colored_for_stdout(self):
        with mock.patch("shared._cli.detect_color", return_value="16m"):
            out = self.run_app(flnd_app(), "-h")[1]
        self.assertIn("\x1b[38;5;243m  cat list.txt | flnd download\x1b[0m", out)
        self.assertIn("\x1b[1;38;2;117;113;249mCommands:\x1b[0m", out)
        self.assertNotIn("]8;;", out)
        self.assertEqual(theme.color, "none")

    def test_required_option_usage(self):
        app = cli.App("tool")

        @app.command
        def go(name: str, *, path: Path, count: int = 1): ...
        self.assertIn("Usage:\n  tool go [options] --path PATH name\n", app.help("go"))


class UsageErrorTest(CliCase):
    def test_unrecognized_arguments(self):
        code, _, err = self.run_app(flnd_app(), "download", "--limit", "5")
        self.assertEqual(code, 2)
        self.assertEqual(err, "✗ unrecognized arguments: --limit\n\nUsage:\n  flnd download targets...\n")

    def test_usage_error_from_handler(self):
        code, _, err = self.run_app(flnd_app(), "auth", "set-cookie")
        self.assertEqual(code, 2)
        self.assertEqual(err, "✗ give --path, --content, or pipe the cookie text in\n\n"
                              "Usage:\n  flnd auth set-cookie [options]\n")

    def test_unknown_command(self):
        code, _, err = self.run_app(flnd_app(), "no-such")
        self.assertEqual((code, err), (2, "✗ unknown command 'no-such'\n\nUsage:\n  flnd [command]\n"))

    def test_missing_and_bad_values(self):
        app = cli.App("tool")

        @app.command
        def go(n: int): ...
        self.assertIn("the following arguments are required: n", self.run_app(app, "go")[2])
        self.assertIn("invalid int value: 'x'", self.run_app(app, "go", "x")[2])

    def test_exit_codes(self):
        app = cli.App("tool")

        @app.command
        def none(): ...

        @app.command
        def three() -> int:
            return 3

        @app.command
        def stop():
            raise KeyboardInterrupt

        @app.command
        def boom():
            raise RuntimeError("boom")

        self.assertEqual([self.run_app(app, name)[0] for name in ("none", "three", "stop")], [0, 3, 130])
        with self.assertRaises(RuntimeError):
            app.run(["boom"])


class TopLevelFlagTest(CliCase):
    def test_version(self):
        app = cli.App("tool", version="1.2.3")

        @app.command
        def go(verbose: bool = False): ...

        self.assertEqual(self.run_app(app, "-V")[:2], (0, "tool 1.2.3\n"))
        self.assertEqual(self.run_app(app, "--version")[:2], (0, "tool 1.2.3\n"))
        self.assertEqual(self.run_app(app, "-v", "go")[0], 2)
        text = app.help()
        self.assertIn("Usage:\n  tool [options] [command]", text)
        self.assertIn("Options:\n  -V, --version       show the version\n  -h, --help\n", text)

    def test_cli_does_not_import_prompt(self):
        # A bare package stands in for shared/__init__.py, which imports every terminal module.
        code = ("import sys, types; package = types.ModuleType('shared'); package.__path__ = ['shared']; "
                "sys.modules['shared'] = package; import shared._cli; print('shared._prompt' in sys.modules)")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=root)
        self.assertEqual(result.stdout.strip(), "False")

    def test_parse_returns_bound_arguments(self):
        app = flnd_app()
        self.assertEqual(app.parse(["download", "a"]).arguments, {"targets": ["a"]})
        self.assertIsNone(app.parse(["download", "-h"]))
        self.assertEqual(app.calls, [])


class StdinTest(CliCase):
    def test_piped_lines(self):
        app = flnd_app()
        self.pipe("ins:a\n\n  # comment\n  tik:b  \n")
        self.assertEqual(self.run_app(app, "download")[0], 0)
        self.assertEqual(app.calls, [("download", ["ins:a", "tik:b"])])

    def test_command_line_wins(self):
        app = flnd_app()
        self.pipe("ins:a\n")
        self.run_app(app, "download", "twt:c")
        self.assertEqual(app.calls, [("download", ["twt:c"])])
        self.assertEqual(sys.stdin.read(), "ins:a\n")

    def test_empty_stdin_with_plus(self):
        self.pipe("")
        code, _, err = self.run_app(flnd_app(), "download")
        self.assertEqual(code, 2)
        self.assertIn("the following arguments are required: targets", err)

    def test_stdin_values_are_converted(self):
        def fn(*numbers: int): ...
        self.pipe("1\n2\n")
        self.assertEqual(self.parsed(fn), {"numbers": (1, 2)})
        self.pipe("x\n")
        app = single(fn)
        self.assertEqual(self.run_app(app)[0], 2)

    def test_stdin_false(self):
        def fn(items: Annotated[list[str], cli.arg(nargs="*", stdin=False)] = []): ...
        self.pipe("a\n")
        self.assertEqual(self.parsed(fn), {"items": []})

    def test_option_reads_whole_stdin(self):
        app = flnd_app()
        self.pipe("# Netscape\n\nline  \n")
        self.run_app(app, "auth", "set-cookie")
        self.assertEqual(app.calls, [("set-cookie", None, "# Netscape\n\nline  \n")])

    def test_option_on_command_line_wins(self):
        app = flnd_app()
        self.pipe("piped")
        self.run_app(app, "auth", "set-cookie", "--content", "given")
        self.assertEqual(app.calls, [("set-cookie", None, "given")])

    def test_option_skips_stdin_when_other_input_given(self):
        class NeverCloses(Stdin):
            def read(self, *args):
                raise AssertionError("stdin must not be read when --path is given")
        app = flnd_app()
        sys.stdin = NeverCloses("")
        sys.stdin.buffer = sys.stdin
        self.run_app(app, "auth", "set-cookie", "--path", "c.txt")
        self.assertEqual(app.calls, [("set-cookie", Path("c.txt"), None)])

    def test_option_takes_stdin_from_positional(self):
        def fn(*names: str, body: Annotated[str, cli.opt(stdin=True)] = ""): ...
        self.pipe("a\nb\n")
        self.assertEqual(self.parsed(fn), {"body": "a\nb\n"})

    def test_stdin_option_with_bool_flag(self):
        def fn(verbose: bool = False, body: Annotated[str, cli.opt(stdin=True)] = ""): ...
        self.pipe("piped\n")
        self.assertEqual(self.parsed(fn), {"verbose": False, "body": "piped\n"})

    def test_two_stdin_options_raise(self):
        app = cli.App("tool")
        with self.assertRaises(ValueError):
            @app.command
            def go(a: Annotated[str, cli.opt(stdin=True)] = "", b: Annotated[str, cli.opt(stdin=True)] = ""): ...


if __name__ == "__main__":
    unittest.main()
