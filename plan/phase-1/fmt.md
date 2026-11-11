# fmt

Phase 1. Depends on nothing.

[`shared/_fmt.py`](../../shared/_fmt.py). Pure functions. They read neither `theme` nor `style`. The caller adds color: `style.dim(fmt.bytes(n))`.

```python
fmt.bytes(0)            # "0.0 B"
fmt.bytes(1536)         # "1.5 KB"
fmt.bytes(1_048_576)    # "1.0 MB"

fmt.duration(0.25)      # "250ms"
fmt.duration(12.4)      # "12.4s"
fmt.duration(90)        # "1m 30s"
fmt.duration(3661)      # "1h 1m"

fmt.date(date(2026, 11, 11))                  # "2026-11-11"
fmt.date(datetime(2026, 11, 11, 12, 8))       # "2026-11-11 12:08"
fmt.date(when, "%Y-%m-%d")                    # strftime pattern
```

`fmt.bytes(n)` takes an int and walks B, KB, MB, GB, TB by 1024, always with one decimal.

`fmt.duration(seconds)` takes a float:

- under one second, whole milliseconds (`250ms`);
- under one minute, seconds with one decimal (`12.4s`);
- otherwise, the two largest non-zero whole units, largest first (`1h 1m`, `1m 30s`).

`fmt.date(value, pattern=None)` takes a `date`, a `datetime`, or a Unix timestamp. With no pattern, a `date` prints `YYYY-MM-DD`, and a `datetime` or timestamp prints `YYYY-MM-DD HH:MM`. `pattern` is a `strftime` pattern.

## Tests

- each unit boundary, each duration range, and each `date` input type.
