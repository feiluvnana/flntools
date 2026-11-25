# flnh

Make folders and files vanish in plain sight across Windows, macOS, and Linux.

## Commands

Only two commands exist: `flnh hide` and `flnh show`.

```sh
flnh hide ./photos                     # level 1 (flag): instant vanish
flnh hide ./photos --level lock        # level 2 (lock): vanish + ACL/chmod lock
flnh hide ./photos --level lock --pass # level 2 + password check
flnh hide ./photos --level vault       # level 3 (vault): encrypted vanished blob
flnh hide E:\photos --level volume     # level 4 (volume): vault + hidden tail space
flnh show ./photos                     # auto-detect and restore
flnh show                              # pick from tracked hidden items
flnh show --list                       # list tracked hidden items
```

## Development

Run commands inside `flnh/`:

```sh
make setup
make check
```
