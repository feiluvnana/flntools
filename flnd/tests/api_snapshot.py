"""The API snapshot: every public name of flnd, one line each, as `tests/data/api.txt` holds it.

    make api                              # write tests/data/api.txt
    uv run python tests/api_snapshot.py   # print the snapshot instead

The snapshot is how a change to the public surface is reviewed: a rename, a new option or a new
error shows as a one-line diff of `tests/data/api.txt`. Three tests read this module:
`tests/test_api.py` checks that the file matches the code, `tests/test_names.py` checks the names
against the naming rules of AGENTS.md, and `tests/test_docs.py` finds the public names that must
have docstrings. They import it as `api_snapshot`, since `tests/` is on the path of a test run.

How the snapshot is made:
1. `api_names` imports each module of `API_MODULES` and walks its `__all__`.
2. Each name becomes an `ApiName`: a module, a function, a class or an error, and, inside a
   class, its methods, properties, fields and the attributes its `__init__` sets.
3. Each line is the name plus a detail: a signature for a function or class, the bases for an
   error, a kind and a type for the rest. `_signature` and `_default_text` draw them so that
   the text is the same on every OS, every Python from 3.11 and every run.
4. `api_text` sorts the lines, and `--write` saves them with Unix line ends.

It imports `flnd`, so it runs inside the project environment (`uv run`). It reads only the
public modules and what they expose: nothing here reaches into a private module.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import importlib
import inspect
import re
import sys
import textwrap
import types
import typing
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from pathlib import Path, PurePath
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parent.parent

# The modules the snapshot lists, each through its `__all__`: the APIs a plugin is written
# against, the library a script imports, and the kit a plugin is tested
# with.
API_MODULES = ("flnd.plugin", "flnd", "flnd.testing")
API_FILE = ROOT / "tests" / "data" / "api.txt"
# Classes whose members are listed: the project's own. A base from the stdlib (MutableMapping,
# NamedTuple, Exception) adds its own methods, which aren't this project's API.
PROJECT_PACKAGES = ("flnd", "flnkit")
# Each package's folder: flnd is the folder src/, flnkit its own (AGENTS.md › Layout).
PACKAGE_DIRS = {"flnd": ROOT / "src", "flnkit": ROOT / "flnkit"}


class ApiName(NamedTuple):
    """One public name: one line of the API snapshot, and the object the line was made from."""

    # The dotted name a caller types, such as "flnd.plugin.Plugin.resolve".
    name: str
    # "module" (also an object standing in for one, such as `flnd.ui`), "function", "method",
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
    """Write `api_text()` to `tests/data/api.txt`, with Unix line ends on every OS, so the file
    never differs by OS."""
    API_FILE.write_text(api_text(), encoding="utf-8", newline="\n")


def api_text(modules: tuple[str, ...] = API_MODULES) -> str:
    """The API snapshot: one line per public name, sorted, as `tests/data/api.txt` holds it.

        flnd.db.Store.keys(prefix: str = '') -> KeysView[str]
        flnd.Blocked(flnd.SiteError)
        flnd.ui.level: property, settable -> Level

    Functions and methods show their signature without `self`, classes their constructor's, errors
    their bases, and the rest their kind and type. Annotations are shown as written in the source.
    """
    return "".join(f"{name.line}\n" for name in api_names(modules))


def api_names(modules: tuple[str, ...] = API_MODULES) -> list[ApiName]:
    """Every public name of `API_MODULES`, sorted by name.

        names = {name.name: name for name in api_names()}
        names["flnd.db.Store.keys"].kind     # "method"

    A module's names are its `__all__`. A class's are its public methods, properties, fields and
    class attributes, its own and its project bases', and the public attributes its `__init__`
    sets on `self`. An object that stands in for a module (`flnd.ui` is the `ui` object of
    `src/_ui.py`) has its class's members, then the rest of its file's `__all__`, under its
    name. Imports every module listed, so it runs inside the project environment.

    Returns:
        One `ApiName` per name; a name reached twice (a class re-exported) is listed once.
    """
    names: dict[str, ApiName] = {}
    for module_name in modules:
        _add(module_name, importlib.import_module(module_name), names)
    # Errors show their bases by public name, which is known only once everything is collected.
    public = {}
    for name in sorted(names.values(), key=lambda name: (len(name.name), name.name)):
        if name.kind in ("class", "error"):
            public.setdefault(id(name.obj), name.name)
    for name, entry in names.items():
        if entry.kind == "error":
            bases = ", ".join(
                public.get(id(base), base.__qualname__) for base in entry.obj.__bases__
            )
            names[name] = entry._replace(detail=f"({bases})")
    return sorted(names.values(), key=lambda name: name.name)


def parameters(
    name: ApiName, function: Callable[..., Any] | None = None
) -> list[inspect.Parameter]:
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
        if value.__name__.split(".")[0] not in PROJECT_PACKAGES:
            return
        for member in value.__all__:
            member_value = getattr(value, member)
            # A public name that is an object, not a module, stands for the private file `_<name>`
            # beside it, the way `flnd.ui` is the `ui` object of `src/_ui.py`.
            backing = sys.modules.get(f"{value.__name__}._{member}")
            if backing is not None and not isinstance(member_value, types.ModuleType):
                _add_stand_in(f"{name}.{member}", member_value, backing, names)
            else:
                _add(f"{name}.{member}", member_value, names)
    elif inspect.isclass(value):
        kind = "error" if issubclass(value, BaseException) else "class"
        names[name] = ApiName(name, kind, value, owner,
                              detail="" if kind == "error" else _signature(value))
        _add_members(name, value, names)
    elif inspect.isfunction(value):
        names[name] = ApiName(name, "function", value, owner, detail=_signature(value))
    else:
        names[name] = ApiName(name, "value", value, owner, detail=f": {_kind(value)}")


def _add_stand_in(
    name: str, value: Any, backing: types.ModuleType, names: dict[str, ApiName]
) -> None:
    """Add an object that stands in for a module: its class's members, then its file's
    other names."""
    names[name] = ApiName(name, "module", value, detail=f": {builtins.type(value).__name__}")
    _add_members(name, builtins.type(value), names)
    for member in backing.__all__:
        if getattr(backing, member) is not value:
            _add(f"{name}.{member}", getattr(backing, member), names)


def _add_members(name: str, cls: builtins.type, names: dict[str, ApiName]) -> None:
    """Add a class's public members: fields, then its own and its project bases' members,
    then `__init__`'s.

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
                names.setdefault(f"{name}.{member}", ApiName(f"{name}.{member}", "attribute", None,
                                                             cls,
                                                             detail=_typed(": attribute",
                                                                           annotation)))


