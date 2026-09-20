# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

# Offline tests for PL/SQL associative-array (index-by table) binds (#122):
# the arrayvar bind container, the array bind-OAC (ARRAY flag + max elements),
# the count+N value encode, and the OUT assignment from an array IOV record.

import unittest

from seerdb.client.cursor import Cursor, _assign_out_binds
from seerdb.common.datatypes import Var
from seerdb.common.tns import (
    _ENCODE_FIELD_VERSION,
    decode_ub4,
    encode_sb4,
    encode_token_oac,
    encode_token_rxd,
)
from seerdb.common.tns_consts import TNS_TYPE_NUMBER


def _arrayvar(typ, value_or_n):
    # arrayvar is a plain factory (no cursor state) -- call it unbound.
    return Cursor.arrayvar(None, typ, value_or_n)


class TestArrayVar(unittest.TestCase):
    def test_arrayvar_from_list(self):
        v = _arrayvar(int, [1, 2, 3])
        self.assertTrue(v.is_array)
        self.assertEqual(v.num_elements, 3)
        self.assertEqual(v._value, [1, 2, 3])

    def test_arrayvar_from_size(self):
        v = _arrayvar(int, 25)
        self.assertTrue(v.is_array)
        self.assertEqual(v.num_elements, 25)
        self.assertEqual(v._value, [])


class TestArrayEncode(unittest.TestCase):
    def setUp(self):
        _ENCODE_FIELD_VERSION.set(8)  # 12c+ OAC form

    def tearDown(self):
        _ENCODE_FIELD_VERSION.set(6)  # restore module default

    def test_oac_sets_array_flag_and_max(self):
        oac = encode_token_oac(_arrayvar(int, 7))
        # 12c+ OAC: [type, flag, 0, 0] + sb4(length) + sb4(max_elements) + ...
        self.assertEqual(oac[0], TNS_TYPE_NUMBER)
        self.assertEqual(oac[1], 0x41)  # USE_INDICATORS | ARRAY
        (length, rest) = decode_ub4(oac[4:])
        (max_elems, _rest) = decode_ub4(rest)
        self.assertEqual(max_elems, 7)
        # a scalar Var has the plain flag and 0 max elements
        scalar = encode_token_oac(Var(int))
        self.assertEqual(scalar[1], 1)

    def test_value_is_count_then_elements(self):
        out = encode_token_rxd(_arrayvar(int, [11, 22, 33]))
        self.assertEqual(out[: len(encode_sb4(3))], encode_sb4(3))  # count = 3
        # empty array -> count 0, no elements
        self.assertEqual(encode_token_rxd(_arrayvar(int, 5)), encode_sb4(0))


class TestNationalArray(unittest.TestCase):
    """A national (csfrm 2) array carries AL16UTF16 in BOTH directions (#991).

    The charset form is a property of the BIND, not of the value. An element is
    encoded and decoded as a bare Python value, so it never saw the Var's csfrm:
    going out it went as UTF-8, and the server read those bytes as UTF-16BE and
    got a string of half the length -- with no error, because they are a
    perfectly valid if wrong string. Coming back it was the raw UTF-16BE bytes
    read one per character.
    """

    def setUp(self):
        _ENCODE_FIELD_VERSION.set(8)

    def tearDown(self):
        _ENCODE_FIELD_VERSION.set(6)

    def test_elements_go_out_as_utf16be(self):
        from seerdb.common.datatypes import DB_TYPE_NVARCHAR

        text = 'Unicode \u3042 0'  # 11 characters, 13 bytes in UTF-8
        out = encode_token_rxd(_arrayvar(DB_TYPE_NVARCHAR, [text]))
        # The count, then the element: a length and the AL16UTF16 bytes.
        self.assertTrue(out.startswith(encode_sb4(1)))
        body = out[len(encode_sb4(1)) :]
        self.assertIn(text.encode('utf-16-be'), body)
        self.assertNotIn(text.encode('utf-8'), body)

    def test_an_ordinary_array_still_goes_out_as_utf8(self):
        from seerdb.common.datatypes import DB_TYPE_VARCHAR

        text = 'Unicode \u3042 0'
        out = encode_token_rxd(_arrayvar(DB_TYPE_VARCHAR, [text]))
        self.assertIn(text.encode('utf-8'), out)

    def test_out_elements_are_decoded_from_utf16be(self):
        from seerdb.common.datatypes import DB_TYPE_NVARCHAR

        text = 'Test out \u3042'
        v = _arrayvar(DB_TYPE_NVARCHAR, 10)
        record = {
            'out_positions': [0],
            'out_values': [{'_array': True, 'values': [text.encode('utf-16-be')]}],
        }
        _assign_out_binds([v], (None, None, None, None, [record]))
        self.assertEqual(v.getvalue(), [text])


class TestArrayAssign(unittest.TestCase):
    def test_out_array_decoded_to_list(self):
        v = _arrayvar(int, 10)
        # NUMBER images for 1, 2, 3 (Oracle base-100): c1 02 / c1 03 / c1 04.
        record = {
            'out_positions': [0],
            'out_values': [
                {'_array': True, 'values': [b'\xc1\x02', b'\xc1\x03', b'\xc1\x04']}
            ],
        }
        result = (None, None, None, None, [record])
        _assign_out_binds([v], result)
        self.assertEqual(v.getvalue(), [1, 2, 3])


if __name__ == '__main__':
    unittest.main()
