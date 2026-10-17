<!-- SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com> -->
<!-- SPDX-License-Identifier: MIT -->

# Security policy

## Reporting a vulnerability

Report it privately, through GitHub's
[private vulnerability reporting](https://github.com/seerdb/seerdb/security/advisories/new),
not in a public issue, discussion or pull request. A report becomes a private
security advisory. Its fix is prepared there and released, and the advisory is
published with the release that fixes it.

Please include what is affected (the client, or the Mirror and which backend),
the seerdb version, and how to reproduce it.

## Supported versions

Fixes go into the latest release. There are no backport branches.

## Scope

- **The client** (`seerdb`, the DB-API 2.0 driver): any vulnerability.
- **The Mirror** (`seerdb.server`, the `mirror` extra): a technology preview
  until its general availability. Its intended deployment is behind a firewall
  or VPN, as an Oracle database's is, for clients that may log in but are not
  trusted otherwise. Exposure to the open internet is out of scope. See the
  *Security* section of [docs/MIRROR.md](docs/MIRROR.md) for how to run it.
