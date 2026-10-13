<!-- SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com> -->
<!-- SPDX-License-Identifier: MIT -->

# The Mirror

> **A highly experimental technology preview.** The Mirror ships with seerdb so
> you can try it, not to run production on. It is outside the
> semantic-versioning promise the client (DB-API 2.0) keeps: its Python API,
> its command line and its behaviour may change or break in any release, a
> minor or patch one included. Pin an exact seerdb version if you depend on it.

> **Not hardened for untrusted networks.** Run it on localhost or a network you
> trust, for clients you trust. See *Security* below for why.

The Mirror is a server that speaks Oracle's wire protocol. Oracle clients
(python-oracledb, seerdb itself, sqlplus, SeerODBC, anything built on Oracle's
thin or OCI protocol) connect to it as they would to an Oracle database, and it
answers them from a backend:

- **PostgreSQL.** Oracle SQL and PL/SQL are translated to PostgreSQL as they
  arrive, so an Oracle application can run against a PostgreSQL database
  unchanged, and move to native PostgreSQL a statement at a time.
- **A real Oracle database (passthrough).** The Mirror relays every call to an
  Oracle server and returns its answers. It exists for testing and capture:
  run a client's test suite through it, and any failure is a gap in the Mirror's
  protocol handling, not in SQL translation.

Its protocol details are in [PROTOCOL.md](PROTOCOL.md). This guide is about
running one.

## Installing

```sh
pip install "seerdb[mirror]"
```

The `mirror` extra adds psycopg 3.2 or later, which the PostgreSQL backend uses.
The passthrough needs nothing beyond seerdb. A plain `pip install seerdb` stays
client-only in its dependencies, and `import seerdb` never loads the Mirror.

## Starting one

`seerdb-mirror` (or `python -m seerdb.server`, the same program) starts a
Mirror in the foreground:

```sh
# Over PostgreSQL
MIRROR_USERS='scott:tiger' seerdb-mirror postgres 'host=db dbname=app user=mirror password=...'

# As a passthrough to a real Oracle
MIRROR_USERS='scott:tiger' seerdb-mirror passthrough oracle.example.com:1521/FREEPDB1
```

Then point a client at it:

```python
import oracledb
conn = oracledb.connect(user='scott', password='tiger', dsn='127.0.0.1:1521/ANY')
```

Any service name is accepted.

### Options

| option | default | meaning |
|---|---|---|
| `--host` | `127.0.0.1` | listen address; give `0.0.0.0` to accept other hosts |
| `--port` | `1521` | listen port |
| `--encryption` | `requested` | native network encryption (ANO): `requested` encrypts whenever the client offers AES, `required` refuses a client that doesn't, `accepted` never encrypts |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `--tls-cert` | | serve TCPS: the server's certificate chain, PEM (see *Encryption*) |
| `--tls-key` | | that certificate's private key, PEM |
| `--tls-ca` | | require client certificates issued by this CA, PEM |
| `--config FILE` | | an INI file with the same options (below) |

For `postgres`:

| option | default | meaning |
|---|---|---|
| `CONNINFO` | | a libpq connection string for the backing database |
| `--presents` | `23ai` | the Oracle release the Mirror presents: `23ai` or `12.1` |
| `--auth-conninfo` | | keep the accounts in the database, connecting as this role (see *Accounts*) |
| `--translation-report` | off | record each statement's translation in `sys.ora_translation_log` |

For `passthrough`:

| option | default | meaning |
|---|---|---|
| `UPSTREAM` | | `host:port/service` of the Oracle database |
| `--field-version` | probed | the protocol field version to present; by default the Mirror logs in to the upstream once at start-up and presents what it negotiates |

`seerdb-mirror observe` runs a bring-up tool instead: it accepts a client and
logs every packet the client sends, in hex. It answers nothing.

The command line wins over the config file, and the file over the defaults.

### The config file

```ini
[server]
host = 127.0.0.1
port = 1521
encryption = requested
log_level = INFO
; tls_cert = /etc/mirror/server.pem
; tls_key = /etc/mirror/server.key
; tls_ca = /etc/mirror/clients-ca.pem

[clients]
SCOTT = tiger

[backend]
type = postgres
conninfo = host=db dbname=app user=mirror password=...
presents = 23ai
; auth_conninfo = host=db dbname=app user=mirror_auth password=...
; translation_report = no

; For a passthrough instead:
; type = passthrough
; upstream = oracle.example.com:1521/FREEPDB1
; field_version = 24
```

```sh
seerdb-mirror --config mirror.ini
```

The file holds passwords, so keep it readable only by the account that runs
the Mirror.

### Accounts

