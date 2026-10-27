# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The ws:// / wss:// transport, a seerdb extension (#1988).

Three tiers, all offline:

* the dsn forms parse to the ws / wss protocol;
* the byte-pipe adapters (:class:`WsSocket`, the async reader/writer) round-trip
  bytes, serve partial reads, surface a timeout and read a close as EOF;
* a full client connects over ws:// through an in-process WebSocket<->TCP bridge
  to an in-process Mirror, sync and async -- no Oracle bed needed.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from typing import Any

import pytest

import seerdb
from seerdb.client.dsn import parse_dsn
from seerdb.common.tns_consts import TNS_TYPE_VARCHAR
from seerdb.server import (
    ColumnMeta,
    Result,
    UnsupportedFeature,
    credential_lookup,
)

# The transport needs the optional `websockets` package (the `ws` extra). Where
# it is absent -- a core CI install -- the whole module skips.
websockets = pytest.importorskip('websockets')

from seerdb.client._ws import connect_ws, connect_ws_async  # noqa: E402

# --------------------------------------------------------------------------- #
# dsn forms
# --------------------------------------------------------------------------- #


def test_easy_connect_ws_and_wss_parse() -> None:
    ws = parse_dsn('ws://host.example:1522/FREEPDB1')
    assert (ws.protocol, ws.host, ws.port, ws.service_name) == (
        'ws',
        'host.example',
        1522,
        'FREEPDB1',
    )
    wss = parse_dsn('wss://host.example:1522/FREEPDB1')
    assert wss.protocol == 'wss'
    # The /segment stays the Oracle service name, as over tcps:// -- the
    # WebSocket resource path is '/'.
    assert wss.service_name == 'FREEPDB1'


def test_descriptor_wss_parses() -> None:
    target = parse_dsn(
        '(DESCRIPTION=(ADDRESS=(PROTOCOL=WSS)(HOST=h)(PORT=1522))'
        '(CONNECT_DATA=(SERVICE_NAME=S)))'
    )
    assert (target.protocol, target.host, target.port, target.service_name) == (
        'wss',
        'h',
        1522,
        'S',
    )


def test_an_unknown_scheme_is_rejected() -> None:
    with pytest.raises(Exception, match='tcp, tcps, ws or wss'):
        parse_dsn('wwss://h:1/S')


# --------------------------------------------------------------------------- #
# the byte-pipe adapters
# --------------------------------------------------------------------------- #


def _sync_echo(ws) -> None:
    # Echo every binary message; a b'BYE' control makes the server return, which
    # closes the connection so the client reads EOF.
    for message in ws:
        if message == b'BYE':
            return
        ws.send(message)


class _SyncServer:
    def __init__(self, handler) -> None:
        from websockets.sync.server import serve

        self._server = serve(handler, '127.0.0.1', 0)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def sync_echo() -> Iterator[int]:
    server = _SyncServer(_sync_echo)
    try:
        yield server.port
    finally:
        server.close()


def test_ws_socket_round_trips_and_serves_partial_reads(sync_echo: int) -> None:
    sock = connect_ws('ws', '127.0.0.1', sync_echo, '/', None, 5)
    try:
        assert sock.send(b'hello') == 5
        assert sock.recv(64) == b'hello'
        # One message, read in two slices: the byte pipe keeps the remainder.
        sock.send(b'abcdef')
        assert sock.recv(3) == b'abc'
        assert sock.recv(3) == b'def'
    finally:
        sock.close()


def test_ws_socket_recv_times_out(sync_echo: int) -> None:
    sock = connect_ws('ws', '127.0.0.1', sync_echo, '/', None, 5)
    try:
        sock.settimeout(0.2)
        with pytest.raises(TimeoutError):
            sock.recv(16)  # nothing was sent, so nothing echoes back
    finally:
        sock.close()


def test_ws_socket_reads_a_close_as_eof(sync_echo: int) -> None:
    sock = connect_ws('ws', '127.0.0.1', sync_echo, '/', None, 5)
    try:
        sock.send(b'BYE')  # server returns -> connection closes
        assert sock.recv(16) == b''
        assert sock.recv(16) == b''  # idempotent at EOF
    finally:
        sock.close()


def test_async_stream_round_trips_and_serves_partial_reads() -> None:
    from websockets.asyncio.server import serve

    async def run() -> None:
        async def echo(ws) -> None:
            async for message in ws:
                await ws.send(message)

        async with serve(echo, '127.0.0.1', 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            (reader, writer) = await connect_ws_async(
                'ws', '127.0.0.1', port, '/', None, 5
            )
            writer.write(b'abcdef')
            await writer.drain()
            assert await reader.read(3) == b'abc'
            assert await reader.read(3) == b'def'
            writer.close()
            await writer.wait_closed()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# end to end: client ws:// -> bridge -> in-process Mirror
# --------------------------------------------------------------------------- #


class _DualBackend:
    capabilities: frozenset = frozenset()

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


class _WsTcpBridge:
    """A protocol-blind WebSocket<->TCP bridge: each WS client is pumped, in
    both directions, to a TCP connection to ``target``. Its own asyncio loop
    runs in a background thread so a sync client can drive it."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._server: Any = None
        self.port = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError('ws bridge did not start')

    async def _handle(self, ws) -> None:
        (reader, writer) = await asyncio.open_connection(*self._target)

        async def ws_to_tcp() -> None:
            try:
                async for message in ws:
                    writer.write(
                        message if isinstance(message, bytes) else message.encode()
                    )
                    await writer.drain()
            finally:
                writer.close()

        async def tcp_to_ws() -> None:
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await ws.send(data)
            finally:
                await ws.close()

        await asyncio.gather(ws_to_tcp(), tcp_to_ws(), return_exceptions=True)

    def _run(self) -> None:
        from websockets.asyncio.server import serve

        asyncio.set_event_loop(self._loop)

        async def start() -> None:
            self._server = await serve(self._handle, '127.0.0.1', 0, max_size=None)
            self.port = self._server.sockets[0].getsockname()[1]

        self._loop.run_until_complete(start())
        self._ready.set()
        self._loop.run_forever()

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


@pytest.fixture
def mirror() -> Iterator[seerdb.Server]:
    server = seerdb.Server(host='127.0.0.1', port=0, backend_factory=_DualBackend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.close()
        thread.join(timeout=5)


@pytest.fixture
def ws_bridge(mirror: seerdb.Server) -> Iterator[int]:
    bridge = _WsTcpBridge('127.0.0.1', mirror.port)
    try:
        yield bridge.port
    finally:
        bridge.close()


def _kwargs(**extra) -> dict:
    return {'user': 'PYO', 'password': 'pyo123', 'timeout': 5000, **extra}


def test_connect_over_ws_runs_a_query(ws_bridge: int) -> None:
    conn = seerdb.connect(**_kwargs(dsn=f'ws://127.0.0.1:{ws_bridge}/XE'))
    try:
        cur = conn.cursor()
        cur.execute('select * from dual')
        assert cur.fetchone() == ('X',)
    finally:
        conn.close()


def test_connect_async_over_ws_runs_a_query(ws_bridge: int) -> None:
    async def run() -> tuple:
        conn = await seerdb.connect_async(
            **_kwargs(dsn=f'ws://127.0.0.1:{ws_bridge}/XE')
        )
        async with conn:
            cur = conn.cursor()
            await cur.execute('select * from dual')
            return await cur.fetchone()

    assert asyncio.run(run()) == ('X',)
