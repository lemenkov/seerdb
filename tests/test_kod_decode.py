# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The OCI object-type describe, TTI_KOD, read off a live 11g (#1411).

sqlplus with ``set serveroutput on`` describes DBMSOUTPUT_LINESARRAY by name,
then SYS.KOTTD by REF; docs/PROTOCOL.md §40 has the layout. These pin the decoder
to the captured bytes, which the Mirror's encoder will be checked against too.
"""

import kod_11g as fx

from seerdb.common.tns import (
    KOD_BY_NAME,
    KOD_BY_REF,
    decode_kod_image,
    decode_kod_reply,
    parse_kod_request,
)

_KOTTD_ID = bytes(15) + b'\x01'
_LINESARRAY_ID = bytes.fromhex('ab9eebb9d6af92fce040e50a194e4680')


def test_sqlplus_asks_for_the_type_by_name() -> None:
    request = parse_kod_request(fx.BY_NAME_REQUEST)
    assert request.opcode == KOD_BY_NAME
    assert request.schema is None
    assert request.name == 'DBMSOUTPUT_LINESARRAY'


def test_then_for_kottd_by_ref() -> None:
    request = parse_kod_request(fx.KOTTD_REQUEST)
    assert request.opcode == KOD_BY_REF
    assert request.ref is not None and request.ref[5:21] == _KOTTD_ID


def test_the_by_name_reply_is_the_type_as_one_kottd_record() -> None:
    reply = decode_kod_reply(fx.BY_NAME_REPLY)
    # The header names the CONNECTED user's schema, not the type's owner.
    assert reply.named is not None
    (schema, name, ref) = reply.named
    assert (schema, name) == ('PYO', 'DBMSOUTPUT_LINESARRAY')
    assert ref[5:21] == _LINESARRAY_ID
    assert len(reply.records) == 1
    (record,) = reply.records
    assert record.type_id == _KOTTD_ID
    assert record.oid == _LINESARRAY_ID
    values = decode_kod_image(record.image)
    assert len(values) == 10
    assert values[1:4] == [b'SYS', b'DBMSOUTPUT_LINESARRAY', b'$8.0']
    assert values[4] == (122).to_bytes(2, 'big')  # a named collection


def test_kottd_describes_itself() -> None:
    reply = decode_kod_reply(fx.KOTTD_REPLY)
    assert reply.named is None
    (record,) = reply.records
    assert record.type_id == record.oid == _KOTTD_ID
    values = decode_kod_image(record.image)
    assert values[1:5] == [b'SYS', b'KOTTD', b'$8.0', (108).to_bytes(2, 'big')]
    # Its own TDS lists exactly the ten attributes its image carries.
    tds = values[5]
    assert tds is not None and int.from_bytes(tds[8:10], 'big') == 10


def test_the_linesarray_tds_is_what_the_pg_backend_builds() -> None:
    # The type's TDS is byte for byte the GET_TYPE_SHAPE TDS the PG backend
    # already builds, so the Mirror can answer KOD from it (§40.3).
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'examples'))
    import postgres_backend as pb

    (record,) = decode_kod_reply(fx.BY_NAME_REPLY).records
    want = pb._tds(
        pb._TdsCollection(
            varray=True, bound=0x7FFFFFFF, element=pb._tds_chars(7, 32767, False)
        )
    )
    assert decode_kod_image(record.image)[5] == want


def test_the_mirror_answers_kottd_as_11g_does() -> None:
    # A client handed a type descriptor asks SYS.KOTTD by REF how to read one.
    # The Mirror's answer is the live 11g reply byte for byte, given the same
    # reply and call sequences (#1411).
    from seerdb.common.tns import (
        _ENCODE_OCI_CALL_SEQ,
        KOD_KOTTD,
        encode_kod_reply,
        kod_system_record,
    )

    record = kod_system_record(KOD_KOTTD)
    assert record is not None
    token = _ENCODE_OCI_CALL_SEQ.set(0x0E)
    try:
        assert encode_kod_reply([record], sequence=0x0B) == fx.KOTTD_REPLY
    finally:
        _ENCODE_OCI_CALL_SEQ.reset(token)
    assert kod_system_record(_LINESARRAY_ID) is None


def test_an_image_encodes_back_to_its_bytes() -> None:
    from seerdb.common.tns import encode_kod_image

    for reply in (fx.BY_NAME_REPLY, fx.KOTTD_REPLY):
        for record in decode_kod_reply(reply).records:
            assert encode_kod_image(decode_kod_image(record.image)) == record.image


def test_the_oci_loop_serves_kottd_and_still_refuses_the_rest() -> None:
    # The by-name describe is not served yet (it needs the backend's type), so
    # it is refused as before and the session carries on; the KOTTD one is
    # answered with its record.
    from typing import Any

    from seerdb.common.tns_consts import TNS_DATA
    from seerdb.server.session import _serve_oci_session

    class _Stream:
        def __init__(self) -> None:
            self.inbox = [
                (TNS_DATA, fx.BY_NAME_REQUEST),
                (TNS_DATA, fx.KOTTD_REQUEST),
                None,
            ]
            self.sent: list[bytes] = []

        def read_packet(self, **_kw):
            return self.inbox.pop(0)

        def write_packet(self, ptype: int, body: bytes, **_kw) -> None:
            self.sent.append(body)

    stream: Any = _Stream()
    backend: Any = object()
    assert _serve_oci_session(stream, backend, 'PYO') == 'PYO'
    (refused, kottd) = stream.sent
    assert b'ORA-03115' in refused
    (record,) = decode_kod_reply(kottd).records
    assert decode_kod_image(record.image)[2] == b'KOTTD'