Oracle's login (O5LOGON) is mutual: the server proves it knows the password as
the client does. So the Mirror has to hold each account's password, not just a
hash of it. It serves exactly the accounts it is given, from:

- the config file's `[clients]` section;
- `MIRROR_USERS`, a comma-separated list of `user:password` pairs;
- for the PostgreSQL backend with `--auth-conninfo`, a `seerdb_auth.accounts`
  table in the database (below).

Names fold to upper case, as Oracle's do. There is no default account, and a
Mirror with no accounts refuses to start. Passwords are never taken from the
command line, where other local users could read them.

Without `--auth-conninfo`, the accounts live in the Mirror process. A password
changed by a client, or a user created with `CREATE USER`, lasts until the
Mirror stops. With it, they live in the database and persist. That table
belongs to the auth role; the role the sessions run as must not be able to read
it, so it must be a different role, not a superuser, and not the table's owner.

### Encryption

**TLS (TCPS).** Given a certificate, the Mirror serves TCPS: every connection is
TLS from its first byte, as an Oracle TCPS listener's is, and the Oracle
protocol runs inside it.

```sh
seerdb-mirror --port 2484 --tls-cert server.pem --tls-key server.key postgres '...'
```

- **Versions.** TLS 1.2 and later.
- **Client certificates.** With `--tls-ca`, a client must present a
  certificate that CA issued (mutual TLS, as an Oracle wallet provides).
- **The certificate itself.** Clients check its DN or host name against what
  they expect (`ssl_server_cert_dn`, `ssl_server_dn_match`). Recent Python
  clients, python-oracledb's thin mode among them, also refuse a certificate
  without the standard extensions (such as an Authority Key Identifier), so
  issue it from a proper CA.
- **Mixing transports.** A listener serves either TCP or TCPS. Run two Mirrors
  to offer both.

TLS is the way to protect a Mirror that listens beyond localhost: unlike ANO, it
authenticates the server.

