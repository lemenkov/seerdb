# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Run a Mirror that relays to a real Oracle — a transparent Oracle relay.

    python examples/mirror_over_oracle.py [LISTEN_PORT] [UPSTREAM_HOST:PORT/SERVICE]

Defaults: listen on 1521, relay to 127.0.0.1:1522/XE. Point any Oracle client —
including the integration suite — at the listen port; every statement runs on
the real upstream Oracle and the real results come back through the Mirror, so a
test failure isolates a Mirror protocol gap.

**Accounts.** By default one, from SEERDB_TEST_USER / SEERDB_TEST_PASSWORD
(PYO / pyo123). A client logging in as anyone else is rejected by the backend's
authenticate() before it reaches a line of protocol code, which blocks anything
multi-user: proxy authentication needs two accounts by definition, and
python-oracledb's own suite needs three — a main user, a proxy user and a DBA
(#826/#834). Add more with MIRROR_USERS, a comma-separated list of
``user:password`` pairs::

    MIRROR_USERS=pythontest:pythontest,pythontestproxy:pythontestproxy,pyoadm:pyoadm123 \
        python examples/mirror_over_oracle.py 1600 192.168.0.150:1523/FREEPDB1

The SEERDB_TEST_* pair is always included, so existing use is unchanged.
"""

from __future__ import annotations

import logging
import os
import sys

from oracle_passthrough_backend import OraclePassthroughBackend

import seerdb


def main() -> None:
    listen_port = int(sys.argv[1]) if len(sys.argv) > 1 else 1521
    upstream = sys.argv[2] if len(sys.argv) > 2 else '127.0.0.1:1521/XE'
    hostport, service = upstream.rsplit('/', 1)
    host, port = hostport.rsplit(':', 1)

    user = os.environ.get('SEERDB_TEST_USER', 'PYO')
    password = os.environ.get('SEERDB_TEST_PASSWORD', 'pyo123')

    # ONE SHARED credential map, passed by reference to every session's backend.
    # Not a copy per session, and this is not a style preference: a
    # changepassword on one connection has to be visible to the next (#21/#486).
    # A hand-written launcher for #826 used `credentials=dict(CREDS)` so each
    # session got its own copy; the suite's change-password test then moved the
    # real password upstream while every other session kept the stale one, and
    # 2393 logins failed with ORA-01017 before anyone noticed. The cascade looks
    # exactly like a driver bug and is not one, so keep the single dict.
    credentials = {user.upper(): password}
    for pair in filter(None, os.environ.get('MIRROR_USERS', '').split(',')):
        extra_user, _, extra_password = pair.partition(':')
        if not extra_user or not _:
            raise SystemExit(f'MIRROR_USERS entry is not user:password: {pair!r}')
        credentials[extra_user.strip().upper()] = extra_password

    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s'
    )
    # The passthrough presents whatever its target speaks (§ Backend.field_version).
    # By default that is auto-detected — probe the target once at startup and let
    # the Mirror advertise the release it negotiates (11.2 -> fv6, 21c -> fv16,
    # 23ai -> fv24). MIRROR_FIELD_VERSION overrides it (e.g. to force a lower
    # version than the target, or when the target is not reachable at startup).
    override = os.environ.get('MIRROR_FIELD_VERSION')
    if override is not None:
        field_version: int | None = int(override)
    else:
        field_version = OraclePassthroughBackend.detect_version(
            host, int(port), service, user, password
        )
    logging.getLogger('seerdb.server').info(
        'passthrough to %s:%s/%s presenting field version %s, serving %d account(s): %s',
        host,
        port,
        service,
        field_version if field_version is not None else '(Mirror default)',
        len(credentials),
        ', '.join(sorted(credentials)),
    )
    seerdb.serve(
        '127.0.0.1',
        listen_port,
        backend_factory=lambda: OraclePassthroughBackend(
            host=host,
            port=int(port),
            service=service,
            credentials=credentials,
            field_version=field_version,
        ),
    )


if __name__ == '__main__':
    main()
