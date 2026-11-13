"""Reads a find query, in Lucene's syntax, into the searches a site's finder can run.

`plan` is the entry point. It parses the query with luqum (no other module imports it), checks
each part against the finder's `params`, rewrites the query into an OR of AND-groups, and returns
one `flnd.finder.Query` per group, in order, with names turned into ids by each param's `lookup`.
What the site can't do is a `QueryError` naming the part, before any request. Running the
searches, and merging their results by `Found.id`, is the engine's.

    plan("genre:(vanilla OR romance) -genre:example", params)
    # [Query(terms=(Term("genre", ("vanilla",)), Term("genre", ("example",), negated=True))),
    #  Query(terms=(Term("genre", ("romance",)), Term("genre", ("example",), negated=True)))]
"""

from __future__ import annotations

import datetime
import functools
import math
import re
import threading
import unicodedata
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn, Union

import ply.lex
import ply.yacc
from luqum.exceptions import ParseError
from luqum.visitor import TreeVisitor

from flnd.finder import Date, Lookup, Number, Param, Query, QueryError, Term, Value
from flnd.source import Range

__all__ = ["MAX_SEARCHES", "plan"]

# The most searches one query may fan out to, so an OR-heavy query never floods a site unnoticed.
MAX_SEARCHES = 8
# ply's parser keeps its state in module globals, so luqum parses one query at a time.
_LOCK = threading.Lock()


def plan(text: str, params: Mapping[str, Param], *, site: Any = None,
         cache: MutableMapping[str, str] | None = None, time: Range | None = None) -> list[Query]:
    """The searches that together find what `text` asks for, in the order the query gives them.

        plan("author:example_creator chapters:1", params, site=site, cache=ids)

    Args:
        text: the query, in Lucene's syntax.
        params: the finder's parameters, by name.
        site: what each param's `lookup` is called with.
        cache: ids found by lookups, by `<param>:<name folded>`, read and filled here; the caller
            keeps one per site. None keeps them for this call only.
        time: `--time`, set on every search.

    Raises:
        QueryError: the query doesn't parse, names a parameter the site doesn't take, gives a
            value of the wrong kind, asks what the site can't send, needs more than
            `MAX_SEARCHES` searches, or names a thing its lookup doesn't find once.
    """
    tree = _Reader(params).read(_tree(text))
    shape = _merged(_pushed(tree, negated=False, params=params), params)
    count = _count(shape)
    if count > MAX_SEARCHES:
        raise QueryError(f"the query needs {count} searches and flnd runs at most {MAX_SEARCHES} "
                         "for one; narrow its ORs")
    names = _Names(params, site, {} if cache is None else cache)
    return [names.query(group, time) for group in _groups(shape)]


def _tree(text: str) -> Any:
    """luqum's tree of `text`; luqum's error, with its position, becomes a QueryError."""
    if not text.strip():
        raise QueryError("the query is empty")
    with _LOCK:
        try:
            return _parser().parse(text)
        except ParseError as error:
            message = re.sub(r"\s+", " ", str(error)).removeprefix("Syntax error in input : ")
            raise QueryError(f"can't read the query: {message.rstrip('!')}") from None


@functools.cache
def _parser() -> Any:
    """luqum's parser, built once with ply's warnings silenced and no table files written."""
    # luqum builds its lexer and parser when imported, and ply then prints grammar warnings and
    # writes parser.out and parsetab.py beside luqum: both are swapped for quiet builds meanwhile.
    lex, yacc = ply.lex.lex, ply.yacc.yacc
    ply.lex.lex = functools.partial(lex, errorlog=ply.lex.NullLogger())
    ply.yacc.yacc = functools.partial(yacc, debug=False, write_tables=False,
                                      errorlog=ply.yacc.NullLogger())
    try:
        from luqum.parser import parser
    finally:
        ply.lex.lex, ply.yacc.yacc = lex, yacc
    return parser


