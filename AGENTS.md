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

CI runs the offline suite and 23ai. Everything else — 8i, 9i, 10g, 11g, 21c, the
Mirror legs and the SQLAlchemy compliance suite — is **local only**, so a green
CI badge says little about a change to the codec or the login path. Run the
tiers your change can reach, and put the numbers in the PR description.

A docs-only change needs none of the test tiers; the repository's own workflows
skip them on those paths too.

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
bump; bug fixes are a **patch**. The experimental server-side "Mirror" API is
explicitly *not* covered by semver and may change in any release. A breaking
change to the client API (e.g. the `oracle` → `seerdb` package rename in 2.0.0)
is a **major** bump.
