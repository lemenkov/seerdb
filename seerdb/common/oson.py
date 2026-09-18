# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Decoder for Oracle's OSON binary JSON image (the on-the-wire form of a
native ``JSON`` column, 21c+).

The format was reverse-engineered from images captured off a live 21c server
(see docs/PROTOCOL.md §17); every encoding below is backed by a captured sample
with known content. An OSON image is:

    magic "FF 4A 5A" | version(ub1) | flags(ub2) | body

``flags & 0x2000`` marks a *tree* (container) image; otherwise the body is a
single bare scalar. A tree body is::

    num_fnames(ub1) | fnames_seg_size(ub2) | tree_seg_size(ub2) | num_tiny(ub2)
    hash_array(num_fnames * 1)        # one hash byte per field name (unused here)
    offset_array(num_fnames * ub2)    # field-id -> offset into fnames_seg
    fnames_seg                        # the field names, each <ub1 len><utf8>
    tree_seg                          # the node tree, root at offset 0

**Version 3** (#826) adds a *second* field-name segment, for names too long for
the ub1 length prefix above. Its three sizing fields sit between
``fnames_seg_size`` and ``tree_seg_size``::

    secondary_flags(ub2) | num_long_fnames(ub4) | long_fnames_seg_size(ub4)

and the segment itself follows the short one::

    long_hash_array(num_long * 2)     # TWO bytes per name here, not one
    long_offset_array(num_long * ub2 or ub4)   # ub2 when secondary flag 0x0100
    long_fnames_seg                   # the names, each <ub2 len><utf8>

The two segments share one field-id space: ids ``1..num_fnames`` index the short
segment, the rest index the long one. A live 23ai emits version 3 only when a
long name is actually present, and only for documents the **server** encoded --
from a JSON-text insert, say. An image the client encoded stays version 1.

Nodes (within tree_seg, or the lone scalar of a non-tree image):

    0x00..0x1F  short string, length = tag, then that many UTF-8 bytes
    0x20..0x2F  number, Oracle NUMBER of (tag - 0x1F) bytes
    0x30        null      0x31  true      0x32  false
    0x33        string, ub1 length prefix, then UTF-8 bytes
    0x34        number, ub1 length prefix, then Oracle NUMBER bytes
    (tag & 0xC0) == 0x80   object: count(ub1), field_id(ub1)*count,
                           value_offset(ub2)*count   (offsets rel. to tree_seg)
    (tag & 0xC0) == 0xC0   array:  count(ub1), value_offset(ub2)*count

A field id is 1-based; ``offset_array[id-1]`` locates its name in fnames_seg.

Extended scalar nodes (binary double/float, date, timestamp, interval) are
decoded too (see ``_EXT_SCALAR``). Not yet covered (raise ``OsonError`` rather
than decode wrong): images whose flags select ub4 segment sizes / ub4 node
offsets (oracledb-produced and large documents) and ub2 field-ids (>255 distinct
keys) — both tracked under #69.
"""

from seerdb.common.types import (
    decode_binary_double,
    decode_binary_float,
    decode_date,
    decode_interval_ds,
    decode_interval_ym,
    decode_number,
)

OSON_MAGIC = b'\xff\x4a\x5a'

# Extended scalar node tags (#69): each is a tag byte followed by a fixed-width
# Oracle binary value (no length prefix — the width is intrinsic to the type),
# decoded by the same routines used for the column wire forms. Tag values
# reverse-engineered from JSON_SCALAR(<native>) images captured on 21c (each
# backed by a fixture in tests/test_oson.py); binary_float/double are stored in
# the order-preserving ("sortable") form, which decode_binary_* already invert.
_EXT_SCALAR = {
    0x36: (8, decode_binary_double),
    0x7F: (4, decode_binary_float),
    0x3C: (7, decode_date),  # DATE
    0x7D: (7, decode_date),  # DATE (variant seen in ub4-offset images)
    0x39: (11, decode_date),  # TIMESTAMP
    0x7C: (13, decode_date),  # TIMESTAMP WITH TIME ZONE
    0x3D: (5, decode_interval_ym),
    0x3E: (11, decode_interval_ds),
}

# Image flags (header ub2).
_FLAG_TREE = 0x2000  # container image (object/array) vs bare scalar
_FLAG_REL_OFFSETS = 0x01  # container value-offsets are relative to the
# container's own offset, not absolute in the tree
# segment. A `store as (compress high)` JSON column
# is written this way (#826).
_FLAG_UB2_OFFSETS = 0x04  # container value-offsets are ub2; else ub4 (#69).
# Server JSON_OBJECT / JSON() literals set it;
# oracledb-produced images (flags 0x2102) clear it
# and use ub4 offsets.
_FLAG_UB2_FNAMES = 0x0400  # num_fnames is ub2 (object with > 255 field names);
# else ub1 (#69). A container node tag with the
# 0x08 bit then also has a ub2 count + ub2 field-ids.
_FLAG_UB4_TREE_SIZE = 0x1000  # tree-segment size is ub4, not ub2 (tree > 64 KiB);
# set on large documents (#88).
_FLAG_UB4_FNAMES_SIZE = 0x0800  # field-name segment size (and its offset array)
# are ub4, not ub2.
# Image versions. Version 3 adds a second field-name segment for names longer
# than 255 bytes, whose length prefix is ub2 rather than ub1 (#826). A live 23ai
# emits it only when such a name is present -- a document the SERVER encoded from
# JSON text keeps version 1 while every field name is short.
_VERSION_FNAME_255 = 1
_VERSION_FNAME_65535 = 3
# Version 3's own flags word, read straight after the field-name segment size.
_SEC_FLAG_UB2_FNAME_OFFSETS = 0x0100  # long-name offset array is ub2, else ub4
_TAG_WIDE_COUNT = 0x08  # container count + field-ids are ub2, not ub1
_TAG_UB4_COUNT = 0x10  # container count + field-ids are ub4 (> 65535
# entries/keys, #88); takes precedence over 0x08.
_TAG_COUNT_BITS = 0x18  # the two bits selecting a container's count width
_TAG_SHARED_FIELDS = 0x18  # ...and, when BOTH are set, "my field ids live in
# another container, whose offset follows" (#826)
_TAG_UB4_OFFSETS = 0x20  # this container's value-offsets are ub4 (a container
# whose values span a >64 KiB tree, #88), overriding
# the image-level offset width.


from seerdb.common.exceptions import NotSupportedError


class OsonError(Exception):
    """Raised on an OSON image whose encoding we do not yet decode."""


def json_to_text(value: object) -> str:
    """Serialise a Python value to JSON text for a JSON bind (#50).

    The text-cast fallback: seerdb binds this string and the server casts it to
    the column's JSON type. It is used when :func:`encode_oson` cannot build a
    compact native image (a wide / large document, #70) — the native binary
    OSON encoder covers the small-document shape the server accepts inline.
    ``Decimal`` is emitted as a JSON number (integral values stay exact; others
    go through ``float``); other unsupported types raise ``TypeError`` from
    :func:`json.dumps`. ``ensure_ascii=False`` keeps UTF-8 text natural (seerdb
    advertises AL32UTF8)."""
    import json
    from decimal import Decimal

    def default(o):
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        raise TypeError(f'object of type {type(o).__name__} is not JSON-serialisable')

    return json.dumps(value, ensure_ascii=False, default=default)


def _width(n: int) -> int:
    # The byte width a count / field-id / offset of magnitude `n` needs: ub1,
    # ub2 or ub4. Mirrors the widths decode_oson reads (#69/#88).
    return 1 if n <= 0xFF else (2 if n <= 0xFFFF else 4)


def _count_bits(width: int) -> int:
    # The container-tag bits selecting a ub2 / ub4 count + field-id width; ub1
    # sets neither (the compact form).
    return 0x00 if width == 1 else (_TAG_WIDE_COUNT if width == 2 else _TAG_UB4_COUNT)


def _oson_str_node(b: bytes) -> bytes:
    # A string node in the narrowest form its length allows: inline (<= 0x1F),
    # 0x33 ub1 (<= 0xFF), 0x37 ub2 (<= 0xFFFF, #88), else 0x38 ub4 (#88).
    n = len(b)
    if n <= 0x1F:
        return bytes([n]) + b
    if n <= 0xFF:
        return b'\x33' + bytes([n]) + b
    if n <= 0xFFFF:
        return b'\x37' + n.to_bytes(2, 'big') + b
    return b'\x38' + n.to_bytes(4, 'big') + b


def _oson_scalar_node(value, allow_wide: bool = False) -> bytes:
    import array
    import datetime
    from decimal import Decimal

    from seerdb.common.datatypes import IntervalYM
    from seerdb.common.tns import (
        encode_token_datetime,
        encode_token_decimal,
        encode_token_interval_ds,
        encode_token_interval_ym,
        encode_token_num,
    )

    if isinstance(value, array.array):
        # A VECTOR rides as the EXTENDED wrapper: 0x7b, sub-tag 0x01, a ub4
        # length, then the bare vector image (the inverse of the decoder).
        from seerdb.common.vector import encode_vector

        img = encode_vector(value)
        return b'\x7b\x01' + len(img).to_bytes(4, 'big') + img
    if value is None:
        return b'\x30'
    if value is True:
        return b'\x31'
    if value is False:
        return b'\x32'
    if isinstance(value, str):
        b = value.encode('utf-8')
        if len(b) > 0xFF and not allow_wide:
            raise OsonError('string too long for the native OSON encoder')
        return _oson_str_node(b)
    if isinstance(value, (bytes, bytearray)):
        # RAW: a ub2 (0x3a) or, past 64 KiB, a ub4 (0x3b) length then the bytes.
        raw = bytes(value)
        if len(raw) <= 0xFFFF:
            return b'\x3a' + len(raw).to_bytes(2, 'big') + raw
        return b'\x3b' + len(raw).to_bytes(4, 'big') + raw
    if isinstance(value, Decimal):
        nb = encode_token_decimal(value)
        return b'\x34' + bytes([len(nb)]) + nb
    if isinstance(value, datetime.datetime):
        # encode_token_datetime picks the width from the value; pair it with the
        # tag the decoder reads at that width: 13 B -> TIMESTAMP_TZ (0x7c),
        # 11 B -> TIMESTAMP (0x39), 7 B -> the 7-byte timestamp form (0x7d).
        if value.tzinfo is not None:
            tag = 0x7C
        elif value.microsecond:
            tag = 0x39
        else:
            tag = 0x7D
        return bytes([tag]) + encode_token_datetime(value)
    if isinstance(value, datetime.date):  # a bare date -> DATE (0x3c, 7 B)
        midnight = datetime.datetime(value.year, value.month, value.day)
        return b'\x3c' + encode_token_datetime(midnight)
    if isinstance(value, datetime.timedelta):  # INTERVAL DAY TO SECOND (0x3e)
        return b'\x3e' + encode_token_interval_ds(value)
    if isinstance(value, IntervalYM):  # INTERVAL YEAR TO MONTH (0x3d)
        return b'\x3d' + encode_token_interval_ym(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        nb = encode_token_num(value)
        return b'\x34' + bytes([len(nb)]) + nb
    raise OsonError(f'cannot OSON-encode {type(value).__name__}')


def _oson_emit(
    value, buf: bytearray, fid, off_size: int, nfw: int, allow_wide: bool
) -> int:
    # Append `value`'s node to `buf`; return its start offset. A container's
    # count + field-ids share a width (ub1 / ub2 / ub4, selected by the tag's
    # 0x08 / 0x10 bits) sized to the larger of its entry count and — for an
    # object — the field-id space `nfw`; value-offsets are `off_size` wide. The
    # compact form (all ub1, ub2 offsets, tag 0x84 / 0xC4) is the small-document
    # case and stays byte-for-byte what the server sends. With `allow_wide` off,
    # a container that would need a wider count raises so the compact-only
    # callers fall back to the text cast (#70); on the Mirror read path it is set
    # so a wide / large document (#69/#88) re-encodes for the client's decoder.
    start = len(buf)
    if isinstance(value, dict):
        items = list(value.items())
        cw = max(_width(len(items)), nfw)
        if cw > 1 and not allow_wide:
            raise OsonError('object too wide for the native OSON encoder')
        buf += bytes([0x80 | _count_bits(cw) | 0x04]) + len(items).to_bytes(cw, 'big')
        for k in value:
            buf += fid(k).to_bytes(cw, 'big')
        off_pos = len(buf)
        buf += b'\x00' * (off_size * len(items))
        offs = [_oson_emit(v, buf, fid, off_size, nfw, allow_wide) for _, v in items]
        for i, o in enumerate(offs):
            buf[off_pos + off_size * i : off_pos + off_size * (i + 1)] = o.to_bytes(
                off_size, 'big'
            )
        return start
    if isinstance(value, list):
        cw = _width(len(value))
        if cw > 1 and not allow_wide:
            raise OsonError('array too long for the native OSON encoder')
        buf += bytes([0xC0 | _count_bits(cw) | 0x04]) + len(value).to_bytes(cw, 'big')
        off_pos = len(buf)
        buf += b'\x00' * (off_size * len(value))
        offs = [_oson_emit(v, buf, fid, off_size, nfw, allow_wide) for v in value]
        for i, o in enumerate(offs):
            buf[off_pos + off_size * i : off_pos + off_size * (i + 1)] = o.to_bytes(
                off_size, 'big'
            )
        return start
    buf += _oson_scalar_node(value, allow_wide)
    return start


def encode_oson(value, *, allow_wide: bool = False) -> bytes:
    """Encode a Python value to an OSON image (the inverse of decode_oson).

    The default (``allow_wide`` False) emits only the compact small-document
    shape — scalars, objects/arrays up to 255 entries, strings up to 255 bytes,
    segments up to 64 KiB — and raises OsonError for anything larger so a native
    JSON bind falls back to the text cast (#70), which the server parses just as
    well. This path is byte-for-byte the form the server sends: container
    value-offsets are ub2 (the compact 0x04 form) and field-name hashes are zero
    (the server accepts that — verified by round-trip on 21c).

    ``allow_wide`` unlocks the large-document forms the decoder already reads
    (#69/#88): ub2 / ub4 counts and field-ids, ub2 / ub4 string lengths, a ub4
    tree size and ub4 value-offsets. The Mirror serves a JSON column's value
    back to a thin client through it (that client decodes with decode_oson, the
    exact inverse), so a wide (> 255-key) or large (> 64 KiB) document reads
    back correctly. It is *not* used for a real bind: the server has never been
    asked to parse a wide image the compact path can't already carry."""
    if not isinstance(value, (dict, list)):
        node = _oson_scalar_node(value, allow_wide)
        # The bare-scalar image carries the node length in a single value_size
        # byte (decode_oson reads reserved(ub1) + value_size(ub1)), so a scalar
        # over 255 bytes -- a long RAW / string -- cannot ride at top level; it
        # has to sit inside a container, whose value-offsets are ub2 / ub4.
        if len(node) > 0xFF:
            raise OsonError('scalar too large for the bare-scalar OSON image')
        return OSON_MAGIC + b'\x01' + b'\x00\x16\x00' + bytes([len(node)]) + node
    fnames: list[str] = []
    ids = {}

    def fid(name):
        if not isinstance(name, str):
            raise OsonError('JSON object keys must be strings')
        if name not in ids:
            ids[name] = len(fnames) + 1
            fnames.append(name)
            if len(fnames) > 0xFF and not allow_wide:
                raise OsonError('too many distinct keys for the native encoder')
        return ids[name]

    def walk(v):
        if isinstance(v, dict):
            for k, sub in v.items():
                fid(k)
                walk(sub)
        elif isinstance(v, list):
            for sub in v:
                walk(sub)

    walk(value)
    nfw = _width(len(fnames))

    # A field name over 255 bytes does not fit the ub1 length prefix of the
    # ordinary names segment, and goes in version 3's second segment instead.
    # The two share one field-id space with every short name first, so the ids
    # walk() handed out have to be renumbered into that order (#826).
    encoded = [n.encode('utf-8') for n in fnames]

    def _sort_key(index: int) -> tuple:
        # The server keeps its field names ORDERED BY HASH and looks them up that
        # way, so an image whose names are in any other order matches no path.
        # Ties break on the name's length, then its bytes. The key uses the low
        # byte of the hash even for a long name, whose stored hash is 16 bits.
        name = encoded[index]
        return (_fname_hash(name) & 0xFF, len(name), name)

    short_at = sorted(
        (i for i, b in enumerate(encoded) if len(b) <= 0xFF), key=_sort_key
    )
    long_at = sorted((i for i, b in enumerate(encoded) if len(b) > 0xFF), key=_sort_key)
    # Renumber unconditionally: sorting reorders the ids walk() handed out even
    # when there is no long name to move to the back.
    renumber = {old + 1: new + 1 for new, old in enumerate(short_at + long_at)}
    remapped = {name: renumber[old] for name, old in ids.items()}
    ids.clear()
    ids.update(remapped)
    fnames_b = [encoded[i] for i in short_at]
    fnames_seg = b''.join(bytes([len(b)]) + b for b in fnames_b)
    off_arr = b''.join(off.to_bytes(2, 'big') for off in _fname_offsets(fnames_b))
    hash_arr = bytes(_fname_hash(b) & 0xFF for b in fnames_b)
    long_b = [encoded[i] for i in long_at]
    long_seg = b''.join(len(b).to_bytes(2, 'big') + b for b in long_b)
    if len(long_seg) > 0xFFFF:
        long_off_size, secondary = 4, 0
    else:
        long_off_size, secondary = 2, _SEC_FLAG_UB2_FNAME_OFFSETS
    long_offsets = []
    _pos = 0
    for b in long_b:
        long_offsets.append(_pos)
        _pos += 2 + len(b)
    long_off_arr = b''.join(o.to_bytes(long_off_size, 'big') for o in long_offsets)
    # A long name's hash is the same FNV-1a, low SIXTEEN bits, little-endian --
    # so its first byte is the short form's byte and the second extends it.
    # Captured from 23ai as c5 62 / c5 dd for 'A'*256 / 'B'*256.
    long_hash_arr = b''.join(
        (_fname_hash(b) & 0xFFFF).to_bytes(2, 'little') for b in long_b
    )
    # Build the tree with ub2 value-offsets; if the tree exceeds what a ub2
    # offset can address (either it grows past 0xFFFF or an individual offset
    # would overflow), rebuild with ub4 offsets (#88). Two passes at most.
    tree = bytearray()
    off_size = 2
    for off_size in (2, 4):
        tree = bytearray()
        try:
            _oson_emit(value, tree, fid, off_size, nfw, allow_wide)
        except OverflowError:
            continue
        if off_size == 4 or len(tree) <= 0xFFFF:
            break
    if not allow_wide and (len(fnames_seg) > 0xFFFF or len(tree) > 0xFFFF):
        raise OsonError('document too large for the native OSON encoder')
    if len(fnames_seg) > 0xFFFF:
        # The field-name offset array is always ub2, so the names segment must
        # stay under 64 KiB even in the wide form (300 short keys is ~2 KiB).
        raise OsonError('field-name segment too large for the OSON encoder')
    flags = 0x2106
    if off_size == 4:
        flags &= ~_FLAG_UB2_OFFSETS
    # The header's field-name count is the SHORT names', but the flag that sizes
    # it also sizes every field id in the tree, so it follows the TOTAL -- a
    # document of 676 long names and no short ones still needs ub2 ids (#826).
    if len(fnames) > 0xFF:
        flags |= _FLAG_UB2_FNAMES
    tree_ub4 = len(tree) > 0xFFFF
    if tree_ub4:
        flags |= _FLAG_UB4_TREE_SIZE
    num_fnames = len(fnames_b).to_bytes(2 if len(fnames) > 0xFF else 1, 'big')
    tree_size = len(tree).to_bytes(4 if tree_ub4 else 2, 'big')
    # Version 3 only when a long name is actually present: a live server emits the
    # compact version 1 whenever it can, and so do we.
    version = _VERSION_FNAME_65535 if long_b else _VERSION_FNAME_255
    long_header = (
        secondary.to_bytes(2, 'big')
        + len(long_b).to_bytes(4, 'big')
        + len(long_seg).to_bytes(4, 'big')
        if long_b
        else b''
    )
    header = (
        OSON_MAGIC
        + bytes([version])
        + flags.to_bytes(2, 'big')
        + num_fnames
        + len(fnames_seg).to_bytes(2, 'big')
        + long_header
        + tree_size
        + b'\x00\x00'
    )
    return (
        header
        + hash_arr
        + off_arr
        + fnames_seg
        + long_hash_arr
        + long_off_arr
        + long_seg
        + bytes(tree)
    )


def _count_width(tag: int) -> int:
    # The byte width of a container's entry count (and of its field ids), from the
    # two count bits in its tag: 00 ub1, 01 ub2, 10 ub4. The fourth combination
    # means "shared field ids" and carries no count of its own.
    if tag & _TAG_UB4_COUNT:
        return 4
    return 2 if (tag & _TAG_WIDE_COUNT) else 1


def _fname_hash(name: bytes) -> int:
    # FNV-1a, 32-bit. The server indexes field names by this, so an image whose
    # hash array is wrong stores and serialises fine but matches NO json path.
    h = 0x811C9DC5
    for byte in name:
        h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
    return h


def _fname_offsets(fnames_b: list[bytes]) -> list[int]:
    # The ub2 offset of each field name within the names segment (each name is a
    # ub1 length + its bytes).
    offsets = []
    pos = 0
    for b in fnames_b:
        offsets.append(pos)
        pos += 1 + len(b)
    return offsets


def _u16(buf: bytes, pos: int) -> int:
    return (buf[pos] << 8) | buf[pos + 1]


def _uint(buf: bytes, pos: int, size: int) -> int:
    return int.from_bytes(buf[pos : pos + size], 'big')


def decode_oson(data: bytes) -> object:
    """Decode an OSON image to the corresponding Python value.

    A structurally short image (a header, node, or segment that indexes past
    the end) raises OsonError, honouring the module's "malformed -> OsonError"
    contract instead of leaking the raw IndexError of an out-of-range read.
    """
    if data[:3] != OSON_MAGIC:
        raise OsonError(f'not an OSON image (magic {data[:3].hex()})')
    try:
        return _decode_image(data)
    except (IndexError, UnicodeDecodeError) as exc:
        raise OsonError('truncated or malformed OSON image') from exc


def _decode_image(data: bytes) -> object:
    version = data[3]
    if version not in (_VERSION_FNAME_255, _VERSION_FNAME_65535):
        raise OsonError(f'unsupported OSON image version {version}')
    flags = _u16(data, 4)
    pos = 6
    # Container value-offsets are ub2 when the compact flag is set, else ub4.
    off_size = 2 if (flags & _FLAG_UB2_OFFSETS) else 4
    relative = bool(flags & _FLAG_REL_OFFSETS)
    if not (flags & _FLAG_TREE):
        # Bare scalar image: reserved(ub1), value_size(ub1), scalar node.
        size = data[pos + 1]
        seg = data[pos + 2 : pos + 2 + size]
        value, _ = _decode_node(seg, 0, None, seg, off_size)
        return value
    if flags & _FLAG_UB2_FNAMES:  # > 255 field names (#69)
        num_fnames = _u16(data, pos)
        pos += 2
    else:
        num_fnames = data[pos]
        pos += 1
    # The short-name segment's size, and the width of its offset array, move
    # together.
    name_off_size = 4 if (flags & _FLAG_UB4_FNAMES_SIZE) else 2
    fnames_size = _uint(data, pos, name_off_size)
    pos += name_off_size
    # Version 3 describes a second segment here, for the field names too long for
    # the ub1 length prefix the first one uses (#826).
    num_long = 0
    long_size = 0
    long_off_size = 4
    if version == _VERSION_FNAME_65535:
        secondary = _u16(data, pos)
        pos += 2
        if secondary & _SEC_FLAG_UB2_FNAME_OFFSETS:
            long_off_size = 2
        num_long = _uint(data, pos, 4)
        long_size = _uint(data, pos + 4, 4)
        pos += 8
    if flags & _FLAG_UB4_TREE_SIZE:  # tree > 64 KiB (#88)
        tree_size = _uint(data, pos, 4)
        pos += 4
    else:
        tree_size = _u16(data, pos)
        pos += 2
    pos += 2  # number of "tiny" nodes (unused here)
    pos += num_fnames  # hash array (1 byte / field)
    offsets = [
        _uint(data, pos + name_off_size * i, name_off_size) for i in range(num_fnames)
    ]
    pos += name_off_size * num_fnames
    fnames_seg = data[pos : pos + fnames_size]
    pos += fnames_size
    # The long names follow the short ones and continue the same field-id space:
    # ids 1..num_fnames index the short segment, the rest index this one. Their
    # hash array is TWO bytes per name, not one.
    long_names: list[str] = []
    if num_long:
        pos += num_long * 2
        long_offsets = [
            _uint(data, pos + long_off_size * i, long_off_size) for i in range(num_long)
        ]
        pos += long_off_size * num_long
        long_seg = data[pos : pos + long_size]
        pos += long_size
        for off in long_offsets:
            length = _u16(long_seg, off)
            long_names.append(long_seg[off + 2 : off + 2 + length].decode('utf-8'))
    tree_seg = data[pos : pos + tree_size]

    def field_name(field_id: int) -> str:
        index = field_id - 1
        if index < num_fnames:
            off = offsets[index]
            length = fnames_seg[off]
            return fnames_seg[off + 1 : off + 1 + length].decode('utf-8')
        return long_names[index - num_fnames]

    value, _ = _decode_node(
        tree_seg, 0, field_name, tree_seg, off_size, relative=relative
    )
    return value


def _decode_child(
    tree: bytes,
    off: int,
    field_name,
    off_size: int,
    memo: dict,
    stack: set,
    relative: bool = False,
):
    # Bounded recursion into a container value-offset. Memoise the decoded value
    # by offset so a shared child (a "diamond" of offsets) is decoded once rather
    # than exponentially, and reject an offset already on the current path (a
    # cycle) as OsonError rather than recursing forever. Together these cap the
    # total work at O(number of nodes) even for a crafted image: a malformed or
    # hostile OSON image must never hang the client (#165). Valid server images
    # are acyclic, so this only changes behaviour on bad input; a genuinely
    # shared subtree decodes to an equal (possibly aliased) value, which is fine
    # for JSON value semantics.
    if off in memo:
        return memo[off]
    if off in stack:
        raise OsonError(f'cyclic OSON node offset {off}')
    stack.add(off)
    value = _decode_node(tree, off, field_name, tree, off_size, memo, stack, relative)[
        0
    ]
    stack.discard(off)
    memo[off] = value
    return value


def _decode_node(
    seg: bytes,
    off: int,
    field_name,
    tree: bytes,
    off_size: int = 2,
    memo: dict | None = None,
    stack: set | None = None,
    relative: bool = False,
):
    # Returns (python_value, next_offset). `tree` is the tree segment that
    # container value-offsets are relative to; `field_name` maps an object's
    # field id to its key (None for scalar-only images). `off_size` is the
    # width (2 or 4) of container value-offsets for this image (#69). `memo` /
    # `stack` bound the offset-following recursion (see _decode_child); they are
    # created on the top-level call and threaded through the descent.
    if memo is None:
        memo = {}
    if stack is None:
        stack = set()
    tag = seg[off]
    if tag <= 0x1F:  # inline short string
        return seg[off + 1 : off + 1 + tag].decode('utf-8'), off + 1 + tag
    if 0x20 <= tag <= 0x2F:  # number, length packed in tag
        length = tag - 0x1F
        return decode_number(seg[off + 1 : off + 1 + length]), off + 1 + length
    if tag == 0x30:
        return None, off + 1
    if tag == 0x31:
        return True, off + 1
    if tag == 0x32:
        return False, off + 1
    if tag == 0x33:  # string, ub1 length prefix
        length = seg[off + 1]
        return seg[off + 2 : off + 2 + length].decode('utf-8'), off + 2 + length
    if tag == 0x34:  # number, ub1 length prefix
        length = seg[off + 1]
        return decode_number(seg[off + 2 : off + 2 + length]), off + 2 + length
    if tag == 0x37:  # string, ub2 length prefix (>255 B, #88)
        length = _u16(seg, off + 1)
        return seg[off + 3 : off + 3 + length].decode('utf-8'), off + 3 + length
    if tag == 0x38:  # string, ub4 length prefix (>64 KiB, #88)
        length = _uint(seg, off + 1, 4)
        return seg[off + 5 : off + 5 + length].decode('utf-8'), off + 5 + length
    if tag == 0x3A:  # RAW, ub2 length prefix -> bytes (#826)
        length = _u16(seg, off + 1)
        return seg[off + 3 : off + 3 + length], off + 3 + length
    if tag == 0x3B:  # RAW, ub4 length prefix -> bytes (#826)
        length = _uint(seg, off + 1, 4)
        return seg[off + 5 : off + 5 + length], off + 5 + length
    if 0x40 <= tag <= 0x5F:  # integer, byte length in the low nibble (#826)
        length = tag & 0x0F
        return decode_number(seg[off + 1 : off + 1 + length]), off + 1 + length
    if 0x60 <= tag <= 0x6F:  # number, (low nibble + 1) byte length (#826)
        length = (tag & 0x0F) + 1
        return decode_number(seg[off + 1 : off + 1 + length]), off + 1 + length
    if tag == 0x7B:  # extended-type wrapper: a ub1 sub-tag then the payload (#826)
        sub = seg[off + 1]
        if sub == 0x01:  # VECTOR: ub4 length, then a bare vector image
            from seerdb.common.vector import decode_vector

            length = _uint(seg, off + 2, 4)
            payload = seg[off + 6 : off + 6 + length]
            return decode_vector(payload), off + 6 + length
        raise NotSupportedError(
            f'unsupported OSON extended sub-tag 0x{sub:02x} at offset {off}'
        )
    # A container with > 255 entries / field-ids uses ub2 count + ub2 field-ids
    # (tag 0x08 bit); otherwise ub1. The value-offset width is per-image
    # (off_size), but a large container overrides it to ub4 via the tag 0x20 bit
    # (#88) — its values span a >64 KiB tree so ub2 offsets can't address them.
    osz = 4 if (tag & _TAG_UB4_OFFSETS) else off_size
    # A value-offset is relative to the CONTAINER'S OWN offset when the image is
    # in relative-offset mode, absolute within the tree segment otherwise (#826).
    # Only a container that does not start the tree is affected, which is why an
    # image whose root is its only container decodes either way.
    base = off if relative else 0
    # Containers only: the count bits are only count bits in a container tag, and
    # plenty of extended scalars (DATE 0x3C, TIMESTAMP 0x39, the intervals) happen
    # to carry both of them.
    if (tag & 0x80) and (tag & _TAG_COUNT_BITS) == _TAG_SHARED_FIELDS:  # (#826)
        # This object stores only its own value offsets; its keys come from an
        # earlier object, whose offset it carries in their place. A compressed
        # JSON column uses this for every repeat of a shape it has already
        # written. Read as an ordinary container the shared offset is taken for a
        # count, which is how it surfaced ("OSON object count exceeds image").
        if field_name is None:
            raise OsonError('object node in a scalar-only OSON image')
        # The pointer to the donor is ABSOLUTE within the tree segment even in
        # relative-offset mode -- only the value offsets below are rebased.
        donor = _uint(seg, off + 1, osz)
        val_pos = off + 1 + osz
        if not 0 <= donor < len(seg):
            raise OsonError('OSON shared-field offset outside image')
        donor_tag = seg[donor]
        donor_csz = _count_width(donor_tag)
        count = _uint(seg, donor + 1, donor_csz)
        ids_pos = donor + 1 + donor_csz
        if ids_pos + donor_csz * count > len(seg) or val_pos + osz * count > len(seg):
            raise OsonError('OSON object count exceeds image')
        ids = [_uint(seg, ids_pos + donor_csz * i, donor_csz) for i in range(count)]
        val_offsets = [_uint(seg, val_pos + osz * i, osz) for i in range(count)]
        return (
            {
                field_name(i): _decode_child(
                    tree, o + base, field_name, off_size, memo, stack, relative
                )
                for i, o in zip(ids, val_offsets)
            },
            val_pos + osz * count,
        )
    csz = _count_width(tag)
    if (tag & 0xC0) == 0xC0:  # array container
        count = _uint(seg, off + 1, csz)
        p = off + 1 + csz
        # The offset array (count * osz bytes) must fit in the image; a crafted
        # count (e.g. ub4 0xffffffff) would otherwise spin building a
        # multi-billion-entry list before any offset is followed (#165).
        if p + osz * count > len(seg):
            raise OsonError('OSON array count exceeds image')
        elem_offsets = [_uint(seg, p + osz * i, osz) for i in range(count)]
        return (
            [
                _decode_child(
                    tree, o + base, field_name, off_size, memo, stack, relative
                )
                for o in elem_offsets
            ],
            p + osz * count,
        )
    if (tag & 0xC0) == 0x80:  # object container
        count = _uint(seg, off + 1, csz)
        p = off + 1 + csz
        # Field-id array (count * csz) + value-offset array (count * osz) must
        # fit in the image, else a crafted count spins as above (#165).
        if p + (csz + osz) * count > len(seg):
            raise OsonError('OSON object count exceeds image')
        if field_name is None:
            # An object node reached in a scalar-only image (no field-name
            # table); resolving its keys would call None(...) -> TypeError.
            # Reject cleanly instead (#230).
            raise OsonError('object node in a scalar-only OSON image')
        ids = [_uint(seg, p + csz * i, csz) for i in range(count)]
        p += csz * count
        val_offsets = [_uint(seg, p + osz * i, osz) for i in range(count)]
        return (
            {
                field_name(i): _decode_child(
                    tree, o + base, field_name, off_size, memo, stack, relative
                )
                for i, o in zip(ids, val_offsets)
            },
            p + osz * count,
        )
    if tag in _EXT_SCALAR:  # extended scalar (#69)
        length, dec = _EXT_SCALAR[tag]
        return dec(seg[off + 1 : off + 1 + length]), off + 1 + length
    # A tag we do not implement is a FEATURE GAP: raise the DB-API's
    # NotSupportedError so the Mirror answers ORA-03115 and the session lives,
    # rather than ORA-00600, which clients treat as fatal (#875). A malformed or
    # truncated image keeps OsonError -- bad data is a different thing.
    raise NotSupportedError(f'unsupported OSON node tag 0x{tag:02x} at offset {off}')
