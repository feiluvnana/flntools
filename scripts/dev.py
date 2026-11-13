"""Every development task of flntools, the same on macOS, Linux and Windows, with nothing but uv.

    uv run --no-project scripts/dev.py setup
    uv run --no-project scripts/dev.py check --python 3.11
    uv run --no-project scripts/dev.py flnd download ins:example_user

`main` takes the task name from `sys.argv[1]`, then an optional `--python X` (the environment's
`PYTHON` otherwise, 3.14 by default), and hands the rest to the task function of that name. Every
task takes the same two arguments: `python`, the version passed to uv's `--python`, and `args`,
the command-line arguments after the task name, which `setup`, `test`, `type` and `flnd` pass on. Each
task runs uv commands in the project root; uv creates and syncs `.venv` from `uv.lock`, so this
script only uses the stdlib and runs under `--no-project`, outside the environment it manages.
The Makefile is a shortcut to it and holds no logic of its own.

`api` is the one task that needs the project's own code: it writes the API snapshot, which imports
`shared` and `flnd`, so it runs `api_text` inside the environment through `uv run python`. The same
`api_names` feed `tests/test_api.py` (the snapshot is current) and `tests/test_names.py` (it follows
the naming conventions of plan/phase-6/redesign.md §2).

An unknown or missing task prints the task list to stderr and exits 2. A failing command stops
the task and exits with that command's own code, so make and CI see the failure.
"""

import ast
import builtins
import hashlib
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
import textwrap
import types
import typing
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from pathlib import Path, PurePath
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parent.parent
# The Python setup builds .venv with when neither --python nor PYTHON says otherwise; releases also check 3.11.
DEFAULT_PYTHON = "3.14"

FFMPEG_SOURCE_HEADER = ("ffmpeg-source\n"
                        "The ffmpeg program in this bundle, as it reports itself. The source offer is in ffmpeg.txt.\n")

# The modules the API snapshot lists, each through its `__all__`: the toolkit, the module sources
# are written against, and the engine API (`flnd`'s own `__all__`, redesign B1).
API_MODULES = ("shared", "flnd.source", "flnd")
API_FILE = ROOT / "tests" / "data" / "api.txt"
# Classes whose members are listed: the project's own. A base from the stdlib (MutableMapping,
# NamedTuple, Exception) adds its own methods, which aren't this project's API.
PROJECT_PACKAGES = ("shared", "flnd")
# What `api` runs inside the environment: this file, imported from scripts/, writing the snapshot.
WRITE_API = "import sys; sys.path.insert(0, 'scripts'); import dev; dev.write_api()"


def setup(python: str, args: list[str]) -> None:
    """Create or update `.venv` from `uv.lock`, with the dev group; downloads no browser.

    `--extra archive` keeps pyzipper and py7zr installed for the archive tests, also once flnd's own
    dependencies drop them. A second run checks the lock and installs nothing, well under a second.
    """
    run(["uv", "sync", "--python", python, "--extra", "archive", *args])


def test(python: str, args: list[str]) -> None:
    """Run every unittest under `tests/`."""
    run(["uv", "run", "--python", python, "python", "-m", "unittest", "discover", "-s", "tests", *args])


def lint(python: str, args: list[str]) -> None:
    """Byte-compile `flnd` and `shared`, then check their docstrings (`doc_breaches`); `--strict` ignores `EXPLAINING`.

    The docstring check reports every file's breach count. It fails on a file that breaches and isn't in
    `EXPLAINING`, and on a file in `EXPLAINING` that already passes, so the list only shrinks; once it is
    empty, every breach fails. Exits 1 on failure.
    """
    run(["uv", "run", "--python", python, "python", "-m", "compileall", "-q", "flnd", "shared"])
    counts = {path: len(doc_breaches(path)) for path in source_files()}
    strict = "--strict" in args or not EXPLAINING
    failed = False
    for path, count in counts.items():
        listed = path in EXPLAINING and not strict
        if count and listed:
            print(f"{path}: {count} docstring breaches (still being explained)")
        elif count:
            failed = True
            print(f"{path}: {count} docstring breaches")
            for breach in doc_breaches(path):
                print(f"    {breach}")
        elif listed:
            failed = True
            print(f"{path}: meets the docstring standard, so remove it from EXPLAINING in scripts/dev.py")
    if failed:
        raise subprocess.CalledProcessError(1, ["lint"])


