# Roadmap

What is being worked on, what comes next, and what is only being considered. It
is a statement of direction, not a promise of dates.

Issue and milestone links are the source of truth. This file explains the
*shape* of the work and records the threads that would otherwise live only in
someone's head.

## How the work is organised

seerdb uses **feature milestones, never version milestones**. A milestone
describes what it delivers, not which release it lands in. Version buckets force
unrelated work together and go stale as scope moves.

Releases of the client follow semantic versioning and are cut when there is
something to ship. Larger goals are **gated** instead of scheduled: a gate
milestone holds no issues of its own, and it is reached when every milestone it
names is closed. A milestone does *not* gate when it is blocked on something
outside this project's control, or is deliberately open-ended. Otherwise a goal
becomes hostage to work that may never finish.

## Where the project is

The wire protocol is implemented directly, with no vendor client libraries. The
client is validated live against every supported server, from **Oracle 8i
(8.1.7) through 26ai**; see the support matrix in the [README](README.md).

Done (items marked *unreleased* are on `master`, awaiting the next release):

- **Protocol coverage** across the whole version range:
  - the pre-10g dialects (8i, 9i), including `DML … RETURNING` there through a
    PL/SQL rewrite;
  - 12c+ framing;
  - the 23ai surface: JSON/OSON, `BOOLEAN`, `VECTOR`, column annotations and
    fast-auth.
