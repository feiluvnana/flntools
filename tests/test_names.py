"""The naming conventions of plan/phase-6/redesign.md §2, checked over the API snapshot.

The names are the ones `scripts/dev.py api` writes to tests/data/api.txt (test_api.py keeps that
file current), read from the same `api_names()` with their objects, so a check can look at a real
signature. Nothing is faked: the names come from importing `shared` and `flnd`.

Today's code breaks some conventions. Every such breach is in `ALLOWED`, by module, and nowhere else:
a new breach fails `test_no_new_breach`, and a fixed one still listed fails `test_no_stale_allowance`,
so the list can only shrink.
"""

import inspect
import re
import typing
import unittest
from typing import Any

from test_packaging import load_dev

dev = load_dev()
NAMES = dev.api_names()

# W7: prefixes that only say "this is a function".
PREFIXES = ("get_", "do_", "to_", "load_from_", "make_", "create_")
# §2.2: option names the vocabulary has another word for.
RENAMED = {"out": "dest", "dest_dir": "dest", "preset": "level"}
POSITIONAL = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)

# Every breach today's code has, by the module it is in (`media.video`'s under `media`). Each module's
# step-4 commit renames its names and removes its own entries here, with the api.txt diff that shows
# the renames. Nothing else is exempt: a new name that breaks a convention fails.
ALLOWED: dict[str, tuple[str, ...]] = {
    "shared.browser": (
        "shared.browser.Page.scroll: option progress, not a progress(done, total) callback",
        "shared.browser.installed_profile: positional option profile",
    ),
    "shared.bundler": (
        "shared.bundler.build: option out, not dest",
    ),
    "shared.cli": (
        "shared.cli.App.group: positional option description",
        "shared.cli.arg: positional option choices",
        "shared.cli.arg: positional option metavar",
        "shared.cli.arg: positional option nargs",
        "shared.cli.arg: positional option stdin",
        "shared.cli.arg: positional option type",
    ),
    "shared.cookies": (
        "shared.cookies.load_from_browser: prefix load_from_",
        "shared.cookies.load_from_file: prefix load_from_",
        "shared.cookies.to_file: prefix to_",
        "shared.cookies.to_header: prefix to_",
    ),
    "shared.fmt": (
        "shared.fmt.date: positional option pattern",
    ),
    "shared.log": (
        "shared.log.ProgressBar.update: positional option total",
    ),
    "shared.media": (
        "shared.media.compress: option preset, not level",
        "shared.media.video.frames: option dest_dir, not dest",
    ),
    "shared.theme": (
        "shared.theme.Screen: option out, not dest",
    ),
    "shared.worker": (
        "shared.worker.map: option progress, not a progress(done, total) callback",
        "shared.worker.pool: option progress, not a progress(done, total) callback",
        "shared.worker.race: option progress, not a progress(done, total) callback",
    ),
}


def breaches() -> set[str]:
    """Every breach of the conventions in the snapshot, one text per breach, as `ALLOWED` spells it."""
    found: set[str] = set()
    for name in NAMES:
        found |= prefix_breaches(name) | option_breaches(name)
    return found | error_breaches()


def prefix_breaches(name: Any) -> set[str]:
    """W7: the name's last part starts with a prefix that only says "function"."""
    last = name.name.rsplit(".", 1)[-1]
    return {f"{name.name}: prefix {prefix}" for prefix in PREFIXES if last.startswith(prefix)}


def option_breaches(name: Any) -> set[str]:
    """S3 and §2.2 over one function, method or class: in its signature and in each of its overloads."""
    if name.kind not in ("function", "method", "class"):
        return set()
    found: set[str] = set()
    overloads = typing.get_overloads(name.obj) if name.kind != "class" else []
    for function in [None, *overloads]:
        try:
            parameters = dev.parameters(name, function)
        except ValueError:  # no signature to read: a class whose constructor is a builtin's
            continue
        # S3, for functions and methods; a constructor's fields are its data, so they may be positional.
        if name.kind != "class" and not follows_protocol(name):
            found |= {f"{name.name}: positional option {parameter.name}" for parameter in parameters[1:]
                      if parameter.kind in POSITIONAL and parameter.default is not inspect.Parameter.empty}
        for parameter, scope in expanded(parameters, name.obj, function):
            if parameter.name in RENAMED:
                found.add(f"{name.name}: option {parameter.name}, not {RENAMED[parameter.name]}")
            if parameter.name == "progress" and not progress_callback(parameter.annotation, scope):
                found.add(f"{name.name}: option progress, not a progress(done, total) callback")
    return found


