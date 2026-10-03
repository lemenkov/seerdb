# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""8i and 9i hand back a fetched BFILE as its locator (#1103).

Reading a BFILE opens the file, so reading it during the fetch made a BFILE
naming a missing directory fail the SELECT itself, where 10g and later return
the locator and only read() fails. The live tests in test_integration exercise
the real exchange; these pin the dialects' side of it.
"""

import unittest

from seerdb.client.dialect import Fv2Dialect, O8iDialect, Send
from seerdb.common.exceptions import DatabaseError
from seerdb.common.lob import LOB
from seerdb.common.tns_consts import TNS_TYPE_BFILE

# What 8.1.7 answers a FILE_OPEN of a BFILE whose directory does not exist: an
# OER whose error is the trailing ORA- text. Captured live.
_O8I_FILE_OPEN_MISSING_DIR = bytes.fromhex(
    '04010000000d570000000000000000030040000000000000000000000000000000000000'
    '00000000080000010000000000000000000000000000000000414f52412d32323238353a'
    '206e6f6e2d6578697374656e74206469726563746f7279206f722066696c6520666f7220'
    '46494c454f50454e206f7065726174696f6e0a'
)


def _run(gen):
    # Drive a dialect generator that must not touch the wire; return its result.
    try:
        intent = next(gen)
    except StopIteration as done:
        return done.value
    raise AssertionError(f'the dialect asked the server for something: {intent!r}')


class TestTheFetchKeepsTheLocator(unittest.TestCase):
    def test_neither_dialect_reads_a_bfile_while_fetching(self):
        for dialect in (Fv2Dialect(), O8iDialect(lambda: 1)):
            lob = LOB(TNS_TYPE_BFILE, b'\x00\x10locator-bytes-xx')
            rows = [[lob]]
            _run(dialect.resolve_lobs(rows, [{'data_type': TNS_TYPE_BFILE}]))
            self.assertIs(rows[0][0], lob, type(dialect).__name__)


class TestO8iFileOpenError(unittest.TestCase):
    def test_a_missing_directory_raises_its_ora_error(self):
        # The 9i error reader found no error in this reply, so the read failed
        # with "Unexpected 8i BFILE FILE_OPEN reply" instead of ORA-22285.
        gen = O8iDialect(lambda: 1).bfile_read(b'locator')
        self.assertIsInstance(next(gen), Send)  # FILE_OPEN
        next(gen)  # RECV
        with self.assertRaises(DatabaseError) as caught:
            gen.send((None, _O8I_FILE_OPEN_MISSING_DIR))
        self.assertEqual(caught.exception.code, 22285)


if __name__ == '__main__':
    unittest.main()
