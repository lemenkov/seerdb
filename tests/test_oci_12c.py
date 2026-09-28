# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The sqlplus / thick-OCI replies at the 12c band (#1282).

From 12.1 the Mirror serves sqlplus in 18c's layout (the one 12c-band layout
there are captures of). It is the 11g layout with a few fields widened; these
pin each widening against a live 18c reply to sqlplus 23.26.
"""

from collections.abc import Iterator

import pytest

from seerdb.common.tns import (
    _ENCODE_FIELD_VERSION,
    ColumnMeta,
    encode_describe_oci,
    encode_dml_status_oci,
    encode_fetch_terminator_oci,
    encode_oci_oer,
    encode_status_oci,
)
from seerdb.common.tns_consts import (
    FIELD_VERSION_11_2,
    FIELD_VERSION_18_1_EXT1,
    TNS_TYPE_DATE,
    TNS_TYPE_NUMBER,
    TNS_TYPE_VARCHAR,
)


@pytest.fixture
def at_12c() -> Iterator[None]:
    token = _ENCODE_FIELD_VERSION.set(FIELD_VERSION_18_1_EXT1)
    yield
    _ENCODE_FIELD_VERSION.reset(token)


# 18c's describe of `select * from t1282` (ID NUMBER, S VARCHAR2(20), D DATE),
# up to the describe tail.
_DESCRIBE_18C = bytes.fromhex(
    '101700000041719583e1d6441e7ff9a1e6a5623369787e091c0d3b2631000000'
    '030000005c010200008116000000000000000000000000000000000000000000'
    '0000000000000000000000000000000000000000000001020200000002494400'
    '0000000000000000000000000001018000001400000000000000000000000000'
    '0000000000000000000000000000000000006903010014000000fe3f00000101'
    '0100000001530000000000000000010000000000010c00000001000000000000'
    '0000000000000000000000000000000000000000000000000000000000000000'
    '0000000000010101000000014400000000000000000200000000'
)

# Where the Mirror's describe knowingly differs from a live server's, in 11g's
# layout and 18c's alike: the cursor uuid and describe time the client skips,
# and four fields the Mirror has always filled its own way (a NUMBER's max size,
# two DATE fields, a trailing count).
_DESCRIBE_UUID = range(5, 28)
_DESCRIBE_OWN_FIELDS = {78, 174, 185, 221, 245}


def test_describe_columns_take_18c_layout(at_12c: None) -> None:
    columns = [
        ColumnMeta(
            name=b'ID',
            data_type=TNS_TYPE_NUMBER,
            data_length=22,
            max_size=22,
            scale=-127,
        ),
        ColumnMeta(
            name=b'S',
            data_type=TNS_TYPE_VARCHAR,
            data_length=20,
            max_size=20,
            charset=873,
            csfrm=1,
        ),
        ColumnMeta(name=b'D', data_type=TNS_TYPE_DATE, data_length=7, max_size=7),
    ]
    ours = encode_describe_oci(columns)
    assert len(ours) == len(_DESCRIBE_18C)
    differ = {i for i in range(len(ours)) if ours[i] != _DESCRIBE_18C[i]}
    assert differ - set(_DESCRIBE_UUID) == _DESCRIBE_OWN_FIELDS


def test_oer_grows_by_the_extended_error_and_row_count(at_12c: None) -> None:
    # 144 bytes: the error number again at 132, then a ub8 row count.
    oer = encode_oci_oer(1, sequence=5, error_code=942, rowcount=3)
    assert len(oer) == 144
    assert oer[132:136] == (942).to_bytes(4, 'little')
    assert oer[136:144] == (3).to_bytes(8, 'little')


def test_the_11g_band_is_unchanged() -> None:
    token = _ENCODE_FIELD_VERSION.set(FIELD_VERSION_11_2)
    try:
        assert len(encode_oci_oer(1, sequence=5)) == 136
        assert len(encode_status_oci(5)) == 171
    finally:
        _ENCODE_FIELD_VERSION.reset(token)


def test_end_of_fetch_carries_1403_and_the_row_count(at_12c: None) -> None:
    # A live 18c ends a 30-row fetch with 1403 at 132 and 30 rows (#1282).
    reply = encode_fetch_terminator_oci(9, rowcount=30)
    assert reply[132:136] == (1403).to_bytes(4, 'little')
    assert reply[136:144] == (30).to_bytes(8, 'little')
    assert reply[145:].startswith(b'ORA-01403')


def test_dml_status_carries_its_row_count_in_the_oer(at_12c: None) -> None:
    # sqlplus at the 12c band prints "N rows created" from the OER's row count;
    # without it every DML statement reported 0 rows.
    reply = encode_dml_status_oci('INSERT', 1, sequence=7)
    assert len(reply) == 35 + 144 + 16  # prefix, OER, rowid trailer
    assert reply[35 + 136 : 35 + 144] == (1).to_bytes(8, 'little')
    assert len(encode_status_oci(7)) == 179
