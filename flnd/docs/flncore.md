# flncore

The shared kit flnd and other programs import: log, prompts, the CLI frame, clock, HTTP,
browser, files, and errors. Import lazily from the top:

```python
from flncore import App, log, ask, confirm, choose, Run, Error
```

## cli

`App` turns annotated functions into commands (cyclopts underneath), with Charm-style help,
error hints, JSON error lines, and exit codes.

```python
from flncore import App
from flncore.log import log

app = App("example", version="1.0.0", help="a demo program")

@app.command
def hello(name: str = "world") -> int:
  """Say hello."""
  log.info("hello {name}", name=name)
  return 0

app.run(["hello"])
```

## log

One shared `log` writes to standard error: lines (`log.info`, `log.warn`, …) and live items
(`with log.item(key, label, total=…) as item:`). See the PLAN step 4 design; behaviour is
covered by `tests/test_log.py`.

## prompt

`ask`, `confirm`, and `choose` use questionary with the Charm palette. Off a terminal they
return each question's default, or raise `UsageError` when there is none.