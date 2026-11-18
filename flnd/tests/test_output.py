"""No module but `src/ui/` writes to standard output or error (PLAN.md section 8.2).

Every file under `flnd/` outside `src/ui/` is read with `ast`, and nothing is imported or run.
A write is a call of `print`, a call on `sys.stdout` or `sys.stderr` or their `buffer`
(`write`, `flush`, `writelines`), `os.write` to descriptor 1 or 2 (or a stream's `fileno()`),
or a Rich `Console`, which writes to standard output by default. Reading the
streams (`sys.stdout.isatty()`) and handing one to a child process (`stdout=sys.stderr`) are not
writes.
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FLNKIT = ROOT / "flnkit"
UI = SRC / "ui"
STREAMS = {"stdout", "stderr", "__stdout__", "__stderr__"}
WRITES = {"write", "flush", "writelines"}


def _stream(node: ast.AST) -> bool:
    """Whether `node` is `sys.stdout` or `sys.stderr`, or the `buffer` under one."""
    if isinstance(node, ast.Attribute) and node.attr == "buffer":
        node = node.value
    return (isinstance(node, ast.Attribute) and node.attr in STREAMS
            and isinstance(node.value, ast.Name) and node.value.id == "sys")


def _descriptor(node: ast.AST) -> bool:
    """Whether `node` names standard output or error's file descriptor: 1, 2, or a `fileno()`."""
    if isinstance(node, ast.Constant):
        return node.value in (1, 2)
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "fileno" and _stream(node.func.value))


def writes(source: str) -> list[str]:
    """Every write to a standard stream in `source`, as `line N: what`."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("print", "Console"):
            found.append(f"line {node.lineno}: {func.id}()")
        elif isinstance(func, ast.Attribute) and func.attr == "Console":
            found.append(f"line {node.lineno}: Console()")
        elif isinstance(func, ast.Attribute) and func.attr in WRITES and _stream(func.value):
            found.append(f"line {node.lineno}: {ast.unparse(func)}()")
        elif (isinstance(func, ast.Attribute) and func.attr == "write"
              and isinstance(func.value, ast.Name) and func.value.id == "os"
              and node.args and _descriptor(node.args[0])):
            found.append(f"line {node.lineno}: os.write({ast.unparse(node.args[0])})")
    return found


class OutputTest(unittest.TestCase):
    def test_only_ui_writes_to_the_terminal(self) -> None:
        for path in sorted([*SRC.rglob("*.py"), *FLNKIT.rglob("*.py")]):
            if UI in path.parents:
                continue
            with self.subTest(str(path.relative_to(ROOT))):
                self.assertEqual(writes(path.read_text(encoding="utf-8")), [])

    def test_the_search_finds_writes_and_ignores_reads(self) -> None:
        source = ("import sys\nprint('x')\nsys.stderr.write('x')\nsys.stdout.flush()\n"
                  "rich.console.Console()\nsys.stdout.isatty()\nrun(stdout=sys.stderr)\n"
                  "'print(\"in a string\")'\nsys.stdout.buffer.write(b'x')\nos.write(2, b'x')\n"
                  "os.write(sys.stdout.fileno(), b'x')\nos.write(fd, b'x')\n")
        self.assertEqual(writes(source), ["line 2: print()", "line 3: sys.stderr.write()",
                                          "line 4: sys.stdout.flush()", "line 5: Console()",
                                          "line 9: sys.stdout.buffer.write()",
                                          "line 10: os.write(2)",
                                          "line 11: os.write(sys.stdout.fileno())"])


if __name__ == "__main__":
    unittest.main()
