# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""The backends the Mirror ships with.

- ``postgres``: serves every statement from PostgreSQL (needs ``psycopg``).
- ``oracle_compat``: answers sqlplus's session bootstrap over any backend.
- ``passthrough``: relays to a real Oracle database through seerdb.

Import the one you need; this package imports none of them, so loading it pulls
in no backend's dependencies.
"""
