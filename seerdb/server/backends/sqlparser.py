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

It is a front end (a recursive-descent parse over the shared
:func:`seerdb.common.sqltext` tokenizer), an Oracle-leaning tree, and a
PostgreSQL emitter — a classic transpiler shape, so each new construct is a
node and a lowering rather than another text pass that must coexist with all
the others. Three constructs live here so far:

* :func:`rewrite_outer_joins` — the ``(+)`` outer-join operator, lowered to
  ANSI ``LEFT JOIN``. ``(+)`` has no regex translation (a legacy-outer-join
  query reaches PostgreSQL verbatim and errors), so this was net-new capability.
* :func:`rewrite_rownum` — a top-level ``ROWNUM`` filter, lowered to ``LIMIT``.
  This one *replaces* a mature regex rule (``_rewrite_rownum`` in
  ``postgres.py``), and the design's shadow-compare proves the two agree before
  the parsed rule goes live.
* :func:`rewrite_connect_by` — an Oracle ``CONNECT BY`` hierarchical query,
  lowered to a ``WITH RECURSIVE`` CTE. Also replaces a regex rule
  (``_translate_connect_by``), proven by the same shadow-compare; the tokeniser
  replaces a non-greedy mega-regex that could not see parenthesis depth.

They are deliberately conservative. A statement outside the supported subset is
*cleanly declined* (``None``), so the caller keeps it unchanged. ``ROWNUM``
additionally has shapes the parser recognises but cannot lower to a single
faithful ``LIMIT`` (Oracle numbers rows before a sort, grouping or aggregate);
those raise :class:`Unsupported`, which the backend turns into a by-name refusal
rather than letting an untranslatable ``ROWNUM`` reach PostgreSQL.
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


# --- ROWNUM as a top-level LIMIT filter (#1271) -------------------------------
#
# The second construct. Unlike `(+)`, a top-level `ROWNUM <= n` filter already
# has a faithful regex translation in the backend; this is the parsed form the
# design's shadow-compare proves equivalent to it before it goes live.


class Unsupported(Exception):
    """A statement the parser owns but cannot faithfully lower.

    Distinct from :class:`_Decline` (outside the subset → return ``None``, keep
    the statement): this is a ``ROWNUM`` with no single ``LIMIT`` that keeps the
    same rows. The backend turns it into an ``UnsupportedFeature`` so the
    statement is refused by name rather than reaching PostgreSQL with a ``ROWNUM``
    column PostgreSQL does not have.
    """


# A fast pre-check and the word scan both need ROWNUM as a whole word.
_ROWNUM_WORD = re.compile(r'\bROWNUM\b', re.IGNORECASE)
# The one leaf predicate ROWNUM lowers: `ROWNUM <op> n`, n an integer literal or
# a bind. Matched against a whole WHERE conjunct (hence `fullmatch`), so a
# conjunct that is anything more -- `ROWNUM > 1`, `ROWNUM <= f(x)`, `ROWNUM
# BETWEEN ...` -- does not match and the statement is refused.
_ROWNUM_PREDICATE = re.compile(r'ROWNUM\s*(<=|<|=)\s*(\d+|:\w+)', re.IGNORECASE)
# Top-level clauses under which Oracle's ROWNUM (assigned before the clause runs)
# and SQL's LIMIT (applied after) part ways, plus OR (which could bind the filter
# to one branch). Any of these at depth zero refuses the LIMIT rewrite.
_ROWNUM_BLOCKERS = frozenset(
    {'ORDER', 'GROUP', 'HAVING', 'UNION', 'INTERSECT', 'MINUS', 'EXCEPT',
     'CONNECT', 'START', 'FOR', 'OR', 'FETCH', 'OFFSET', 'LIMIT', 'MODEL'}
)  # fmt: skip
# Aggregate and analytic functions: ROWNUM numbers the detail rows before they
# are aggregated, so a LIMIT applied to the aggregate keeps a different set.
_ROWNUM_AGGREGATES = frozenset(
    {'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'LISTAGG', 'STDDEV', 'VARIANCE',
     'MEDIAN', 'COLLECT', 'XMLAGG', 'JSON_ARRAYAGG', 'JSON_OBJECTAGG',
     'ARRAY_AGG', 'STRING_AGG', 'CORR', 'COVAR_POP', 'COVAR_SAMP',
     'RANK', 'DENSE_RANK', 'CUME_DIST', 'PERCENT_RANK'}
)  # fmt: skip
_ROWNUM_AGGREGATE_PREFIXES = ('REGR_', 'PERCENTILE_')


