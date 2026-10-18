# Contributing to seerdb

Thank you for considering a contribution. Before opening a pull request,
please read this document — seerdb's posture toward the proprietary
protocol it implements imposes some requirements that other open-source
projects don't have.

## Project posture: clean-room reverse-engineering

seerdb is an independent implementation of Oracle's TNS / TTC wire
protocol. The project has no relationship to Oracle Corporation and no
access to Oracle's proprietary protocol specifications. Every byte the
driver sends or decodes was derived from public artifacts and / or from
empirical observation of a running Oracle server that the contributor
was authorized to use.

To keep the project on solid legal footing — both for the maintainers
and for users who ship it — **contributions must follow clean-room
practice.** That means the rules below apply whether you are sending a
1-line typo fix or a 1000-line LOB implementation.

## What you may use as a reference

The following sources are explicitly fine to consult, cite, and derive
work from:

- **python-oracledb source** (Universal Permissive License 1.0 / Apache
  License 2.0). Reading the Cython implementation to understand a
  message layout is fine. Copying code wholesale is not — both
  licenses are permissive but require attribution, and direct copies
  would muddy seerdb's own license. Re-express what you learn in
  your own words / code.
- **cx_Oracle 5.x and earlier** under its old BSD-style license, same
  rules as above.
- **Independent, third-party reimplementations of the protocol** (in any
  language) published under a permissive open-source license (MIT, BSD,
  Apache, and the like). Same rules as above: consult them to understand
  a layout or to corroborate a capture-derived finding, re-express in
  your own code, and attribute any code you do borrow per its license.
  Treat them as a **cross-check against your own packet captures, not a
  primary source** — the wire facts you confirm are what matter, and the
  facts themselves are not copyrightable.
- **Public RFCs, conference talks, blog posts, academic papers,**
  packet-protocol guides, and anything else freely available on the
  web. Cite the URL in your commit message.
- **Wireshark / tcpdump packet captures** of an Oracle server the
  contributor is authorized to use, talking to any client (sqlplus,
  JDBC, sqlcl, oracledb itself). The wire bytes are facts; what they
  represent can be derived empirically.
- **Your own experiments** — sending crafted requests and reading the
  response — against an Oracle instance you have a valid license for.

## What you may NOT use as a reference

- **Oracle source code** (the C / Cython sources of OCI, the server,
  any proprietary client). If you have access to these via an Oracle
  employment, partnership, or NDA, you cannot contribute work derived
  from them. Period.
- **Decompiled or disassembled Oracle binaries.** Reverse-engineering
  against running behavior is fine; reading the binary's internals is
  not.
- **Oracle's documentation text verbatim.** In particular, do not paste
  Oracle's error message catalog — the strings like "ORA-00942: table
  or view does not exist" — into source code or comments as a static
  table. The driver should obtain those strings from the server at
  runtime (the OER block on the wire carries the text), not embed
  them. Brief paraphrases for technical explanation in comments are
  fine; copy-paste of documentation prose is not.
- **Protocol material obtained under an Oracle NDA**, support
  contract, partner program, or any other restricted distribution.
- **Internal Oracle wikis, Slack archives, training material, etc.**
  obtained through privileged channels.

If you are unsure whether something is OK to use, ask before
contributing rather than after.

## In your pull request

- **Cite your sources.** A one-line note in the commit message — e.g.
  "layout cross-referenced with python-oracledb's _process_X" or
  "derived from packet capture of sqlplus 19c against XE 11g" — is
  enough. The point is to leave a paper trail showing the work was
  derived from permissible material.
- **Don't ship Oracle docs prose.** Comments and docstrings should be
  your own description of what the code does and why.
- **Don't ship static error-message tables.** If you need to associate
  numeric ORA codes with PEP 249 exception subclasses or with branching
  behavior, the codes themselves are fine to embed (they're API
  constants); the human-readable strings must come from the server.
