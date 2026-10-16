# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Comparing two migration profiles (#1882): the join by fingerprint, the
ranking by time lost, where the loss is, and the text and HTML reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from seerdb.server.launcher import main
from seerdb.server.profile import Profile
from seerdb.server.profile_compare import compare, html_report, load, text_report


def _recorded(path: Path, description: str, calls: list[tuple[Any, ...]]) -> Path:
    # A profile as --profile writes it: (sql, times, seconds per call, and
    # over PostgreSQL the seconds waiting on it per call, or None).
    profile = Profile(str(path), description)
    for sql, times, seconds, waited, *error in calls:
        for _ in range(times):
            profile.record(
                sql,
                'execute',
                seconds,
                rows_in=1,
                rows_out=1,
                error=error[0] if error else None,
                backend_statements=None if waited is None else 2,
                backend_seconds=waited,
                translated=None,
            )
    profile.dump()
    return path


@pytest.fixture
def profiles(tmp_path: Path) -> tuple[Path, Path]:
    oracle = _recorded(
        tmp_path / 'oracle.json',
        'passthrough to db:1521/FREEPDB1',
        [
            ('SELECT a FROM t WHERE id = 1', 1000, 0.001, None),
            ('SELECT b FROM u WHERE x < 5', 2, 0.001, None),
            ('UPDATE v SET n = 1', 10, 0.002, None),
            ('SELECT * FROM only_oracle', 3, 0.001, None),
        ],
    )
    postgres = _recorded(
        tmp_path / 'postgres.json',
        'PostgreSQL presenting 23ai',
        [
            # Twice as slow, a thousand calls: 1000 ms lost, and the Mirror's.
            ('SELECT a FROM t WHERE id = :1', 1000, 0.002, 0.0005),
            # Fifty times as slow, twice: 98 ms, and PostgreSQL's.
            ('select b from u where x < 7', 2, 0.050, 0.045),
            # Faster.
            ('UPDATE v SET n = 2', 10, 0.001, 0.0008),
            ('SELECT * FROM only_pg', 1, 0.001, None, 942),
        ],
    )
    return (oracle, postgres)


def test_statements_rank_by_the_time_lost_not_the_ratio(
    profiles: tuple[Path, Path],
) -> None:
    comparison = compare(load(str(profiles[0])), load(str(profiles[1])))
    (hot, rare, faster) = comparison.compared
    assert hot.fingerprint == 'SELECT A FROM T WHERE ID=?'
    assert (hot.lost_ms, hot.ratio) == pytest.approx((1000, 2))
    assert rare.fingerprint == 'SELECT B FROM U WHERE X<?'
    assert (rare.lost_ms, rare.ratio) == pytest.approx((98, 50))
    assert faster.lost_ms == pytest.approx(-10)
    assert comparison.lost_ms == pytest.approx(1098)
    assert comparison.won_ms == pytest.approx(10)
    # Where each loss is: the Mirror's own time, or PostgreSQL's beyond
    # Oracle's whole call. A statement that got faster needs no fix.
    assert hot.split == pytest.approx((2, 0.5, 1.5))
    assert (hot.fix, rare.fix, faster.fix) == ('Mirror', 'SQL', '')
    # What only one run saw is kept apart, not dropped.
    assert [s['fingerprint'] for s in comparison.only_baseline] == [
        'SELECT * FROM ONLY_ORACLE'
    ]
    assert [s['errors'] for s in comparison.only_candidate] == [{'ORA-00942': 1}]


def test_the_reports_say_both_runs_and_escape_the_sql(
    profiles: tuple[Path, Path],
) -> None:
    comparison = compare(load(str(profiles[0])), load(str(profiles[1])))
    text = text_report(comparison, top=1)
    assert 'baseline:  passthrough to db:1521/FREEPDB1: 0 sessions, 4 statements' in (
        text
    )
    assert 'By time lost (top 1):' in text
    assert 'SELECT A FROM T WHERE ID=?' in text
    assert 'SELECT B FROM U' not in text
    assert 'SELECT * FROM ONLY_PG  [ORA-00942 x1]' in text
    assert 'similar hardware' in text
    page = html_report(comparison)
    assert page.startswith('<!doctype html>')
    assert 'SELECT B FROM U WHERE X&lt;?' in page
    assert 'x < ' not in page
    assert 'x &lt; 5' in page and 'x &lt; 7' in page
    assert '<script' not in page


def test_seerdb_mirror_profile_compare(
    profiles: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / 'report.html'
    main(
        [
            'profile',
            'compare',
            str(profiles[0]),
            str(profiles[1]),
            '--html',
            str(report),
        ]
    )
    assert 'net 1,088 ms' in capsys.readouterr().out
    assert 'Migration profile comparison' in report.read_text()
    (tmp_path / 'other.json').write_text('{"format": "something else"}')
    with pytest.raises(SystemExit, match='is not a Mirror profile'):
        main(['profile', 'compare', str(profiles[0]), str(tmp_path / 'other.json')])
