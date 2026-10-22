# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""One message schema, two serialisations (#2064): the messages written from a
schema keep their wire bytes, in both dialects, and decode back."""

from __future__ import annotations

import contextlib
import random
from collections.abc import Iterator

import pytest

from seerdb.common import tns, wire_schema

FIELD_VERSIONS = (6, 7, 24)  # 11g, 12.1, 23ai
LENGTHS = (0, 1, 63, 64, 65, 252, 253, 254, 255, 256, 510, 511, 3026, 70000)
CODECS = (tns._THIN, tns._OCI)


@contextlib.contextmanager
def _field_version(version: int) -> Iterator[None]:
    tokens = (
        tns._ENCODE_FIELD_VERSION.set(version),
        tns._DECODE_FIELD_VERSION.set(version),
    )
    try:
        yield
    finally:
        tns._ENCODE_FIELD_VERSION.reset(tokens[0])
        tns._DECODE_FIELD_VERSION.reset(tokens[1])


def test_the_live_captures_are_written_as_they_were() -> None:
    # 23ai's logoff reply, 18c's answer to sqlplus's ping after a cancel, and
    # 23ai's SYNC reporting transaction priority `low`.
    assert tns.encode_logoff_status_thin() == bytes.fromhex('090101001d')
    assert tns.encode_ping_status_oci(0xFA2F) == bytes.fromhex('09010000002ffa')
    assert tns.encode_sync_piggyback([(204, 'low')]) == bytes.fromhex(
        '17050101100101160103036c6f770001cc00'
    )


def test_a_long_value_is_chunked_as_each_dialect_reads_it() -> None:
    # 253 bytes: thin's 64-byte chunks with a ub1 length below 12.1 and a
    # variable-width one from it; OCI's 255-byte chunks with a ub1 length.
    data = bytes(253)
    with _field_version(6):
        assert tns._bytes_with_length(data) == (
            b'\xfe' + (b'\x40' + bytes(64)) * 3 + b'\x3d' + bytes(61) + b'\x00'
        )
    with _field_version(7):
        assert tns._bytes_with_length(data) == (
            b'\xfe' + (b'\x01\x40' + bytes(64)) * 3 + b'\x01\x3d' + bytes(61) + b'\x00'
        )
    assert tns._oci_value_bytes(data) == b'\xfe\xfd' + data + b'\x00'


@pytest.mark.parametrize('version', FIELD_VERSIONS)
@pytest.mark.parametrize('length', LENGTHS)
def test_values_and_lob_data_decode_back(version: int, length: int) -> None:
    data = random.Random(length).randbytes(length)
    with _field_version(version):
        for codec in CODECS:
            for schema in ((('value', 'v'),), tns._LOB_DATA):
                key = schema[-1][1]
                wire = wire_schema.compile_encoder(schema, codec)({key: data})
                decode = wire_schema.compile_decoder(schema, codec)
                assert decode(wire + b'rest') == ({key: data}, b'rest')


@pytest.mark.parametrize('version', FIELD_VERSIONS)
def test_sync_decodes_back_in_both_dialects(version: int) -> None:
    pairs = [(0x10, 'AMERICAN'), (7, ''), (0x23, 'x' * 300)]
    with _field_version(version):
        for codec, slot in ((tns._THIN, 'text'), (tns._OCI, 'binary')):
            fields = tns._sync_fields(pairs, slot)
            wire = wire_schema.compile_encoder(tns._SYNC, codec)(fields)
            assert wire_schema.compile_decoder(tns._SYNC, codec)(wire) == (
                fields,
                b'',
            )


def test_an_oci_sync_value_past_255_bytes_is_chunked() -> None:
    # A bare ub1 length couldn't frame it, and raised (#2064).
    wire = tns.encode_sync_piggyback_oci([(1, 'y' * 300)])
    (fields, rest) = wire_schema.compile_decoder(tns._SYNC, tns._OCI)(wire)
    assert rest == b''
    assert fields['pairs'][0]['binary'] == b'y' * 300


def test_sta_decodes_back_in_both_dialects() -> None:
    values = {'call_status': 5, 'sequence': 0xFA2F}
    for codec in CODECS:
        wire = wire_schema.compile_encoder(tns._STA, codec)(values)
        assert wire_schema.compile_decoder(tns._STA, codec)(wire) == (values, b'')


def test_an_unknown_field_kind_is_refused() -> None:
    with pytest.raises(ValueError, match='unknown field kind'):
        wire_schema.compile_encoder((('ub3', 'x'),), tns._THIN)


@pytest.mark.parametrize('version', (6, 24))
def test_fetch_and_exec_by_cursor_decode_back_in_both_dialects(version: int) -> None:
    # Thin sends a call's token number from 23ai, OCI never (#2064).
    fetch = {'seq': 15, 'cursor': 2, 'rows': 15}
    execute = {
        'function': 4,
        'seq': 8,
        'cursor': 3,
        'iterations': 1,
        'options': 0x20,
        'options_2': 0,
    }
    with _field_version(version):
        for codec in CODECS:
            token = 0 if codec is tns._THIN and version >= 24 else None
            for schema, fields in ((tns._FETCH, fetch), (tns._EXEC_BY_CURSOR, execute)):
                values = fields | {'token_number': token}
                wire = wire_schema.compile_encoder(schema, codec)(values)
                decode = wire_schema.compile_decoder(schema, codec)
                assert decode(wire + b'rest') == (values, b'rest')


def test_an_oci_fetch_is_the_thin_fields_at_fixed_width() -> None:
    # Live 11g / 18c: `03 05 0f 02 00 00 00 0f 00 00 00` asks cursor 2 for 15 rows.
    wire = bytes.fromhex('03050f020000000f000000')
    assert tns.parse_fetch_oci(wire) == 15
    with _field_version(6):
        assert tns.encode_dictionary_fetch(
            {'seq': 15, 'cursor': 2, 'fetch': 15, 'field_version': 6}
        ) == bytes.fromhex('03050f0102010f')


@pytest.mark.parametrize('version', FIELD_VERSIONS)
@pytest.mark.parametrize(
    'content', (None, b'', b'x', bytes(252), bytes(253), bytes(600))
)
def test_a_long_cell_decodes_back_in_both_dialects(
    version: int, content: bytes | None
) -> None:
    # Thin's trailer has two indicators, OCI's one (#2064).
    with _field_version(version):
        for codec in CODECS:
            values = {'content': content, 'actual_length': 0, 'return_code': 0}
            if codec is tns._OCI:
                del values['return_code']
            wire = wire_schema.compile_encoder(tns._LONG_CELL, codec)(values)
            decode = wire_schema.compile_decoder(tns._LONG_CELL, codec)
            assert decode(wire) == (values, b'')


def test_the_describe_trailer_is_the_same_fields_in_both_dialects() -> None:
    # OCI's 33 bytes: a lead byte, the 7-byte date in the two-length form, four
    # ub4 flags and the query key's ub4 length; sqlplus 10.2's 29 lack the key.
    # A LOB describe sets the second flag (#405).
    with _field_version(24):
        oci = tns._describe_trailer(oci=True)
        assert oci == b'\x00\x07\x00\x00\x00\x07' + bytes(7) + bytes(16) + bytes(4)
        assert tns._describe_trailer(oci=False) == bytes(6)
        lob = tns._describe_trailer(oci=True, mdbz=8168)
        assert lob[17:21] == (8168).to_bytes(4, 'little')
    with _field_version(4):
        assert len(tns._describe_trailer(oci=True)) == 29
        assert tns._describe_trailer(oci=False) == bytes(5)


def test_a_ref_cursor_value_is_one_schema_with_its_dialects_lead() -> None:
    values = {'lead': 1, 'describe': b'D', 'cursor': 300, 'return_code': 0}
    assert tns._REFCURSOR_OUT_THIN(values) == b'\x01D\x02\x01\x2c\x00'
    values['lead'] = 0x4C
    assert tns._REFCURSOR_OUT_OCI(values) == b'\x4cD\x2c\x01\x00\x00\x00\x00'
