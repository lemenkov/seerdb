# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Server-side framing: read_packet is a true inverse of the client's send()."""

from __future__ import annotations

import socket

from seerdb.common.tns import encode_packet
from seerdb.common.tns_consts import TNS_CONNECT, TNS_DATA
from seerdb.server.framing import PacketStream


def _pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair()


def test_reads_a_single_connect_packet() -> None:
    left, right = _pair()
    try:
        body = b'(CONNECT_DATA=(SERVICE_NAME=orcl))'
        packet, rest = encode_packet(TNS_CONNECT, body, 8192)
        assert rest is None
        left.sendall(packet)
        stream = PacketStream(right, sdu=8192)
        result = stream.read_packet()
        assert result == (TNS_CONNECT, body)
    finally:
        left.close()
        right.close()


def test_eof_returns_none() -> None:
    left, right = _pair()
    left.close()
    try:
        stream = PacketStream(right, sdu=8192)
        assert stream.read_packet() is None
    finally:
        right.close()


def test_data_roundtrips_through_write_then_read() -> None:
    left, right = _pair()
    try:
        writer = PacketStream(left, sdu=8192)
        reader = PacketStream(right, sdu=8192)
        payload = b'\x03\x05exec-body-goes-here'
        writer.write_packet(TNS_DATA, payload)
        assert reader.read_packet() == (TNS_DATA, payload)
    finally:
        left.close()
        right.close()


def test_large_data_fragments_reassemble() -> None:
    # A payload several times the SDU must split on write and reassemble on
    # read — the case assemble_packet's server-side heuristic would mishandle.
    left, right = _pair()
    try:
        sdu = 64
        writer = PacketStream(left, sdu=sdu)
        reader = PacketStream(right, sdu=sdu)
        payload = bytes(range(256)) * 3  # 768 bytes >> sdu, forces many splits
        writer.write_packet(TNS_DATA, payload)
        assert reader.read_packet() == (TNS_DATA, payload)
    finally:
        left.close()
        right.close()


def _client_reassemble(raw: bytes, sdu: int) -> bytes:
    # Mimic the real client's response reassembly (Connection.recv): assemble_packet
    # keys on the SDU-37 / SDU-81 continuation sizes and ignores the 0x0020 flag.
    from seerdb.common.tns import assemble_packet

    acc, out = raw, b''
    while len(acc) >= 8:
        flag, _type, body, rest = assemble_packet(acc, sdu, False)
        if body is None:
            break
        out += body
        acc = rest or b''
        if flag and not acc:
            break
    return out


def test_large_response_reassembles_via_client_path() -> None:
    # A DATA response bigger than the SDU must fragment so the *client's*
    # assemble_packet reassembles it whole — the full-SDU form encode_packet
    # emits is misread as complete after the first fragment. Covers sizes around
    # the fragment boundary, including the magic continuation sizes themselves.
    # The write runs on a thread so a big payload can't deadlock the socketpair
    # buffer against a same-thread read.
    import threading

    sdu = 8192
    for n in (sdu - 10, sdu - 37, sdu - 81, sdu * 3, sdu * 2 - 47, 200_000):
        payload = bytes((i * 7) % 256 for i in range(n))
        left, right = _pair()
        try:

            def _send(sock=left, data=payload) -> None:
                PacketStream(sock, sdu=sdu).write_packet(TNS_DATA, data)
                sock.shutdown(socket.SHUT_WR)

            writer = threading.Thread(target=_send)
            writer.start()
            raw = b''
            while True:
                chunk = right.recv(65536)
                if not chunk:
                    break
                raw += chunk
            writer.join(timeout=5)
            assert _client_reassemble(raw, sdu) == payload, (
                f'size {n} did not reassemble'
            )
        finally:
            left.close()
            right.close()


