# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Run a Mirror backed by PostgreSQL — Oracle clients, a PostgreSQL database.

    python examples/mirror_over_postgres.py [CONNINFO] [PORT]

CONNINFO is a libpq connection string (default reads the ``MIRROR_PG`` env var,
falling back to ``host=127.0.0.1 port=5432 user=pyo password=pyo123
dbname=mirror``); PORT defaults to 1521.

Then point a thin-dialect Oracle client at ``127.0.0.1:PORT`` as ``PYO`` /
``pyo123`` (service ``XE``) and run PostgreSQL-flavoured SQL, e.g.:

    cur.execute('create table t (id integer, name varchar(20))')
    cur.execute("insert into t values (1, 'alice')")
    cur.execute('select * from t')     # -> [(1, 'alice')]

More users log in with MIRROR_USERS, a comma-separated list of
``user:password`` pairs, as for the passthrough example (#1655)::

    MIRROR_USERS=pythontestproxy:pythontestproxy \
        python examples/mirror_over_postgres.py

Each user's objects go to a schema of its name, which ``CREATE USER`` makes.
PYO is always included, so existing use is unchanged.

**Accounts in the database (#879).** With MIRROR_AUTH_PG, a libpq conninfo as
a role of its own, the accounts live in that role's ``seerdb_auth.accounts``
table instead of this process: CREATE USER, ALTER USER ... IDENTIFIED BY, DROP
USER and a password change persist, and the accounts above seed it once. The
role the Mirror's sessions run as must not be a superuser and must not own
that schema, or a client's SQL could read the passwords::

    MIRROR_AUTH_PG='host=127.0.0.1 dbname=mirror user=mirror_auth password=...' \
        python examples/mirror_over_postgres.py

Requires the ``psycopg`` package.
"""

from __future__ import annotations

import os
import sys

from mirror_launch import mirror_credentials, setup_logging
from oracle_compat_backend import OracleCompatBackend
from postgres_backend import PostgresBackend

import seerdb

_DEFAULT_CONNINFO = 'host=127.0.0.1 port=5432 user=pyo password=pyo123 dbname=mirror'


def main() -> None:
    conninfo = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get('MIRROR_PG', _DEFAULT_CONNINFO)
    )
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 1521
    setup_logging()
    # One shared credential map across every session's backend, so a
    # changepassword on one connection is visible to the next (#515).
    credentials = mirror_credentials('PYO', 'pyo123')
    # One PostgreSQL session per client connection, behind the OracleCompatBackend
    # so a real sqlplus can bootstrap its session (thin clients pass through).
    seerdb.serve(
        '127.0.0.1',
        port,
        backend_factory=lambda: OracleCompatBackend(
            PostgresBackend(
                conninfo,
                credentials=credentials,
                # SEERDB_TRANSLATION_REPORT=1 logs, per statement, the Oracle
                # translation it took, to sys.ora_translation_log (#1557).
                translation_report=os.environ.get('SEERDB_TRANSLATION_REPORT') == '1',
                auth_conninfo=os.environ.get('MIRROR_AUTH_PG') or None,
                # The release it presents (#1704): Oracle 23ai, field version 24,
                # by default (#1747); MIRROR_PRESENTS=12.1 pins the older one.
                # A client of any version from 11.2 negotiates its own (#816).
                presents=os.environ.get('MIRROR_PRESENTS', '23ai'),
            )
        ),
    )


if __name__ == '__main__':
    main()