**Native network encryption (ANO).** AES-128, -192 or -256 with a SHA-2
checksum. By default (`requested`) the Mirror encrypts whenever the client
offers ANO, and serves a client that doesn't in plaintext. Give `--encryption
required` to serve only clients that encrypt, or `accepted` to never encrypt
(a client that requires encryption is then refused). seerdb's client offers ANO
by default; sqlplus and other OCI clients offer it when their `sqlnet.ora`
asks for it. ANO's key exchange does not authenticate the server, so it does
not protect against a man in the middle; use TLS for that.

## Mirror over PostgreSQL

### What the database needs

- **PostgreSQL**, tested on 16.
- **[orafce](https://github.com/orafce/orafce)**, which supplies Oracle's
  built-in functions (`NVL`, `DECODE`, `TO_CHAR`, `ADD_MONTHS`, ...). It is a
  requirement: without it, much ordinary Oracle SQL fails. The Mirror runs
  `CREATE EXTENSION IF NOT EXISTS orafce` when it connects; if its role may not
  create extensions, have an administrator create it in the database first.
- **[pgvector](https://github.com/pgvector/pgvector)**, optional, for Oracle's
  `VECTOR` type. Without it a `VECTOR` column is refused.

The repository's [`examples/mirror-pg.Dockerfile`](../examples/mirror-pg.Dockerfile)
builds PostgreSQL 16 with both, as CI uses it.

### Which role to connect as

**The role in the connection string is the Mirror's security boundary.** Every
account the Mirror serves runs as that one PostgreSQL role, and a client can
send native PostgreSQL through the Mirror (below). So whatever that role may do
in PostgreSQL, every Mirror user may do. Give the Mirror a role of its own:

- **not a superuser**;
- owning the database it serves, or at least its schemas, and nothing outside it;
- without `pg_read_server_files`, `pg_write_server_files` or
  `pg_execute_server_program`, unless you want the features that need them (BFILE
  needs `pg_read_server_files`, and with it every Mirror user can read any file
  that PostgreSQL's server process can).

Some features are better with a superuser and degrade without one:

- **Ordering a `TIMESTAMP WITH TIME ZONE` column.** Its operator classes need a
  superuser. Without them, comparison still works by instant.
- **Storing a `BOOLEAN` into a `NUMBER` column.** That needs a cast between
  built-in types, which needs a superuser. Without it, the assignment is refused.

Prefer the restricted role and accept these.

### One tenant per database

All Mirror accounts share the one PostgreSQL role, so they share its
privileges:

- **Schemas.** Each account gets a schema of its name, and its unqualified names
  resolve there, as an Oracle user's do. But a user can read and change another
  user's tables by naming the schema, whatever was granted. A `GRANT` on a type
  only decides whether another user can look the type up.
- **Accounts.** Today any account can manage accounts with `CREATE USER`,
  `ALTER USER` and `DROP USER`, including other users' passwords.

Treat one Mirror over one database as **one tenant**: users who would trust each
other with each other's data. Serve separate tenants from separate Mirrors over
separate databases, each with its own role.

### Native PostgreSQL

A statement can bypass translation and run as PostgreSQL:

```sql
SELECT /*+ PG */ CAST(jsonb_build_object('a', n) AS text) FROM generate_series(1, 3) AS g(n)
ALTER SESSION SET seerdb_dialect = 'postgres'   -- every statement native, until
ALTER SESSION SET seerdb_dialect = 'oracle'
```

This is how an application moves to PostgreSQL over the same connection, one
statement at a time. Binds stay Oracle's (`:name`, `:1`). Since an Oracle client
reads `::type` as a bind, write casts as `CAST(x AS type)`. A result of a type
with no Oracle form (`jsonb`, `uuid`, arrays, ...) is refused by name.

Native SQL is always available to every account, which is why the role matters.

## Passthrough

The passthrough presents what its upstream speaks: it logs in once at start-up
with the first account and presents the protocol version it negotiates, unless
`--field-version` sets one.

Each client session opens its own upstream session, with the same user name and
password the client logged in with. So every account the Mirror serves must
exist upstream with the same password. Where else it could get its accounts is
open (#1703).

Uses:

- **Testing a client.** Run its suite through the Mirror against a real database.
  A failure that the direct run doesn't show is a Mirror protocol gap.
- **Capture.** `--log-level DEBUG` logs each statement the Mirror dispatches,
  with how it decoded the call.
- **Migration.** Put the passthrough in front of the application today, and
  replace it with the PostgreSQL backend later, with no change to the
  application.

## Security

The Mirror is experimental, and a security review (#1687) found work still to
do before it can face clients you don't trust. Until those fixes land:

- **Who can reach it.** A client can tie the Mirror up before logging in: hold
  connections open, or send packets it handles badly. Keep the default listen
  address (`127.0.0.1`), or put the Mirror behind a firewall that admits only
  your own hosts.
- **Who logs in.** Every account can do more than an Oracle user of the same
  name could: manage the other accounts, including their passwords, and read
  any file the PostgreSQL role can, through DIRECTORY objects. Serve only
  accounts that may see each other's data (*One tenant per database*).
- **What the role may do.** Native SQL makes the PostgreSQL role the real
  boundary (*Which role to connect as*). Never a superuser; no server-file or
  server-program privileges unless you need them.
- **The wire.** Use TLS (`--tls-cert`) beyond localhost. ANO alone doesn't
  authenticate the server.

This section shrinks as the fixes land.

## Known limits

The PostgreSQL backend does not reproduce all of Oracle. The main limits:

- **Invalid UTF-8** cannot be stored in a string column (#1659).
- **Fractional seconds** stop at six digits; a `TIMESTAMP(9)` is a
  `TIMESTAMP(6)`.
- **`ROWNUM`** works as a top-N filter only. With `ORDER BY`, `GROUP BY`,
  `DISTINCT`, an aggregate, a set operator or `OR` at the same level, it is
  refused by name.
- **`ROWID`** is PostgreSQL's `ctid`, which changes when a row is updated or the
  table is vacuumed. `DBMS_ROWID` is not there.
- **PL/SQL packages, partly.** They have no package variables or initialization
  section. A `COMMIT` inside a routine waits until the call ends. A `ROLLBACK`
  inside one fails.
- **Object types** have no methods. A collection computed in a query fetches as
  a plain array.
- **Privileges** cover type lookup only (see *One tenant per database*).
  System privileges and roles are accepted and do nothing.
- **DIRECTORY and BFILE.** Files are read on the PostgreSQL host. `DBMS_LOB`'s
  file routines are not there.
- **Editions** are kept by name only; objects are not versioned per edition.
- **`VECTOR`** columns of `FLOAT32` hold at most 16000 dimensions, pgvector's
  limit (#1734).
- **SQL domains and annotations** are supported on columns only.
- **Sessionless transactions** resume only within the same Mirror process.
- **Transaction priority** is recorded and reported, but has no effect.
- **A quoted lower-case identifier**, such as `"t1"`, comes back upper-case.

The full list, with the reasons, is in the backend module's docstring
(`seerdb/server/backends/postgres.py`).

One limit applies to the Mirror itself: it serves thin clients from 10.2 on,
and sqlplus / OCI from 10.2 on. Oracle 8i and 9i clients are not served.
