# flnh

Make folders and files vanish in plain sight across Windows, macOS, and Linux, without encryption.

## Install

`flnh` needs [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer.

```sh
git clone https://github.com/feiluvnana/flntools
cd flntools
uv tool install -e ./flnh
```

## Commands

Only two commands exist: `flnh hide` and `flnh show`.

```sh
flnh hide ./photos             # instant vanish (default)
flnh hide ./photos --lock      # vanish + ACL/chmod lock
flnh hide E:\photos --volume   # hide behind partition boundary (drive used space drops)
flnh show ./photos             # auto-detect and restore
flnh show                      # pick from tracked hidden items
flnh show --list               # list tracked hidden items
```

## Development

Run commands inside `flnh/`:

```sh
make setup
make check
```
