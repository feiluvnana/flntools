# fmt

Phase 4, step 2. `shared/_fmt.py` gains `filename`, which replaces `path.safename`, and `bytes` is renamed `size`.

```python
fmt.size(1536)                                  # "1.5 KB"   (was fmt.bytes)
fmt.duration(90)                                # "1m 30s"
fmt.date(when)                                  # "2026-11-11 12:08"
fmt.filename("Summer: 2026 / day 1?")           # "Summer_2026_day_1"
```

`fmt` turns values into text. `filename` turns text into text that is safe as a file or folder name.

## filename

`filename(text, *, fallback="untitled", maxlen=200) -> str`

- Replaces `\ / * ? : " < > |` and control characters with `_`.
- Collapses runs of whitespace and `_` into one `_`, and strips `._ ` from both ends.
- Cuts the result to `maxlen` bytes of UTF-8 without splitting a character.
- Returns `fallback` when nothing is left.
- Names that Windows reserves (`CON`, `NUL`, `COM1`, …) get a `_` after the part before the first dot, because Windows ignores the extension: `CON` becomes `CON_`, and `nul.txt` becomes `nul_.txt`.

For every input in flnd's existing `sanitize_name` tests, it gives the same output.

## size

`fmt.bytes` becomes `fmt.size`. The old name shadowed the builtin `bytes` inside the module, and "size" says what it formats. Its output is unchanged.

## Tests

- `filename`:
  - every replaced character;
  - collapsing runs;
  - the fallback;
  - the length cut on a multi-byte character;
  - reserved Windows names, with and without an extension;
  - the old `sanitize_name` cases.
- `size` gives the old `bytes` outputs, and `fmt.bytes` no longer exists.
