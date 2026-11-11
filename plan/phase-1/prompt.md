# prompt

Phase 1. Depends on [theme](theme.md) and [style](style.md). It shares the live area with [log](log.md).

[`shared/_prompt.py`](../../shared/_prompt.py). Questions in the terminal. `_format_prompt` draws each frame from `glyphs` and `theme.templates.prompt`. Each function returns the answer.

```python
prompt.input("Name") -> str
prompt.input("Name", default="ada") -> str
prompt.input("Name", placeholder="ada") -> str
prompt.secret("Token") -> str
prompt.secret("Token", mask="*") -> str
prompt.confirm("Deploy?") -> bool
prompt.confirm("Overwrite?", default=False, yes="Overwrite", no="Keep") -> bool

prompt.choice("Source", ["tik", "fbk", "ins", "twt"]) -> str
prompt.choice("Source", {"TikTok": "tik", "Instagram": "ins"}) -> str
prompt.choices("Sources", ["tik", "fbk", "ins", "twt"]) -> list[str]
prompt.choices("Sources", ["tik", "fbk", "ins", "twt"], min=1, max=3) -> list[str]
prompt.choice("Source", ["tik", "fbk", "ins", "twt"], filter=True) -> str
```

| Argument | Where | Meaning |
| --- | --- | --- |
| `message` | all | the question; required |
| `default` | all | the starting value, and the answer inside `prompt.defaults()` |
| `placeholder` | `input` | shown while the buffer is empty, never returned |
| `mask` | `secret` | one character; default `•` |
| `yes`, `no` | `confirm` | the two labels; default `"Yes"` and `"No"` |
| `options` | `choice`, `choices` | a sequence, or a mapping of label to value |
| `min`, `max` | `choices` | how many must be checked; default `0` and no maximum |
| `filter` | `choice`, `choices` | `True` hides rows that do not start with the typed text; default `False` |
| `keys` | all | `True` draws the key guide under the rows; default `False` |
| `validate` | all | `(value) -> str \| None`; a string is shown as an error and the question stays open |
| `theme` | all | theme for this question |

Option rules:

- A sequence shows each item and returns it. A mapping shows the key and returns the value.
- `default` for `choice` is a value, not a label. `default` for `choices` is a sequence of values.
- An unknown default, or an empty `options`, raises `prompt.PromptError` before anything is drawn.
- `min` below 0, or `max` below `min`, raises `ValueError`.
- `secret` shows `mask` once per character and returns the real text.

Prompts need an interactive screen:

- `with prompt.defaults():` answers each question with its `default` and reads no keys. A question with no `default` inside that block raises `prompt.PromptError`.
- When `theme.screen.interactive` is `False`, a question returns its `default`, or raises `prompt.PromptError` if it has none.
- Ctrl-C raises `KeyboardInterrupt`.

```python
with prompt.defaults():
    assert prompt.confirm("Deploy?", default=True) is True
```

## Frames

```python
prompt.input("Name", placeholder="ada")
```

```text
Name
› ada                                  ada is muted until the user types
```

The user types `sam` and presses enter. It returns `"sam"`, and the frame becomes:

```text
✓ Name: sam
```

`default="ada"` shows `ada` in the normal color, as the real buffer. `secret` shows `› •••` and ends as `✓ Token`, with no value on the line.

```python
prompt.confirm("Deploy?")
```

`default` is `False`, so the cursor starts on No. `y` moves to Yes and accepts, and `n` accepts No.

```text
Deploy?
  ○ Yes
› ● No
```

Enter returns `False`, and the frame becomes `✓ Deploy? No`.

```python
prompt.choice("Source", ["tik", "fbk", "ins", "twt"], keys=True)
```

```text
Source
› ● tik
  ○ fbk
  ○ ins
  ○ twt
up/k up  down/j down  enter accept
```

Down, down, enter returns `"ins"`, and the frame becomes `✓ Source: ins`. The key guide disappears along with the frame.

```python
prompt.choices("Sources", ["tik", "fbk", "ins", "twt"])
```

Space, down, down, space, enter returns `["tik", "ins"]`:

```text
Sources
  ✔ tik
  · fbk
› ✔ ins
  · twt
```

Then the frame becomes `✓ Sources: tik, ins`.

With `filter=True`, typing `in` leaves only `› ● ins`. A filter that matches nothing keeps the question open and shows the error `no match`.

```python
def nonempty(value: str) -> str | None:
    return None if value.strip() else "Enter a name."

prompt.input("Name", validate=nonempty)
```

Pressing enter with nothing typed shows:

```text
Name
›
✗ Enter a name.
```

## Keymap

```python
prompt.keymap.up = ("up", "k")
prompt.keymap.down = ("down", "j")
prompt.keymap.left = ("left", "h")
prompt.keymap.right = ("right", "l")
prompt.keymap.accept = ("enter",)
prompt.keymap.toggle = ("space",)
prompt.keymap.backspace = ("backspace",)
prompt.keymap.yes = ("y",)
prompt.keymap.no = ("n",)
prompt.keymap.interrupt = ("ctrl+c",)
```

Assigning a tuple replaces that action's keys, so `prompt.keymap.up = ("up",)` frees `k`. An unknown action name raises `AttributeError`.

`keys=True` lists only the actions that question uses, in this order: `up`, `down`, `yes`, `no`, `toggle`, `accept`. An action with an empty tuple is skipped. The keys are read from `prompt.keymap` when the frame is drawn.

| Question | Actions listed |
| --- | --- |
| `input`, `secret` | `accept` |
| `confirm` | `up`, `down`, `yes`, `no`, `accept` |
| `choice` | `up`, `down`, `accept` |
| `choices` | `up`, `down`, `toggle`, `accept` |

## Tests

- every frame drawn above, and every key in the keymap;
- `filter`, including no match;
- `validate`, `min` and `max`, and `defaults()`;
- a non-interactive screen;
- a prompt asked while a bar is open.
