# SPDX-FileCopyrightText: 2025 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT
"""The 9i DML status reply, captured live (#711).

The reply is an RPA piggyback of two parameters, then the short OER. The first
parameter is a counter that grows with the instance; once it passes 2**24 its
length byte is 0x04, the OER token, and a decoder that stopped the parameter
loop at a token-looking byte read the counter as the status: every successful
CREATE, INSERT and DROP on 9i raised a garbled negative ORA code.
"""

import unittest

from seerdb.common.tns import decode_fv2_dml_response

# Captured from 9.2.0.4: the counter 0x0129c868 is the first RPA parameter.
_CREATE = bytes.fromhex(
    '080102040129c868000400000000010100010000000000000000000000000000010100000000'
)
_INSERT = bytes.fromhex(
    '080102040129c79d000401010000000101010c020000000000028dca01010002baba00000000000101010d0d0100008dca00010000baba00000000'
)
_DROP = bytes.fromhex(
    '080102040129c8750004000000000101010b0c0000000000000000000000000000010100000000'
)
# The same shape while the counter still fitted three bytes.
_CREATE_YOUNG = bytes.fromhex(
    '08010203029c6800040000000001010001000000000000000000000000000001010000'
)


class TestNineiStatusWithAWideCounter(unittest.TestCase):
    def test_a_successful_create(self):
        self.assertEqual(decode_fv2_dml_response(_CREATE), (0, 0))

    def test_a_successful_insert_reports_its_row(self):
        self.assertEqual(decode_fv2_dml_response(_INSERT), (1, 0))

    def test_a_successful_drop(self):
        self.assertEqual(decode_fv2_dml_response(_DROP), (0, 0))

    def test_a_young_instance_still_decodes(self):
        self.assertEqual(decode_fv2_dml_response(_CREATE_YOUNG), (0, 0))


# #1079: the short status carries the rowid of the row the statement touched,
# in the 10g+ layout. Captured on 9.2 (the third of three one-row INSERTs, then
# a CREATE INDEX) and on 8.1.7 (the third INSERT, then a failed statement). The
# rowids are Oracle's own ROWIDTOCHAR of those rows.
_INSERT_SLOT_2 = bytes.fromhex(
    '0801020401955943000401010000000101010c02000000000003016624010100'
    '02b9120102000000000101010d0d010001662400010000b9120002000000'
)
_DDL_AFTER_IT = bytes.fromhex(
    '080102040195594e000400000000010101180900000000000301662401010002'
    'b912010200000000010100000000'
)
_8I_INSERT_SLOT_2 = bytes.fromhex(
    '0804004a12100000000000070000000100000000000000040100000000000000'
    '000007000c00020000000000311d0100050000cf000000020000000000000d00'
    '00010000000d000d0100011d310005000000cf00020000000000000000000000'
    '00'
)


class TestTheRowidInTheShortStatus(unittest.TestCase):
    def test_9i_reports_the_inserted_row(self):
        from seerdb.common.tns import decode_fv2_dml_rowid

        self.assertEqual(decode_fv2_dml_rowid(_INSERT_SLOT_2), 'AAAWYkAABAAALkSAAC')
        # The #711 capture's INSERT: data object 36298, file 1, block 47802, slot 0.
        self.assertEqual(decode_fv2_dml_rowid(_INSERT), 'AAAI3KAABAAALq6AAA')

    def test_9i_leaves_the_last_rowid_in_a_ddl_reply(self):
        # The server does not clear the field: a DDL (row count 0) still carries
        # the INSERT's rowid, which the cursor drops for touching no row.
        from seerdb.common.tns import decode_fv2_dml_response, decode_fv2_dml_rowid

        self.assertEqual(decode_fv2_dml_response(_DDL_AFTER_IT), (0, 0))
        self.assertEqual(decode_fv2_dml_rowid(_DDL_AFTER_IT), 'AAAWYkAABAAALkSAAC')

    def test_9i_statements_that_touched_nothing_carry_no_rowid(self):
        from seerdb.common.tns import decode_fv2_dml_rowid

        for reply in (_CREATE, _DROP, _CREATE_YOUNG, b''):
            self.assertIsNone(decode_fv2_dml_rowid(reply))

    def test_8i_reports_the_inserted_row(self):
        from seerdb.common.tns import decode_8i_dml_response, decode_8i_dml_rowid

        self.assertEqual(decode_8i_dml_response(_8I_INSERT_SLOT_2)[:2], (1, 0))
        self.assertEqual(decode_8i_dml_rowid(_8I_INSERT_SLOT_2), 'AAAR0xAAFAAAADPAAC')

    def test_8i_error_carries_no_rowid(self):
        from seerdb.common.tns import decode_8i_dml_rowid

        failed = (
            bytes.fromhex('0400000000ae03')
            + bytes(40)
            + b'(ORA-00942: table or view does not exist\n'
        )
        self.assertIsNone(decode_8i_dml_rowid(failed))