def _rownum_limit(op: str, bound: str) -> str:
    # The LIMIT keeping the same rows as `ROWNUM <op> bound`. `<= n` is n rows;
    # `< n` is n - 1, never below zero; `= 1` is one row and `= k` for k > 1 is
    # none (Oracle assigns ROWNUM = 1 to the first row it would return, so no row
    # ever has ROWNUM = 2 without a row before it). A bound that is a bind keeps
    # the arithmetic in SQL.
    if bound.isdigit():
        count = int(bound)
        if op == '<=':
            return str(count)
        if op == '<':
            return str(max(count - 1, 0))
        return str(int(count == 1))
    if op == '<=':
        return bound
    if op == '<':
        return f'greatest({bound} - 1, 0)'
    return f'CASE WHEN {bound} = 1 THEN 1 ELSE 0 END'


def _is_rownum_aggregate(name: str) -> bool:
    upper = name.upper()
    return upper in _ROWNUM_AGGREGATES or upper.startswith(_ROWNUM_AGGREGATE_PREFIXES)


def rewrite_rownum(sql: str) -> str | None:
    """Lower a top-level ``ROWNUM`` filter to ``LIMIT`` (#1271).

    Returns the rewritten statement, ``None`` when the statement has no
    ``ROWNUM`` outside a string or comment (the caller keeps it unchanged), or
    raises :class:`Unsupported` for a ``ROWNUM`` the parser owns but cannot lower
    to one faithful ``LIMIT``.

    The supported shape is a single top-level
    ``SELECT ... FROM ... WHERE <conjunction>`` exactly one conjunct of which is
    ``ROWNUM <op> n`` (``op`` one of ``<=``, ``<``, ``=``; ``n`` an integer or a
    bind). The ``ROWNUM`` conjunct becomes the ``LIMIT`` and every other conjunct
    stays in ``WHERE``. Every other ``ROWNUM`` raises :class:`Unsupported` —
    under an ``ORDER BY``, ``GROUP BY``, ``DISTINCT``, aggregate, set operator,
    ``OR`` or ``FOR UPDATE``; more than one ``ROWNUM``; or a ``ROWNUM`` in the
    select list or nested in a sub-query — because none has a single-``LIMIT``
    equivalent (#33). This mirrors the regex rule it replaces, which also refuses
    a nested ``ROWNUM`` rather than rewriting inside the sub-query.
    """
    if not _ROWNUM_WORD.search(sql):
        return None
    toks = _tokenize(sql)
    depth = 0
    depth0: list[tuple[int, str]] = []  # (index, word) of each depth-zero word
    rownum_at: list[int] = []  # index of every ROWNUM word, at any depth
    from_at = where_at = -1
    for i, t in enumerate(toks):
        if t.text == '(':
            depth += 1
        elif t.text == ')':
            depth -= 1
        elif t.kind == 'word':
            if depth == 0:
                depth0.append((i, t.word))
                if t.word == 'FROM' and from_at < 0:
                    from_at = i
                elif t.word == 'WHERE' and where_at < 0:
                    where_at = i
            if t.word == 'ROWNUM':
                rownum_at.append(i)
    if not rownum_at:
        return None  # ROWNUM matched only inside a string or a comment
    refusal = Unsupported('ROWNUM is not a plain top-level LIMIT filter')
    names = [w for _, w in depth0]
    if (
        len(rownum_at) != 1
        or not names
        or names[0] != 'SELECT'
        or from_at < 0
        or where_at < 0
        or _ROWNUM_BLOCKERS & set(names)
    ):
        raise refusal
    # The one ROWNUM must be a depth-zero word that falls inside the WHERE clause
    # -- not nested in a sub-query, and not in the select list or FROM.
    rownum = rownum_at[0]
    where_end = _clause_end(toks, where_at + 1)
    if rownum not in {i for i, _ in depth0} or not where_at < rownum < where_end:
        raise refusal
    # No DISTINCT / UNIQUE / aggregate / analytic in the select list: Oracle
    # numbers rows before any of those collapse or reorder them.
    select = toks[1:from_at]
    first_word = next((t.word for t in select if t.kind == 'word'), '')
    if first_word in ('DISTINCT', 'UNIQUE'):
        raise refusal
    for j in range(1, from_at):
        t = toks[j]
        call = j + 1 < from_at and toks[j + 1].text == '('
        if t.kind == 'word' and (
            t.word == 'OVER' or (call and _is_rownum_aggregate(t.text))
        ):
            raise refusal
    # Split the WHERE into AND conjuncts: exactly the ROWNUM one becomes LIMIT,
    # and the rest are re-joined with AND. (Splitting then re-joining restores a
    # conjunct that itself spans an AND, e.g. `x BETWEEN 1 AND 5`.)
    rest: list[str] = []
    limit: str | None = None
    for s, e in _split_top_level(toks, where_at + 1, where_end, 'AND'):
        if s >= e:
            raise refusal  # a dangling AND
        text = sql[toks[s].start : toks[e - 1].end].strip()
        if s <= rownum < e:
            match = _ROWNUM_PREDICATE.fullmatch(text)
            if match is None:
                raise refusal
            limit = _rownum_limit(match.group(1), match.group(2))
        else:
            rest.append(text)
    if limit is None:  # ROWNUM sat on an AND boundary, not within a conjunct
        raise refusal
    head = sql[: toks[where_at].start].rstrip()
    body = f'{head} WHERE {" AND ".join(rest)}' if rest else head
    return f'{body} LIMIT {limit}'


