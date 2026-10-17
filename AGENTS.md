# Notes for coding agents

This file collects repeatable procedures for automated / AI agents working
on seerdb. For the project's clean-room posture and contribution rules, read
[`CONTRIBUTING.md`](CONTRIBUTING.md) first — those requirements always apply.

## Before you open a PR

Linting and type checking are expensive to run on GitHub, so they are **not**
what CI is for. Run them locally — they gate opening a PR:

```sh
ruff check seerdb tests
ruff format --check seerdb tests
python -m mypy
reuse lint
```

All four, every time, including before amending onto a PR that is already open.

Two things bite repeatedly:

- **`python -m mypy` takes no path.** Bare, it checks every file including the
  tests; `mypy seerdb` checks a third of them and has passed while the real gate
  failed. Prefer narrowing a type to silencing it — a wrong `# type: ignore[...]`
  error code is itself an error.
- **`reuse lint` sees untracked files.** A suite run leaves
  `mirror-passthrough.log` and `mirror-postgres.log` beside the checkout, and
  `reuse` flags them even though git ignores them. Delete them before linting,
  and never stage them.

### Tests

CI runs the offline suite on every change. Each integration leg runs only for the
paths it can reach (#1470):

- **Oracle legs** (11g, 18c, 21c, 23ai, 23ai at 12.1, and SQLAlchemy on 11g and
  23ai) run when `seerdb/client`, `seerdb/common`, the root package, the test
  harness or `pyproject.toml` changed. A Mirror-only change, or a new test in
  `tests/test_integration.py` alone, does not run them.
- **Mirror-over-PG legs** (the integration suite and SQLAlchemy through the
  Mirror) run for any change under `seerdb/` (the seerdb client drives the
  Mirror, so a client change can break it), `examples/`, or the integration and
  backend test modules. The passthrough backend
  (`seerdb/server/backends/passthrough.py`) is the exception: those legs never
  load it, and python-oracledb's suite through it runs weekly in CI and locally
  per PR.
- A **nightly** run takes every leg whatever changed.

8i, 9i, 10g, the passthrough Mirror and python-oracledb's own suite are **local
only** per PR. Locally, run what CI never runs for the paths your PR touches,
not the whole matrix (a PR touching several rows runs the union):

| PR touches | run locally |
|---|---|
| anything | the static checks above, before pushing |
| `seerdb/server/backends/postgres.py` (the Mirror-over-PG backend) or `examples/` | `tests/test_postgres_backend.py` before pushing, then python-oracledb's suite (`tools/run_oracledb_suite.py`) against a Mirror-over-PG |
| the rest of `seerdb/server` | python-oracledb's suite against a passthrough Mirror over 23ai, on the branch and on its `master` base, and against a Mirror-over-PG |
| `seerdb/client` | the integration suite on 8i, 9i and 10g |
| `seerdb/common` | the integration suite on every bed (8i to 26ai), both Mirror legs, the offline suite, and both python-oracledb suite runs |

Run these before the first push, and put the legs you ran and their numbers in
the PR. CI covers the rest for those paths, and the nightly run covers every CI
leg whatever changed; 8i, 9i and 10g run nowhere but locally, which is why a
client change still needs them.

CI minutes are a limited quota and local runs are free, and every push to a PR
re-runs every leg its paths reach (#1942). So:

- Push once the local legs are green, so the first CI run is the only one.
- Collect review fixes into one amend and one push.
- When two PRs touch the same code, open the second after the first merges,
  rebased once. Don't rebase a PR that still merges cleanly.
- A **draft** PR runs only the offline suite, typecheck, packaging, lint and
  REUSE. The Oracle and Mirror integration legs, both SQLAlchemy runs and
  CodeQL wait until it is marked ready for review, then run on every push.

A docs-only change needs none of the test tiers; the repository's own workflows
skip them on those paths too.

## Merging

Merging is the maintainer's, with one exception: an agent may merge a
**docs-only** PR itself, once its checks (lint, REUSE) have passed. Docs-only
means it touches nothing under `.github/`, `seerdb/`, `tests/`, `tools/` or
`examples/`, and not `pyproject.toml`: README, `docs/`, the ChangeLog, this file
and the like. Anything else waits for the maintainer, a release PR included.

## Cutting a release

seerdb releases publish to PyPI automatically via GitHub Actions Trusted
Publishing, triggered when the maintainer **pushes a version tag**. An agent's
job is to *prepare* the release on a branch and open the PR — never to merge or
tag it. The steps:

1. **Branch off `master`**, named `release-x.y.z` (e.g. `release-2.3.0`).

2. **Bump the version in both places — they must stay in sync:**
   - `pyproject.toml` → `version = "x.y.z"`
   - `seerdb/common/tns.py` → `_CLIENT_VERSION = 'x.y.z'`

   `_CLIENT_VERSION` is also packed into the `SESSION_CLIENT_VERSION` the driver
   sends on the wire, so a mismatch is a real bug, not just cosmetic.
   `seerdb.__version__` is that same string re-exported, so it needs no bump of
   its own; `tests/test_version.py` fails when the two places drift.

3. **Update the [`ChangeLog`](ChangeLog):** prepend a new `version x.y.z:` block
   at the top (releases are newest-first; bullets within a release run
   oldest-to-youngest). Each bullet is a short prose line — bold the headline
   phrase and cite the issue/PR as `(#NNN)`. Cover what changed since the last
   tag (`git log <last-tag>..master`), skipping dependabot / pure-CI noise.
   Mirror (`seerdb.server`) changes are listed too, from the release that
   first ships it (3.1.0), each bullet starting **Mirror (tech preview):** so
   nobody reads it as a promise the client keeps.

4. **Open a PR** against upstream `master` following the normal fork→upstream
   flow (push the branch to your fork, open the PR against `seerdb/seerdb`).
   Title it `Release x.y.z`. In the body, summarise the shipped features and
   state the validation status honestly (which tiers were tested live; call out
   anything validated only against the Mirror / a local bed rather than a real
   server).

5. **Stop there. Do NOT merge the PR, and do NOT create or push the tag.** The
   maintainer reviews, merges, runs a final live-matrix pass, and pushes the
   `x.y.z` tag. Tagging is what publishes to PyPI and creates the GitHub
   release — it is deliberately a human step.

### Compatibility with the SQLAlchemy dialect

The driver and `sqlalchemy-seerdb` release independently, so each one's suite
says what the other may rely on:

- **This driver must pass the released dialect's compliance suite**: the
  `SQLAlchemy` workflow checks out the dialect at its newest git tag, installs
  the driver from the checkout on top of it, and runs SQLAlchemy's dialect
  compliance suite against 11g and 23ai. It gates: a driver change that breaks
  what users have installed fails here.
- **The dialect, for its part, must pass against this driver's releases**,
  and only watches this driver's `master` as an early warning that does not
  gate. A failure on that leg is raised here, as a driver regression, unless
  it is a dialect change waiting for the next driver release.
- Release order follows: this driver releases first, then the dialect raises
  its `seerdb>=` floor and releases against it.

### Versioning

Follow semantic versioning against the **client (DB-API 2.0) surface**, which is
the stable public API. Additive, backward-compatible features are a **minor**
bump; bug fixes are a **patch**. A breaking change to the client API (e.g.
the `oracle` → `seerdb` package rename in 2.0.0) is a **major** bump.

The server-side "Mirror" (`seerdb.server`, the `mirror` extra) is a **highly
experimental technology preview** until its general availability: it ships with
the package but is *not* covered by semver, and its API, command line and
behaviour may change in any release. From GA on, semver covers the list in
docs/MIRROR.md *Stability* (#1861):
- the launcher's options and config keys, its defaults, and `seerdb.serve` /
  `seerdb.Server`;
- the `Backend` protocol and the types it exchanges;
- a deployment's database across upgrades within a major release;
- the migration profile's format.

The wire-level helpers `seerdb.server` exports, and what the backend keeps in
PostgreSQL, stay outside it.

**The Mirror-over-PG data version** (#1862) lives in
`seerdb/server/backends/postgres.py`:
- `_DATA_VERSION`: the version this release writes;
- `_DATA_BASELINE`: the oldest version it upgrades from;
- `_DATA_UPGRADES`: the steps, each `(version, SQL)` and idempotent;
- `_ADOPT_UNVERSIONED`: whether a database with no version is adopted.

How to use them:
- **The GA release** sets `_DATA_BASELINE = _DATA_VERSION`, bumped to a number
  of its own, and `_ADOPT_UNVERSIONED = False`. A preview database is then
  refused with "set it up afresh".
- **From GA on**, a change to what the records hold (a `sys.ora_*` table's
  columns or rows, the accounts) bumps `_DATA_VERSION` and adds the step that
  brings an existing database there, with a test that upgrades one.
- **Before GA**, the no-compatibility rule stands: no steps are written.

**Renaming or removing a Mirror launcher option or config key** follows the
deprecation policy in docs/MIRROR.md (*Deprecations*, #1863). It binds from the
Mirror's general availability on:

1. Add the old spelling to `_DEPRECATED_OPTIONS` (`--old`) or `_DEPRECATED_KEYS`
   (`(section, key)`) in `seerdb/server/launcher.py`. Name its replacement, or
   `None` for an option removed with nothing in its place, which then stays
   defined. Name the release that removes it, at least one minor release away.
2. Add a ChangeLog bullet naming it in backquotes with the word *deprecated*.
   `tests/test_server_launcher.py` checks that it is there.
3. Remove the entry and the old spelling in the release it names.
