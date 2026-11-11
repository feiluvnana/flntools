# packaging

Phase 4, step 7, the last. flnd can be built into a folder that runs on a machine without Python set up, with one command:

```sh
make dist            # → dist/flnd/flnd (flnd.exe on Windows), for the OS you're on
```

```makefile
dist:
	.venv/bin/python -c "from shared import bundler; bundler.build('flnd/__main__.py', name='flnd')"
```

With `uv` installed, `uv tool install --editable .` remains the simplest install. Cython is not used: flnd waits on the network and browsers, not on Python code, and Cython 3 changes how annotations behave, which `cli` depends on.

## bundler

`shared/_bundler.py`, a thin wrapper over PyInstaller, needs the `bundler` extra (`pyinstaller`).

```python
bundler.build("flnd/__main__.py", name="flnd")                       # dist/flnd/
bundler.build("tool.py", name="tool", hidden=("mypkg.plugins",), data=("assets",))
```

`build(entry, *, name, hidden=(), data=(), out="dist") -> Path`
- builds a **one-folder** bundle, which starts at once. A one-file build would unpack about 100 MB to a temp folder on every run.
- **adds the hidden imports itself:** every module `shared` loads lazily, and the packages those modules need (`playwright`, `cryptography`, `pyzipper`, `py7zr`, `parsel`, `telethon` when installed). PyInstaller can't see lazy imports, and a hand-kept list would drift. `hidden=` adds a script's own lazy imports, such as flnd's sources.
- `data=` copies folders into the bundle.
- runs PyInstaller in-process with a generated command line, so there is no `.spec` file to maintain.
- returns the folder it built. A PyInstaller failure raises `RuntimeError` with PyInstaller's last lines.

That is the whole module.

## Chromium

Chromium (about 150 MB) isn't bundled. `flnd download` never asks, so a source that needs a browser fails with `✗ Chromium is not installed; run flnd install-browser`. `flnd install-browser` runs Playwright's installer into the user cache, with a progress bar. It works the same from source and from the bundle, which has no `python -m playwright`.

## Data

The bundled `flnd` writes `.flnd/` in the working directory, never inside its own folder, so cleanup is the same: `flnd logout --all`, or delete `.flnd/`.

## Tests

- `bundler.build` on a tiny script that does `from shared import kv, worker`. The built program runs, so the lazy imports were found. The test is skipped when PyInstaller isn't installed.
- The generated PyInstaller command line includes `shared`'s lazy modules and the `hidden=` extras.
- `flnd install-browser` with Playwright's installer mocked.
