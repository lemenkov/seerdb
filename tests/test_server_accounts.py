# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The passthrough's accounts follow what its upstream accepted (#1703)."""

from __future__ import annotations

import os

import pytest

from seerdb.server.backends.passthrough import OraclePassthroughBackend
from seerdb.server.launcher import build

_USERS = {'MIRROR_USERS': 'scott:tiger'}


def _backend(credentials: dict[str, str], auth: str | None = None):
    return OraclePassthroughBackend(
        host='127.0.0.1',
        port=1,
        service='X',
        credentials=credentials,
        auth_conninfo=auth,
    )


def _follow(backend, statements: list[str]) -> None:
    # What execute() does once the upstream accepted each statement.
    for statement in statements:
        backend._follow_accounts(statement)


def test_a_user_created_altered_and_dropped_upstream_is_followed() -> None:
    accounts = {'SCOTT': 'tiger'}
    backend = _backend(accounts)
    _follow(backend, ['create user newbie identified by "Pw 1"'])
    assert backend.authenticate('NEWBIE') == 'Pw 1'
    _follow(backend, ['alter user newbie identified by pw2 replace "Pw 1"'])
    assert backend.authenticate('newbie') == 'pw2'
    # Shared: another session's backend serves the same accounts.
    assert _backend(accounts).authenticate('NEWBIE') == 'pw2'
    _follow(backend, ['drop user newbie cascade'])
    assert backend.authenticate('NEWBIE') is None
    # Statements that change no account change nothing.
    _follow(backend, ['create user ext identified externally', 'select 1 from dual'])
    assert backend.authenticate('EXT') is None
    assert backend.authenticate('SCOTT') == 'tiger'


@pytest.mark.skipif(not os.environ.get('MIRROR_PG'), reason='needs MIRROR_PG')
def test_with_an_auth_database_the_accounts_persist() -> None:
    import psycopg

    auth = os.environ['MIRROR_PG']
    name = f'PASS_PERSIST_{os.getpid()}'
    try:
        _follow(_backend({}, auth), [f'create user {name} identified by kept1'])
        # A new backend, as after a restart, reads it from the database.
        assert _backend({}, auth).authenticate(name) == 'kept1'
    finally:
        with psycopg.connect(auth, autocommit=True) as conn:
            conn.execute(
                'DELETE FROM seerdb_auth.accounts WHERE username = %s', (name,)
            )


def test_the_passthrough_takes_an_auth_database() -> None:
    launch = build(
        [
            'passthrough',
            'oracle:1521/S',
            '--auth-conninfo',
            'dbname=auth',
            '--field-version',
            '24',
        ],
        env={},
    )
    assert launch.description == 'passthrough to oracle:1521/S'
    # Its accounts, or an auth database: one of them.
    with pytest.raises(SystemExit, match='auth-conninfo'):
        build(['passthrough', 'oracle:1521/S'], env={})
    # Without an account to probe the upstream with, the version is the
    # operator's to give.
    with pytest.raises(SystemExit, match='field-version'):
        build(['passthrough', 'oracle:1521/S', '--auth-conninfo', 'dbname=a'], env={})
