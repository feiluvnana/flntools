# setup

Phase 6, step 2. `make setup` is slow, fails when run twice, and the Makefile only half works on Windows. This step makes setup one quick, repeatable `uv sync`, moves every task into one stdlib Python script, and keeps the Makefile as a thin shortcut that runs the same way under `sh` and `cmd.exe`.

## What is slow, measured

Measured 2026-11-12 on macOS arm64 with uv 0.12.1:

| Step | Cost |
| --- | --- |
| `uv pip install -e ".[…]" --group dev`, empty uv cache | 37 s, 45 packages, about 341 MB of cache |
| the same, warm cache | 0.3 s |
| `playwright install chromium` | **about 305 MB downloaded** (Chrome for Testing 200 MB + Chrome Headless Shell 105 MB), 580 MB on disk |
| first `make type` without `node` on `PATH` | pyright downloads a Node.js through `nodeenv` |

- **The browser download is the slow part, not Python.** flnd never uses the headless shell: it launches real Chrome first, and otherwise Playwright's `chromium` in new-headless mode (`shared/_browser.py`, `LAUNCH_CHANNELS`). README and `flnd install-browser` say "about 150 MB", which is wrong.
- **`playwright` is unpinned**, so every new Playwright release downloads a new browser build. The cache here already holds two, about 1.1 GB.
- **A second `make setup` fails.** `uv venv .venv` exits with "A virtual environment already exists". With `--clear` it would rebuild the whole venv every time instead.
- **There is no lockfile.** Every setup resolves the versions again, so two machines can get different ones (`playwright` 1.65.0 here, 1.65.1 in a fresh venv).
- Listing a package both in `dependencies` and in an extra costs nothing measurable. It is still noise, so it goes ([below](#pyproject)).

## Design

1. **`uv sync` with a committed `uv.lock`.** It creates `.venv` when missing, installs only what changed (well under a second when nothing did), and every machine gets the same versions, including Playwright's browser build.
2. **`setup` downloads no browser.** flnd drives the installed Google Chrome, and only when Chrome is missing does `browser.connect` offer to download Chromium, once, when it's first needed ([cli › Chrome only](cli.md#chrome-only)). The 305 MB download leaves setup entirely, and the unused headless shell is never downloaded.
3. **pyright brings its own Node.js** through the `pyright[nodejs]` extra (the `nodejs-wheel-binaries` wheel), so `type` downloads nothing on first use and works on Windows.
4. **PyInstaller is only for `dist`.** It moves from the `bundler` extra into a `dist` dependency group, which `setup` doesn't install; the `dist` task syncs it itself.
5. **Every task is in `scripts/dev.py`**, stdlib only, the same on macOS, Linux and Windows. It needs nothing but uv:

```
uv run --no-project scripts/dev.py setup        # or: make setup
uv run --no-project scripts/dev.py check
uv run --no-project scripts/dev.py flnd download ins:example_user
```

## `scripts/dev.py`

About 70 lines of code. Each task is a function, and `main` dispatches `sys.argv[1]`. Every command is a list of arguments run with `subprocess.run(…, check=True)`, never a shell string, so quoting can't differ between shells.

| Task | Runs |
| --- | --- |
| `setup` | `uv sync --python $PYTHON` (default 3.14); no browser download |
| `test` | `uv run python -m unittest discover -s tests` |
| `lint` | `uv run python -m compileall -q flnd shared` |
| `type` | `uv run pyright` |
| `check` | `lint`, `type`, then `test`, stopping at the first failure |
| `flnd …` | `uv run flnd …`, passing the rest of the arguments through |
| `dist` | `uv sync --group dist`, then `bundler.build(…)`, then writes `dist/flnd/licenses/ffmpeg-source.txt` |

- **`ffmpeg-source.txt`** is written by a function, `ffmpeg_source() -> str`, with `Path.write_text(…, encoding="utf-8")`. That replaces the `FFMPEG_SOURCE` snippet that make had to join into one line and both shells had to leave alone. `cmd.exe`'s `>` would have written it in the console's code page.
- **`PYTHON`** comes from the environment or `--python 3.11`, so the release check is `uv run --no-project scripts/dev.py check --python 3.11`.
- A failing command exits with its own exit code, so `make` and CI see the failure.

## Makefile

For anyone who has GNU make. It holds no logic and no shell syntax, so it runs the same under `sh`, Git Bash and `cmd.exe`:

```makefile
TASKS := setup test lint type check flnd dist
.PHONY: $(TASKS)

$(TASKS):
	uv run --no-project scripts/dev.py $@ $(ARGS)
```

Windows has no make by default. README says to run `uv run --no-project scripts/dev.py <task>` there, or to install make with `winget install ezwinports.make`. The `ifeq ($(OS),Windows_NT)` block and the quoted `.venv/Scripts/*.exe` paths go.

## pyproject

```toml
dependencies = ["httpx>=0.27.0", "playwright==1.65.*", "parsel>=1.9.0", "python-jsonpath", "cryptography>=42",
                "telethon>=1.36", "qrcode>=7.4", "pillow>=11.3", "pi-heif>=1.0", "imageio-ffmpeg>=0.6"]

[project.optional-dependencies]                    # for shared alone, without flnd
archive = ["pyzipper", "py7zr"]                     # flnd no longer needs it
selector = ["parsel>=1.9.0", "python-jsonpath"]
browser = ["playwright==1.65.*", "cryptography>=42"]
media = ["pillow>=11.3", "pi-heif>=1.0", "imageio-ffmpeg>=0.6"]
classify = ["onnxruntime>=1.20", "numpy>=2"]         # media.classify; flnd --classify

[dependency-groups]
dev = ["pyright[nodejs]==1.1.414", "typing_extensions>=4.12"]
dist = ["pyinstaller>=6"]

[tool.uv]
default-groups = ["dev"]
```

- **`pyzipper` and `py7zr` leave flnd's dependencies:** `--unpack` and `--zip` were flnd's only use of `archive`, which stays in `shared` with its extra ([api](api.md#archive-432-lines)).
- **`setup` installs the `classify` and `archive` extras too**, for their tests: `uv sync --extra classify --extra archive`.
- **`playwright` is pinned to a minor version**, so a new release doesn't silently download a new browser. Raising it is a deliberate change to `uv.lock`.
- **The other extras** stay so `shared` can be installed without flnd; flnd's `dependencies` already hold what they'd add.

## Other changes

- **`flnd install-browser` goes** ([cli](cli.md#chrome-only)). README says flnd needs Google Chrome, and that it offers a Chromium download (about 200 MB, not the 150 MB it says today) when Chrome is missing.
- **README › Setup:** `uv sync`, then `uv run flnd …`, or `make setup`. The manual `uv venv` / `source .venv/bin/activate` / `uv pip install -e .` steps go, and so does the Windows activation line.
- **plan/README.md › Makefile** shows the Makefile and the task table above instead of the old copy.

## Tests

- `tests/test_packaging.py` tests `scripts/dev.ffmpeg_source()` instead of parsing `export FFMPEG_SOURCE =` out of the Makefile, and checks that the `dist` task writes `licenses/ffmpeg-source.txt`.
- A test runs `scripts/dev.py` with an unknown task and checks that it lists the tasks and exits 2.
- A test checks that every Makefile target is a task in `scripts/dev.py`, and that the Makefile has no `ifeq`, quotes or `.venv` paths.
- `make setup` run twice in a row succeeds, and the second run takes under 5 s (checked by hand on macOS and Windows).
