# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""An XMLTYPE column served to sqlplus, checked against live 11g and 18c (#2050).

sqlplus hung on one: the Mirror described it as an object (109), where a live
server tells an OCI client type 58, then answers sqlplus's describe of
SYS.XMLTYPE and sends the row on the fetch.
"""

import pytest
import xmltype_oci as fx

from seerdb.common import tns
from seerdb.common.tns import (
    KOD_BY_NAME,
    ColumnMeta,
    decode_kod_reply,
    encode_describe_oci,
    encode_kod_named_reply,
    kod_xmltype_record,
    parse_kod_request,
)
from seerdb.common.tns_consts import FIELD_VERSION_11_2, FIELD_VERSION_18_1_EXT_1

_BANDS = [
    pytest.param(FIELD_VERSION_11_2, '11G', id='11g'),
    pytest.param(FIELD_VERSION_18_1_EXT_1, '18C', id='18c'),
]

# The column as a backend hands it over: the passthrough's describe of
# `xmltype('<a/>')` through the thin client.
_COLUMN = ColumnMeta(
    name=b'X',
    data_type=109,
    data_length=0,
    max_size=0,
    charset=0,
    csfrm=0,
    precision=0,
    scale=0,
    null_ok=1,
    type_oid=bytes(13) + b'\x02\x01\x00',
    type_schema=b'SYS',
    type_name=b'XMLTYPE',
)

# The describe's 23-byte preamble, a cursor id and a time the Mirror zeroes.
_PREAMBLE = slice(5, 28)


@pytest.fixture
def band(request: pytest.FixtureRequest):
    token = tns._ENCODE_FIELD_VERSION.set(request.param)
    yield
    tns._ENCODE_FIELD_VERSION.reset(token)


@pytest.mark.parametrize(('band', 'tag'), _BANDS, indirect=['band'])
def test_the_column_is_described_as_live_servers_describe_it(band, tag) -> None:
    # Type 58 with SYS.XMLTYPE's id, version, schema and name; length 2000, no
    # max size, and nothing toward the max row size.
    live = bytearray(getattr(fx, f'DESCRIBE_{tag}'))
    live[_PREAMBLE] = bytes(23)
    assert encode_describe_oci([_COLUMN])[: len(live)] == bytes(live)


@pytest.mark.parametrize('tag', ['11G', '18C'])
def test_a_describe_by_name_with_a_schema_is_read(tag) -> None:
    # It was refused, so the type's describe could not be answered.
    request = parse_kod_request(getattr(fx, f'KOD_BY_NAME_REQUEST_{tag}'))
    assert (request.opcode, request.schema, request.name) == (
        KOD_BY_NAME,
        'SYS',
        'XMLTYPE',
    )


@pytest.mark.parametrize(('band', 'tag'), _BANDS, indirect=['band'])
def test_sys_xmltype_is_described_as_live_servers_describe_it(band, tag) -> None:
    live = decode_kod_reply(getattr(fx, f'KOD_BY_NAME_REPLY_{tag}'))
    reply = encode_kod_named_reply(
        schema='SYS', name='XMLTYPE', record=kod_xmltype_record(), sequence=1
    )
    mine = decode_kod_reply(reply)
    assert mine.named == live.named
    assert mine.records == live.records


@pytest.mark.parametrize(('band', 'tag'), _BANDS, indirect=['band'])
def test_a_cell_goes_out_as_live_servers_send_it(band, tag) -> None:
    assert tns._encode_oci_value('<r>1</r>', _COLUMN) == getattr(
        fx, f'SHORT_CELL_{tag}'
    )
    assert tns._encode_oci_value(None, _COLUMN) == getattr(fx, f'NULL_CELL_{tag}')


@pytest.mark.parametrize('band', [FIELD_VERSION_11_2], indirect=True)
def test_a_long_document_goes_in_ub1_chunks(band) -> None:
    # Past 252 bytes the image is chunked, 255 bytes at a time, as 11g sends it.
    # 18c sends ub4 chunks of 2000 instead, which it may: it advertises
    # BIG_CHUNK_CLR, and the Mirror does not (#2042).
    document = '<a>x' + 'y' * 399 + '</a>'
    assert tns._encode_oci_value(document, _COLUMN) == fx.LONG_CELL_11G
