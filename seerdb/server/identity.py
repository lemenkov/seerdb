# SPDX-FileCopyrightText: 2019 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: MIT

"""What the Mirror answers when a client asks which server it is.

The login result carries the server's release (``AUTH_VERSION_NO``, the value a
client turns into ``connection.version``) and sqlplus prints a banner after
"Connected to:". Those were pinned to the captured XE 11.2 server, so a Mirror
advertising a 12c field version still introduced itself as 11.2.0.2.0. They now
follow the advertised field version instead.

**The packed release format**: ``major<<24 | minor<<20 | update<<12 | patch<<8 |
port`` before 18c, ``major<<24 | update<<16 | revision<<12 | increment<<4 |
extension`` from it (#2097). Verified by decoding what three live servers send —
XE 11.2 -> 11.2.0.2.0, XE 21c -> 21.3.0.0.0, 23ai -> 23.26.2.0.0.

``AUTH_VERSION_SQL`` is a small counter that moves with the release; the captured
anchors are 11.2 -> 22, 21c -> 25, 23ai -> 26. **12.2's value is not captured**
(there is no 12.2 testbed here), so 23 is taken from the gap between those
anchors. Nothing reads it back — a client decodes only ``AUTH_VERSION_NO`` into
its version property — so it is descriptive, not load-bearing.

The 21c and 23ai banners are what those two testbeds print after "Connected
to:" (``v$version``), measured 2026-09-06. The 23ai testbed is the 26ai Free
build of the 23 lineage, release 23.26.2.
"""

from __future__ import annotations

from dataclasses import dataclass

from seerdb.common.tns_consts import (
    FIELD_VERSION_12_2,
    FIELD_VERSION_21_1,
    FIELD_VERSION_23_1,
    VERSION_11_2_0_2,
    VERSION_12_1_0_2,
    VERSION_12_2_0_1,
    VERSION_21_0_0_0,
    VERSION_21_3_0_0,
    VERSION_23_0_0_0,
    VERSION_23_26_2_0,
)


@dataclass(frozen=True)
class ServerIdentity:
    """The release a Mirror session claims to be."""

    version_no: int
    """Packed release, sent as ``AUTH_VERSION_NO`` and decoded by the client."""

    version_sql: bytes
    """``AUTH_VERSION_SQL`` — descriptive; nothing reads it back."""

    version_string: bytes
    """``AUTH_VERSION_STRING`` — the build suffix, e.g. ``- 64bit Production``."""

    banner: bytes
    """What sqlplus prints after "Connected to:"."""

    # What sqlplus's version call is answered with (#2024). A server tells an
    # 18c or later client its full release and the two-line banner_full, an
    # older one its base release and the one-line banner: measured, 23ai sends
    # sqlplus 23.26 23.26.2.0.0 and "...\nVersion 23.26.2.0.0", and sqlplus 11.2
    # 23.0.0.0.0 and the banner alone.
    release: int = 0
    """The packed base release (``product_component_version.version``) the
    version call carries to a client before 18c; 0 for ``version_no``."""

    release_full: int = 0
    """The packed full release (``version_full``), in the 18c layout, the
    version call carries to an 18c or later client; 0 for ``version_no``."""

    banner_full: bytes = b''
    """``v$version.banner_full``, what an 18c or later sqlplus prints; empty
    for ``banner``."""

    def version_reply(self, full: bool) -> tuple[bytes, int]:
        """The banner and packed release sqlplus's version call is answered
        with, for an 18c or later client (``full``) or an older one."""
        if full:
            return self.banner_full or self.banner, self.release_full or self.version_no
        return self.banner, self.release or self.version_no


IDENTITY_11_2 = ServerIdentity(
    version_no=VERSION_11_2_0_2,  # 11.2.0.2.0
    version_sql=b'22',
    version_string=b'- 64bit Production',
    banner=(
        b'Oracle Database 11g Express Edition Release 11.2.0.2.0 - 64bit Production'
    ),
)

IDENTITY_12_1 = ServerIdentity(
    version_no=VERSION_12_1_0_2,  # 12.1.0.2.0
    version_sql=b'22',
    version_string=b'- 64bit Production',
    banner=(
        b'Oracle Database 12c Enterprise Edition Release 12.1.0.2.0 - 64bit Production'
    ),
)

IDENTITY_12_2 = ServerIdentity(
    version_no=VERSION_12_2_0_1,  # 12.2.0.1.0
    version_sql=b'23',
    version_string=b'- 64bit Production',
    banner=(
        b'Oracle Database 12c Enterprise Edition Release 12.2.0.1.0 - 64bit Production'
    ),
)

IDENTITY_21 = ServerIdentity(
    version_no=VERSION_21_3_0_0,  # 21.3.0.0.0
    version_sql=b'25',
    version_string=b'- Production',
    banner=b'Oracle Database 21c Express Edition Release 21.0.0.0.0 - Production',
    release=VERSION_21_0_0_0,
    banner_full=(
        b'Oracle Database 21c Express Edition Release 21.0.0.0.0 - Production\n'
        b'Version 21.3.0.0.0'
    ),
)

IDENTITY_23 = ServerIdentity(
    version_no=VERSION_23_26_2_0,  # 23.26.2.0.0
    version_sql=b'26',
    version_string=b'- Develop, Learn, and Run for Free',
    banner=(
        b'Oracle AI Database 26ai Free Release 23.26.2.0.0 '
        b'- Develop, Learn, and Run for Free'
    ),
    release=VERSION_23_0_0_0,
    banner_full=(
        b'Oracle AI Database 26ai Free Release 23.26.2.0.0 '
        b'- Develop, Learn, and Run for Free\nVersion 23.26.2.0.0'
    ),
)


def server_identity(field_version: int) -> ServerIdentity:
    """The identity a Mirror advertising ``field_version`` introduces itself with.

    Four tiers, each the highest release there is an identity for at that
    field version: below 12.2 the captured 11.2, then 12.2 up to the 21c field
    version, 21c up to the 23ai one, and 23ai from there on. A client gates
    features on the release it decodes from the login (SODA from 18c, for
    one), so a Mirror at a 23ai field version that still said 12.2 kept those
    features out of reach of whatever its backend could do.
    """
    if field_version >= FIELD_VERSION_23_1:
        return IDENTITY_23
    if field_version >= FIELD_VERSION_21_1:
        return IDENTITY_21
    if field_version >= FIELD_VERSION_12_2:
        return IDENTITY_12_2
    return IDENTITY_11_2