def baseline(python: str, args: list[str]) -> None:
    """Write `tests/data/code_baseline.json`: one hash of each file's code without docstrings and comments."""
    BASELINE.write_text(json.dumps({"python": PYTHON_OF_BASELINE, "files": code_hashes()}, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")


def type(python: str, args: list[str]) -> None:  # noqa: A001 - the task's name; the snapshot code says `builtins.type`
    """Type-check with pyright, whose Node.js comes from the `pyright[nodejs]` wheel."""
    run(["uv", "run", "--python", python, "pyright", *args])


def check(python: str, args: list[str]) -> None:
    """Run `lint`, `type` and `test` in that order, stopping at the first failure."""
    for step in (lint, type, test):
        step(python, [])


def flnd(python: str, args: list[str]) -> None:
    """Run `flnd` from the project environment, passing every remaining argument through."""
    run(["uv", "run", "--python", python, "flnd", *args])


def try_(python: str, args: list[str]) -> None:
    """Run a source for real on one target and print what it would save, downloading and recording nothing.

        dev.py try ins:example_user --from reels --time 2026-01-01,

    The listing is live (network, browser, login) with the saved login of the `.flnd` in this
    folder; the post tree comes out with each file's planned path and its URL without the query,
    so no signed token is printed. This is gallery-dl's `--simulate`, kept out of `flnd` itself.
    The work is `flnd/dry.py`, which needs the project's code, so it runs inside the environment.
    """
    run(["uv", "run", "--python", python, "python", "-m", "flnd.dry", *args])


def api(python: str, args: list[str]) -> None:
    """Write `tests/data/api.txt`, the API snapshot, from inside the project environment.

    The snapshot imports `shared` and `flnd`, which this script can't do under `--no-project`, so it
    runs `write_api` through `uv run python`. Commit the file with the change it shows: a rename is a
    one-line diff there, and `tests/test_api.py` fails until the file matches the code.
    """
    run(["uv", "run", "--python", python, "python", "-c", WRITE_API])


def dist(python: str, args: list[str]) -> None:
    """Build the one-folder bundle `dist/flnd/` and add `licenses/ffmpeg-source.txt` to it."""
    # PyInstaller lives in the `dist` group, which `setup` leaves out; `uv run` below keeps it installed.
    run(["uv", "sync", "--python", python, "--group", "dist"])
    # This script runs outside the project environment, so the build runs inside it, through uv.
    build = "from shared import bundler; bundler.build('flnd/__main__.py', name='flnd', data=('licenses',), dest='dist')"
    run(["uv", "run", "--python", python, "--group", "dist", "python", "-c", build])
    # utf-8 on every OS: a shell's `>` on Windows would write the console's code page instead.
    (ROOT / "dist" / "flnd" / "licenses" / "ffmpeg-source.txt").write_text(ffmpeg_source(python), encoding="utf-8")


def ffmpeg_source(python: str = DEFAULT_PYTHON) -> str:
    """The text of `ffmpeg-source.txt`: the bundled ffmpeg's `-version` output, for the GPL source offer.

        Path("ffmpeg-source.txt").write_text(ffmpeg_source(), encoding="utf-8")

    Args:
        python: the Python version `uv run` uses to find imageio-ffmpeg's binary in the project environment.
    """
    find = "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
    ffmpeg = run(["uv", "run", "--python", python, "python", "-c", find], capture_output=True).stdout.strip()
    version = run([ffmpeg, "-hide_banner", "-version"], capture_output=True).stdout
    return f"{FFMPEG_SOURCE_HEADER}\n{version}\n"


class ApiName(NamedTuple):
    """One public name: one line of the API snapshot, and the object the line was made from."""

    # The dotted name a caller types, such as "shared.kv.Store.keys".
    name: str
    # "module" (also an object standing in for one, such as `shared.log`), "function", "method",
    # "class", "error", "property", "field", "attribute" or "value".
    kind: str
    # The function (taken out of a staticmethod or classmethod), class, property or value.
    obj: Any
    # The class a member was found on; None for a module's own names.
    owner: Any = None
    # Whether a method's first parameter is `self` or `cls`, which a caller never passes.
    bound: bool = False
    # The rest of the line after the name: a signature, an error's bases, or a kind and a type.
    detail: str = ""

    @property
    def line(self) -> str:
        """The snapshot's line for this name."""
        return f"{self.name}{self.detail}"


def write_api() -> None:
    """Write `api_text()` to `tests/data/api.txt`, with Unix line ends on every OS, so the file never differs by OS."""
    API_FILE.write_text(api_text(), encoding="utf-8", newline="\n")


def api_text() -> str:
    """The API snapshot: one line per public name, sorted, as `tests/data/api.txt` holds it.

        shared.kv.Store.keys(prefix: str = '') -> KeysView[str]
        shared.archive.BombError(shared.archive.Error, shared.UnsafeError)
        shared.log.level: property, settable -> Level

    Functions and methods show their signature without `self`, classes their constructor's, errors
    their bases, and the rest their kind and type. Annotations are shown as written in the source.
    """
    return "".join(f"{name.line}\n" for name in api_names())


def api_names() -> list[ApiName]:
    """Every public name of `API_MODULES`, sorted by name.

        names = {name.name: name for name in api_names()}
        names["shared.kv.Store.keys"].kind     # "method"

    A module's names are its `__all__`. A class's are its public methods, properties, fields and
    class attributes, its own and its project bases', and the public attributes its `__init__`
    sets on `self`. An object that stands in for a module (`shared.log` is the `log` object of
    `shared/_log.py`) has its class's members, then the rest of its file's `__all__`, under its
    name. Imports every module listed, so it runs inside the project environment.

    Returns:
        One `ApiName` per name; a name reached twice (a class re-exported) is listed once.
    """
    names: dict[str, ApiName] = {}
    for module_name in API_MODULES:
        _add(module_name, importlib.import_module(module_name), names)
    # Errors show their bases by public name, which is known only once everything is collected.
    public = {}
    for name in sorted(names.values(), key=lambda name: (len(name.name), name.name)):
        if name.kind in ("class", "error"):
            public.setdefault(id(name.obj), name.name)
    for name, entry in names.items():
        if entry.kind == "error":
            bases = ", ".join(public.get(id(base), base.__qualname__) for base in entry.obj.__bases__)
            names[name] = entry._replace(detail=f"({bases})")
    return sorted(names.values(), key=lambda name: name.name)


def parameters(name: ApiName, function: Callable[..., Any] | None = None) -> list[inspect.Parameter]:
    """The parameters a caller passes to a function, method or class: `self` and `cls` left out.

    Args:
        name: the function, method or class.
        function: one of the function's `typing.get_overloads`, read instead of the function itself.

    Raises:
        ValueError: the object has no signature, such as a builtin.
    """
    found = list(inspect.signature(function or name.obj).parameters.values())
    return found[1:] if name.bound else found


def _add(name: str, value: Any, names: dict[str, ApiName], owner: Any = None) -> None:
    """Add `value` as `name`, then the names inside it: a module's `__all__`, a class's members."""
    if name in names:
        return
    if isinstance(value, types.ModuleType):
        names[name] = ApiName(name, "module", value, owner, detail=": module")
        for member in value.__all__:
            member_value = getattr(value, member)
            # A public name that is an object, not a module, stands for the private file `_<name>`
            # beside it, the way `shared.log` is the `log` object of `shared/_log.py`.
            backing = sys.modules.get(f"{value.__name__}._{member}")
            if backing is not None and not isinstance(member_value, types.ModuleType):
                _add_stand_in(f"{name}.{member}", member_value, backing, names)
            else:
                _add(f"{name}.{member}", member_value, names)
    elif inspect.isclass(value):
        kind = "error" if issubclass(value, BaseException) else "class"
        names[name] = ApiName(name, kind, value, owner, detail="" if kind == "error" else _signature(value))
        _add_members(name, value, names)
    elif inspect.isfunction(value):
        names[name] = ApiName(name, "function", value, owner, detail=_signature(value))
    else:
        names[name] = ApiName(name, "value", value, owner, detail=f": {builtins.type(value).__name__}")


def _add_stand_in(name: str, value: Any, backing: types.ModuleType, names: dict[str, ApiName]) -> None:
    """Add an object that stands in for a module: its class's members, then its file's other names."""
    names[name] = ApiName(name, "module", value, detail=f": {builtins.type(value).__name__}")
    _add_members(name, builtins.type(value), names)
    for member in backing.__all__:
        if getattr(backing, member) is not value:
            _add(f"{name}.{member}", getattr(backing, member), names)


def _add_members(name: str, cls: builtins.type, names: dict[str, ApiName]) -> None:
    """Add a class's public members: fields, then its own and its project bases' members, then `__init__`'s.

    Nearest class first, so an override is listed with its own signature, and a property keeps its
    place even when `__init__` assigns to it (`self.color = …` runs `Theme.color`'s setter).
    """
    seen: set[str] = set()
    for member, annotation in _record_fields(cls).items():
        seen.add(member)
        if not member.startswith("_"):
            names.setdefault(f"{name}.{member}", ApiName(f"{name}.{member}", "field", None, cls,
                                                         detail=_typed(": field", annotation)))
    for klass in cls.__mro__:
        if klass.__module__.split(".")[0] not in PROJECT_PACKAGES:
            continue
        # inspect.get_annotations, not `__annotations__`, which Python 3.14 makes on first use.
        annotations = inspect.get_annotations(klass)
        for member, raw in vars(klass).items():
            if member.startswith("_") or member in seen:
                continue
            seen.add(member)
            _add_member(f"{name}.{member}", raw, cls, names, annotation=annotations.get(member))
        # Instance attributes exist only once `__init__` has run, so they are read from its source.
        for member, annotation in _instance_attributes(klass).items():
            if member not in seen:
                seen.add(member)
                names.setdefault(f"{name}.{member}", ApiName(f"{name}.{member}", "attribute", None, cls,
                                                             detail=_typed(": attribute", annotation)))


def _add_member(name: str, raw: Any, cls: builtins.type, names: dict[str, ApiName], *, annotation: Any) -> None:
    """Add one member found in a class's `__dict__`, by what it is.

    Args:
        name: the member's dotted name.
        raw: the value in the class's `__dict__`, before the descriptor protocol unwraps it.
        cls: the class whose members are being listed.
        names: the names found so far, added to.
        annotation: its annotation in the class body, or None.
    """
    if name in names:
        return
    if isinstance(raw, (staticmethod, classmethod)):
        function = raw.__func__
        bound = isinstance(raw, classmethod)
        names[name] = ApiName(name, "method", function, cls, bound, _signature(function, bound=bound))
    elif inspect.isfunction(raw):
        names[name] = ApiName(name, "method", raw, cls, True, _signature(raw, bound=True))
    elif isinstance(raw, property):
        returns = inspect.signature(raw.fget).return_annotation if raw.fget else None
        kind = ": property, settable" if raw.fset else ": property"
        names[name] = ApiName(name, "property", raw, cls, detail=_typed(kind, returns))
    elif inspect.isclass(raw):
        _add(name, raw, names, owner=cls)
    else:
        names[name] = ApiName(name, "attribute", raw, cls,
                              detail=_typed(": attribute", annotation or builtins.type(raw).__name__))


def _record_fields(cls: builtins.type) -> dict[str, Any]:
    """The fields of a dataclass or a NamedTuple, in order, with their annotations; empty for any other class."""
    if is_dataclass(cls):
        return {field.name: field.type for field in fields(cls)}
    annotations = inspect.get_annotations(cls)
    return {field: annotations.get(field) for field in getattr(cls, "_fields", ())}


def _instance_attributes(cls: builtins.type) -> dict[str, str]:
    """The public attributes a class's own `__init__` sets on `self`, with their annotations or "".

    Read from the source with `ast`, so nothing is constructed. A class without an `__init__` of its
    own in Python, such as a dataclass, whose `__init__` is generated, has none.
    """
    init = vars(cls).get("__init__")
    if not inspect.isfunction(init):
        return {}
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(init)))
    except (OSError, TypeError):  # generated code (a dataclass's) has no source file to read
        return {}
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            targets = [(node.target, ast.unparse(node.annotation))]
        elif isinstance(node, ast.Assign):
            # `self.home, self.sources = home, sources` sets two attributes.
            targets = [(element, "") for target in node.targets
                       for element in (target.elts if isinstance(target, ast.Tuple) else [target])]
        else:
            continue
        for target, annotation in targets:
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                    and target.value.id == "self" and not target.attr.startswith("_"):
                found.setdefault(target.attr, annotation)
    return found


