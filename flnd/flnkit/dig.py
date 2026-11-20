"""Walk parsed JSON by a path: `dig(data, path)`.

SPEC.md#writing-a-plugin explains it.
"""

from __future__ import annotations

import functools
import re
from typing import Any, NamedTuple

__all__ = ["dig"]


class _Search(NamedTuple):
    """One `..[field=value]` step: every object whose `field` equals `value`."""

    field: str
    value: str


ALL = None

STEP = re.compile(
    r"""
      \.\.\[(?P<field>[^\]=]+)=(?P<value>[^\]]*)\]     # ..[field=value]
    | \[(?P<index>-?\d*)\]                              # [] or [3]
    | (?:^|\.)(?P<key>[^.\[\]|]+)                       # key, or .key after a step
""",
    re.VERBOSE,
)


def dig(data: Any, path: str) -> Any:
    """Walk parsed JSON by a path, and return what it reaches.

    A path is steps joined by dots: `key`, `[0]` for an index, `[]` for every item, and
    `..[field=value]` for every object, at any depth, with that field. Several paths joined
    by `||` are tried in order, and the first one that finds something wins.

    Args:
        data: the parsed JSON: dicts, lists and plain values.
        path: the path to walk; see above.

    Returns:
        What the path reaches: a value, or a list when it spreads over items. None when a
        plain path finds nothing; a spreading path finds nothing as `[]`.

    Raises:
        ValueError: the path has a step that cannot be read.

    Example:
        >>> data = {"items": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}
        >>> dig(data, "items[0].name")
        'a'
        >>> dig(data, "items[].id")
        [1, 2]
        >>> dig(data, "missing || items[1].id")
        2
    """
    found = None
    for steps in _compile(path):
        found = _walk(data, steps)
        if found is None and any(_spreads(step) for step in steps):
            found = []
        if found not in (None, "", [], {}):
            return found
    return found


@functools.lru_cache(maxsize=512)
def _compile(path: str) -> tuple[tuple[Any, ...], ...]:
    """Split a path into its `||` alternatives, each a tuple of steps."""
    alternatives = []
    for alternative in path.split("||"):
        text, steps, at = alternative.strip(), [], 0
        while at < len(text):
            step = STEP.match(text, at)
            if step is None or step.end() == at:
                raise ValueError(
                    f"dig: can't read {text[at:]!r} in {path!r}: use a.b, a[0], "
                    "a[], ..[field=value] or a || b"
                )
            if step["field"] is not None:
                steps.append(_Search(step["field"], step["value"]))
            elif step["index"] is not None:
                steps.append(int(step["index"]) if step["index"] else ALL)
            else:
                steps.append(step["key"])
            at = step.end()
        alternatives.append(tuple(steps))
    return tuple(alternatives)


def _spreads(step: Any) -> bool:
    return step is ALL or isinstance(step, _Search)


def _walk(data: Any, steps: tuple[Any, ...]) -> Any:
    """Follow `steps` from `data`, spreading over items at each `[]` or search step."""
    for position, step in enumerate(steps):
        if not _spreads(step):
            data = _step(data, step)
            if data is None:
                return None
            continue
        items = data if step is ALL and isinstance(data, list) else []
        if isinstance(step, _Search):
            items = list(_search(data, step))
        rest = steps[position + 1 :]
        found: list[Any] = []
        if any(map(_spreads, rest)):
            for item in items:
                value = _walk(item, rest)
                if value:
                    found.extend(value)
            return found
        if len(rest) == 1 and isinstance(rest[0], str):
            key = rest[0]
            return [
                value
                for item in items
                if isinstance(item, dict) and (value := item.get(key)) is not None
            ]
        for item in items:
            for later in rest:
                if isinstance(item, dict) and isinstance(later, str):
                    item = item.get(later)
                else:
                    item = _step(item, later)
                if item is None:
                    break
            else:
                if isinstance(item, list) and not rest:
                    found.extend(item)
                else:
                    found.append(item)
        return found
    return data


def _step(data: Any, step: str | int) -> Any:
    if isinstance(step, int):
        if isinstance(data, list) and -len(data) <= step < len(data):
            return data[step]
        return None
    return data.get(step) if isinstance(data, dict) else None


def _search(data: Any, step: _Search) -> Any:
    """Yield each object under `data`, at any depth, whose field equals the step's value."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get(step.field) == step.value:
                yield node
            stack.extend(reversed(node.values()))
        elif isinstance(node, list):
            stack.extend(reversed(node))
