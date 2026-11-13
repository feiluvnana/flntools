"""What a site's finder declares and receives: its parameters, their kinds, and the parsed query.

A finder declares `params`, a mapping of name to `Param`; `flnd.query.plan` reads a query written
in Lucene's syntax against them and hands the finder one plain `Query` per search it needs. Each
`Query` holds the free words, one `Term` per parameter part, and `--time`; a finder never sees the
parsed tree. A query this site can't run is a `QueryError`, a usage error.

    params = {
        "genre": Param(Name, "a genre", negate=True, lookup=genres),
        "chapters": Param(Number, "how many chapters", compare=False),
        "kind": Param(Choice("page", "user"), "what to find"),
    }
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Union

from flnd.source import Range

__all__ = ["Choice", "Date", "Error", "Kind", "Lookup", "Name", "Number", "Param", "Query",
           "QueryError", "Term", "Text", "Value"]

# One value of a term, as its kind reads it: words and names are text, numbers whole, dates days.
Value = Union[str, int, datetime.date]
# Turns a name into the site's ids: called as `lookup(site, name)`, it yields `(name, id)` for
# each thing the site calls by a name like it; `flnd.query` keeps the ones that match exactly.
Lookup = Callable[[Any, str], Iterable[tuple[str, str]]]


class Error(Exception):
    """The base of the errors of finding: `except flnd.finder.Error` catches them all."""


class QueryError(Error, ValueError):
    """A query this site can't run, naming the part at fault: a usage error, before any search."""


class Kind:
    """What a parameter's value is; `parse` reads one as written in a query."""

    # How a message names a value of this kind: "chapters takes a number".
    noun = "text"

    def parse(self, text: str) -> Value:
        """The value `text` stands for; else a `ValueError` saying what it should be."""
        return text


class Text(Kind):
    """Words, matched as the site matches words."""

    noun = "words"


class Name(Kind):
    """The name of one thing (a genre, an author), turned into its id by a param's `lookup`."""

    noun = "a name"


class Number(Kind):
    """A whole number, which a param with `compare` also takes as a range or comparison."""

    noun = "a whole number"

    def parse(self, text: str) -> Value:
        """The number `text` is written as; a `ValueError` when it isn't a whole number."""
        try:
            return int(text)
        except ValueError:
            raise ValueError(self.noun) from None


class Date(Kind):
    """A day, written YYYY-MM-DD, which a param with `compare` also takes as a range."""

    noun = "a day (YYYY-MM-DD)"

    def parse(self, text: str) -> Value:
        """The day `text` is written as; a `ValueError` when it isn't YYYY-MM-DD."""
        try:
            return datetime.datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(self.noun) from None


class Choice(Kind):
    """One of a fixed set of words, matched ignoring case: `Choice("page", "user")`."""

    def __init__(self, *choices: str):
        self.choices = choices
        self.noun = f"one of {', '.join(choices)}"

    def parse(self, text: str) -> Value:
        """The choice `text` names, spelled as declared; a `ValueError` listing them when none."""
        for choice in self.choices:
            if choice.casefold() == text.casefold():
                return choice
        raise ValueError(self.noun)


@dataclass(frozen=True)
class Param:
    """One of a finder's parameters: its kind, its help, and what the site can do with it.

        Param(Name, "a genre", any_of=True, negate=True, lookup=genres)

    Args:
        kind: a `Kind` class (`Text`, `Name`, `Number`, `Date`) or instance (`Choice(...)`).
        help: one line for `flnd find <site> --help`.
        any_of: the site takes an OR of this parameter's values in one search; without it, an
            OR of them runs as several searches.
        negate: the site can exclude a value (`-genre:example`).
        compare: the site takes ranges and comparisons of a `Number` or `Date`.
        lookup: turns a name into the site's ids; None sends the name as written.
    """

    kind: Kind | type[Kind]
    help: str
    any_of: bool = False
    negate: bool = False
    compare: bool = True
    lookup: Lookup | None = None

    @property
    def reader(self) -> Kind:
        """The kind as an instance, whether it was declared as a class or as one."""
        return self.kind() if isinstance(self.kind, type) else self.kind


@dataclass(frozen=True)
class Term:
    """One parameter part of a search: any of `values`, or the range `low` to `high`, both included.

        Term("genre", ("12", "40"))             # genre 12 or genre 40 (a param with any_of)
        Term("genre", ("7",), negated=True)     # not genre 7
        Term("chapters", low=5)                 # 5 chapters or more

    Several terms of one parameter in a search must all hold. A negated term holds when none of
    its values does. A range leaves `values` empty, and None is no limit on that side.
    """

    param: str
    values: tuple[Value, ...] = ()
    negated: bool = False
    low: int | datetime.date | None = None
    high: int | datetime.date | None = None


@dataclass(frozen=True)
class Query:
    """One search a finder runs: the free words, the parameter terms in query order, and `--time`.

        for term in query.terms:
            if term.param == "genre": ...
    """

    text: str = ""
    terms: tuple[Term, ...] = ()
    time: Range | None = None
