# flntools

A monorepo; each project lives in its own folder.

- `flnd/`: a downloader that saves whole accounts from the web. See [flnd/README.md](flnd/README.md).

Each project folder is its own uv project: run `make` inside it.

To run flnd from anywhere: `uv tool install -e ./flnd` (from the repository root).
