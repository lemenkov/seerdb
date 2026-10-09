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


class BfileFileOpenReply(unittest.TestCase):
    """The Mirror's answer to FILE_OPEN (#1672).

    The client sends the open mode as an amount and reads one back for any op
    that sent one::

        elif self.send_amount:
            buf.read_sb8(&self.amount)

    Answered with the locator alone, it took the OER's first byte for the
    amount and desynced -- ``DPY-5000: unknown protocol message type 0`` -- so
    no BFILE could be read.
    """

    # python-oracledb 4.0.1 opening PYO_BFILE_DIR/pyoracle_bfile_test.txt on a
    # live 23ai, read-only (mode 11), and what 23ai answered up to its OER.
    REQUEST = bytes.fromhex(
        '0360070001013800000000000000020100000000000100000000000000360001080800'
        '000001000000000000000d50594f5f4246494c455f444952001770796f7261636c655f'
        '6266696c655f746573742e747874010b'
    )
    REPLY = bytes.fromhex(
        '0800360001080800000001000100000000000d50594f5f4246494c455f444952001770'
        '796f7261636c655f6266696c655f746573742e747874010b'
    )

    def _reply(self) -> bytes:
        from typing import Any

        from seerdb.common.tns import _DECODE_FIELD_VERSION, parse_lobops_request
        from seerdb.server.session import _answer_bfile

        class _Backend:
            def bfile_exists(self, directory, filename):
                return True

        class _Stream:
            def __init__(self):
                self.body = b''

            def write_packet(self, kind, body):
                self.body = body

        token = _DECODE_FIELD_VERSION.set(24)
        try:
            request = parse_lobops_request(self.REQUEST)
            self.assertEqual(request.kind, 'file_open')
            self.assertEqual(request.amount, 11)
            stream: Any = _Stream()
            backend: Any = _Backend()
            _answer_bfile(stream, backend, request)
            return stream.body
        finally:
            _DECODE_FIELD_VERSION.reset(token)

    def test_the_mode_follows_the_locator(self):
        body = self._reply()
        # 23ai flags the locator open (the byte at offset 12 of the reply);
        # the Mirror holds nothing open and hands it back as it came.
        self.assertEqual(
            body[:12] + body[13 : len(self.REPLY)], self.REPLY[:12] + self.REPLY[13:]
        )
        self.assertEqual(body[12], 0)
        self.assertEqual(body[len(self.REPLY)], 4)  # then the OER


class BfileContentReply(unittest.TestCase):
    """The Mirror's answer to GET_LENGTH and READ on a BFILE (#1672), from the
    backend's ``bfile_length`` / ``bfile_read`` hooks, in the shapes 23ai
    answers ``size()`` and ``read()`` of PYO_BFILE_DIR/pyoracle_bfile_test.txt.
    """

    # The locator FIELD (ub2 + body) both requests carry, as the Mirror's
    # FILE_OPEN handed it back -- 23ai's own READ carries it open-flagged.
    FIELD = bytes.fromhex(
        '00360001080800000001000000000000000d50594f5f4246494c455f444952001770'
        '796f7261636c655f6266696c655f746573742e747874'
    )
    GET_LENGTH = bytes.fromhex('036005000101380000000000000001010000000001000000000000')
    READ = bytes.fromhex('03600800010138000000000000000102000001010001000000000000')
    DATA = b'hello bfile from disk'

    def _reply(self, request: bytes, backend: object) -> bytes:
        from typing import Any

        from seerdb.common.tns import _DECODE_FIELD_VERSION
        from seerdb.server.session import _answer_lobops, _TempLobs

        class _Stream:
            def __init__(self):
                self.body = b''

            def write_packet(self, kind, body):
                self.body = body

        token = _DECODE_FIELD_VERSION.set(24)
        try:
            stream: Any = _Stream()
            _answer_lobops(stream, request, [], _TempLobs(), backend=backend)  # type: ignore[arg-type]
            return stream.body
        finally:
            _DECODE_FIELD_VERSION.reset(token)

    def _backend(self) -> object:
        data = self.DATA

        class _Backend:
            calls: list[tuple] = []

            def bfile_length(self, directory, filename):
                self.calls.append(('length', directory, filename))
                return len(data)

            def bfile_read(self, directory, filename, offset, amount):
                self.calls.append(('read', directory, filename, offset, amount))
                return data[offset - 1 : offset - 1 + amount]

        return _Backend()

    def test_size(self):
        backend = self._backend()
        body = self._reply(self.GET_LENGTH + self.FIELD + b'\x00', backend)
        self.assertEqual(
            body[: 1 + len(self.FIELD) + 2], b'\x08' + self.FIELD + b'\x01\x15'
        )
        self.assertEqual(
            backend.calls,
            [('length', 'PYO_BFILE_DIR', 'pyoracle_bfile_test.txt')],  # type: ignore[attr-defined]
        )

    def test_read(self):
        backend = self._backend()
        body = self._reply(
            self.READ + self.FIELD + bytes.fromhex('04ffffffff'), backend
        )
        # 23ai frames the bytes in the long form (0e fe 01 15 <data> 00); the
        # short one every Mirror LOB read uses reads the same.
        expected = b'\x0e\x15' + self.DATA + b'\x08' + self.FIELD + b'\x01\x15'
        self.assertEqual(body[: len(expected)], expected)
        self.assertEqual(
            backend.calls,  # type: ignore[attr-defined]
            [('read', 'PYO_BFILE_DIR', 'pyoracle_bfile_test.txt', 1, 0xFFFFFFFF)],
        )

    def test_a_backend_without_the_hooks_refuses(self):
        body = self._reply(self.GET_LENGTH + self.FIELD + b'\x00', object())
        self.assertNotIn(b'\x08' + self.FIELD, body)
