# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Suggestions for the statements a migration profile says lose time (#1883):
each rule, whose fix it says it is, and what it leaves alone."""

from __future__ import annotations

from typing import Any

from seerdb.server.profile_suggest import APPLICATION, MIRROR, suggest


def _entry(translated: str, **kw: Any) -> dict[str, Any]:
    # A statement as a profile over PostgreSQL records it.
    entry: dict[str, Any] = {
        'calls': 100,
        'rows_out': 100,
        'kinds': {'execute': 100},
        'translated': translated,
        'backend': {'statements_per_call': 2.0, 'database_ms': 1, 'mirror_ms': 1},
    }
    entry.update(kw)
    return entry


def _rules(entry: dict[str, Any]) -> list[tuple[str, str]]:
    return [(s.rule, s.owner) for s in suggest(entry)]


def test_a_plain_statement_has_no_suggestion() -> None:
    assert suggest(_entry('SELECT a FROM t WHERE id = %(b1)s')) == []
    assert suggest(_entry('', kinds={'plsql': 1}, backend=None)) == []


def test_statements_per_call_above_the_usual_are_the_mirrors() -> None:
    # A query usually sends two, anything else four; well above that, the
    # Mirror does work of its own on every call. A statement called only a
    # few times is still filling the Mirror's caches, and says nothing.
    busy = {'statements_per_call': 7.0, 'database_ms': 1, 'mirror_ms': 1}
    assert _rules(_entry('SELECT 1 FROM dual', backend=busy)) == [
        ('statements per call', MIRROR)
    ]
    assert suggest(_entry('SELECT 1 FROM dual', backend=busy, calls=3)) == []
    usual = {'statements_per_call': 5.0, 'database_ms': 1, 'mirror_ms': 1}
    assert suggest(_entry('UPDATE t SET a = 1', backend=usual)) == []
    assert _rules(_entry('SELECT a FROM t', backend=usual)) == [
        ('statements per call', MIRROR)
    ]


def test_a_rowid_asked_for_on_insert_is_the_mirrors() -> None:
    assert _rules(
        _entry('INSERT INTO t VALUES (%(b1)s) RETURNING tableoid::bigint, ctid::text')
    ) == [('ROWID', MIRROR)]


def test_a_helper_run_per_row_is_the_applications() -> None:
    text = 'SELECT sys.deref(r), sys.ora_rowid(t.ctid) FROM t'
    (found,) = suggest(_entry(text, rows_out=5000))
    assert (found.rule, found.owner) == ('helper per row', APPLICATION)
    assert 'sys.deref, sys.ora_rowid' in found.text
    assert '50 rows a call' in found.text
    # A row or two a call is no matter.
    assert suggest(_entry(text, rows_out=100)) == []


def test_an_anonymous_block_is_the_applications() -> None:
    block = _entry('DO $$ BEGIN NULL; END $$', kinds={'plsql': 1})
    assert _rules(block) == [('anonymous block', APPLICATION)]
    # DDL the Mirror runs as a DO block of its own is not the application's.
    assert suggest(_entry('DO $$ BEGIN DROP TYPE x; END $$')) == []


def test_a_function_around_a_column_defeats_an_index() -> None:
    (found,) = suggest(_entry('SELECT id FROM t WHERE trunc(t) = %(b1)s::timestamptz'))
    assert (found.rule, found.owner) == ('index', APPLICATION)
    assert found.text.startswith('trunc(t) in a condition')
    # Around a bind, or in the select list, it leaves the index alone.
    assert suggest(_entry('SELECT id FROM t WHERE t = trunc(%(b1)s)')) == []
    assert suggest(_entry('SELECT upper(name) FROM t WHERE id = 1')) == []


def test_application_fixes_come_first() -> None:
    busy = {'statements_per_call': 9.0, 'database_ms': 1, 'mirror_ms': 1}
    text = 'SELECT a FROM t WHERE upper(name) = %(b1)s'
    assert [s.owner for s in suggest(_entry(text, backend=busy))] == [
        APPLICATION,
        MIRROR,
    ]
