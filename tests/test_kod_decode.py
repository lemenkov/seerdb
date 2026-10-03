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


def _linesarray_tds_pair() -> tuple[bytes, bytes]:
    (record,) = decode_kod_reply(fx.BY_NAME_REPLY).records
    values = decode_kod_image(record.image)
    assert values[5] is not None and values[6] is not None
    return (values[5], values[6])


def test_the_mirror_answers_the_by_name_describe_as_11g_does() -> None:
    # Given the type's identity and TDS pair -- what a backend's describe_type
    # returns -- the reply is the live 11g one byte for byte, header naming the
    # connected user's schema and all (#1411).
    from seerdb.common.tns import (
        _ENCODE_OCI_CALL_SEQ,
        encode_kod_named_reply,
        kod_type_record,
    )

    (tds, null_tds) = _linesarray_tds_pair()
    record = kod_type_record(
        oid=_LINESARRAY_ID,
        schema='SYS',
        name='DBMSOUTPUT_LINESARRAY',
        kind='varray',
        tds=tds,
        null_tds=null_tds,
    )
    token = _ENCODE_OCI_CALL_SEQ.set(0x0D)
    try:
        got = encode_kod_named_reply(
            schema='PYO', name='DBMSOUTPUT_LINESARRAY', record=record, sequence=0x0A
        )
    finally:
        _ENCODE_OCI_CALL_SEQ.reset(token)
    assert got == fx.BY_NAME_REPLY


def _without_instance_marker(reply: bytes) -> bytes:
    # Every Mirror OER carries 11g's instance marker at OER offset 72; a live
    # 18c sends its own. Blank it so the comparison is about the rest.
    oer = len(reply) - 144
    return reply[: oer + 72] + bytes(6) + reply[oer + 78 :]


def test_the_12c_band_answers_as_18c_does() -> None:
    # The Mirror speaks 18c's layout to a 12c+ sqlplus, and there KOD differs:
    # the record descriptor is another 35 bytes, the by-name header ends with
    # four more, KOTTD's names are VARCHAR(128) and the OER's row kind and
    # offset 52 are 0. With those, both replies are 18c's (#1411).
    import kod_18c as fx18

    from seerdb.common.tns import (
        _ENCODE_FIELD_VERSION,
        _ENCODE_OCI_CALL_SEQ,
        KOD_KOTTD,
        encode_kod_named_reply,
        encode_kod_reply,
        kod_system_record,
        kod_type_record,
    )
    from seerdb.common.tns_consts import FIELD_VERSION_18_1_EXT_1

    request = parse_kod_request(fx18.BY_NAME_REQUEST)
    assert request.name == 'DBMSOUTPUT_LINESARRAY'
    named = decode_kod_reply(fx18.BY_NAME_REPLY)
    assert named.named is not None
    (record,) = named.records
    values = decode_kod_image(record.image)
    assert values[5] is not None and values[6] is not None
    version = _ENCODE_FIELD_VERSION.set(FIELD_VERSION_18_1_EXT_1)
    try:
        call = _ENCODE_OCI_CALL_SEQ.set(0x0D)
        got = encode_kod_named_reply(
            schema='PYO',
            name='DBMSOUTPUT_LINESARRAY',
            record=kod_type_record(
                oid=record.oid,
                schema='SYS',
                name='DBMSOUTPUT_LINESARRAY',
                kind='varray',
                tds=values[5],
                null_tds=values[6],
            ),
            sequence=0x1E3,
        )
        _ENCODE_OCI_CALL_SEQ.reset(call)
        assert _without_instance_marker(got) == _without_instance_marker(
            fx18.BY_NAME_REPLY
        )
        call = _ENCODE_OCI_CALL_SEQ.set(0x0E)
        kottd = kod_system_record(KOD_KOTTD)
        assert kottd is not None
        got = encode_kod_reply([kottd], sequence=0x1E4)
        _ENCODE_OCI_CALL_SEQ.reset(call)
        assert _without_instance_marker(got) == _without_instance_marker(
            fx18.KOTTD_REPLY
        )
    finally:
        _ENCODE_FIELD_VERSION.reset(version)


