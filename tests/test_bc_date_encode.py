# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""A date before year 1 can be served: ``BcDate`` and its wire form (#1071).

No ``datetime`` can hold a year before 1, so a Mirror backend had nothing to
hand the encoder for one. The expected bytes are Oracle's own, taken with
``DUMP(col, 16)`` of stored DATE (Typ=12) and TIMESTAMP (Typ=180) columns on 23ai
-- not produced by seerdb's decoder, which could only confirm a shared mistake.
"""

import datetime
import unittest

from seerdb.common.datatypes import BcDate
from seerdb.common.exceptions import DataError, DateOutOfRangeError
from seerdb.common.tns import _encode_temporal, encode_value
from seerdb.common.tns_consts import (
    TNS_TYPE_DATE,
    TNS_TYPE_TIMESTAMP,
    TNS_TYPE_TIMESTAMPTZ,
)
from seerdb.common.types import decode_date
from seerdb.server import BcDate as ServerBcDate

# (value, stored DATE bytes, stored TIMESTAMP bytes), from DUMP on 23ai. Across
# the century boundaries: -1 is century 0, -100 and -101 straddle one.
DUMPED = [
    (BcDate(-4712, 1, 1), '35580101010101', None),
    (BcDate(-4712, 12, 31, 23, 59, 58), '35580c1f183c3b', None),
    (BcDate(-1, 12, 31, 13, 14, 15), '64630c1f0e0f10', None),
    (BcDate(-100, 6, 15), '6364060f010101', None),
    (BcDate(-101, 6, 15), '6363060f010101', None),
    (
        BcDate(-44, 3, 15, 12, 30, 45, 123456),
        '6438030f0d1f2e',
        '6438030f0d1f2e075bca00',
    ),
]


class TestBcDateWireForm(unittest.TestCase):
    def test_a_date_column_gets_oracles_seven_bytes(self):
        for value, date_hex, _ in DUMPED:
            with self.subTest(value=value):
                self.assertEqual(_encode_temporal(value, TNS_TYPE_DATE).hex(), date_hex)

    def test_a_timestamp_column_carries_the_fraction(self):
        # Oracle stores a zero fraction in 7 bytes, but a TIMESTAMP column is a
        # fixed 11 on the wire, as it is for a datetime.
        (value, date_hex, stamp_hex) = DUMPED[-1]
        self.assertEqual(_encode_temporal(value, TNS_TYPE_TIMESTAMP).hex(), stamp_hex)
        self.assertEqual(
            _encode_temporal(BcDate(-4712, 1, 1), TNS_TYPE_TIMESTAMP).hex(),
            '35580101010101' + '00000000',
        )

    def test_the_row_encoder_takes_it(self):
        # What a backend's row value goes through: length-prefixed, like any date.
        self.assertEqual(
            encode_value(BcDate(-4712, 1, 1), TNS_TYPE_DATE).hex(), '0735580101010101'
        )

    def test_the_client_reads_it_as_the_real_servers_bc_date(self):
        # The client meets these bytes exactly as it would from Oracle, and says
        # so: out of range, not malformed (#1060).
        with self.assertRaises(DateOutOfRangeError):
            decode_date(_encode_temporal(BcDate(-4712, 1, 1), TNS_TYPE_DATE))

    def test_a_time_zone_is_refused_rather_than_invented(self):
        with self.assertRaises(DataError):
            _encode_temporal(BcDate(-4712, 1, 1), TNS_TYPE_TIMESTAMPTZ)

    def test_an_ad_datetime_is_unchanged(self):
        self.assertEqual(
            _encode_temporal(datetime.datetime(2024, 6, 15), TNS_TYPE_DATE).hex(),
            '787c060f010101',
        )


class TestBcDateValue(unittest.TestCase):
    def test_only_years_a_datetime_cannot_hold(self):
        for year in (0, 1, 2024, -4713):
            with self.subTest(year=year), self.assertRaises(ValueError):
                BcDate(year, 1, 1)

    def test_fields_are_range_checked(self):
        for args in ((-1, 13, 1), (-1, 1, 32), (-1, 1, 1, 24), (-1, 1, 1, 0, 0, 60)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                BcDate(*args)

    def test_value_semantics(self):
        self.assertEqual(BcDate(-44, 3, 15), BcDate(-44, 3, 15))
        self.assertNotEqual(BcDate(-44, 3, 15), BcDate(-44, 3, 16))
        self.assertEqual(len({BcDate(-44, 3, 15), BcDate(-44, 3, 15)}), 1)
        self.assertEqual(repr(BcDate(-44, 3, 15)), 'BcDate(-44, 3, 15, 0, 0, 0, 0)')

    def test_backends_find_it_in_the_server_api(self):
        self.assertIs(ServerBcDate, BcDate)


if __name__ == '__main__':
    unittest.main()
