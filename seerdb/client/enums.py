# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""Enumerations of the client API, named and valued as python-oracledb's."""

from __future__ import annotations

import enum


class TransactionPriority(str, enum.Enum):
    """A session's priority for 23ai's priority transactions (#1776): the value
    of ``connect(transaction_priority=...)`` and of
    ``connection.transaction_priority``. ``DEFAULT`` restores the database's
    default on the next round trip."""

    DEFAULT = ''
    LOW = 'low'
    MEDIUM = 'medium'
    HIGH = 'high'
