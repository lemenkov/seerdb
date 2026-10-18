# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Opt-in live wallet mutual-TLS test (#127).

Skipped unless ``SEERDB_WALLET_LIVE`` is set, so it never runs in the normal
offline suite. Point it at any TCPS (TLS) Oracle listener configured for client
-certificate authentication — an Autonomous Database, or the self-hosted 23ai
Free + TCPS setup in ``docs/wallet_mtls_live_testing.md`` — and it runs a real
``SELECT`` over wallet mTLS, sync and async.

Defaults match the self-hosted runbook (the committed fixture wallet, the
``seerdb_test`` DSN whose server DN is ``CN=seerdb-test-server``). Override any
piece via environment variables:

    SEERDB_WALLET_LIVE=1            enable this test (required)
    SEERDB_LIVE_HOST=127.0.0.1     TCPS listener host (falls back to SEERDB_TEST_HOST)
    SEERDB_LIVE_TCPS_PORT=2484     TCPS listener port
    SEERDB_LIVE_SERVICE=FREEPDB1   service name
    SEERDB_LIVE_USER=PYO           database user
    SEERDB_LIVE_PASSWORD=pyo123    database password
    SEERDB_LIVE_WALLET=<path>      client wallet dir (default: tests/fixtures/wallet)
    SEERDB_LIVE_DSN=seerdb_test    tnsnames alias in the wallet (for the server DN)

The alias names the address (#1975): with SEERDB_LIVE_WALLET it is used as it
is, and HOST / TCPS_PORT / SERVICE are ignored; with the fixture wallet they are
written into a copy of its alias.
"""

import asyncio
import os
import shutil
import unittest

from _tls_proxy import wallet_naming_port

import seerdb


def _cfg(wallet):
    return {
        'user': os.environ.get('SEERDB_LIVE_USER', 'PYO'),
        'password': os.environ.get('SEERDB_LIVE_PASSWORD', 'pyo123'),
        'wallet_location': wallet,
        'dsn': os.environ.get('SEERDB_LIVE_DSN', 'seerdb_test'),
        'timeout': 15000,
    }


def _wallet(test: unittest.TestCase) -> str:
    # A wallet dsn names the address (#1975). SEERDB_LIVE_WALLET is used as it
    # is: its alias names the server. Otherwise the committed fixture wallet is
    # copied with its alias aimed at the TCPS bed.
    if os.environ.get('SEERDB_LIVE_WALLET'):
        return os.environ['SEERDB_LIVE_WALLET']
    path = wallet_naming_port(
        int(os.environ.get('SEERDB_LIVE_TCPS_PORT', '2484')),
        host=os.environ.get(
            'SEERDB_LIVE_HOST', os.environ.get('SEERDB_TEST_HOST', '127.0.0.1')
        ),
        service=os.environ.get('SEERDB_LIVE_SERVICE', 'FREEPDB1'),
    )
    test.addCleanup(shutil.rmtree, path)
    return path


@unittest.skipUnless(
    os.environ.get('SEERDB_WALLET_LIVE'),
    'set SEERDB_WALLET_LIVE to run the live wallet mTLS test',
)
class TestWalletLive(unittest.TestCase):
    def test_sync_select_over_live_wallet_mtls(self):
        Conn = seerdb.connect(**_cfg(_wallet(self)))
        try:
            Cur = Conn.cursor()
            Cur.execute("SELECT 'wallet-mtls' FROM dual")
            Row = Cur.fetchone()
        finally:
            Conn.close()
        self.assertEqual(Row, ('wallet-mtls',))

    def test_async_select_over_live_wallet_mtls(self):
        Wallet = _wallet(self)

        async def run():
            Conn = await seerdb.connect_async(**_cfg(Wallet))
            async with Conn:
                Cur = Conn.cursor()
                await Cur.execute("SELECT 'wallet-mtls' FROM dual")
                return await Cur.fetchone()

        self.assertEqual(asyncio.run(run()), ('wallet-mtls',))


if __name__ == '__main__':
    unittest.main()
