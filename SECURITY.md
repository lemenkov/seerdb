<!-- SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com> -->
<!-- SPDX-License-Identifier: MIT -->

# Security policy

## Reporting a vulnerability

Report it privately, through GitHub's
[private vulnerability reporting](https://github.com/seerdb/seerdb/security/advisories/new),
not in a public issue, discussion or pull request. A report becomes a private
security advisory.

Please include what is affected (the client, or the Mirror and which backend),
the seerdb version, and how to reproduce it.

## How a report is handled

Until the Mirror's general availability, while the user base is small:

- **The fix** is an ordinary public pull request, which names the advisory.
  There is no embargo: once the fix is public, keeping the advisory private
  would only hide it from the people who act on it.
- **The advisory** is published with the release that ships the fix, which it
  names as the patched version.
- **A fix for a released version** gets a release of its own promptly. It does
  not wait for unrelated work.
- **A flaw in code that was never released** still gets its advisory
  published, as a record, saying that no released version is affected.
- **A CVE** is requested through GitHub for a flaw that affected a release.
- **The ChangeLog** names the advisory in the fix's entry.

From the Mirror's general availability on, or once deployments are known, fixes
are developed in the advisory's private fork and published together with the
advisory, under an embargo with response times. The details are being settled
in [#1959](https://github.com/seerdb/seerdb/issues/1959).

## Supported versions

Fixes go into the latest release. There are no backport branches.

## Scope

- **The client** (`seerdb`, the DB-API 2.0 driver): any vulnerability.
- **The Mirror** (`seerdb.server`, the `mirror` extra): a technology preview
  until its general availability. Its intended deployment is behind a firewall
  or VPN, as an Oracle database's is, for clients that may log in but are not
  trusted otherwise. Exposure to the open internet is out of scope. See the
  *Security* section of [docs/MIRROR.md](docs/MIRROR.md) for how to run it.