def test_two_back_to_back_packets() -> None:
    # Consecutive packets in one buffer are framed independently.
    left, right = _pair()
    try:
        writer = PacketStream(left, sdu=8192)
        reader = PacketStream(right, sdu=8192)
        writer.write_packet(TNS_DATA, b'first')
        writer.write_packet(TNS_DATA, b'second')
        assert reader.read_packet() == (TNS_DATA, b'first')
        assert reader.read_packet() == (TNS_DATA, b'second')
    finally:
        left.close()
        right.close()


# --- end-of-response framing and pipelining (#1059) --------------------------


def _write_and_capture(stream_setup, body: bytes) -> bytes:
    # One DATA write through a configured stream, returned as the raw packet so
    # the flags and the trailing bytes can be read directly.
    left, right = _pair()
    try:
        writer = PacketStream(left, sdu=8192)
        stream_setup(writer)
        writer.write_packet(TNS_DATA, body)
        left.shutdown(socket.SHUT_WR)
        raw = b''
        while chunk := right.recv(65536):
            raw += chunk
        return raw
    finally:
        left.close()
        right.close()


def test_a_reply_is_unchanged_until_end_of_response_is_on() -> None:
    raw = _write_and_capture(lambda s: None, b'\x09\x01\x01\x00')
    assert raw[8:10] == b'\x00\x00'  # data flags
    assert raw[10:] == b'\x09\x01\x01\x00'


def test_end_of_response_marks_and_terminates_every_reply() -> None:
    # Flags 0x2000 and a trailing 0x1d -- on EVERY reply once negotiated, not
    # only a pipelined one: the client reads until the marker.
    def on(s: PacketStream) -> None:
        s.end_of_response = True

    raw = _write_and_capture(on, b'\x09\x01\x01\x00')
    assert raw[8:10] == b'\x20\x00'
    assert raw[10:] == b'\x09\x01\x01\x00\x1d'


def test_a_pipelined_reply_opens_with_its_token_once() -> None:
    # Captured from 23ai: `21 01 02 <reply> 1d` for the second call of a burst.
    # The token is spent on the write that uses it.
    left, right = _pair()
    try:
        writer = PacketStream(left, sdu=8192)
        writer.end_of_response = True
        writer.response_token = 2
        writer.write_packet(TNS_DATA, b'\x09\x01\x01\x00')
        assert writer.response_token is None
        writer.write_packet(TNS_DATA, b'\x09\x01\x01\x00')
        left.shutdown(socket.SHUT_WR)
        raw = b''
        while chunk := right.recv(65536):
            raw += chunk
    finally:
        left.close()
        right.close()
    first = raw[: int.from_bytes(raw[0:2], 'big')]
    second = raw[len(first) :]
    assert first[10:] == b'\x21\x01\x02\x09\x01\x01\x00\x1d'
    assert second[10:] == b'\x09\x01\x01\x00\x1d'  # no token: not pipelined


def test_a_pre_framed_handshake_reply_carries_the_marker_too() -> None:
    # A live 23ai puts the marker on its PRO answer, the first DATA reply after
    # the ACCEPT -- so send_raw's DATA path has to add it as well, or the client
    # waits for a marker the handshake never sends and hangs before login.
    left, right = _pair()
    try:
        writer = PacketStream(left, sdu=8192)
        writer.end_of_response = True
        pre_framed, _ = encode_packet(TNS_DATA, b'\x00\x00\x01\x06\x00', 8192)
        writer.send_raw(pre_framed)
        left.shutdown(socket.SHUT_WR)
        raw = b''
        while chunk := right.recv(65536):
            raw += chunk
    finally:
        left.close()
        right.close()
    assert raw[8:10] == b'\x20\x00'
    assert raw.endswith(b'\x1d')


def test_read_packet_keeps_the_data_flags() -> None:
    # A pipelined call is marked only in its data flags.
    from seerdb.common.tns import encode_data_packet

    left, right = _pair()
    try:
        left.sendall(encode_data_packet(b'\x03\x5e\x05', 0x1800, False))
        reader = PacketStream(right, sdu=8192)
        assert reader.read_packet() == (TNS_DATA, b'\x03\x5e\x05')
        assert reader.last_data_flags == 0x1800
    finally:
        left.close()
        right.close()
