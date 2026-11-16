"""A lazy, immutable query over any collection: narrow it with option methods, run it with `get()`.

    rows = Query(lambda: files)
    rows.filter(kind="image").filter(lambda f: f.size > 1e6).sort("size", desc=True).limit(5).get()
    rows.first, rows.count

A source may also push the work down (`store` turns it into SQL): give `Query` a `Source`, whose
`run(plan)` gets the filters, sort and limit at once and returns the rows.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Generic, Protocol, TypeVar

__all__ = ["Plan", "Query", "Source"]

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
Predicate = Callable[[Any], bool]


@dataclass(frozen=True)
class Plan:
    """What a query asks for: equality filters, predicates, a sort key and a limit."""

    equal: tuple[tuple[str, Any], ...] = ()
    predicates: tuple[Predicate, ...] = ()
    by: str | Callable[[Any], Any] | None = None
    desc: bool = False
    limit: int | None = None


class Source(Protocol[T_co]):
    """A collection that runs a whole `Plan` itself, such as a database table."""

    def run(self, plan: Plan, /) -> Iterable[T_co]: ...

    def count(self, plan: Plan, /) -> int: ...


class Query(Generic[T]):
    """Rows from a source, narrowed by `filter`, `sort` and `limit`, read by `get`."""

    def __init__(self, source: Callable[[], Iterable[T]] | Source[T], plan: Plan = Plan()) -> None:
        self._source = source
        self._plan = plan

    def filter(self, predicate: Predicate | None = None, /, **equal: Any) -> Query[T]:
        """Keep rows where `predicate(row)` holds and each named field equals its value."""
        plan = self._plan
        if predicate is not None:
            plan = replace(plan, predicates=(*plan.predicates, predicate))
        if equal:
            plan = replace(plan, equal=(*plan.equal, *equal.items()))
        return Query(self._source, plan)

    def sort(self, by: str | Callable[[T], Any], *, desc: bool = False) -> Query[T]:
        """Order rows by a field name or a key function."""
        return Query(self._source, replace(self._plan, by=by, desc=desc))

    def limit(self, n: int) -> Query[T]:
        """Keep at most `n` rows."""
        if n < 0:
            raise ValueError(f"limit must be at least 0, not {n}")
        return Query(self._source, replace(self._plan, limit=n))

    def get(self) -> list[T]:
        """Run the query and return its rows."""
        if not callable(self._source):
            return list(self._source.run(self._plan))
        return run(self._source(), self._plan)

    @property
    def first(self) -> T | None:
        """The first row, or None when there is none."""
        found = self.limit(1).get()
        return found[0] if found else None

    @property
    def count(self) -> int:
        """How many rows the query holds."""
        if not callable(self._source):
            return self._source.count(self._plan)
        return len(self.get())

    def __repr__(self) -> str:
        return f"Query({self._plan})"


def run(rows: Iterable[T], plan: Plan) -> list[T]:
    """Apply `plan` to plain rows: lazily filtered, then a heap for a sort with a limit."""
    kept: Iterable[T] = (row for row in rows if matches(row, plan))
    if plan.by is None:
        return list(itertools.islice(kept, plan.limit))
    key = plan.by if callable(plan.by) else field(plan.by)
    if plan.limit is not None:
        pick = heapq.nlargest if plan.desc else heapq.nsmallest
        return pick(plan.limit, kept, key=key)
    return sorted(kept, key=key, reverse=plan.desc)


def matches(row: Any, plan: Plan) -> bool:
    """Whether `row` passes every equality filter and predicate of `plan`."""
    return (all(value_of(row, name) == value for name, value in plan.equal)
            and all(predicate(row) for predicate in plan.predicates))


def field(name: str) -> Callable[[Any], Any]:
    """A key function reading `name` from a row, as an attribute or a mapping key."""
    return lambda row: value_of(row, name)


def value_of(row: Any, name: str) -> Any:
    """`row[name]` for a mapping, else `row.name`; a missing field reads as None."""
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)