# --- CONNECT BY -> WITH RECURSIVE (#760) --------------------------------------
#
# Oracle's hierarchical query, the third construct and the one a tokeniser most
# improves. The regex rule it replaces parses the whole statement with a single
# non-greedy pattern that has no parenthesis-depth awareness, so a sub-query or a
# string literal holding a clause keyword (FROM, WHERE, ORDER BY, ...) throws it
# off. Here the clause boundaries, the single table and the PRIOR equality are
# found over the tokeniser at depth zero; the projection / WHERE / ORDER text and
# the LEVEL / SYS_CONNECT_BY_PATH / CONNECT_BY_ROOT rewrites are reused verbatim,
# so the emitted CTE is byte-for-byte the regex form's for every shape both
# accept. The leaf regexes and the emit are a transitional copy of the backend's
# `_translate_connect_by_regex`, the shadow-compare oracle; they retire with it.

_HAS_CONNECT_BY = re.compile(r'\bCONNECT\s+BY\b', re.IGNORECASE)
_HIER_CB_COND = re.compile(
    r'^\s*(?:NOCYCLE\s+)?(?P<l>.+?)\s*=\s*(?P<r>.+?)\s*$', re.IGNORECASE
)
_HIER_PRIOR = re.compile(r'^\s*PRIOR\s+(?P<col>.+?)\s*$', re.IGNORECASE)
_HIER_SIMPLE_COL = re.compile(r'^[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?$')
_HIER_STAR = re.compile(r'(^|,)\s*(\w+\s*\.\s*)?\*\s*(,|$)')
_HIER_LEVEL = re.compile(r'\bLEVEL\b', re.IGNORECASE)
_HIER_PATH = re.compile(
    r"\bSYS_CONNECT_BY_PATH\s*\(\s*(?P<col>[\w.]+)\s*,\s*'(?P<sep>[^']*)'\s*\)",
    re.IGNORECASE,
)
_HIER_ROOT = re.compile(r'\bCONNECT_BY_ROOT\s+(?P<col>[\w.]+)', re.IGNORECASE)
_HIER_COMPOUND = re.compile(r'\b(AND|OR)\b', re.IGNORECASE)
# Depth-zero clauses that put the statement outside the hierarchical shape the
# regex rule matched; their presence declines (the statement is kept unchanged).
_HIER_DECLINE = frozenset(
    {'GROUP', 'HAVING', 'UNION', 'INTERSECT', 'MINUS', 'EXCEPT', 'FOR', 'MODEL',
     'FETCH', 'OFFSET'}
)  # fmt: skip
# The depth-zero clause keyword orders the regex rule accepts: FROM, an optional
# WHERE, then CONNECT BY with an optional START WITH on either side, then an
# optional ORDER BY. SELECT always leads (checked separately).
_HIER_ORDERS = frozenset(
    ('FROM',) + (('WHERE',) if w else ()) + h + (('ORDER',) if o else ())
    for h in (('CONNECT',), ('START', 'CONNECT'), ('CONNECT', 'START'))
    for w in (False, True)
    for o in (False, True)
)


