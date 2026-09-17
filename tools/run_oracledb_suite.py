#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Run python-oracledb's own test suite against a passthrough Mirror (#826).

This is the independent-client leg of the Mirror conformance matrix: the same
suite an Oracle client's author runs against a real server, pointed at a Mirror
that relays to a real 23ai. Because a second implementation writes and reads the
bytes, a shared protocol misunderstanding shows up as a failure rather than
passing silently -- which is exactly how the OSON and VECTOR gaps were found.

The suite lives outside this repo (python-oracledb's source tree, pinned to the
version whose expectations you want to match). Launch a passthrough Mirror over
a real 23ai first::

    MIRROR_USERS=pythontest:pythontest,pythontestproxy:pythontestproxy,pyoadm:pyoadm123 \\
        python examples/mirror_over_oracle.py 1600 192.168.0.150:1523/FREEPDB1

then::

    tools/run_oracledb_suite.py --suite ~/work/python-oracledb \\
        --dsn localhost:1600/FREEPDB1

The pass/fail this prints is an *honest* Mirror-conformance number: the DENYLIST
below excludes whole test files for features the Mirror does not implement by
design, so the total is not drowned out by them. It is a denylist of
out-of-scope features, not a curated allowlist -- everything else runs. Two
other categories still inflate the residual and are NOT denylisted here because
they are per-test rather than per-feature: a handful of expectations pinned to
an older server version (the Mirror faithfully relays this server's real code),
and full-suite-only cascade artifacts that pass in isolation. Read the number as
a trend against a fixed suite version, not as an absolute.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Whole test files for features the Mirror does not implement by design. Each is
# a server-side or client-side capability outside a query/DML relay's job, so
# the file is out of scope in full -- not an error-mapping or fidelity gap.
DENYLIST: dict[str, str] = {
    # Advanced Queuing: a message-broker subsystem, no Mirror surface.
    'test_2700_aq_dbobject.py': 'Advanced Queuing (AQ)',
    'test_2800_aq_bulk.py': 'Advanced Queuing (AQ)',
    'test_7800_aq_raw.py': 'Advanced Queuing (AQ)',
    'test_7900_aq_raw_async.py': 'Advanced Queuing (AQ)',
    'test_8200_aq_bulk_async.py': 'Advanced Queuing (AQ)',
    'test_8300_aq_json.py': 'Advanced Queuing (AQ)',
    'test_8400_aq_dbobject_async.py': 'Advanced Queuing (AQ)',
    'test_8500_aq_json_async.py': 'Advanced Queuing (AQ)',
    # Continuous Query Notification: needs a server->client callback channel.
    'test_3000_subscription.py': 'Continuous Query Notification (subscriptions)',
    # SODA: a document-store API layered on DBMS_SODA, not a wire relay concern.
    'test_3300_soda_database.py': 'SODA document store',
    'test_3400_soda_collection.py': 'SODA document store',
    # Two-phase commit / XA: distributed-transaction coordination.
    'test_4400_tpc.py': 'Two-phase commit / XA',
    'test_7400_tpc_async.py': 'Two-phase commit / XA',
    # External / OS authentication: no password for the Mirror to verify.
    'test_5000_externalauth.py': 'External / OS authentication',
    # Sessionless transactions: a 23ai transaction-portability feature.
    'test_8700_sessionless_transaction.py': 'Sessionless transactions',
    'test_8800_sessionless_transaction_async.py': 'Sessionless transactions',
    # Direct path load: a dedicated bulk-load protocol, not ordinary DML.
    'test_9600_direct_path_load.py': 'Direct path load',
    'test_9700_direct_path_load_async.py': 'Direct path load',
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        '--suite', required=True, help='python-oracledb checkout (its tests/)'
    )
    ap.add_argument('--dsn', default='localhost:1600/FREEPDB1', help='Mirror DSN')
    ap.add_argument('--user', default='pythontest')
    ap.add_argument('--password', default='pythontest')
    ap.add_argument('--admin-user', default='pyoadm')
    ap.add_argument('--admin-password', default='pyoadm123')
    ap.add_argument('--proxy-user', default='pythontestproxy')
    ap.add_argument('--proxy-password', default='pythontestproxy')
    ap.add_argument('pytest_args', nargs='*', help='extra args passed to pytest')
    args = ap.parse_args()

    env = dict(os.environ)
    env.update(
        PYO_TEST_MAIN_USER=args.user,
        PYO_TEST_MAIN_PASSWORD=args.password,
        PYO_TEST_ADMIN_USER=args.admin_user,
        PYO_TEST_ADMIN_PASSWORD=args.admin_password,
        PYO_TEST_PROXY_USER=args.proxy_user,
        PYO_TEST_PROXY_PASSWORD=args.proxy_password,
        PYO_TEST_CONNECT_STRING=args.dsn,
    )

    cmd = [
        sys.executable,
        '-m',
        'pytest',
        'tests',
        '-q',
        '-p',
        'no:randomly',
        '--tb=line',
        # A per-test signal timeout segfaults the C extension, so bound the whole
        # run at the OS level instead; `ext` holds the one known statement-cache
        # hang plus config-provider features that need external cloud services.
        '--ignore=tests/ext',
    ]
    for filename in DENYLIST:
        cmd += ['--ignore', f'tests/{filename}']
    cmd += args.pytest_args

    print(f'# {len(DENYLIST)} files denylisted (out-of-scope features):')
    for filename, reason in sorted(DENYLIST.items()):
        print(f'#   {filename:<42} {reason}')
    print(f'# running in {args.suite} against {args.dsn}\n', flush=True)
    return subprocess.call(cmd, cwd=args.suite, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
