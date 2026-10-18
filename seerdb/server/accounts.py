# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The accounts a Mirror serves, and the statements that change them (#879,
#1703).

O5LOGON proves a password both ways, so the Mirror holds each account's
secret itself. A backend keeps them in an :class:`_AccountStore`: the map the
launcher gives, or with an auth database, its `seerdb_auth.accounts` table,
where CREATE USER, ALTER USER ... IDENTIFIED BY, DROP USER and a password
change persist. Shared by the PostgreSQL backend and the passthrough.
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING

from seerdb.server.backend import credential_lookup

if TYPE_CHECKING:
    import psycopg

# CREATE USER, ALTER USER ... IDENTIFIED BY and DROP USER (#879): the name,
# quoted or not, and the password, quoted or not -- an unquoted one keeps its
# case, as Oracle's has since 11g.
_USER_NAME = r'("[^"]+"|[A-Za-z_][\w$#]*)'
_USER_PASSWORD = r'("[^"]*"|[^\s";]+)'
_CREATE_USER_STATEMENT = re.compile(
    rf'(?is)\s*CREATE\s+USER\s+{_USER_NAME}\s+IDENTIFIED\s+'
    rf'(?:BY\s+{_USER_PASSWORD}|EXTERNALLY|GLOBALLY)(?![\w$#])'
)
_ALTER_USER_PASSWORD = re.compile(
    rf'(?is)\s*ALTER\s+USER\s+{_USER_NAME}\s+IDENTIFIED\s+BY\s+{_USER_PASSWORD}'
    rf'(?:\s+REPLACE\s+{_USER_PASSWORD})?'
)
_DROP_USER_STATEMENT = re.compile(
    rf'(?is)\s*DROP\s+USER\s+{_USER_NAME}(\s+CASCADE)?\s*;?\s*$'
)


def _user_name(written: str) -> str:
    # An Oracle user name as written: a quoted one as it is, an unquoted one
    # upper-cased.
    return written[1:-1] if written.startswith('"') else written.upper()


def _password_text(written: str) -> str:
    return written[1:-1] if written.startswith('"') else written


_ACCOUNTS_SCHEMA = 'seerdb_auth'
_ACCOUNTS_DDL = (
    f'CREATE SCHEMA IF NOT EXISTS {_ACCOUNTS_SCHEMA};'
    f'REVOKE ALL ON SCHEMA {_ACCOUNTS_SCHEMA} FROM PUBLIC;'
    f'CREATE TABLE IF NOT EXISTS {_ACCOUNTS_SCHEMA}.accounts '
    '(username text PRIMARY KEY, password text NOT NULL);'
    # In per-user mode (#1857), the password of the user's PostgreSQL role,
    # which the Mirror makes up and logs the user's sessions in with.
    f'ALTER TABLE {_ACCOUNTS_SCHEMA}.accounts ADD COLUMN IF NOT EXISTS role_password text'
)
# The auth databases whose accounts table this process has made sure of, and
# seeded from its launcher's accounts, so each is done once (#879).
_ACCOUNTS_READY: set[str] = set()
_ACCOUNTS_LOCK = threading.Lock()


