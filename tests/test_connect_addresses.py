# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT
"""The client reaches each address its host resolves to (#2000), and the
Mirror listens on IPv6 (#2001).

Both connection classes forced their socket to IPv4, so an IPv6 host could not
be reached at all. They now try the addresses getaddrinfo returns, in order and
each in its own family, as python-oracledb does. The Mirror bound IPv4 only; it
now binds the family its host resolves to, and a wildcard serves both.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import pytest

import seerdb
from seerdb.common.tns_consts import TNS_TYPE_VARCHAR
from seerdb.server import ColumnMeta, Result, UnsupportedFeature, credential_lookup
from seerdb.server.backend import Capability


class _DualBackend:
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


def _has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(('::1', 0))
        return True
    except OSError:
        return False


@pytest.fixture
def mirror() -> Iterator[seerdb.Server]:
    server = seerdb.Server(host='127.0.0.1', port=0, backend_factory=_DualBackend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.close()
    thread.join(timeout=5)


def _serve(host: str) -> Iterator[seerdb.Server]:
    server = seerdb.Server(host=host, port=0, backend_factory=_DualBackend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.close()
    thread.join(timeout=5)


@pytest.fixture
def ipv6_port() -> Iterator[int]:
    # The Mirror itself on [::1], which it can bind since #2001.
    if not _has_ipv6_loopback():
        pytest.skip('this host has no IPv6 loopback')
    for server in _serve('::1'):
        yield server.port


def _kwargs(**extra) -> dict:
    return {'user': 'PYO', 'password': 'pyo123', 'timeout': 5000, **extra}


@pytest.mark.parametrize('form', ['host', 'dsn'])
def test_an_ipv6_host_connects(ipv6_port: int, form: str) -> None:
    address = (
        {'host': '::1', 'port': ipv6_port, 'service_name': 'XE'}
        if form == 'host'
        else {'dsn': f'[::1]:{ipv6_port}/XE'}
    )
    conn = seerdb.connect(**_kwargs(**address))
    try:
        cur = conn.cursor()
        cur.execute('select * from dual')
        assert cur.fetchone() == ('X',)
    finally:
        conn.close()


def test_an_ipv6_host_connects_async(ipv6_port: int) -> None:
    async def run() -> tuple:
        conn = await seerdb.connect_async(
            **_kwargs(host='::1', port=ipv6_port, service_name='XE')
        )
        async with conn:
            cur = conn.cursor()
            await cur.execute('select * from dual')
            return await cur.fetchone()

    assert asyncio.run(run()) == ('X',)


def test_localhost_still_reaches_an_ipv4_only_listener(mirror: seerdb.Server) -> None:
    # The case the IPv4-only socket was for: localhost resolves to ::1 first,
    # and the Mirror listens on 127.0.0.1 only. The refused ::1 costs nothing,
    # and the connection goes on to the next address at once.
    start = time.monotonic()
    conn = seerdb.connect(
        **_kwargs(host='localhost', port=mirror.port, service_name='XE')
    )
    conn.close()
    assert time.monotonic() - start < 3

    async def run() -> None:
        conn = await seerdb.connect_async(
            **_kwargs(host='localhost', port=mirror.port, service_name='XE')
        )
        await conn.close()

    start = time.monotonic()
    asyncio.run(run())
    assert time.monotonic() - start < 3


def test_a_wildcard_mirror_serves_ipv4_and_ipv6() -> None:
    # '::' binds one dual-stack socket: the same Mirror answers on 127.0.0.1
    # and on ::1 (#2001).
    if not (_has_ipv6_loopback() and socket.has_dualstack_ipv6()):
        pytest.skip('this host has no dual-stack IPv6')
    for server in _serve('::'):
        for host in ('127.0.0.1', '::1'):
            conn = seerdb.connect(
                **_kwargs(host=host, port=server.port, service_name='XE')
            )
            try:
                cur = conn.cursor()
                cur.execute('select * from dual')
                assert cur.fetchone() == ('X',)
            finally:
                conn.close()