def follows_protocol(name: Any) -> bool:
    """Whether a method overrides one of a stdlib base's, as `Store.get` does `Mapping.get`.

    Such a method keeps the stdlib's signature (W4), positional default and all, since code written
    for the base calls it that way.
    """
    if name.kind != "method":
        return False
    member = name.name.rsplit(".", 1)[-1]
    return any(member in vars(base) for base in name.owner.__mro__[1:]
               if base.__module__.split(".")[0] not in dev.PROJECT_PACKAGES)


def expanded(parameters: list[inspect.Parameter], obj: Any, function: Any) -> list[tuple[inspect.Parameter, dict]]:
    """Each parameter with the globals its annotation is read in; `**options: Unpack[T]` becomes T's keys.

    `worker`'s functions take their options as a TypedDict, so its keys are the real option names.
    """
    scope = dev._scope(function or obj)
    found = []
    for parameter in parameters:
        unpacked = re.fullmatch(r"Unpack\[(\w+)\]", str(parameter.annotation))
        if parameter.kind is inspect.Parameter.VAR_KEYWORD and unpacked and unpacked.group(1) in scope:
            options = scope[unpacked.group(1)]
            options_scope = dev._scope(options)
            found += [(inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, annotation=annotation), options_scope)
                      for key, annotation in inspect.get_annotations(options).items()]
        else:
            found.append((parameter, scope))
    return found


def progress_callback(annotation: Any, scope: dict) -> bool:
    """Whether an annotation is a `progress(done, total)` callback, or None: `Callable[[A, B], R]` or `Callable[..., R]`.

    Args:
        annotation: the annotation as written, such as "Progress | None"; a bare alias is looked up.
        scope: the globals of the function's module, where an alias like `Progress` is defined.
    """
    kinds = [part for part in split_top_level(dev._annotation_text(annotation), "|") if part != "None"]
    if len(kinds) != 1:
        return False
    kind = kinds[0]
    if kind.isidentifier() and kind in scope:  # an alias: `Progress = Callable[..., Any]`
        kind = dev._annotation_text(scope[kind])
    kind = re.sub(r"^(typing\.|collections\.abc\.)", "", kind)
    if not kind.startswith("Callable[") or not kind.endswith("]"):
        return False
    arguments = split_top_level(kind[len("Callable["):-1], ",")[0]
    if arguments == "...":
        return True
    return arguments.startswith("[") and len([part for part in split_top_level(arguments[1:-1], ",") if part]) == 2


def split_top_level(text: str, separator: str) -> list[str]:
    """`text` split at `separator` outside brackets, each part stripped: "a[b, c], d" gives "a[b, c]" and "d"."""
    parts, depth, start = [], 0, 0
    for index, character in enumerate(text):
        depth += {"[": 1, "]": -1}.get(character, 0)
        if character == separator and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    return [*parts, text[start:].strip()]


def module_of(name: str, names: list[Any] = NAMES) -> str:
    """The module a name is in: the longest module among `names` that it starts with."""
    modules = {entry.name for entry in names if entry.kind == "module"}
    parts = name.split(".")
    return next(".".join(parts[:end]) for end in range(len(parts), 0, -1) if ".".join(parts[:end]) in modules)


def error_breaches(names: list[Any] = NAMES) -> set[str]:
    """S6: every module of `shared` has `Error(shared.Error)`, and each module's errors share its `Error`.

    Args:
        names: the snapshot's names; a test passes made-up ones.
    """
    found: set[str] = set()
    by_name = {entry.name: entry for entry in names}
    shared_error = by_name["shared.Error"].obj
    errors: dict[str, list[Any]] = {}
    for entry in names:
        if entry.kind == "error":
            errors.setdefault(module_of(entry.name, names), []).append(entry)
    for module in {entry.name for entry in names if entry.kind == "module"}:
        base = by_name.get(f"{module}.Error")
        # C4 asks every module directly under `shared` for its own Error, even one that raises nothing yet.
        if module.count(".") == 1 and module.startswith("shared.") and \
                (base is None or base.kind != "error" or not issubclass(base.obj, shared_error)):
            found.add(f"{module}: no {module}.Error under shared.Error")
        stray = sorted(entry.name for entry in errors.get(module, [])
                       if base is None or base.kind != "error" or not issubclass(entry.obj, base.obj))
        if stray:
            found.add(f"{module}: errors outside {module}.Error: {', '.join(stray)}")
    return found


