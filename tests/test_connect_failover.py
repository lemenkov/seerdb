# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT
"""A connect descriptor's addresses are tried in turn (#1983).

As python-oracledb 26.0.1 tries them, measured: in order, the next when the TCP
connect fails or the listener refuses; only the first with FAILOVER=OFF;
shuffled with LOAD_BALANCE=ON; the whole list again RETRY_COUNT times,
RETRY_DELAY seconds apart. A failed login is not a reason to try the next.
An in-process Mirror is the live address; a port nothing listens on is dead.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import pytest

import seerdb
from seerdb.client.aconnection import AsyncOracleConnect
from seerdb.client.connection import OracleConnect
from seerdb.client.netconfig import Address, Route, parse_descriptor
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


@pytest.fixture
def mirror() -> Iterator[seerdb.Server]:
    server = seerdb.Server(host='127.0.0.1', port=0, backend_factory=_DualBackend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.close()
    thread.join(timeout=5)


@pytest.fixture
def dead_port() -> int:
    # A port that was free a moment ago: a connect to it is refused.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _address(port: int) -> str:
    return f'(ADDRESS=(PROTOCOL=TCP)(HOST=127.0.0.1)(PORT={port}))'


def _descriptor(*ports: int, options: str = '') -> str:
    addresses = ''.join(_address(p) for p in ports)
    return f'(DESCRIPTION={options}{addresses}(CONNECT_DATA=(SERVICE_NAME=XE)))'


def _tried(
    monkeypatch: pytest.MonkeyPatch, cls: type[OracleConnect] | type[AsyncOracleConnect]
) -> list[int]:
    # The ports a connection's route tries, in order.
    ports: list[int] = []
    original = cls._use_address

    def use(self, address: Address) -> None:
        ports.append(address.port)
        original(self, address)

    monkeypatch.setattr(cls, '_use_address', use)
    return ports


def test_the_reader_keeps_every_address_and_the_options() -> None:
    route = parse_descriptor(
        _descriptor(
            1, 2, 3, options='(RETRY_COUNT=2)(RETRY_DELAY=0.5)(LOAD_BALANCE=ON)'
        )
    ).route
    assert route is not None
    assert [a.port for a in route.addresses] == [1, 2, 3]
    assert (route.retry_count, route.retry_delay, route.load_balance) == (2, 0.5, True)
    assert parse_descriptor(_descriptor(1)).route is None
    off = Route((Address('h', 1), Address('h', 2)), failover=False)
    assert [a.port for a in off.order()] == [1]


def test_a_dead_address_is_passed_over(
    mirror: seerdb.Server, dead_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried = _tried(monkeypatch, OracleConnect)
    with seerdb.connect(
        user='PYO', password='pyo123', dsn=_descriptor(dead_port, mirror.port)
    ) as conn:
        cur = conn.cursor()
        cur.execute('select * from dual')
        assert cur.fetchone() == ('X',)
    assert tried == [dead_port, mirror.port]


def test_failover_off_tries_only_the_first(
    mirror: seerdb.Server, dead_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried = _tried(monkeypatch, OracleConnect)
    with pytest.raises(OSError):
        seerdb.connect(
            user='PYO',
            password='pyo123',
            dsn=_descriptor(dead_port, mirror.port, options='(FAILOVER=OFF)'),
        )
    assert tried == [dead_port]


def test_retries_run_the_list_again_after_the_delay(
    dead_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried = _tried(monkeypatch, OracleConnect)
    start = time.monotonic()
    with pytest.raises(OSError):
        seerdb.connect(
            user='PYO',
            password='pyo123',
            dsn=_descriptor(dead_port, options='(RETRY_COUNT=2)(RETRY_DELAY=0.2)'),
        )
    assert tried == [dead_port] * 3
    assert time.monotonic() - start >= 0.4


def test_easy_connect_retries_one_address(
    dead_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried = _tried(monkeypatch, OracleConnect)
    with pytest.raises(OSError):
        seerdb.connect(
            user='PYO',
            password='pyo123',
            dsn=f'127.0.0.1:{dead_port}/XE?retry_count=1&retry_delay=0',
        )
    assert tried == [dead_port] * 2


def test_load_balance_shuffles_each_connect(
    dead_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Eight dead copies of one port would all look alike; distinct ports, all
    # refused, show the order instead.
    ports = [dead_port + i for i in range(8)]
    orders = set()
    for _ in range(6):
        tried = _tried(monkeypatch, OracleConnect)
        with pytest.raises(OSError):
            seerdb.connect(
                user='PYO',
                password='pyo123',
                dsn=_descriptor(*ports, options='(LOAD_BALANCE=ON)'),
            )
        assert sorted(tried) == ports
        orders.add(tuple(tried))
        monkeypatch.undo()
    assert len(orders) > 1


def test_a_failed_login_is_not_failed_over(
    mirror: seerdb.Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried = _tried(monkeypatch, OracleConnect)
    with pytest.raises(seerdb.DatabaseError) as info:
        seerdb.connect(
            user='PYO', password='wrong', dsn=_descriptor(mirror.port, mirror.port)
        )
    assert info.value.code == 1017
    assert tried == [mirror.port]


def test_async_passes_a_dead_address_over(
    mirror: seerdb.Server, dead_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried = _tried(monkeypatch, AsyncOracleConnect)

    async def run() -> tuple:
        conn = await seerdb.connect_async(
            user='PYO', password='pyo123', dsn=_descriptor(dead_port, mirror.port)
        )
        async with conn:
            cur = conn.cursor()
            await cur.execute('select * from dual')
            return await cur.fetchone()

    assert asyncio.run(run()) == ('X',)
    assert tried == [dead_port, mirror.port]


def test_async_failover_off_and_retries(
    dead_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried = _tried(monkeypatch, AsyncOracleConnect)

    async def run(dsn: str) -> None:
        await seerdb.connect_async(user='PYO', password='pyo123', dsn=dsn)

    with pytest.raises(OSError):
        asyncio.run(
            run(_descriptor(dead_port, options='(RETRY_COUNT=1)(RETRY_DELAY=0)'))
        )
    assert tried == [dead_port] * 2
