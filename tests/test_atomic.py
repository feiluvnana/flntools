"""shared.atomic: the part file next to the destination, the one-step replace, and the cleanup, in a temp folder.

Also C6's files rule: no other module in `shared` opens a file for writing, which `FilesRuleTest`
checks by parsing every module's source.
"""

import ast
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shared
from shared import _atomic, atomic, clock


class AtomicCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.dest = self.folder / "a.txt"

    def names(self):
        return sorted(path.name for path in self.folder.iterdir())


class WritingTest(AtomicCase):
    def test_the_part_is_hidden_next_to_dest_and_moved_onto_it(self):
        with atomic.writing(self.dest) as part:
            self.assertEqual(part.parent, self.folder)
            self.assertRegex(part.name, r"^\.a\.txt\.[0-9a-f]{8}\.part$")
            part.write_text("new")
            self.assertFalse(self.dest.exists())
        self.assertEqual(self.dest.read_text(), "new")
        self.assertEqual(self.names(), ["a.txt"])

    def test_two_writers_get_their_own_parts(self):
        with atomic.writing(self.dest) as first, atomic.writing(self.dest) as second:
            self.assertNotEqual(first, second)

    def test_an_existing_dest_is_refused_unless_replaced(self):
        self.dest.write_text("old")
        with self.assertRaises(FileExistsError):
            with atomic.writing(self.dest):
                self.fail("the block must not run")
        with atomic.writing(self.dest, replace=True) as part:
            part.write_text("new")
        self.assertEqual(self.dest.read_text(), "new")

    def test_an_error_or_ctrl_c_leaves_dest_as_it_was_and_no_part(self):
        self.dest.write_text("old")
        for error in (ValueError("broken"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    with atomic.writing(self.dest, replace=True) as part:
                        part.write_text("half")
                        raise error
                self.assertEqual(self.dest.read_text(), "old")
                self.assertEqual(self.names(), ["a.txt"])

    def test_a_block_that_writes_nothing_leaves_dest_alone(self):
        self.dest.write_text("old")
        with atomic.writing(self.dest, replace=True) as part:
            part.write_text("bigger")
            part.unlink()
        self.assertEqual(self.dest.read_text(), "old")

    def test_a_failed_rename_leaves_no_part(self):
        with mock.patch("os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                with atomic.writing(self.dest) as part:
                    part.write_text("new")
        self.assertEqual(self.names(), [])


class WindowsLockTest(AtomicCase):
    """A scanner holding `dest` on Windows: `os.replace` fails a few times, then goes through."""

    def held(self, times):
        real, calls = os.replace, []

        def replace(source, target):
            calls.append(target)
            if len(calls) <= times:
                raise PermissionError(13, "The process cannot access the file", str(target))
            real(source, target)

        return mock.patch.object(_atomic.os, "replace", replace), calls

    def test_on_windows_the_replace_is_tried_again_while_the_file_is_held(self):
        patch, calls = self.held(3)
        with patch, mock.patch.object(_atomic, "WINDOWS", True), clock.use(clock.Fake()) as fake:
            atomic.write(self.dest, b"new")
        self.assertEqual((self.dest.read_bytes(), len(calls)), (b"new", 4))
        self.assertEqual(fake.slept, [0.05] * 3)

    def test_after_about_a_second_the_error_is_raised_and_dest_kept(self):
        self.dest.write_bytes(b"old")
        patch, calls = self.held(100)
        with patch, mock.patch.object(_atomic, "WINDOWS", True), clock.use(clock.Fake()) as fake, \
                self.assertRaises(PermissionError):
            atomic.write(self.dest, b"new")
        self.assertEqual((self.dest.read_bytes(), len(calls)), (b"old", 20))
        self.assertAlmostEqual(sum(fake.slept), 0.95)
        self.assertEqual(self.names(), ["a.txt"])

    def test_elsewhere_a_refused_replace_is_raised_at_once(self):
        patch, calls = self.held(1)
        with patch, mock.patch.object(_atomic, "WINDOWS", False), \
                self.assertRaises(PermissionError):
            atomic.write(self.dest, b"new")
        self.assertEqual(len(calls), 1)


class WriteTest(AtomicCase):
    def test_bytes_are_written_as_they_are(self):
        # No "\r\n" on Windows: the file is opened in binary mode there.
        atomic.write(self.dest, b"one\ntwo\n")
        self.assertEqual(self.dest.read_bytes(), b"one\ntwo\n")
        atomic.write(self.dest, b"three")
        self.assertEqual(self.dest.read_bytes(), b"three")
        self.assertEqual(self.names(), ["a.txt"])

    @unittest.skipIf(sys.platform == "win32", "Windows has no POSIX modes")
    def test_mode_is_set_from_the_start(self):
        self.dest.write_text("old")
        os.chmod(self.dest, 0o644)
        modes = []
        real_replace = os.replace

        def replace(part, dest):
            modes.append(stat.S_IMODE(os.stat(part).st_mode))
            real_replace(part, dest)

        with mock.patch("os.replace", replace):
            atomic.write(self.dest, b"secret", mode=0o600)
        self.assertEqual(modes, [0o600])
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o600)

    def test_a_failed_rename_or_ctrl_c_keeps_the_old_file_and_no_part(self):
        """Fix 13: a write that fails, or is interrupted, removes its part and leaves the old file as it was."""
        self.dest.write_bytes(b"old")
        for error in (OSError("disk full"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with mock.patch("os.replace", side_effect=error), self.assertRaises(type(error)):
                    atomic.write(self.dest, b"new")
                self.assertEqual(self.names(), ["a.txt"])
                self.assertEqual(self.dest.read_bytes(), b"old")

    def test_a_str_path_is_accepted(self):
        atomic.write(str(self.dest), b"x")
        with atomic.writing(str(self.dest), replace=True) as part:
            self.assertIsInstance(part, Path)
            part.write_bytes(b"y")
        self.assertEqual(self.dest.read_bytes(), b"y")

    def test_a_failed_write_leaves_no_part(self):
        with mock.patch("os.fdopen", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic.write(self.dest, b"x")
        self.assertEqual(self.names(), [])


# Write modes of `open`, and the `os.open` flags that make a file writable.
WRITE_MODES = set("wax+")
WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"}
# What a mode string looks like: "rb", "w", and tarfile's "w:gz".
MODE = re.compile(r"[rwxabt+]+(:\w*)?")

# The writes outside `_atomic.py` that are allowed, by file and enclosing function, each with why:
# every one is a scratch or staging file that never is a destination itself, so a reader never
# sees it half written. Anything else must go through `atomic`.
ALLOWED = {
    ("_archive.py", "_write_tar"): "writes the part `atomic.writing` gave `zip`; tarfile's mode is a variable there",
    ("_archive.py", "_split"): "the pieces of that part, staged beside it and renamed onto the final names "
                               "together, or all deleted",
    ("_archive.py", "_join"): "a split archive's parts joined into one scratch file in a temporary folder",
    ("_archive.py", "_Output.__init__"): "one entry unpacked into `unzip`'s hidden staging folder, which is "
                                         "renamed into place only once every entry is written",
    ("_media_video.py", "_concat_copy"): "ffmpeg's concat list, in a temporary folder deleted with it",
    ("_http.py", "_open_part"): "a download's resumable `.part`, which must outlive a failed call to be resumed; "
                                "it is renamed onto the file through `atomic.writing` once whole and checked",
    ("_log.py", "Log.record"): "a run's log, appended to line by line and flushed, so a killed run keeps every "
                               "line written so far; it is never a file another reader waits on whole",
}


def writes(tree):
    """Each call in `tree` that opens a file for writing, as (enclosing function, line, what).

    The function is named with its class, such as `_Output.__init__`, and is "<module>" at the top level.
    """
    found = []

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = [*scope, child.name]
            if isinstance(child, ast.Call) and (what := write_call(child)):
                found.append((".".join(scope) or "<module>", child.lineno, what))
            visit(child, inner)

    visit(tree, [])
    return found


def write_call(call):
    """What `call` writes with, such as "open(…, 'wb')", or None when it opens nothing for writing."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
    if name in ("write_text", "write_bytes"):
        return f"{name}()"
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os" \
            and name == "open":
        flags = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "flags"), None)
        used = {node.attr for node in ast.walk(flags) if isinstance(node, ast.Attribute)} if flags else set()
        return "os.open(…, O_WRONLY/O_CREAT…)" if used & WRITE_FLAGS else None
    if name not in ("open", "fdopen"):
        return None
    # `open(path, "w")`, `path.open("wb")`, `os.fdopen(fd, "ab")`, `tarfile.open(path, "w:gz")`: the
    # mode is the first argument that looks like one, wherever it sits. A variable named `mode`
    # can't be shown to be a read, so it counts as a write.
    for arg in [*call.args, *(keyword.value for keyword in call.keywords if keyword.arg == "mode")]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and MODE.fullmatch(arg.value):
            return f"{name}(…, {arg.value!r})" if WRITE_MODES & set(arg.value) else None
        if isinstance(arg, ast.Name) and arg.id == "mode":
            return f"{name}(…, mode)"
    return None


class FilesRuleTest(unittest.TestCase):
    """C6: every write in `shared` goes through `atomic`, so no reader ever sees half a file."""

    def found(self):
        folder = Path(shared.__file__).parent
        found = {}
        for path in sorted(folder.glob("*.py")):
            if path.name != "_atomic.py":
                for function, line, what in writes(ast.parse(path.read_text(encoding="utf-8"))):
                    found.setdefault((path.name, function), []).append(f"{path.name}:{line} in {function}: {what}")
        return found

    def test_no_module_but_atomic_opens_a_file_for_writing(self):
        unexpected = [line for key, lines in self.found().items() if key not in ALLOWED for line in lines]
        self.assertEqual(unexpected, [], "write through shared.atomic, or list the exception in ALLOWED with why")

    def test_every_allowed_exception_is_still_needed(self):
        self.assertEqual(set(ALLOWED) - set(self.found()), set(), "remove the exceptions that no longer write")

    def test_the_check_catches_each_way_of_writing(self):
        source = """
def f(path, fd):
    open(path, "w"); path.open("wb"); os.fdopen(fd, "ab"); tarfile.open(path, mode=mode); tarfile.open(path, "w:gz")
    os.open(path, os.O_WRONLY | os.O_CREAT); path.write_text("x"); path.write_bytes(b"x")
    open(path); open("CONIN$", "rb"); path.open("rb"); os.open(path, os.O_RDONLY); tarfile.open(path, "r:*")
    zipped.open(entry.member); _pillow.open(path, memory=False)

class C:
    def m(self, path):
        open(path, "x")
"""
        self.assertEqual([(function, what) for function, _, what in writes(ast.parse(source))], [
            ("f", "open(…, 'w')"), ("f", "open(…, 'wb')"), ("f", "fdopen(…, 'ab')"), ("f", "open(…, mode)"),
            ("f", "open(…, 'w:gz')"), ("f", "os.open(…, O_WRONLY/O_CREAT…)"), ("f", "write_text()"),
            ("f", "write_bytes()"), ("C.m", "open(…, 'x')")])


if __name__ == "__main__":
    unittest.main()
