# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Spike for #2064: the schema's output is byte-identical to today's encoders,
in both dialects and across field versions, and decodes back to its values."""

from __future__ import annotations

import contextlib
import random
from collections.abc import Iterator

import pytest

from seerdb.common import tns
from seerdb.common.wire_schema import (
    LOB_DATA,
    LOGOFF_REPLY,
    OCI,
    STA,
    SYNC,
    THIN,
    VALUE,
    decode,
    encode,
    sync_values,
)

FIELD_VERSIONS = (6, 7, 24)  # 11g, 12.1, 23ai
LENGTHS = (0, 1, 63, 64, 65, 252, 253, 254, 255, 256, 510, 511, 3026, 70000)


@contextlib.contextmanager
def _field_version(version: int) -> Iterator[None]:
    token = tns._ENCODE_FIELD_VERSION.set(version)
    try:
        yield
    finally:
        tns._ENCODE_FIELD_VERSION.reset(token)


def _payload(length: int) -> bytes:
    return random.Random(length).randbytes(length)


@pytest.mark.parametrize('version', FIELD_VERSIONS)
@pytest.mark.parametrize('length', LENGTHS)
def test_a_value_is_byte_identical(version: int, length: int) -> None:
    data = _payload(length)
    with _field_version(version):
        assert encode(VALUE, THIN, {'value': data}) == tns._bytes_with_length(data)
        assert encode(VALUE, OCI, {'value': data}) == tns._oci_value_bytes(data)
        for codec in (THIN, OCI):
            wire = encode(VALUE, codec, {'value': data})
            assert decode(VALUE, codec, wire + b'rest') == ({'value': data}, b'rest')


@pytest.mark.parametrize('version', FIELD_VERSIONS)
@pytest.mark.parametrize('length', LENGTHS)
def test_lob_data_is_byte_identical(version: int, length: int) -> None:
    data = _payload(length)
    with _field_version(version):
        assert encode(LOB_DATA, THIN, {'content': data}) == tns._lob_data_thin(data)
        assert encode(LOB_DATA, OCI, {'content': data}) == tns._oci_lob_data(data)
        for codec in (THIN, OCI):
            wire = encode(LOB_DATA, codec, {'content': data})
            assert decode(LOB_DATA, codec, wire) == ({'content': data}, b'')


@pytest.mark.parametrize('sequence', (0, 1, 0x2F, 0xFA2F, 0xFFFF))
def test_sta_is_byte_identical(sequence: int) -> None:
    commit = {'call_status': 5, 'sequence': sequence}
    assert encode(STA, OCI, commit) == tns.encode_commit_status_oci(sequence)
    ping = {'call_status': 1, 'sequence': sequence}
    assert encode(STA, OCI, ping) == tns.encode_ping_status_oci(sequence)
    for codec in (THIN, OCI):
        assert decode(STA, codec, encode(STA, codec, commit)) == (commit, b'')
    logoff = {'call_status': 1, 'sequence': 0}
    assert encode(LOGOFF_REPLY, THIN, logoff) == tns.encode_logoff_status_thin()
    assert encode(LOGOFF_REPLY, OCI, logoff) == tns.encode_logoff_status_oci()


def test_sta_matches_the_live_captures() -> None:
    # 23ai's logoff reply, and 18c's answer to sqlplus's ping after a cancel.
    assert encode(LOGOFF_REPLY, THIN, {'call_status': 1, 'sequence': 0}) == (
        bytes.fromhex('090101001d')
    )
    assert encode(STA, OCI, {'call_status': 1, 'sequence': 0xFA2F}) == (
        bytes.fromhex('0901000000 2ffa'.replace(' ', ''))
    )


PAIRS = (
    [],
    [(204, 'low')],
    [(0x10, 'AMERICAN'), (0x11, 'AMERICA'), (0x23, 'DD-MON-RR')],
    [(7, ''), (8, 'x' * 300)],
)


@pytest.mark.parametrize('version', FIELD_VERSIONS)
@pytest.mark.parametrize('pairs', PAIRS)
def test_sync_is_byte_identical(version: int, pairs: list[tuple[int, str]]) -> None:
    with _field_version(version):
        assert encode(SYNC, THIN, sync_values(THIN, pairs)) == (
            tns.encode_sync_piggyback(pairs)
        )
        # Today's OCI encoder writes a bare ub1 length, so it can't frame a
        # value past 252 bytes; the schema chunks it as every other value.
        if all(len(text) <= 252 for _keyword, text in pairs):
            assert encode(SYNC, OCI, sync_values(OCI, pairs)) == (
                tns.encode_sync_piggyback_oci(pairs)
            )
        for codec in (THIN, OCI):
            values = sync_values(codec, pairs)
            assert decode(SYNC, codec, encode(SYNC, codec, values)) == (values, b'')


def test_sync_decodes_the_live_23ai_capture() -> None:
    # Keyword 204, transaction priority, = 'low', as 23ai sends it.
    wire = bytes.fromhex('17050101100101160103036c6f770001cc00')
    (values, rest) = decode(SYNC, THIN, wire)
    assert rest == b''
    assert [(p['keyword'], p['text']) for p in values['pairs']] == [(204, b'low')]


def test_sync_in_oci_is_the_thin_layout_with_the_value_as_binary() -> None:
    # The OCI pair, read with the one schema: an empty text value, then the
    # value in the binary slot, as the live 18c bytes have it.
    wire = tns.encode_sync_piggyback_oci([(0x10, 'AMERICAN')])
    (values, _rest) = decode(SYNC, OCI, wire)
    [pair] = values['pairs']
    assert (pair['text_length'], pair['binary'], pair['keyword']) == (
        0,
        b'AMERICAN',
        0x10,
    )
