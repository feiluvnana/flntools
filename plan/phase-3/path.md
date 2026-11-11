# path

Phase 3. Depends on nothing. Stdlib only.

[`shared/_path.py`](../../shared/_path.py). A few functions that `pathlib` lacks. Every function takes a `str` or `Path` and returns a `Path`. There is no `Path` subclass.

```python
path.safename("Summer: 2026 / day 1?")        # "Summer_2026_day_1"
path.safename("...")                          # "untitled"
path.mkdir(".flnd/downloads/ins")             # creates parents, returns the Path
path.unique("out/clip.mp4")                   # out/clip.mp4, or out/clip (1).mp4 if that exists
path.expand("~/Downloads/$USER/cookies.txt")  # ~ and environment variables, then resolve()

with path.atomic("out/clip.mp4") as f:        # binary by default; mode="w" for text
    f.write(chunk)
```

## Functions

| Function | Behavior |
| --- | --- |
| `safename(text, *, fallback="untitled", maxlen=200)` | Replaces `\ / * ? : " < > \|` and control characters with `_`. Collapses runs of whitespace and `_` into one `_`, and strips `._ ` from both ends. Cuts the result to `maxlen` bytes of UTF-8 without splitting a character. Returns `fallback` when nothing is left. Names that Windows reserves (`CON`, `NUL`, `COM1`, …) get a trailing `_`. |
| `mkdir(p)` | `mkdir(parents=True, exist_ok=True)`, then returns the path. |
| `unique(p)` | Returns `p` when it does not exist. Otherwise it tries `name (1).ext`, `name (2).ext`, and so on. |
| `expand(p)` | Applies `expanduser`, then `expandvars`, then `resolve()`. |
| `atomic(p, mode="wb", encoding=None)` | A context manager that writes to `p.part` next to the target. A normal exit renames the file into place with `replace`. An exception deletes the `.part` file and re-raises. Parent directories are created. |

`safename` replaces `flnd.utils.sanitize_name`. Its output is the same for every input today's tests use. Only the `maxlen` and reserved-name rules are new.

## Tests

- `safename`:
  - every replaced character;
  - collapsing runs;
  - the fallback;
  - the length cut on a multi-byte character;
  - reserved Windows names;
  - the same results as the existing `sanitize_name` tests.
- `unique` with zero, one, and two clashes.
- `expand` with `~` and an environment variable.
- `atomic`:
  - a normal exit leaves only the target file;
  - an exception leaves neither file;
  - an existing target is replaced only on success.
