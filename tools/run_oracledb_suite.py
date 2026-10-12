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
    'aq': 'Advanced Queuing (AQ)',
    # SODA: a document-store API layered on DBMS_SODA, not a wire relay concern.
    'soda': 'SODA document store',
    # Continuous Query Notification: needs a server->client callback channel.
    'connection/test_1800_subscription.py': 'Continuous Query Notification',
    # Two-phase commit / XA: distributed-transaction coordination.
    'connection/test_1400_tpc.py': 'Two-phase commit / XA',
    'connection/test_1500_tpc_async.py': 'Two-phase commit / XA',
    # External / OS authentication: no password for the Mirror to verify.
    'connection/test_1900_externalauth.py': 'External / OS authentication',
    # Sessionless transactions: a 23ai transaction-portability feature.
    'connection/test_1600_sessionless_transaction.py': 'Sessionless transactions',
    'connection/test_1700_sessionless_transaction_async.py': 'Sessionless transactions',
    # Direct path load: a dedicated bulk-load protocol, not ordinary DML.
    'connection/test_2000_direct_path_load.py': 'Direct path load',
    'connection/test_2100_direct_path_load_async.py': 'Direct path load',
}

# Individual tests that cannot pass through a Mirror **by construction**, in
# files that otherwise do. Unlike DENYLIST above these are not whole features,
# so each entry says exactly why it can never go green rather than what has not
# been built yet -- something merely unimplemented belongs in a ticket, and in
# OPEN_TICKET_TESTS below until it is fixed.
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
    ('cursor/test_1800_other.py::test_cursor_1822', 'v$mystat round-trip count'),
    ('cursor/test_1800_other.py::test_cursor_1823', 'v$mystat round-trip count'),
    ('cursor/test_1800_other.py::test_cursor_1833', 'v$mystat parse count'),
    ('cursor/test_1900_other_async.py::test_cursor_1915', 'v$mystat round-trip count'),
    ('cursor/test_1900_other_async.py::test_cursor_1916', 'v$mystat round-trip count'),
    ('cursor/test_1900_other_async.py::test_cursor_1922', 'v$mystat parse count'),
    # The same thing through the suite's own `round_trip_checker` fixture, which
    # reads the counter before and after and asserts the difference.
    (
        'data_types/test_1000_cursor.py::test_data_types_1006',
        'round-trip count around a REF cursor',
    ),
    (
        'data_types/test_2600_cursor_async.py::test_data_types_2606',
        'round-trip count around a REF cursor',
    ),
    (
        'connection/test_1300_shortcut_methods_async.py::test_connection_1302',
        'round-trip count around fetchall(arraysize)',
    ),
    # Same root cause: the count comes from v$temporary_lobs for the session id
    # the CLIENT sees, which is the upstream session the Mirror shares with its
    # own temp-LOB bookkeeping.
    (
        'data_types/test_1500_lobs.py::test_data_types_1518',
        'v$temporary_lobs on a shared session',
    ),
    (
        'data_types/test_2500_lobs_async.py::test_data_types_2515',
        'v$temporary_lobs on a shared session',
    ),
):
    DENYLIST_TESTS[_test] = _reason

# Tests that fail against ANY server before 23ai, real Oracle included, so they
# are deselected only when the server the suite runs against reports one -- a
# Mirror presenting 12.1, say. python-oracledb pipelines for real only with a
# 23ai-class server; with an older one it runs a pipeline's operations one by
# one, and two with the same SQL share a cursor, so the last one's fetch
# settings apply to both. 18c and 21c fail these exactly as such a Mirror does
# (#1615). The second three are new in v26 and fail the same way directly
# against 21c: an operation after a failed one in a continue_on_error pipeline
# is lost (1215, 1223), and a failing last operation raises StopIteration
# (1224).
PRE_23AI_TESTS: dict[str, str] = {
    f'connection/test_1200_pipelining_async.py::test_connection_{n}': reason
    for n, reason in (
        ('1237', 'pre-23ai: no real pipelining'),
        ('1245', 'pre-23ai: no real pipelining'),
        ('1246', 'pre-23ai: no real pipelining'),
        ('1247', 'pre-23ai: no real pipelining'),
        ('1248', 'pre-23ai: no real pipelining'),
        ('1215', 'pre-23ai: pipeline error handling'),
        ('1223', 'pre-23ai: pipeline error handling'),
        ('1224', 'pre-23ai: pipeline error handling'),
    )
}

