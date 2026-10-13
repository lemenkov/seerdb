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
    notices = [r for r in caplog.records if 'provisional' in r.getMessage()]
    assert [r.levelno for r in notices] == [logging.WARNING]