def _add_member(name: str, raw: Any, cls: builtins.type, names: dict[str, ApiName], *,
                annotation: Any) -> None:
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
        names[name] = ApiName(name, "method", function, cls, bound, _signature(function,
                                                                               bound=bound))
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
                              detail=_typed(": attribute",
                                            annotation or builtins.type(raw).__name__))


def _record_fields(cls: builtins.type) -> dict[str, Any]:
    """The fields of a dataclass or a NamedTuple, in order, with their annotations;
    empty for any other class."""
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
    """A function's or class's signature as the snapshot shows it:
    `(path, *, table: str = 'default', timeout: float = 5.0) -> Store`.

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
                all(earlier.kind is not inspect.Parameter.VAR_POSITIONAL
                    for earlier in found[:index]) and \
                (index == 0 or found[index - 1].kind is not inspect.Parameter.KEYWORD_ONLY):
            parts.append("*")
        parts.append(_parameter_text(parameter, scope))
        last_positional_only = parameter.kind is inspect.Parameter.POSITIONAL_ONLY and \
            (index + 1 == len(found)
             or found[index + 1].kind is not inspect.Parameter.POSITIONAL_ONLY)
        if last_positional_only:
            parts.append("/")
    returns = "" if signature.return_annotation is inspect.Signature.empty or inspect.isclass(obj) \
        else f" -> {_annotation_text(signature.return_annotation)}"
    return f"({', '.join(parts)}){returns}"


def _parameter_text(parameter: inspect.Parameter, scope: dict[str, Any]) -> str:
    """One parameter: `name: annotation = default`, or `name=default` without an annotation."""
    stars = {inspect.Parameter.VAR_POSITIONAL: "*",
             inspect.Parameter.VAR_KEYWORD: "**"}.get(parameter.kind, "")
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

    Python 3.11's NamedTuple wraps that string in a `ForwardRef`, which is unwrapped, so the
    snapshot reads the same on every supported Python.
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
    return f"{builtins.type(value).__name__}(...)" if re.search(r" at 0x[0-9a-fA-F]+",
                                                                text) else text


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



def main(argv: list[str] | None = None) -> int:
    """Print the snapshot, or write it to `tests/data/api.txt` with `--write`.

    Args:
        argv: the command-line arguments; `None` reads `sys.argv`.

    Returns:
        0, the exit code.
    """
    parser = argparse.ArgumentParser(description="Print or write the public API snapshot.")
    parser.add_argument("--write", action="store_true", help=f"write {API_FILE.relative_to(ROOT)}")
    args = parser.parse_args(argv)
    if args.write:
        write_api()
    else:
        sys.stdout.write(api_text())
    return 0


def _kind(value: object) -> str:
    """The name of a value's type, the same on every supported Python.

    A `X | Y` alias is a `typing.Union` on 3.14 but a private `_UnionGenericAlias` on 3.11, and the
    snapshot must not change with the interpreter, so both read as `Union`.
    """
    name = builtins.type(value).__name__
    return "Union" if name in ("Union", "_UnionGenericAlias", "UnionType") else name


if __name__ == "__main__":
    sys.exit(main())
