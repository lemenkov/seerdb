# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT
"""The TTC field versions, each one declared (#1487)."""

import unittest

from seerdb.common import tns_consts

# Every TTC field version, by the value the handshake carries; an extension is
# always spelled _EXT_<n>.
FIELD_VERSIONS = {
    '11_2': 6,
    '12_1': 7,
    '12_2': 8,
    '12_2_EXT_1': 9,
    '18_1': 10,
    '18_1_EXT_1': 11,
    '19_1': 12,
    '19_1_EXT_1': 13,
    '20_1': 14,
    '20_1_EXT_1': 15,
    '21_1': 16,
    '23_1': 17,
    '23_1_EXT_1': 18,
    '23_1_EXT_2': 19,
    '23_1_EXT_3': 20,
    '23_1_EXT_4': 21,
    '23_1_EXT_5': 22,
    '23_3_EXT_6': 23,
    '23_4': 24,
    'MAX': 24,
}


class TestFieldVersions(unittest.TestCase):
    def test_every_field_version_is_declared(self):
        for name, value in FIELD_VERSIONS.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(tns_consts, f'FIELD_VERSION_{name}'), value)

    def test_nothing_else_but_the_pre_11_2_versions(self):
        # Beyond the table: the 9i and 10g versions below 11.2.
        declared = {
            n.removeprefix('FIELD_VERSION_')
            for n in dir(tns_consts)
            if n.startswith('FIELD_VERSION_')
        }
        self.assertEqual(declared - set(FIELD_VERSIONS), {'9_2', '10_2'})
