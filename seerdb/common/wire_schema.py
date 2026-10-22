# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""One message schema, two serialisations (#2064).

Thin and OCI clients write the same TTC messages, but pack their primitives
differently: thin writes TTC's variable-width integers, OCI writes fixed-width
little-endian ones, and their long values are chunked to different rules. A
schema says *what* a message carries, once; a codec says *how* a dialect packs
each primitive. :func:`compile_encoder` and :func:`compile_decoder` turn the
pair into an encoder and a decoder, once, at import.

A schema is a tuple of fields:

- ``('const', b'...')``: literal bytes, the same in every dialect;
- ``('u8', name)``: one byte, the same in every dialect;
- ``('n4', name)`` / ``('n2', name)``: an integer the codec packs, TTC
  variable-width in thin, a ub4 / ub2 little-endian in OCI;
- ``('value', name)``: bytes with their length, chunked past 252 bytes;
- ``('lob', name)``: LOB_DATA content, always chunked unless empty;
- ``('when', name, fields)``: ``fields`` only when the integer ``name`` is not
  zero;
- ``('each', name, count, fields)``: ``fields`` once per item of the list
  ``name``, whose length the integer ``count`` gives;
- ``('only', dialect, fields)``: ``fields`` in that dialect alone.

A codec has a ``name`` (``'thin'`` or ``'oci'``) and the primitives ``n4``,
``n2``, ``value`` and ``lob``, and to decode ``read_n4``, ``read_n2``,
``read_value`` and ``read_lob``, each taking the bytes and returning the value
and the rest. This module knows no codec: :mod:`seerdb.common.tns` defines
them, beside the encoders they replaced.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

Schema = tuple[tuple[Any, ...], ...]
Encoder = Callable[[dict[str, Any]], bytes]
Decoder = Callable[[bytes], tuple[dict[str, Any], bytes]]


class Codec(Protocol):
    name: str

    def n4(self, value: int) -> bytes: ...
    def n2(self, value: int) -> bytes: ...
    def value(self, data: bytes) -> bytes: ...
    def lob(self, data: bytes) -> bytes: ...
    def read_n4(self, data: bytes) -> tuple[int, bytes]: ...
    def read_n2(self, data: bytes) -> tuple[int, bytes]: ...
    def read_value(self, data: bytes) -> tuple[bytes, bytes]: ...
    def read_lob(self, data: bytes) -> tuple[bytes, bytes]: ...


def _encode_part(field: tuple[Any, ...], codec: Codec) -> Callable[[dict], bytes]:
    kind = field[0]
    if kind == 'const':
        literal = field[1]
        return lambda values: literal
    if kind == 'u8':
        name = field[1]
        return lambda values: bytes((values[name],))
    if kind in ('n4', 'n2', 'value', 'lob'):
        (pack, name) = (getattr(codec, kind), field[1])
        return lambda values: pack(values[name])
    if kind == 'when':
        (name, inner) = (field[1], compile_encoder(field[2], codec))
        return lambda values: inner(values) if values[name] else b''
    if kind == 'each':
        (name, inner) = (field[1], compile_encoder(field[3], codec))
        return lambda values: b''.join([inner(item) for item in values[name]])
    if kind == 'only':
        if field[1] != codec.name:
            return lambda values: b''
        return compile_encoder(field[2], codec)
    raise ValueError(f'unknown field kind {kind!r}')


def compile_encoder(schema: Schema, codec: Codec) -> Encoder:
    """The encoder of ``schema`` in ``codec``'s dialect: the message's bytes
    from a dict of its fields' values."""
    parts = [_encode_part(field, codec) for field in schema]
    if len(parts) == 1:
        return parts[0]
    return lambda values: b''.join([part(values) for part in parts])


def compile_decoder(schema: Schema, codec: Codec) -> Decoder:
    """The decoder of ``schema`` in ``codec``'s dialect: the fields' values
    and the bytes that follow the message."""

    def decode(data: bytes) -> tuple[dict[str, Any], bytes]:
        values: dict[str, Any] = {}
        for field in schema:
            kind = field[0]
            if kind == 'const':
                if not data.startswith(field[1]):
                    raise ValueError(f'expected {field[1].hex()} at {data[:8].hex()}')
                data = data[len(field[1]) :]
            elif kind == 'u8':
                (values[field[1]], data) = (data[0], data[1:])
            elif kind in ('n4', 'n2', 'value', 'lob'):
                (values[field[1]], data) = getattr(codec, 'read_' + kind)(data)
            elif kind == 'when':
                if values[field[1]]:
                    (more, data) = compile_decoder(field[2], codec)(data)
                    values.update(more)
            elif kind == 'each':
                inner = compile_decoder(field[3], codec)
                items = []
                for _ in range(values[field[2]]):
                    (item, data) = inner(data)
                    items.append(item)
                values[field[1]] = items
            elif kind == 'only':
                if field[1] == codec.name:
                    (more, data) = compile_decoder(field[2], codec)(data)
                    values.update(more)
            else:
                raise ValueError(f'unknown field kind {kind!r}')
        return values, data

    return decode
