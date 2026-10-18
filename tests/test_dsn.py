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
from seerdb.client.dsn import DsnTarget, parse_dsn, resolve_dsn_arguments
from seerdb.common.exceptions import NotSupportedError, ProgrammingError


class ParseDsnTarget(unittest.TestCase):
    def test_the_forms_it_reads(self):
        cases = {
            'db:1523/FREEPDB1': DsnTarget('db', 1523, 'FREEPDB1'),
            'db/FREEPDB1': DsnTarget('db', 1521, 'FREEPDB1'),
            '//db:1523/FREEPDB1': DsnTarget('db', 1523, 'FREEPDB1'),
            'db:1523': DsnTarget('db', 1523, ''),
            '//db': DsnTarget('db', 1521, ''),
            '[::1]:1523/FREEPDB1': DsnTarget('::1', 1523, 'FREEPDB1'),
            '[::1]': DsnTarget('::1', 1521, ''),
            'scott/tiger@db:1523/FREEPDB1': DsnTarget(
                'db', 1523, 'FREEPDB1', 'scott', 'tiger'
            ),
            'scott@db/FREEPDB1': DsnTarget('db', 1521, 'FREEPDB1', 'scott', None),
            # A password may hold '/' and '@'; the address cannot.
            'scott/p/w@x@db/FREEPDB1': DsnTarget(
                'db', 1521, 'FREEPDB1', 'scott', 'p/w@x'
            ),
        }
        for dsn, expected in cases.items():
            with self.subTest(dsn=dsn):
                self.assertEqual(parse_dsn(dsn), expected)

    def test_a_form_not_read_yet_is_refused_not_dropped(self):
        for dsn in (
            'mydb',  # a tnsnames alias
            'tcps://db:2484/FREEPDB1',
            'db:1523/FREEPDB1?connect_timeout=2',
            'db:1523/FREEPDB1:dedicated',
        ):
            with self.subTest(dsn=dsn), self.assertRaises(NotSupportedError) as ctx:
                parse_dsn(dsn)
            self.assertIn(dsn, str(ctx.exception))

    def test_a_malformed_dsn_is_a_programming_error(self):
        for dsn in ('db:x/FREEPDB1', '@db/FREEPDB1', '[::1/FREEPDB1', ''):
            with self.subTest(dsn=dsn), self.assertRaises(ProgrammingError):
                parse_dsn(dsn)


_ADDR = '(ADDRESS=(PROTOCOL=TCP)(HOST=db)(PORT=1523))'