def _signature(obj: Any, *, bound: bool = False) -> str:
    """A function's or class's signature as the snapshot shows it: `(path, *, table: str = 'default', timeout: float = 5.0) -> Store`.

    Args:
        obj: the function or class.
        bound: leave out the first parameter, a method's `self` or `cls`.
    """
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):  # a builtin, or a class whose constructor comes from C
        return "(...)"
    found = list(signature.parameters.values())[1 if bound else 0:]
    scope = _scope(obj)
    parts: list[str] = []
    for index, parameter in enumerate(found):
        # The markers Python's own rendering puts in: `/` after the positional-only parameters,
        # and `*` before the keyword-only ones when there is no `*args`.
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and \
                all(earlier.kind is not inspect.Parameter.VAR_POSITIONAL for earlier in found[:index]) and \
                (index == 0 or found[index - 1].kind is not inspect.Parameter.KEYWORD_ONLY):
            parts.append("*")
        parts.append(_parameter_text(parameter, scope))
        last_positional_only = parameter.kind is inspect.Parameter.POSITIONAL_ONLY and \
            (index + 1 == len(found) or found[index + 1].kind is not inspect.Parameter.POSITIONAL_ONLY)
        if last_positional_only:
            parts.append("/")
    returns = "" if signature.return_annotation is inspect.Signature.empty or inspect.isclass(obj) \
        else f" -> {_annotation_text(signature.return_annotation)}"
    return f"({', '.join(parts)}){returns}"


