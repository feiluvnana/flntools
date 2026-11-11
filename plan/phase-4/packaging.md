# packaging

Phase 4, step 7, the last. flnd can be built into a folder that runs on a machine without Python set up, with one command:

```sh
make dist            # → dist/flnd/flnd (flnd.exe on Windows), for the OS you're on
```

```makefile
dist:
	.venv/bin/python -c "from shared import bundler; bundler.build('flnd/__main__.py', name='flnd', data=('licenses',))"
```

The folder holds the `flnd` program, PyInstaller's `_internal/` (Python, the packages, and Playwright's driver with Node.js, about 150 MB of the 210 MB), and `licenses/`.

With `uv` installed, `uv tool install --editable .` remains the simplest install. Cython is not used: flnd waits on the network and browsers, not on Python code, and Cython 3 changes how annotations behave, which `cli` depends on.

## bundler

`shared/_bundler.py`, a thin wrapper over PyInstaller, needs the `bundler` extra (`pyinstaller`). Importing it doesn't load PyInstaller; `build` raises the missing-extra `ImportError` on first use.

```python
bundler.build("flnd/__main__.py", name="flnd")                       # dist/flnd/
bundler.build("tool.py", name="tool", hidden=("mypkg.plugins",), data=("assets",))
```

`build(entry, *, name, hidden=(), data=(), out="dist") -> Path`
- builds a **one-folder** bundle, which starts at once. A one-file build would unpack about 100 MB to a temp folder on every run.
- **adds the hidden imports itself:** every module `shared` loads lazily (read from `shared.LAZY_MODULES`, so it can't drift), and what those modules import inside functions or by name, each when installed: `playwright.sync_api`, `cryptography`, `pyzipper`, `py7zr`, `parsel`, `jmespath`, `jsonpath`, `telethon`, and the stdlib's `compression.zstd` on 3.14. `archive` imports `pyzipper`, `py7zr` and `compression.zstd` by name, which PyInstaller can't see. `hidden=` adds a script's own lazy imports; flnd needs none, since its sources are imported normally.
- **leaves itself out:** `shared/__init__.py`'s `TYPE_CHECKING` block makes `shared._bundler` reachable, and it would bring PyInstaller (and tkinter) into every bundle, so both are excluded.
- `data=` copies folders beside the program, where the program finds them at `Path(sys.executable).parent / name` and a person sees them; a missing folder raises `FileNotFoundError` before building.
- runs PyInstaller in-process with a generated command line, at log level `WARN`, with the `.spec` file and the build cache in a temp folder, so there is no `.spec` file to maintain and no `build/` left behind.
- returns the folder it built. A PyInstaller failure raises `RuntimeError` with PyInstaller's last lines.

That is the whole module.

## Chromium

Chromium (about 150 MB) isn't bundled. `flnd download` never asks, so a source that needs a browser fails with `✗ <target>: Chromium is not installed; run flnd install-browser`: the session replaces `shared.browser`'s message, which names `playwright install chromium`. `flnd install-browser` runs Playwright's installer (`install chromium`) into the user cache, with Playwright's own progress bar. It works the same from source and from the bundle, which has no `python -m playwright`: it runs the Node.js driver Playwright ships, found with `playwright._impl._driver.compute_driver_executable` and `get_driver_env`, as `python -m playwright` does. That is Playwright's private API, so a Playwright upgrade checks it. Like `playwright install`, it also removes browser builds no installed Playwright uses any more.

## Data

The bundled `flnd` writes `.flnd/` in the working directory, never inside its own folder, so cleanup is the same: `flnd logout --all`, or delete `.flnd/`.

## Licences

`licenses/` holds one `<distribution>.txt` per third-party package in the bundle, named by its normalized distribution name (`python-jsonpath.txt`), plus `python.txt` (the interpreter) and `pyinstaller.txt` (its bootloader, GPL with the bootloader exception). Each starts with the name, a `License:` line and the project link, then the package's own licence files from its `.dist-info`. Node.js and Playwright's driver notices ship inside `_internal/playwright/driver/`. Adding a dependency means adding its file; a test lists flnd's installed dependencies and names any without one.

## Tests

- `bundler.build` on a tiny script that does `from shared import archive, kv, worker` and writes and reads a password 7z. The built program runs, so the lazy imports were found; the 7z fails without the `py7zr` hidden import (the `TYPE_CHECKING` block already makes `shared`'s own modules visible). It takes a bundle of Playwright's driver, so it runs only with `FLNTOOLS_BUILD_TESTS=1` and PyInstaller installed.
- The generated PyInstaller command line includes `shared`'s lazy modules and the `hidden=` extras, packages only when installed, and excludes the bundler and PyInstaller.
- With a stand-in PyInstaller: the built folder is returned with `data=` beside the program; a failure raises `RuntimeError` with PyInstaller's last lines; a missing data folder raises before building; no PyInstaller gives the missing-extra `ImportError`.
- `flnd install-browser` with Playwright's installer mocked, and its failures; a missing Chromium names `flnd install-browser`.
- Every package in flnd's installed dependencies has a `licenses/` file, and every file starts with its name and licence.
