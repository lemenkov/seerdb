# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Native network encryption against a live server that requires it (#1345).

Needs a server with encryption and data integrity REQUIRED and out-of-band
breaks disabled, so SQL errors travel in-band as a break/reset exchange. Runs
only when pointed at one:

  SEERDB_TEST_ANO_HOST / SEERDB_TEST_ANO_PORT / SEERDB_TEST_ANO_SERVICE
  SEERDB_TEST_ANO_USER / SEERDB_TEST_ANO_PASSWORD (default pyo / pyo123)

The account needs only CREATE SESSION: every statement here is a query.
"""

import asyncio
import os
import unittest

import seerdb
from seerdb.common.exceptions import DatabaseError

_HOST = os.environ.get('SEERDB_TEST_ANO_HOST')
_KWARGS = dict(
    host=_HOST,
    port=int(os.environ.get('SEERDB_TEST_ANO_PORT', '1521')),
    service_name=os.environ.get('SEERDB_TEST_ANO_SERVICE', 'FREEPDB1'),
    user=os.environ.get('SEERDB_TEST_ANO_USER', 'pyo'),
    password=os.environ.get('SEERDB_TEST_ANO_PASSWORD', 'pyo123'),
)
_MISSING = 'select * from seerdb_no_such_table_1345'


@unittest.skipUnless(_HOST, 'SEERDB_TEST_ANO_HOST names no encrypted server')
class TestEncryptedSessionSurvivesErrors(unittest.TestCase):
    def test_an_error_does_not_end_the_session(self):
        # A SQL error arrives as a break/reset exchange; after it both sides
        # re-derive their integrity keystreams. A client that did not failed the
        # error's MAC and lost the session (#1345). Twice, since each episode
        # re-derives again.
        conn = seerdb.connect(**_KWARGS)
        try:
            self.assertTrue(conn._ano is not None and conn._ano.active)
            cur = conn.cursor()
            for n in (1, 2):
                with self.assertRaises(DatabaseError) as raised:
                    cur.execute(_MISSING)
                self.assertEqual(raised.exception.code, 942)
                cur.execute('select :1 from dual', [n])
                self.assertEqual(cur.fetchone(), (n,))
        finally:
            conn.close()

    def test_an_error_does_not_end_the_async_session(self):
        async def run():
            conn = await seerdb.connect_async(**_KWARGS)
            try:
                self.assertTrue(conn._ano is not None and conn._ano.active)
                cur = conn.cursor()
                for n in (1, 2):
                    with self.assertRaises(DatabaseError) as raised:
                        await cur.execute(_MISSING)
                    self.assertEqual(raised.exception.code, 942)
                    await cur.execute('select :1 from dual', [n])
                    self.assertEqual(await cur.fetchone(), (n,))
            finally:
                await conn.close()

        asyncio.run(run())