def _parameter_text(parameter: inspect.Parameter, scope: dict[str, Any]) -> str:
    """One parameter: `name: annotation = default`, or `name=default` without an annotation."""
    stars = {inspect.Parameter.VAR_POSITIONAL: "*", inspect.Parameter.VAR_KEYWORD: "**"}.get(parameter.kind, "")
    text = f"{stars}{parameter.name}"
    annotated = parameter.annotation is not inspect.Parameter.empty
    if annotated:
        text += f": {_annotation_text(parameter.annotation)}"
    if parameter.default is not inspect.Parameter.empty:
        text += f" = {_default_text(parameter.default, scope)}" if annotated \
            else f"={_default_text(parameter.default, scope)}"
    return text


def _annotation_text(annotation: Any) -> str:
    """An annotation as written: `from __future__ import annotations` keeps it a string already.

    Python 3.11's NamedTuple wraps that string in a `ForwardRef`, which is unwrapped, so the snapshot
    reads the same on every supported Python.
    """
    if isinstance(annotation, typing.ForwardRef):
        return annotation.__forward_arg__
    return annotation if isinstance(annotation, str) else inspect.formatannotation(annotation)


def _default_text(value: Any, scope: dict[str, Any]) -> str:
    """A default value, the same on every OS and in every run.

    A `Path` is shown without its OS's class name (`PosixPath`), a module-level setting by its name
    (`sources = SOURCES`, not the dict), a function or class by its name, and anything whose
    `repr` holds a memory address by its type alone.

    Args:
        value: the default.
        scope: the globals of the function's module, where a module-level setting is looked up.
    """
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return repr(value)
    if isinstance(value, PurePath):
        return f"Path({value.as_posix()!r})"
    if isinstance(value, tuple):
        items = [_default_text(item, scope) for item in value]
        return f"({items[0]},)" if len(items) == 1 else f"({', '.join(items)})"
    if isinstance(value, frozenset) and not value:
        return "frozenset()"
    # By identity, so only for objects that aren't shared singletons the way () and small ints are.
    for name, bound in scope.items():
        if bound is value and not name.startswith("__"):
            return name
    if inspect.isclass(value) or inspect.isroutine(value):
        return value.__qualname__
    text = repr(value)
    return f"{builtins.type(value).__name__}(...)" if re.search(r" at 0x[0-9a-fA-F]+", text) else text


