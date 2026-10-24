# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""When the 8i row reader reads on, and when it lets an error through (#1061).

8i caps each DATA packet at the SDU with no end-of-message flag, so a message
larger than one packet arrives with nothing saying more is coming (#377). The
reader therefore decodes what it has and, if a field runs off the end, asks for
the next packet. That is only right for a field that RAN OFF THE END.

It used to catch the whole DataError family as "read on". A value that is
complete but does not decode -- a BC date, valid Oracle data that no Python
datetime can hold -- raised a DataError too, so the reader asked for a packet
that was never coming and the fetch hung for the full read timeout.

These drive the reader's generator with the decoder stubbed to raise each kind
of exception, so they test the reader's policy rather than a hand-built frame
(which would only encode the assumption under test). The live test in
test_integration exercises the real bytes on 8i.
"""

import unittest
from unittest import mock

from seerdb.client.dialect import RECV, O8iDialect
from seerdb.common.exceptions import DataError, DateOutOfRangeError, Truncated


def _start(side_effect):
    # A recv_rows generator whose decoder raises `side_effect`, primed up to its
    # first yield. Returns the generator and what it yielded.
    dialect = O8iDialect(next_seq=lambda: 1)
    patch = mock.patch(
        'seerdb.client.dialect.decode_8i_exec_response', side_effect=side_effect
    )
    patch.start()
    gen = dialect.recv_rows(b'partial', [], None)
    try:
        return gen, next(gen), patch
    except BaseException:
        patch.stop()
        raise


class TestReadOnOnlyForTruncation(unittest.TestCase):
    def test_a_truncated_field_reads_on(self):
        # Genuinely incomplete: ask for the next packet.
        gen, first, patch = _start(Truncated('ran off the end'))
        try:
            self.assertIs(first, RECV)
        finally:
            patch.stop()

    def test_an_index_error_from_an_older_primitive_reads_on(self):
        gen, first, patch = _start(IndexError('index out of range'))
        try:
            self.assertIs(first, RECV)
        finally:
            patch.stop()

    def test_a_complete_but_undecodable_value_raises_instead_of_waiting(self):
        # The #1061 case. Used to yield RECV here and wait for the read timeout.
        with self.assertRaises(DateOutOfRangeError):
            _start(DateOutOfRangeError('year -4712 is out of range'))

    def test_any_other_data_error_raises_too(self):
        # Not only dates: a corrupt value in a complete message is an error to
        # report, not a sign that more bytes are on the way.
        with self.assertRaises(DataError) as caught:
            _start(DataError('malformed Oracle NUMBER'))
        self.assertNotIsInstance(caught.exception, Truncated)

    def test_a_connection_closed_on_a_truncated_message_returns_no_rows(self):
        # Nothing more is coming, so a genuinely incomplete message has no rows.
        gen, first, patch = _start(Truncated('ran off the end'))
        try:
            self.assertIs(first, RECV)
            with self.assertRaises(StopIteration) as done:
                gen.send(False)  # the connection closed
            self.assertEqual(done.exception.value, ([], b'', None))
        finally:
            patch.stop()

    def test_a_connection_closed_on_a_bad_value_still_raises(self):
        # The first decode reads on (truncated); after the close, the retry finds
        # a complete-but-bad value, which must raise rather than become "no rows".
        errors = [Truncated('short'), DataError('malformed Oracle DATE')]
        gen, first, patch = _start(errors)
        try:
            self.assertIs(first, RECV)
            with self.assertRaises(DataError):
                gen.send(False)
        finally:
            patch.stop()


class TestReadOnUntilTheStatusIsWhole(unittest.TestCase):
    """A reply cut a few bytes short of its status reads on (#2280).

    8i splits a long reply into packets at no fixed point. Fetching a 50 000-byte
    LONG RAW, it once sent all but the last 3 bytes, then those 3 (`00 00 00`)
    in a packet of their own. The status ends in zero fields, so the short reply
    decoded cleanly: the reader stopped, the 3 bytes answered the next fetch, and
    that fetch's ORA-01403 answered the DROP after it. Replies captured on 8.1.7.
    """

    # SELECT CAST('X' AS VARCHAR2(1)), CAST(N'Y' AS NVARCHAR2(1)), ... : the
    # describe, one row, then the RPA and the 60-byte status.
    REPLY = bytes.fromhex(
        '1019c3bff959d084a5050000787e0a180c320f395c020000000000060000000300000033'
        '018000000100000000000000000000000000000000001f00010116160000001643415354'
        '282758274153564152434841523228312929000000000000000001800000010000000000'
        '0000000000000000000000001f00020118180000001843415354284e27592741534e5641'
        '524348415232283129290000000000000000608000000400000000000000000000000000'
        '000000001f0002010101000000014300000000000000000700000007787e0a180c321506'
        '020300000001000000000000000000000007015800000000015900000000046162202000'
        '0000000804002ca01d000000000001000000010000000000000004010000000000000000'
        '000100000003004000000000000000000000000000000000000000000007000001000000'
        '0000000000000000000000000000'
    )
    # An INSERT's status: its OER carries the row's 13-byte rowid.
    INSERTED = bytes.fromhex(
        '08040035a01d000000000003000000010000000000000004010000000000000000000300'
        '0c00020000000000272d02000500001501000001000000000000090000010000000d000d'
        '0100022d270005000001150001000000000000000000000000'
    )
    # The end of a fetch: ORA-01403 and its message, with no RPA before it.
    NO_DATA = bytes.fromhex(
        '04010000007b050000000001000000030040000000000000000000000000000000000000'
        '000000080000010000000000000000000000000000000000194f52412d30313430333a20'
        '6e6f206461746120666f756e640a'
    )

    def _rows(self):
        from seerdb.common.tns import decode_8i_dcb_describe

        return decode_8i_dcb_describe(self.REPLY)

    def test_each_status_is_whole_and_not_a_byte_short(self):
        from seerdb.common.tns import decode_8i_exec_response, o8i_terminal_complete

        (columns, rows) = self._rows()
        (_, terminal, _) = decode_8i_exec_response(rows, columns)
        for status in (terminal, self.INSERTED, self.NO_DATA):
            self.assertTrue(o8i_terminal_complete(status), status.hex())
            for short in range(1, len(status)):
                self.assertFalse(o8i_terminal_complete(status[:-short]), short)

    def test_a_reply_three_bytes_short_waits_for_the_rest(self):
        (columns, rows) = self._rows()
        gen = O8iDialect(next_seq=lambda: 1).recv_rows(rows[:-3], columns, None)
        # Before the fix the short reply returned here, three bytes unread.
        self.assertIs(next(gen), RECV)
        with self.assertRaises(StopIteration) as done:
            gen.send((6, rows[-3:]))
        (got, terminal, _) = done.exception.value
        self.assertEqual(got, [['X', 'Y', 'ab  ']])
        self.assertEqual(len(terminal), 83)

    def test_a_whole_reply_reads_no_further(self):
        (columns, rows) = self._rows()
        with self.assertRaises(StopIteration) as done:
            next(O8iDialect(next_seq=lambda: 1).recv_rows(rows, columns, None))
        self.assertEqual(done.exception.value[0], [['X', 'Y', 'ab  ']])


if __name__ == '__main__':
    unittest.main()