# Tests that fail for a gap with an open ticket, deselected until it is fixed so
# a run stays clean and a new failure stands out. Each names its ticket; delete
# the entry in the PR that closes it. OPEN_TICKET_TESTS fail through either
# Mirror, POSTGRES_OPEN_TICKET_TESTS through a Mirror-over-PostgreSQL only.
OPEN_TICKET_TESTS: dict[str, str] = {}

POSTGRES_OPEN_TICKET_TESTS: dict[str, str] = {
    'connection/test_1200_pipelining_async.py::test_connection_1229': (
        'PL/SQL executemany in a pipeline (#1783)'
    ),
    'cursor/test_1200_executemany.py::test_cursor_1234': 'a(k).f := x in PKG_TESTRECORDS (#1795)',
    'cursor/test_1300_executemany_async.py::test_cursor_1330': (
        'a(k).f := x in PKG_TESTRECORDS (#1795)'
    ),
    'cursor/test_2700_features_12_1.py::test_cursor_2710': 'a(k).f := x in PKG_TESTRECORDS (#1795)',
    'cursor/test_2700_features_12_1.py::test_cursor_2711': 'a(k).f := x in PKG_TESTRECORDS (#1795)',
    'cursor/test_2700_features_12_1.py::test_cursor_2712': 'a(k).f := x in PKG_TESTRECORDS (#1795)',
    'data_types/test_1900_dbobject.py::test_data_types_1937': 'INTERVAL in a %ROWTYPE (#1781)',
    'data_types/test_3500_dbobject_async.py::test_data_types_3516': (
        'INTERVAL in a %ROWTYPE (#1781)'
    ),
}

# Tests that fail through a Mirror-over-PostgreSQL by what PostgreSQL itself is,
# not by anything the Mirror leaves undone, deselected with --backend postgres.
# A passthrough to Oracle runs them, and they pass there.
POSTGRES_BACKEND_TESTS: dict[str, str] = {
    # PostgreSQL text never holds bytes invalid in UTF-8, which these store
    # through UTL_RAW.CAST_TO_VARCHAR2 and read back with encoding_errors (#1659).
    'cursor/test_2000_var.py::test_cursor_2032': 'PostgreSQL text holds no invalid UTF-8',
    'hooks/test_1100_typehandler.py::test_hooks_1107': 'PostgreSQL text holds no invalid UTF-8',
    # pgvector's vector, a FLOAT32 VECTOR's storage, holds at most 16000
    # dimensions, where Oracle's holds 65535; these insert 32768 and 65535
    # into a FLOAT32 column (#1734).
    'data_types/test_2700_vector.py::test_data_types_2728': 'pgvector holds at most 16000 dimensions',
    'data_types/test_3000_sparse_vector.py::test_data_types_3024': 'pgvector holds at most 16000 dimensions',
}


def _server_major(dsn: str, user: str, password: str) -> int | None:
    # The major release the server reports, or None when it cannot be asked.
    try:
        import oracledb

        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            return int(conn.version.split('.')[0])
    except Exception as exc:  # the suite itself reports a server it cannot reach
        print(f'# server version not read ({exc}); nothing deselected for it')
        return None


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
    ap.add_argument(
        '--backend',
        choices=('oracle', 'postgres'),
        default='oracle',
        help='what the Mirror runs on: postgres deselects what PostgreSQL cannot do',
    )
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
    deselected = dict(DENYLIST_TESTS) | OPEN_TICKET_TESTS
    major = _server_major(args.dsn, args.user, args.password)
    if major is not None and major < 23:
        deselected.update(PRE_23AI_TESTS)
    if args.backend == 'postgres':
        deselected.update(POSTGRES_BACKEND_TESTS)
        deselected.update(POSTGRES_OPEN_TICKET_TESTS)
    for test in deselected:
        cmd += ['--deselect', f'tests/{test}']
    cmd += args.pytest_args

    print(f'# {len(DENYLIST)} files denylisted (out-of-scope features):')
    for filename, reason in sorted(DENYLIST.items()):
        print(f'#   {filename:<42} {reason}')
    print(f'# {len(deselected)} individual tests deselected (cannot pass here):')
    for test, reason in sorted(deselected.items()):
        print(f'#   {test:<52} {reason}')
    print(f'# running in {args.suite} against {args.dsn}\n', flush=True)
    return subprocess.call(cmd, cwd=args.suite, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
