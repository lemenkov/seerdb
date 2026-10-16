# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Suggestions for the statements a migration profile says lose time (#1883).

The third stage of the migration profiler: for a statement the run over
PostgreSQL is slower on (#1882), the patterns in its recording that say how
to win the time back. Each rule reads what the profile recorded for the
statement over PostgreSQL -- the PostgreSQL text it ran as, the statements
sent per call, the rows per call, the kind of call -- and says whose fix it
is: the application's, with a native rewrite it can send over the same
connection, or the Mirror's, when every application would pay it. A
Mirror cost is a Mirror performance issue to report, not a rewrite.

The rules start small, from what the Mirror's own performance work measured.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

APPLICATION = 'application'
MIRROR = 'Mirror'

# The statements the Mirror sends PostgreSQL per call for a plain query and
# for anything else, measured over seerdb's integration suite (#1883): a
# query is about two, a DML or DDL about four. Above these by more than
# _EXTRA, the Mirror is doing work of its own on every call.
_QUERY_STATEMENTS = 2.0
_OTHER_STATEMENTS = 4.0
_EXTRA = 1.5
# The calls from which statements per call say something: a statement's
# first call also fills the Mirror's caches.
_WARM_CALLS = 10
# The rows per call from which a helper evaluated per row is worth naming.
_MANY_ROWS = 10

_QUERY = re.compile(r'\s*(?:\(\s*)*(?:select|with)\b', re.IGNORECASE)
_HELPER = re.compile(r'\bsys\.(\w+)\s*\(', re.IGNORECASE)
_ROWID = re.compile(r'\breturning\b[^;]*\bctid\b', re.IGNORECASE)
_DO_BLOCK = re.compile(r'\s*do\s+\$', re.IGNORECASE)
_WHERE = re.compile(r'\bwhere\b', re.IGNORECASE)
# A function around a bare column in a condition: not around a bind
# (%(b1)s), a literal or a number, which leave an index usable.
_WRAPPED_COLUMN = re.compile(
    r'\b(trunc|upper|lower|coalesce|to_char|to_date|substr|date_trunc)\s*\(\s*'
    r'([a-z_"][\w$#".]*)\s*[,)]',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Suggestion:
    """One way to win a statement's time back, and whose fix it is."""

    rule: str
    owner: str
    text: str


def _per_call(entry: dict[str, Any], translated: str) -> Suggestion | None:
    backend = entry.get('backend')
    if not backend or entry['calls'] < _WARM_CALLS:
        return None
    sent = backend['statements_per_call']
    usual = _QUERY_STATEMENTS if _QUERY.match(translated) else _OTHER_STATEMENTS
    if sent <= usual + _EXTRA:
        return None
    return Suggestion(
        'statements per call',
        MIRROR,
        f'The Mirror sends PostgreSQL {sent:.1f} statements per call, where '
        f'about {usual:.0f} usually do: it looks something up, or takes extra '
        'steps, on every call. A Mirror cost: report it as a Mirror '
        'performance issue.',
    )


def _rowid(entry: dict[str, Any], translated: str) -> Suggestion | None:
    if not _ROWID.search(translated):
        return None
    return Suggestion(
        'ROWID',
        MIRROR,
        "The Mirror asks PostgreSQL for each inserted row's ROWID "
        '(RETURNING ctid), for the reply Oracle gives, whether or not the '
        'application reads it. Every application pays it: a Mirror cost.',
    )


def _helper(entry: dict[str, Any], translated: str) -> Suggestion | None:
    helpers = sorted({m.lower() for m in _HELPER.findall(translated)})
    rows = entry['rows_out'] / entry['calls']
    if not helpers or rows < _MANY_ROWS or not _QUERY.match(translated):
        return None
    named = ', '.join(f'sys.{h}' for h in helpers)
    return Suggestion(
        'helper per row',
        APPLICATION,
        f'{named} emulates an Oracle feature and runs once per row, {rows:,.0f} '
        'rows a call. A native rewrite without the feature (a join in place '
        'of a DEREF, a plain column in place of a ROWID) runs as one query.',
    )


def _anonymous_block(entry: dict[str, Any], translated: str) -> Suggestion | None:
    if 'plsql' not in entry['kinds'] or not _DO_BLOCK.match(translated):
        return None
    return Suggestion(
        'anonymous block',
        APPLICATION,
        'The PL/SQL block runs as an anonymous DO block, which PostgreSQL '
        'plans afresh on every call and which returns nothing. Create it '
        'once as a PostgreSQL function and call that.',
    )


def _index(entry: dict[str, Any], translated: str) -> Suggestion | None:
    where = _WHERE.search(translated)
    if where is None:
        return None
    found = _WRAPPED_COLUMN.search(translated, where.end())
    if found is None:
        return None
    (function, column) = (found.group(1).lower(), found.group(2))
    return Suggestion(
        'index',
        APPLICATION,
        f'{function}({column}) in a condition keeps PostgreSQL from using an '
        f'index on {column}. Compare the column itself (a range in place of '
        f'trunc, say), or index the expression.',
    )


_RULES: tuple[Callable[[dict[str, Any], str], Suggestion | None], ...] = (
    _per_call,
    _rowid,
    _helper,
    _anonymous_block,
    _index,
)


def suggest(entry: dict[str, Any]) -> list[Suggestion]:
    """The suggestions for one statement, from what a profile over
    PostgreSQL recorded for it: application rewrites first."""
    translated = entry.get('translated') or ''
    found = [s for rule in _RULES if (s := rule(entry, translated)) is not None]
    return sorted(found, key=lambda s: s.owner != APPLICATION)