@dataclass(frozen=True)
class _Atom:
    """One checked part: a parameter's values or range, or free words (`param` empty)."""

    param: str
    values: tuple[Value, ...] = ()
    low: int | datetime.date | None = None
    high: int | datetime.date | None = None
    # The query's text of it, for messages; two atoms that differ only here are the same.
    part: str = field(default="", compare=False)


@dataclass(frozen=True)
class _Not:
    node: _Node


@dataclass(frozen=True)
class _And:
    nodes: tuple[_Node, ...]


@dataclass(frozen=True)
class _Or:
    nodes: tuple[_Node, ...]


@dataclass(frozen=True)
class _Lit:
    """An atom or its negation, once every NOT is pushed down to an atom."""

    atom: _Atom
    negated: bool


_Node = Union[_Atom, _Not, _And, _Or, _Lit]


class _Reader(TreeVisitor):
    """luqum's tree as flnd's own nodes, each parameter and value checked against `params`."""

    def __init__(self, params: Mapping[str, Param]):
        super().__init__()
        self.params = params

    def read(self, tree: Any) -> _Node:
        """The tree as `_Atom`s under `_And`, `_Or` and `_Not`."""
        return self.one(tree, {})

    def one(self, node: Any, context: dict[str, str]) -> _Node:
        """The single node a visit method yields for `node`."""
        return next(iter(self.visit_iter(node, context)))

    def visit_search_field(self, node: Any, context: dict[str, str]) -> Iterator[_Node]:
        part = str(node).strip()
        if "param" in context:
            raise QueryError(f"'{part}': a parameter can't hold another")
        if node.name not in self.params:
            takes = ", ".join(self.params) if self.params else "only words"
            raise QueryError(f"'{part}': no parameter {node.name}; this site takes {takes}")
        yield self.one(node.expr, {"param": node.name, "part": part})

    def visit_base_group(self, node: Any, context: dict[str, str]) -> Iterator[_Node]:
        yield self.one(node.expr, context)

    def visit_and_operation(self, node: Any, context: dict[str, str]) -> Iterator[_Node]:
        yield _And(tuple(self.one(operand, context) for operand in node.operands))

    # Words side by side, with no operator between them, mean all of them.
    visit_unknown_operation = visit_and_operation

    def visit_or_operation(self, node: Any, context: dict[str, str]) -> Iterator[_Node]:
        yield _Or(tuple(self.one(operand, context) for operand in node.operands))

    def visit_not(self, node: Any, context: dict[str, str]) -> Iterator[_Node]:
        yield _Not(self.one(node.a, context))

    visit_prohibit = visit_not

    def visit_plus(self, node: Any, context: dict[str, str]) -> Iterator[_Node]:
        # `+word` is Lucene's "must", which every AND-group already means.
        yield self.one(node.a, context)

    def visit_regex(self, node: Any, context: dict[str, str]) -> NoReturn:
        return self.generic_visit(node, context)

    def visit_term(self, node: Any, context: dict[str, str]) -> Iterator[_Node]:
        part = context.get("part", str(node).strip())
        if node.has_wildcard():
            raise QueryError(f"'{part}': flnd queries take no wildcards")
        # A phrase's value keeps its quotes; the words inside are what is searched for.
        text = node.unescaped_value
        text = text.strip('"') if node.value.startswith('"') else text
        param = context.get("param", "")
        value = self.value(param, text, part) if param else text
        yield _Atom(param, (value,), part=part)

    def visit_range(self, node: Any, context: dict[str, str]) -> Iterator[_Node]:
        yield self.range(context, (node.low, node.include_low), (node.high, node.include_high))

    def visit_from(self, node: Any, context: dict[str, str]) -> Iterator[_Node]:
        yield self.range(context, (node.a, node.include), None)

    def visit_to(self, node: Any, context: dict[str, str]) -> Iterator[_Node]:
        yield self.range(context, None, (node.a, node.include))

    def generic_visit(self, node: Any, context: dict[str, str]) -> NoReturn:
        raise QueryError(f"'{str(node).strip()}': flnd queries take no fuzzy, proximity, boost "
                         "or regex terms")

    def value(self, param: str, text: str, part: str) -> Value:
        """`text` as the kind of `param` reads it."""
        try:
            return self.params[param].reader.parse(text)
        except ValueError as error:
            raise QueryError(f"'{part}': {param} takes {error}") from None

    def range(self, context: dict[str, str], low: tuple[Any, bool] | None,
              high: tuple[Any, bool] | None) -> _Atom:
        """A range or comparison, each side a `(luqum term, included)` or None for no limit."""
        param, part = context.get("param", ""), context.get("part", "")
        if not param:
            raise QueryError("a range or comparison needs a parameter, such as chapters:>5")
        if not isinstance(self.params[param].reader, (Number, Date)):
            raise QueryError(f"'{part}': {param} takes no range or comparison")
        if not self.params[param].compare:
            raise QueryError(f"'{part}': this site can't compare {param}; give one value")
        first = self.bound(param, low, part, up=True)
        last = self.bound(param, high, part, up=False)
        if first is not None and last is not None and _after(first, last):
            raise QueryError(f"'{part}': the range is empty")
        return _Atom(param, low=first, high=last, part=part)

    def bound(self, param: str, side: tuple[Any, bool] | None, part: str,
              *, up: bool) -> int | datetime.date | None:
        """One side of a range as an included value; a side left open (`*`) is None."""
        if side is None or side[0].value == "*":
            return None
        value = self.value(param, side[0].unescaped_value, part)
        if not isinstance(value, (int, datetime.date)):
            raise QueryError(f"'{part}': {param} takes no range or comparison")
        return value if side[1] else _step(value, up=up)


