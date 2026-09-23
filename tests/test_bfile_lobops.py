# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Offline tests for native BFILE TTI_LOBOPS encoding (#46).

A BFILE is read natively over TTI_LOBOPS — FILE_OPEN -> READ -> FILE_CLOSE —
rather than through a PL/SQL helper. These pin the request bytes reverse-
engineered from python-oracledb on 21c (docs/PROTOCOL.md §14); no server needed.
"""

import unittest

from seerdb.common.tns import encode_dictionary_lobops
from seerdb.common.tns_consts import (
    TNS_LOB_OP_FILE_CLOSE,
    TNS_LOB_OP_FILE_OPEN,
    TNS_LOB_OP_READ,
)

# The 54-byte BFILE locator body (no leading ub2 length) captured on 21c for
# DIRECTORY "PYO_BFILE_DIR" / file "pyoracle_bfile_test.txt".
LOC = bytes.fromhex(
    '0001080800000001000000000000000d50594f5f4246494c455f4449'
    '52001770796f7261636c655f6266696c655f746573742e747874'
)


class BfileLobopsEncode(unittest.TestCase):
    def test_file_open(self):
        # op 0x0100; amount pointer set with the read-only mode (sb4 0x0B);
        # source offset 0; ub2-prefixed locator (declared length + 2).
        Out = encode_dictionary_lobops(
            {'seq': 6, 'operation': TNS_LOB_OP_FILE_OPEN, 'locator': LOC}
        )
        self.assertEqual(
            Out.hex(),
            '0360060101380000000000000002010000000000010000000000000036'
            + LOC.hex()
            + '010b',
        )

    def test_file_close(self):
        # op 0x0200; no amount, no trailing data.
        Out = encode_dictionary_lobops(
            {'seq': 8, 'operation': TNS_LOB_OP_FILE_CLOSE, 'locator': LOC}
        )
        self.assertEqual(
            Out.hex(),
            '0360080101380000000000000002020000000000000000000000000036' + LOC.hex(),
        )

    def test_read_prefixed(self):
        # The READ of an opened BFILE reuses the READ opcode with the
        # ub2-prefixed locator form (source offset 1, amount 0xFFFFFFFF).
        Out = encode_dictionary_lobops(
            {
                'seq': 7,
                'operation': TNS_LOB_OP_READ,
                'locator': LOC,
                'locator_prefixed': True,
                'amount': 0xFFFFFFFF,
            }
        )
        self.assertEqual(
            Out.hex(),
            '0360070101380000000000000001020000010100010000000000000036'
            + LOC.hex()
            + '04ffffffff',
        )


if __name__ == '__main__':
    unittest.main()


class BfileFileExistsReply(unittest.TestCase):
    """The Mirror's answer to FILE_EXISTS (#1120).

    The reference client reads ``len(locator)`` raw bytes and then ONE ub1::

        if self.operation in (TNS_LOB_OP_IS_OPEN, TNS_LOB_OP_FILE_EXISTS,
                              TNS_LOB_OP_FILE_ISOPEN):
            buf.read_ub1(&temp8)
            self.bool_flag = temp8 > 0

    and a BFILE locator's length includes its own leading ub2 -- so the reply
    owes the whole FIELD, not the body inside it. Short by those two bytes, the
    boolean is read from inside the locator: our own client reported a file that
    is there as missing, and python-oracledb desynced outright with
    ``DPY-5000: unknown protocol message type 1 at position 68``.
    """

    # A real FILE_EXISTS request, captured off python-oracledb 4.0.1 asking a
    # live 23ai about PYO_BFILE_DIR/pyoracle_bfile_test.txt.
    REQUEST = bytes.fromhex(
        '03600500010138000000000000010208000000000000000000000000003600010808'
        '00000001000000000000000d50594f5f4246494c455f444952001770796f7261636c'
        '655f6266696c655f746573742e747874'
    )

    def _reply(self, present: bool) -> bytes:
        from typing import Any

        from seerdb.common.tns import _DECODE_FIELD_VERSION, parse_lobops_request
        from seerdb.server.session import _answer_bfile

        class _Backend:
            def bfile_exists(self, directory, filename):
                return present

        class _Stream:
            def __init__(self):
                self.body = b''

            def write_packet(self, kind, body):
                self.body = body

        token = _DECODE_FIELD_VERSION.set(24)
        try:
            request = parse_lobops_request(self.REQUEST)
            self.assertEqual(request.kind, 'file_exists')
            stream: Any = _Stream()
            backend: Any = _Backend()
            _answer_bfile(stream, backend, request)
            return stream.body
        finally:
            _DECODE_FIELD_VERSION.reset(token)

    def test_the_locator_field_is_echoed_whole(self):
        # 54 bytes of locator arrive behind their own ub2, so 56 go back -- and
        # the flag is the byte after all 56.
        from seerdb.common.tns_consts import TTI_RPA

        body = self._reply(True)
        self.assertEqual(body[0], TTI_RPA)
        field = self.REQUEST[-56:]
        self.assertEqual(field[:2], b'\x00\x36')  # the locator's own ub2
        self.assertEqual(body[1:57], field)
        self.assertEqual(body[57], 1)

    def test_a_missing_file_answers_zero_in_the_same_place(self):
        body = self._reply(False)
        self.assertEqual(body[1:57], self.REQUEST[-56:])
        self.assertEqual(body[57], 0)