class NamesTest(unittest.TestCase):
    def test_no_new_breach(self):
        allowed = {breach for group in ALLOWED.values() for breach in group}
        new = sorted(breaches() - allowed)
        self.assertEqual(new, [], "these break plan/phase-6/redesign.md §2; rename them, don't allow them")

    def test_no_stale_allowance(self):
        allowed = {breach for group in ALLOWED.values() for breach in group}
        stale = sorted(allowed - breaches())
        self.assertEqual(stale, [], "fixed: delete these from ALLOWED in tests/test_names.py")

    def test_allowances_are_grouped_by_their_module(self):
        for module, group in ALLOWED.items():
            for breach in group:
                found = module_of(breach.split(":", 1)[0])
                self.assertTrue(found == module or found.startswith(f"{module}."), breach)


class ChecksTest(unittest.TestCase):
    """The checks themselves, on made-up names, so a check that silently passes everything fails here."""

    def name(self, kind: str, obj: Any, *, owner: Any = None, bound: bool = False, name: str = "shared.x.f") -> Any:
        return dev.ApiName(name, kind, obj, owner, bound)

    def test_prefix(self):
        self.assertEqual(prefix_breaches(self.name("function", None, name="shared.x.get_keys")),
                         {"shared.x.get_keys: prefix get_"})
        self.assertEqual(prefix_breaches(self.name("function", None, name="shared.x.target")), set())

    def test_positional_option(self):
        def fetch(url, timeout=30.0, *, attempts=3): ...

        self.assertEqual(option_breaches(self.name("function", fetch)), {"shared.x.f: positional option timeout"})

    def test_first_parameter_may_have_a_default(self):
        def save(dest=None, *, mode=0o666): ...

        self.assertEqual(option_breaches(self.name("function", save)), set())

    def test_method_leaves_out_self(self):
        class Store:
            def keys(self, prefix=""): ...

        self.assertEqual(option_breaches(self.name("method", Store.keys, owner=Store, bound=True)), set())

    def test_a_stdlib_protocol_keeps_its_signature(self):
        class Store(dict):
            def get(self, key, default=None): ...      # dict.get's own signature

            def find(self, key, default=None): ...

        found = set()
        for member in (Store.get, Store.find):
            found |= option_breaches(self.name("method", member, owner=Store, bound=True,
                                               name=f"shared.x.Store.{member.__name__}"))
        self.assertEqual(found, {"shared.x.Store.find: positional option default"})

    def test_renamed_options(self):
        def build(entry, *, out="dist", dest_dir=None, preset="mid"): ...

        self.assertEqual(option_breaches(self.name("function", build)),
                         {"shared.x.f: option out, not dest", "shared.x.f: option dest_dir, not dest",
                          "shared.x.f: option preset, not level"})

    def test_progress_must_be_a_two_argument_callback(self):
        scope = {"Progress": typing.Callable[..., Any]}
        self.assertTrue(progress_callback("Callable[[float, float | None], Any] | None", scope))
        self.assertTrue(progress_callback("Progress | None", scope))
        self.assertFalse(progress_callback("str | None", scope))
        self.assertFalse(progress_callback("Callable[[], int] | None", scope))

    def test_errors_share_their_modules_error(self):
        class Error(Exception): ...

        class Shared(Error): ...       # stands for shared.Error

        class Module(Shared): ...      # shared.x.Error

        class Kept(Module): ...

        class Stray(ValueError): ...

        names = [self.name("error", Shared, name="shared.Error"), self.name("module", None, name="shared"),
                 self.name("module", None, name="shared.x"), self.name("error", Module, name="shared.x.Error"),
                 self.name("error", Kept, name="shared.x.KeptError"),
                 self.name("error", Stray, name="shared.x.Inner.StrayError"),
                 self.name("module", None, name="shared.y")]
        self.assertEqual(error_breaches(names), {"shared.x: errors outside shared.x.Error: shared.x.Inner.StrayError",
                                                 "shared.y: no shared.y.Error under shared.Error"})


if __name__ == "__main__":
    unittest.main()
