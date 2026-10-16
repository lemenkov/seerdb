# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Compare two migration profiles (#1882).

The second stage of the migration profiler: the recordings of one application
run twice (#1881) -- once against Oracle through the passthrough Mirror, once
against the Mirror over PostgreSQL -- joined statement by statement::

    seerdb-mirror profile compare oracle.json postgres.json --html report.html

The first profile is the baseline, the second the candidate. Statements are
ranked by the time the candidate loses on them, not by how many times slower
it is: twice as slow on a statement called a million times outranks fifty
times on one called twice. A statement that only one run saw is listed apart.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

FORMAT = 'seerdb-mirror-profile/1'

CAVEAT = (
    'Two runs compare only when they ran on similar hardware, network and '
    'data: the same application, the same work, and databases of about the '
    'same size. The run details above say what the recordings can: how long '
    'each ran, the sessions, and the rows the statements read and wrote.'
)


@dataclass
class Run:
    """One recorded profile."""

    path: str
    backend: str
    seconds: float
    sessions: int
    profiler_seconds: float
    statements: dict[str, dict[str, Any]]

    @property
    def calls(self) -> int:
        return sum(s['calls'] for s in self.statements.values())

    @property
    def total_ms(self) -> float:
        return sum(s['total_ms'] for s in self.statements.values())

    @property
    def rows_in(self) -> int:
        return sum(s['rows_in'] for s in self.statements.values())

    @property
    def rows_out(self) -> int:
        return sum(s['rows_out'] for s in self.statements.values())


def load(path: str) -> Run:
    """The profile written at ``path`` by ``--profile``."""
    try:
        with open(path, encoding='utf-8') as source:
            data = json.load(source)
    except (OSError, ValueError) as exc:
        raise SystemExit(f'cannot read the profile {path}: {exc}') from None
    if not isinstance(data, dict) or data.get('format') != FORMAT:
        raise SystemExit(f'{path} is not a Mirror profile ({FORMAT})')
    return Run(
        path=path,
        backend=data.get('backend') or path,
        seconds=max(data.get('written', 0.0) - data.get('started', 0.0), 0.0),
        sessions=data.get('sessions_ended', 0),
        profiler_seconds=data.get('profiler_seconds', 0.0),
        statements={s['fingerprint']: s for s in data['statements']},
    )


@dataclass
class Compared:
    """One statement both runs saw."""

    fingerprint: str
    baseline: dict[str, Any]
    candidate: dict[str, Any]

    @property
    def lost_ms(self) -> float:
        # Per call, then times the candidate's calls: a run that did a little
        # more or less work is still compared call for call.
        return (self.candidate['mean_ms'] - self.baseline['mean_ms']) * self.candidate[
            'calls'
        ]

    @property
    def ratio(self) -> float:
        base = self.baseline['mean_ms']
        return self.candidate['mean_ms'] / base if base > 0 else math.inf

    @property
    def split(self) -> tuple[float, float, float] | None:
        """Over PostgreSQL: the statements sent PostgreSQL per call, and a
        call's milliseconds waiting on PostgreSQL and the Mirror's own."""
        backend = self.candidate.get('backend')
        if not backend:
            return None
        calls = self.candidate['calls']
        return (
            backend['statements_per_call'],
            backend['database_ms'] / calls,
            backend['mirror_ms'] / calls,
        )

    @property
    def fix(self) -> str:
        """Where the lost time is: in the Mirror's emulation (its own time),
        or in the SQL (what PostgreSQL took beyond Oracle's whole call)."""
        split = self.split
        if split is None or self.lost_ms <= 0:
            return ''
        (_, database, mirror) = split
        return 'Mirror' if mirror >= database - self.baseline['mean_ms'] else 'SQL'


@dataclass
class Comparison:
    baseline: Run
    candidate: Run
    compared: list[Compared]
    only_baseline: list[dict[str, Any]]
    only_candidate: list[dict[str, Any]]

    @property
    def lost_ms(self) -> float:
        return sum(c.lost_ms for c in self.compared if c.lost_ms > 0)

    @property
    def won_ms(self) -> float:
        return -sum(c.lost_ms for c in self.compared if c.lost_ms < 0)


def compare(baseline: Run, candidate: Run) -> Comparison:
    """``candidate`` against ``baseline``, statement by statement."""
    compared = [
        Compared(key, entry, candidate.statements[key])
        for key, entry in baseline.statements.items()
        if key in candidate.statements
    ]
    compared.sort(key=lambda c: -c.lost_ms)

    def apart(run: Run, other: Run) -> list[dict[str, Any]]:
        alone = [s for k, s in run.statements.items() if k not in other.statements]
        return sorted(alone, key=lambda s: -s['total_ms'])

    return Comparison(
        baseline,
        candidate,
        compared,
        apart(baseline, candidate),
        apart(candidate, baseline),
    )


def _ms(value: float) -> str:
    return f'{value:,.0f}' if abs(value) >= 100 else f'{value:,.2f}'


def _ratio(value: float) -> str:
    return 'new' if math.isinf(value) else f'{value:.2f}x'


def _errors(entry: dict[str, Any]) -> str:
    return ', '.join(f'{code} x{n}' for code, n in sorted(entry['errors'].items()))


def _run_line(run: Run) -> str:
    return (
        f'{run.backend}: {run.sessions:,} sessions, {len(run.statements):,} '
        f'statements, {run.calls:,} calls, {run.total_ms / 1000:,.1f} s in '
        f'statements over {run.seconds:,.1f} s, rows {run.rows_in:,} in and '
        f'{run.rows_out:,} out'
    )


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + '...'


def text_report(comparison: Comparison, top: int = 20) -> str:
    """The comparison as a plain-text summary for a terminal: the ``top``
    statements by time lost, and those only one run saw."""
    c = comparison
    lines = [
        f'baseline:  {_run_line(c.baseline)}',
        f'candidate: {_run_line(c.candidate)}',
        '',
        f'{len(c.compared):,} statements in both runs: '
        f'{_ms(c.lost_ms)} ms lost, {_ms(c.won_ms)} ms won back, '
        f'net {_ms(c.lost_ms - c.won_ms)} ms',
    ]
    if c.compared:
        lines += [
            '',
            f'By time lost (top {min(top, len(c.compared))}):',
            f'{"lost ms":>10} {"ratio":>7} {"calls":>8} {"base ms":>9} '
            f'{"cand ms":>9} {"stmts":>5} {"fix":<6} statement',
        ]
        for row in c.compared[:top]:
            split = row.split
            lines.append(
                f'{_ms(row.lost_ms):>10} {_ratio(row.ratio):>7} '
                f'{row.candidate["calls"]:>8,} {_ms(row.baseline["mean_ms"]):>9} '
                f'{_ms(row.candidate["mean_ms"]):>9} '
                f'{(f"{split[0]:.1f}" if split else ""):>5} {row.fix:<6} '
                f'{_clip(row.fingerprint, 70)}'
            )
    for title, alone in (
        ('Only in the baseline', c.only_baseline),
        ('Only in the candidate', c.only_candidate),
    ):
        if alone:
            lines += ['', f'{title} ({len(alone):,}):']
            for entry in alone[:top]:
                errors = _errors(entry)
                lines.append(
                    f'{_ms(entry["total_ms"]):>10} ms {entry["calls"]:>8,} calls  '
                    f'{_clip(entry["fingerprint"], 70)}'
                    + (f'  [{errors}]' if errors else '')
                )
    lines += ['', CAVEAT]
    return '\n'.join(lines) + '\n'


_STYLE = """
:root { --bg: #ffffff; --fg: #1d1d1f; --muted: #6e6e73; --line: #d2d2d7;
  --head: #f5f5f7; --lost: #b3261e; --won: #1b7f3b; --note: #fff8e1; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #161618; --fg: #ececf0; --muted: #a1a1a6; --line: #3a3a3c;
  --head: #222225; --lost: #ff8a80; --won: #7ee2a0; --note: #2d2816; } }
:root[data-theme="dark"] { --bg: #161618; --fg: #ececf0; --muted: #a1a1a6;
  --line: #3a3a3c; --head: #222225; --lost: #ff8a80; --won: #7ee2a0;
  --note: #2d2816; }
body { background: var(--bg); color: var(--fg); margin: 0 auto; padding: 16px;
  max-width: 1400px; font: 14px/1.45 system-ui, sans-serif; }
h1 { font-size: 1.4em; } h2 { font-size: 1.15em; margin-top: 2em; }
.note { background: var(--note); border: 1px solid var(--line); padding: 8px 12px; }
.wide { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { border-bottom: 1px solid var(--line); padding: 4px 8px; text-align: right;
  vertical-align: top; white-space: nowrap; }
th { background: var(--head); position: sticky; top: 0; }
td.sql, th.sql { text-align: left; white-space: normal; }
code, pre { font: 12px/1.4 ui-monospace, monospace; }
pre { white-space: pre-wrap; margin: 4px 0; color: var(--muted); }
.lost { color: var(--lost); } .won { color: var(--won); }
.muted { color: var(--muted); }
"""


def _sql_cell(fingerprint: str, *entries: dict[str, Any]) -> str:
    # The fingerprint, and each run's example of it as its client sent it.
    examples = dict.fromkeys(
        e['example'] for e in entries if e.get('example') not in (None, fingerprint)
    )
    body = f'<code>{html.escape(fingerprint)}</code>'
    if examples:
        shown = ''.join(f'<pre>{html.escape(text)}</pre>' for text in examples)
        body = f'<details><summary>{body}</summary>{shown}</details>'
    return f'<td class="sql">{body}</td>'


def html_report(comparison: Comparison) -> str:
    """The comparison as one self-contained HTML page."""
    c = comparison
    e = html.escape
    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>Migration profile comparison</title>',
        f'<style>{_STYLE}</style>',
        '</head><body><h1>Migration profile comparison</h1>',
        '<table><tr><th></th><th class="sql">recording</th><th>sessions</th>'
        '<th>statements</th><th>calls</th><th>s in statements</th>'
        '<th>s recorded</th><th>rows in</th><th>rows out</th></tr>',
    ]
    for name, run in (('baseline', c.baseline), ('candidate', c.candidate)):
        out.append(
            f'<tr><th>{name}</th><td class="sql">{e(run.backend)}<br>'
            f'<span class="muted">{e(run.path)}</span></td>'
            f'<td>{run.sessions:,}</td><td>{len(run.statements):,}</td>'
            f'<td>{run.calls:,}</td><td>{run.total_ms / 1000:,.1f}</td>'
            f'<td>{run.seconds:,.1f}</td><td>{run.rows_in:,}</td>'
            f'<td>{run.rows_out:,}</td></tr>'
        )
    out += [
        '</table>',
        f'<p class="note">{e(CAVEAT)}</p>',
        f'<p>{len(c.compared):,} statements in both runs: '
        f'<span class="lost">{_ms(c.lost_ms)} ms lost</span>, '
        f'<span class="won">{_ms(c.won_ms)} ms won back</span>, '
        f'net {_ms(c.lost_ms - c.won_ms)} ms.</p>',
        '<h2>Statements in both runs, by time lost</h2>',
        '<p class="muted">Time lost is the difference in mean time per call, '
        "times the candidate's calls. Over PostgreSQL a call's time splits "
        "into waiting on PostgreSQL and the Mirror's own; <em>fix</em> says "
        "which holds more of the loss: the Mirror's emulation, or the SQL "
        "(PostgreSQL's time beyond Oracle's whole call).</p>",
        '<div class="wide"><table><tr><th>lost ms</th><th>ratio</th>'
        '<th>calls<br>base / cand</th><th>mean ms<br>base / cand</th>'
        '<th>total ms<br>base / cand</th><th>p95 ms<br>base / cand</th>'
        '<th>PG stmts<br>per call</th><th>PG ms<br>per call</th>'
        '<th>Mirror ms<br>per call</th><th>fix</th><th>errors<br>base / cand</th>'
        '<th class="sql">statement</th></tr>',
    ]
    for row in c.compared:
        (b, k) = (row.baseline, row.candidate)
        split = row.split
        tone = 'lost' if row.lost_ms > 0 else 'won' if row.lost_ms < 0 else ''
        cells = (
            [f'{split[0]:.1f}', _ms(split[1]), _ms(split[2])] if split else ['', '', '']
        )
        errors = ' / '.join(_errors(s) or '-' for s in (b, k))
        out.append(
            f'<tr><td class="{tone}">{_ms(row.lost_ms)}</td>'
            f'<td>{_ratio(row.ratio)}</td>'
            f'<td>{b["calls"]:,} / {k["calls"]:,}</td>'
            f'<td>{_ms(b["mean_ms"])} / {_ms(k["mean_ms"])}</td>'
            f'<td>{_ms(b["total_ms"])} / {_ms(k["total_ms"])}</td>'
            f'<td>{_ms(b["p95_ms"])} / {_ms(k["p95_ms"])}</td>'
            + ''.join(f'<td>{cell}</td>' for cell in cells)
            + f'<td>{row.fix}</td>'
            f'<td>{e(errors) if errors != "- / -" else ""}</td>'
            + _sql_cell(row.fingerprint, b, k)
            + '</tr>'
        )
    out.append('</table></div>')
    for title, alone in (
        ('Only in the baseline', c.only_baseline),
        ('Only in the candidate', c.only_candidate),
    ):
        if not alone:
            continue
        out += [
            f'<h2>{title}</h2><div class="wide"><table><tr><th>total ms</th>'
            '<th>calls</th><th>mean ms</th><th>errors</th>'
            '<th class="sql">statement</th></tr>',
        ]
        for entry in alone:
            out.append(
                f'<tr><td>{_ms(entry["total_ms"])}</td><td>{entry["calls"]:,}</td>'
                f'<td>{_ms(entry["mean_ms"])}</td><td>{e(_errors(entry))}</td>'
                + _sql_cell(entry['fingerprint'], entry)
                + '</tr>'
            )
        out.append('</table></div>')
    out.append('</body></html>\n')
    return '\n'.join(out)


def main(argv: Sequence[str] | None = None) -> None:
    """``seerdb-mirror profile compare BASELINE CANDIDATE``."""
    parser = argparse.ArgumentParser(
        prog='seerdb-mirror profile', description='Work with migration profiles.'
    )
    commands = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)
    comparing = commands.add_parser(
        'compare',
        help='compare a profile against a baseline, statement by statement',
    )
    comparing.add_argument('baseline', help='the profile of the run against Oracle')
    comparing.add_argument('candidate', help='the profile of the run over PostgreSQL')
    comparing.add_argument('--html', metavar='FILE', help='write an HTML report')
    comparing.add_argument(
        '--top',
        type=int,
        default=20,
        help='statements listed in the summary (default 20)',
    )
    args = parser.parse_args(argv)
    comparison = compare(load(args.baseline), load(args.candidate))
    print(text_report(comparison, args.top), end='')
    if args.html:
        with open(args.html, 'w', encoding='utf-8') as out:
            out.write(html_report(comparison))
