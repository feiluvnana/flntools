# fixes

Phase 4, step 1. Bugs that the independent review of Phases 1–3 reproduced, in the modules that stay as they are. Each fix comes with a test that fails before it.

Bugs found in modules that Phase 4 reshapes are not fixed here. They are written into the new spec as requirements, so the rewrite can't bring them back:
- worker teardown, Ctrl-C and retries: [worker](worker.md);
- tar symlink chains: [archive](archive.md);
- Playwright responses: [selector](selector.md);
- store batches and the key file: [kv](kv.md) and [engine › secrets](engine.md#secrets).

This step also sets up type checking (see [typing](typing.md)), so every later step is checked.

## cli

**A stdin option never reads stdin when the command has any `bool` flag.** Reproduced.
- **Cause:** `_cli.py` counts `False` as "given", because it is not `None` or `[]`.
- **Fix:** a parameter counts as given only when its parsed value differs from its own default.

## log

**The live area can be taller than the terminal.**
- **What goes wrong:** rows above the top of the screen can't be reached by cursor-up, so every redraw leaves copies in the scrollback. flnd hits this with four sources, each drawing a pool bar, a spinner and several file bars.
- **Fix:** `Screen.height` is added. The live area draws at most `height - 1` rows, keeps the oldest, and folds the rest into one `… N more` row. On a pipe nothing changes.

## fmt

**`fmt.date(True)` raises `AttributeError`.** A `bool` is an `int`, but it is not a timestamp: it now raises `TypeError`.

## flnd

**Cookie files are readable by other users.** `flnd/cookies.py` writes with `write_text`, which gives mode 0644 and is not atomic. It now writes to a temp file opened with mode 0600, then renames it into place. In step 3, flnd switches to `cookies.to_file`, which does the same.

The two other flnd findings (stacked retries, and dropped downloads that aren't logged) are fixed in the [engine](engine.md#downloads), which replaces the code they were in.

## Tests

- `test_stdin_option_with_bool_flag`
- `test_live_area_capped_to_height`
- `test_date_rejects_bool`
- `test_cookie_file_mode_0600`
