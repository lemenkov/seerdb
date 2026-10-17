# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""seerdb.Server / seerdb.serve — the top-level Mirror entry point."""

from __future__ import annotations

import logging
import threading

import seerdb
from seerdb.common.tns_consts import FIELD_VERSION_10_2, TNS_TYPE_VARCHAR
from seerdb.server import (
    ColumnMeta,
    Result,
    UnsupportedFeature,
    credential_lookup,
)
from seerdb.server.backend import Capability


class _DualBackend:
    # A fresh instance is created per session by the factory below.
    capabilities: frozenset[Capability] = frozenset()

    def authenticate(self, username: str) -> str | None:
        return credential_lookup({'PYO': 'pyo123'}, username)

    def execute(self, sql: str, binds=()) -> Result:
        if 'dual' in sql.lower():
            col = ColumnMeta(
                name=b'DUMMY', data_type=TNS_TYPE_VARCHAR, data_length=1, max_size=1
            )
            return Result(columns=[col], rows=[('X',)])
        raise UnsupportedFeature(sql)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_seerdb_server_serves_a_client() -> None:
    # port=0 binds an ephemeral port, readable before serving.
    server = seerdb.Server(
        host='127.0.0.1',
        port=0,
        backend_factory=_DualBackend,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = seerdb.connect(
            host='127.0.0.1',
            port=server.port,
            user='PYO',
            password='pyo123',
            service_name='XE',
            timeout=5000,
        )
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        row = cursor.fetchone()
        conn.close()
    finally:
        server.close()
        thread.join(timeout=5)

    assert row == ('X',)


def test_a_10g_client_is_served() -> None:
    # A thin client that negotiates 10g (field version 4) logs in and reads a
    # row: the describe it gets ends each column, and the block, where a 10g
    # describe does (#814).
    server = seerdb.Server(host='127.0.0.1', port=0, backend_factory=_DualBackend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = seerdb.connect(
            host='127.0.0.1',
            port=server.port,
            user='PYO',
            password='pyo123',
            service_name='XE',
            timeout=5000,
            field_version=FIELD_VERSION_10_2,
        )
        negotiated = conn.field_version
        cursor = conn.cursor()
        cursor.execute('select * from dual')
        rows = cursor.fetchall()
        conn.close()
    finally:
        server.close()
        thread.join(timeout=5)

    assert negotiated == FIELD_VERSION_10_2
    assert rows == [('X',)]


def test_the_mirror_says_it_is_provisional(caplog) -> None:
    # Outside the version promise the client keeps (#1688): the package says so
    # to code, and a Mirror logs it, at WARNING, as it starts serving.
    import seerdb.server

    assert seerdb.server.__provisional__ is True
    server = seerdb.Server(host='127.0.0.1', port=0, backend_factory=_DualBackend)
    server.close()
    with caplog.at_level(logging.WARNING, logger='seerdb.server'):
        server.serve_forever()  # returns at once: the socket is closed
    notices = [r for r in caplog.records if 'technology preview' in r.getMessage()]
    assert [r.levelno for r in notices] == [logging.WARNING]


def test_a_mirror_serves_at_most_max_sessions() -> None:
    # GHSA-5827-wqfh-x2v3: with max_sessions, a connection past the limit is
    # closed as it arrives, and the slot frees when a session ends.
    import socket
    import time

    server = seerdb.Server(
        host='127.0.0.1', port=0, backend_factory=_DualBackend, max_sessions=1
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first = socket.create_connection(('127.0.0.1', server.port))
        time.sleep(0.3)
        second = socket.create_connection(('127.0.0.1', server.port))
        second.settimeout(5)
        assert second.recv(1) == b''
        second.close()
        first.close()
        time.sleep(0.3)
        conn = seerdb.connect(
            host='127.0.0.1',
            port=server.port,
            user='PYO',
            password='pyo123',
            service_name='XE',
            timeout=5000,
        )
        conn.close()
    finally:
        server.close()
        thread.join(timeout=5)


def test_a_client_that_does_not_log_in_is_dropped(monkeypatch) -> None:
    # GHSA-5827-wqfh-x2v3: a connection that stalls before logging in is
    # dropped once the login's deadline passes.
    import socket

    from seerdb.server import session

    monkeypatch.setattr(session, 'LOGIN_SECONDS', 0.3)
    server = seerdb.Server(host='127.0.0.1', port=0, backend_factory=_DualBackend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        idle = socket.create_connection(('127.0.0.1', server.port))
        idle.settimeout(5)
        assert idle.recv(1) == b''
        idle.close()
    finally:
        server.close()
        thread.join(timeout=5)


def _serving(server):  # a started server's thread
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _login_error(port: int, user: str, password: str) -> str:
    try:
        seerdb.connect(
            host='127.0.0.1',
            port=port,
            user=user,
            password=password,
            service_name='XE',
            timeout=5000,
        ).close()
    except seerdb.DatabaseError as exc:
        return str(exc).split(':')[0]
    return 'connected'


def test_an_unknown_user_is_refused_as_a_wrong_password_is(monkeypatch) -> None:
    # GHSA-742q-fjfr-w5x3: a name that is no account's is challenged and
    # refused with the same ORA-01017 as a wrong password.
    from seerdb.server import lockout

    monkeypatch.setattr(lockout, '_failures', {})
    monkeypatch.setattr(lockout, '_DELAY_STEP', 0.0)
    server = seerdb.Server(host='127.0.0.1', port=0, backend_factory=_DualBackend)
    thread = _serving(server)
    try:
        assert _login_error(server.port, 'NO_SUCH_USER', 'x') == 'ORA-01017'
        assert _login_error(server.port, 'PYO', 'wrong') == 'ORA-01017'
        assert _login_error(server.port, 'PYO', 'pyo123') == 'connected'
    finally:
        server.close()
        thread.join(timeout=5)


def test_failed_logins_lock_a_name_until_it_is_unlocked(monkeypatch) -> None:
    # GHSA-742q-fjfr-w5x3: enough failures in a row lock the name, an
    # account's or not, with ORA-28000; unlocking it lets it log in again.
    from seerdb.server import lockout

    monkeypatch.setattr(lockout, '_failures', {})
    monkeypatch.setattr(lockout, '_DELAY_STEP', 0.0)
    monkeypatch.setattr(lockout, '_attempts', 3)
    server = seerdb.Server(host='127.0.0.1', port=0, backend_factory=_DualBackend)
    thread = _serving(server)
    try:
        for name in ('PYO', 'NO_SUCH_USER'):
            for _ in range(3):
                assert _login_error(server.port, name, 'wrong') == 'ORA-01017'
            assert _login_error(server.port, name, 'pyo123') == 'ORA-28000'
        lockout.unlock('PYO')
        assert _login_error(server.port, 'PYO', 'pyo123') == 'connected'
        # A good login starts the count over.
        assert _login_error(server.port, 'PYO', 'wrong') == 'ORA-01017'
        assert _login_error(server.port, 'PYO', 'pyo123') == 'connected'
        assert not lockout.is_locked('PYO')
    finally:
        server.close()
        thread.join(timeout=5)
