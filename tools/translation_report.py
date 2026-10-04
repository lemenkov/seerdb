#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Summarise the Mirror-over-PostgreSQL translation report (#1557).

With ``SEERDB_TRANSLATION_REPORT=1`` (or ``ALTER SESSION SET
seerdb_translation_report = true``), the PostgreSQL backend records in
``sys.ora_translation_log`` what each statement an application sent needed
translated from Oracle. This prints the migration's to-do list from it::

    python3 tools/translation_report.py 'host=... dbname=mirror' [--top N]

* the statements that still need translating, ranked by runs times the number of
  rules they need, each with its rules;
* the runs per rule across the application;
* how much of the work is already portable, or sent as native PostgreSQL.

A statement that needed no rule runs on PostgreSQL as written: it is done.
"""

from __future__ import annotations

import argparse
import sys

import psycopg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        'conninfo', help='libpq connection string of the backing database'
    )
    parser.add_argument('--top', type=int, default=25, help='statements to list')
    args = parser.parse_args(argv)
    with psycopg.connect(args.conninfo) as conn:
        rows = conn.execute(
            'SELECT statement, rules, runs, failures, native, last_error '
            'FROM sys.ora_translation_log'
        ).fetchall()
    if not rows:
        print('The translation report is empty.')
        return 0
    total = sum(r[2] for r in rows)
    native = sum(r[2] for r in rows if r[4])
    todo = [r for r in rows if r[1] and not r[4]]
    portable = total - native - sum(r[2] for r in todo)
    print(
        f'{len(rows)} statements, {total} runs: '
        f'{sum(r[2] for r in todo)} still translated ({len(todo)} statements), '
        f'{portable} portable, {native} native'
    )
    print(f'\nStill translated, by runs x rules (top {args.top}):')
    for statement, rules, runs, failures, _native, _error in sorted(
        todo, key=lambda r: (-r[2] * len(r[1]), r[0])
    )[: args.top]:
        failed = f', {failures} failed' if failures else ''
        print(f'  {runs:>7} runs{failed}  [{", ".join(rules)}]')
        print(f'          {statement[:160]}')
    per_rule: dict[str, int] = {}
    for _statement, rules, runs, _f, _native, _error in todo:
        for rule in rules:
            per_rule[rule] = per_rule.get(rule, 0) + runs
    print('\nRuns per rule:')
    for rule, runs in sorted(per_rule.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f'  {runs:>7}  {rule}')
    failing = [r for r in rows if r[3]]
    if failing:
        print(f'\nFailing statements ({len(failing)}):')
        for statement, _rules, _runs, failures, _native, error in sorted(
            failing, key=lambda r: -r[3]
        )[: args.top]:
            print(f'  {failures:>7}x  {statement[:100]}')
            print(f'          {error}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
