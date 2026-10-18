# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

# Offline tests for DRCP / implicit connection pooling (#130): the connect
# descriptor gains (SERVER=POOLED), the connection threads cclass/purity, and
# the response decoder skips the server-side piggyback DRCP sessions carry. The
# live cclass/purity auth round-trip is covered on 21c/23ai.

import unittest

from seerdb.client.connection import OracleConnect
from seerdb.common.tns import (
    decode_packet,
    encode_dictionary_auth,
    encode_dictionary_description,
)
from seerdb.common.tns_consts import (
    FIELD_VERSION_12_1,
    PURITY_SELF,
    TTI_STA,
    TTI_SVR_PIGGYBACK,
)
from seerdb.server.auth import make_challenge


def _env(**extra):
    base = {
        'host': 'h',
        'port': 1521,
        'user': 'u',
        'service_name': 'svc',
        'app_name': 'seerdb',
    }
    base.update(extra)
    return {'env': base}


class TestDescriptor(unittest.TestCase):
    def test_a_class_or_purity_alone_requests_no_pooled_server(self):
        # As in python-oracledb: only server_type='pooled' does (#1990).
        for env in (_env(cclass='APP'), _env(purity=PURITY_SELF)):
            with self.subTest(env=env):
                self.assertNotIn(b'(SERVER=', encode_dictionary_description(env))

    def test_no_pooled_otherwise(self):
        self.assertNotIn(b'(SERVER=POOLED)', encode_dictionary_description(_env()))


def _auth_request(**env) -> bytes:
    challenge = make_challenge(b'p')
    (request, _) = encode_dictionary_auth(
        {
            'seq': 1,
            'field_version': FIELD_VERSION_12_1,
            'auth': {
                'sess': challenge.auth_sesskey,
                'salt': challenge.salt,
                'derived_salt': None,
            },
            'env': {'user': 'U', 'password': 'p', **env},
        }
    )
    return request


# What python-oracledb 26.0.1 sent for AUTH_KPPL_PURITY = NEW, captured from its
# login to 23ai: the key, then the value '1' with flags 1 (#1977).
_PURITY_NEW = b'AUTH_KPPL_PURITY' + bytes.fromhex('010101310101')


class TestServerType(unittest.TestCase):
    # server_type (#1977): (SERVER=...) as python-oracledb writes it, and
    # 'pooled' asks for DRCP on its own, sending PURITY = NEW and no class.
    def test_the_descriptor_names_the_server_type(self):
        for kind in ('dedicated', 'shared', 'pooled'):
            with self.subTest(kind=kind):
                text = encode_dictionary_description(_env(server_type=kind))
                self.assertIn(b'(SERVER=' + kind.encode() + b')', text)
                self.assertNotIn(b'(SERVER=POOLED)', text)

    def test_server_type_wins_over_the_drcp_default(self):
        text = encode_dictionary_description(_env(server_type='dedicated', cclass='A'))
        self.assertIn(b'(SERVER=dedicated)', text)
        self.assertNotIn(b'POOLED', text)

    def test_pooled_alone_sends_purity_new_and_no_class(self):
        request = _auth_request(server_type='pooled')
        self.assertIn(_PURITY_NEW, request)
        self.assertNotIn(b'AUTH_KPPL_CONN_CLASS', request)

    def test_no_drcp_pairs_without_it(self):
        self.assertNotIn(b'AUTH_KPPL', _auth_request(server_type='dedicated'))
        self.assertNotIn(b'AUTH_KPPL', _auth_request())


# What python-oracledb 26.0.1 sent to 23ai with a connection class or a purity
# and no server type: each pair as given, and no purity it was not given (#1990).
_CONN_CLASS_C1 = b'AUTH_KPPL_CONN_CLASS' + bytes.fromhex('0102024331')
_PURITY_SELF = b'AUTH_KPPL_PURITY' + bytes.fromhex('010101320101')


class TestDrcpPairs(unittest.TestCase):
    def test_a_class_alone_labels_the_session(self):
        request = _auth_request(cclass='C1')
        self.assertIn(_CONN_CLASS_C1, request)
        self.assertNotIn(b'AUTH_KPPL_PURITY', request)

    def test_a_purity_goes_out_as_given(self):
        request = _auth_request(purity=PURITY_SELF)
        self.assertIn(_PURITY_SELF, request)
        self.assertNotIn(b'AUTH_KPPL_CONN_CLASS', request)

    def test_pooled_still_defaults_to_new(self):
        self.assertIn(_PURITY_NEW, _auth_request(server_type='pooled', cclass='C1'))


class TestParams(unittest.TestCase):
    def test_connection_threads_cclass_purity(self):
        c = OracleConnect(
            host='x', port=1, user='u', password='p', cclass='MYAPP', purity=PURITY_SELF
        )
        self.assertEqual(c.cclass, 'MYAPP')
        self.assertEqual(c.purity, PURITY_SELF)
        env = c._make_dict(None)['env']
        self.assertEqual(env['cclass'], 'MYAPP')
        self.assertEqual(env['purity'], PURITY_SELF)


class TestPiggyback(unittest.TestCase):
    def test_skips_sess_ret(self):
        # token 23, opcode 4 (SESS_RET) with all-empty fields, then STA -> the
        # decoder consumes the piggyback and lands on the status token.
        pkt = bytes([TTI_SVR_PIGGYBACK, 4, 0, 0, 0, 0, 0, 0, TTI_STA])
        self.assertEqual(decode_packet(pkt, (None, None, [])), (True, (None, None, [])))

    def test_skips_os_pid_mts(self):
        # opcode 2 (OS_PID_MTS): a ub2 then a length-prefixed pid, then STA
        pkt = bytes([TTI_SVR_PIGGYBACK, 2, 0, 3]) + b'pid' + bytes([TTI_STA])
        self.assertEqual(decode_packet(pkt, (None, None, [])), (True, (None, None, [])))


if __name__ == '__main__':
    unittest.main()
