# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The Mirror's own TCPS listener (#1834).

    seerdb client ── wallet mTLS ──▶ Mirror (TLS, then TNS)

No proxy in between: the Mirror terminates TLS itself, with the committed
wallet fixture's server certificate, and requires the client certificate the
same fixture's CA issued.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import ssl
import threading
from collections.abc import Iterator

import pytest
from _tls_proxy import wallet_naming_port

import seerdb
from seerdb.common.tns_consts import TNS_TYPE_VARCHAR
from seerdb.server import ColumnMeta, Result, UnsupportedFeature, credential_lookup
from seerdb.server.backend import Capability
from seerdb.server.launcher import _tls_context, build

_WALLET = os.path.join(os.path.dirname(__file__), 'fixtures', 'wallet')
_CA = os.path.join(_WALLET, 'ca_cert.pem')
_CERT = os.path.join(_WALLET, 'server_cert.pem')
_KEY = os.path.join(_WALLET, 'server_key.pem')


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
def tcps_mirror() -> Iterator[seerdb.Server]:
    server = seerdb.Server(
        host='127.0.0.1',
        port=0,
        backend_factory=_DualBackend,
        ssl_context=_tls_context(_CERT, _KEY, _CA),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.close()
    thread.join(timeout=5)


@pytest.fixture
def wallet(tcps_mirror: seerdb.Server) -> Iterator[str]:
    # A wallet dsn names the address, so its alias names the Mirror (#1975).
    path = wallet_naming_port(tcps_mirror.port)
    yield path
    shutil.rmtree(path)


def _wallet_kwargs(wallet: str) -> dict:
    return {
        'user': 'PYO',
        'password': 'pyo123',
        'wallet_location': wallet,
        'dsn': 'seerdb_test',
        'timeout': 5000,
    }


def _select_dual(wallet: str) -> tuple:
    conn = seerdb.connect(**_wallet_kwargs(wallet))
    try:
        cur = conn.cursor()
        cur.execute('select * from dual')
        return cur.fetchone()
    finally:
        conn.close()


def test_a_wallet_client_runs_sql_over_the_mirrors_own_tls(wallet: str) -> None:
    assert _select_dual(wallet) == ('X',)


def test_an_async_wallet_client_runs_sql_over_it(wallet: str) -> None:
    async def run() -> tuple:
        conn = await seerdb.connect_async(**_wallet_kwargs(wallet))
        async with conn:
            cur = conn.cursor()
            await cur.execute('select * from dual')
            return await cur.fetchone()

    assert asyncio.run(run()) == ('X',)


def test_a_client_that_does_not_speak_tls_is_dropped_and_the_mirror_serves_on(
    tcps_mirror: seerdb.Server, wallet: str
) -> None:
    # Plain TNS bytes where a TLS ClientHello belongs: that connection fails its
    # handshake and closes, and the listener goes on serving TLS clients.
    with socket.create_connection(('127.0.0.1', tcps_mirror.port), timeout=5) as raw:
        raw.sendall(b'\x00\x3a\x00\x00\x01\x00\x00\x00' + bytes(50))
        # The Mirror ends it: a TLS alert then EOF, or a reset.
        try:
            while raw.recv(4096):
                pass
        except ConnectionResetError:
            pass
    assert _select_dual(wallet) == ('X',)


@pytest.fixture
def tls_mirror() -> Iterator[seerdb.Server]:
    # TLS without a client certificate: what a tcps:// dsn alone reaches.
    server = seerdb.Server(
        host='127.0.0.1',
        port=0,
        backend_factory=_DualBackend,
        ssl_context=_tls_context(_CERT, _KEY, ''),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.close()
    thread.join(timeout=5)


def test_a_tcps_dsn_runs_sql_over_tls(tls_mirror: seerdb.Server) -> None:
    # tcps:// turns TLS on; an ssl= configuration beside it is kept (#1977).
    conn = seerdb.connect(
        user='PYO',
        password='pyo123',
        dsn=f'tcps://127.0.0.1:{tls_mirror.port}/XE',
        ssl={'ca_certs': _CA, 'check_hostname': False},
        timeout=5000,
    )
    try:
        cur = conn.cursor()
        cur.execute('select * from dual')
        assert cur.fetchone() == ('X',)
    finally:
        conn.close()


def test_a_tcps_dsn_alone_verifies_against_the_system_trust_store(
    tls_mirror: seerdb.Server,
) -> None:
    # Without ssl=, tcps:// is ssl=True: the fixture CA is not trusted, so the
    # handshake is refused -- it was attempted (#1977).
    with pytest.raises(ssl.SSLCertVerificationError):
        seerdb.connect(
            user='PYO',
            password='pyo123',
            dsn=f'tcps://127.0.0.1:{tls_mirror.port}/XE',
            timeout=5000,
        )


def test_the_launcher_builds_a_tls_context_from_its_options() -> None:
    launch = build(
        ['--tls-cert', _CERT, '--tls-key', _KEY, '--tls-ca', _CA, 'observe'], env={}
    )
    assert launch.ssl_context is None  # the observer serves no TLS
    launch = build(
        ['--tls-cert', _CERT, '--tls-key', _KEY, 'postgres', 'dbname=mirror'],
        env={'MIRROR_USERS': 'scott:tiger'},
    )
    assert launch.ssl_context is not None
    assert (
        build(['postgres', 'dbname=mirror'], env={'MIRROR_USERS': 'a:b'}).ssl_context
        is None
    )


@pytest.mark.parametrize(
    ('cert', 'key', 'ca', 'message'),
    [
        ('', _KEY, '', 'need --tls-cert'),
        ('', '', _CA, 'need --tls-cert'),
        ('/nonexistent/cert.pem', '', '', 'cannot load'),
        (_CERT, _CA, '', 'cannot load'),  # not the certificate's key
    ],
)
def test_tls_options_that_cannot_work_stop_the_launcher(
    cert: str, key: str, ca: str, message: str
) -> None:
    with pytest.raises(SystemExit, match=message):
        _tls_context(cert, key, ca)
