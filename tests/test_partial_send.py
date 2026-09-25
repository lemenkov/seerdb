# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""A packet the socket only partly takes is still sent whole (#861).

socket.send() may write only part of what it is given: when the server stops
reading, the TCP window fills, and a socket with a timeout sends what fits and
returns the count. The client ignored the count, so the rest of that TNS packet
was dropped. The server then read the next packet's bytes as its tail and
refused the call with ORA-03146, or waited for bytes that never came. Seen as
the intermittent 500 KiB LOB bind failure, whenever a server paused mid-request.

These drive the real send paths against a socket that takes at most a few
bytes per call, and compare what reached it with what a socket taking
everything receives.
"""

import unittest

from seerdb.client.connection import OracleConnect
from seerdb.common.exceptions import OperationalError
from seerdb.common.tns_consts import TNS_DATA, TNS_DATA_FLAGS_END_OF_REQUEST


class _Sock:
    """Takes at most `cap` bytes per send(), like a socket whose window is full;
    after `fail_after` calls, times out instead."""

    def __init__(self, cap: int | None = None, fail_after: int | None = None):
        self.cap, self.fail_after = cap, fail_after
        self.received = bytearray()
        self.calls = 0

    def send(self, data, *flags):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise TimeoutError('timed out')
        taken = bytes(data)[: self.cap] if self.cap else bytes(data)
        self.received += taken
        return len(taken)


def _conn(sock: _Sock) -> OracleConnect:
    c = OracleConnect(host='x', port=1, user='pyo', password='p')
    c.sock = sock  # type: ignore[assignment]
    return c


_PAYLOAD = bytes(range(256)) * 2000  # 512000 bytes, spanning many packets


class TestAPartlyTakenPacketIsSentWhole(unittest.TestCase):
    def test_send_writes_every_byte_in_order(self):
        whole, trickle = _Sock(), _Sock(cap=1000)
        _conn(whole).send(TNS_DATA, _PAYLOAD)
        _conn(trickle).send(TNS_DATA, _PAYLOAD)
        self.assertGreater(trickle.calls, whole.calls)  # it really was cut up
        self.assertEqual(bytes(trickle.received), bytes(whole.received))

    def test_a_pipelined_op_is_written_whole_too(self):
        whole, trickle = _Sock(), _Sock(cap=1000)
        _conn(whole)._pipeline_send_op(_PAYLOAD, TNS_DATA_FLAGS_END_OF_REQUEST)
        _conn(trickle)._pipeline_send_op(_PAYLOAD, TNS_DATA_FLAGS_END_OF_REQUEST)
        self.assertEqual(bytes(trickle.received), bytes(whole.received))

    def test_a_socket_that_stops_taking_bytes_times_out(self):
        # No progress within the timeout is a write timeout, as before.
        with self.assertRaisesRegex(OperationalError, 'network write timed out'):
            _conn(_Sock(cap=1000, fail_after=3)).send(TNS_DATA, _PAYLOAD)


if __name__ == '__main__':
    unittest.main()
