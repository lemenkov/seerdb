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

# Individual tests that cannot pass through a Mirror **by construction**, in
# files that otherwise do. Unlike DENYLIST above these are not whole features,
# so each entry says exactly why it can never go green rather than what has not
# been built yet -- something merely unimplemented belongs in a ticket and stays
# red until it is fixed.
#
# Keep this short. Every line here is a test whose signal is being given up.
DENYLIST_TESTS: dict[str, str] = {}

for _test, _reason in (
    # A passthrough measures the UPSTREAM session, not the client's. The Mirror
    # holds one upstream connection per client session and issues its own
    # statements on it (type lookups, the describe fallback), so a round-trip or
    # parse count read from v$mystat/v$sesstat counts the Mirror's work as well
    # as the client's. There is no arrangement of the relay that makes these
    # agree: the number the test asserts is a property of a DIRECT connection.
    ('test_4300_cursor_other.py::test_4322', 'v$mystat round-trip count'),
    ('test_4300_cursor_other.py::test_4323', 'v$mystat round-trip count'),
    ('test_4300_cursor_other.py::test_4333', 'v$mystat parse count'),
    ('test_6300_cursor_other_async.py::test_6315', 'v$mystat round-trip count'),
    ('test_6300_cursor_other_async.py::test_6316', 'v$mystat round-trip count'),
    ('test_6300_cursor_other_async.py::test_6322', 'v$mystat parse count'),
    # Same root cause: the count comes from v$temporary_lobs for the session id
    # the CLIENT sees, which is the upstream session the Mirror shares with its
    # own temp-LOB bookkeeping.
    ('test_1900_lob_var.py::test_1918', 'v$temporary_lobs on a shared session'),
    ('test_5700_lob_var_async.py::test_5715', 'v$temporary_lobs on a shared session'),
    # The bed's server is NEWER than the suite's expectation and raises a
    # different (also correct) code; the Mirror relays it faithfully. Both fail
    # DIRECTLY against the same server too, which is how they are told apart
    # from a relay gap -- verified by running the whole suite direct: 2 failed
    # out of 2134, and these are the two.
    (
        'test_6400_vector_var.py::test_6430',
        'server raises ORA-51807, suite wants 51805',
    ),
    (
        'test_7700_sparse_vector.py::test_7734',
        'server raises a newer vector error code',
    ),
):
    DENYLIST_TESTS[_test] = _reason


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
    for test in DENYLIST_TESTS:
        cmd += ['--deselect', f'tests/{test}']
    cmd += args.pytest_args

    print(f'# {len(DENYLIST)} files denylisted (out-of-scope features):')
    for filename, reason in sorted(DENYLIST.items()):
        print(f'#   {filename:<42} {reason}')
    print(f'# {len(DENYLIST_TESTS)} individual tests deselected (cannot pass here):')
    for test, reason in sorted(DENYLIST_TESTS.items()):
        print(f'#   {test:<52} {reason}')
    print(f'# running in {args.suite} against {args.dsn}\n', flush=True)
    return subprocess.call(cmd, cwd=args.suite, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