- Keep the existing MIT / SPDX header on new source files. The project
  is [REUSE](https://reuse.software/) compliant — see `REUSE.toml`.
- Add tests for new behavior. The offline test suite runs without a
  database; integration tests gated on `SEERDB_TEST_USER` are the
  right place for anything that needs to talk to a real Oracle. Cover every
  supported server version the feature reaches; where a version cannot have
  the scenario, skip the test there explicitly, with the reason, so it does
  not pass silently.
- **Keep `docs/PROTOCOL.md` current.** A change that fixes a codec, or adds,
  tightens or clarifies a wire layout, limit or edge case, updates the
  relevant section of `docs/PROTOCOL.md` in the same PR. It is the reference
  other implementations build on.
- **Link the issue so it closes itself.** Put `Closes #N` in the PR
  description and the commit message, one keyword per issue
  (`Closes #9, closes #15`; `Closes #9 and #15` closes only #9). A PR that
  only partly addresses an issue, or one of several PRs for it, says
  `Refs #N`, and the issue is closed by hand when the last one lands.

## Code conventions

- **When behaviour is ambiguous, match python-oracledb (thin):** API names,
  defaults, edge cases. The deliberate exceptions are listed in the README
  under *Intentional differences from python-oracledb*; a new one is added
  there, not left implicit.
- **Sync and async stay in step.** A feature added to `Connection`/`Cursor`
  lands in `AsyncConnection`/`AsyncCursor` in the same PR, with the same
  signature, defaults and exceptions, and with tests on both.
- **Field-version constants** are spelled `FIELD_VERSION_<major>_<minor>` and,
  for an extension, `FIELD_VERSION_<major>_<minor>_EXT_<n>`
  (`FIELD_VERSION_12_2_EXT_1`), even where another driver spells a name
  differently.
- **Pick a number's base from what it is.** Bit flags, masks, wire markers,
  type codes and packed versions are hex (`0x20`, `0x0B200200`); ORA error
  codes, lengths, counts, ports and field versions are decimal
  (`ORA_NO_DATA_FOUND = 1403`). Don't mix bases within one kind of value.
- **Move or remove code cleanly.** When a name moves or is deleted, leave no
  comment at the old place pointing to its new home.
- **Regexes that run on statement text must not backtrack.** The Mirror
  applies them to SQL from clients, so a catastrophic pattern is a denial of
  service, and CodeQL fails the PR on it. Write a block-comment body as
  `/\*(?:[^*]|\*(?!/))*\*/`, never a lazy `.*?` inside a repeated group, and
  time the pattern on pathological input such as `'/*' + '*//*' * 100_000`.

## Branches and pull-request scope

- **One isolated feature per pull request.** Keep each branch and PR to a
  single, self-contained change — one ticket, one concern. Don't bundle
  separable changes (e.g. several datatypes, or a fix plus an unrelated
  refactor) into one PR; split them so each can be reviewed and reverted on
  its own.
- **Open every PR against `master` — never against another feature branch.**
  Even when a follow-up genuinely builds on an as-yet-unmerged branch, set the
  PR's base to `master`. Branch off the dependency locally for the code if you
  need it, but the PR base stays `master`: until the dependency merges the
  follow-up's diff will also show its commits, and once it merges to `master`
  the diff narrows to just the follow-up. Basing a PR on another feature branch
  invites merging it into that branch by mistake instead of `master`, stranding
  the work off `master`.
- **One commit per PR, kept that way.** Fixes found while it is open, from
  review, CI or a failing test leg, are amended into that commit and
  force-pushed, not added as extra commits or a follow-up PR. A separate
  problem found along the way gets its own issue and its own PR.

## Developer Certificate of Origin

By submitting a pull request, you certify that:

1. The contribution was created in whole or in part by you, and you
   have the right to submit it under the project's MIT license; **or**
   it is based upon previous work that, to the best of your knowledge,
   is covered under an appropriate open-source license that allows you
   to modify it and submit it under the MIT license.
2. The contribution does not contain or derive from any Oracle
   proprietary material as described in the "What you may NOT use"
   section above.
3. The contribution is provided in good faith and you understand and
   agree that the contribution and a record of the contribution
   (including all metadata and the personal information you submit
   with it) is maintained indefinitely and may be redistributed
   consistent with the project's license.

A `Signed-off-by:` trailer in your commit message — easily added with
`git commit -s` — is a convenient way to attest to all three points.

## Not legal advice

This document describes the project's contribution policy and is not
legal advice. If you have any concern about whether you may contribute
specific work, consult a lawyer familiar with software licensing in
your jurisdiction before submitting.
