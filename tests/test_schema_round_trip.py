# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Every wire schema decodes what it encodes (#2290).

A schema (seerdb/common/wire_schema.py) says what a message carries once, and
compile_encoder / compile_decoder build both directions from it with a codec.
So for every schema in seerdb/common/tns.py, in every codec, encoding a set of
field values and decoding the bytes must give the same values back, at each
field version where a codec's framing changes. tns.py compiles many schemas in
one direction only; this compiles both, here.

The values are built from the schema itself: each field kind takes the edges it
can hold (0, a byte's and a ub2's limits, empty and over-252-byte values, an
absent `when` branch, an empty `each`), a different mix per sample. A schema
with a `raw` field holds bytes already encoded and has no decoder; those are
listed, so that a new one shows up in review.
"""

import itertools
import unittest
from typing import Any

from seerdb.common import tns, wire_schema
from seerdb.common.tns_consts import (
    FIELD_VERSION_11_2,
    FIELD_VERSION_12_1,
    FIELD_VERSION_23_1_EXT_1,
    FIELD_VERSION_23_4,
)

CODECS = {
    'thin': tns._THIN,
    'oci': tns._OCI,
    'oci-wide': tns._OCI_WIDE,
    'o8i': tns._O8I,
}
# The field versions at which a codec's framing changes: thin chunks long
# values with one-byte lengths before 12.1 and ub4 ones from it.
FIELD_VERSIONS = (FIELD_VERSION_11_2, FIELD_VERSION_12_1, FIELD_VERSION_23_4)
# Schemas holding a `raw` field: bytes already encoded, written as they are,
# with no length to read them back by. Encode-only by design.
ONE_WAY = {'_REFCURSOR_OUT', '_OBJECT_FRAME', '_LOB_READ_REPLY', '_DCB_COLUMN'}
SAMPLES = 12


def _auth_head(values: dict) -> None:
    # The user goes one way or the other, prefixed with its length or as the
    # bytes the header's own length counts, and that length is the user's.
    values['user_raw'] = 0 if values.get('user_prefixed') else 1
    values['user_length'] = len(values.get('user', b''))


# What a caller keeps to that the schema does not say, per schema.
CONTRACTS = {'_AUTH_HEAD': _auth_head}


def _schemas() -> dict[str, wire_schema.Schema]:
    # Every module-level `name: wire_schema.Schema = (...)` in tns.py.
    return {
        name: getattr(tns, name)
        for name, annotation in tns.__annotations__.items()
        if annotation in (wire_schema.Schema, 'wire_schema.Schema')
    }


def _has(schema: wire_schema.Schema, kind: str) -> bool:
    for field in schema:
        if field[0] == kind:
            return True
        if field[0] in ('when', 'only') and _has(field[2], kind):
            return True
        if field[0] == 'each' and _has(field[3], kind):
            return True
    return False


def _width(codec_name: str, kind: str) -> int:
    # The largest integer a kind holds in a codec, in bytes.
    if kind in ('u8', 'n1'):
        return 1
    if kind in ('n2', 'be2'):
        return 2
    if kind == 'n8' and codec_name in ('oci', 'oci-wide'):
        return 8
    if kind == 'wn4' and codec_name == 'oci-wide':
        return 8
    return 4


class _Values:
    # Field values for one sample: each kind cycles through its edges, from an
    # offset that differs per sample, so the samples mix them.

    def __init__(self, codec_name: str, sample: int, fv: int) -> None:
        self.codec_name = codec_name
        self.sample = sample
        self.fv = fv
        self.counter = itertools.count(sample)

    def _pick(self, choices: list) -> Any:
        return choices[next(self.counter) % len(choices)]

    def integer(self, kind: str) -> int:
        top = (1 << (8 * _width(self.codec_name, kind))) - 1
        return self._pick([v for v in (0, 1, 255, 256, 65535, 65536, top) if v <= top])

    def blob(self) -> bytes:
        return self._pick([b'', b'x', bytes(range(252)), bytes(range(256)) * 2])

    def fill(self, schema: wire_schema.Schema, values: dict[str, Any]) -> None:
        for field in schema:
            kind = field[0]
            if kind in ('const', 'pad'):
                continue
            if kind == 'only':
                if field[1] == self.codec_name.split('-')[0] or (
                    field[1] == 'oci' and self.codec_name == 'o8i'
                ):
                    self.fill(field[2], values)
                continue
            if kind == 'when':
                # The test is a field read before, or what the caller gives.
                if field[1] not in values:
                    values[field[1]] = self._pick([0, 1])
                if values[field[1]]:
                    self.fill(field[2], values)
                continue
            if kind == 'each':
                count = self._pick([0, 1, 2])
                values[field[2]] = count
                items = []
                for _ in range(count):
                    item: dict[str, Any] = {}
                    self.fill(field[3], item)
                    items.append(item)
                values[field[1]] = items
                continue
            if kind == 'derive':
                values[field[1]] = field[2](values)
                continue
            name = field[1]
            if name in values:
                # A length or count chosen for a later field already set it.
                continue
            if kind == 'bytes':
                size = field[2]
                if not isinstance(size, int):
                    if size not in values:
                        values[size] = self._pick([0, 1, 5])
                    size = values[size]
                values[name] = bytes(range(size % 256)) * (size // 256) + bytes(
                    range(size % 256)
                )
                values[name] = values[name][:size]
            elif kind in ('u8', 'n1', 'n2', 'n4', 'n8', 'wn4', 'be2', 'be4'):
                values[name] = self.integer(kind)
            elif kind == 'scale':
                values[name] = self._pick([0, 1, 127, -1, -127])
            elif kind == 'ptr':
                values[name] = self._pick([False, True])
            elif kind == 'token':
                # Thin sends a function's token from 23ai and OCI never. The
                # encoder writes whatever it is given and the decoder reads by
                # the field version, so a caller gives None below 23ai.
                thin_token = (
                    self.codec_name == 'thin' and self.fv >= FIELD_VERSION_23_1_EXT_1
                )
                values[name] = self._pick([0, 7]) if thin_token else None
            elif kind in ('value', 'chr', 'lob'):
                values[name] = self.blob()
            elif kind == 'long':
                values[name] = self._pick([None, *[self.blob()]])
            else:
                raise AssertionError(f'no sample for field kind {kind!r}')


def _needed_ahead(schema: wire_schema.Schema) -> set[str]:
    # The names a `bytes` length or an `each` count refers to, which a sample
    # chooses with the field that uses them.
    names: set[str] = set()
    for field in schema:
        if field[0] == 'bytes' and not isinstance(field[2], int):
            names.add(field[2])
        elif field[0] == 'each':
            names.add(field[2])
            names |= _needed_ahead(field[3])
        elif field[0] in ('when', 'only'):
            names |= _needed_ahead(field[2])
    return names


def _sample(
    name: str, schema: wire_schema.Schema, codec_name: str, sample: int, fv: int
) -> dict:
    values: dict[str, Any] = {}
    gen = _Values(codec_name, sample, fv)
    # A length or count read before the field it sizes is set from what that
    # field holds: choose those first, so the earlier field writes them.
    for ahead in _needed_ahead(schema):
        values[ahead] = gen._pick([0, 1, 2])
    gen.fill(schema, values)
    if name in CONTRACTS:
        CONTRACTS[name](values)
    return values


def _same(sent: Any, got: Any) -> bool:
    if isinstance(sent, list):
        return (
            isinstance(got, list)
            and len(sent) == len(got)
            and all(_same(a, b) for a, b in zip(sent, got))
        )
    if isinstance(sent, dict):
        return isinstance(got, dict) and all(
            k in got and _same(v, got[k]) for k, v in sent.items()
        )
    return sent == got


class TestEverySchemaRoundTrips(unittest.TestCase):
    def test_the_one_way_schemas_are_those_with_a_raw_field(self):
        raw = {name for name, schema in _schemas().items() if _has(schema, 'raw')}
        self.assertEqual(raw, ONE_WAY)

    def test_every_schema_decodes_what_it_encodes(self):
        schemas = _schemas()
        self.assertGreater(len(schemas), 25)
        for name, schema in schemas.items():
            if name in ONE_WAY:
                continue
            for codec_name, codec in CODECS.items():
                encode = wire_schema.compile_encoder(schema, codec)
                decode = wire_schema.compile_decoder(schema, codec)
                for fv in FIELD_VERSIONS:
                    enc = tns._ENCODE_FIELD_VERSION.set(fv)
                    dec = tns._DECODE_FIELD_VERSION.set(fv)
                    try:
                        for sample in range(SAMPLES):
                            values = _sample(name, schema, codec_name, sample, fv)
                            with self.subTest(
                                schema=name, codec=codec_name, fv=fv, sample=sample
                            ):
                                wire = encode(values)
                                (got, rest) = decode(wire + b'\xee', values)
                                self.assertEqual(rest, b'\xee')
                                self.assertTrue(
                                    _same(values, got),
                                    f'{values!r} came back as {got!r}',
                                )
                    finally:
                        tns._ENCODE_FIELD_VERSION.reset(enc)
                        tns._DECODE_FIELD_VERSION.reset(dec)


if __name__ == '__main__':
    unittest.main()
