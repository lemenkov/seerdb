# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT
"""Shared pytest fixtures for the seerdb test suite."""

import pytest

from seerdb.common.tns import _DECODE_FIELD_VERSION, _ENCODE_FIELD_VERSION
from seerdb.common.tns_consts import FIELD_VERSION_11_2


@pytest.fixture(autouse=True)
def _reset_field_version_contextvars():
    # The codec pins the negotiated field version in two module-level ContextVars,
    # and a real decode/encode sets them without resetting -- production is
    # self-correcting, because every top-level decode_packet / encode_dictionary_exec
    # sets the version fresh for the reply or request it is about to handle. A test
    # that decodes at the default 11.2 layout has no such call, so it would inherit
    # whatever version an earlier test's client or Mirror session last set (e.g. the
    # higher-field-version serve_session cases leave it at 23ai), and misread the
    # bytes. Pin the default before each test so every test starts isolated.
    _DECODE_FIELD_VERSION.set(FIELD_VERSION_11_2)
    _ENCODE_FIELD_VERSION.set(FIELD_VERSION_11_2)
    yield
