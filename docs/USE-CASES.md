<!-- SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com> -->
<!-- SPDX-License-Identifier: MIT -->

# Possible use cases

Where seerdb could fit, where it doesn't, and why. This page collects
situations we know of, or can describe honestly. It is not a list of
deployments: the project has no customers yet, and nothing here claims anyone
runs seerdb. Each case says what it would rely on and where it stops being the
right tool. The client (the DB-API 2.0 driver) is the product. The Mirror is a
technology preview ([MIRROR.md](MIRROR.md)), and the cases that use it say so.

## A Python program that talks to an older Oracle server

**The situation.** A script, a service or a data job needs to read or write an
Oracle database that is older than current drivers support.

**Why seerdb.** python-oracledb, the reference Python driver, connects in its
thin mode to Oracle Database 12.1 and later. Its thick mode needs Oracle Client
libraries, versions 23, 21 or 19, and Client 19, the oldest of those, reaches
Oracle Database 11.2 and later ([its installation
guide](https://python-oracledb.readthedocs.io/en/latest/user_guide/installation.html)).
seerdb is pure Python, needs no Oracle client software, and speaks to every
release from 8i (8.1.7) to 26ai with one build: see the README's
*Compatibility* table for what each tier supports.

**Where it stops.** The older the server, the smaller the surface: the README's
notes on 9i and 8i list what those tiers lack. 8i, 9i and 10g are tested on
local beds, not in CI.

## The long middle of a migration away from Oracle

**The situation.** An organisation is moving off a large Oracle database, and
the move takes years. Through all of it the old database stays live, and
around it grows code that reads it: reconciliation scripts that compare the old
system with the new, extracts into a warehouse, and exchange services that keep
both sides in step.

**What it often looks like.** A database decades old, with much of the
business logic in stored PL/SQL (packages, procedures, triggers), sometimes
even generating the application's pages. Rather than converting that code, the
logic is rewritten as new services. The old database stays live while users
move over one group at a time. Some data is synced both ways for the duration,
and the two systems are compared by scripts and reconciled by hand.

**Why seerdb.** Code like that runs next to the old database for as long as the
migration does, often on whatever machines are at hand. A pure-Python driver
with no client libraries to install is easy to put there. It works whatever the
legacy server's release is, which is often an old one in a database that age.
Long-lived sessions are where cursor handling matters, and it has been tested
for them: a session runs more statements than the server's `open_cursors` on
every tier (#2286, #2288, #2291).

**Where it stops.** seerdb reads and writes the old database; it does not move
logic out of it. A database whose logic lives in large amounts of stored
PL/SQL needs that logic rewritten. No driver or compatibility layer removes
that work.

## Running an Oracle application against PostgreSQL (Mirror, tech preview)

**The situation.** An application written for Oracle should run against
PostgreSQL, ideally without changing its code, and then move to native
PostgreSQL a statement at a time.

**Why the Mirror.** It speaks Oracle's wire protocol and translates Oracle SQL
and PL/SQL to PostgreSQL as statements arrive, so the application's driver
connects to it as to an Oracle server ([MIRROR.md](MIRROR.md)). Its migration
profile records per-statement statistics during a run, and compares an Oracle
run with a PostgreSQL one.

**Where it stops.** It fits when the logic lives in the application and its
SQL. A database whose logic is large stored PL/SQL, or one that generates its
application's pages from stored procedures, is past what the Mirror's PL/SQL
translation covers. The Mirror is a technology preview:
not for production, and outside the version promise the client keeps.

## Testing or capturing a client against a real server (Mirror, passthrough)

**The situation.** A team building or testing an Oracle client wants to see
exactly what it sends and receives, or to run its test suite against something
that answers like Oracle.

**Why the Mirror.** In passthrough mode it relays every call to a real Oracle
server and returns its answers. A client's suite run through it shows every
gap in the Mirror's protocol handling. seerdb uses it this way for
python-oracledb's own suite.

**Where it stops.** It is a test and capture tool, not a gateway to put in front
of a production database.

## Adding a case

A case belongs here when it can be described honestly: the situation, what
seerdb or the Mirror would rely on, and where it stops. Describe a situation,
not an organisation: no company or person is named here, and nothing that
points to one -- a name, a link to their write-up, figures from it.
