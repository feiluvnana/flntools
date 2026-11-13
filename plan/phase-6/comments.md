# comments

**Replaced on 2026-11-13** by Phase 7's [readable](../phase-7/readable.md#the-writing-rules), by the user's decision: its verbose rules (complete `Args:` lists, a comment at every caught exception, heading comments, the complete-`Args:` lint) no longer hold. It stays as history.

Phase 6, step 9, and a rule for every step before it. The user asked on 2026-11-12 for much more extensive comments across the whole project. The code has grown faster than its explanations, so a reader has to work rules out from the code itself.

## Where it stands

Measured 2026-11-12 over `flnd/` and `shared/` (43 files, 8,701 non-blank lines):

| | Count |
| --- | --- |
| public modules, classes, functions and methods | 581 |
| with no docstring | 200 (34%) |
| with a one-line docstring only | 256 (44%) |
| with more than one line | 125 (22%) |
| documented, taking two or more arguments, but naming none of them | 46 |
| private functions over 5 lines with no docstring | 30 |
| `#` comments | 268, about one per 32 lines |

The least explained files: `flnd/dash.py`, `flnd/vault.py`, `flnd/cli.py`, `shared/_crypto.py`, `shared/_selector.py`, `shared/_cli.py` and `shared/_screen.py`, with almost no comments at all. They are the ones a reader is most likely to misread (manifest choice, key handling, flag parsing, terminal escapes).

## The standard

It replaces the "Docstrings" and "Comments" bullets of plan/README.md's [Readable code](../README.md#conventions). The other bullets stay.

### Every module

A module docstring of three parts:
1. what the module is for, in one sentence;
2. how it works: the flow from its entry point to its result, and what it leaves to other modules;
3. a short example of its main use.

For a source, the docstring also gives the site's quirks it works around and its folder layout, as `ins.py` does today. For a `shared` module it also gives the library it wraps and the `.raw` it exposes.

### Every public class and function

```python
def save(self, source, name, *, category, date=None, folder=None, key=None, size=None, headers=None, audio=None) -> bool:
    """Queue one file for download, and return False when it is skipped.

        session.save(url, f"{day}_{post_id}.jpg", category="posts", date=posted)

    Args:
        source: a URL, mirror URLs tried in order, or a `fetch(dest, progress)` function.
        name: the file name; its extension may change to match the content.
        category: one of the source's `categories`; `--from` filters on it.
        date: the post's day, which `--since` and `--until` filter on; None is always kept.
        …

    Returns:
        True when queued; False when filtered out, already saved or queued, past the limit,
        or when `folder` is None.

    Raises:
        ValueError: `category` isn't one the source declares, a mistake in the source.
    """
```

- **One plain sentence** first, saying what it does, as today, then **a short example**.
- **`Args:`** names every argument with what it means, its unit, and what None or the default does. A keyword whose meaning is obvious from its name and type still gets a line: the list is complete, so a reader never wonders whether an argument was forgotten. `self` and `cls` are left out.
- **`Returns:`** when the result isn't obvious from the first sentence.
- **`Raises:`** every exception a caller might catch, and when.
- **Rules a caller must know** come last, as today: what is slow, what can't be undone, which thread it runs on, what it does over the network.
- A **property** gets one sentence and, when it can be None or raise, a second one saying when.
- A **dataclass or NamedTuple** documents each field with a comment above it, as `Source`'s class attributes are today.

`flnd.cli`'s docstrings keep their `Args:` form already, because `shared.cli` reads its help from it.

### Private functions

A private function over about 5 lines gets a docstring of one sentence saying what it does. A longer one, or one with a non-obvious contract, gets its arguments too. A short helper whose name says it all needs none.

### `#` comments

Comments still explain **why**, never restate the next line. They are now expected at each of these:
- **every protocol detail and magic value:** header names, API IDs, status codes, epochs and bit shifts (Instagram's `pk >> 23`), escape sequences (`\x1b[?2026h`), limits and timeouts, with where the value comes from;
- **every branch for a site's quirk**, with what the site does and roughly when it was seen (`# 2026-11: the tagged grid lists only thumbnails for videos`);
- **every thread, lock and event loop:** who else touches the value, and why the lock is needed;
- **every caught exception:** why it is safe to catch here, and why the others aren't caught;
- **every platform branch:** what differs on Windows or macOS, and how it was found;
- **at each step of a function over about 15 lines:** a one-line heading comment where a reader would pause, so a long function reads as a list of steps.

### Tests

Each test file starts with a docstring saying what it covers and what it fakes (the screen, the clock, the network). A test whose name can't say the whole rule gets one sentence on what it pins and why it matters, such as which past bug it guards against.

## How it's done

- **Each Phase 6 step writes its own code to the standard**, so new and changed code is never written twice.
- **Step 9 brings the remaining files up to it**, in the order of the "least explained" list above, then the rest: one commit per module or source, `docs:explain <module>`, never mixed with behaviour changes.
- **Only comments and docstrings change in step 9.** A test checks that the code with comments and docstrings stripped (`ast.unparse` of the parsed module) is the same before and after.
- **Comments don't count against the size limits**, as plan/README.md already says, so this step never competes with the line budget.

## Checks

`scripts/dev.py lint` gains a docstring check (stdlib `ast`, about 30 lines in `scripts/dev.py`), which fails when:
- a module, or a public class, function or method in `flnd/` or `shared/`, has no docstring;
- a public function with two or more arguments besides `self` has a docstring of one line only;
- an `Args:` section leaves out an argument the signature has, or names one it doesn't have;
- a private function over 15 lines has no docstring.

The check starts in step 9, once every file meets it, so earlier steps aren't blocked by files they don't touch. Until then it reports without failing.