class _AccountStore:
    """The accounts a Mirror-over-PG serves: each user's password (#879).

    O5LOGON proves a password both ways, so the Mirror has to hold each
    secret itself. Without an auth database they live in the map the launcher
    gives, shared by every session, as before. With one -- `auth_conninfo`, a
    connection as a role of its own -- they live in its `seerdb_auth.accounts`,
    so CREATE USER, ALTER USER ... IDENTIFIED BY, DROP USER and a password
    change persist, and a user is added with SQL, not a restart.

    The role sessions run as must have no access to that schema, so no SQL a
    client sends can read a password: PostgreSQL refuses it, dynamic SQL
    included. Created by the auth role, the schema is that role's; it must not
    be the session role, and a superuser session role sees everything.
    """

    def __init__(self, seed: dict[str, str], auth_conninfo: str | None) -> None:
        self._seed = seed
        self._auth_conninfo = auth_conninfo

    @property
    def auth_conninfo(self) -> str | None:
        return self._auth_conninfo

    def _connect(self) -> psycopg.Connection:
        assert self._auth_conninfo is not None
        # Only an auth database needs psycopg, which the passthrough's
        # install need not have (the `mirror` extra brings it).
        import psycopg

        conn = psycopg.connect(self._auth_conninfo, autocommit=True)
        with _ACCOUNTS_LOCK:
            if self._auth_conninfo not in _ACCOUNTS_READY:
                # The launcher's accounts, where the store has none of that name
                # yet: a password changed since stays changed.
                conn.execute(_ACCOUNTS_DDL)
                for name, password in self._seed.items():
                    conn.execute(
                        f'INSERT INTO {_ACCOUNTS_SCHEMA}.accounts VALUES (%s, %s) '
                        'ON CONFLICT DO NOTHING',
                        (name.upper(), password),
                    )
                _ACCOUNTS_READY.add(self._auth_conninfo)
        return conn

    def secrets(self, username: str) -> tuple[str | None, str | None]:
        """The account's secret and its PostgreSQL role's password, read
        together for a per-user login (#1939); either None where there is
        none."""
        assert self._auth_conninfo is not None
        with self._connect() as conn:
            row = conn.execute(
                f'SELECT password, role_password FROM {_ACCOUNTS_SCHEMA}.accounts '
                'WHERE username = %s',
                (username.upper(),),
            ).fetchone()
        return (None, None) if row is None else (row[0], row[1])

    def secret(self, username: str) -> str | None:
        if self._auth_conninfo is None:
            return credential_lookup(self._seed, username)
        with self._connect() as conn:
            row = conn.execute(
                f'SELECT password FROM {_ACCOUNTS_SCHEMA}.accounts WHERE username = %s',
                (username.upper(),),
            ).fetchone()
        return None if row is None else row[0]

    def set(self, username: str, password: str) -> None:
        if self._auth_conninfo is None:
            for name in list(self._seed):
                if name.upper() == username.upper():
                    self._seed[name] = password
                    return
            self._seed[username.upper()] = password
            return
        with self._connect() as conn:
            conn.execute(
                f'INSERT INTO {_ACCOUNTS_SCHEMA}.accounts VALUES (%s, %s) '
                'ON CONFLICT (username) DO UPDATE SET password = EXCLUDED.password',
                (username.upper(), password),
            )

    def configured(self, username: str) -> bool:
        """Whether the operator configured the account -- in the config file
        or MIRROR_USERS -- rather than a CREATE USER made it: in per-user mode,
        the administrators (#1857)."""
        return any(name.upper() == username.upper() for name in self._seed)

    def role_password(self, username: str) -> str | None:
        """The password of the user's PostgreSQL role, in per-user mode
        (#1857); None while it has none."""
        assert self._auth_conninfo is not None
        with self._connect() as conn:
            row = conn.execute(
                f'SELECT role_password FROM {_ACCOUNTS_SCHEMA}.accounts '
                'WHERE username = %s',
                (username.upper(),),
            ).fetchone()
        return None if row is None else row[0]

    def set_role_password(self, username: str, password: str) -> None:
        assert self._auth_conninfo is not None
        with self._connect() as conn:
            conn.execute(
                f'UPDATE {_ACCOUNTS_SCHEMA}.accounts SET role_password = %s '
                'WHERE username = %s',
                (password, username.upper()),
            )

    def drop(self, username: str) -> None:
        if self._auth_conninfo is None:
            for name in list(self._seed):
                if name.upper() == username.upper():
                    del self._seed[name]
            return
        with self._connect() as conn:
            conn.execute(
                f'DELETE FROM {_ACCOUNTS_SCHEMA}.accounts WHERE username = %s',
                (username.upper(),),
            )
