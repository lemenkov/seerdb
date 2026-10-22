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
- ``('chr', name)``: a character or RAW cell's bytes, framed as each dialect
  frames text;
- ``('lob', name)``: LOB_DATA content, always chunked unless empty;
- ``('scale', name)``: a describe column's scale, one signed byte, which thin
  before 12.1 writes in its variable-width signed form;
- ``('long', name)``: a LONG / LONG RAW cell's content, always chunked, and
  ``None`` for NULL;
- ``('token', name)``: a function call's token number, which thin sends from
  23ai (``None`` for none) and OCI never;
- ``('ptr', name)``: whether a pointer is present, a flag byte in thin, an
  8-byte pointer in OCI;
- ``('n8', name)``: a 64-bit count, TTC variable-width in thin, a ub8
  little-endian in OCI;
- ``('wn4', name)``: a count that a wide OCI preamble writes as a ub8, and is
  an ``n4`` otherwise;
- ``('bytes', name, length)``: as many bytes as the integer ``length``
  says, as they are;
- ``('raw', name)``: bytes already encoded, written as they are; a schema
  that has one encodes only;
- ``('when', name, fields)``: ``fields`` only when the integer ``name`` is not
  zero;
- ``('each', name, count, fields)``: ``fields`` once per item of the list
  ``name``, whose length the integer ``count`` gives;
- ``('only', dialect, fields)``: ``fields`` in that dialect alone.

A codec has a ``name`` (``'thin'`` or ``'oci'``) and the primitives ``n4``,
``n2``, ``value``, ``lob``, ``token``, ``ptr`` and ``wn4``, and to decode the
same names after ``read_``, each taking the bytes and returning the value
and the rest. This module knows no codec: :mod:`seerdb.common.tns` defines
them, beside the encoders they replaced.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

Schema = tuple[tuple[Any, ...], ...]
# The field kinds a codec packs itself.
_PRIMITIVES = (
    'n4',
    'n2',
    'value',
    'chr',
    'lob',
    'long',
    'scale',
    'token',
    'ptr',
    'wn4',
    'n8',
)
Encoder = Callable[[dict[str, Any]], bytes]
Decoder = Callable[..., tuple[dict[str, Any], bytes]]


class Codec(Protocol):
    name: str

    def n4(self, value: int) -> bytes: ...
    def n2(self, value: int) -> bytes: ...
    def value(self, data: bytes) -> bytes: ...
    def lob(self, data: bytes) -> bytes: ...
    def chr(self, data: str | bytes) -> bytes: ...
    def long(self, data: bytes | None) -> bytes: ...
    def scale(self, value: int) -> bytes: ...
    def token(self, value: int | None) -> bytes: ...
    def ptr(self, value: bool) -> bytes: ...
    def wn4(self, value: int) -> bytes: ...
    def n8(self, value: int) -> bytes: ...
    def read_n4(self, data: bytes) -> tuple[int, bytes]: ...
    def read_n2(self, data: bytes) -> tuple[int, bytes]: ...
    def read_value(self, data: bytes) -> tuple[bytes, bytes]: ...
    def read_lob(self, data: bytes) -> tuple[bytes, bytes]: ...
    def read_chr(self, data: bytes) -> tuple[bytes, bytes]: ...
    def read_long(self, data: bytes) -> tuple[bytes | None, bytes]: ...
    def read_scale(self, data: bytes) -> tuple[int, bytes]: ...
    def read_token(self, data: bytes) -> tuple[int | None, bytes]: ...
    def read_ptr(self, data: bytes) -> tuple[bool, bytes]: ...
    def read_wn4(self, data: bytes) -> tuple[int, bytes]: ...
    def read_n8(self, data: bytes) -> tuple[int, bytes]: ...


def _encode_part(field: tuple[Any, ...], codec: Codec) -> Callable[[dict], bytes]:
    kind = field[0]
    if kind == 'const':
        literal = field[1]
        return lambda values: literal
    if kind == 'u8':
        name = field[1]
        return lambda values: bytes((values[name],))
    if kind in ('raw', 'bytes'):
        name = field[1]
        return lambda values: values[name]
    if kind in _PRIMITIVES:
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
    and the bytes that follow the message. A ``when`` that tests what isn't on
    the wire -- the session's field version, say -- reads it from ``given``,
    which the result includes."""

    def decode(
        data: bytes, given: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], bytes]:
        values: dict[str, Any] = dict(given or {})
        for field in schema:
            kind = field[0]
            if kind == 'const':
                if not data.startswith(field[1]):
                    raise ValueError(f'expected {field[1].hex()} at {data[:8].hex()}')
                data = data[len(field[1]) :]
            elif kind == 'u8':
                (values[field[1]], data) = (data[0], data[1:])
            elif kind == 'raw':
                raise ValueError('a raw field has no length to decode by')
            elif kind == 'bytes':
                size = values[field[2]]
                (values[field[1]], data) = (bytes(data[:size]), data[size:])
            elif kind in _PRIMITIVES:
                (values[field[1]], data) = getattr(codec, 'read_' + kind)(data)
            elif kind == 'when':
                if values[field[1]]:
                    (more, data) = compile_decoder(field[2], codec)(data, values)
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
                    (more, data) = compile_decoder(field[2], codec)(data, values)
                    values.update(more)
            else:
                raise ValueError(f'unknown field kind {kind!r}')
        return values, data

    return decode
