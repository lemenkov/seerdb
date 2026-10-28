# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""A hand-rolled SQL parser for the PostgreSQL backend (#1865).

The backend translates Oracle SQL to PostgreSQL with ~30 sequential text→text
regex passes (``_translate_*`` in ``postgres.py``). That approach has a ceiling:
a text pass cannot track aliases, scope or operator precedence, so the
structural Oracle-isms — ``ROWNUM`` placement, ``CONNECT BY``, and the legacy
``(+)`` outer-join operator — are where it is weakest. The design (#1865) is to
grow a real parser beside the regex passes and migrate construct by construct,
each gated by the conformance suite; a library (``sqlglot``) was evaluated and
rejected because it silently corrupts exactly these constructs.

This module is the first proof of that shape, end to end for one construct: the
``(+)`` outer-join operator, lowered to ANSI ``LEFT JOIN``. It is a front end
(a recursive-descent parse over the shared :func:`seerdb.common.sqltext`
tokenizer), an Oracle-leaning tree, and a PostgreSQL emitter — a classic
transpiler shape, so the next construct is a node and a lowering rather than
another text pass that must coexist with all the others.

It is deliberately conservative: it handles the common, well-formed ``(+)``
shapes and *cleanly declines* everything else (returning ``None``), so the
caller falls back to the existing path. ``(+)`` has no regex translation today —
a legacy-outer-join query reaches PostgreSQL verbatim and errors — so declining
is never a regression, and a successful rewrite is new capability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from seerdb.common.sqltext import sql_tokens

# The clauses that end the FROM list / the WHERE search condition and begin the
# statement's tail, at parenthesis depth zero. A set operator or a hierarchical
# query interacts with join order in ways this first construct does not model,
# so their presence declines the statement (see `_parse`).
_TAIL_CLAUSES = frozenset(
    {'WHERE', 'GROUP', 'HAVING', 'ORDER', 'CONNECT', 'START', 'MODEL',
     'UNION', 'INTERSECT', 'MINUS', 'EXCEPT', 'FOR', 'FETCH', 'OFFSET',
     # LIMIT is Oracle-foreign, but an earlier pass (`_rewrite_rownum`) can
     # emit one before this rule runs; end the WHERE at it so a ROWNUM + (+)
     # statement is handled rather than declined.
     'LIMIT'}
)  # fmt: skip
# Everything a FROM list ends at: a tail clause, or the WHERE we handle.
_FROM_ENDS = _TAIL_CLAUSES
# Tail clauses that are a hard decline wherever they appear at top level: the
# rewrite cannot reason about join order across them.
_JOIN_UNSAFE = frozenset(
    {'CONNECT', 'START', 'MODEL', 'UNION', 'INTERSECT', 'MINUS', 'EXCEPT'}
)
# The characters a comparison operator is built from. The tokenizer splits
# `<=`, `<>`, `!=` into single `other` tokens, so seeing one of these is enough
# to know a `(+)` conjunct is a comparison and not a bare column.
_COMPARISON_CHARS = frozenset({'=', '<', '>'})


class _Decline(Exception):
    """Raised internally when the statement is outside the supported subset.

    Caught at the module boundary (:func:`rewrite_outer_joins`), which then
    returns ``None`` so the caller keeps the statement unchanged.
    """


@dataclass
class _Tok:
    """One significant token: whitespace and comments dropped, and a ``(+)``
    operator collapsed into a single ``plus`` token so parenthesis-depth
    tracking is not thrown off by the parentheses inside it."""

    kind: str  # 'word', 'string', 'identifier', 'bind', 'plus', or 'other'
    text: str
    start: int
    end: int

    @property
    def word(self) -> str:
        # The upper-cased keyword this token is, or '' if it is not a bare word.
        return self.text.upper() if self.kind == 'word' else ''


def _tokenize(sql: str) -> list[_Tok]:
    # Significant tokens of `sql`, `(+)` collapsed. `(+)` is three tokens from
    # the shared tokenizer -- `(`, `+`, `)` -- which would otherwise open and
    # close a parenthesis group and hide a clause keyword or a comma from the
    # depth-zero scans below.
    raw = [
        _Tok(t.kind, sql[t.start : t.end], t.start, t.end)
        for t in sql_tokens(sql)
        if t.kind not in ('space', 'comment')
    ]
    out: list[_Tok] = []
    i = 0
    while i < len(raw):
        if (
            i + 2 < len(raw)
            and raw[i].text == '('
            and raw[i + 1].text == '+'
            and raw[i + 2].text == ')'
        ):
            out.append(_Tok('plus', '(+)', raw[i].start, raw[i + 2].end))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return out


def _fold(ident: str) -> str:
    # An identifier's match key. A quoted identifier keeps its exact inner text
    # (it is case-sensitive); an unquoted one folds to upper, as Oracle does.
    ident = ident.strip()
    if len(ident) >= 2 and ident.startswith('"') and ident.endswith('"'):
        return ident[1:-1]
    return ident.upper()


@dataclass
class _TableRef:
    """A FROM-list table reference: ``schema.name alias``."""

    text: str  # the reference verbatim, re-emitted unchanged
    key: str  # the folded alias (or table name) a column qualifier matches


@dataclass
class _OuterJoin:
    """One null-producing (``(+)``) table and how it is joined.

    ``parents`` are the folded keys of the preserved tables its ON condition
    refers to; ``on`` are the predicate texts (``(+)`` already stripped) that
    make up that ON condition.
    """

    key: str
    parents: set[str] = field(default_factory=set)
    on: list[str] = field(default_factory=list)


@dataclass
class _Select:
    """The parsed shape this construct needs: the text before FROM (the SELECT
    and its list, kept verbatim), the table references, the plain WHERE
    predicates that stay, the outer joins to build, and the tail after WHERE."""

    prefix: str
    tables: list[_TableRef]
    where_plain: list[str]
    outer: dict[str, _OuterJoin]
    tail: str


def _split_top_level(
    toks: list[_Tok], start: int, end: int, on: str
) -> list[tuple[int, int]]:
    # [start, end) split into spans on each depth-zero `on` word/punctuation.
    spans: list[tuple[int, int]] = []
    depth = 0
    span_start = start
    for i in range(start, end):
        t = toks[i]
        if t.text == '(':
            depth += 1
        elif t.text == ')':
            depth -= 1
        elif depth == 0 and (t.word == on or t.text == on):
            spans.append((span_start, i))
            span_start = i + 1
    spans.append((span_start, end))
    return spans


def _clause_end(toks: list[_Tok], start: int) -> int:
    # The index of the first depth-zero tail clause at or after `start`, or
    # len(toks). A `;` ends the statement too.
    depth = 0
    for i in range(start, len(toks)):
        t = toks[i]
        if t.text == '(':
            depth += 1
        elif t.text == ')':
            depth -= 1
        elif depth == 0 and (t.word in _FROM_ENDS or t.text == ';'):
            return i
    return len(toks)


def _parse_table_ref(sql: str, toks: list[_Tok], start: int, end: int) -> _TableRef:
    # One FROM-list item, `schema.name [AS] alias`. Anything else -- a subquery,
    # an ANSI JOIN, a dblink, a table function -- is outside this construct.
    items = toks[start:end]
    if not items:
        raise _Decline('empty table reference')
    for t in items:
        if t.text in ('(', ')') or t.word in ('JOIN', 'ON', 'USING', 'SELECT'):
            raise _Decline('non-trivial table reference')
        if t.text == '@':
            raise _Decline('database link')
    # name = word ['.' word]; then an optional alias (word or quoted identifier,
    # an optional AS before it).
    pos = 0

    def take_name_word() -> _Tok:
        nonlocal pos
        if pos >= len(items) or items[pos].kind not in ('word', 'identifier'):
            raise _Decline('table name expected')
        tok = items[pos]
        pos += 1
        return tok

    name = take_name_word()
    if pos < len(items) and items[pos].text == '.':
        pos += 1
        name = take_name_word()
    alias_tok: _Tok | None = None
    if pos < len(items) and items[pos].word == 'AS':
        pos += 1
    if pos < len(items):
        if items[pos].kind in ('word', 'identifier'):
            alias_tok = items[pos]
            pos += 1
    if pos != len(items):
        raise _Decline('trailing tokens in table reference')
    key = _fold((alias_tok or name).text)
    text = sql[items[0].start : items[-1].end]
    return _TableRef(text=text, key=key)


def _qualified_ref(toks: list[_Tok], i: int) -> tuple[str, str, int] | None:
    # A `qualifier.column` reference starting at `toks[i]`, as
    # (folded qualifier, column word, index after it), or None. A following `(`
    # makes it a function call, not a column, so it is not one.
    if (
        i + 2 < len(toks)
        and toks[i].kind in ('word', 'identifier')
        and toks[i + 1].text == '.'
        and toks[i + 2].kind in ('word', 'identifier')
    ):
        after = i + 3
        if after < len(toks) and toks[after].text == '(':
            return None  # schema.func(...) -- a call, not a column
        return (_fold(toks[i].text), toks[i + 2].text, after)
    return None


def _strip_plus(sql: str, toks: list[_Tok], start: int, end: int) -> str:
    # The predicate text [start, end) with its `(+)` operators removed and any
    # space left before them tidied, so it can serve as an ON condition.
    pieces: list[str] = []
    at = toks[start].start
    for i in range(start, end):
        if toks[i].kind == 'plus':
            pieces.append(sql[at : toks[i].start].rstrip())
            at = toks[i].end
    pieces.append(sql[at : toks[end - 1].end])
    return ''.join(pieces).strip()


@dataclass
class _Predicate:
    text: str  # verbatim, `(+)` and all
    tables: set[str]  # folded qualifiers it refers to
    plus: set[str]  # folded qualifiers that carry `(+)`
    stripped: str  # text with `(+)` removed (for an ON condition)


def _analyse_predicate(
    sql: str, toks: list[_Tok], start: int, end: int, known: set[str]
) -> _Predicate:
    # Read one WHERE conjunct. A conjunct with no `(+)` is kept verbatim and not
    # examined further (it stays in WHERE). One with `(+)` must be a simple
    # comparison of qualified columns / constants, or it is declined: this is
    # where correctness lives, so the parser refuses anything it cannot be sure
    # of rather than guess.
    tables: set[str] = set()
    plus: set[str] = set()
    has_plus = any(toks[i].kind == 'plus' for i in range(start, end))
    text = sql[toks[start].start : toks[end - 1].end]
    if not has_plus:
        return _Predicate(text=text, tables=set(), plus=set(), stripped=text)
    if any(toks[i].word == 'OR' for i in range(start, end)):
        raise _Decline('(+) with OR')
    i = start
    saw_comparison = False
    last_ref_qualifier: str | None = None
    while i < end:
        t = toks[i]
        ref = _qualified_ref(toks, i)
        if ref is not None:
            qualifier, _column, nxt = ref
            if qualifier not in known:
                raise _Decline(f'unknown qualifier {qualifier!r}')
            tables.add(qualifier)
            last_ref_qualifier = qualifier
            i = nxt
            continue
        if t.kind == 'plus':
            if last_ref_qualifier is None:
                raise _Decline('(+) not after a column reference')
            plus.add(last_ref_qualifier)
            last_ref_qualifier = None
            i += 1
            continue
        last_ref_qualifier = None
        if t.kind == 'word':
            # A bare word in a `(+)` predicate is an unqualified column, a
            # function, or a keyword (IS/NULL/IN/BETWEEN/...). Any of them is a
            # shape this construct does not model faithfully, so decline.
            raise _Decline(f'bare word {t.text!r} in a (+) predicate')
        if t.text in _COMPARISON_CHARS:
            saw_comparison = True
        i += 1
    if len(plus) > 1:
        raise _Decline('(+) on more than one table in one predicate')
    if not plus:
        raise _Decline('(+) present but not attached to a table')
    if not saw_comparison:
        # Without a comparison the conjunct is not a boolean condition, and its
        # stripped text would be an invalid ON clause.
        raise _Decline('(+) conjunct is not a comparison')
    stripped = _strip_plus(sql, toks, start, end)
    return _Predicate(text=text, tables=tables, plus=plus, stripped=stripped)


def _parse(sql: str) -> _Select | None:
    # Parse the statement into the shape the emitter needs, or decline. Returns
    # None when there is no `(+)` to do anything about.
    toks = _tokenize(sql)
    if not any(t.kind == 'plus' for t in toks):
        return None  # nothing to translate
    if not toks or toks[0].word != 'SELECT':
        raise _Decline('not a top-level SELECT')
    # The FROM keyword at depth zero.
    depth = 0
    from_at = -1
    for i, t in enumerate(toks):
        if t.text == '(':
            depth += 1
        elif t.text == ')':
            depth -= 1
        elif depth == 0 and t.word == 'FROM':
            from_at = i
            break
        elif depth == 0 and t.word in _JOIN_UNSAFE:
            raise _Decline('set operator or hierarchical query')
    if from_at < 0:
        raise _Decline('no top-level FROM')
    from_end = _clause_end(toks, from_at + 1)
    # A `(+)` is only meaningful in the top-level WHERE; one anywhere else (a
    # subquery, the select list, the tail) is out of scope.
    for i, t in enumerate(toks):
        if t.kind == 'plus' and not (from_end <= i):
            raise _Decline('(+) outside the top-level WHERE')
    # The tail: WHERE (if any) then everything after it. A hierarchical or set
    # clause at top level is unsafe for the join rewrite.
    where_at = (
        from_end if from_end < len(toks) and toks[from_end].word == 'WHERE' else -1
    )
    if where_at < 0:
        raise _Decline('(+) without a WHERE clause')
    where_end = _clause_end(toks, where_at + 1)
    for i in range(where_end, len(toks)):
        if toks[i].word in _JOIN_UNSAFE:
            raise _Decline('set operator or hierarchical query after WHERE')
    # The FROM list.
    tables: list[_TableRef] = []
    for s, e in _split_top_level(toks, from_at + 1, from_end, ','):
        tables.append(_parse_table_ref(sql, toks, s, e))
    known = {t.key for t in tables}
    if len(known) != len(tables):
        raise _Decline('duplicate table key')
    # Confirm there is a `(+)` after all (it must be inside WHERE).
    if not any(toks[i].kind == 'plus' for i in range(where_at + 1, where_end)):
        raise _Decline('(+) not in the WHERE clause')
    # The WHERE conjuncts.
    where_plain: list[str] = []
    outer: dict[str, _OuterJoin] = {}
    for s, e in _split_top_level(toks, where_at + 1, where_end, 'AND'):
        if s >= e:
            raise _Decline('empty conjunct')
        pred = _analyse_predicate(sql, toks, s, e, known)
        if not pred.plus:
            where_plain.append(pred.text.strip())
            continue
        (opt,) = pred.plus
        parents = pred.tables - {opt}
        join = outer.setdefault(opt, _OuterJoin(key=opt))
        join.parents |= parents
        join.on.append(pred.stripped)
    prefix = sql[: toks[from_at].start]
    # From the end of the last WHERE token, so the whitespace before the tail
    # clause (and any trailing `;`) is preserved verbatim.
    tail = sql[toks[where_end - 1].end :]
    return _Select(
        prefix=prefix,
        tables=tables,
        where_plain=where_plain,
        outer=outer,
        tail=tail,
    )


def _emit(parsed: _Select) -> str:
    # Build the PostgreSQL statement: the preserved tables cross-joined in their
    # original order, then each null-producing table LEFT JOINed after the
    # tables its ON condition needs. JOINs are left-associative, so a left-deep
    # chain of explicit JOIN keywords keeps every earlier table in scope for a
    # later ON -- which a comma-separated list would not.
    optional = set(parsed.outer)
    base = [t for t in parsed.tables if t.key not in optional]
    if not base:
        raise _Decline('no preserved table to anchor the outer joins')
    placed = {t.key for t in base}
    # Order the optional tables so each comes after all of its parents (a
    # topological sort; a cycle, or a parent that is not a table, declines).
    pending = [t for t in parsed.tables if t.key in optional]
    ordered: list[_TableRef] = []
    while pending:
        progressed = False
        still: list[_TableRef] = []
        for t in pending:
            parents = parsed.outer[t.key].parents
            if not parents <= (placed | {t.key}):
                # A parent not yet placed (and not itself) -- wait for it.
                if not parents <= {x.key for x in parsed.tables}:
                    raise _Decline('outer join to an unknown table')
                still.append(t)
                continue
            ordered.append(t)
            placed.add(t.key)
            progressed = True
        if not progressed:
            raise _Decline('cyclic outer joins')
        pending = still
    parts = [base[0].text]
    for t in base[1:]:
        parts.append(f'CROSS JOIN {t.text}')
    for t in ordered:
        join = parsed.outer[t.key]
        on = ' AND '.join(join.on)
        parts.append(f'LEFT JOIN {t.text} ON {on}')
    from_clause = 'FROM ' + ' '.join(parts)
    where = f' WHERE {" AND ".join(parsed.where_plain)}' if parsed.where_plain else ''
    return f'{parsed.prefix}{from_clause}{where}{parsed.tail}'


# A fast pre-check so the common statement -- no `(+)` at all -- never tokenises.
_HAS_PLUS = re.compile(r'\(\s*\+\s*\)')


def rewrite_outer_joins(sql: str) -> str | None:
    """Lower Oracle's ``(+)`` outer-join operator to ANSI ``LEFT JOIN`` (#1865).

    Returns the rewritten statement, or ``None`` when the statement has no
    ``(+)`` or is outside the supported subset -- a single top-level
    ``SELECT ... FROM a, b, ... WHERE <conjunction>`` whose ``(+)`` conjuncts are
    simple comparisons of qualified columns against columns or constants. The
    caller keeps the original statement when this returns ``None``.

    The rewrite moves each ``(+)`` conjunct into the ON condition of the
    null-producing table's LEFT JOIN and leaves every plain conjunct in WHERE;
    the SELECT list and everything after WHERE are preserved verbatim.
    """
    if not _HAS_PLUS.search(sql):
        return None
    try:
        parsed = _parse(sql)
    except _Decline:
        return None
    if parsed is None:
        return None
    try:
        return _emit(parsed)
    except _Decline:
        return None
