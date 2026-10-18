# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT
"""An Easy Connect ``dsn`` without a wallet names the address (#1972).

What the parser accepts, which side wins, and that both connection classes take
it. The precedence -- the DSN over host / port / service_name passed beside it
-- is what python-oracledb 26.0.1 does, measured with its ConnectParams.
"""

import unittest

from seerdb.client.aconnection import AsyncOracleConnect
from seerdb.client.connection import OracleConnect
from seerdb.client.dsn import EasyConnect, parse_easy_connect, resolve_dsn_arguments
from seerdb.common.exceptions import NotSupportedError, ProgrammingError


class ParseEasyConnect(unittest.TestCase):
    def test_the_forms_it_reads(self):
        cases = {
            'db:1523/FREEPDB1': EasyConnect('db', 1523, 'FREEPDB1'),
            'db/FREEPDB1': EasyConnect('db', 1521, 'FREEPDB1'),
            '//db:1523/FREEPDB1': EasyConnect('db', 1523, 'FREEPDB1'),
            'db:1523': EasyConnect('db', 1523, ''),
            '//db': EasyConnect('db', 1521, ''),
            '[::1]:1523/FREEPDB1': EasyConnect('::1', 1523, 'FREEPDB1'),
            '[::1]': EasyConnect('::1', 1521, ''),
            'scott/tiger@db:1523/FREEPDB1': EasyConnect(
                'db', 1523, 'FREEPDB1', 'scott', 'tiger'
            ),
            'scott@db/FREEPDB1': EasyConnect('db', 1521, 'FREEPDB1', 'scott', None),
            # A password may hold '/' and '@'; the address cannot.
            'scott/p/w@x@db/FREEPDB1': EasyConnect(
                'db', 1521, 'FREEPDB1', 'scott', 'p/w@x'
            ),
        }
        for dsn, expected in cases.items():
            with self.subTest(dsn=dsn):
                self.assertEqual(parse_easy_connect(dsn), expected)

    def test_a_form_not_read_yet_is_refused_not_dropped(self):
        for dsn in (
            'mydb',  # a tnsnames alias
            '(DESCRIPTION=(ADDRESS=(HOST=db)(PORT=1521)))',
            'tcps://db:2484/FREEPDB1',
            'db:1523/FREEPDB1?connect_timeout=2',
            'db:1523/FREEPDB1:dedicated',
        ):
            with self.subTest(dsn=dsn), self.assertRaises(NotSupportedError) as ctx:
                parse_easy_connect(dsn)
            self.assertIn(dsn, str(ctx.exception))

    def test_a_malformed_dsn_is_a_programming_error(self):
        for dsn in ('db:x/FREEPDB1', '@db/FREEPDB1', '[::1/FREEPDB1', ''):
            with self.subTest(dsn=dsn), self.assertRaises(ProgrammingError):
                parse_easy_connect(dsn)


class ResolveDsnArguments(unittest.TestCase):
    def test_the_dsn_wins_over_host_port_and_service_passed_beside_it(self):
        self.assertEqual(
            resolve_dsn_arguments('db:1523/A', 'other', 2, 'B', '', 'u', 'p'),
            ('db', 1523, 'A', '', 'u', 'p'),
        )

    def test_a_dsn_without_a_port_means_1521(self):
        self.assertEqual(
            resolve_dsn_arguments('db/A', 'localhost', 2, '', '', 'u', 'p')[:2],
            ('db', 1521),
        )

    def test_a_dsn_without_a_service_keeps_the_one_passed(self):
        self.assertEqual(
            resolve_dsn_arguments('db:1523', 'localhost', 1521, 'B', '', 'u', 'p'),
            ('db', 1523, 'B', '', 'u', 'p'),
        )

    def test_a_service_in_the_dsn_replaces_a_sid(self):
        self.assertEqual(
            resolve_dsn_arguments('db/A', 'localhost', 1521, '', 'ORCL', 'u', 'p')[2:4],
            ('A', ''),
        )

    def test_credentials_in_the_dsn_fill_an_absent_user(self):
        self.assertEqual(
            resolve_dsn_arguments(
                'scott/tiger@db/A', 'localhost', 1521, '', '', '', ''
            ),
            ('db', 1521, 'A', '', 'scott', 'tiger'),
        )

    def test_a_user_in_the_dsn_and_in_user_is_refused(self):
        with self.assertRaises(ProgrammingError):
            resolve_dsn_arguments('scott@db/A', 'localhost', 1521, '', '', 'u', '')


class ConnectionTakesTheDsn(unittest.TestCase):
    def test_both_connection_classes_take_the_address_from_the_dsn(self):
        for cls in (OracleConnect, AsyncOracleConnect):
            with self.subTest(cls=cls.__name__):
                conn = cls(user='pyo', password='p', dsn='db.example.com:1523/FREEPDB1')
                self.assertEqual(
                    (conn.host, conn.port, conn.service_name),
                    ('db.example.com', 1523, 'FREEPDB1'),
                )

    def test_a_proxy_user_in_the_dsn_is_split_as_in_user(self):
        for cls in (OracleConnect, AsyncOracleConnect):
            with self.subTest(cls=cls.__name__):
                conn = cls(dsn='proxy[schema]/p@db/FREEPDB1')
                self.assertEqual((conn.user, conn.proxy_user), ('proxy', 'schema'))
                self.assertEqual(conn.password, 'p')


if __name__ == '__main__':
    unittest.main()
