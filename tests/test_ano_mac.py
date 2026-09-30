# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Tests for the ANO AES-keystream data-integrity MAC (#437, phase 4).

Offline: a client and a server instance (with swapped send/receive keystreams)
exercise the real MAC round-trip in both directions, plus tamper detection and
the stateful keystream, validated by self-consistent round-trip.
"""

import unittest

from seerdb.common.ano_mac import AnoMac, AnoMacError

KEY = bytes(range(32))  # a stand-in DH session key
IV = bytes(range(64, 80))  # 16-byte IV


class TestAnoMac(unittest.TestCase):
    def _pair(self, algo='SHA256'):
        Client = AnoMac(KEY, IV, algo, ClientSide=True)
        Server = AnoMac(KEY, IV, algo, ClientSide=False)
        return (Client, Server)

    def test_client_to_server_roundtrip(self):
        (Client, Server) = self._pair()
        for Msg in (b'x', b'select 1 from dual', bytes(range(200))):
            with self.subTest(msg=Msg[:8]):
                self.assertEqual(Server.validate(Client.sign(Msg)), Msg)

    def test_server_to_client_roundtrip(self):
        (Client, Server) = self._pair()
        # The other direction uses the mirror keystream.
        self.assertEqual(
            Client.validate(Server.sign(b'reply payload')), b'reply payload'
        )

    def test_all_sha2_sizes(self):
        for Algo in ('SHA256', 'SHA384', 'SHA512'):
            with self.subTest(algo=Algo):
                (Client, Server) = self._pair(Algo)
                self.assertEqual(Server.validate(Client.sign(b'payload')), b'payload')

    def test_keystream_is_stateful(self):
        # Identical payloads produce different MACs (the keystream advances),
        # and a matched receiver still validates both in order.
        (Client, Server) = self._pair()
        Mac1 = Client.compute(b'same')
        Mac2 = Client.compute(b'same')
        self.assertNotEqual(Mac1, Mac2)
        self.assertEqual(Server.validate(b'same' + Mac1), b'same')
        self.assertEqual(Server.validate(b'same' + Mac2), b'same')

    def test_tamper_is_rejected(self):
        (Client, Server) = self._pair()
        Tagged = bytearray(Client.sign(b'important'))
        Tagged[0] ^= 0x01  # flip a payload bit
        with self.assertRaises(AnoMacError):
            Server.validate(bytes(Tagged))

    def test_wrong_mac_rejected(self):
        (Client, Server) = self._pair()
        with self.assertRaises(AnoMacError):
            Server.validate(b'payload' + b'\x00' * 32)

    def test_short_input_rejected(self):
        (_Client, Server) = self._pair()
        with self.assertRaises(AnoMacError):
            Server.validate(b'short')

    def test_unsupported_algorithm(self):
        with self.assertRaises(AnoMacError):
            AnoMac(KEY, IV, 'MD5')  # RC4-keystream path is deferred


if __name__ == '__main__':
    unittest.main()


class TestRederiveAfterReset(unittest.TestCase):
    """A break/reset re-derives the integrity keystreams (#1345).

    Replays one real session of seerdb's client against a 26ai server requiring
    AES-256 + SHA-256: every server payload and MAC it received, with the first
    packet after each break/reset exchange marked. The server re-derives its
    keystreams at each reset, so every MAC verifies only if the client does too.
    """

    @staticmethod
    def _session():
        import pathlib

        fields: dict[str, str] = {}
        packets: list[tuple[bool, bytes]] = []
        path = pathlib.Path(__file__).parent / 'fixtures' / 'ano_reset_session.txt'
        for line in path.read_text().splitlines():
            if not line or line.startswith('#'):
                continue
            if line.startswith('packet '):
                reset, data = (part.split('=', 1)[1] for part in line.split()[1:])
                packets.append((reset == '1', bytes.fromhex(data)))
            else:
                key, value = line.split('=', 1)
                fields[key] = value
        return fields, packets

    def _mac(self, fields):
        from seerdb.common.ano_session import make_mac

        mac = make_mac(
            int(fields['integrity']),
            bytes.fromhex(fields['shared']),
            bytes.fromhex(fields['server_iv']),
        )
        assert mac is not None
        return mac

    def test_every_server_packet_verifies_when_the_client_rederives(self):
        fields, packets = self._session()
        self.assertGreaterEqual(sum(reset for reset, _ in packets), 2)
        mac = self._mac(fields)
        for reset, data in packets:
            if reset:
                mac.rederive()
            mac.validate(data)  # raises on a mismatch

    def test_without_rederiving_the_first_packet_after_a_reset_fails(self):
        fields, packets = self._session()
        mac = self._mac(fields)
        for reset, data in packets:
            if reset:
                with self.assertRaises(AnoMacError):
                    mac.validate(data)
                return
            mac.validate(data)
        self.fail('the recorded session has no reset')
