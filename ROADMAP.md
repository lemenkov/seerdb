# Roadmap

What is being worked on, what is deliberately parked, and what is only being
considered. It is a statement of direction, not a promise of dates.

Issue and milestone links are the source of truth; this file explains the
*shape* of the work and records the threads that would otherwise live only in
someone's head.

## How the work is organised

seerdb uses **feature milestones, never version milestones**. A milestone
describes what it delivers, not which release it lands in — version buckets
force unrelated work together and go stale as scope moves.

Releases are gated instead of scheduled:

> **3.0.0 is reachable when every gating milestone is closed.**

Each milestone's description states whether it gates. A milestone does *not*
gate when it is blocked on something outside this project's control, or is
deliberately open-ended — otherwise a release becomes hostage to work that may
never finish. See [milestone #18](https://github.com/seerdb/seerdb/milestone/18).

## Where the project is

The wire protocol is implemented directly, with no vendor client libraries, and
is validated live against every supported server from **Oracle 8i (8.1.7)
through 26ai** — see the support matrix in the [README](README.md).

Shipped and closed:

- **Protocol coverage** across the full version range, including the pre-10g
  dialects (8i, 9i), 12c+ framing, and the 23ai surface (JSON/OSON, `BOOLEAN`,
  `VECTOR`, column annotations, fast-auth).
- **Native network encryption** (ANO), validated against a server that requires
  AES256 + SHA256.
- **SQLAlchemy support** — seerdb 2.5.0 and the
  [sqlalchemy-seerdb](https://github.com/seerdb/sqlalchemy-seerdb) dialect
  shipped together, with the dialect's compliance suite green.

## Now

**[#35 — 9i/8i `DML … RETURNING`](https://github.com/seerdb/seerdb/milestone/35)**
*(gates 3.0.0)*

`RETURNING … INTO` is the one capability the pre-10g tiers lack against every
other supported version. The server refuses the 10g+ request form below 10g
(`ORA-00439`), but it accepts the same statement wrapped in an anonymous PL/SQL
block with OUT binds — machinery the driver already has. No new wire protocol is
involved.

When this lands, the pre-10g limitation described in the README is removed.

Also open, outside any milestone:

- [#805](https://github.com/seerdb/seerdb/issues/805) — `connect()` reports
  success when the server closed the connection mid-login, deferring the real
  failure to the next statement.
- [#380](https://github.com/seerdb/seerdb/issues/380) — audit the places the
  pre-10g codec assumes little-endian, where the wire is server-native-endian.
- [#800](https://github.com/seerdb/seerdb/issues/800) — drop the demo backend's
  hand-rolled `UTL_RAW` once orafce 4.17 is released, since that release carries
  it upstream. Waiting on the upstream tag.

## Blocked

**[#36 — the native 9i OALL8 dialect](https://github.com/seerdb/seerdb/milestone/36)**
*(does not gate a release)*

Oracle's own client reaches a request form seerdb cannot currently negotiate.
Repeated attempts established that this is a property of the **login mode**, not
of a capability byte: the server refuses the form from an ordinary login
regardless of which capabilities are advertised or which field widths are sent.
Reaching it requires reproducing the native client's whole login handshake.

These tickets stay open because they describe genuinely missing functionality.
They are not closed merely because the protocol has resisted so far, and they do
not gate a release, because they may not be solvable on any schedule.

## Under consideration

Directions that are *not* commitments. They are recorded here so they are not
lost, and so that promoting one into a gating milestone is a deliberate act.

- **Higher-version conformance for the server-side implementation.** The
  experimental Oracle-wire server currently presents as 11g. Negotiating and
  answering at later field versions exercises far more of the protocol from the
  serving side than any client test can. The remaining gaps at field version 17
  are concentrated in the newest types (JSON/OSON, `VECTOR`) and their async
  equivalents.
- **Presenting as 12c.** The protocol work for this is largely done; what
  remains is framing, identity and defaults.
- **Consolidating the codec.** Moving every encode/decode primitive into the
  shared `common` package, so encoder and decoder for a given format sit
  together and neither the client nor the server owns a private copy. Partially
  done.
- **A Python `sqlplus` equivalent.** An application built *on* the driver rather
  than protocol work — a command interpreter, statement buffer, output
  formatting, substitution and bind variables, `DBMS_OUTPUT`. Attractive as a
  demonstration; not started, and not obviously the best use of effort.

## Non-goals

- **Compatibility with proprietary client libraries.** The point is to need
  none.
- **Being a general Oracle-compatibility layer.** The PostgreSQL-backed
  demonstration in `examples/` is a prototype for exercising the server side,
  not a product, and it may raise its dependency requirements whenever that
  buys a better prototype.
- **Bug-for-bug parity with other drivers.** Where behaviour is ambiguous the
  project follows the widely-used Python driver's thin mode, but deliberate
  differences are kept and documented.
