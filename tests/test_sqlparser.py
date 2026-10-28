# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The hand-rolled SQL parser (#1865): ``(+)`` outer joins and ``ROWNUM``.

Pure text-in / text-out, so these run in the offline suite with no database.
The semantic proof -- that the lowered ANSI joins return the rows Oracle's
``(+)`` would -- is a live Mirror-over-PG test in ``test_postgres_backend.py``,
which also shadow-compares the ``ROWNUM`` rule against the regex form it
replaces.
"""

from __future__ import annotations

import pytest

from seerdb.server.backends.sqlparser import (
    Unsupported,
    rewrite_outer_joins,
    rewrite_rownum,
)


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


# --- ROWNUM as a top-level LIMIT filter ---------------------------------------


@pytest.mark.parametrize(
    ('sql', 'expected'),
    [
        # `<= n` is n rows, `< n` is n - 1, `= 1` is one row, `= k>1` is none.
        ('SELECT id FROM t WHERE rownum <= 1', 'SELECT id FROM t LIMIT 1'),
        ('SELECT id FROM t WHERE rownum < 3', 'SELECT id FROM t LIMIT 2'),
        ('SELECT id FROM t WHERE rownum = 1', 'SELECT id FROM t LIMIT 1'),
        ('SELECT id FROM t WHERE rownum = 2', 'SELECT id FROM t LIMIT 0'),
        # A bind keeps the arithmetic in SQL.
        ('SELECT id FROM t WHERE rownum <= :1', 'SELECT id FROM t LIMIT :1'),
        (
            'SELECT id FROM t WHERE rownum < :n',
            'SELECT id FROM t LIMIT greatest(:n - 1, 0)',
        ),
        (
            'SELECT id FROM t WHERE rownum = :n',
            'SELECT id FROM t LIMIT CASE WHEN :n = 1 THEN 1 ELSE 0 END',
        ),
        # The ROWNUM conjunct drops out; the other conjuncts stay in WHERE.
        (
            'SELECT id FROM t WHERE rownum < 3 AND a = 1',
            'SELECT id FROM t WHERE a = 1 LIMIT 2',
        ),
        (
            'SELECT id FROM t WHERE a = 1 AND rownum <= 2 AND b = 2',
            'SELECT id FROM t WHERE a = 1 AND b = 2 LIMIT 2',
        ),
        # Splitting then re-joining on AND restores a conjunct that spans one.
        (
            'SELECT id FROM t WHERE x BETWEEN 1 AND 5 AND rownum <= 3',
            'SELECT id FROM t WHERE x BETWEEN 1 AND 5 LIMIT 3',
        ),
        # An ORDER BY *inside a sub-query* is not a top-level blocker.
        (
            'SELECT * FROM (SELECT id FROM t ORDER BY id) WHERE rownum <= 3',
            'SELECT * FROM (SELECT id FROM t ORDER BY id) LIMIT 3',
        ),
    ],
)
def test_a_top_level_rownum_filter_becomes_limit(sql: str, expected: str) -> None:
    assert rewrite_rownum(sql) == expected


@pytest.mark.parametrize(
    'sql',
    [
        # no ROWNUM: nothing to do
        'SELECT id FROM t WHERE a = 1',
        # ROWNUM only inside a string literal is not the pseudo-column
        "SELECT 'rownum' FROM dual",
    ],
)
def test_rownum_leaves_a_statement_with_nothing_to_do(sql: str) -> None:
    # None means "keep the statement unchanged", as the backend wrapper does.
    assert rewrite_rownum(sql) is None


@pytest.mark.parametrize(
    'sql',
    [
        # Oracle numbers rows before ORDER BY / GROUP BY / DISTINCT / aggregates
        # / set operators, so a LIMIT applied after keeps a different set.
        'SELECT id FROM t WHERE rownum <= 1 ORDER BY id',
        'SELECT count(*) FROM t WHERE rownum <= 5',
        'SELECT DISTINCT a FROM t WHERE rownum <= 5',
        'SELECT max(x) OVER () FROM t WHERE rownum <= 1',
        'SELECT id FROM t WHERE rownum <= 1 UNION SELECT id FROM u',
        'SELECT id FROM t GROUP BY id HAVING rownum <= 1',
        # OR could bind the filter to one branch; FOR UPDATE locks first.
        'SELECT id FROM t WHERE a = 1 OR rownum <= 1',
        'SELECT id FROM t WHERE rownum <= 1 FOR UPDATE',
        # not a `<op> n` comparison the LIMIT models
        'SELECT id FROM t WHERE rownum > 1',
        'SELECT id FROM t WHERE rownum BETWEEN 1 AND 5',
        'SELECT id FROM t WHERE rownum <= f(x)',
        # ROWNUM in the select list, or more than one ROWNUM
        'SELECT id, rownum FROM t',
        'SELECT id FROM t WHERE rownum <= 1 AND rownum <= 2',
        # a nested ROWNUM is refused, not rewritten inside the sub-query
        'SELECT * FROM (SELECT id FROM t WHERE rownum <= 3) x',
        'SELECT * FROM (SELECT id FROM t WHERE rownum <= 3) x WHERE y > 1',
        # not a top-level SELECT
        'UPDATE t SET x = 1 WHERE rownum <= 1',
    ],
)
def test_rownum_without_a_faithful_limit_is_unsupported(sql: str) -> None:
    with pytest.raises(Unsupported):
        rewrite_rownum(sql)