def _step(value: int | datetime.date, *, up: bool) -> int | datetime.date:
    """The next whole number or day after `value` (before it when not `up`): `>5` is `>=6`."""
    if isinstance(value, int):
        return value + 1 if up else value - 1
    one = datetime.timedelta(days=1)
    return value + one if up else value - one


def _after(first: int | datetime.date, last: int | datetime.date) -> bool:
    """Whether `first` comes after `last`, both being of the one kind a param reads."""
    if isinstance(first, int) and isinstance(last, int):
        return first > last
    return isinstance(first, datetime.date) and isinstance(last, datetime.date) and first > last


def _pushed(node: _Node, *, negated: bool, params: Mapping[str, Param]) -> _Node:
    """`node` with every NOT pushed down to an atom (De Morgan), each negation checked."""
    if isinstance(node, _Not):
        return _pushed(node.node, negated=not negated, params=params)
    if isinstance(node, _Atom):
        if negated and not node.param:
            raise QueryError(f"'{node.part}': this site can't exclude words")
        if negated and not params[node.param].negate:
            raise QueryError(f"'{node.part}': this site can't exclude by {node.param}")
        return _Lit(node, negated)
    if not isinstance(node, (_And, _Or)):
        return node
    nodes = tuple(_pushed(child, negated=negated, params=params) for child in node.nodes)
    # Under a NOT, an AND turns into an OR of the negations, and an OR into an AND.
    return _Or(nodes) if isinstance(node, _And) == negated else _And(nodes)


def _merged(node: _Node, params: Mapping[str, Param]) -> _Node:
    """`node` with each OR of an `any_of` param's plain values joined into one literal."""
    if isinstance(node, _And):
        return _And(tuple(_merged(child, params) for child in node.nodes))
    if not isinstance(node, _Or):
        return node
    joined: list[_Node] = []
    # Where each any_of param's literal sits in `joined`, and the literal itself.
    first: dict[str, tuple[int, _Lit]] = {}
    for child in _flat(tuple(_merged(child, params) for child in node.nodes)):
        if isinstance(child, _Lit) and _joinable(child, params):
            name = child.atom.param
            if name in first:
                at, held = first[name]
                values = tuple(dict.fromkeys(held.atom.values + child.atom.values))
                first[name] = at, _Lit(replace(held.atom, values=values), negated=False)
                joined[at] = first[name][1]
                continue
            first[name] = len(joined), child
        joined.append(child)
    return joined[0] if len(joined) == 1 else _Or(tuple(joined))


