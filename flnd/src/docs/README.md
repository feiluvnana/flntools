# flnd's documentation

flnd downloads photos, videos and files from the sites you follow, and keeps them up to date. It
is a Python package with a command line on top. These pages describe flnd as the code on this
branch is; a part that isn't built yet is named as such, never described as working.

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
| [architecture.md](architecture.md) | the parts of flnd, one download traced from the CLI to the disk, and the threads |
| [cli.md](cli.md) | every command, flag, default and exit code |
| [library.md](library.md) | the calls a script can make, their options and what they return |
| [writing-a-plugin.md](writing-a-plugin.md) | a tutorial that builds and tests a small plugin |
| [errors.md](errors.md) | each error, what causes it and what to do |
| [storage.md](storage.md) | the home folder: every file, store, table and field |
| [extension.md](extension.md) | the Chrome extension: what it may read, its host, the relay, its messages and ops, the popup, install |
| [performance.md](performance.md) | the measured numbers, `make bench`, and how a speed-up is proven |
| [glossary.md](glossary.md) | the words these pages use: target, plugin, route, category, post |

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

`tests/test_docs.py` runs in `make check`. It runs every `python` block on these pages, checks
that every link resolves, that [cli.md](cli.md) names every command and flag, and that every
built-in plugin has its page. It also checks the docstrings of the code against the rules in
AGENTS.md: any gap fails, with a line naming it and the rule it breaks.

A change of behaviour updates its page here in the same commit.
