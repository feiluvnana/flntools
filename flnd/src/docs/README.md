# flnd's documentation

flnd downloads photos, videos and files from the sites you choose, and skips what it already
has. It is a Python package with a command line on top. These pages describe flnd as the code
is now. A part that isn't built yet is named as such, never described as working.

## Where to start

- **Using flnd from the command line:** [cli.md](cli.md), then your site's page below.
- **Using flnd from a Python script:** [library.md](library.md).
- **Adding a site:** [writing-a-plugin.md](writing-a-plugin.md), then the page of a built-in
  plugin that works like yours.
- **Changing flnd itself:** [architecture.md](architecture.md), then the module docstrings of the
  part you change. [AGENTS.md](../../AGENTS.md) has the rules every change follows.

## Guides

| page | what it covers |
| --- | --- |
| [architecture.md](architecture.md) | the layers and parts, one download end to end, threads |
| [cli.md](cli.md) | every command and flag, its default, examples and the exit codes |
| [cookbook.md](cookbook.md) | practical CLI and Python recipes: filters, `--again`, repair, auth |
| [library.md](library.md) | the calls a script can make, and what they return |
| [writing-a-plugin.md](writing-a-plugin.md) | writing, testing and installing a plugin on API 2 |
| [errors.md](errors.md) | each error class, what raises it, and what to do about it |
| [storage.md](storage.md) | the home folder: its files, the database tables and their fields |
| [extension.md](extension.md) | the Chrome extension: its parts, what it reads, install, relay |
| [performance.md](performance.md) | the measured numbers, `make bench`, how a speed-up is shown |
| [glossary.md](glossary.md) | the words these pages use, such as target, plugin, route and post |

## Sites

| code | site | page |
| --- | --- | --- |
| `fbk` | Facebook | [plugins/fbk.md](plugins/fbk.md) |
| `ins` | Instagram | [plugins/ins.md](plugins/ins.md) |
| `loi` | LOIBUS | [plugins/loi.md](plugins/loi.md) |
| `mht` | mimihentai | [plugins/mht.md](plugins/mht.md) |
| `tik` | TikTok | [plugins/tik.md](plugins/tik.md) |
| `tlg` | Telegram | [plugins/tlg.md](plugins/tlg.md) |
| `twt` | Twitter/X | [plugins/twt.md](plugins/twt.md) |
| `web` | any other link | [plugins/web.md](plugins/web.md) |

## Keeping the docs true

`make check` runs `tests/test_docs.py`, which checks these pages against the code:

- every `python` block on these pages runs, in a scratch folder;
- every `python` block and every `flnd` command line in the README runs or parses, with the
  CLI's own parser;
- every relative link resolves, and every `#fragment` names a heading in its page;
- this index links every page in `src/docs/`, and the glossary defines the core terms;
- [cli.md](cli.md) names every command and flag the CLI parses;
- every built-in plugin has its page in `plugins/`, and no page is left for a plugin that is gone;
- every module docstring names its `src/docs` page, every public name has a summary line, and
  the `>>>` examples in the code run.

A change of behaviour updates its page here in the same commit.
