# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Spike for #2064: one message schema, two primitive codecs. Not for merging.

A message is a tuple of fields. Each field names what it carries; the codec
decides how it is packed:

- ``('const', b'...')``: literal bytes, the same in every dialect;
- ``('u8', name)``: one byte, the same in every dialect;
- ``('n4', name)`` / ``('n2', name)``: an integer, TTC variable-width in thin,
  a ub4 / ub2 little-endian in OCI;
- ``('value', name)``: bytes with a length, chunked past 252 bytes;
- ``('lob', name)``: LOB_DATA content, always chunked unless empty;
- ``('when', name, fields)``: ``fields`` only when the integer ``name`` is
  not zero;
- ``('each', name, count, fields)``: ``fields`` once per item of the list
  ``name``, whose length the integer ``count`` gives;
- ``('only', dialect, fields)``: ``fields`` in that dialect alone.

One schema gives both the encoder and the decoder.
"""

from __future__ import annotations

from typing import Any

from .tns import _ENCODE_FIELD_VERSION, decode_ub4, encode_sb4
from .tns_consts import (
    FIELD_VERSION_12_1,
    TNS_LONG_LENGTH_INDICATOR,
    TNS_MAX_SHORT_LENGTH,
    TNS_SERVER_PIGGYBACK_SYNC,
    TTI_END_OF_RESPONSE,
    TTI_LOB,
    TTI_STA,
    TTI_SVR_PIGGYBACK,
)

Schema = tuple[tuple[Any, ...], ...]


class Thin:
    name = 'thin'
    value_chunk = 0x40
    lob_chunk = 0xFF

    def n4(self, value: int) -> bytes:
        return encode_sb4(value)

    n2 = n4

    def read_n4(self, data: bytes) -> tuple[int, bytes]:
        return decode_ub4(data)

    read_n2 = read_n4

    def chunk_length(self, value: int) -> bytes:
        # A chunk's length: a ub1 below 12.1, TTC's variable width from it.
        if _ENCODE_FIELD_VERSION.get() >= FIELD_VERSION_12_1:
            return encode_sb4(value)
        return bytes([value])

    def read_chunk_length(self, data: bytes) -> tuple[int, bytes]:
        if _ENCODE_FIELD_VERSION.get() >= FIELD_VERSION_12_1:
            return decode_ub4(data)
        return data[0], data[1:]


class Oci:
    name = 'oci'
    value_chunk = 0xFF
    lob_chunk = 0xFF

    def n4(self, value: int) -> bytes:
        return value.to_bytes(4, 'little')

    def n2(self, value: int) -> bytes:
        return value.to_bytes(2, 'little')

    def read_n4(self, data: bytes) -> tuple[int, bytes]:
        return int.from_bytes(data[:4], 'little'), data[4:]

    def read_n2(self, data: bytes) -> tuple[int, bytes]:
        return int.from_bytes(data[:2], 'little'), data[2:]

    def chunk_length(self, value: int) -> bytes:
        return bytes([value])

    def read_chunk_length(self, data: bytes) -> tuple[int, bytes]:
        return data[0], data[1:]


THIN = Thin()
OCI = Oci()


def _chunks(codec: Thin | Oci, data: bytes, size: int) -> bytes:
    out = bytearray([TNS_LONG_LENGTH_INDICATOR])
    for at in range(0, len(data), size):
        chunk = data[at : at + size]
        out += codec.chunk_length(len(chunk)) + chunk
    return bytes(out + codec.chunk_length(0))


def _read_chunks(codec: Thin | Oci, data: bytes) -> tuple[bytes, bytes]:
    out = bytearray()
    while True:
        (length, data) = codec.read_chunk_length(data)
        if not length:
            return bytes(out), data
        out += data[:length]
        data = data[length:]


def encode(schema: Schema, codec: Thin | Oci, values: dict[str, Any]) -> bytes:
    out = bytearray()
    for field in schema:
        kind = field[0]
        if kind == 'const':
            out += field[1]
        elif kind == 'u8':
            out.append(values[field[1]])
        elif kind in ('n4', 'n2'):
            out += getattr(codec, kind)(values[field[1]])
        elif kind == 'value':
            data = values[field[1]]
            if len(data) <= TNS_MAX_SHORT_LENGTH:
                out += bytes([len(data)]) + data
            else:
                out += _chunks(codec, data, codec.value_chunk)
        elif kind == 'lob':
            data = values[field[1]]
            out += _chunks(codec, data, codec.lob_chunk) if data else b'\x00'
        elif kind == 'when':
            if values[field[1]]:
                out += encode(field[2], codec, values)
        elif kind == 'each':
            for item in values[field[1]]:
                out += encode(field[3], codec, item)
        elif kind == 'only':
            if codec.name == field[1]:
                out += encode(field[2], codec, values)
        else:
            raise ValueError(f'unknown field kind {kind!r}')
    return bytes(out)


def decode(schema: Schema, codec: Thin | Oci, data: bytes) -> tuple[dict, bytes]:
    values: dict[str, Any] = {}
    for field in schema:
        kind = field[0]
        if kind == 'const':
            if not data.startswith(field[1]):
                raise ValueError(f'expected {field[1].hex()} at {data[:8].hex()}')
            data = data[len(field[1]) :]
        elif kind == 'u8':
            (values[field[1]], data) = (data[0], data[1:])
        elif kind in ('n4', 'n2'):
            (values[field[1]], data) = getattr(codec, 'read_' + kind)(data)
        elif kind in ('value', 'lob'):
            length = data[0]
            if length == TNS_LONG_LENGTH_INDICATOR:
                (values[field[1]], data) = _read_chunks(codec, data[1:])
            elif kind == 'lob' and length:
                raise ValueError('LOB_DATA content is always chunked')
            else:
                (values[field[1]], data) = (data[1 : 1 + length], data[1 + length :])
        elif kind == 'when':
            if values[field[1]]:
                (more, data) = decode(field[2], codec, data)
                values.update(more)
        elif kind == 'each':
            items = []
            for _ in range(values[field[2]]):
                (item, data) = decode(field[3], codec, data)
                items.append(item)
            values[field[1]] = items
        elif kind == 'only':
            if codec.name == field[1]:
                (more, data) = decode(field[2], codec, data)
                values.update(more)
        else:
            raise ValueError(f'unknown field kind {kind!r}')
    return values, data


# --- The spike's four messages ------------------------------------------------

# TTI_STA: the OER call status and the end-to-end sequence.
STA: Schema = (
    ('const', bytes([TTI_STA])),
    ('n4', 'call_status'),
    ('n2', 'sequence'),
)
# A logoff's reply: the STA, and in thin the end-of-response marker.
LOGOFF_REPLY: Schema = STA + (
    ('only', 'thin', (('const', bytes([TTI_END_OF_RESPONSE])),)),
)

# TTI_LOB: LOB_DATA, its content always chunked.
LOB_DATA: Schema = (('const', bytes([TTI_LOB])), ('lob', 'content'))

# A value with its length, chunked past 252 bytes.
VALUE: Schema = (('value', 'value'),)

# The SYNC server piggyback: the DTYs, then keyword-value pairs, each a text
# value and a binary value, either empty, and the flags. Thin carries the
# value as text; OCI, as sqlplus reads it, as binary.
SYNC_PAIR: Schema = (
    ('n4', 'text_length'),
    ('when', 'text_length', (('value', 'text'),)),
    ('n4', 'binary_length'),
    ('when', 'binary_length', (('value', 'binary'),)),
    ('n2', 'keyword'),
)
SYNC: Schema = (
    ('const', bytes([TTI_SVR_PIGGYBACK, TNS_SERVER_PIGGYBACK_SYNC])),
    ('n2', 'dty_count'),
    ('u8', 'dty_length'),
    ('n4', 'pair_count'),
    ('u8', 'pairs_length'),
    ('each', 'pairs', 'pair_count', SYNC_PAIR),
    ('n4', 'flags'),
)


def sync_values(codec: Thin | Oci, pairs: list[tuple[int, str]]) -> dict[str, Any]:
    # The SYNC fields for (keyword, text) pairs: the one per-dialect choice is
    # which slot carries the value.
    items = []
    for keyword, text in pairs:
        value = text.encode('utf-8')
        slot = ('text', 'binary') if codec is THIN else ('binary', 'text')
        item = {f'{slot[0]}_length': len(value), f'{slot[1]}_length': 0}
        if value:
            item[slot[0]] = value
        items.append(item | {'keyword': keyword})
    return {
        'dty_count': 1,
        'dty_length': 0x10,
        'pair_count': len(pairs),
        'pairs_length': 0x16,
        'pairs': items,
        'flags': 0,
    }