- **The native 9i dialect** (*unreleased*), an experimental opt-in (`oall8=True`). It is the
  9i server as a native 9.2 OCI client sees it, and passes the integration
  suite there. It stays an opt-in because the client has to choose it before
  the server identifies itself
  ([#799](https://github.com/seerdb/seerdb/issues/799)).
- **One codec for both sides.** Every frame is described once in a wire schema,
  in `seerdb/common`, and compiled into both its encoder and its decoder, so the
  client and the Mirror share one description of the wire.
- **Native network encryption** (ANO), validated against a server that requires
  AES256 + SHA256.
- **SQLAlchemy support** through the
  [sqlalchemy-seerdb](https://github.com/seerdb/sqlalchemy-seerdb) dialect, with
  its compliance suite green.
- **ws:// and wss:// transport** (*unreleased*) for browser-side clients.
- **The Mirror** (`seerdb.server`, the `mirror` extra), shipped since 3.1.0. It
  is an Oracle-wire server over PostgreSQL, or a passthrough to a real Oracle.
  - It serves thin clients from field version 4 up, and sqlplus over the OCI
    dialect from 10.2 to 23.26.
  - It presents as 23ai by default, or as 12.1.
  - python-oracledb's own test suite runs through it.

## Guiding principle: features are not tied to protocol versions

What a session can do should depend on what the **database behind it** can do,
not on the protocol generation the client happens to speak. For example, a
sqlplus that logs in over a 10g-era protocol should be able to use a 23ai or 26ai
feature when the backend has it. The Mirror would express the result in a form
that client's protocol can carry, as Oracle does for its own older clients.

That needs a per-session table of what the client can carry, negotiated rather
than assumed from its version, and a mapping for each feature that has no
older-protocol form. It cuts across the codec, the Mirror's session layer and the
type mapping, so it starts with a design ticket.

**In the client, the same principle has a hard limit.** The client is
decoupled from protocol limits, never from what the database can do:

1. **The database lacks the feature: the client refuses, clearly and early.**
   It never emulates one, for example `BOOLEAN` columns on 11g or JSON on a
   server without it. An emulation drifts from the real thing in comparisons,
   NULLs, ordering and errors, and those differences surface in production,
   not at the first call. A clear refusal tells users what their database is.
2. **The database has the feature but the protocol form is missing: the client
   may route it another way, only when the result is exact.** The values, the
   errors and the transaction behaviour must be the server's own. Pre-10g
   `RETURNING` is the model case. 9i and 8i run `RETURNING` inside PL/SQL and
   refuse only the 10g request form, so the client sends the statement as a
   PL/SQL block. If a route can't be exact, the client refuses instead.
3. **Every such route is documented with its limits**, as the README does for
   pre-10g `RETURNING`.

The Mirror's translation for older clients is rule 2 seen from the server side.
The backend really has the feature, and only its form changes.

## Now: the Mirror towards general availability

The Mirror is an **early-stage product**, close to production grade. Until
general availability it remains a technology preview, outside semantic
versioning ([docs/MIRROR.md](docs/MIRROR.md)).

It reaches general availability at the gate
[mirror-pg-general-availability](https://github.com/seerdb/seerdb/milestone/51),
when these milestones close:

- [mirror-security-hardening](https://github.com/seerdb/seerdb/milestone/52):
  the security review before the public release.
- [mirror-interface-stability](https://github.com/seerdb/seerdb/milestone/54):
  the user guide.
- [mirror-pg-performance](https://github.com/seerdb/seerdb/milestone/44): a
  measured baseline and no known pathologies. It is open with no issues while
  further optimisations are measured.
- [mirror-pg-multi-user](https://github.com/seerdb/seerdb/milestone/53):
  closed.

**Security**, beyond the Mirror: a disclosure and embargo policy
([#1959](https://github.com/seerdb/seerdb/issues/1959)).

**Performance**, beyond the Mirror: the wire-schema decoder costs about a
microsecond and a half per call. A faster decoder is deferred, behind bugs and
features.

## Next

**Thin breadth across versions.** The client speaks each server version's
protocol through the form that version's own thin drivers use:

- [9i features over the thin ALL7 path](https://github.com/seerdb/seerdb/milestone/60):
  what the client still lacks on 9i, delivered over the protocol it already
  speaks there, with no native login.
- [legacy-reach](https://github.com/seerdb/seerdb/milestone/45): older Oracle
  generations at both ends.
- [post-23ai protocol](https://github.com/seerdb/seerdb/milestone/29): wire
  features above the 23ai baseline. Open-ended; it does not gate.

**Thick: the native OCI protocol, from both sides.** The client already speaks
the native form to 8i and 9i. The aims:

- **The Mirror serves older OCI clients**, below the sqlplus 10.2 it serves
  today. The shared wire schemas make this cheaper: the 9.2 frames the client
  decodes are the same frames a Mirror would encode.
- **Reach the server features that answer only an OCI client**, in the client
  and through the Mirror. Each one is identified from a capture first.

This stays research-shaped, one capture at a time, until each step is proven.

**Authentication** is a separate track from thick:

- the native sqlplus 16-byte O3LOGON variant;
- Kerberos5 over the ANO auth service
  ([#2378](https://github.com/seerdb/seerdb/issues/2378)).

## Opportunistic

These move on someone else's schedule, or are not needed for general
availability. They do not gate.

- **Upstreaming** the Mirror's PostgreSQL-side Oracle compatibility into orafce,
  PostgreSQL and others ([upstream](https://github.com/seerdb/seerdb/milestone/40)).
  Every piece that lands upstream is SQL the Mirror stops shipping.
- **Storage fidelity** where PostgreSQL's types are narrower than Oracle's
  ([mirror-pg-storage-fidelity](https://github.com/seerdb/seerdb/milestone/50)).
- **A parsed SQL translator**
  ([mirror-sql-transpiler](https://github.com/seerdb/seerdb/milestone/55)).
  Postponed: general availability ships on the current translator.

## Under consideration

Directions that are *not* commitments. They are recorded here so they are not
lost, and so that promoting one into a milestone is a deliberate act.

- **A Python `sqlplus` equivalent.** An application built *on* the driver rather
  than protocol work: a command interpreter, statement buffer, output
  formatting, substitution and bind variables, `DBMS_OUTPUT`. Not started.
- **Testbed continuity.** The 8i, 9i and 10g servers are validated only on
  local beds. If the thick and legacy work is strategic, those beds need to be
  reproducible, not one-off machines.
- **The protocol description as a deliverable.**
  [docs/PROTOCOL.md](docs/PROTOCOL.md) already serves other implementations.
  Keeping it complete enough to build from is a goal of its own.

## Non-goals

- **Compatibility with proprietary client libraries.** The point is to need
  none.
- **A complete Oracle replacement.** The Mirror serves what its backend can
  express, and where it differs from Oracle the difference is documented rather
  than hidden.
- **Bug-for-bug parity with other drivers.** Where behaviour is ambiguous the
  project follows the widely-used Python driver's thin mode, but deliberate
  differences are kept and documented.
