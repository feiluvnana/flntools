# typing

Phase 4, step 1, then checked in every later step. `shared` and flnd are type-checked with pyright in **`standard`** mode, and `shared` ships its types.

## Setup

- **`shared/py.typed`:** an empty marker file, so type checkers read `shared`'s annotations. hatch packages it.
- **pyright** in `standard` mode, with `pythonVersion = "3.10"`, over `shared`, `flnd` and `tests/typing`. It is pinned in the dev group.
- **`make typecheck`**, and **`make check`**, which runs `lint`, `typecheck` and `test`.
- **`typing_extensions`** is imported only under `TYPE_CHECKING`, so `shared` stays stdlib-only at runtime. It's a dev dependency.

```toml
[tool.pyright]
include = ["shared", "flnd", "tests/typing"]
typeCheckingMode = "standard"
pythonVersion = "3.10"
venvPath = "."
venv = ".venv"
reportUnnecessaryTypeIgnoreComment = "error"
```

`venvPath` and `venv` point pyright at `.venv`, where the extras are installed. Without them it can't resolve httpx, parsel or cryptography.

## What must type-check

- **The 33 errors** pyright finds today are fixed. 9 are real problems, including the `fmt.date(True)` bug, `__all__` being built at runtime, and flnd's annotations naming `httpx.Client` where it called methods that only `shared.http.Client` had. The other 24 are missing `None` narrowing, an undeclared attribute, and Windows-only code without a `sys.platform` guard.
- **Lazy modules:** `shared/__init__.py` keeps its runtime `__getattr__`, and adds a `TYPE_CHECKING` block that imports each module under its public name. Checkers then see the real modules, and runtime stays lazy.
- **Generics:**
  - `worker.map(fn, items)` returns `list[R]`, and `Result` is `Result[R]`. Overloads cover one to three iterables, like the builtin `map`; beyond that it is `Any`.
  - `prompt.choice` returns the mapping's value type, or the sequence's item type, through two overloads.
- **Options with fixed values are `Literal` types:** `theme.color`, `log.level`, and `archive.unzip`'s `conflict`. The runtime checks stay.
- **The session interface:** `Session` is a `typing.Protocol`, so pyright checks that the engine's session and `FakeSession` both match it.
- **Source declarations:** `Source` attributes are typed `ClassVar`s, so `needs = "sessionid"` (a string, not a tuple) is a type error.
- **JSON values** (JSON `Selection.first` / `all`, `session.fetch_json`, handler data) are `Any`. JSON is untyped by nature. The runtime `type=` checks with TypedDict support are deferred.

## Tests

- `make typecheck` passes with zero errors.
- `tests/typing/usage.py` is type-checked, never run. It uses `assert_type` for `worker.map`, `prompt.choice`, `kv` and `selector`. The lines that must fail carry `# pyright: ignore[<rule>]` with `reportUnnecessaryTypeIgnoreComment` enabled. Pyright reads `# type: ignore[...]` as a blanket ignore whatever the brackets say, so only the pyright form pins the expected error, so a change that makes one of them type-check by accident fails the build. Those lines are a misspelt `theme.color`, a `Source` with `domains` (and, from step 3, `needs`) as a string, and `worker.map(len, [1, 2])`. Each step adds the lines for the modules it builds: `kv` and `selector` in step 2, `Session` in step 3.
- A test checks that `shared/py.typed` is in the built wheel.
