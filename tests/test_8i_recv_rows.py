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


if __name__ == '__main__':
    unittest.main()
