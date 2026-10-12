# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Transaction priority (ALTER SESSION piggyback, func 204, #1776).

A change rides the next call once as an ALTER SESSION piggyback; the server
reports the session's priority as keyword 204 of a SYNC piggyback, at login and
after a change. Bytes pinned against python-oracledb 26's capture on 23ai.
"""

import pytest

from seerdb import TransactionPriority
from seerdb.client._conn_logic import _transaction_priority
from seerdb.client.aconnection import AsyncOracleConnect
from seerdb.client.connection import OracleConnect
from seerdb.common.exceptions import NotSupportedError
from seerdb.common.tns import (
    _alter_session_clause,
    _decode_sync_pairs,
    encode_alter_session_piggyback,
    recording_session_keywords,
)
from seerdb.common.tns_consts import (
    CCAP_FEATURE_BACKPORT2,
    FIELD_VERSION_23_4,
    TNS_KEYWORD_NUM_TXN_PRIORITY,
)

# python-oracledb 26.0.1 on 23ai: `transaction_priority = 'low'`, then `= ''`.
_CAPTURED_SET_LOW = bytes.fromhex(
    '11cc030000010101010c0c54584e5f5052494f52495459 0103036c6f77 00'
)
_CAPTURED_RESET = bytes.fromhex(
    '11cc050000010101010c0c54584e5f5052494f52495459 00 0101'
)
# The server's answers, after `17 05` (TTI_SVR_PIGGYBACK, SYNC): keyword 204.
_REPORTED_LOW = bytes.fromhex('010110010116 0103036c6f77 00 01cc 00')
_REPORTED_HIGH = bytes.fromhex('010110010116 01040448494748 00 01cc 00')


def test_set_matches_the_capture():
    assert encode_alter_session_piggyback(3, FIELD_VERSION_23_4, 'low') == (
        _CAPTURED_SET_LOW
    )


def test_reset_matches_the_capture():
    assert encode_alter_session_piggyback(5, FIELD_VERSION_23_4, '') == _CAPTURED_RESET


def test_a_reported_priority_is_recorded():
    sink: dict = {}
    with recording_session_keywords(sink):
        assert _decode_sync_pairs(_REPORTED_LOW) == b''
    assert sink == {TNS_KEYWORD_NUM_TXN_PRIORITY: 'low'}
    with recording_session_keywords(sink):
        _decode_sync_pairs(_REPORTED_HIGH)
    assert sink == {TNS_KEYWORD_NUM_TXN_PRIORITY: 'HIGH'}


def test_pairs_are_only_walked_outside_a_recording():
    # Nothing records unless a connection asked, so one connection's reply
    # never lands in another's state.
    sink: dict = {}
    assert _decode_sync_pairs(_REPORTED_LOW) == b''
    assert sink == {}


def test_the_login_alter_session_carries_the_priority():
    plain = _alter_session_clause({})
    assert plain.startswith(b"ALTER SESSION SET TIME_ZONE='")
    assert plain.endswith(b"'\x00")
    asked = _alter_session_clause({'txn_priority': 'medium'})
    assert asked == plain[:-1] + b" TXN_PRIORITY='medium'\x00"


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        (TransactionPriority.HIGH, TransactionPriority.HIGH),
        ('low', TransactionPriority.LOW),
        ('Medium', TransactionPriority.MEDIUM),
        ('default', TransactionPriority.DEFAULT),
        ('', TransactionPriority.DEFAULT),
        (None, TransactionPriority.DEFAULT),
    ],
)
def test_accepted_values(value, expected):
    assert _transaction_priority(value) is expected


def test_rejected_values():
    with pytest.raises(ValueError):
        _transaction_priority('urgent')
    with pytest.raises(TypeError):
        _transaction_priority(1)


def _bare(cls, supported=True):
    conn = object.__new__(cls)
    caps = bytearray(CCAP_FEATURE_BACKPORT2 + 1)
    caps[CCAP_FEATURE_BACKPORT2] = 0x03 if supported else 0x02
    conn._server_compile_caps = bytes(caps)
    conn._txn_priority = 'HIGH'
    conn._txn_priority_pending = None
    conn._session_keywords = {}
    conn.seq = 1
    conn.field_version = FIELD_VERSION_23_4
    return conn


@pytest.mark.parametrize('cls', [OracleConnect, AsyncOracleConnect])
class TestAttribute:
    def test_reads_what_the_server_reported(self, cls):
        conn = _bare(cls)
        assert conn.transaction_priority is TransactionPriority.HIGH
        conn._session_keywords[TNS_KEYWORD_NUM_TXN_PRIORITY] = 'low'
        conn._absorb_session_keywords()
        assert conn.transaction_priority is TransactionPriority.LOW
        assert conn._session_keywords == {}

    def test_a_set_is_sent_once(self, cls):
        conn = _bare(cls)
        conn.transaction_priority = 'low'
        assert conn.transaction_priority is TransactionPriority.LOW
        out = conn._flush_alter_session_bytes()
        assert out == encode_alter_session_piggyback(1, FIELD_VERSION_23_4, 'low')
        assert conn._flush_alter_session_bytes() == b''

    def test_a_reset_sends_an_empty_value(self, cls):
        conn = _bare(cls)
        conn.transaction_priority = None
        assert conn.transaction_priority is None  # until the server reports it
        out = conn._flush_alter_session_bytes()
        assert out == encode_alter_session_piggyback(1, FIELD_VERSION_23_4, '')

    def test_refused_without_the_capability(self, cls):
        conn = _bare(cls, supported=False)
        with pytest.raises(NotSupportedError):
            conn.transaction_priority
        with pytest.raises(NotSupportedError):
            conn.transaction_priority = 'low'
        assert conn._txn_priority_pending is None
