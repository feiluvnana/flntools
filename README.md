# flntools

A monorepo; each project lives in its own folder.

- `flnd/`: a downloader that saves whole accounts from the web. See [flnd/README.md](flnd/README.md).
- `flnh/`: a CLI tool for making folders and files vanish in plain sight. See [flnh/README.md](flnh/README.md).
- `flnkit/`: shared kit package (`errors`, `files`, `db`, `text`, `clock`, `ui`, `web`).

Each project folder is its own uv project: run `make` inside it.

To run from anywhere (from the repository root):

```sh
uv tool install -e ./flnd
uv tool install -e ./flnh
```