class ParseDescriptor(unittest.TestCase):
    # A connect descriptor with one address (#1976). The shapes python-oracledb
    # 26.0.1 connects with, measured against 23ai, minus the ones refused below.
    def test_the_shapes_it_reads(self):
        cases = {
            f'(DESCRIPTION={_ADDR}(CONNECT_DATA=(SERVICE_NAME=svc)))': DsnTarget(
                'db', 1523, 'svc'
            ),
            # Keys are case-insensitive, whitespace is free.
            '(description = (address = (protocol = tcp)(host = db)(port = 1523))'
            ' (connect_data = (service_name = svc)))': DsnTarget('db', 1523, 'svc'),
            f'(DESCRIPTION={_ADDR}(CONNECT_DATA=(SID=ORCL)))': DsnTarget(
                'db', 1523, '', sid='ORCL'
            ),
            # No PROTOCOL means TCP, no PORT means 1521.
            '(DESCRIPTION=(ADDRESS=(HOST=db))(CONNECT_DATA=(SERVICE_NAME=svc)))': (
                DsnTarget('db', 1521, 'svc')
            ),
            f'(DESCRIPTION=(ADDRESS_LIST={_ADDR})(CONNECT_DATA=(SERVICE_NAME=svc)))': (
                DsnTarget('db', 1523, 'svc')
            ),
            f'(DESCRIPTION_LIST=(DESCRIPTION={_ADDR}(CONNECT_DATA=(SERVICE_NAME=svc))))': (
                DsnTarget('db', 1523, 'svc')
            ),
            # CID describes the client, FAILOVER means nothing with one address.
            f'(DESCRIPTION=(FAILOVER=ON){_ADDR}(CONNECT_DATA=(SERVICE_NAME=svc)'
            '(CID=(PROGRAM=app)(HOST=me)(USER=u))))': DsnTarget('db', 1523, 'svc'),
            f'scott/tiger@(DESCRIPTION={_ADDR}(CONNECT_DATA=(SERVICE_NAME=svc)))': (
                DsnTarget('db', 1523, 'svc', 'scott', 'tiger')
            ),
            # An '@' inside the descriptor is not a credentials separator.
            f'(DESCRIPTION={_ADDR}(CONNECT_DATA=(SERVICE_NAME=svc)(CID=(USER="a@b"))))': (
                DsnTarget('db', 1523, 'svc')
            ),
        }
        for dsn, expected in cases.items():
            with self.subTest(dsn=dsn):
                self.assertEqual(parse_dsn(dsn), expected)

    def test_what_seerdb_would_have_to_act_on_is_refused_by_name(self):
        cases = {
            f'(DESCRIPTION={_ADDR}{_ADDR}(CONNECT_DATA=(SERVICE_NAME=s)))': 'several',
            f'(DESCRIPTION_LIST=(DESCRIPTION={_ADDR})(DESCRIPTION={_ADDR}))': 'several',
            '(DESCRIPTION=(ADDRESS=(PROTOCOL=TCPS)(HOST=db)(PORT=2484)))': 'TCPS',
            f'(DESCRIPTION=(RETRY_COUNT=3){_ADDR})': 'RETRY_COUNT',
            f'(DESCRIPTION={_ADDR}(SECURITY=(SSL_SERVER_DN_MATCH=yes)))': 'SECURITY',
            f'(DESCRIPTION={_ADDR}(CONNECT_DATA=(SERVICE_NAME=s)(SERVER=DEDICATED)))': (
                'SERVER'
            ),
            '(DESCRIPTION=(ADDRESS=(HOST=db)(HTTPS_PROXY=p)))': 'HTTPS_PROXY',
        }
        for dsn, named in cases.items():
            with self.subTest(dsn=dsn), self.assertRaises(NotSupportedError) as ctx:
                parse_dsn(dsn)
            self.assertIn(named, str(ctx.exception))

    def test_a_malformed_descriptor_is_a_programming_error(self):
        for dsn in (
            f'(DESCRIPTION={_ADDR}',  # unbalanced
            '(DESCRIPTION=(CONNECT_DATA=(SERVICE_NAME=s)))',  # no address
            '(DESCRIPTION=(ADDRESS=(PORT=1)))',  # no host
            '(DESCRIPTION=(ADDRESS=(HOST=db)(PORT=x)))',
            f'(DESCRIPTION={_ADDR}) trailing',
            f'(ADDRESS_LIST={_ADDR})',  # not a DESCRIPTION
        ):
            with self.subTest(dsn=dsn), self.assertRaises(ProgrammingError):
                parse_dsn(dsn)

    def test_a_sid_in_the_descriptor_replaces_a_service_passed(self):
        self.assertEqual(
            resolve_dsn_arguments(
                f'(DESCRIPTION={_ADDR}(CONNECT_DATA=(SID=ORCL)))',
                'localhost',
                1521,
                'svc',
                '',
                'u',
                'p',
            ),
            ('db', 1523, '', 'ORCL', 'u', 'p'),
        )


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

    def test_both_connection_classes_take_a_descriptor(self):
        for cls in (OracleConnect, AsyncOracleConnect):
            with self.subTest(cls=cls.__name__):
                conn = cls(
                    user='pyo',
                    password='p',
                    dsn=f'(DESCRIPTION={_ADDR}(CONNECT_DATA=(SID=ORCL)))',
                )
                self.assertEqual(
                    (conn.host, conn.port, conn.service_name, conn.sid),
                    ('db', 1523, '', 'ORCL'),
                )

    def test_a_proxy_user_in_the_dsn_is_split_as_in_user(self):
        for cls in (OracleConnect, AsyncOracleConnect):
            with self.subTest(cls=cls.__name__):
                conn = cls(dsn='proxy[schema]/p@db/FREEPDB1')
                self.assertEqual((conn.user, conn.proxy_user), ('proxy', 'schema'))
                self.assertEqual(conn.password, 'p')


if __name__ == '__main__':
    unittest.main()