def _hier_colname(ref: str) -> str:
    return ref.split('.')[-1].strip()


def rewrite_connect_by(sql: str) -> str | None:
    """Lower an Oracle ``CONNECT BY`` hierarchical query to a PostgreSQL
    ``WITH RECURSIVE`` CTE (#760), or return ``None`` for any shape outside the
    supported subset. ``CONNECT BY`` has no other translation, so a decline
    keeps the statement, which then errors on PostgreSQL exactly as before --
    never a wrong-but-successful result.

    Supported: a single top-level ``SELECT`` over one table, with ``START WITH``
    (the anchor filter), ``CONNECT BY PRIOR <col> = <col>`` (the recursive join,
    ``PRIOR`` on exactly one side, ``NOCYCLE`` tolerated), and ``LEVEL`` / one
    distinct ``SYS_CONNECT_BY_PATH(col,'sep')`` / one distinct
    ``CONNECT_BY_ROOT col`` as the CTE's computed columns. Declined: multiple
    tables, ``SELECT *``, a compound or non-equality ``CONNECT BY``,
    ``ORDER SIBLINGS BY``, more than one distinct path/root, or any other
    top-level clause.
    """
    if _HAS_CONNECT_BY.search(sql) is None:
        return None
    toks = _tokenize(sql)
    if not toks or toks[0].word != 'SELECT':
        return None
    end = len(toks) - 1 if toks[-1].text == ';' else len(toks)
    from_at = where_at = start_at = cb_at = order_at = -1
    siblings = False
    depth = 0
    for i in range(end):
        t = toks[i]
        if t.text == '(':
            depth += 1
        elif t.text == ')':
            depth -= 1
        elif depth == 0 and t.kind == 'word':
            w = t.word
            nxt = toks[i + 1].word if i + 1 < end else ''
            if w == 'FROM':
                if from_at >= 0:
                    return None  # a second FROM: a set operation or sub-select
                from_at = i
            elif w == 'WHERE':
                if where_at >= 0:
                    return None
                where_at = i
            elif w == 'START':
                if nxt != 'WITH' or start_at >= 0:
                    return None
                start_at = i
            elif w == 'CONNECT':
                if nxt != 'BY' or cb_at >= 0:
                    return None
                cb_at = i
            elif w == 'ORDER':
                if order_at >= 0 or nxt not in ('BY', 'SIBLINGS'):
                    return None
                order_at = i
                siblings = nxt == 'SIBLINGS'
            elif w in _HIER_DECLINE:
                return None
    if siblings:
        return None
    present = sorted(
        (p, name)
        for p, name in (
            (from_at, 'FROM'),
            (where_at, 'WHERE'),
            (start_at, 'START'),
            (cb_at, 'CONNECT'),
            (order_at, 'ORDER'),
        )
        if p >= 0
    )
    if tuple(name for _, name in present) not in _HIER_ORDERS:
        return None
    bounds = [p for p, _ in present]

    def value_span(kw_start: int, kw_len: int) -> tuple[int, int]:
        start = kw_start + kw_len
        stop = end
        for b in bounds:
            if b > kw_start:
                stop = b
                break
        return start, stop

    def clause(kw_start: int, kw_len: int) -> str:
        start, stop = value_span(kw_start, kw_len)
        return (
            sql[toks[start].start : toks[stop - 1].end].strip() if start < stop else ''
        )

    # The single table, bare identifiers only (the regex's `_HIER_SINGLE_TABLE`):
    # no schema qualifier, quoting, join or sub-query.
    fvs, fve = value_span(from_at, 1)
    from_toks = toks[fvs:fve]
    if not 1 <= len(from_toks) <= 2 or any(ft.kind != 'word' for ft in from_toks):
        return None
    table = from_toks[0].text
    alias = from_toks[1].text if len(from_toks) == 2 else table

    proj = clause(0, 1)
    connect_by = clause(cb_at, 2)
    where = clause(where_at, 1) if where_at >= 0 else ''
    start_with = clause(start_at, 2) if start_at >= 0 else ''
    order = clause(order_at, 2) if order_at >= 0 else ''

    if _HIER_COMPOUND.search(connect_by):  # a compound CONNECT BY is not modelled
        return None
    cond = _HIER_CB_COND.match(connect_by)
    if cond is None:
        return None
    left, right = cond.group('l').strip(), cond.group('r').strip()
    left_prior, right_prior = _HIER_PRIOR.match(left), _HIER_PRIOR.match(right)
    if left_prior is not None and right_prior is None:  # PRIOR on exactly one side
        prior_side, child_side = left_prior.group('col'), right
    elif right_prior is not None and left_prior is None:
        prior_side, child_side = right_prior.group('col'), left
    else:
        return None
    if not (_HIER_SIMPLE_COL.match(prior_side) and _HIER_SIMPLE_COL.match(child_side)):
        return None  # both operands must be plain column references
    parent_col, child_col = _hier_colname(prior_side), _hier_colname(child_side)

    if _HIER_STAR.search(proj):  # SELECT * would leak the CTE's computed columns
        return None

    scan = ' '.join(part for part in (proj, where, order) if part)
    paths = {(col.lower(), sep) for col, sep in _HIER_PATH.findall(scan)}
    roots = {col.lower() for col in _HIER_ROOT.findall(scan)}
    if len(paths) > 1 or len(roots) > 1:  # one distinct path / root handled
        return None

    def rewrite(text: str) -> str:
        text = _HIER_LEVEL.sub('__level', text)
        text = _HIER_PATH.sub('__path', text)
        return _HIER_ROOT.sub('__root', text)

    anchor_cols = ['1 AS __level']
    rec_cols = ['__p.__level + 1']
    if paths:
        path_match = _HIER_PATH.search(scan)
        assert path_match is not None
        pcol, sep = _hier_colname(path_match.group('col')), path_match.group('sep')
        anchor_cols.append(f"'{sep}' || {alias}.{pcol} AS __path")
        rec_cols.append(f"__p.__path || '{sep}' || {alias}.{pcol}")
    if roots:
        root_match = _HIER_ROOT.search(scan)
        assert root_match is not None
        anchor_cols.append(
            f'{alias}.{_hier_colname(root_match.group("col"))} AS __root'
        )
        rec_cols.append('__p.__root')

    anchor_where = f' WHERE {start_with.strip()}' if start_with else ''
    join = f'__p.{parent_col} = {alias}.{child_col}'
    outer_where = f' WHERE {rewrite(where).strip()}' if where else ''
    outer_order = f' ORDER BY {rewrite(order).strip()}' if order else ''
    return (
        'WITH RECURSIVE __hcte AS ('
        f'SELECT {alias}.*, '
        + ', '.join(anchor_cols)
        + f' FROM {table} {alias}{anchor_where}'
        ' UNION ALL '
        f'SELECT {alias}.*, '
        + ', '.join(rec_cols)
        + f' FROM {table} {alias} JOIN __hcte __p ON {join}'
        f') SELECT {rewrite(proj)} FROM __hcte {alias}{outer_where}{outer_order}'
    )
