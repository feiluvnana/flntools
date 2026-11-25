# Agent guidelines (flnh)

Shared repository rules (commits, privacy, ruff, line length <= 100, models) live in
`../AGENTS.md`. Run all `flnh` commands inside `flnh/`.

## Checks
- `make setup` syncs `.venv` from `uv.lock`.
- `make check` runs `ruff check src tests` and `python -m unittest discover -s tests`.
- `make lint` runs ruff alone.

## Layout
- `src/__init__.py`: `__version__` only (no public Python library API).
- `src/cli.py`: `hide` and `show` commands via `flnkit.ui`.
- `src/errors.py`: `flnkit.errors` re-exports + `flnh` subclasses.
- `src/state.py`: local tracking DB via `flnkit.db`.
- `src/vault.py`: Level 3 streaming AES-256-GCM + scrypt vault.
- `src/os_hide/`: Level 1 (`flag`) and Level 2 (`lock`) per OS (`win32`, `darwin`, `linux`).
- `src/volume/`: Level 4 (`volume`) MBR/GPT table, `Disk` / `FileDisk`, OS backends.

## Safety & privacy
- Never execute shell strings (`shell=True` is forbidden); pass argument lists only.
- Never log or write passwords or keyfile contents.
- Never delete plaintext before verified write (Levels 3 & 4).
- Never touch OS/boot/swap disks or run destructive disk operations in unit tests.