def _flat(nodes: tuple[_Node, ...]) -> Iterator[_Node]:
    """An OR's operands, with the operands of an OR among them taken up into it."""
    for node in nodes:
        yield from node.nodes if isinstance(node, _Or) else (node,)


def _joinable(lit: _Lit, params: Mapping[str, Param]) -> bool:
    """Whether a literal is a plain value of a param the site takes an OR of in one search."""
    atom = lit.atom
    return bool(atom.param) and not lit.negated and bool(atom.values) and params[atom.param].any_of


def _count(node: _Node) -> int:
    """How many searches `node` runs as: an OR adds its operands' counts, an AND multiplies them."""
    if isinstance(node, _Or):
        return sum(_count(child) for child in node.nodes)
    if isinstance(node, _And):
        return math.prod(_count(child) for child in node.nodes)
    return 1


def _groups(node: _Node) -> list[tuple[_Lit, ...]]:
    """`node` as an OR of AND-groups of literals, each group and literal once, in query order."""
    if isinstance(node, _Lit):
        return [(node,)]
    if isinstance(node, _Or):
        found = [group for child in node.nodes for group in _groups(child)]
    else:
        found = [()]
        for child in node.nodes if isinstance(node, _And) else ():
            found = [group + more for group in found for more in _groups(child)]
    return list(dict.fromkeys(tuple(dict.fromkeys(group)) for group in found))


class _Names:
    """Turns AND-groups into `Query`s, asking each param's `lookup` for the ids of names."""

    def __init__(self, params: Mapping[str, Param], site: Any, cache: MutableMapping[str, str]):
        self.params = params
        self.site = site
        self.cache = cache

    def query(self, group: tuple[_Lit, ...], time: Range | None) -> Query:
        """The search for one AND-group: its free words joined, one term per parameter part."""
        words = " ".join(str(lit.atom.values[0]) for lit in group if not lit.atom.param)
        terms = tuple(self.term(lit) for lit in group if lit.atom.param)
        return Query(words, terms, time)

    def term(self, lit: _Lit) -> Term:
        """The term of one literal, with names turned into ids where its param has a lookup."""
        atom = lit.atom
        lookup = self.params[atom.param].lookup
        values = atom.values
        if lookup is not None:
            values = tuple(self.id(atom, lookup, str(value)) for value in values)
        return Term(atom.param, values, lit.negated, atom.low, atom.high)

    def id(self, atom: _Atom, lookup: Lookup, name: str) -> str:
        """The site's id of the thing called `name`, from the cache or else from `lookup`."""
        key = f"{atom.param}:{_folded(name)}"
        if key not in self.cache:
            self.cache[key] = self.match(atom, list(lookup(self.site, name)), name)
        return self.cache[key]

    def match(self, atom: _Atom, found: list[tuple[str, str]], name: str) -> str:
        """The one id whose name is `name`, ignoring case and accents; else a QueryError."""
        exact = [(each, id_) for each, id_ in found if _folded(each) == _folded(name)]
        ids = list(dict.fromkeys(id_ for _, id_ in exact))
        if len(ids) == 1:
            return ids[0]
        if ids:
            listed = ", ".join(f"{each} ({id_})" for each, id_ in exact)
            raise QueryError(f"'{atom.part}': several of {atom.param} are named {name}: {listed}")
        # A lookup may list every name the site has (all its genres), so only near ones are shown.
        near = [each for each, _ in found if _folded(name) in _folded(each)][:5]
        like = f"; like it: {', '.join(near)}" if near else ""
        raise QueryError(f"'{atom.part}': this site has no {atom.param} named {name}{like}")


def _folded(name: str) -> str:
    """`name` as names are compared: without case, accents or extra spaces."""
    # Unicode gives đ no decomposition, yet Vietnamese readers type it as a plain d.
    text = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).casefold().split())
