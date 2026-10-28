# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The hand-rolled SQL parser (#1865), first construct: ``(+)`` outer joins.

Pure text-in / text-out, so these run in the offline suite with no database.
The semantic proof -- that the lowered ANSI joins return the rows Oracle's
``(+)`` would -- is a live Mirror-over-PG test in ``test_postgres_backend.py``.
"""

from __future__ import annotations

import pytest

from seerdb.server.backends.sqlparser import rewrite_outer_joins


def test_a_right_hand_plus_is_a_left_join_keeping_the_other_table() -> None:
    # `b.k(+)` makes b the null-producing side, so every a row is kept: a LEFT
    # JOIN b. The join predicate moves into ON; the SELECT list is untouched.
    assert (
        rewrite_outer_joins(
            'SELECT e.name, d.dname FROM emp e, dept d WHERE e.deptno = d.deptno(+)'
        )
        == 'SELECT e.name, d.dname FROM emp e LEFT JOIN dept d ON e.deptno = d.deptno'
    )


def test_a_left_hand_plus_preserves_the_right_table() -> None:
    # `e.deptno(+)` makes e the null-producing side; d is preserved, so d anchors
    # the chain and e is LEFT JOINed onto it.
    assert (
        rewrite_outer_joins(
            'SELECT e.name, d.dname FROM emp e, dept d WHERE e.deptno(+) = d.deptno'
        )
        == 'SELECT e.name, d.dname FROM dept d LEFT JOIN emp e ON e.deptno = d.deptno'
    )


def test_an_optional_side_filter_folds_into_the_on_clause() -> None:
    # A single-table `(+)` filter on the null-producing side belongs in ON, not
    # WHERE -- in WHERE it would turn the outer join back into an inner one.
    assert rewrite_outer_joins(
        "SELECT * FROM emp e, dept d WHERE e.deptno = d.deptno(+) AND d.loc(+) = 'NY'"
    ) == ("SELECT * FROM emp e LEFT JOIN dept d ON e.deptno = d.deptno "
          "AND d.loc = 'NY'")  # fmt: skip


def test_a_plain_predicate_stays_in_where() -> None:
    # A predicate with no `(+)` is a filter on the result, so it stays in WHERE.
    assert rewrite_outer_joins(
        'SELECT * FROM emp e, dept d WHERE e.deptno = d.deptno(+) AND e.sal > 1000'
    ) == ('SELECT * FROM emp e LEFT JOIN dept d ON e.deptno = d.deptno '
          'WHERE e.sal > 1000')  # fmt: skip


def test_a_bind_is_kept_verbatim_in_the_on_clause() -> None:
    assert rewrite_outer_joins(
        'SELECT * FROM emp e, dept d WHERE e.deptno = d.deptno(+) AND d.loc(+) = :loc'
    ) == ('SELECT * FROM emp e LEFT JOIN dept d ON e.deptno = d.deptno '
          'AND d.loc = :loc')  # fmt: skip


def test_two_optional_tables_off_one_anchor_become_two_left_joins() -> None:
    # A star: b and c both hang off a. Left-deep JOINs keep a in scope for both.
    assert (
        rewrite_outer_joins('SELECT * FROM a, b, c WHERE a.x = b.x(+) AND a.y = c.y(+)')
        == 'SELECT * FROM a LEFT JOIN b ON a.x = b.x LEFT JOIN c ON a.y = c.y'
    )


def test_a_chain_orders_each_table_after_its_parent() -> None:
    # c hangs off b, which hangs off a: a LEFT JOIN b LEFT JOIN c, so b is in
    # scope when c's ON is evaluated.
    assert (
        rewrite_outer_joins('SELECT * FROM a, b, c WHERE a.x = b.x(+) AND b.y = c.y(+)')
        == 'SELECT * FROM a LEFT JOIN b ON a.x = b.x LEFT JOIN c ON b.y = c.y'
    )


def test_an_unrelated_table_is_cross_joined() -> None:
    # c takes no part in the outer join (only a plain filter), so it stays a
    # base table, cross-joined; b hangs off a as a LEFT JOIN.
    assert rewrite_outer_joins(
        'SELECT * FROM a, b, c WHERE a.x = b.x(+) AND c.flag = 1'
    ) == ('SELECT * FROM a CROSS JOIN c LEFT JOIN b ON a.x = b.x '
          'WHERE c.flag = 1')  # fmt: skip


def test_the_tail_after_where_is_preserved() -> None:
    assert rewrite_outer_joins(
        'SELECT d.dname, count(*) FROM emp e, dept d WHERE e.deptno = d.deptno(+) '
        'GROUP BY d.dname ORDER BY d.dname'
    ) == ('SELECT d.dname, count(*) FROM emp e LEFT JOIN dept d ON e.deptno = d.deptno '
          'GROUP BY d.dname ORDER BY d.dname')  # fmt: skip


def test_a_limit_tail_is_preserved() -> None:
    # An earlier pass can rewrite ROWNUM to LIMIT before this rule runs, so a
    # LIMIT tail must end the WHERE rather than defeat the parse.
    assert (
        rewrite_outer_joins('SELECT * FROM a, b WHERE a.x = b.x(+) LIMIT 5')
        == 'SELECT * FROM a LEFT JOIN b ON a.x = b.x LIMIT 5'
    )


def test_a_trailing_semicolon_is_kept() -> None:
    assert (
        rewrite_outer_joins('SELECT * FROM emp e, dept d WHERE e.deptno = d.deptno(+);')
        == 'SELECT * FROM emp e LEFT JOIN dept d ON e.deptno = d.deptno;'
    )


@pytest.mark.parametrize(
    'sql',
    [
        # no (+): nothing to do
        'SELECT * FROM emp e, dept d WHERE e.deptno = d.deptno',
        # (+) on both sides of a predicate is an Oracle error, not a join
        'SELECT * FROM emp e, dept d WHERE e.deptno(+) = d.deptno(+)',
        # OR in a (+) predicate: no faithful single-join lowering
        'SELECT * FROM emp e, dept d WHERE e.deptno = d.deptno(+) OR e.sal > 1',
        # (+) inside a subquery, not the top-level WHERE
        'SELECT * FROM emp e WHERE e.x = (SELECT y FROM d WHERE d.z(+) = e.z)',
        # a function around the operand -- a bare word appears
        'SELECT * FROM emp e, dept d WHERE nvl(e.c, 0) = d.deptno(+)',
        # an unqualified column cannot be attributed to a table
        'SELECT * FROM emp e, dept d WHERE c = d.deptno(+)',
        # a qualifier that names no FROM table
        'SELECT * FROM emp e, dept d WHERE e.deptno = x.deptno(+)',
        # IS NULL is not a comparison the lowering models
        'SELECT * FROM emp e, dept d WHERE d.deptno(+) IS NULL AND e.x = d.x(+)',
        # not a top-level SELECT
        'UPDATE emp e SET x = (SELECT 1 FROM d WHERE d.k(+) = e.k)',
        # an ANSI join already present
        'SELECT * FROM emp e JOIN dept d ON e.d = d.d, x WHERE x.k = d.k(+)',
    ],
)
def test_declines_outside_the_supported_subset(sql: str) -> None:
    assert rewrite_outer_joins(sql) is None