def test_a_collection_out_value_goes_back_as_live_servers_send_it() -> None:
    # DBMS_OUTPUT.GET_LINES's lines come back as the collection's image framed
    # for OCI, and the count as an ordinary NUMBER; each bind's marker is its
    # direction, 0x10 OUT and 0x30 IN OUT (#1411). Compared from the markers to
    # the status tail, which carries live-instance fields of its own.
    import kod_18c as fx18

    from seerdb.common.dbobject import ObjectImage
    from seerdb.common.tns import _ENCODE_FIELD_VERSION, encode_out_bind_response_oci
    from seerdb.common.tns_consts import FIELD_VERSION_11_2, FIELD_VERSION_18_1_EXT_1

    def values_part(reply: bytes) -> bytes:
        return reply[50 : reply.index(b'\x08\x06', 50)]

    cases = (
        (FIELD_VERSION_11_2, _LINESARRAY_ID, fx.GET_LINES_REPLY, 1),
        (FIELD_VERSION_11_2, _LINESARRAY_ID, fx.GET_LINES_EMPTY_REPLY, 0),
        (
            FIELD_VERSION_18_1_EXT_1,
            bytes.fromhex('787d0d2b19c46933e0530caae80a12fb'),
            fx18.GET_LINES_REPLY,
            1,
        ),
    )
    for version, toid, want, count in cases:
        at = want.index(b'\x88\x01')
        image = want[at : at + want[at + 2]]
        token = _ENCODE_FIELD_VERSION.set(version)
        try:
            got = encode_out_bind_response_oci(
                [ObjectImage(toid, 'SYS', 'DBMSOUTPUT_LINESARRAY', 0, image), count],
                sequence=1,
                types=[(109, toid), (2, b'')],
                inputs=[False, True],
            )
        finally:
            _ENCODE_FIELD_VERSION.reset(token)
        assert values_part(got) == values_part(want), hex(version)


def test_a_collection_object_goes_back_as_oracle_pickles_it() -> None:
    # A backend hands a collection OUT value back as an object of its type
    # (DBMS_OUTPUT.GET_LINES's lines, #1411). Its image goes to an OCI client as
    # a live server's does: a one-byte length when it fits and the VARRAY kind in
    # the prefix -- `88 01 13 01 03 ...` -- byte for byte the 11g capture.
    from seerdb.common.dbobject import COLLECTION_VARRAY, DbObject, DbObjectType
    from seerdb.common.tns import _ENCODE_FIELD_VERSION, encode_out_bind_response_oci
    from seerdb.common.tns_consts import FIELD_VERSION_11_2, TNS_TYPE_VARCHAR

    typ = DbObjectType(
        'SYS',
        'DBMSOUTPUT_LINESARRAY',
        _LINESARRAY_ID,
        1,
        [],
        is_collection=True,
        collection_type=COLLECTION_VARRAY,
        element={
            'name': 'element',
            'type_name': 'VARCHAR2',
            'data_type': TNS_TYPE_VARCHAR,
            'charset': None,
        },
        max_elements=0x7FFFFFFF,
    )

    def values_part(reply: bytes) -> bytes:
        return reply[50 : reply.index(b'\x08\x06', 50)]

    for want, lines, count in (
        (fx.GET_LINES_REPLY, ['from plsql', None], 1),
        (fx.GET_LINES_EMPTY_REPLY, [None, None], 0),
    ):
        token = _ENCODE_FIELD_VERSION.set(FIELD_VERSION_11_2)
        try:
            got = encode_out_bind_response_oci(
                [DbObject(typ.name, elements=lines, dbtype=typ), count],
                sequence=1,
                types=[(109, _LINESARRAY_ID), (2, b'')],
                inputs=[False, True],
            )
        finally:
            _ENCODE_FIELD_VERSION.reset(token)
        assert values_part(got) == values_part(want)
