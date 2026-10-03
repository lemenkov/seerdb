# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Golden test: the OCI OER return-status trailers are now GENERATED from named
fields (seerdb.common.tns.encode_oci_oer) rather than stored as three
near-identical 136-byte blobs. The captures below (live 11g via sqlplus) pin the
generator byte-for-byte so the Mirror stays wire-identical to the real server.

The OER field map was reverse-engineered by controlled capture (docs/PROTOCOL.md
§36): connecting sqlplus through a logging proxy to 11g and varying one thing at
a time (rowcount, error, command type) to locate each field.
"""

import unittest

from seerdb.common.oci import (
    OCI_OER_ROW_KIND_LOB,
    OCI_OER_ROW_KIND_LONG,
    OCI_OER_STATUS_ERROR,
    OCI_OER_STATUS_SUCCESS,
)
from seerdb.common.tns import (
    _ENCODE_OCI_CALL_SEQ,
    _OCI_CMD_TYPE_OFF,
    _OCI_DML_ROWCOUNT_OFF,
    encode_changepassword_status_oci,
    encode_ddl_status_oci,
    encode_dml_status_oci,
    encode_error_oci,
    encode_oci_oer,
)

# --- captured golden OER records (live 11g) ---
ERROR_OER = bytes.fromhex(
    '04050000001300010000000000000000000002000e00030000000000000000000000'
    '00000000000000000000000000000015000001000000360100000000000000000000'
    '0000000020f6310a0000000000000000000000000000000000000000000000000000'
    '00000000000000000000000000000000000000000000000000000000000000000000'
)
LONG_FETCH_STATUS = bytes.fromhex(
    '04010000001100010200000000000000000002000000030000000000000000000000'
    '00000000000000000000000000000013000001000000360100000000000000000000'
    '0000000020f6310a0000000000000000000000000000000000000000000000000000'
    '00000000000000000000000000000000000000000000000000000000000000000000'
)
LOB_FETCH_STATUS = bytes.fromhex(
    '04010000001000010100000000000000000002000000030000000000000000000000'
    '00000000000000000000000000000012000001000000360100000000000000000000'
    '0000000020f6310a0000000000000000000000000000000000000000000000000000'
    '00000000000000000000000000000000000000000000000000000000000000000000'
)


class _CallSeq:
    """Publish the answered call's sequence, as the OCI session loop does per
    message. Each golden below was captured two sequences after its reply's own
    counter, which is why `sequence + 2` fitted them before it was decoded."""

    def __init__(self, call_sequence):
        self.call_sequence = call_sequence

    def __enter__(self):
        self._token = _ENCODE_OCI_CALL_SEQ.set(self.call_sequence)

    def __exit__(self, *exc):
        _ENCODE_OCI_CALL_SEQ.reset(self._token)
        return False


class TestOciOerGeneration(unittest.TestCase):
    def test_long_fetch_status(self):
        with _CallSeq(0x13):
            self.assertEqual(
                encode_oci_oer(
                    OCI_OER_STATUS_SUCCESS,
                    sequence=0x11,
                    row_kind=OCI_OER_ROW_KIND_LONG,
                ),
                LONG_FETCH_STATUS,
            )

    def test_lob_fetch_status(self):
        with _CallSeq(0x12):
            self.assertEqual(
                encode_oci_oer(
                    OCI_OER_STATUS_SUCCESS,
                    sequence=0x10,
                    row_kind=OCI_OER_ROW_KIND_LOB,
                ),
                LOB_FETCH_STATUS,
            )

    def test_error_oer_envelope(self):
        # the error path builds the same envelope with the code patched in at
        # offset 12 (ub4 LE), then appends the ORA-… message DALC.
        expected = bytearray(ERROR_OER)
        expected[12:16] = (942).to_bytes(4, 'little')
        # sequence=0x13 and call sequence 0x15 are the values the golden
        # capture carried; passing both reproduces the live 11g error reply
        # byte-for-byte.
        with _CallSeq(0x15):
            got = encode_error_oci(942, 'table or view does not exist', sequence=0x13)
        self.assertEqual(got[:136], bytes(expected))

    def test_error_oer_status_and_code(self):
        oer = encode_oci_oer(
            OCI_OER_STATUS_ERROR, sequence=0x13, error_pos=0x0E, error_code=1017
        )
        self.assertEqual(oer[1], OCI_OER_STATUS_ERROR)
        self.assertEqual(int.from_bytes(oer[12:16], 'little'), 1017)
        self.assertEqual(len(oer), 136)

    def test_error_message_appended(self):
        got = encode_error_oci(942, 'table or view does not exist', sequence=0x13)
        self.assertEqual(
            got[136:], bytes([40]) + b'ORA-00942: table or view does not exist\n'
        )

    def test_a_long_error_message_is_chunked(self):
        # A message over 252 bytes follows a 0xFE marker in chunks of up to 255
        # bytes with one-byte lengths, ending in a zero-length chunk, as a live
        # 11g sends it -- captured with sqlplus 23.26 for RAISE_APPLICATION_ERROR
        # of a 300-character message, 333 bytes as sent. A one-byte length could
        # not hold it at all, and the session died while encoding the reply
        # (#1520). sqlplus reads this form from the Mirror in both bands.
        from seerdb.common.tns import _ENCODE_FIELD_VERSION
        from seerdb.common.tns_consts import (
            FIELD_VERSION_11_2,
            FIELD_VERSION_18_1_EXT_1,
        )

        message = 'x' + 'y' * 299 + '\nORA-06512: at line 1'
        text = f'ORA-20001: {message}\n'.encode()
        self.assertEqual(len(text), 333)
        form = b'\xfe\xff' + text[:255] + b'\x4e' + text[255:] + b'\x00'
        for version in (FIELD_VERSION_11_2, FIELD_VERSION_18_1_EXT_1):
            token = _ENCODE_FIELD_VERSION.set(version)
            try:
                got = encode_error_oci(20001, message, sequence=0x13)
            finally:
                _ENCODE_FIELD_VERSION.reset(token)
            self.assertEqual(got[-len(form) :], form, hex(version))
        # Up to 252 bytes the one-byte length stays.
        short = encode_error_oci(942, 'x' * 240, sequence=0x13)
        self.assertEqual(short[136], 252)

    def test_offset_49_is_the_call_sequence_not_the_counter(self):
        # Offset 5 is the server's own counter; offset 49 is the sequence of the
        # call being answered. They are independent — the captures this was first
        # read off happened to sit two apart, which is where `sequence + 2` came
        # from, but a live session's two counters diverge (#884).
        with _CallSeq(0x31):
            oer = encode_oci_oer(OCI_OER_STATUS_SUCCESS, sequence=0x20)
        self.assertEqual(oer[5], 0x20)
        self.assertEqual(oer[49], 0x31)

    def test_offset_49_moves_with_the_call_not_the_counter(self):
        # Holding the counter and moving the call sequence changes offset 49 and
        # nothing else; the reverse changes offset 5 and nothing else.
        with _CallSeq(0x31):
            a = encode_oci_oer(OCI_OER_STATUS_SUCCESS, sequence=0x20)
        with _CallSeq(0x32):
            b = encode_oci_oer(OCI_OER_STATUS_SUCCESS, sequence=0x20)
        self.assertEqual([i for i in range(len(a)) if a[i] != b[i]], [49])


class TestExecuteStatusGeneration(unittest.TestCase):
    """The DML/DDL execute-status replies are generated from one frame each,
    varying only the V$SQL command type (offset 57) and — for DML — the rowcount
    (offset 43). Validated live against sqlplus (see PROTOCOL.md §36)."""

    def test_dml_verbs_share_frame_and_carry_command_type(self):
        codes = {'INSERT': 2, 'UPDATE': 6, 'DELETE': 7}
        # sequence=19 is the captured DML value; a fixed sequence keeps the three
        # verbs' frames identical bar the command type.
        bodies = {kw: encode_dml_status_oci(kw, 5, sequence=19) for kw in codes}
        for kw, code in codes.items():
            self.assertEqual(bodies[kw][_OCI_CMD_TYPE_OFF], code)
            self.assertEqual(
                int.from_bytes(
                    bodies[kw][_OCI_DML_ROWCOUNT_OFF : _OCI_DML_ROWCOUNT_OFF + 4],
                    'little',
                ),
                5,
            )
        # all three differ only at the command-type and rowcount offsets
        ins = bodies['INSERT']
        for kw in ('UPDATE', 'DELETE'):
            diffs = [i for i in range(len(ins)) if ins[i] != bodies[kw][i]]
            self.assertEqual(diffs, [_OCI_CMD_TYPE_OFF])

    def test_dml_rowcount(self):
        body = encode_dml_status_oci('UPDATE', 42, sequence=19)
        self.assertEqual(
            int.from_bytes(
                body[_OCI_DML_ROWCOUNT_OFF : _OCI_DML_ROWCOUNT_OFF + 4], 'little'
            ),
            42,
        )

    def test_dml_unknown_verb_falls_back_to_insert(self):
        self.assertEqual(
            encode_dml_status_oci('MERGE', 3, sequence=19),
            encode_dml_status_oci('INSERT', 3, sequence=19),
        )

    def test_ddl_verbs_share_frame_and_carry_command_type(self):
        create = encode_ddl_status_oci(1, sequence=17)  # CREATE TABLE
        drop = encode_ddl_status_oci(12, sequence=17)  # DROP TABLE
        self.assertEqual(create[_OCI_CMD_TYPE_OFF], 1)
        self.assertEqual(drop[_OCI_CMD_TYPE_OFF], 12)
        diffs = [i for i in range(len(create)) if create[i] != drop[i]]
        self.assertEqual(diffs, [_OCI_CMD_TYPE_OFF])

    def test_status_replies_carry_the_live_sequence(self):
        # The OER sequence is a per-session counter threaded in, not a frozen
        # capture constant: a different sequence changes offset 5 of the OER and
        # nothing else, now that offset 49 tracks the call instead (#884). Tested
        # on the bare-OER error reply, whose OER starts at offset 0.
        with _CallSeq(0x15):
            a = encode_error_oci(942, 'x', sequence=0x13)[:136]
            b = encode_error_oci(942, 'x', sequence=0x14)[:136]
        self.assertEqual(a[5], 0x13)
        self.assertEqual(b[5], 0x14)
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        self.assertEqual(diffs, [5])


class ChangePasswordStatusGolden(unittest.TestCase):
    # The OCIPasswordChange ("Password changed") reply, captured live from 11g
    # via sqlplus through a logging proxy — byte-identical across four separate
    # password changes in independent sessions, so the whole reply is a fixed
    # constant with no per-session counter (docs/PROTOCOL.md § 4.1.3 / §36.1).
    _CAPTURE = bytes.fromhex(
        '08000004050000001300010100000000000000000000000000000000000000000000000000000000000000000000000000000000160000010000003601000000000000000000000000000020f6310a000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
    )

    def test_matches_the_live_capture(self):
        self.assertEqual(encode_changepassword_status_oci(), self._CAPTURE)

    def test_is_the_shared_oer_envelope_plus_an_empty_rpa(self):
        # The reply is an empty RPA return envelope (08 00 00) + the shared OER
        # envelope: only six fixed bytes of the 136-byte OER body differ from it.
        from seerdb.common.tns import _OCI_OER_ENVELOPE

        reply = encode_changepassword_status_oci()
        self.assertEqual(reply[:3], bytes([8, 0, 0]))
        body = reply[3:]
        self.assertEqual(len(body), len(_OCI_OER_ENVELOPE))
        diffs = [i for i in range(len(body)) if body[i] != _OCI_OER_ENVELOPE[i]]
        self.assertEqual(diffs, [1, 5, 8, 18, 22, 49])


class ErrorPositionOverride(unittest.TestCase):
    # The OCI error reply carries the parse offset at OER offset 20 (the column
    # sqlplus draws its caret under). None keeps the captured 0x0E default; a
    # backend that knows the real offset overrides it.
    def test_default_keeps_the_captured_position(self):
        reply = encode_error_oci(904, 'x', sequence=0x13)
        self.assertEqual(reply[20], 0x0E)

    def test_override_places_the_real_offset(self):
        reply = encode_error_oci(904, 'x', sequence=0x13, error_pos=7)
        self.assertEqual(reply[20], 7)

    def test_large_offset_is_clamped_to_one_byte(self):
        # The frame's position field is a single byte; a longer statement's offset
        # clamps rather than overflowing.
        reply = encode_error_oci(904, 'x', sequence=0x13, error_pos=1000)
        self.assertEqual(reply[20], 0xFF)


if __name__ == '__main__':
    unittest.main()


# How a live server refuses sqlplus 23.26's wrong password, after the break /
# reset marker exchange: 11g, and 18c (the 12c band) (#1290).
_LOGIN_REFUSAL_11G = bytes.fromhex(
    '040100000000000100000000f903000000000000000000000000000000000000'
    '0000000000000000000000000000000000030000000000003601000000000000'
    '000000000000000020f6310a0000000000000000000000000000000000000000'
    '0000000000000000000000000000000000000000000000000000000000000000'
    '0000000000000000334f52412d30313031373a20696e76616c69642075736572'
    '6e616d652f70617373776f72643b206c6f676f6e2064656e6965640a'
)
_LOGIN_REFUSAL_18C = bytes.fromhex(
    '040100000000000100000000f903000000000000000000000000000000000000'
    '0000000000000000000000000000000000030000000000003601000000000000'
    '0000000000000000403eb424a37f000000000000000000000000000000000000'
    '0000000000000000000000000000000000000000000000000000000000000000'
    '00000000f90300000000000000000000334f52412d30313031373a20696e7661'
    '6c696420757365726e616d652f70617373776f72643b206c6f676f6e2064656e'
    '6965640a'
)


class LoginRefusal(unittest.TestCase):
    def _refusal(self, field_version: int) -> bytes:
        from seerdb.common.tns import _ENCODE_FIELD_VERSION, encode_login_refusal_oci

        token = _ENCODE_FIELD_VERSION.set(field_version)
        try:
            return encode_login_refusal_oci(
                1017,
                'ORA-01017: invalid username/password; logon denied',
                call_sequence=3,  # the refused AUTH, `03 73 03`
            )
        finally:
            _ENCODE_FIELD_VERSION.reset(token)

    def test_the_11g_refusal_is_byte_identical(self) -> None:
        self.assertEqual(self._refusal(6), _LOGIN_REFUSAL_11G)

    def test_the_12c_refusal_differs_only_by_the_leaked_pointer(self) -> None:
        # 18c leaks a native pointer where 11g's envelope carries its marker.
        ours = self._refusal(11)
        self.assertEqual(len(ours), len(_LOGIN_REFUSAL_18C))
        differ = [i for i in range(len(ours)) if ours[i] != _LOGIN_REFUSAL_18C[i]]
        self.assertEqual(differ, list(range(72, 78)))
