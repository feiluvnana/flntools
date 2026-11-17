# flnd's documentation

flnd downloads photos, videos and files from the sites you follow, and keeps them up to date. It
is a Python package with a command line on top. These pages describe flnd as the code on this
branch is; a part that isn't built yet is named as such, never described as working.

## Where to start

- **Using flnd from the command line:** [cli.md](cli.md), then your site's page below.
- **Using flnd from a Python script:** [library.md](library.md).
- **Adding a site:** [writing-a-source.md](writing-a-source.md), then the page of a built-in
  site that works like yours.
- **Changing flnd itself:** [architecture.md](architecture.md), then the module docstrings of the
  part you change. [AGENTS.md](../AGENTS.md) has the rules every change follows.

## Guides

| page | what it covers |
| --- | --- |
| [architecture.md](architecture.md) | the parts of flnd, one download traced from the CLI to the disk, and the threads |
| [cli.md](cli.md) | every command, flag, default and exit code |
| [library.md](library.md) | the calls a script can make, their options and what they return |
| [writing-a-source.md](writing-a-source.md) | a tutorial that builds and tests a small source |
| [errors.md](errors.md) | each error, what causes it and what to do |
| [storage.md](storage.md) | the home folder: every file, store, table and field |
| [performance.md](performance.md) | the measured numbers, `make bench`, and how a speed-up is proven |
| [glossary.md](glossary.md) | the words these pages use: target, source, route, category, post, … |

## Sites

| code | site | page |
| --- | --- | --- |
| `fbk` | Facebook | [sources/fbk.md](sources/fbk.md) |
| `ins` | Instagram | [sources/ins.md](sources/ins.md) |
| `loi` | LOIBUS | [sources/loi.md](sources/loi.md) |
| `mht` | mimihentai | [sources/mht.md](sources/mht.md) |
| `tik` | TikTok | [sources/tik.md](sources/tik.md) |
| `tlg` | Telegram | [sources/tlg.md](sources/tlg.md) |
| `twt` | Twitter/X | [sources/twt.md](sources/twt.md) |

## Keeping the docs true

`tests/test_docs.py` runs in `make check`. It runs every `python` block on these pages, checks
that every link resolves, that [cli.md](cli.md) names every command and flag, and that every
source has its page. It also checks the docstrings of the code against the rules in AGENTS.md:
any gap fails, with a line naming it and the rule it breaks.

A change of behaviour updates its page here in the same commit.
