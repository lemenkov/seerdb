# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

# Offline tests for `cursor.parse()` (#1018): the parse-only execute the client
# builds, checked by decoding it with the server-side parser the Mirror uses.
#
# The option words are measured against a live 23ai, by capturing the reference
# client's own parse and diffing it against ours (docs/PROTOCOL.md §20.4).

import unittest
from contextlib import contextmanager

from seerdb.common.tns import (
    _DECODE_FIELD_VERSION,
    _ENCODE_FIELD_VERSION,
    encode_dictionary_exec,
    parse_exec,
    parse_options,
)
from seerdb.common.tns_consts import (
    FIELD_VERSION_11_2,
    FIELD_VERSION_23_1,
    FIELD_VERSION_23_4,
)

_EXEC_OPTION_PARSE = 0x01
_EXEC_OPTION_EXECUTE = 0x20
_EXEC_OPTION_DESCRIBE = 0x20000


def _exec_bytes(sql: str, kind: str, *, parse_only: bool, version: int) -> bytes:
    """The wire bytes of one execute, as `OracleConnect.execute` builds them."""
    return encode_dictionary_exec(
        {
            'field_version': version,
            'seq': 3,
            'query': {
                'type': kind,
                'auto': 0,
                'fetch': 100,
                'server_version': 0,
                'cursor': 0,
                'query': sql,
                'bind': [],
                'batch': [],
                'def': [],
                'batcherrors': False,
                'arraydmlrowcounts': False,
                'return_binds': None,
                'scrollable': False,
                'scroll': None,
                'parse_only': parse_only,
            },
        }
    )


@contextmanager
def _at_field_version(version: int):
    # Pin both codec contextvars, the way the Mirror's session loop does per
    # request — the decoder reads the 12c+ execute fields only when told to.
    d_tok, e_tok = (
        _DECODE_FIELD_VERSION.set(version),
        _ENCODE_FIELD_VERSION.set(version),
    )
    try:
        yield
    finally:
        _DECODE_FIELD_VERSION.reset(d_tok)
        _ENCODE_FIELD_VERSION.reset(e_tok)


def _options(payload: bytes, version: int) -> int:
    # The option word is the first sb4 after the TTI_FUN header — three bytes
    # (TTI_FUN, TTI_ALL8, seq), plus the 23ai token number.
    head = 4 if version > FIELD_VERSION_23_1 else 3
    width = payload[head]
    return int.from_bytes(payload[head + 1 : head + 1 + width], 'big')


class TestParseOnlyRequest(unittest.TestCase):
    def test_a_query_parse_asks_for_parse_and_describe_only(self):
        payload = _exec_bytes(
            'select 1 from dual', 'select', parse_only=True, version=FIELD_VERSION_23_4
        )
        self.assertEqual(
            _options(payload, FIELD_VERSION_23_4),
            _EXEC_OPTION_PARSE | _EXEC_OPTION_DESCRIBE,
        )
        with _at_field_version(FIELD_VERSION_23_4):
            request = parse_exec(payload)
        self.assertTrue(request.parse_only)
        self.assertTrue(request.describe_only)

    def test_any_other_parse_asks_for_parse_alone(self):
        for kind, sql in (
            ('change', 'insert into t (v) values (:1)'),
            ('block', 'begin null; end;'),
        ):
            with self.subTest(kind=kind):
                payload = _exec_bytes(
                    sql, kind, parse_only=True, version=FIELD_VERSION_23_4
                )
                self.assertEqual(
                    _options(payload, FIELD_VERSION_23_4), _EXEC_OPTION_PARSE
                )
                with _at_field_version(FIELD_VERSION_23_4):
                    request = parse_exec(payload)
                self.assertTrue(request.parse_only)
                self.assertFalse(request.describe_only)

    def test_what_makes_it_a_parse_is_the_absence_of_execute(self):
        # Not the PARSE bit: an ordinary execute sets that too. Guarding the
        # invariant `parse_options` is built on, from the encode side.
        for version in (FIELD_VERSION_11_2, FIELD_VERSION_23_4):
            for kind, sql in (
                ('select', 'select 1 from dual'),
                ('change', 'insert into t (v) values (:1)'),
                ('block', 'begin null; end;'),
            ):
                with self.subTest(version=version, kind=kind):
                    run = _exec_bytes(sql, kind, parse_only=False, version=version)
                    parse = _exec_bytes(sql, kind, parse_only=True, version=version)
                    run_opt = _options(run, version)
                    parse_opt = _options(parse, version)
                    self.assertTrue(run_opt & _EXEC_OPTION_EXECUTE)
                    self.assertTrue(run_opt & _EXEC_OPTION_PARSE)
                    self.assertFalse(parse_opt & _EXEC_OPTION_EXECUTE)
                    self.assertTrue(parse_options(parse_opt)[0])

    def test_a_query_parse_drops_the_query_execute_fields(self):
        # Two fields ride with the EXECUTE bit on a 23ai SELECT and have to come
        # off with it: the prefetch row count (1, not the connection's `fetch`)
        # and the query flag in al8i4[9]. Leaving either in place asks the server
        # to fetch from a cursor the parse never positioned -- ORA-01002.
        run = _exec_bytes(
            'select 1 from dual', 'select', parse_only=False, version=FIELD_VERSION_23_4
        )
        parse = _exec_bytes(
            'select 1 from dual', 'select', parse_only=True, version=FIELD_VERSION_23_4
        )
        self.assertIn(bytes([0x01, 0x64]), run)  # fetch = 100
        self.assertNotIn(bytes([0x01, 0x64]), parse)
        self.assertIn(bytes([0x02, 0x80, 0x00]), run)  # al8i4[9] = 0x8000
        self.assertNotIn(bytes([0x02, 0x80, 0x00]), parse)

    def test_the_parse_still_carries_the_statement(self):
        # The whole point: a parse sends the SQL (it is what the server parses).
        payload = _exec_bytes(
            'select 1 from dual', 'select', parse_only=True, version=FIELD_VERSION_23_4
        )
        with _at_field_version(FIELD_VERSION_23_4):
            self.assertEqual(parse_exec(payload).sql, 'select 1 from dual')


if __name__ == '__main__':
    unittest.main()
