#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Run sqlplus through a corpus of everyday work and compare targets (#2022).

The Mirror speaks the OCI (thick) dialect to sqlplus, and what sqlplus does with
a reply is visible only in what it prints. This runs the corpus in
tools/sqlplus_survey.sql through one target and keeps each section's output;
``compare`` then sets two targets side by side, section by section.

Run it three ways and compare them in pairs:

- **direct** to a real Oracle (23ai);
- through a **passthrough** Mirror relaying to that Oracle. A section that
  prints differently here than direct is a gap in the Mirror's OCI dialect
  itself, since the database behind it is the real one;
- through a **Mirror-over-PG**. A difference only here is a backend gap.

    python tools/sqlplus_survey.py run direct pyo/pyo123@//192.168.0.150:1523/FREEPDB1
    python -m seerdb.server --port 1610 passthrough 192.168.0.150:1523/FREEPDB1 &
    python tools/sqlplus_survey.py run passthrough pyo/pyo123@//127.0.0.1:1610/FREEPDB1
    python tools/sqlplus_survey.py compare direct passthrough

Each section runs in a sqlplus session of its own, so a crash or a hang (the
exit status is recorded, 139 for SIGSEGV, 124 for the timeout) costs that
section and not the rest. sqlplus is Oracle's binary, so this is a local leg,
not a CI job; ``--sqlplus`` or ``$SQLPLUS`` names it, and its directory is put
on ``LD_LIBRARY_PATH`` as an Instant Client needs.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import pathlib
import re
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_CORPUS = _HERE / 'sqlplus_survey.sql'
_SECTION = re.compile(r'(?m)^(?=prompt ### )')
_SECTION_ID = re.compile(r'prompt ### (\d+)')

# What differs between any two runs whatever the target: the banners, the
# connect and last-login lines, timings, and the generated constraint names.
_NOISE = re.compile(
    r'^(Elapsed|Connected|SQL\*Plus: Release|Copyright|Version|Last Successful'
    r'|Oracle (Database|AI Database)|Disconnected)|SYS_C0'
)


def sections(text: str) -> dict[str, str]:
    """The corpus split into runnable scripts, by section number: each is the
    header (the lines before the first section) followed by its own lines and
    an ``exit``."""
    (head, *parts) = _SECTION.split(text)
    out = {}
    for part in parts:
        match = _SECTION_ID.match(part)
        assert match is not None
        body = re.sub(r'(?m)^exit\s*$', '', part)
        out[match.group(1)] = f'{head}{body.rstrip()}\nexit\n'
    return out


def significant(output: str) -> list[str]:
    """``output``'s lines with the run-to-run noise left out."""
    return [line for line in output.splitlines() if not _NOISE.search(line)]


def run(
    label: str, connect: str, sqlplus: str, out: pathlib.Path, timeout: int
) -> None:
    target = out / label
    target.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = os.path.dirname(sqlplus)
    statuses = {}
    for number, script in sections(_CORPUS.read_text()).items():
        path = target / f'sec{number}.sql'
        path.write_text(script)
        try:
            done = subprocess.run(
                [sqlplus, '-L', connect, f'@{path}'],
                capture_output=True,
                text=True,
                errors='replace',
                env=env,
                timeout=timeout,
            )
            (status, output) = (done.returncode, done.stdout + done.stderr)
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout if isinstance(exc.stdout, str) else ''
            status = 124
        if status < 0:
            status = 128 - status  # killed by a signal, as a shell reports it
        (target / f'sec{number}.txt').write_text(output)
        statuses[number] = status
        print(f'{label} section {number}: exit {status}')
    (target / 'exits.json').write_text(json.dumps(statuses, indent=1))


def compare(a: pathlib.Path, b: pathlib.Path, show: bool) -> int:
    """Print each section as the same or differing between targets ``a`` and
    ``b``; the number of differing sections."""
    exits_b = json.loads((b / 'exits.json').read_text())
    differing = 0
    for path in sorted(a.glob('sec*.txt')):
        other = b / path.name
        number = path.stem[3:]
        left = significant(path.read_text())
        right = significant(other.read_text()) if other.exists() else []
        if left == right:
            print(f'section {number}: same')
            continue
        differing += 1
        print(f'section {number}: DIFFERS (exit {exits_b.get(number)} in {b.name})')
        if show:
            sys.stdout.writelines(
                line + '\n'
                for line in difflib.unified_diff(
                    left, right, a.name, b.name, n=1, lineterm=''
                )
            )
    return differing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--out', type=pathlib.Path, default=pathlib.Path('sqlplus-survey'))
    sub = ap.add_subparsers(dest='command', required=True)
    r = sub.add_parser('run', help='run the corpus through one target')
    r.add_argument('label')
    r.add_argument(
        'connect', help='a sqlplus connect string, user/password@//host:port/service'
    )
    r.add_argument('--sqlplus', default=os.environ.get('SQLPLUS', 'sqlplus'))
    r.add_argument('--timeout', type=int, default=60, help='seconds per section')
    c = sub.add_parser('compare', help='compare two targets section by section')
    c.add_argument('a')
    c.add_argument('b')
    c.add_argument('--diff', action='store_true', help='show each difference')
    args = ap.parse_args()
    if args.command == 'run':
        run(args.label, args.connect, args.sqlplus, args.out, args.timeout)
        return 0
    return 1 if compare(args.out / args.a, args.out / args.b, args.diff) else 0


if __name__ == '__main__':
    sys.exit(main())
