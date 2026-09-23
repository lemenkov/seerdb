# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Exercise the capture-replay harness (#439).

Proves a captured server response — pasted from ``hexdump -C`` output into a
``tests/captures/*.hexdump`` file — can be pushed through seerdb's packet
framing offline, so future decode regressions can be pinned from a Wireshark /
tcpdump capture with no server. The seed capture is a real UROWID fetch that
ends in ORA-01403 (adopted as a capture fact from the go-ora driver, MIT).
"""

import unittest

from replay import ReplaySocket, load_capture, parse_hexdump

from seerdb.common.tns import assemble_packet, decode_packet
from seerdb.common.tns_consts import (
    FIELD_VERSION_11_2,
    FIELD_VERSION_12_1,
    FIELD_VERSION_12_2,
    FIELD_VERSION_23_4,
    TNS_DATA,
)


class TestParseHexdump(unittest.TestCase):
    def test_extracts_bytes_ignoring_offset_and_gutter(self):
        dump = '00000000  41 42 43 44  |ABCD|\n00000004  45  |E|'
        self.assertEqual(parse_hexdump(dump), b'ABCDE')

    def test_skips_comments_and_blank_lines(self):
        dump = '# a captured response\n\n00000000  01 02 03  |...|\n\n# trailing note'
        self.assertEqual(parse_hexdump(dump), b'\x01\x02\x03')

    def test_offset_only_line_yields_nothing(self):
        # A line with no byte tokens (short final gutter) contributes nothing.
        self.assertEqual(parse_hexdump('00000000  |.|'), b'')


class TestReplaySocket(unittest.TestCase):
    def test_hands_out_bytes_then_eof(self):
        sock = ReplaySocket(b'hello world')
        self.assertEqual(sock.recv(5), b'hello')
        self.assertEqual(sock.recv(100), b' world')
        self.assertEqual(sock.recv(100), b'')  # EOF once drained

    def test_send_is_discarded(self):
        sock = ReplaySocket(b'')
        self.assertEqual(sock.send(b'ignored'), 7)
        self.assertIsNone(sock.sendall(b'ignored'))


class TestUrowidCaptureReplay(unittest.TestCase):
    # The seed capture is a single large-framed (4-byte length) TNS_DATA packet:
    # a UROWID column fetch whose result set ends in ORA-01403.
    _CAPTURE = load_capture('urowid_ora01403.hexdump')

    def test_capture_is_one_data_packet_of_the_declared_length(self):
        # The 4-byte header length matches the captured byte count exactly.
        declared = int.from_bytes(self._CAPTURE[:4], 'big')
        self.assertEqual(declared, len(self._CAPTURE))
        flag, packet_type, body, rest = assemble_packet(self._CAPTURE, 8192, True)
        self.assertTrue(flag)
        self.assertEqual(packet_type, TNS_DATA)
        self.assertEqual(len(body), declared - 10)  # 8-byte header + 2 data flags
        self.assertEqual(rest, b'')  # exactly one packet, nothing trailing

    def test_framed_body_preserves_the_ttc_content(self):
        # The framed body carries the describe column name and the terminating
        # ORA-01403 error text intact — the wire content a decoder would read.
        (_flag, _type, body, _rest) = assemble_packet(self._CAPTURE, 8192, True)
        self.assertIn(b'COL_UROWID', body)
        self.assertIn(b'ORA-01403: no data found', body)


class TestUrowidCaptureDecode(unittest.TestCase):
    # The capture-replay harness end to end (#439): frame the captured packet,
    # then run it through the TTC token decoder and check the values a live
    # fetch would have yielded — a describe, the UROWID rows, and the ORA-01403.
    _CAPTURE = load_capture('urowid_ora01403.hexdump')
    _FIELD_VERSION = 11  # the TTC field version this response was captured at

    def _decode(self) -> tuple:
        (_flag, _type, body, _rest) = assemble_packet(self._CAPTURE, 8192, True)
        # A fresh describe-led response decodes from the empty seed context; the
        # positional result is decode_packet's contract (err_code, describe,
        # rows, err_msg at indices 1/3/4/5).
        assert body is not None
        return decode_packet(body, (None, None, []), self._FIELD_VERSION)

    def test_describe_identifies_the_urowid_column(self):
        column = self._decode()[3][1][0]
        self.assertEqual(column['column_name'], b'COL_UROWID')
        self.assertEqual(column['data_type'], 208)  # UROWID

    def test_decodes_the_urowid_rows(self):
        rows = self._decode()[4]
        self.assertEqual(len(rows), 12)
        # The first row's UROWID and a NULL UROWID mid-set both decode.
        # These are PHYSICAL rowids (tag 0x01: object 122, file 1, block 1, slot
        # 10), which Oracle and python-oracledb print as ordinary extended
        # rowids. The '*' form this used to expect was seerdb's old rendering,
        # written down as the answer (#1086).
        self.assertEqual(rows[0], ['AAAAB6AABAAAAABAAK'])
        self.assertIsNone(rows[3][0])
        for (value,) in rows:
            if value is not None:
                self.assertEqual(len(value), 18)
                self.assertFalse(value.startswith('*'))

    def test_terminating_error_is_ora_01403(self):
        result = self._decode()
        self.assertEqual(result[1], 1403)
        self.assertEqual(result[5], 'ORA-01403: no data found')


class TestAlterSessionCurrentSchemaReplay(unittest.TestCase):
    # 11g answers ALTER SESSION SET CURRENT_SCHEMA with a return-parameters
    # block that carries two session-state key/value pairs. Skipping only zero
    # bytes past the al8o4l words left the first pair's `01` to be read as a
    # token: "no decoder for response token 1" (#1141).
    _CAPTURE = load_capture('alter_session_current_schema_11g.hexdump')

    def test_the_key_value_pairs_are_consumed_and_the_call_succeeds(self):
        (_flag, packet_type, body, rest) = assemble_packet(self._CAPTURE, 8192, False)
        self.assertEqual(packet_type, TNS_DATA)
        self.assertEqual(rest, b'')
        assert body is not None
        result = decode_packet(body, (None, None, []), FIELD_VERSION_11_2)
        self.assertEqual(result[1], 0)  # the OER behind the block: success

    def test_a_block_with_no_trailing_fields_still_decodes(self):
        # A changepassword reply's block is `08 00` and then its OER, with none
        # of an execute's trailing fields. Reading them regardless took the
        # OER's own bytes for a length and ran off the end, after the server had
        # already changed the password. Live 23ai bytes.
        raw = bytes.fromhex(
            '0000003106000000200008000401010298e800000000000000000000000000'
            '00000000000007000000000000000000001d'
        )
        (_flag, _type, body, _rest) = assemble_packet(raw, 8192, True)
        assert body is not None
        result = decode_packet(body, (None, None, []), FIELD_VERSION_12_1)
        self.assertEqual(result[1], 0)


class TestOerTailFollowsTheServerRelease(unittest.TestCase):
    # A 20.1+ server ends every OER with a SQL type and a checksum, whatever
    # field version the session negotiated. These are live 23ai bytes, taken
    # from a session that negotiated 12.2 (8): `... 02 06 ba | 00 | 01 03 | 00
    # | 7e "ORA-01722 ..."`. Read by the negotiated version, the SQL type was
    # taken for the message length and the text was lost (#1145).
    _OER = bytes.fromhex(
        '04010102ee7c000206ba0000010101110300000000000000000000000006000101'
        '000000000206ba000103007e4f52412d30313732323a20756e61626c6520746f20'
        '636f6e7665727420737472696e672076616c756520636f6e7461696e696e672027'
        '782720746f2061206e756d6265723a200a4f52412d30333330323a20284f52412d'
        '30313732322064657461696c732920696e76616c696420737472696e672076616c'
        '75653a20780a1d'
    )

    def test_the_message_survives_a_session_below_the_server_release(self):
        result = decode_packet(
            self._OER, (None, None, []), FIELD_VERSION_12_2, FIELD_VERSION_23_4
        )
        self.assertEqual(result[1], 1722)
        self.assertTrue(result[5].startswith('ORA-01722: unable to convert'))


class TestTwelveOneQueryReplay(unittest.TestCase):
    # A live 23ai reply to `SELECT CAST(12.34 AS NUMBER(5,2)) AS n, 'abc' AS s
    # FROM dual` in a session that negotiated 12.1 (7): a describe, one row and
    # the end-of-fetch OER. The describe already carries the one-byte scale of
    # the 12c layout but not yet the 12.2 oaccolid; decoded as 11g or as 12.2,
    # the second column desyncs (#1144).
    _REPLY = bytes.fromhex(
        '10170f5a3945b1fb23d02169ce288349a8e7787e091713211701190102590200050201'
        '160000000000000001010101014e00000000608000000103000000000203690101030101'
        '010101530000010100010707787e091713211700021fe80102010200062201020001640000'
        '000703c10d230361626308010604018fe5f7000101000000000000040101021'
        '1a9010102057b00000101013803000000000000000000000000060001010000000002057b'
        '0101010300194f52412d30313430333a206e6f206461746120666f756e640a1d'
    )

    def test_describe_and_row_decode_at_field_version_7(self):
        result = decode_packet(
            self._REPLY, (None, None, []), FIELD_VERSION_12_1, FIELD_VERSION_23_4
        )
        columns = result[3][1]
        self.assertEqual([c['column_name'] for c in columns], [b'N', b'S'])
        self.assertEqual(columns[0]['data_scale'], 2)
        self.assertEqual(len(result[4]), 1)
        self.assertEqual(result[4][0][1], 'abc')
        self.assertEqual(result[1], 1403)


if __name__ == '__main__':
    unittest.main()
