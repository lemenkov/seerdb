# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Application context (func-154 piggyback, #1775).

``set_app_context()`` / ``clear_app_context()`` queue a change that rides the
next call once. The bytes are pinned against python-oracledb 26's capture on
23ai (field version 24); below field version 12.1 the zero byte before the
namespace is absent, as 10g, 11g and 23ai held at 11.2 require.
"""

import unittest

import pytest

from seerdb.client.aconnection import AsyncOracleConnect
from seerdb.client.connection import OracleConnect
from seerdb.common.exceptions import NotSupportedError
from seerdb.common.tns import encode_app_context_piggyback
from seerdb.common.tns_consts import (
    FIELD_VERSION_9_2,
    FIELD_VERSION_11_2,
    FIELD_VERSION_12_1,
    FIELD_VERSION_23_4,
)

# python-oracledb 26.0.1 against 23ai: set_app_context('CLIENTCONTEXT',
# ATTR1='VALUE1', ATTR2='V2'), then clear_app_context('CLIENTCONTEXT').
_CAPTURED_SET = bytes.fromhex(
    '119a03000101 0d0101020101000d434c49454e54434f4e54455854'
    '0105054154545231 010606 56414c554531 00'
    '0105054154545232 010202 5632 00'
)
_CAPTURED_CLEAR = bytes.fromhex(
    '119a05000101 0d0000010200 0d434c49454e54434f4e54455854'
)
_NS = b'CLIENTCONTEXT'
_ENTRIES = [(b'ATTR1', b'VALUE1'), (b'ATTR2', b'V2')]


class TestEncoder(unittest.TestCase):
    def test_set_matches_the_capture(self):
        self.assertEqual(
            encode_app_context_piggyback(3, FIELD_VERSION_23_4, _NS, _ENTRIES),
            _CAPTURED_SET,
        )

    def test_clear_matches_the_capture(self):
        self.assertEqual(
            encode_app_context_piggyback(5, FIELD_VERSION_23_4, _NS, []),
            _CAPTURED_CLEAR,
        )

    def test_the_zero_byte_starts_at_12_1(self):
        # No ub8 token before 23.1; the zero byte before the namespace is the
        # only difference between 12.1 and 11.2.
        at_12_1 = encode_app_context_piggyback(5, FIELD_VERSION_12_1, _NS, [])
        at_11_2 = encode_app_context_piggyback(5, FIELD_VERSION_11_2, _NS, [])
        self.assertEqual(at_12_1, _CAPTURED_CLEAR[:3] + _CAPTURED_CLEAR[4:])
        self.assertEqual(at_11_2, at_12_1[:-15] + at_12_1[-14:])


def _bare(cls=OracleConnect, field_version=FIELD_VERSION_23_4):
    conn = object.__new__(cls)
    conn._app_context = {}
    conn.seq = 1
    conn.field_version = field_version
    return conn


@pytest.mark.parametrize('cls', [OracleConnect, AsyncOracleConnect])
class TestQueue:
    def test_sets_accumulate_and_flush_once(self, cls):
        conn = _bare(cls)
        conn.set_app_context('CLIENTCONTEXT', ATTR1='VALUE1')
        conn.set_app_context('CLIENTCONTEXT', ATTR2='V2')
        out = conn._flush_app_context_bytes()
        assert out == encode_app_context_piggyback(1, FIELD_VERSION_23_4, _NS, _ENTRIES)
        assert conn._flush_app_context_bytes() == b''

    def test_clear_replaces_pending_sets(self, cls):
        conn = _bare(cls)
        conn.set_app_context('CLIENTCONTEXT', ATTR1='VALUE1')
        conn.clear_app_context('CLIENTCONTEXT')
        out = conn._flush_app_context_bytes()
        assert out == encode_app_context_piggyback(1, FIELD_VERSION_23_4, _NS, [])

    def test_one_piggyback_per_namespace(self, cls):
        conn = _bare(cls)
        conn.set_app_context('CLIENTCONTEXT', ATTR1='VALUE1')
        conn.clear_app_context('OTHER')
        out = conn._flush_app_context_bytes()
        assert out == encode_app_context_piggyback(
            1, FIELD_VERSION_23_4, _NS, [(b'ATTR1', b'VALUE1')]
        ) + encode_app_context_piggyback(2, FIELD_VERSION_23_4, b'OTHER', [])

    @pytest.mark.parametrize(
        'call',
        [
            lambda c: c.set_app_context('', attr='value'),
            lambda c: c.clear_app_context(''),
            lambda c: c.set_app_context('CLIENTCONTEXT'),
            lambda c: c.set_app_context('CLIENTCONTEXT', **{'X' * 129: 'value'}),
            lambda c: c.set_app_context('CLIENTCONTEXT', attr='X' * 4001),
        ],
    )
    def test_invalid_input_is_a_value_error(self, cls, call):
        # The checks python-oracledb 26 makes (its test_connection_1063).
        conn = _bare(cls)
        with pytest.raises(ValueError):
            call(conn)
        assert conn._app_context == {}

    def test_a_value_must_be_a_string(self, cls):
        with pytest.raises(TypeError):
            _bare(cls).set_app_context('CLIENTCONTEXT', attr=1)

    def test_refused_before_10g(self, cls):
        # 8i and 9i take the piggyback and ignore it.
        conn = _bare(cls, field_version=FIELD_VERSION_9_2)
        with pytest.raises(NotSupportedError):
            conn.set_app_context('CLIENTCONTEXT', attr='value')
        with pytest.raises(NotSupportedError):
            conn.clear_app_context('CLIENTCONTEXT')