def _scope(obj: Any) -> dict[str, Any]:
    """The globals of the module a function or class is defined in."""
    function = inspect.unwrap(obj) if inspect.isfunction(obj) else None
    if function is not None:
        return function.__globals__
    module = sys.modules.get(getattr(obj, "__module__", "") or "")
    return vars(module) if module else {}


def _typed(kind: str, annotation: Any) -> str:
    """A member's kind with its type when known: `: property -> str`, or `: attribute`."""
    missing = annotation is None or annotation == "" or annotation is inspect.Signature.empty
    text = "" if missing else _annotation_text(annotation)
    return f"{kind} -> {text}" if text else kind


def run(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    """Run `command` in the project root, raising CalledProcessError when it fails.

    Always a list of arguments, never a shell string, so quoting can't differ between sh and cmd.exe.
    """
    return subprocess.run(command, cwd=ROOT, check=True, capture_output=capture_output, text=True)


# Files whose docstrings are still being brought up to plan/phase-6/comments.md's standard. A file leaves the
# list when it meets the standard (lint fails while it stays), and none joins it; empty, lint is always strict.
EXPLAINING: set[str] = {
}

BASELINE = ROOT / "tests" / "data" / "code_baseline.json"
# `ast.unparse` prints some code differently from one Python version to the next, so the baseline is only
# comparable on the version that wrote it.
PYTHON_OF_BASELINE = "3.14"


def source_files() -> list[str]:
    """The `.py` files of `flnd/` and `shared/` as sorted paths relative to the project root, with `/` separators."""
    return sorted(p.relative_to(ROOT).as_posix() for folder in ("flnd", "shared") for p in (ROOT / folder).rglob("*.py"))


def without_docstrings(tree: ast.Module) -> ast.Module:
    """`tree` with every module, class and function docstring removed (a body left empty gets `pass`)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and ast.get_docstring(node, clean=False) is not None:
            node.body = node.body[1:] or [ast.Pass()]
    return tree


def code_hashes() -> dict[str, str]:
    """The SHA-256 of each source file's code with docstrings dropped; comments never reach the AST."""
    return {path: hashlib.sha256(ast.unparse(without_docstrings(ast.parse((ROOT / path).read_text(encoding="utf-8")))).encode()).hexdigest()
            for path in source_files()}


def doc_breaches(path: str) -> list[str]:
    """The ways a file breaks the docstring standard, one `line N: what` text each."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    found = [] if ast.get_docstring(tree) else ["line 1: module has no docstring"]

    def visit(node: ast.AST, public: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                judge(child, public and not child.name.startswith("_"))
                visit(child, public and not child.name.startswith("_"))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                judge(child, public and not child.name.startswith("_"))
            elif not isinstance(child, (ast.expr, ast.Lambda)):
                visit(child, public)

    def judge(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, public: bool) -> None:
        doc = ast.get_docstring(node)
        where = f"line {node.lineno}: {node.name}"
        if isinstance(node, ast.ClassDef):
            if public and not doc:
                found.append(f"{where} has no docstring")
            return
        if not public:
            if not doc and (node.end_lineno or node.lineno) - node.lineno + 1 > 15:
                found.append(f"{where} is private, over 15 lines and has no docstring")
            return
        if any(isinstance(d, ast.Name) and d.id == "overload" for d in node.decorator_list):
            return
        if not doc:
            found.append(f"{where} has no docstring")
            return
        a = node.args
        names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if x and x.arg not in ("self", "cls")]
        if len(names) >= 2 and "\n" not in doc.strip():
            found.append(f"{where} takes {len(names)} arguments but has a one-line docstring")
        listed = _args_section(doc)
        if listed is not None and set(listed) != set(names):
            found.append(f"{where}: Args lacks {sorted(set(names) - set(listed))} and names {sorted(set(listed) - set(names))}")

    visit(tree, True)
    return found


def _args_section(doc: str) -> list[str] | None:
    """The argument names an `Args:` section lists, or None when the docstring has no such section."""
    lines = doc.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "Args:"), None)
    if start is None:
        return None
    indent = len(lines[start]) - len(lines[start].lstrip())
    names: list[str] = []
    item = None
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        depth = len(line) - len(line.lstrip())
        if depth <= indent:
            break
        item = depth if item is None else item
        match = re.match(r"\*{0,2}(\w+)(?: \([^)]*\))?:", line.strip())
        if depth == item and match:
            names.append(match.group(1))
    return names


TASKS: dict[str, Callable[[str, list[str]], None]] = {
    # `try` is a keyword, so its function is `try_` and the task keeps the plain name.
    task.__name__.rstrip("_"): task for task in (setup, test, lint, type, check, baseline, api, flnd, dist, try_)
}


def main(argv: list[str]) -> int:
    """Run the task `argv[0]` with the rest of `argv`, and return the exit code.

        main(["check", "--python", "3.11"])

    Args:
        argv: the task name, then an optional `--python X`, then the task's own arguments.

    Returns:
        0 on success, 2 for an unknown or missing task, or the failing command's own exit code.
    """
    if not argv or argv[0] not in TASKS:
        print(f"usage: dev.py <task> [--python X] [args...]\ntasks: {' '.join(TASKS)}", file=sys.stderr)
        return 2
    name, args = argv[0], argv[1:]
    python = os.environ.get("PYTHON") or DEFAULT_PYTHON
    # Only right after the task, so `flnd`'s own arguments are passed through untouched.
    if args[:1] == ["--python"] and len(args) > 1:
        python, args = args[1], args[2:]
    try:
        TASKS[name](python, args)
    except subprocess.CalledProcessError as error:
        # The command already printed its own error; a traceback here would only bury it.
        return error.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
